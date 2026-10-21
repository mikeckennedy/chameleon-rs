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

**Per-phase regression check (changed 2026-10-20, after Phase 4).** Phases 2 to 4 checked the
listing against a fixed figure ("within 10% of the spike", 506 ns). On a shared machine the
listing's run-to-run spread is about ±15 ns, and session-to-session drift was larger (Phase 3's
build measured 489 to 525 ns on the same day), so that check measured noise. From Phase 4 on,
each phase instead shows **no regression against the previous phase's build in a same-process
A/B** (`bench/bench_ab.py <previous phase commit>`: both builds loaded side by side, measured
interleaved after a Chameleon render; the median difference must be at most +5 ns per item),
and reports the ratio to Chameleon from the same run. This is a change of measurement method
made because of measured noise, not a looser target: the targets in the table above (inline
listing 0.6 µs or less, `render_partial` listing 0.8 µs or less, beating hand-written Python)
are unchanged, and `bench_skeleton.py` keeps reporting the absolute figures.

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

**Done (2026-10-20).** `chameleon_rs.PageTemplate(body).render(**kw)` runs through every layer:
a minimal tokenizer and element parser, the ZPT program builder with the Interpolator's
longest-candidate-first retry, a bytecode compiler, the resumable VM over the `Runtime` trait,
and the Python runtime with the real `ExprCompiler` (fast path for Name, Attribute, Constant,
Call, `not`, Subscript, Compare, IfExp, BoolOp, arithmetic and literals; exact fallback for the
rest). Numbers from `bench/bench_skeleton.py`, release build, n=500, three runs after the
review fixes below (min of seven repeats each; medians are 1-13 ns higher):

| Scenario, per item | Chameleon | chameleon_rs | Gate |
|---|---|---|---|
| Cut-down listing (`listing_loop.pt`) | 2,845-2,900 ns | **481-493 ns** (5.8-6.0x) | ≤ 506 ns (within 10% of 442-460): **GO** |
| Same listing through two partials per item (native frames vs chameleon_partials) | 6,724-6,819 ns | **618-640 ns** (10.7-10.9x, a lower bound, see below) | ≤ 800 ns: **GO** |

The partial gate is the absolute figure against the budget. The ratio to Chameleon is only a
lower bound for the real feature: the prototype skips path resolution, the loader cache
lookup, the `Markup` round trip, and putting `__translate` and `render_partial` into each
partial's scope, all of which Phase 9 adds.

The spike, re-measured the same day before deletion, ran the listing at 428 ns, so the
skeleton's 481-493 ns is 12-15% above it. Before the review fixes it measured 472-477 ns; the
fixes cost about 10 ns, probably the countdown and unwind-entry matching in `LoopNext` and
the checked text reads. None of the gap is profiled yet (see "Profile before Phase 4" below);
likely contributors are iterating the Python iterator `repeat()` returns instead of a Rust
queue, the attribute-dropping rule, and the general instruction set. Outputs are
byte-identical in both scenarios. Tests: 46 Rust (toy runtime: control flow, frames, the
unwind stack, suspension inside frames and loops, compile errors), 324 pytest plus 3 skipped
deferred probes (byte-for-byte differential cases, an 84-case conversion grid, error parity,
the `Scope` gc doctest, partials, 800 concurrent renders on 16 threads, the corpus), and the
upstream suite on the reference engine (227 passed, 6 xfailed). All 229 Talk Python templates
compile or stop with `UnsupportedError` (no panics). Deviations from the design are recorded
in the architecture's "Phase 2 amendments".

