# chameleon-rs architecture

This is the decision record. Each decision says what we chose, why, and what would make us
revisit it. The phased build plan lives in [plan.md](plan.md).

Status legend: **Proposed** (drafted), **Reviewed** (Rust Architect reviewed, changes folded
in, awaiting Michael's sign-off), **Accepted**. Every decision below was **Accepted** on
2026-10-20; later changes are recorded as amendments with their date. Behaviors described as "verified" were checked by running the reference
Chameleon, not just by reading it.

## Context

[Chameleon](https://github.com/malthe/chameleon) compiles a page template into a Python
module: the generated code appends each static string and each escaped expression result to
a list, and joins it. Our baseline (`bench/`, Python 3.15) showed that on a typical listing
item **about 94% of render time is the engine's own work** (25 small appends, a `getname()`
call per variable use, a `__quote()` call with a regex scan per expression), and only 6% is
the user's expressions. A hand-written Python renderer is 4x faster than Chameleon. Compiling
a template takes Chameleon a median of 3.5 ms (up to 12 ms).

Goals, in order:

1. **Compatibility.** Same syntax, same output, same API. At least 95% of Chameleon's own test
   suite passes unmodified (216 of the 227 tests that pass on the reference engine), and real
   sites (Talk Python) produce byte-identical HTML.
2. **Speed.** At least 5x faster renders on the listing benchmark, and at least 10x where
   `render_partial` is used.
3. **New capabilities, opt-in.** `render_async`, concurrent slow expressions, built-in partials,
   free-threaded Python. Default behavior stays exactly Chameleon's.

Non-goals: PyPy (Chameleon supports it; we target CPython 3.12+), Zope/Plone packages
(`z3c.pt`, `five.pt`) that subclass Chameleon's compiler internals, and the `.source`
attribute containing generated Python code (see D11).

## D1. Workspace shape: a pure-Rust core and a PyO3 shell

```
crates/chameleon-core   pure Rust, no Python: source/spans, tokenizer, parser, ZPT program
                        builder, compiler to bytecode, VM, escaping, output buffer, errors
crates/chameleon-py     cdylib `chameleon_rs._native`: the Python Runtime and ExprCompiler,
                        native pyclasses (Program, Scope, Macro, awaitable)
python/chameleon_rs     the Python package: public API classes, compat modules, the bridge
                        for custom expression types, exception formatting
```

Dependency direction: `chameleon-py -> chameleon-core`. The core never sees a `PyObject`.

The core is Python-free but not expression-blind **at compile time**: Chameleon's
`Interpolator` decides where each `${...}` ends by trying candidate substrings with the
expression parser, longest first (compiler.py:336-385). So the program builder takes an
injected compile-time trait, the counterpart of the run-time `Runtime` (D2):

```rust
pub trait ExprCompiler {
    type Error;
    /// Parse and register one expression. `Err(Invalid)` means "not a valid expression" and
    /// drives the Interpolator's retry; other errors abort compilation.
    fn compile(&mut self, kind: ExprKind, src: &str, span: Span) -> Result<ExprId, ExprCompileError<Self::Error>>;
}
```

Core tests use a toy implementation (balanced braces, identifiers); `chameleon-py` implements
it with CPython's `ast` (D4).

Why: the parts that must be byte-exact with Chameleon (tokenizing, parsing, attribute handling,
whitespace rules) are functions of the template text and test in microseconds without an
interpreter. This is the house "thick core, thin shell" rule with Python as the shell.

## D2. Program / VmState / Runtime: the VM lives in the core

The VM is split into three parts so that no Python lifetime (`'py`) ever appears on it:

```rust
/// Immutable compiled template: blocks, instructions, static strings, expression ids.
/// Send + Sync; shared through Arc.
pub struct Program { /* ... */ }

/// Per-render state. Holds only `'static` values (Py<PyAny> in chameleon-py), so a
/// suspended render can be stored inside a pyclass (D9).
pub struct VmState<V> { frames: Vec<Frame>, unwind: Vec<Unwind<V>>, out: Output /* ... */ }

pub trait Runtime {
    type Value;               // Py<PyAny> in chameleon-py
    type Error;               // wraps PyErr untouched (D8)
    fn eval(&mut self, e: ExprId) -> Result<Step<Self::Value>, Self::Error>;
    fn emit(&mut self, v: Self::Value, esc: Escape, out: &mut Output) -> Result<Emitted, Self::Error>;
    fn truthy(&mut self, v: &Self::Value) -> Result<bool, Self::Error>;
    fn define(&mut self, name: NameId, v: Self::Value, scope: DefScope) -> Result<Option<Self::Value>, Self::Error>;
    fn restore(&mut self, name: NameId, backup: Option<Self::Value>) -> Result<(), Self::Error>;
    fn iter_start(&mut self, name: NameId, v: Self::Value) -> Result<Self::Value, Self::Error>;
    fn iter_next(&mut self, it: &Self::Value) -> Result<Option<Self::Value>, Self::Error>;
    fn translate(&mut self, req: TranslateReq<'_, Self::Value>) -> Result<Self::Value, Self::Error>;
    fn is_catchable(&self, e: &Self::Error) -> bool;   // Exception, not BaseException (D3)
    // macro resolution, scope push/pop, error-info binding ...
}

pub enum Step<V> { Ready(V), Suspend }

pub fn run<R: Runtime>(p: &Program, st: &mut VmState<R::Value>, rt: &mut R) -> Result<Done, VmError<R::Error>>;
```

- `PyRuntime<'py>` holds `Python<'py>` and the bound scope, and is rebuilt on every resume.
- `VmState` stores `Py<PyAny>`; `unbind()` / `bind(py)` are free. Eval and emit are fused into
  one instruction (`EmitExpr`) so most values never land in `VmState` at all; only loop
  iterators, define backups and macro scopes do.
- The toy runtime in core tests exercises **control flow only** (repeat whitespace, on-error
  rollback, slot filling, translation marks). Conversion and escaping semantics are tested
  through Python, against the reference.

Revisit if the toy runtime starts re-implementing Python semantics to make VM tests pass;
then the VM moves into `chameleon-py` and only the compiler stays in the core.

## D3. Execution model: flat, resumable bytecode VM

The compiler lowers each macro (and the main body) into a flat instruction block: static text
(pre-joined at compile time), `EmitExpr`, branches and jumps, loop instructions, define and
restore, attribute emits, translation marks, macro calls, slot dispatch. The VM runs with an
explicit frame stack and a single unwind stack, not recursive Rust calls.

1. **One unwind stack**, like CPython's block stack. Entries: `Define{name, backup}`,
   `Loop{iter, repeat}`, `Try{handler_pc, out_mark, unwind_depth}`, `Translate{out_mark,
   names}`, `Macro{...}`, `SlotPush{name}`. Normal exits, errors and async cancellation all
   unwind through it.
2. **`on-error` copies Chameleon exactly** (verified, compiler.py:1287): it catches only
   `Exception` (so `CancelledError`, `KeyboardInterrupt`, `SystemExit` pass through), truncates
   output to the mark, binds `ErrorInfo` as `error`, calls `on_error_handler`, then renders the
   fallback. Inner `Define` entries are **dropped without restoring**, because Chameleon's
   generated code has no try/finally: `<div tal:on-error="string:ERR"><span tal:define="v 1">
   ${1/0}</span></div>|${v}` renders `<div>ERR</div>|1`. A "clean" restore-on-unwind would
   produce different output.
3. **Frames are for macro calls, slot bodies and native partials only.** Conditions, loops and
   switch are jumps within a block. A frame is `(program: Arc<Program>, block, pc, scope)`.
4. **Macros from other templates:** if the evaluated macro object downcasts to the native
   `Macro` pyclass, the VM pushes a frame and never calls into Python. Anything else falls back
   to the Python `include()` protocol. Native frames are what make the partials target and
   async possible (a macro called through Python cannot suspend).
5. **Slots:** Chameleon keeps `econtext['__slot_x']` deques and runs fill bodies with
   `econtext.copy()` from the define-slot site (compiler.py:1688-1694). We keep a native
   per-name slot stack in `VmState` with `SlotPush` unwind entries, and do not mirror the
   `__slot_*` keys into the scope dict (listed in D11).
6. **Output marks, not sub-buffers.** Translation and on-error use `out.mark()`,
   `out.take_since(mark)` and `out.truncate(mark)` on the single buffer, so they compose. The
   representation stays behind that API because D9's placeholders turn the buffer into a list
   of segments.
7. **Recursion limit.** An explicit stack would turn infinite macro recursion into unbounded
   memory growth; a frame-depth limit raises `RecursionError` instead, as Chameleon does (the
   upstream suite checks this, and checks it is *not* wrapped in `RenderError`).

Why: pre-joined static text makes most of a page a few `memcpy`s; the explicit stack is what
lets the VM suspend at an await point and resume (D9); no recursion depth issues.

Rejected: generating optimized Python source like Chameleon (caps us near hand-written-Python
speed, gives up the Rust buffer and escaping wins); a recursive tree-walking interpreter
(cannot suspend for async).

## D4. Expressions: CPython's parser, a Rust fast path, an exact fallback

At compile time every `python:` expression is parsed with **CPython's own `ast` module**, so
the grammar always matches the running Python. Then:

- **Fast path:** a strict whitelist keyed on AST node type (names in Load context, constants,
  attribute access, subscripts, calls, unary/binary/boolean operators, comparisons,
  conditional expressions, tuple/list/dict literals) is lowered to a Rust expression tree
  evaluated with the same C-API calls the interpreter uses. Anything not on the whitelist
  falls back.
- **Fallback:** everything else (comprehensions, lambdas, walrus, generators, f-strings at
  first, 3.14 t-strings, `<?python ?>` code blocks, any Store context) becomes a real Python
  function built with **Chameleon's name-rewriting rules ported exactly**, so scoping quirks
  match, including comprehension variables being written into the template scope.

Semantics the fast path must reproduce (from the reference):

- **Attribute access is `lookup_attr`** (utils.py:515): `getattr` first; on `AttributeError`,
  look up `__getitem__` *as an attribute* (not `PyMapping_Check`) and call it; a `KeyError`
  from that call re-raises the *original* `AttributeError`; any other exception propagates
  (`[1].foo` raises `TypeError` from `[1]['foo']`, which changes which `|` alternatives fire).
- **Names follow `NameTransform`:** ordinary names are `getname(x)` (scope local dict, then
  root, else `NameError`); builtins are `get('len', len)` (a render keyword overrides the
  builtin); `__`-prefixed and internal names are left alone; `default` and `attrs` are
  compile-time aliases.
- **Evaluation order** strictly left to right, including call arguments, keyword arguments,
  `**` merging and dict displays; chained comparisons evaluate the middle operand once and
  short-circuit; `and`/`or` return an operand; conditional expressions are lazy.
- **Constants** keep the `ast.Constant` value objects (`Py<PyAny>`), so identity matches.
- **No reference cycles:** fallback functions' `__globals__` never contain the template object
  (template -> Program -> function -> globals -> template would leak on every `auto_reload`);
  `template` and `macros` are passed per render. Every pyclass holding `Py` fields implements
  `__traverse__` / `__clear__`.

The whitelist is built in the order the Talk Python census ranks node types (`Name`,
`Attribute`, `Constant`, `Call`, `not`, `Subscript`, `Compare`, `IfExp`, `BoolOp`, ...); 99.9% of
the 4,693 real expressions qualify. Attributes of an original `ast.Constant` of an exact
immutable type are resolved once at compile time (one level, a separate `Folded` node; a failed
lookup stays a runtime lookup). Calls with few positional arguments use vectorcall without a
temporary tuple. The `ExprCompiler` rejects impossible Interpolator candidates structurally
before calling `ast.parse` (1,150 of the census's parse attempts were such candidates).

*Amended 2026-10-20 after [spike 1](spike-1.md):* the spike's 3x threshold failed (1.7-1.8x),
because much of each expression's cost is CPython's own work on either path. D4 was revisited
and kept: a fallback-only VM (722-759 ns per listing item) misses the 0.6 µs target, the fast
path (442-460 ns) meets it. **Revisit D4 if a fallback-only build meets both the inline (0.6 µs)
and partial (0.8 µs) targets, or if the fast path saves less than 20 ns per expression on the
benchmark mix** (measured: 27-53 ns).

## D5. The scope is a native dict subclass

Chameleon's `Scope` is a two-level mapping: the local dict, then a shared `_root` dict that
holds globals (utils.py:382). We implement it as `#[pyclass(extends = PyDict)]` with `root:
Option<Py<PyDict>>` as a Rust field, overriding `__getitem__`, `get`, `__contains__`,
`__iter__`, `copy`, `set_global` and `get_name`. The VM does at most two direct dict lookups
on interned keys; no `getattr("_root")`.

Why it stays a dict: user code can see `econtext`, fallback expressions and foreign macros
need a real mapping, and `define`'s backup/restore is defined in terms of it. The upstream
`Scope` doctest checks `gc.collect() == 0`, so `Scope` implements `__traverse__` (visiting
`root`; the inherited dict `tp_clear` breaks cycles, since a frozen class has no `__clear__`),
and is constructed through `dict.__init__`. Spike 1: 3-6 ns per lookup vs 64-89 ns for
Chameleon's `getname()`; 30 ns to build a fresh scope from Rust. Every macro call and slot body pays
an O(n) `copy()`, as in Chameleon; partials don't, because they start from a small new scope.

Revisit if scope traffic dominates profiles; next step would be compile-time slots for loop
and define variables, with a dict view materialized on demand.

## D6. Output buffer and value conversion

- **Buffer:** a `Vec<u8>` of UTF-8 plus a `has_surrogates` flag. Python `str` values are read
  through their UTF-8 view; if that fails (lone surrogates, which Chameleon renders without
  complaint, verified) the value is encoded with `surrogatepass` and the flag is set. The
  final `str` is built once: a plain UTF-8 decode normally, a `surrogatepass` decode when the
  flag is set. The common path costs nothing.
- **Memory:** `PyUnicode_AsUTF8AndSize` caches a UTF-8 copy inside every non-ASCII `str` for
  its lifetime (spike 1: a 10,000-character Latin-1 string grows 3x). Use `to_str()` for now;
  in Phase 10 try `encode_utf8()` above a length threshold, and only if that isn't enough add
  the `chameleon-capi` crate (D12) with a kind-aware view.
- **Conversion uses exact types, like `__convert`/`__quote` (verified):** exact `str` is
  escaped; exact `float` uses `str()` (never Rust formatting: Python prints `1e16` as
  `1e+16`); an exact `int` that fits `i64` uses Rust's `Display`, which matches `str(int)`,
  else `str()`; `None` drops; the `default` marker means the static default; `bytes` are
  decoded strictly with the template's decoder (`__decode`); the result of `__html__()` is
  used as is; anything else, including
  `str` subclasses and `bool`, goes `__html__` -> translate -> `str()`. That is why a
  `(str, Enum)` member renders as `E.A` and `True` renders as `True`.
- **Escaping (verified, amended after spike 1):** Chameleon escapes only if the string
  contains one of `& < > " '` (its gate regex); then `&`, `<`, `>` and the context's quote
  character are replaced, with the quote's entity supplied by the compiler (`char2entity`):
  NUL -> `&#0;` in text, `"` -> `&quot;`, `'` -> `&#39;`. So `'a\0b'` stays raw in text while
  `'a&\0b'` becomes `a&amp;&#0;b`. No entity lookahead. A gate scan, then one table-driven
  pass; no gate character means one copy.
- **Capacity hint:** each `Program` keeps the last render's size in an `AtomicUsize`
  (lock-free, fine under free-threading).

## D7. Public API in Python, hot path in Rust

`PageTemplate`, `PageTemplateFile`, `PageTextTemplate(File)`, `PageTemplateLoader`,
`Macros`/`Macro` and the exception classes are Python classes in `chameleon_rs`, mirroring
Chameleon's attributes and config keywords, because users **subclass** them and set class
attributes (`expression_types`, `boolean_attributes`, ...). Native surface:

- `#[pyclass(frozen)] Program`: `Arc<core::Program>` plus the expression table (fast-path
  trees, fallback functions as `Py<PyAny>`, interned names). Compile-time settings are baked in.
- `#[pyclass(frozen)] RenderConfig`: render-time settings (translate, on_error_handler,
  value_repr).
- `#[pyclass(frozen)] Macro { program: Py<Program>, index }`: enables native macro calls.
- `Scope` (D5) and the awaitable (D9, the one non-frozen class; PyO3's borrow flag gives the
  "already executing" error).

Frozen classes with `Arc`/`Py` fields are `Send + Sync`, which PyO3 enforces for
free-threaded builds. No `RefCell`/`Cell`/unsynchronized caches. The Python layer guards
cooking and `auto_reload` with a `threading.Lock`. Partials never go back through the Python
`render()` wrapper.

`chameleon_rs.compat.install()` registers the compat modules under the name `chameleon` in
`sys.modules`, so `from chameleon import PageTemplateLoader` (fastapi-chameleon,
chameleon-flask, chameleon-robyn, pyramid_chameleon) runs unchanged. The compat package also
provides the internals the upstream tests import (`chameleon.tokenize.Token`,
`chameleon.parser.ElementParser`, `chameleon.utils.Scope`, ...), backed by the Rust
implementation; Chameleon's pure-Python utility modules (`astutil`, `codegen`, `exc`, parts of
`utils`) are vendored rather than rewritten. The compat package never imports the real
Chameleon.

Revisit if the wrapper's per-render overhead (measured in the spike) shows up in benchmarks;
then `render()` setup moves into Rust while the classes stay subclassable.

## D8. Errors

- **Compile time:** core errors are `thiserror` enums carrying `Span { start: u32, end: u32 }`
  (byte offsets). Conversion to Chameleon's classes (`ParseError`, `LanguageError`,
  `ExpressionError`, `TranslationError`, ...) computes code-point offsets lazily on the error
  path (`src[..b].chars().count()`); the upstream tests check `exc.offset` and the `(line N:
  col M)` text.
