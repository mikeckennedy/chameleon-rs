# Spike 1: the cost model

Phase 1 of [plan.md](plan.md). Throwaway code in `crates/spike` (its own `pyproject.toml`, not
in the workspace's default members, deleted at the Phase 2 exit), driven by `bench/spike1.py`;
every number below comes from that driver. Measured on Python 3.15.0 (GIL build), macOS
arm64, `--release` build, 2026-10-20. Ranges are min to max over three full runs; each figure
within a run is the best of seven `timeit` repeats (note that `timeit` disables the garbage
collector, which slightly flatters Chameleon's allocation-heavy code). Reviewed by the Rust
Architect.

## Summary

| Threshold (registered before the spike) | Result | Verdict |
|---|---|---|
| 1. Listing item at 0.6 µs or less | **442-460 ns** (Chameleon 2,909-3,598 ns), output byte-identical to Chameleon | **GO** (6.5-8x faster than Chameleon) |
| 2. Fast path at least 3x faster than one Python function per expression | **1.7-1.8x** on the item's expressions | **NO-GO.** D4 revisited and kept on threshold-1 evidence (see below) |
| 3. Suspend on a Python awaitable and resume, with cancellation | Correct output, cancellation, exceptions, reuse error; 100 concurrent renders overlap | **GO** for the concept; protocol gaps listed for Phase 11 |
| D2 Program / `VmState<Py<PyAny>>` / `Runtime` split | The generic VM compiled first time with no Python types in it; `Py` vs `Bound` storage costs nothing | **GO** for the structure; frames and unwinding not yet exercised |

## Real-world input: the Talk Python expression census

`bench/expression_census.py` recorded every expression the reference Chameleon compiled in the
229 Talk Python and Talk Python Training templates (all compile cleanly):

- 6,091 expressions: 5,765 default (`python`), 152 `load:`, 86 `structure:`, 71 `python:`,
  17 `string:`.
- Of 4,693 parsed Python expressions, **4,688 (99.9%) use only fast-path constructs**. The
  fallback is needed for 4 lambdas and 1 f-string.
- Most common nodes, which set the order the whitelist gets built in: `Name` (4,433),
  `Attribute` (1,643), `Constant` (1,225), `Call` (668), `not` (338), `Subscript` (224),
  `Compare` (222), `IfExp` (196), `BoolOp` (182).
- 1,150 parse attempts were syntax errors: the Interpolator's longest-candidate-first retry for
  `${...}` boundaries (D1). Compile time pays for these, so the `ExprCompiler` should reject
  impossible candidates structurally before calling `ast.parse`.

## [5] End to end: the listing loop

`bench/templates/home/listing_loop.pt` (the listing item without the layout), 500 videos, eight
expressions per item, static text pre-joined. Both spike programs produce output
**byte-identical** to Chameleon's; the driver asserts it before timing.

| Engine | ns per item |
|---|---|
| Chameleon | 2,909-3,598 |
| Hand-written Python (f-strings + `str.translate`) | 738-771 |
| Spike VM, every expression a Python function (fallback) | 722-759 |
| **Spike VM, fast path** | **442-460** |
| Spike VM, static text and loop only | 57-64 |

### [5b] Per expression

Eight copies of one expression per item, minus the empty loop; `fast` and `fallback` include
eval and emit. `Python eval` is the bare expression in Python bytecode, with no emit (a
reference for the irreducible work).

| Expression | Fast | Fallback | Saved | Ratio | Python eval |
|---|---|---|---|---|---|
| `video.id` | 27-28 ns | 56-58 ns | 29-30 ns | 2.1x | 9-10 ns |
| `video.title` (4 entities to escape) | 67-68 ns | 94-98 ns | 27-31 ns | 1.4x | 9-10 ns |
| `video.author` | 26 ns | 55 ns | 29 ns | 2.1x | 10 ns |
| `' '.join([])` | 21 ns | 51 ns | 30 ns | 2.4x | 12 ns |
| `"{:,}".format(video.views)` | 99-107 ns | 151-158 ns | 50-53 ns | 1.5x | 93-130 ns |
| `video.views` (exact int) | 29 ns | 59 ns | 30 ns | 2.1x | 10 ns |

The fast path saves a steady **27-53 ns per expression**. How much of the total that is depends
on the expression's own work: CPython's int formatting dominates `"{:,}".format` on either path,
and escaping dominates `video.title`.

### Optimizations tried in the spike (all keep Chameleon's semantics)

They took the item from 559 ns to the numbers above:

1. **Exact `int` formatted in Rust:** `i64` `Display` equals Python's `str(int)`; the exact-type
   check excludes `bool`; larger ints fall back to `str()`.
2. **Calls with 0-2 positional arguments through vectorcall** without a temporary tuple
   (arguments still evaluated left to right).
3. **Attributes of constants resolved at compile time** (`'{:,}'.format`, `' '.join`).
   Constants from `ast.parse` are immutable builtins (`str`, `bytes`, `int`, `float`,
   `complex`, `bool`, `None`, `Ellipsis`), so the bound method is the same every render; if the
   lookup fails it stays a runtime lookup, so the error is raised at render time. The only
   observable difference is a shared bound-method identity across renders. The real
   implementation folds **one level only**, on an original `ast.Constant` of an exact
   immutable type, in a separate `Folded` variant (the spike chains, so
   `'x'.join.__self__` would also fold).
4. **A gate scan, then table-driven escaping** in one pass. Chameleon's rule, verified against
   it by the driver: escape only if the string contains one of `& < > " '`; then replace `&`,
   `<`, `>` and the context's quote character. So `'a\0b'` stays raw in text while `'a&\0b'`
   becomes `a&amp;&#0;b` (text's quote is NUL); in a double-quoted attribute NUL is never
   replaced; a single-quoted attribute's quote becomes `&#39;` (the compiler supplies the quote
   and its entity, as `char2entity` does).

## Threshold 2: NO-GO, and what the revisit concluded

The registered threshold failed: 1.7-1.8x, not 3x. The prediction behind it was that
per-expression cost is mostly dispatch, which the fast path removes. It isn't: a large part is
the work itself (int formatting, escaping), which both paths pay, and Chameleon's own generated
code does route every attribute through `lookup_attr`, so the spike's fallback was a fair
stand-in for D4's fallback.

The plan says a NO-GO means revisiting D4. The alternatives, with the measured numbers:

- **Fallback only** (every expression a Python function): 722-759 ns per item. That **fails
  threshold 1** (0.6 µs) and is no faster than hand-written Python.
- **A narrower whitelist** saves little complexity: the census is dominated by `Name`,
  `Attribute`, `Constant` and `Call`, which carry the subtle semantics (`lookup_attr`, name
  resolution) anyway.
- **D4 as designed** (fast path plus exact fallback): 442-460 ns, the only option that meets the
  target. The fast path is worth about 30 ns on each of 8 expressions, roughly 40% of the item.

**Decision: keep D4,** building the whitelist in census-frequency order. Its revisit criterion is
replaced with an absolute one, registered now before Phase 2 measures anything: **revisit D4 if
a fallback-only build meets both the inline (0.6 µs) and partial (0.8 µs) targets, or if the fast
path saves less than 20 ns per expression on the benchmark mix.**

## [1] Name lookup and scopes (D5)

| | ns |
|---|---|
| Native two-level `Scope`, local hit | 3.4-4.6 |
| Native `Scope`, local miss, root hit | 5.7 |
| Native `Scope`, miss in both | 5.5 |
| Plain `dict` lookup (reference) | 2.8-3.7 |
| New root `Scope` from a 2-item dict, in Rust (what a native partial frame does) | 30 |
| `Scope(d)` from Python (native class) | 160 |
| `dict(d)` from Python (reference) | 36 |
| Chameleon `getname('video')`, including the Python call | 64-89 |

`#[pyclass(extends = PyDict, frozen)]` works with the root dict as a Rust field. Call
`dict.__init__` directly (`super(dict, self).__init__` skips `dict` itself). Building a scope
from Python is slow (160 ns) but native partial frames build it from Rust (30 ns).

## [2] Attribute access with `lookup_attr` semantics (D4)

| | ns |
|---|---|
| Rust `lookup_attr(model, "id")` | 12-16 |
| Rust `lookup_attr(dict, "id")` (getattr fails, falls back to `__getitem__`) | 119-188 |
| Chameleon `lookup_attr(model, "id")`, including the Python call | 33-47 |

The Chameleon figure includes a Python call; the Rust figures don't, so compare the dict case
with the model case rather than across rows. The dict case pays for creating and discarding an
`AttributeError`. Phase 10 candidate, same semantics: for an exact `dict`, `getattr` can only
succeed for attributes of the `dict` type, so check that set first and go straight to
`__getitem__`, creating the `AttributeError` only if a `KeyError` must be re-raised.

## [3] Fallback call

Calling a pre-built `f(getname, lookup_attr)` that evaluates `video.id`: 45-64 ns.

## [4] Text path (D6)

| | ns per string |
|---|---|
| ASCII, nothing to escape (emit + share of the final `str`) | 10.9 |
| ASCII with `& < > "` (attribute) | 45 |
| Non-ASCII, nothing to escape, UTF-8 view already cached | 30 |
| Python `list.append` + `''.join`, no escaping (reference) | 8.1 |

The non-ASCII figure is warm: the first `to_str()` on a fresh string also pays for creating
PyO3's cached UTF-8 view (19 ns via a Python loop, vs 28 ns for a non-caching `encode_utf8()`).

**Memory finding:** that cached view lives as long as the string. A 10,000-character Latin-1
string grows from 10,057 to 30,058 bytes (3x); ASCII strings have no cache. Per-request values
die with the request, so this mostly matters for long-lived values (cached fragments,
transcripts). Phase 10 plan: first try a safe heuristic (`encode_utf8()` above a length
threshold, `to_str()` below it); only if that isn't enough, add the `chameleon-capi` crate
(D12) with a kind-aware view that transcodes straight into the buffer.

## [6] Async (D9)

All assertions pass:

- A program emitting `fetch(1)`, a sync value and `fetch(2)` renders `a1b&lt;x&gt;2c` through
  `asyncio.create_task(render_async(...))`, where `render_async` is the `async def` wrapper
  (a bare native awaitable is rejected by `create_task`).
- An event-loop ticker kept running during the render (worst lag 0.18-0.29 ms).
- `task.cancel()` arrives as `throw()`, is delegated to the inner awaitable (its `finally` ran),
  and `CancelledError` propagates.
- An exception raised by an awaited value propagates out of the render.
- 100 concurrent renders, each awaiting 2 x 20 ms, finish in 45-50 ms (4,000 ms sequentially).
- Awaiting a finished render again raises `RuntimeError`.
- Async mode with nothing to await costs about 24 ns per item over sync mode (architect's
  measurement).

## [7] `Bound` vs `Py` in `VmState` (D2)

Store and read back: `Vec<Bound>` 1.8-2.1 ns, `Vec<Py>` (unbind, bind) 1.5 ns per value. No cost
to keeping render state `'static`.

## [8] Per-render wrapper overhead (D7)

| | ns |
|---|---|
| Bare native call `noop(**kw)` | 62 |
| Python wrapper doing `PageTemplate.render`'s setup, then native | 225-243 |
| Chameleon, full render of the smallest partial (`video_image.pt`) | 3,019-3,047 |

About 170 ns of Python wrapper is irrelevant once per page but would cost about 340 ns per item
(two partials per video) against a 0.8 µs budget. **Partials must be native frames and never go
through the Python `render()`** (D7, D10). Chameleon's fixed cost per partial render is about
3 µs, which is where the 10x partials target comes from.

## Not validated; carried into Phase 2 and later

The spike proved the shape and the costs, not the semantics. Each item below has to be done
properly in the real implementation (phase in brackets):

- **Escaping:** the gate rule above, with the quote and its entity supplied by the compiler
  (`&#39;`, `&quot;`, `&#0;`) [2].
- **Conversion** (spike shortcuts): `bytes` must be decoded strictly with the template's
  decoder (UnicodeDecodeError, overridable through `__decode`), not lossily; the result of
  `__html__()` is used as is, not passed through `str()`; lone surrogates (D6); the `default`
  marker and the translate step [2, 6a].
- **Loop semantics:** Chameleon takes the backup and assigns `None` to the loop names *before*
  evaluating the iterable (compiler.py:1863-1876); the spike does both after. The `repeat`
  variable (a `RepeatItem` per loop) is not in the spike, so its cost is unmeasured [2].
- **GC:** the native `Scope` leaks reference cycles through `root` (verified with a weakref:
  `root['child'] = child` is never collected). It needs `__traverse__` visiting `root`, relying
  on the inherited dict `tp_clear` to break cycles (a frozen class has no `&mut self` for
  `__clear__`); the upstream doctest's `gc.collect() == 0` must pass. `Program` and the
  awaitable need `__traverse__` too [2].
- **Awaitable detection:** must follow what `await` accepts: the type's `am_await` slot or a
  generator flagged as an iterable coroutine (`@types.coroutine`); not an instance-level
  `__await__`; errors must not be swallowed; exact-type early exits for `str`, `int`, `None`
  [11].
- **Coroutine protocol** (PEP 380 delegation): `throw(GeneratorExit)` closes the inner iterator
  and re-raises; `close()` throws `GeneratorExit` into the render, sets `done` even if the inner
  close raises, and detects an ignored `GeneratorExit`; `send(value)` on a fresh render is a
  `TypeError`; the traceback argument to `throw` is kept; a finalizer closes a render dropped
  while suspended; re-entry raises `ValueError("coroutine already executing")`; the output
  buffer is released when done [11].
- **Frames and the unwind stack** (D3): macro and partial frames, define restore, on-error
  rollback, translation marks, errors with positions, keyword calls. The largest unproven
  *design* risk: frames and unwind entries that stay resumable and are dropped correctly on
  cancellation, with `tal:on-error` not catching `CancelledError` [2, 6a, 6b, 11].
- **Partial frames:** the largest unproven *performance* risk is the 0.8 µs-per-item partials
  target with two partial renders per item. A Rust-built scope costs 30 ns; the frame push,
  keyword-argument handling and nested buffer use are not measured yet [2 gate, 9].

## Amendments to the decision record

- **D4:** threshold 2 recorded as NO-GO; D4 kept on threshold-1 evidence; new absolute revisit
  criterion (above); whitelist built in census-frequency order; one-level compile-time
  folding of attributes on constants; fixed-arity calls via vectorcall; structural pre-filter
  for Interpolator candidates.
- **D6:** Chameleon's escaping gate and quote rules (above); exact `int` via `i64` `Display`;
  the UTF-8 cache memory finding and its Phase 10 plan.
- **D5:** confirmed; construct through `dict.__init__`; `__traverse__` required.
- **D2, D3, D7, D9:** confirmed as designed, with the protocol and unwind work listed above.