Review fixes (2026-10-20, after the Rust Architect's review): Chameleon's two-stage
conversion for non-`str` `__html__`/`translate` results (a value kind x source x position
grid test), user-supplied `repeat` objects (any iterable, a lazily evaluated length), the
single unwind stack with per-frame bases, a restartable `LoopNext`, checked `resume_with`, a
smoothed and capped capacity hint, and new tests (suspension inside callee frames and loop
bodies, concurrent renders from many threads, the corpus compiles without panics, the
architect's probe scripts). The table above is measured after these fixes.

Carried forward:

- **Profile before Phase 4 adds instructions** (in Phase 4, before the new node kinds land):
  the listing item is about 10% above the spike's same-day 428 ns, and nothing has
  attributed that gap yet. Use `make build-profiling` and decomposition (no sudo for
  samplers), and record where the 40-50 ns goes so later phases do not compound it.
- Error-message parity for unpacking (`a, b = item`) and walrus targets (Chameleon raises
  `SyntaxError` from its generated code, we raise `TypeError`): Phase 8.
- `rcontext` persistence across suspension and a `Mutex`-guarded awaitable: Phase 11.
- The partial registry becomes path resolution: Phase 9.

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
the directories from `CHAMELEON_RS_CORPUS` (paths separated by `:`) and skip when it is
unset, so CI stays green without them. No path is hard-coded in the repository (amended
2026-10-20 after the Phase 4 review): each developer sets the variable in their own shell or
in an `.envrc`, which `.gitignore` excludes.

Exit gate: all fixtures, the Talk Python corpus and 100,000 generated inputs tokenize and parse
identically.

**Done (2026-10-20).** The tokenizer assembles `XML_SPE` from Chameleon's `recollector` table
(all 28 expanded patterns equal Chameleon's, checked by pytest), with Python's `\s` / `\d` /
`\w` as explicit classes generated from Python's `re`. Text runs between `<` are found with
memchr and the character after `<` picks the `MarkupSPE` branch; the lookaround-free
branches (`<!`, `<?`, `</`) and the parser's tag-name and processing-instruction regexes run
verbatim on the `regex` crate, and the three patterns with lookaheads (`ElemTagCE`,
`match_single_attribute`, `match_double_hyphen`) are matched by hand from their backtracking
priority order, checked against the verbatim patterns on fancy-regex (a dev-dependency) by
proptest. `ElementParser` is ported exactly: `identify`, attribute splitting with Chameleon's
plain-string empties, the namespace stack (including its pop-once-per-end-tag quirk),
`restricted_namespace`, lenient end tags, and every error with Chameleon's type, message and
position (`ParseError`, a bare `KeyError` for an undefined prefix, a bare `AttributeError`
for a nameless tag). Encoding sniffing is a verbatim Python port in `chameleon_rs.utils` (it
needs Python's codecs). `_native._testing` exposes tokens and trees normalized for
comparison. Decisions and deviations: architecture.md, "Phase 3 amendments".

Review fixes (2026-10-20, after the Rust Architect's review): the first build ran every
pattern on fancy-regex, whose backtracking stack is fixed at 1,000,000 entries; a markup
token of about 1 MB (a long comment, CDATA, a data-URI attribute) overflowed it and the
error was swallowed into a lone `<` token (verified: a 1.1 MB `title` on a
`tal:condition="False"` paragraph renders `''` in Chameleon and raised `ParseError` here).
Production matching now has no failure mode (the dispatch and hand matchers above), and
deterministic over-1 MB tokens of every kind (comment, multi-line comment, CDATA, PI,
doctype subset, quoted / unquoted attribute values, 250,000 and 550,000 attributes, long
names, long text) are compared with Chameleon. Also: the character-class test accepts, on
Python 3.12-3.14, only differences at code points unassigned in that interpreter's Unicode
(verified on 3.12 and 3.14); parser options and trees use `Arc` and `ParseOptions` and
`Program` are asserted `Send + Sync`; `ns_attrs` deduplication is indexed past 16
attributes; the corpus test reports path hashes, not names.

Results:

- **Differential:** all 230 upstream fixtures (228 parse, 2 raise the same `KeyError`), each in
  three configurations (ZPT namespaces, `test_parser.py`'s namespaces unrestricted, text
  mode), 66 hand-picked edge cases in three configurations, the 12 over-1 MB cases, and all
  229 Talk Python templates: tokens, trees (every token text and code-point position,
  namespaces, `ns_map`) and errors identical. **100,000 Hypothesis-generated documents**
  (nested elements biased to well-formed with tricky choices mixed in: unclosed and
  mismatched tags, odd quotes and unquoted values, valueless attributes, CDATA, PIs, comments
  with `--`, doctypes with internal subsets, namespaces declared and not, entities,
  Python-only whitespace such as `\x1c` and `\u3000`, `${}`), over five namespace / mode
  configurations: **zero differences**, run three times (the last after the review fixes, 12
  minutes). Encoding sniffing: fixed documents (every BOM, XML declarations, `<meta>`
  charsets, bad codecs) and 500 generated byte strings identical.
- **Tests:** 63 Rust (59 core, including proptests: tokens cover the input and match the
  literal `finditer` on fancy-regex, the hand matchers match their verbatim patterns, item
  trees cover the tokens in order, tag parts lie inside their token, indexed positions match
  `Token.location`), 675 pytest passed plus 3 skipped deferred probes, upstream suite on the
  reference engine 227 passed, 6 xfailed. `cargo-fuzz` was skipped (needs nightly; the
  toolchain is pinned stable).
- **Compile speed** (`bench/bench_parse.py`, release build, two runs): tokenize plus parse of
  the corpus (229 templates, 1.4 MiB) takes 11.1 ms against Chameleon's 113.2 ms (10.2x;
  median template 27.3-27.6 us against 284.5-285.7 us); the tokenizer alone takes 0.59-0.67
  ms against 18.6-20.4 ms (30-32x). Against Chameleon's 3.5 ms median full compile (Phase 0
  baseline) the tokenizer and parser are under 1% of the budget. The 57 corpus templates the
  builder supports so far compile completely in a median 0.025 ms (Chameleon: 4.8 ms). (The
  first, all-fancy-regex build was 3.5-3.8x faster than Chameleon on tokenize plus parse.)
- **Render speed** is unaffected (the render path did not change): `bench_skeleton.py`,
  release, three runs, 479-486 ns per listing item (medians 482-496) against Chameleon's
  2,871-2,893 ns (5.9-6.0x), in line with Phase 2's 481-493 ns; partial listing 640-660 ns
  against 6,782-6,863 ns.

The builder now outputs doctypes and other declarations, CDATA without `${`, and non-`python`
processing instructions as Chameleon does, and a missing tag suffix as empty; interpolated
CDATA and `<?python ?>` blocks remain `UnsupportedError` until Phase 4.

Carried forward:

- **Phase 7: lone surrogates.** Chameleon renders a template whose `str` contains lone
  surrogates; ours cannot become a Rust `str`. Phase 7 must pick a behaviour (for example a
  `surrogatepass` round trip, or a clear error) and test it.
- **Phase 7:** a user-supplied `tokenizer` whose tokens are not slices of the body needs a
  different parser entry point.
- **Phase 12:** whether `_native._testing` ships in the production wheel (it is small and
  inert, but it is test surface).
- Test-only: `_testing.parse` converts each tag's `ns_map` separately, quadratic in the
  number of tags times prefixes; fine for tests, not for production use.

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

**Done (2026-10-20).** The builder is a port of `zpt/program.py`, `tal.py`, `i18n.py` and
`metal.py` into the typed tree of `nodes.rs`, followed by a resolve pass that compiles the
expressions in `compiler.py`'s order and a lowering pass to bytecode (three passes, so errors
come in Chameleon's order). Positions follow Chameleon's `Token` arithmetic (`Tok`), including
its quirks. Template options are a typed `Options` struct; `PageTemplate` now sniffs the content
type (HTML boolean attributes, newline normalization), accepts `bytes` and keyword options, and
`PageTextTemplate` exists. Decisions, interface changes and the Chameleon quirks reproduced:
architecture.md, "Phase 4 amendments".

Profiling first (the carried-forward item). Rebuilding the spike from git in a scratch
directory gave a same-session comparison, and macOS `sample` profiled the `profiling` build
without sudo. Escaping was the largest absolute cost (17% of samples, two `memmove` calls per
escaped character); constant-length entity copies into a once-reserved buffer fixed that. The
rest of the gap to the spike is our dispatch plumbing, spread over the eight expressions
(`Runtime::eval` indirection, the resume check, the `Attr` instruction's mark and split pushes)
and the loop (Python's iterator and the countdown instead of the spike's Rust queue); CPython's
share is identical in both profiles, and static text is cheaper than the spike's. Measured in
one process with the Phase 3 build loaded side by side (session-to-session drift was larger
than the effects): Phase 3 510-531 ns, Phase 3 plus the escaping fix 490-501 ns (median -27 ns),
Phase 4 497-510 ns (median -18.5 ns against Phase 3). The larger instruction set costs about 10
ns (2%): LLVM stopped inlining `execute` into `run` and `value_of` into the instruction arms,
which `#[inline(always)]` partly recovers (the gap to the escape-only build went from ~20 to
~10 ns); inlining `run` itself gained nothing. `Op` stays 24 bytes (a test checks it; the first
draft was 32). Rendering also got 10-20 ns slower whenever Chameleon had compiled or rendered in
the same process first (process state, most likely the allocator and the 400 KB buffers);
`bench_skeleton.py` always does that, so it remains the yardstick.

| Scenario, per item (`bench_skeleton.py`, release, n=500, 3 runs, after the review fixes) | Chameleon | chameleon_rs | Target |
|---|---|---|---|
| Listing (`listing_loop.pt`) | 2,993-3,005 ns | **492-497 ns** (medians 494-499; 6.0-6.1x) | ≤ 600 ns |
| Partial listing (prototype native frames) | 7,073-7,081 ns | **636-640 ns** (medians 643-646; 11.1x) | ≤ 800 ns |

Regression check (the method described under "How speed is measured"): `bench/bench_ab.py
1d2ead4`, 8 interleaved rounds after the review fixes: Phase 3 501-506 ns, Phase 4 497-501 ns,
median difference **-5.3 ns** (range -7..-4; tolerance +5 ns), 6.03x Chameleon in the same run.
The machine ran 4-7% slower than on the day the Phase 2/3 tables were measured (Chameleon
about 3,000 ns against 2,845-2,900 ns, load average 3-13 from other work).

Results:

Review fixes (2026-10-20, after the Rust Architect's review, each verified against Chameleon
first): the default marker in a `tal:attributes` dictionary is converted like any object
(Chameleon's `__quote` there passes no marker: it writes `&lt;DEFAULT&gt;`, and our marker's
`str()` is now Chameleon's `<DEFAULT>`); `attrs` is one dict per element, shared by its
expressions and across renders (`attrs.setdefault(...)` counts 1 then 2; `x is attrs` holds),
as Chameleon's compile-time `Static` is; dictionary attributes test `name in set(...)` with real
sets; bool fields became enums (`Visibility`, `ContentKind`, `Translation`); `Runtime`'s
methods are grouped in documented sections; tests no longer default to private corpus paths
(set `CHAMELEON_RS_CORPUS` in your shell or an ignored `.envrc`); the per-phase benchmark check
became a same-process A/B (`bench/bench_ab.py`).

- **Builder exactness:** an inventory of every expression and assigned name (text and Chameleon
  position) is compared as a multiset with the `Value` objects in Chameleon's own `MacroProgram`
  tree: all 107 upstream fixtures, 19 edge cases, all 229 Talk Python templates, and **60,000
  Hypothesis-generated templates** dense in TAL/METAL/i18n attributes (tricky clause values,
  nesting, data attributes): zero differences, and identical builder errors (type, message,
  token, offset, location). The generator found three real divergences on the way, all fixed:
  a nested `define-macro` re-registering a name wins (Chameleon registers before visiting the
  children), `i18n:target` with `i18n:name` takes the name's value (a late-binding lambda), and
  an `extend-macro` slot list that is never popped.
- **Compile-time errors:** 53 differential cases, one per error site in program.py, tal.py,
  i18n.py and compiler.py plus all of the upstream suite's (`test_errors`, double underscore,
  compiler internals, strict syntax error, tag mismatch), including error *order* (builder
  errors before expression errors, macros first, fallback before element, slots before macro).
- **Rendering:** `tal:define`, `tal:replace`, `tal:omit-tag`, every `tal:attributes` form
  except i18n (substitutions, booleans, dictionaries, filtering), HTML boolean attributes,
  `tal:switch`/`tal:case`, interpolation in CDATA, comments and processing instructions,
  `meta:interpolation`, `data-tal-*`, `trim_attribute_space` and text mode now render: 89
  differential cases and **30,000 generated templates** rendered by both engines (same output or
  same exception type). The render fuzz found one real bug (a filter consulted before the
  `None` check), fixed. Everything else builds a tree and stops with `UnsupportedError`.
- **Snapshots:** `insta` snapshots of the tree for the 106 UTF-8 fixtures
  (`crates/chameleon-core/src/snapshots/fixtures`; `074-encoded-template.pt` is windows-1251
  and is covered by the pytest inventory instead).
- **Corpus (229 Talk Python templates):** every tree matches Chameleon's; 65 compile fully (57
  in Phase 3), 164 stop with `UnsupportedError`: `load:` 120, `structure:` 35, `string:` 7
  (Phase 5) and `metal:define-macro` 2. Expression types are compiled before lowering, so they
  are reported first; 152 templates also use `metal:use-macro` (Phase 6b).
- **Tests:** 79 Rust (75 core, 4 `chameleon-py`), 958 pytest passed, 1 strict xfail (the
  boolean-attribute gap below), 6 skipped (3 deferred probes, and the 3 corpus tests when
  `CHAMELEON_RS_CORPUS` is unset; with it set they pass), the upstream suite on the reference
  engine 227 passed, 6 xfailed; `make lint` clean.

Carried forward:

- **Phase 5:** non-strict mode (`strict=False`, which the upstream fixture harness uses) defers
  a syntax error to render time; the resolve pass records it (`Resolved::Deferred`) and lowering
  stops with `UnsupportedError` until the VM can raise it at the expression. `<?python ?>` code
  blocks need `ExprCompiler::compile_code`. Error tokens for expressions behind a TALES prefix
  are tracked already; Phase 5 keeps that through the other expression types.
- **Phase 6a:** `tal:on-error` (and its fallback's quirk: static attributes before a dictionary
  keep filters that refer to uncached values, a `TypeError` at render time in Chameleon), global
  `tal:repeat` (Chameleon restores names it never backed up: a `NameError` once the loop ends).
- **Phase 6b:** macros, slots, i18n (`Translate`, `Name`, `Domain`, `TxContext`, `Target` with
  its `default` alias, attribute and implicit translation), `econtext.update(rcontext)` after
  macro calls (global defines already go to `rcontext`).
- **Known approximation (revisit in Phase 6a):** a boolean attribute's interpolated result is
  tested for truth by whether it wrote anything; this differs from Chameleon only when an
  `__html__` returns an object whose own `__html__` returns a false non-`str` (Chameleon then
  drops the attribute, we write its name). A strict-xfail differential test pins the gap
  (`test_boolean_attribute_with_non_str_html`).
- **Phase 6a, `tal:on-error` unwinding:** on an error the handler must **drop, not restore**
  the `Define` and `Loop` entries above its depth (Chameleon's generated code has no
  `try/finally`, so a failed body's defines and loop variables leak), while still truncating
  `Attr` / `Omit` / `Value` / `Switch` entries and the output. Write this test first:
  `<div tal:on-error="x"><p tal:define="x 2"><b tal:repeat="i [7]">${i}${1/0}</b></p></div>|${x}|${i}`
  renders `<div>2</div>|2|7`.
- **D9 analyses** (free-variable sets for concurrent expressions, last-use for the memory
  saver in vision.md) belong in the resolve table's entries, per `ValueId`, not in the tree.

## Phase 5: Expressions

The real `ExprCompiler` in `chameleon-py` (D4): TALES prefixes and pipes (`a | b`, `\|`, which
exceptions fall through), `python` (fast path and fallback with Chameleon's rewriting),
`string`, `not`, `exists`, `import`, `structure`, `load`; name resolution, `lookup_attr`, line
continuations, strict vs non-strict mode (syntax errors raised at render time with position);
free-variable sets per expression (D9); the custom expression-type bridge.

Tests: expression-level differential tests (same expression, same scope, both engines, same
value or same exception type), Hypothesis-generated expressions, the doctests in `tales.py`
and `compiler.py` ported.

**Done (2026-10-20).** The real `ExprCompiler` in `chameleon-py`: TALES prefixes and pipes
with Chameleon's `exceptions` tuples, `python` (fast path plus the exact fallback), `string`
(a port of `Interpolator` with braces optional), `not`, `exists`, `import`, `structure`, and
`load` (`ProxyExpr('__loader')`) for file templates; the template builtins (`template`,
`macros`, `nothing`, `__loader`, `extra_builtins`); non-strict mode raising `ExpressionError`
at the site with Chameleon's token, offset and location; free-variable sets per site in the
resolve table (D9); the custom expression-type bridge (`chameleon_rs._bridge`, D11); a
minimal `PageTemplateFile` with Chameleon's `TemplateLoader` ported (needed by `load:`).
Decisions, the interface changes (`ExprContext.engine`, `ExprCompiler::deferred` /
`free_vars`) and the deliberate deviations: architecture.md, "Phase 5 amendments".

Results:

- **Expression differential** (`tests/python/test_expressions.py`, value or exception type
  and arguments compared): 137 hand-picked expressions (every type, pipe fall-through per
  exception class, `string:` quirks, names and builtins, fast-path/fallback boundaries); every
  expression type and pipe combination in 15 kinds of site (27 expressions x 7 scopes per
  site: text, attributes, `tal:content` / `replace` / `structure`, `tal:attributes`, boolean
  attributes, conditions, defines, repeats, switch, CDATA, comments); 16 non-strict cases
  (type, token, offset, location, message); 22 custom-type renders (value, text, bool
  sub-expressions, scope stores, Chameleon's `Symbol`, an `ExpressionError` driving the
  retry, a custom default type); `load:` (relative, interpolated, pipes, missing, nested,
  absolute, package resources, caching). The tales.py / compiler.py doctests about
  expressions are ported (24 cases plus the JavaScript, `braces_required` and NameTransform
  ones); `ExpressionEvaluator`'s waits for Phase 7.
- **Hypothesis:** generated expressions biased to the fast path's node types and the
  fallback boundaries (comprehensions, lambdas, walrus, starred and `**`, f-strings, slices,
  chained compares, keyword arguments, attribute versus item lookup, shadowed builtins,
  `econtext`, `__` names, prefixes and pipes). Runs of 50,000 found seven differences, each
  fixed or explained: `__builtins__` is the builtins dict in Chameleon's module; lazy `Scope`
  iteration (`RuntimeError` when a comprehension writes the scope it iterates); `KeyError`
  arguments; template-wide constant merging and folding (`${x is 1.5}`); a literal-tuple fold
  that must skip aliases (`(True, attrs)`); the scope's type name in messages and binary
  constant folding (both fixed after the architect's review); and, normalized in the harness
  until Phase 6a, Chameleon's `rcontext['__error__']`. The final generator ran **100,000
  examples with zero differences**, and after the review fixes (identity checks widened from
  atoms to any sub-expression) 30,000 more; the suite runs 600.
- **Corpus (229 Talk Python templates, as `PageTemplateFile`):** every expression compiles;
  4,836 expressions, 4,821 (99.7%) entirely on the fast path, 15 with fallback code. **75
  templates compile fully** (65 in Phase 4); the other 154 stop only at macros (152
  `metal:use-macro`, 2 `metal:define-macro`, Phase 6b). Phase 4's `load:` 120, `structure:`
  35 and `string:` 7 are gone.
- **Tests:** 82 Rust (76 core, 6 `chameleon-py`), 1,211 pytest passed, 3 skipped (deferred
  probes), 1 strict xfail (Phase 4's boolean-attribute gap), the upstream suite on the
  reference engine 227 passed, 6 xfailed; `make lint` clean; corpus variable set.
- **D4 revisit criterion, re-measured** (`bench/bench_d4.py`, release, two runs,
  fallback-only via the private `_fast_path=False` option): a fallback-only build renders the
  listing in 783-845 ns per item and the partial listing in 963-1,067 ns, missing both
  targets (0.6 / 0.8 us); the fast path saves 31-59 ns per expression on the benchmark mix
  (median 33-38 ns; `"{:,}".format` 57-59 ns), above the 20 ns floor. The first run was taken
  at load average 8-12, the second at 7. **D4 stands.** The fast path covers 99.7% of the
  corpus's expressions.
- **Speed** (release): `bench/bench_ab.py 9293cbe`, 8 interleaved rounds, two runs: Phase 4
  478-516 ns, Phase 5 477-515 ns, median difference **-1.2 and -3.2 ns** (tolerance +5), 6.4-6.5x
  Chameleon in the same run; after the review fixes, **-4.1 ns** (load average 6.7-8.7), 6.5x. `bench_skeleton.py`, three runs: listing 492-498 ns (medians
  497-499) against Chameleon's 2,915-2,931 ns (5.9x); partial listing 627-633 ns against
  6,793-6,833 ns (10.8x). The machine was shared (load average 6-25 from other work); earlier
  A/B runs at load 20+ gave +10 to +15 ns with ranges up to +139, and are not counted.
  The first complete build measured +4.6 to +9 ns: the new node kinds had grown `eval`'s stack
  frame to 912 bytes, which every recursive call pays. `eval` now handles only the census's
  first kinds (names, attributes, constants, short calls, `not`, subscripts, `[]`) and passes
  the rest to an out-of-line `eval_more` (and the TALES types to a cold `eval_rare`): a
  288-byte frame, and slightly faster than Phase 4 on expression-dense templates. Compile
  (`bench_parse.py`, release, load average 7-10, corpus as `PageTemplateFile`): Chameleon's
  full compile (cook) median 10.64 ms (max 93 ms); ours, tree plus every expression for all
  229 templates, median 0.103 ms (max 1.1 ms), and full compile of the 75 it supports median
  0.074 ms (max 1.5 ms); tokenize plus parse 10.0x faster than Chameleon's.

Carried forward:

- **Phase 6a:** `<?python ?>` code blocks (render-function locals; see the amendments), and
  the render-error tracking that makes Chameleon's `rcontext['__error__']` visible.
- **Phase 6b, low priority:** `yield` in an expression makes Chameleon's render function (or,
  inside a macro, only that macro's function) a generator, which renders `''`; ours evaluates
  to a generator object.
- **Phase 7:** tighten `PageTemplateFile.cook_check` / `auto_reload` locking (the mtime check
  runs outside the lock).
- **Later:** a custom expression type returning malformed statements raises different
  exception types than Chameleon's code generator does.
- **Phase 7:** `ExpressionEvaluator`, the full loader surface, compat aliases for the scope's
  and marker's module names.
- A rendered template's `macros` builtin is a stub until Phase 6b.

## Phase 6a: VM, core semantics

Lower the node tree to bytecode (D3) and run it (D2, D5, D6): static text coalescing,
precomputed attribute formats, conversion and escaping, `define` local/global with backup and
restore, `repeat` (RepeatDict/RepeatItem, callable `index()` style access, whitespace between
items), `condition`, `switch`/`case`, `omit-tag`, `replace`, `on-error` exactly as D3 item 2,
render-time error tracking and the `RenderError` path (D8), panic catching.

Exit gate: every fixture that uses only these features passes byte-exact; a 3.14t smoke test
renders under free-threading; VM micro-benchmarks recorded.

**Done (2026-10-20).** Phases 2-5 had lowered most of the core already; this phase added what
was left and made errors behave like Chameleon's: `tal:on-error` (D3 item 2, with the
drop-not-restore rule; the plan's leak test was written first and passes:
`<div>2</div>|2|7`), Chameleon's render-time error tracking (`__token`, `rcontext['__error__']`)
and the formatted `RenderError` (`ExceptionFormatter` and `create_formatted_exception` ported
to Python, cold path), global `tal:repeat`, `<?python ?>` code blocks with a per-render
namespace, the full `RepeatItem` (`letter`, `Letter`, `roman`, `Roman`), `ErrorInfo`,
`PageTextTemplateFile.render` returning `bytes`, and the fix for Phase 4's boolean-attribute
approximation. The quirks reproduced (one `__fallback` mark per render function, so an outer
handler truncates to an inner handler's mark; the on-error fallback's compile-time
`AttributeError`; a global loop that always ends in `NameError`; error tokens re-sliced from
the source; `UnboundLocalError` for code-block names) and the interface changes are in
architecture.md, "Phase 6a amendments".

Results:

- **Fixtures (level 2, partial):** `tests/python/test_fixtures.py` renders every upstream
  input as Chameleon's `test_pt_files` / `test_txt_files` feed them (non-strict, the same
  keyword arguments, translate function and target languages) with both engines and compares
  the results byte for byte (stricter than upstream's doctest-style check). **63 of 122
  renders pass**; the other 59 (45 input files) need Phase 6b and are listed in the test's
  `LATER` with the feature (strict: each must still raise `UnsupportedError`): `metal:use-macro`
  21, `metal:define-macro` 12, `i18n:translate` 12 (2 on `tal:content`), `i18n:domain` 8,
  `default` inside `i18n:target` 2, implicit translation 2. The five that use `load:` macros
  (066, 081, 082, 083, 089) then also need the loader (Phase 7).
- **Differential tests:** `test_errors.py` (on-error in 38 shapes, every error path's type and
  *whole formatted message*, `rcontext['__error__']`, `ErrorInfo`, the error handler, the
  `RenderError` class and bases, uncopyable exceptions, nested renders, file templates' source
  line, recursion, global repeat, 29 code-block cases), `test_repeat.py` (every `RepeatItem`
  attribute as value and call over nine kinds of iterable, plus 23 loop shapes, user `repeat`
  objects), the boolean-attribute grid (33 cases; the strict xfail is gone).
- **Render fuzz** (Hypothesis, `test_render_fuzz.py`; a third 30,000-example run after the
  review fixes, zero differences): now with on-error (including a wrapper
  that guards generated subtrees, so caught errors are common), failing expressions, global
  loops and code blocks, comparing outputs and render errors' whole messages (addresses and
  generated ids normalized): **30,000 examples, zero differences**, twice (once before and once
  after adding the guarded wrapper). Earlier runs found two real differences, both fixed: the
  fallback quirk above, and the default marker's `__name__` (`DEFAULT_MARKER`, which shows in
  a render error's arguments). The suite runs 300.
- **Free-threading:** `test_threads.py` gained a check that a free-threaded interpreter keeps
  the GIL disabled after importing the extension, and 4,000 renders on 16 threads of templates
  with on-error, code blocks and formatted render errors. Run on CPython 3.14.7t (a wheel built
  with `maturin build --profile dev-opt -i <3.14t python>` into a scratch venv, separate
  `CARGO_TARGET_DIR`, with `PYO3_PYTHON` pointing at that interpreter): GIL disabled,
  `test_threads.py`, `test_errors.py` and `test_repeat.py` (364 tests) passed five times in a
  row with `-W error::RuntimeWarning`. CI's 3.14t matrix entry runs the whole suite.
- **Micro-benchmarks** (criterion, `cargo bench -p chameleon-core`, release, load average 3-6):
  escaping 1 KiB of plain text 270 ns (3.6 GiB/s), 1 KiB dense with escapes into a quoted
  attribute 778 ns (1.26 GiB/s), a short attribute value 4.7 ns; tokenizer 8.1 us for 10 KB
  (1.17 GiB/s); the VM running the benchmark listing over a toy runtime (8 expressions per item,
  linear-search scope) 91.6 us for 500 items, 183 ns per item, which bounds the VM's own share
  of the 480-490 ns real figure.
- **Speed, after the review fixes:** `bench/bench_ab.py ef700c6` (ef700c6 renders exactly
  as 41613f2), release, 16 interleaved rounds, twice at load average 3.5-5: **+2.9 ns** (range
  -2..+8) and **+0.2 ns** (range -5..+53), 5.8-6.3x Chameleon. Before the review fixes,
  `bench/bench_ab.py 41613f2`, 8 rounds: the first run at load
  average 10-12.5 (+3.1 ns, range -37..+18) is discarded; after folding the per-expression
  token into the expression table, **-1.5 ns** (range -44..+13, load 8) and **-4.1 ns** (range
  -7..-1, load 4-7), 6.2x Chameleon in the same run. `bench_skeleton.py`, three runs: listing
  481-494 ns per item against Chameleon's 2,878-2,900 ns (5.9-6.0x); partial listing 632-644 ns
  against 6,762-6,809 ns (10.6-10.7x). The token costs one store per expression.
- **Tests:** 89 Rust (83 core: on-error unwinding, the fallback mark, handlers that raise,
  errors through frames, suspension around on-error, `throw`, global loops, the boolean
  stage; 6 `chameleon-py`), 1,681 pytest passed and 61 skipped (59 fixtures listed for Phase 6b, the
  Phase 8 walrus probe, the free-threading check on a GIL build), the upstream suite on the
  reference engine 227 passed and 6 xfailed; `make lint test` green with
  `CHAMELEON_RS_CORPUS` set. Two stale probe deferrals (`tal:replace`, doctypes) now pass and
  were removed. `make test-upstream-rust` still exits at the missing compat package (Phase 7).
- **Review fixes (2026-10-20, after the Rust Architect's review; ~60 extra probes found the
  semantics exact):** the catch path was O(template size) per caught error (each `catch` and
  `record_error` rebuilt the line index and scanned the source for the token): 5,000 caught
  errors on a 421 KB template took 575 ms against Chameleon's 2.0 ms. Token spans (byte range,
  line, column) are now resolved once at compile time; 5,000 caught errors take 1.2 ms on a
  200 KB template and 1.0 ms on an 84-byte one (Chameleon 1.8 ms for both), and a pytest guard
  compares the two engines on a 400 KB template. Re-measuring showed the per-evaluation token
  store itself cost about 8 ns per listing item (A/B against ef700c6 at load 3-4: +5.4, +8.8,
  +9.8 ns; +0.1 ns with the store removed): the VM now records the last two expressions per
  frame in `VmState` (one store) and the runtime resolves the token only on the error path
  (architecture.md, D8 amendment). Fixed on the way: a position-less interpolation with two
  expressions (a processing instruction's) is Chameleon's compile-time
  `AttributeError: 'str' object has no attribute 'pos'`. New `chameleon_core::throw(st, rt, error)`
  for Phase 11: resumes a suspended render by raising at the suspended instruction, so an
  enclosing `on-error` renders its fallback and a non-catchable error (cancellation) passes
  through (core tests for both).
- **Corpus (229 Talk Python templates):** unchanged from Phase 5: 75 compile fully, 154 stop
  only at macros (152 `metal:use-macro`, 2 `metal:define-macro`); trees still match.

Carried forward:

- **Phase 6b:** macro frames get their own `__token` (Chameleon resets it to `None` before an
  internal macro call, so an error caught around one is a `KeyError` in the handler), their
  own fallback mark and code-block namespace (ours are per program and render); translation
  streams change what `len(__stream)` means for the fallback mark.
- **Phase 7:** compile errors' `__str__` lacks Chameleon's `- Source:` line (render errors have
  it); the compat aliases must map `chameleon_rs.tal.ErrorInfo` (its repr names our module).
- **Phase 6b:** key the code-block namespace per frame (a frame serial passed through
  `activate`), not per program (`expr.rs`, `NsCode::key` and `RenderCtx::namespaces`).
- **Phase 6b:** split `Runtime` into sub-traits: a base `Values { type Value; type Error; }`;
  `Eval` (activate, eval, emit, truthy, is_default, is_false; token and set_token no longer exist); `Names`
  (lookup, set_null, define, restore); `Loops` (repeat, countdown, next, assign,
  missing_backup); `Errors` (is_catchable, catch, record_error, recursion_error, with defaults
  for the toy runtimes); `Attrs` (yields_name, attr_dict, case_compare); and
  `pub trait Runtime: Eval + Names + Loops + Errors + Attrs {}` with a blanket impl.
- **Known difference, recursion depth:** with an `on-error` at every level of Python-level
  recursive renders, Chameleon recovers at about 198 levels deep and we at about 497; our
  `RecursionError` message differs ("... while rendering a template"). Check macro-depth parity
  against `MAX_FRAMES = 500` in Phase 6b.
- **Known differences:** a fallback expression that reads a code-block name raises `NameError`
  where Chameleon's function raises `UnboundLocalError` only for names it can see are local
  (handled for the common case); the `default` and `attrs` aliases do not apply inside a code
  block; a recursive native partial shares one code-block namespace.

## Phase 6b: VM, macros and i18n

Macros (`define-macro`, `use-macro`, `extend-macro`, `define-slot`, `fill-slot`, `macroname`,
slot stacks, scope copy and global propagation, templates used as macros, native macro frames
across templates, the recursion limit) and i18n (translation marks and whitespace
normalization, `name`, `domain`, `context`, `target`, `attributes`, implicit translation, the
translate protocol).

Exit gate: 100% of fixtures that don't need the file loader pass byte-exact (level 2, partial).

**Done (2026-10-21).** A program now holds several functions, as Chameleon's generated module
does: the main render function at instruction 0, one per `metal:define-macro`, and one per
`metal:fill-slot` body (a closure of the function that defines it). Each runs in a frame of its
own with Chameleon's per-function state: its `__token` (the internal macro call's reset to
`None` included), its `__fallback` mark, its code-block namespace (keyed by a frame serial),
its i18n settings (`__i18n_domain`, `__i18n_context`, `target_language`, passed on to macros
and captured by slot bodies), and the slots it resolved when it started. Macros of other
templates and templates used as macros are native `Macro` objects (program, entry, and that
template's builtins), run as frames; anything else with an `include` is called with
Chameleon's signature, and ours are callable with it too, so macros and slots mix across the
two engines. The slot stacks are Chameleon's `econtext['__slot_<name>']` deques, holding native
`SlotFill`s. Translation streams are output marks: `i18n:translate` and `i18n:name` collect
their text, the message id is normalized as `__re_whitespace(...).strip()`, and every
`translate` call passes exactly the arguments Chameleon's code passes at that site (including
where `target_language` comes from: the render function's local or the scope). `yield` in an
expression or code block makes its function render nothing, as Chameleon's generator
functions do. The `Runtime` trait is split into `Values`, `Eval`, `Names`, `Loops`, `Errors`,
`Attrs`, `Macros` and `Translate` (blanket `Runtime`). Decisions, quirks reproduced and
deviations: architecture.md, "Phase 6b amendments".

Results:

- **Fixtures (level 2): all 122 renders byte-exact**, the loader-dependent ones included (066,
  081, 082, 083, 089: the minimal `PageTemplateFile` and `load:` of Phase 5 suffice); `LATER` in
  `tests/python/test_fixtures.py` is empty.
- **Corpus (229 Talk Python templates):** **229 compile fully** (75 in Phase 6a; trees still
  match Chameleon's). New render smoke test (`test_corpus_renders_like_chameleon`): each
  template rendered by both engines as a file template (layouts through `load:` macros), every
  name it needs bound to one shared stand-in object (a `str` subclass with every attribute,
  callable, indexable and iterable), found by rendering until no `NameError` is left: **228
  render identically, 1 raises the same error with the same formatted message**, 0
  differences; a second pass with a false, empty stand-in (`else` branches, empty loops):
  also 228 and 1 (20 s for both). A plumbing signal, not Phase 8's parity on real data.
- **Differential tests:** `test_macros.py` (65: in-place and called macros, path and Python
  macro expressions, mangled names and lookup errors, errors through macro and slot frames with
  whole formatted messages, extend-macro chains, re-exposed and repeated slots, slots in loops,
  scope copies and `econtext.update(rcontext)`, slot stacks visible in `econtext`, code blocks
  per frame, templates used as macros, other templates' builtins, recursion depth, files and
  `load:`, macros across engines, `yield`), `test_i18n.py` (187: every translation site with
  and without a target language, implicit translation, and **the translate protocol**: every
  call the translation function receives, positional and keyword arguments in order, compared
  with Chameleon's), a free-threading test of macros, slots and translations (4,000 renders on
  16 threads, compared with Chameleon's output).
- **Render fuzz:** a second generator for macros, slots and i18n (define/use/extend-macro,
  fill and define-slot, translate with names, domain, context, target, i18n:attributes,
  implicit translation on and off, two target languages, guarded failures), translation
  function showing all its arguments: **30,000 examples, zero differences**, and the Phase 6a generator 30,000 more, on the final build (9 minutes for both).
  After the architect's review, its translation functions also return shorter, empty and
  non-ASCII results: **30,000 more examples, zero differences** (the stale-mark fix below verified first). Runs found and fixed: the on-error fallback of a translated element is translated; a name
  left unfinished by a caught error maps to its stream (a list); an outer handler cuts the
  translation streams opened inside its body.
- **Recursion:** Chameleon's macros nest as Python calls, so the budget is now the
  interpreter's recursion limit less the frames on the stack (asked once per render, when
  nesting first passes 64): an `on-error` at every level catches at **the same depth as
  Chameleon** (993 for macros, 497 for a template used as a macro, 992 with slots, at the
  default limit; also with a raised limit). The message is CPython's ("maximum recursion depth
  exceeded").
- **Free-threading:** CPython 3.14.7t, wheel built as in Phase 6a: GIL stays disabled;
  `test_threads.py`, `test_errors.py`, `test_repeat.py`, `test_macros.py`, `test_i18n.py` (626
  tests) passed five times in a row with `-W error::RuntimeWarning`; the whole suite passes on
  3.14t, 3.12 and 3.13 too.
- **Speed (release, load average 1.5-2):** `bench/bench_ab.py f04b927`, 16 rounds: **+2.9 ns** median (range -4..+9; tolerance +5), 6.08x Chameleon in the same run (an earlier run of the same build at load 1.7: +1.7 ns, range -4..+6). A dummy change to the baseline alone measures +2.5 ns (range -2..+5): code layout. `bench_skeleton.py`: listing 476-479 ns per item against Chameleon's 2,854 (6.0x); partial listing 635-637 ns against 6,780 (10.7x).
  Building it took work: the first complete build measured +13 ns per listing item; the
  causes were real, not layout: `execute_rare` took `pc` by `&mut`, which kept the loop's `pc`
  (and the program pointer) in memory, two extra memory operations per instruction (fixed: by
  value); and frames had grown (176 bytes, a `Box<Pushed>` per call, i18n clones), which cost
  native partial calls 26 ns each (now about 5-7: a separate `Partial` action, plain frames
  with the rarely used state in a boxed `extra` reused from a pool, since a free on macOS's
  allocator calls `mach_absolute_time`). New `bench/bench_macros.py` (both engines, outputs
  asserted identical), two runs: a layout page (`listing_inline.pt`, a 50-video page filling the
  `shared/_layout.pt` slot through `load:`) **28.0 us per render against 150.7 us (5.4x)**; a
  macro call filling a slot per item **987-993 ns against 4,611-4,660 ns (4.7x)**; a translated
  element with two names and a translated attribute per item, default translation function,
  1,359-1,377 ns against 2,617-2,631 ns (1.9x: the Python `translate` call dominates).
- **Review fixes (2026-10-21, after the Rust Architect's review; 69 extra probes, all matching
  but one):** closing a translation, name or capture left the frame's fallback mark pointing
  into the replaced text, so an outer handler cut inside the translation (and could split a
  multi-byte character: a `UnicodeDecodeError`); the mark now moves to the stream's start
  (output matches the documented deviation, not Chameleon's item-count cut; pinned by three
  cases in `test_i18n.py`). Non-UTF-8 stream text is an internal error (lone surrogates
  excepted); spare `extra` boxes keep their slot vectors. A/B after the fixes, `bench_ab.py
  f04b927`, 16 rounds, load average 2.6-2.9: **+3.5 ns** median (range -17..+29), 5.89x.
- **Tests:** 94 Rust (88 core: macros as frames with a scope copy, slots and extend, the frame
  budget, translations and their names, a slot writing to its caller's stream; 6
  `chameleon-py`), 1,997 pytest passed, 2 skipped and 1 strict xfail (the slot-stream deviation) (the Phase 8 walrus probe, the
  free-threading check on a GIL build), the upstream suite on the reference engine 227 passed
  and 6 xfailed; `make lint test` green with `CHAMELEON_RS_CORPUS` set.

Carried forward:

- **Phase 7:** `Macros`/`Macro` are minimal (`macros['name']`, `macros.names`,
  `template['name']`, `template.include`); a `macros` attribute access (`macros.main`, as the
  upstream recursion test uses) works through `lookup_attr`'s `__getitem__` fallback.
  `Macro.__call__` from another engine converts a foreign scope to ours (its items).
- **Phase 8 / known differences, by design (architecture.md, Phase 6b amendments):** Chameleon's
  one `__fallback` register per function, set inside a translation stream and used by a
  handler outside it, deletes from the outer stream by the inner stream's *item count*; we cut
  at the inner stream's start. An unfinished `i18n:name`'s list holds the text so far as one
  piece (Chameleon: one item per append). A translation function returning a non-`str` for an
  `i18n:translate` element raises `TypeError` at once (Chameleon: at the final `''.join`, with
  the item's index). Inside a slot body, a nested macro call writes to the slot's own stream
  (Chameleon passes the define-slot site's `__stream` to it).
- **Phase 8:** a strict-xfail test pins the slot-stream deviation
  (`test_macro_called_inside_a_slot_body_writes_to_the_define_slot_stream`: Chameleon
  `<b><y>T  U</y>F<a>K</a>G</b>`, ours `<b><y>T <a>K</a> U</y>FG</b>`).
- **Phase 9:** native partial frames are plain frames (`Action::Partial`, no `extra`); keep it
  so. `PARTIAL_COST` (4) mirrors chameleon_partials' call depth for the frame budget.
- **Phase 10, macro speed:** a macro call copies the scope twice per item when it fills a slot
  (the call and the slot body), as Chameleon does; `PyDict_SetItem` is the largest cost of the
  macro benchmark. Candidates: skip the slot body's scope copy when the body cannot observe it
  (a lowering-time flag: no `tal:define`, `tal:repeat`, code block, nested use-macro or
  fill-slot, no Python expression naming `econtext`); cached bound methods or vectorcall for
  the deques' `pop`/`appendleft`; skip `getattr("include")` for an exact `Macro`; a bulk insert
  in `Scope::copy`.
- **Phase 11:** for async renders, use a fixed recursion budget (the recursion limit /
  `DEFAULT_FRAME_BUDGET`) instead of walking `sys._getframe` at a resume, where the stack is
  the event loop's.

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

**Done (2026-10-21).** `chameleon_rs` now has Chameleon's class layout and configuration
surface (`BaseTemplate`, `BaseTemplateFile`, the page template classes, `PageTemplateLoader`
with `formats`, `MemoryLoader`/`ModuleLoader`, `Macros`), every keyword Chameleon accepts
(including `default_marker` and `tokenizer`, which needed native support), the `CHAMELEON_*`
settings, thread-safe `cook_check` and `auto_reload`, Chameleon's `TemplateError` with
`Token`s (compile errors now print the `- Source:` lines), `ExpressionEvaluator`, and
`chameleon_rs.compat.install()`, which makes `import chameleon` give chameleon-rs without
stock Chameleon installed. Per-module decisions (Rust-backed, vendored, ported, not
provided), the lone-surrogate decision and the deviations: architecture.md, "Phase 7
amendments".

Results:

- **Level 1: 227 of 227** upstream tests pass on chameleon_rs (`make test-upstream-rust`,
  `CHAMELEON_ENGINE=rust`, unmodified tests; the 6 environmental xfails are excluded as for the
  reference). `expected_failures_rust.txt` is empty. The harness maps resources of the package
  `chameleon` to the vendored suite (three fixtures load `chameleon:tests/inputs/...`), as it
  already mapped `chameleon.tests`. `make test` now runs the upstream suite on both engines, so
  CI checks the ratchet on every matrix entry.
- **Level 2:** all 122 fixture renders byte-exact (unchanged), loader fixtures included.
- **Level 4: all integration suites pass**, unmodified, on chameleon-rs through the compat
  alias, in a scratch Python 3.14 venv without stock Chameleon (`make test-integration`;
  robyn has no 3.15 wheels): fastapi-chameleon 40, chameleon-flask 16, chameleon-robyn 39,
  chameleon_partials 6 (pinned commits in the Makefile; a new CI job runs them on Linux and
  macOS).
- **Tests:** `tests/python/test_api.py` (304): compile-error messages whole, for strings and
  files, against Chameleon (16 shapes each, plus non-strict render-time ones); custom
  `default_marker` and `tokenizer` renders against Chameleon; every configuration keyword; the
  loader (search paths, `default_extension`, `formats`, caching, package resources, absolute
  paths, `prepend_relative_search_path`, `auto_reload`); 8 threads rendering while the file is
  rewritten 29 times (no errors, no stale or torn output, versions compiled in order) and 16
  threads compiling once; `CHAMELEON_*` settings in subprocesses; `ExpressionEvaluator`
  against Chameleon's; `iter_xml` and `ElementParser` items token for token against
  Chameleon's for every fixture and for streams from several sources or plain strings; macros;
  `compat.install()` with stock Chameleon blocked and replacing it; and a run that imports
  every module with all non-stdlib imports blocked (no runtime dependencies). 96 Rust tests (90
  core, with given tokens and namespace positions new; 6 `chameleon-py`), 2,301 pytest
  passed, 2 skipped, 1 strict xfail (with `CHAMELEON_RS_CORPUS` set); upstream 227 passed on each engine; also green on
  Python 3.12 and on free-threaded 3.14t (GIL disabled, `-W error::RuntimeWarning`).
  `make lint test` green with `CHAMELEON_RS_CORPUS` set (corpus: 228 identical renders, 1 same
  error, unchanged).
- **Speed (release, load average 2-4):** `bench/bench_ab.py 86f2c21`, 16 rounds, twice:
  **+3.6 ns** (range -0..+7) and **+1.6 ns** (range -2..+33) per listing item, 6.0x Chameleon
  in the same run; within the +5 ns tolerance. The render path did not change; the Python
  wrapper costs about +50 ns per render (the `_render` and defaults checks), 0.1 ns per item.
  Not part of the gate: the partial listing measured **+12 ns per item** (two native partial
  calls) in the same kind of A/B, twice, with byte-identical program dumps and no change to
  the runtime's partial path; unexplained (code layout is the likeliest cause), carried to
  Phase 9, which replaces the prototype. `bench_skeleton.py`: listing 500-505 ns against
  Chameleon's 2,925 ns (5.8x; the machine ran ~2.5% slower than in Phase 6b's run), partial
  listing 664-666 ns against 6,800 ns (10.2x).

Review fixes (2026-10-21, after the Rust Architect's review; each checked against Chameleon
first): **blocker:** a file template whose `cook` sets `_render` (pyramid_chameleon's `macro=`
renderer) rendered the whole page on its first render, because the `_render` check ran
before `cook_check()`; fixed and tested with pyramid_chameleon's subclass. Both test harnesses
now clear `CHAMELEON_EAGER`/`_DEBUG`/`_CACHE`/`_RELOAD` first (the owner's shell had
`CHAMELEON_EAGER=1`, which was also the cause of the six "environmental" upstream failures:
they now pass on both engines). `chameleon.tales.TalesExpr` (with `resolve_global`,
`transform_attribute`) vendored, and a custom `TalesExpr` subclass tested against Chameleon;
`tal`'s parsing helpers vendored and differential-tested; `install()` now aliases every public
module, stops any other `chameleon.X` with `ModuleNotFoundError` (no second copy of
`_native` or `compat`), and warns when it replaces stock Chameleon modules; a `default_marker`
must be importable, as in Chameleon; `ExpressionEvaluator` evaluates in the caller's `Scope`
(new `Program.render_scope`); the loader cache shares one template between concurrent loads;
a compilation is one `(program, builtins)` attribute; span offsets are checked before the
`u32` conversion; the per-template lock's deadlock risk is documented; the robyn leg of CI
has cp314 manylinux wheels (0.88.0). **Not done:** lone surrogates in the source keep being
refused: the review's private-use mapping touches the core's text API and some twenty call
sites (architecture.md, Phase 7 amendments), carried to Phase 8. After the fixes:
`tests/python/test_api.py` 364 tests, 2,361 pytest passed, 2 skipped, 1 strict xfail (corpus
set, `CHAMELEON_EAGER` unset), upstream 227 passed on each engine (6 environmental xpassed),
integration suites unchanged (40, 16, 39, 6). A/B against 86f2c21, release, five runs:
+2.1, +1.8, -0.7, -4.5, -0.6 ns per listing item (5.97-6.30x Chameleon; the last three at load
6-8). One intermediate build measured bimodally (+0.5 to +7.7 ns over seven runs) with the
scope handling inside `Program.render`; moving it to `render_scope` and sharing the render
body out of line brought it back.

Carried forward:

- **Phase 8:** lone surrogates in the template source (Chameleon renders them in text,
  attributes and `string:`; we refuse them): the private-use mapping described in
  architecture.md, with differential tests.
- **Phase 9:** the partial listing's +12 ns per item in the A/B against 86f2c21 (see above).
- **Phase 8 / known differences** (architecture.md, Phase 7 amendments): a custom
  `default_marker` is per render, not per template, across templates; a user `tokenizer`'s
  error positions are into its tokens joined; `ExpressionEvaluator` ignores `rcontext` (and
  copies a plain dict, which Chameleon rejects);
  messages naming `chameleon_rs.*` classes (`ErrorInfo`'s repr, the scope's qualified name).
- **Phase 12:** `chameleon.nodes`, `chameleon.program` and `chameleon.zpt.program` are not
  provided (Chameleon's code generator); documented in the compat module.

## Phase 8: Real-site verification

Compatibility is goal #1, so this comes before performance work.

- Crawl-and-diff: render every Talk Python route with each engine and compare HTML word for
  word. Non-deterministic values (CSRF tokens, timestamps) are pinned by the harness, not
  masked.
- Shadow mode: a compat setting that renders every template with both engines and logs any
  difference, for staging on real traffic.

Exit gate: level 5, zero differences (or each one explained and fixed).

**Done (2026-10-21).** Level 5: both of the owner's production applications render
byte-identical pages on both engines. Details: architecture.md, "Phase 8 amendments".

- **Harness** (`scripts/realsite.py`): one engine per process, a private adapter per
  application (kept with the application, not here: app setup, routing table, parameter
  values, an in-process GET client), everything non-deterministic pinned rather than masked
  (`PYTHONHASHSEED=0`, frozen clock, `random`/`os.urandom` seeded per URL so CSRF tokens and
  uuids repeat, outbound network refused, fixed run directory), byte-for-byte comparison with
  minimal diffs written outside the repository. Rendered pages, diffs and timings stay in a
  scratch directory.
- **How the applications ran:** two production Flask/Quart applications, each copied into a
  scratch directory with its own Python 3.14 venv (its pinned requirements, stock Chameleon at
  the reference commit, chameleon-rs built into it), on local fixture databases, with dummy
  secrets and no outbound traffic. Templates, partials (chameleon_partials) and the Flask/Quart
  integration (chameleon-flask) went through the compat aliases unchanged.
- **Coverage:** every GET rule of both routing tables, with parameter values from the
  databases (every instance of the main entity of one application, samples of the rest),
  query-string variants and not-found values, anonymous and signed-in (several account
  states, admin). **10,145 URLs, 5,289 template renders; all responses byte-identical.** The
  first runs found 10 differing pages, none from the engine: run-directory paths, live process
  figures (pinned in the adapters), and one template printing a function instead of calling it
  (its repr holds an address; pinned with a stable repr; worth fixing in the application).
- **Shadow mode** (`chameleon_rs.shadow`, opt-in, zero cost when off): stock Chameleon serves
  and each sampled render is repeated with a chameleon-rs twin (partials and template arguments
  included), differences reported; or `serve='rust'`. Crawling both applications with it on:
  0 renders differed. Each sample doubles the render's latency (blocking an asyncio loop for
  both renders), so `sample_rate` should stay well below 1. Tests: `test_shadow.py` (13).
- **Lone surrogates in the source** (carried from Phase 7): rendered as in Chameleon through
  a private-use window, Python expressions failing with Chameleon's `UnicodeEncodeError`;
  character references to surrogates too; text taken back from the output (translations,
  names, captures) stays WTF-8 to the translation function. `test_surrogates.py`: 76 cases,
  all compared with Chameleon. A source using its private-use characters so densely that no
  2,048 free code points remain is still refused.
- **Slot streams** (the Phase 6b known difference, a strict xfail): now as in Chameleon (a
  macro or slot called inside a slot body writes to the define-slot site's stream; a handler in
  the body cuts only that stream). The xfail is a passing test, plus 19 variations compared
  with Chameleon. Two narrower differences remain, pinned by strict xfails (architecture.md,
  Phase 8 amendments): in such a slot body, a handler keeps the text before a failing
  expression (Chameleon appends a text node at once), and conversions use the body's
  `i18n:context` (Chameleon: the defining function's; also at Phase 7). Render fuzz,
  macros/slots/i18n (now generating macros whose slot sits in a translation, and slot bodies
  that define slots or call macros) and the Phase 6a generator: 15,000 examples each, zero
  differences.
- **Review fixes (2026-10-21, after the Rust Architect's review; each checked against
  Chameleon first):** **blocker:** the first slot-stream fix restarted a slot body's output
  after every call, also when nothing had been flushed (the body had its own translation open,
  or called a partial), orphaning its earlier output (garbled pages; a case that matched
  Chameleon before): the restart now follows only a flush (`Extra::flushed`), on return or
  error; slots called from a slot body flush too, which also fixed three nested-define-slot
  cases that differed at Phase 7. Stream text no longer round-trips through the surrogate
  window (a value's private-use character came back as a surrogate, a macro of a surrogate
  template inside a translation gave U+FFFD, a value's surrogate in a translation gave U+FFFD
  even at Phase 7): it stays WTF-8 to the runtime. Shadow mode can no longer raise into the
  request (a Rust panic is a `BaseException`; a failing `str()` or report is logged), and logs
  capped, deduplicated diffs with each output's length and hash. Numeric references are read
  as Chameleon reads them (Unicode digits). Conversion errors in the runtime are propagated
  instead of swallowed where the signatures allow. The harness refuses to write inside the
  repository; this block names no private infrastructure. All page comparisons re-run after
  the fixes: still 10,145 of 10,145 identical.
- **Real-page render time** (release build, outermost template render only, median of 15
  re-renders inside the request, same arguments, on a busy machine). With the applications'
  development-mode cache busting, which re-reads and hashes static files from inside templates
  on every call: 1.39x and 1.03x Chameleon in total. With production's memoized cache ids:
  **1.90x and 2.51x in total** (per-page medians 2.08x and 2.41x; p10 1.32x/1.68x, p90
  2.54x/2.75x; the heavier application's median page 4.1 ms against 2.0 ms). The rest of each
  page's time is the applications' own Python called from templates (markdown, view models,
  formatting), which both engines pay alike, so request-level gains are smaller than these
  outermost-render ratios (a request also routes, loads data and builds the view model).
  Repeated renders were identical on every page.
- **Speed (A/B, release):** `bench/bench_ab.py 6f0caae`, 16 rounds. Before the review fixes,
  twice at load average 13-24: -0.1 ns (range -67..+119) and +1.3 ns (range -20..+69). After
  them, three runs each started at load average 4.7-4.9 (rising to 7-8 while the machine's
  other work resumed): **+4.8 ns** (range -7..+34), **-4.6 ns** (range -106..+100), **-5.4 ns**
  (range -91..+61) per listing item, 5.9-6.0x Chameleon: no detectable change, on a noisy
  machine (the tolerance is +5 ns).
- **Tests:** 98 Rust (92 core, with the surrogate module's; 6 `chameleon-py`); 2,470 pytest
  passed, 2 skipped, 2 strict xfails (the two known differences above), with
  `CHAMELEON_RS_CORPUS` set and `CHAMELEON_EAGER` unset (`test_surrogates.py` 76,
  `test_shadow.py` 13, 19 slot-stream variations); upstream 227 passed on each engine (6
  environmental xpassed); `make lint test` green; `make test-integration` green
  (fastapi-chameleon 40, chameleon-flask 16, chameleon-robyn 39, chameleon_partials 6).

Carried forward:

- **Known differences** (architecture.md, Phases 6b, 7 and 8): the `__fallback` register cut
  by item count across a translation stream, an unfinished name as one piece, a translation
  function returning a non-`str` in a translated element, a custom `default_marker` per
  render, a user `tokenizer`'s error positions, `ExpressionEvaluator` ignoring `rcontext`,
  class names in messages; new in Phase 8: a handler in a slot body bound to another stream
  keeps the text before a failing expression, and conversions in a slot body use its own
  `i18n:context`/`domain` (strict xfails; the latter would need the runtime's conversions to
  read the slot's defining-function settings).
- **Phase 10, priorities (the architect's, after Phase 8):** (1) profile real pages first
  (`make build-profiling`, py-spy, `scripts/realsite.py --timing`), attributing time to engine
  glue against application code; (2) the Python wrapper per render (`template.py`: `Scope(**kw)`,
  the `cook_check` stat, the defaults key, `output_stream_factory`) into one native render fast
  path; (3) partials: a native partial call reusing the caller's VM and buffer (overlaps
  Phase 9); (4) calls and attribute lookups: vectorcall with interned names, no tuple or dict
  for 0-1 arguments, `__html__` presence cached per type keyed by the type version tag;
  (5) conversion fast paths: exact `str`/`int`/`Markup` before the generic `convert_text`, no
  escape pass when nothing needs escaping; (6) macro and slot overhead: copy-on-write
  `copy_scope` and `econtext.update(rcontext)`.

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