- **Render time:** `VmError<E>` carries the `PyErr` **untouched** (never converted or
  stringified), so Chameleon's dynamic `RenderError` subclass of `type(exc)` keeps `except
  KeyError` working. The VM tracks the current expression; each macro boundary appends
  `(expression, line, column, filename, exception)`; the top-level render builds the formatted
  exception with Chameleon's `ExceptionFormatter` logic in Python (cold path, exact parity).
- **Panics:** PyO3 turns a panic into `PanicException`, a `BaseException` that web frameworks'
  `except Exception` handlers miss. Every compile and render entry point runs inside
  `catch_unwind(AssertUnwindSafe(..))` and raises `chameleon_rs.InternalError(RuntimeError)`.
  Continuing afterwards is safe: `Program` is immutable and render state is per call.
- Byte-index slicing is the likeliest panic source in a parser over Unicode input: the core
  enables `clippy::string_slice` (warn) and uses `str::get` outside hot loops.
- `chameleon-py` uses `PyErr` as its error type; it doesn't need `thiserror`.

## D9. Concurrency and async: opt-in, built on D3

- `render_async()` is an `async def` in the Python class (`return await
  self._program.render_async(...)`), because `asyncio.create_task` and `TaskGroup.create_task`
  reject non-coroutine awaitables (verified). The native awaitable it awaits suspends the VM
  when an expression's **final value** is awaitable, delegates `send` / `throw` / `close` to
  the inner `__await__()` iterator like `yield from` (cancellation arrives through `throw`),
  and resumes with the result. Only final values are awaited: `coro().title` is an
  `AttributeError` on the coroutine; `tal:condition="coro()"` is awaited before the truth test.
  Async iterables in `repeat` are decided in Phase 11. Nested renders that go through Python
  (a fallback expression calling another template) stay synchronous. PyO3's
  `experimental-async` is not used: it drives Rust futures, not Python awaitables.
- Expressions marked concurrent (a decorator on the helper, or template syntax decided in that
  phase) can be **started early**, as soon as their free variables exist, with a placeholder in
  the output filled in when they finish. This changes evaluation order, so it is strictly
  opt-in.
- Built-in `render_partial` calls are independent by construction (keyword arguments only),
  so they are the first candidates for fan-out.
- Free-threaded Python (3.14t/3.15t): the module declares `gil_used = false` from day one;
  every pyclass is `Sync`, all render state is per call.

## D10. Built-in partials

`render_partial(path, **kwargs)` is available in every template without setup, with
chameleon_partials' semantics (the partial sees only its keyword arguments plus
`render_partial`). Path resolution: the loader's search path first, then walking up from the
calling template's directory; resolved once and cached. A partial renders as a native frame
writing straight into the caller's buffer. The old `chameleon_partials.render_partial` passed
in a model is recognized and routed to the native path.

## D11. Known, deliberate differences

- `template.source` (generated Python source) does not exist; with `keep_source=True` we expose
  a readable dump of the compiled program. Upstream tests only print it in failure messages.
- Slot fill functions are kept natively, so the scope has no `__slot_*` keys.
- Custom expression types (classes returning Python AST statements) work through a bridge
  that wraps their AST in a Python function, at fallback speed. Types that depend on Chameleon
  compiler internals beyond that interface are out of scope.
- `CHAMELEON_CACHE` (on-disk compiled modules) is replaced by an optional on-disk cache of the
  compiled program, only if compile time turns out to matter.

## D12. Toolchain and conventions

- Rust 1.99.0 pinned in `rust-toolchain.toml`; MSRV 1.90 (`rust-version`, above PyO3's 1.85)
  checked in CI. Edition 2024. `.cargo/config.toml` pins `PYO3_PYTHON` to `venv/bin/python` so
  cargo, clippy, rust-analyzer and maturin link the same libpython.
- PyO3 0.30 without the deprecated `extension-module` feature (maturin 1.15 sets
  `PYO3_BUILD_EXTENSION_MODULE`). Python 3.12+, per-version wheels (not abi3).
- Workspace lints: `unsafe_code = "forbid"` (compatible with PyO3's macros), clippy pedantic,
  `unwrap_used = "deny"`. If raw FFI is ever needed for speed, it goes in a **separate crate**
  (`crates/chameleon-capi`) with its own lint table (`unsafe_code = "deny"` with per-function
  `#[allow]`, `unsafe_op_in_unsafe_fn`, `clippy::undocumented_unsafe_blocks` and
  `clippy::multiple_unsafe_ops_per_block` denied), exposing only safe functions; `forbid`
  can't be relaxed per module. Created only if the spike shows a measured gap; without abi3,
  PyO3's safe API already uses the fast C-API paths.
