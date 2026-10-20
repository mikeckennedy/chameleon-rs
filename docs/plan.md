# chameleon-rs build plan

How we get from an empty workspace to a drop-in Chameleon replacement. Decisions referenced as
D1..D13 are in [architecture.md](architecture.md).

## How we work

- **One phase at a time.** Each phase ends with an exit gate (tests that must pass, numbers
  that must hold), a Rust Architect review, a commit, and a checkpoint with Michael before the
  next phase starts.
- **The reference implementation is the spec.** When behavior is unclear we read Chameleon's
  source (`temp/chameleon`, pinned at `90b9002`) and, better, run it. Every core component gets
  a **differential test** that feeds the same input to Chameleon and to us and compares the
  result exactly (D13).
- **Ported code says so.** Files that port Chameleon logic carry a header naming the source
  file and noting it was modified (LICENSE.txt clause 4).
- **Every phase keeps `make lint test` green**, locally and in CI.
- **Upstream tracking:** the reference commits are pinned in the Makefile. Moving to a newer
  Chameleon is a deliberate step: bump the pin, re-vendor `tests/upstream`, re-run the
  baselines, review the diff of fixture outputs.

## How compatibility is measured

| Level | What | Target |
|---|---|---|
| 1. Upstream suite | Chameleon's 233 tests, vendored unmodified, run with `CHAMELEON_ENGINE=rust`; misses listed with reasons in `expected_failures_rust.txt` (strict xfail ratchet) | at least 216 of the 227 that pass on the reference (95%) |
| 2. Fixtures, byte-exact | Every `inputs/*.pt` / `*.txt` rendered by both engines, compared byte for byte (stricter than upstream's doctest-style comparison) | 100% |
| 3. Differential fuzzing | Hypothesis-generated templates and markup through both engines: tokens, trees, output, error type and position | no unexplained differences |
| 4. Integration suites | fastapi-chameleon, chameleon-flask, chameleon-robyn, chameleon_partials suites with the compat alias | all pass |
| 5. Real site | Every Talk Python page rendered by both engines, HTML compared word for word; shadow mode on real traffic | identical |

Upstream inventory (227 passing): 122 fixture renders, about 70 API and configuration tests
(loader, encoding sniffing, boolean attributes, errors, debug flags), and about 35 that import
internals by name (`chameleon.tokenize.Token`, `chameleon.parser.ElementParser`,
`chameleon.utils.Scope`, `chameleon.exc.ExceptionFormatter`, `chameleon.astutil`,
`chameleon.codegen.template`, `chameleon.compiler.ExpressionEvaluator`). The compat package
(D7) provides those, so the 95% target is reachable.

## How speed is measured

`bench/` holds the baseline (`make bench`). Each phase that touches rendering re-runs it
against both engines. Python 3.15, M-series Mac:

| Scenario | Chameleon | Target |
|---|---|---|
| Inline listing, per item | 3.1 µs | 0.6 µs or less (5x+) |
| `render_partial` listing, per item | 7.9 µs | 0.8 µs or less (10x+) |
| Hand-written Python (sanity floor) | 0.77 µs | we must beat it |
| Compile time per template | 3.5 ms median, 12 ms max | no slower; expected far faster |

Rust micro-benchmarks (criterion) cover the tokenizer, escaping and the VM loop.

---

## Phase 0: Project setup (done)

- [x] Cargo workspace (`chameleon-core`, `chameleon-py`), workspace lints, rustfmt, clippy,
      cargo-deny; `unsafe_code = "forbid"` verified with PyO3 0.30 macros.
- [x] Toolchain pinned (`rust-toolchain.toml`), `PYO3_PYTHON` pinned (`.cargo/config.toml`),
      `gil_used = false` explicit, `dev-opt` / `profiling` build profiles.
- [x] maturin build; `chameleon_rs._native` imports from Python 3.15; `__version__`.
- [x] Reference Chameleon and chameleon_partials cloned into `temp/` at pinned commits
      (`make reference`).
- [x] Chameleon's suite vendored unmodified in `tests/upstream` with an engine switch,
      strict environmental xfails, and the Rust expected-failures ratchet; baseline
      reproduced: 227 pass, 6 environmental.
- [x] `Makefile` dev loop; ruff for Python; cargo-deny in `make lint`.
- [x] Baselines: render (`bench_render.py`), engine-vs-expression split (`bench_split.py`),
      compile time (`bench_compile.py`).
- [x] CI workflow: macOS and Linux, Python 3.12 to 3.15 and 3.14t, `make lint test`, an MSRV
      check. (Runs once the repository is pushed.)
- [x] Decision record and this plan, reviewed by the Rust Architect.

## Phase 1: Spike, the cost model

Settle D2 to D9 with numbers before building on them. Code lives in `crates/spike` (not in
`default-members`, never shipped), benchmarked from Python.

1. Name lookup: two-level native `Scope` (D5) vs Chameleon's Python `Scope.get_name()`.
2. Attribute chain `a.b.c` with exact `lookup_attr` semantics through PyO3.
3. Fallback cost: calling a pre-built Python function per expression.
4. Text path: UTF-8 view + memchr escape + append to `Vec<u8>`, vs `list.append` +
   `''.join`; final `str` construction; the non-ASCII UTF-8 cache memory question (D6).
5. A hand-built program for the listing item run by a minimal VM loop with the Program /
   `VmState<Py<PyAny>>` / Runtime split (D2): the end-to-end per-item number.
6. Async proof (D9): the VM suspends on a Python awaitable and resumes through a native
   awaitable under `asyncio.run`, including `throw()`-based cancellation and the `async def`
   wrapper used with `create_task`.
7. Cost of storing `Py` vs keeping `Bound` within one resume.
8. Per-render overhead of the Python wrapper (`PageTemplate.render(**kw)` -> native -> back).

Go/no-go thresholds:

- minimal-VM listing item at 0.6 µs or less, otherwise revisit D3 to D6;
- fast-path expressions at least 3x faster than one Python function call per expression,
  otherwise revisit D4;
- the async proof works with cancellation, otherwise revisit D3 before anything is built on it.

Exit gate: `docs/spike-1.md` with the numbers and decisions, reviewed by the architect.

**Done (2026-10-20):** see [spike-1.md](spike-1.md). Threshold 1 GO (442-460 ns per item, 6.5-8x
faster than Chameleon, byte-identical output); threshold 2 NO-GO (1.7-1.8x), D4 revisited and
kept with a new absolute criterion; threshold 3 GO. Its "not validated" list is carried into
the phases below.

## Phase 2: Walking skeleton

One thin path through every layer, so design mistakes surface early: tokenizer (minimal),
program builder, compiler, VM, Python runtime and `PageTemplate(body).render(**kw)`, covering
static text, `${...}` in text and attributes, `tal:content`, `tal:condition` and `tal:repeat`,
with the real `ExprCompiler` (D1) and exact conversion and escaping (D6).

Also from spike 1's "not validated" list: Chameleon's escaping gate and quote entities, strict
`bytes` decoding, `__html__` results used as is, the loop's backup-then-assign-`None` order and
the `repeat` variable, and `__traverse__` on `Scope` and `Program` (the upstream `Scope`
doctest's `gc.collect() == 0` must pass).

Exit gate: the skeleton renders a cut-down listing template identically to Chameleon and its
per-item time is measured with the real benchmark (target: within 10% of the spike's 442-460
ns); a prototype native partial frame (push, keyword arguments, fresh scope, writing into the
caller's buffer) is measured against the 0.8 µs-per-item partials budget; architecture
adjusted if the skeleton disagrees with the spike; `crates/spike` deleted.

## Phase 3: Tokenizer and element parser

Port `tokenize.py` and `parser.py` exactly.

- Tokenizer: port the `XML_SPE` regex **verbatim with `fancy-regex`** (it needs lookahead and
  backtracking), so token boundaries match by construction. Compile time is not on the render
  path; a hand-written lexer only if profiling demands it, tested differentially against the
  regex version. Python's `re` and Rust's Unicode classes (`\s`, `\w`) differ in places;
  differential tests cover them.
- `source` module: source text, byte spans, lazily computed code-point offsets and (line,
  column) like `Token.location`.
- Element parser: `identify()`, `match_tag()` attribute splitting (quotes, unquoted values,
  valueless attributes, whitespace preservation), the namespace stack and prefixes,
  `restricted_namespace`, the lenient end-tag matching (unclosed tags stay start tags), the
  `--` in comments error.
- Encoding sniffing: `read_bytes` / `detect_encoding` / `read_xml_encoding` (BOMs, XML
  declaration, `<meta http-equiv>`).
- `_native._testing` exposes tokens and trees for pytest.

Tests: differential pytest against Chameleon's `iter_xml` and `ElementParser` over every
fixture and the real-template corpus, plus Hypothesis-generated markup; proptest invariants
and `cargo-fuzz` for panics.

Real-template corpus: Talk Python to Me (106 templates) and Talk Python Training (123),
229 in all, read in place and never copied into this repository (private code). Tests take
the directories from `CHAMELEON_RS_CORPUS` (paths separated by `:`), default to the two
local checkouts, and skip when none exist, so CI stays green without them.

Exit gate: all fixtures, the Talk Python corpus and 100,000 generated inputs tokenize and parse
identically.

## Phase 4: ZPT program builder

Port `zpt/program.py`, `tal.py`, `i18n.py`, `metal.py` into a typed node tree, against the
`ExprCompiler` trait with a toy implementation (D1), so the Interpolator's retry logic is
built and tested here.

- Attribute-language parsing: `split_parts` (`;;`, entity handling), `parse_defines`,
  `parse_substitution`, `parse_attributes`, `i18n.parse_attributes`, per-namespace whitelists,
  `data-tal-*` attributes.
- `visit_element` wrapping order (define-slot, define, case, condition, repeat, switch,
  domain, context, target), fill-slot and define-macro registration, the on-error fallback
  element, omit-tag, content/replace with the `default` marker, the `attrs` alias.
- Attributes: static, dynamic, dict, boolean, i18n, implicit translation, filtering when a
  dict expression overrides earlier ones, whitespace and quote preservation,
  `trim_attribute_space`.
- Text, comments (`<!--!`, `<!--?`, interpolation), CDATA, `<?python ?>` code blocks,
  `meta:interpolation`, `$$` escapes, repeat whitespace rules.
- Compile-time errors with Chameleon's messages and offsets.

Tests: `insta` snapshots of the node tree for every fixture; every compile-time error case from
the upstream tests (same exception type and offset).

## Phase 5: Expressions

The real `ExprCompiler` in `chameleon-py` (D4): TALES prefixes and pipes (`a | b`, `\|`, which
exceptions fall through), `python` (fast path and fallback with Chameleon's rewriting),
`string`, `not`, `exists`, `import`, `structure`, `load`; name resolution, `lookup_attr`, line
continuations, strict vs non-strict mode (syntax errors raised at render time with position);
free-variable sets per expression (D9); the custom expression-type bridge.

Tests: expression-level differential tests (same expression, same scope, both engines, same
value or same exception type), Hypothesis-generated expressions, the doctests in `tales.py`
and `compiler.py` ported.

## Phase 6a: VM, core semantics

Lower the node tree to bytecode (D3) and run it (D2, D5, D6): static text coalescing,
precomputed attribute formats, conversion and escaping, `define` local/global with backup and
restore, `repeat` (RepeatDict/RepeatItem, callable `index()` style access, whitespace between
items), `condition`, `switch`/`case`, `omit-tag`, `replace`, `on-error` exactly as D3 item 2,
render-time error tracking and the `RenderError` path (D8), panic catching.

Exit gate: every fixture that uses only these features passes byte-exact; a 3.14t smoke test
renders under free-threading; VM micro-benchmarks recorded.

## Phase 6b: VM, macros and i18n

Macros (`define-macro`, `use-macro`, `extend-macro`, `define-slot`, `fill-slot`, `macroname`,
slot stacks, scope copy and global propagation, templates used as macros, native macro frames
across templates, the recursion limit) and i18n (translation marks and whitespace
normalization, `name`, `domain`, `context`, `target`, `attributes`, implicit translation, the
translate protocol).

Exit gate: 100% of fixtures that don't need the file loader pass byte-exact (level 2, partial).

## Phase 7: Python API and compat layer

- `chameleon_rs` classes with Chameleon's config surface (`auto_reload`, `encoding`,
  `boolean_attributes`, `translate`, `implicit_i18n_*`, `on_error_handler`, `strict`,
  `trim_attribute_space`, `restricted_namespace`, `enable_data_attributes`,
  `enable_comment_interpolation`, `default_marker`, `value_repr`, `tokenizer`, `debug`,
  `keep_source`, `extra_builtins`, `search_path`, `package_name`, `loader_class`,
  `prepend_relative_search_path`, `default_extension`, `formats`), with thread-safe cooking
  and `auto_reload`.
- Loader semantics: search paths, `package:path` specs and package resources,
  `default_extension`, caching, mtime checks, `CHAMELEON_*` environment settings.
- `chameleon_rs.compat` and `install()`, with Chameleon's pure-Python utility modules
  vendored (D7).
- `ExpressionEvaluator`, `Macros`/`Macro`, `template['macro']`, `include()`.

Exit gate: level 1 (216+ of 227), level 2 complete (all fixtures, including loader ones),
level 4 (integration suites).

## Phase 8: Real-site verification

Compatibility is goal #1, so this comes before performance work.

- Crawl-and-diff: render every Talk Python route with each engine and compare HTML word for
  word. Non-deterministic values (CSRF tokens, timestamps) are pinned by the harness, not
  masked.
- Shadow mode: a compat setting that renders every template with both engines and logs any
  difference, for staging on real traffic.

Exit gate: level 5, zero differences (or each one explained and fixed).

## Phase 9: Built-in partials

D10: `render_partial` built in, path resolution, native frames writing into the caller's
buffer, recognizing `chameleon_partials.render_partial`, `auto_reload` checked at most once per
file per render.

Exit gate: the `bench/` partial templates render identically to chameleon_partials; partial
listing at 10x or better.

## Phase 10: Performance pass

Profile with `make build-profiling` under py-spy / Instruments. Candidates: interned key
caches, attribute-chain caching per call site, fewer temporary objects in conversions, SIMD
escaping, capacity hints, moving `render()` setup into Rust if the wrapper shows up, the
`chameleon-capi` crate only if a measured gap needs it (D12).

Exit gate: speed targets met; no regressions on any benchmark.

## Phase 11: Concurrency and async

D9: `render_async` on the resumable VM, concurrent expressions with placeholders, partial
fan-out, async iterables in `repeat` (decide), a free-threaded stress suite (many concurrent
renders of the same templates on 3.15t).

Exit gate: the dashboard benchmark's concurrent render near its 25 ms ceiling; event-loop lag
during an async render under 1 ms; no failures under the free-threaded stress suite.

## Phase 12: Release

Wheels for macOS, Linux and Windows (maturin), docs, a migration guide, the free-threading
classifier.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Tokenizer regex quirks on malformed HTML | Output differences on real pages | Verbatim `fancy-regex` port; differential fuzzing; Talk Python corpus in Phase 3 |
| Python expression semantics (scoping, `lookup_attr`, evaluation order) | Subtle wrong values | Whitelisted fast path using the interpreter's own C-API operations; ported rewriting for everything else |
| Exception message and location parity | Upstream test failures, worse debugging | Formatting ported in Python (cold path); code-point offsets; `PyErr` kept untouched |
| Python wrapper overhead per render | Misses the partials target | Measured in the spike; partials never use the wrapper; setup moves to Rust if needed |
| Async VM complexity | Phase 11 slips | Proven in the spike; D3 makes the VM resumable from the start |
| Internal-API tests (about 35) | 95% target at risk | Compat modules backed by Rust, Chameleon's pure-Python utilities vendored |
| Reference cycles through pyclasses | Memory leak per `auto_reload` | No template in fallback `__globals__`; `__traverse__` on every pyclass with `Py` fields |