- Build profiles: `dev-opt` for the Python test loop, `release` (fat LTO) for benchmarks and
  wheels, `profiling` (release with symbols) for py-spy / Instruments.

## D13. Testing strategy

- **`chameleon-core`:** pure Rust tests with toy `Runtime`/`ExprCompiler`; `insta` snapshots for
  program and bytecode dumps (regression guards, not proof of compatibility); `proptest` for
  invariants (no panics, spans cover the input, tokens concatenate back to the input);
  `cargo-fuzz` on the tokenizer and parser for panics.
- **`chameleon-py`:** a few embedded-interpreter tests that use only the standard library (the
  embedded interpreter does not see the venv, verified), via a dev-dependency on
  `pyo3/auto-initialize`.
- **Differential tests live in pytest**, with the real `chameleon` and `chameleon_rs` imported
  in the same process. Internals (token lists with positions, element trees, program dumps)
  are exposed through `chameleon_rs._native._testing`. **Hypothesis** generates inputs for
  differential fuzzing against the reference (the reference engine is the oracle).
  `tests/python` never calls `compat.install()`.
- **Upstream ratchet:** `tests/upstream/expected_failures_rust.txt` lists every upstream test
  the Rust engine does not pass, each with a reason, applied as strict xfails. A listed test
  that starts passing fails the run; the environmental xfails are strict too.
- **Real templates early:** Talk Python's templates join the tokenizer and parser differential
  corpus in the tokenizer phase, not at the end.
