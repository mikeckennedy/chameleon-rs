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

## Phase 2 amendments (2026-10-20)

The walking skeleton followed the design; these are the places where building it changed or
pinned down a detail. Each decision's text below still describes the intent.

- **D1, the `ExprCompiler` signature:** `compile(&mut self, source, span, cx: ExprContext)`.
  `ExprContext` replaces `ExprKind` and says whether `default` is the marker alias (lexically:
  `tal:content`, its default body, dynamic attributes), whether the value is emitted (the only
  place a native frame call may appear), and whether this is an Interpolator candidate. TALES
  prefixes and pipes are parsed by the implementation, not the core. The structural pre-filter
  runs only on candidates; when the last candidate fails, the core compiles it once more
  without the shortcut so the error carries the parser's own message.
- **D2, the `Runtime` trait as built:** `activate(code, scope)` (called on every frame
  change), `eval(expr, emitted) -> Step` with `Step::{Ready, Suspend, Call}`,
  `emit(value, esc, lone) -> Emitted` (written or nothing; `lone` marks a whole interpolation,
  see D6), `is_default`, `truthy`, and a loop protocol that mirrors Chameleon's generated code:
  `lookup` (the two-level backup), `repeat` (calls whatever `repeat` is bound to and returns
  `iter()` of its first result plus a `Countdown`: an `i64`, or the runtime's own length value
  when it is not an exact int, decremented through `countdown` so a `None` or float length
  fails or works exactly when Chameleon's `index -= 1; if index > 0` does), `set_null`,
  `next` (must not suspend), `assign`, `restore`. `define`, `translate` and `is_catchable`
  arrive with the features that need them. Frames hold
  `(Arc<Program>, pc, code: V, scope: V, on_return, unwind_base)`.
- **D3, frames:** an emitting instruction whose expression returns `Step::Call` pushes a frame
  that writes straight into the caller's buffer (an attribute's closing quote is written on
  return). Depth is capped at `MAX_FRAMES = 500`, raising `RecursionError`. The single unwind
  stack exists from the start (`Unwind::Loop` now; define backups, `on-error` and translation
  marks join it): each frame records its `unwind_base`, popping a frame drops its entries, and
  a frame that ends with entries left is an internal error. Every instruction either completes
  or leaves no trace (`LoopNext` fetches the next item before it touches the countdown or
  writes whitespace), and a resumed value replaces the expression the instruction was
  evaluating; the toy-runtime tests suspend at every expression kind, inside callee frames and
  inside loop bodies with frame calls. `resume_with` on a render that is not suspended, and
  `run` on a suspended one without it, are errors. `VmState::for_each_value` visits every
  held value, for the future awaitable's `__traverse__`.
- **Loop order (correcting spike 1's "not validated" note):** Chameleon backs the loop names up
  *before* evaluating the iterable but assigns `None` *after* registering it with `repeat`
  (compiler.py:1863-1876; verified: `tal:repeat="i [i]"` raises `NameError`, and with
  `i='outer'` the iterable sees `'outer'`). The skeleton does exactly that.
- **The `repeat` variable** is a port of Chameleon's Python `RepeatDict`/`RepeatItem`
  (`chameleon_rs.tal`): exact, including user-supplied `repeat` objects, at one Python call
  per loop (not per item). A native version only if a profile asks for it.
- **D5:** like Chameleon's (a `__slots__` class), the native `Scope` is not
  weak-referenceable. `__traverse__` visits the root; the upstream doctest's `gc.collect() == 0`
  and a root cycle test pass.
- **D6, escaping contexts:** `Text` (quote NUL), `Content` (`tal:content` passes no quote, so
  NUL is never replaced there), `Structure`, and `Attr(Double | Single | Bare)`. An unquoted
  attribute value that triggers escaping gets `&#0;` around every character, as Chameleon's
  `s.replace('', '&#0;')` does. Conversion has Chameleon's two stages: each `${...}` goes
  through `__quote` (convert, escape if the result is a `str`); then a *lone* interpolation
  (`attr="${x}"`, or a text node that is just `${x}`) is converted again with `emit_convert`
  (no escaping) and appended, or `%s`-formatted in an attribute, while a part of a mixed
  interpolation is `%s`-formatted. So an `__html__()` or `translate` returning `5` renders
  `5` everywhere, `b'x'` renders `x` alone but `b'x'` (its repr) inside `a${h}b`, a list
  renders its `str()`, and `None` writes nothing (dropping a lone attribute). Only
  `tal:content` (text or `structure`) appends the first-stage result directly, so a non-`str`
  there is a `TypeError`, as in Chameleon. A grid test covers value kind x source x position.
- **Fallback names:** the rewriting (`chameleon_rs._rewrite`, a port of `NameTransform` and
  friends) gives `econtext.get_name(x)`, `econtext.get(x, builtin)`, `econtext['x'] = ...` for
  stores, `econtext['__translate']` for `translate`/`decode`/`on_error_handler`, and leaves
  `__` names alone; the fast path sends `__` names to the fallback.
- **D10, partial prototype:** `render_partial(path, **kw)` written as a whole emitted
  expression becomes a native frame when no `render_partial` is bound in the scope (one passed
  in, like `chameleon_partials.render_partial`, is called normally). Names resolve against a
  registry passed to `PageTemplate(partials=...)`. The partial's scope holds only its keyword
  arguments, plus a fresh `repeat` when the partial loops or mentions `repeat`;
  `__translate`/`__decode`/`target_language` fall back to the template defaults. Unlike
  chameleon_partials, `econtext` in a partial therefore lacks those keys and `render_partial`
  itself; Phase 9 decides whether that matters. The gate is the absolute figure against the
  0.8 µs budget; the ratio to Chameleon + chameleon_partials is only a lower bound on the real
  feature's speedup, because the prototype skips path resolution, the loader cache lookup,
  the `Markup` round trip, and putting `__translate` and `render_partial` into the partial's
  scope. Deferred: `rcontext` persistence across a suspended render and a `Mutex`-guarded
  awaitable (Phase 11); the redundancy of `Call { program, code }` (the `Arc` is reachable
  from `code`); the partial registry (Phase 9).

## Phase 3 amendments (2026-10-20)

The tokenizer and element parser are exact ports; these are the decisions that made them so,
and the places where the port pins down or changes a Phase 2 detail.

- **Regexes kept verbatim, engines chosen per pattern.** `XML_SPE` is assembled in Rust from
  Chameleon's `recollector` table with the same `%(Name)s` expansion (a pytest asserts every
  one of the 28 expanded patterns equals `chameleon.tokenize.collector.res`).
  - Patterns without lookaround or backreferences run on the `regex` crate, whose
    leftmost-first matches and groups are the ones a backtracking engine reports, in linear
    time: the tokenizer's `<!` (comments, CDATA, doctypes), `<?` and `</` branches, and the
    parser's `match_tag_prefix_and_name` and `match_processing_instruction`.
  - The patterns with lookaheads are matched by hand: `ElemTagCE` (`(?!(?:S)?=)`),
    `match_single_attribute` (two lookaheads and a backreference) and `match_double_hyphen`.
    Each is derived from the pattern's backtracking priority order (the reasoning is in the
    doc comments) and checked against the verbatim pattern on fancy-regex by proptest, and
    against Chameleon by the Hypothesis differential.
  - fancy-regex is a dev-dependency only. **Its backtracking stack is fixed at 1,000,000
    entries** (not configurable in 0.19), so a markup token of about 1 MB (a long comment,
    CDATA section, data-URI attribute value, or a quarter of a million attributes) fails with
    `StackOverflow` where Python's `re` succeeds. The first Phase 3 build ran on it and turned
    that error into a lone `<` token silently (a 1.1 MB `title` made a `tal:condition="False"`
    paragraph a `ParseError`); the review caught it. Production matching now has no failure
    mode at all, and deterministic tests feed over-1 MB tokens of every kind to both engines.
- **Python's character classes.** `crate::pyre` translates the syntax differences: `\s`, `\d`
  and `\w` become explicit classes generated from Python's own `re`
  (`scripts/gen_pychars.py` writes `pychars_table.rs`; Python 3.15, Unicode 17). Python's `\s`
  includes `\x1c`-`\x1f` and its `\w` is `str.isalnum()` plus `_` (no combining marks, all
  numerics), both unlike Rust's; inside a class `[`, `&` and `~` are escaped (Rust gives them
  meaning there), and a bare `]` outside one. The same tables replace Phase 2's approximate
  `\d` / `\w` in entity decoding. **Known deviation:** on Python 3.12-3.14, whose Unicode
  databases are older, the tables also classify code points assigned only in Unicode 16-17
  (3.12 counts 680 `\d` and 137,936 `\w` code points against the tables' 770 and 147,597).
  Such characters are unassigned for that interpreter (`Cn`); the pytest checks that every
  difference is one of them, and verified on 3.12 and 3.14.
- **Tokenizer dispatch.** `XML_SPE` is `[^<]+|MarkupSPE` and every `MarkupSPE` match starts
  with `<`, so text runs are found with memchr; at a `<`, each branch of `MarkupSPE` can match
  empty after its lead character and nothing follows the group, so the character after the
  `<` picks the branch exactly. The literal `finditer` on fancy-regex stays as a test
  reference. Tokenize plus parse is 3-4x faster than Chameleon's (Phase 3 results in
  plan.md).
- **Item kinds as `ElementParser` yields them:** text, comment, cdata, default (declarations
  and doctypes, tokens `identify` calls errors, processing instructions its regex rejects),
  processing_instruction (name and text spans), start_tag (unclosed) and element; the XML
  declaration is an element named `?xml`. Phase 2 rejected declarations, CDATA and
  processing instructions as unsupported; the builder now outputs declarations and CDATA
  without `${` as Chameleon does, visits a non-`python` processing instruction as text, and
  treats a missing tag suffix or space as empty (`Token.__add__(None)` returns the token, so
  `<p>a</p\n` renders `<p>a</p`). Interpolated CDATA and `<?python ?>` wait for Phase 4.
- **Errors as Chameleon raises them:** `ParseError` with the token (`Unexpected end tag.`,
  `The string '--' is not allowed in a comment.` on the `--` itself), a bare
  `KeyError('Undefined namespace prefix: x.')` under `restricted_namespace` (Phase 2 raised
  a `ParseError`), and a bare `AttributeError` for a tag with no name (`</`), which Chameleon
  hits by calling `groupdict()` on a failed match. The core's `ErrorKind` gained `Key` and
  `Attribute` for these.
- **Thread safety:** parser options and trees use `Arc`, so `ParseOptions` (which Phase 7
  keeps in frozen pyclasses) and `Program` are `Send + Sync`, asserted at compile time.
- **Namespaces.** The stack reproduces Chameleon's quirk of popping once per end tag even
  when the end tag closes several unclosed start tags, so prefixes declared on an unclosed
  element stay visible after its parent closes. Each tag keeps an `Rc` snapshot of its prefix
  map (`ns_map`); Chameleon shares the dict, which differs only if an end tag carried
  `xmlns` attributes, which `iter_xml` never produces. A namespace URI remembers whether it
  came from the defaults (a plain string) or an attribute (a token, with its position); so do
  the empty strings Chameleon substitutes for an unquoted or valueless attribute
  (`AttrKind`).
- **`Source`:** byte spans everywhere; code-point offsets and `Token.location`-style
  `(line, column)` come from a line index built on first use (ASCII sources skip counting).
- **Encoding sniffing stays in Python** (`chameleon_rs.utils.read_bytes`, `detect_encoding`,
  `read_xml_encoding`, `read_encoded`): the bytes are decoded with whatever codec a BOM, an
  XML declaration or a `<meta http-equiv>` names, which needs Python's codecs, and running
  Chameleon's `re` patterns unchanged makes the behaviour identical by construction. It runs
  once per file load, off the render path. Differential tests (fixed and generated bytes)
  compare all three functions with Chameleon's.
- **`_native._testing`** returns tokens and trees normalized to plain data (`(text, pos)`
  for a token, `(text, None)` for a plain string, Chameleon's tag dict keys) and raises
  parse errors exactly as compiling does, for the differential tests.
- **Open for Phase 7:** the parser takes spans of one source string, so a user-supplied
  `tokenizer` (a Chameleon option) whose tokens are not slices of the body needs a different
  entry point; a template containing lone surrogates cannot become a Rust `str` at all
  (Chameleon accepts it), which Phase 7 must decide.
- **`cargo-fuzz` skipped:** it needs a nightly toolchain and the project pins stable 1.99;
  proptest covers the no-panic and coverage invariants, Hypothesis the differential ones.

## Phase 4 amendments (2026-10-20)

The ZPT program builder is a port of `zpt/program.py`, `tal.py`, `i18n.py` and `metal.py`;
these are the decisions that shaped it and the places where it changes earlier interfaces.

- **Three passes instead of one.** Chameleon builds the whole program tree first and compiles
  expressions afterwards (macros before the main body), so of a builder error and a bad
  expression it reports the builder error, and of two bad expressions the one its compiler
  visits first. The core now has the same split: `zpt::build` (program.py, never calls the
  expression compiler) produces the typed tree of `nodes.rs`; `resolve` walks it in
  `compiler.py`'s visit order (an `on-error` fallback before the element, a content
  expression before its default content, `tal:attributes` dictionaries before the other
  attributes, a `use-macro` element's slots before its macro expression), keeps the
  compile-time aliases and the expression cache, raises the compiler's own errors, and fills a
  table of compiled expressions indexed by `ValueId`; `compile::lower` turns tree and table
  into bytecode. The tree is independent of the expression compiler, which is what the
  `insta` snapshots and the differential inventory test compare.
- **`Tok`: Chameleon's `Token` arithmetic.** Positions shown to users follow `Token`'s own
  methods, which are not always the text's real location: `Token.split` advances by each
  part's length without the separator (`tal:define="x 1;y 2;z 3; econtext 1"` reports
  `econtext` at 25, not 28), entity decoding keeps the original position for shorter text,
  `strip` moves past what it removed. `Tok { text, pos: Option<TokPos> }` reproduces this;
  `TokPos` is a byte offset plus code points, resolved only on the error path. A `Tok` without
  a position is a plain `str` (`'<?' + name + text + '?>'`): Chameleon then reports offset 0,
  location (0, 0) and no filename. `CompileError::Template` carries an `ErrToken` (a source
  span or a `Tok`) instead of a span, and `Source::location_of_offset` handles positions past
  the end of the source, which the split arithmetic can produce.
- **D1, `ExprCompiler` as built:** `compile(&Tok, ExprContext)` and `compile_code(&Tok)` for
  `<?python ?>` blocks. `ExprContext` gained `default: DefaultAlias` (`Name`, `Marker`, or
  `TargetLanguage` inside `i18n:target`, replacing the `default_is_marker` flag) and `attrs`
  (the enclosing element's static attributes, or `None` outside every element): both are
  compile-time aliases, lexically scoped exactly as Chameleon's `Define`/`Alias` nodes scope
  them, and macro bodies start without the aliases of the elements around their definition.
  `ExprCompileError::Invalid` carries the token the message is about (the stripped Python
  part, as `PythonExpr.translate` reports it); `chameleon-py` threads `Tok` through the TALES
  prefix and pipe splitting for that. The fallback rewriter applies the same aliases, after
  stores and before builtins, as `NameTransform` does. `attrs` is one dict per element
  (`StaticAttrs::id`), shared by all its expressions and by every render, exactly like
  Chameleon's compile-time `Static` (a template that mutates it sees the change next render;
  that is a free-threading hazard in Chameleon too, kept for parity). `chameleon-py` memoizes
  the dict per id; the expressions and fallback globals that reference it keep it alive.
- **D2, the `Runtime` trait** (its methods are grouped in documented sections: frames and
  evaluation, output, names, loops, attributes and `tal:case`; a sub-trait split is due with
  Phase 6b): `emit` takes a `Stage` (`Content`, `Lone`, `Part`) instead of a
  `lone` flag, because interpolation without escaping (CDATA, text mode) needs the lone/part
  conversion stages while `tal:content structure` appends the first stage directly. New
  methods: `define` (local binding, plus `rcontext[name] = value` for each name of a global
  one), `yields_name` (`name in chain(dict)` for attribute filters), `attr_dict`
  (`visit_DictAttributes`, with Python sets for the excluded and boolean names, and the
  default marker treated as an ordinary object, because that `__quote` call passes none),
  `case_compare` and `is_false` (`tal:case` compares with `==` and
  tests the result with `is False`).
- **D3, new instructions:** `Define`/`Undefine` (backups taken before the expression, as
  `_enter_assignment` does; restored in reverse; global names never restored), `OmitTest` /
  `JumpIfOmitted` / `PopOmit` (the omit expression evaluated once, tags skipped), `CacheValue`
  / `PopValues` (a start tag's dictionaries, evaluated after its name is written and only if
  the tag is not omitted), `AttrStatic` (a filtered static attribute), `AttrValue` /
  `AttrLone` / `AttrBegin` / `AttrEnd` / `AttrDict` (`tal:attributes`, boolean attributes and
  filtered interpolated attributes), `Switch` / `PopSwitch` / `CaseTest` / `CaseDefault` (the
  case value is evaluated again only when `value == switch` is `False`, as Chameleon's
  generated code does). Unwind entries `Define`, `Omit`, `Value`, `Switch` and `Attr` joined
  `Loop`. Payloads that do not fit stay in side tables, so `Op` stays 24 bytes (a test checks
  it): a 32-byte `Op` would cost every instruction of the hot loop.
- **Errors Chameleon raises as bare exceptions** are reproduced: `AssertionError` (a
  `tal:repeat` with several clauses; a `tal:case` whose switch has not been cached, which
  happens for a case on its switch's own element and for a case in a `define-macro` whose
  switch is outside it, because macros compile first), `ValueError` from `int()`/`chr()` in
  entity decoding (Unicode digits and `_` accepted as Python accepts them; a reference to a
  surrogate is `UnsupportedError`, since a Rust string cannot hold one), `TypeError` for
  `trim_attribute_space` on a tag part that is `None`, and `KeyError` for an unknown prefix in
  a `data-` attribute. The core's `ErrorKind` gained `Assertion`, `Value` and `Type`.
- **Chameleon quirks reproduced, each verified by running it:** `tal:attributes` records a
  new name's index before inserting it, so a later name differing only in case replaces the
  attribute before it; an `extend-macro` element's slot list is never popped, so a later
  `fill-slot` elsewhere joins it (the tree keeps slot lists in an arena for this); a
  `define-macro` name registered again by a nested element keeps the nested body (the dict
  assignment happens before the children are visited); `i18n:target`'s lambda captures
  `clause` late, so with `i18n:name` on the same element the target expression is the name's
  value; validation checks all TAL attributes, then METAL, then i18n; `tal:omit-tag` on a
  TAL/METAL element is never evaluated. Found by the generated differential tests, not by
  reading.
- **`Options`** (core) carries `MacroProgram`'s keywords plus `strict` and `mode`; the
  `default_marker` stays a runtime value. `PageTemplate` now sniffs the content type as
  `BaseTemplate.write` does, which selects the HTML boolean attributes and newline
  normalization unless the template is XML (the Phase 2 skeleton rendered `checked="${x}"`
  with `x='yes'` as `checked="yes"`, where Chameleon writes `checked="checked"`), accepts
  `bytes`, and takes the options as keywords or class attributes; `PageTextTemplate` exists.
- **Lowered now** (Phases 6a/6b own the rest): `tal:define`, `tal:replace`, `tal:omit-tag`,
  every `tal:attributes` form except i18n, boolean attributes, `tal:switch`/`tal:case`,
  interpolation in CDATA, comments and processing instructions, `meta:interpolation`,
  `data-tal-*`, `trim_attribute_space` and text mode. Everything else (macros, slots, i18n,
  `on-error`, code blocks, global `tal:repeat`, implicit translation, non-strict deferred
  errors) compiles to a tree and then stops with `UnsupportedError`. Known approximation,
  pinned by a strict-xfail test and due in Phase 6a: a boolean attribute's interpolated result
  is tested for truth by whether it wrote anything, which differs from Chameleon only for an
  `__html__` chain ending in a false non-`str`.
- **Profiling before Phase 4 (the carried-forward item):** the gap to the spike was
  decomposed against the spike rebuilt from git and with macOS `sample` (no sudo needed for
  one's own processes). Escaping was the largest absolute cost, fixed with constant-length
  entity copies; the rest is dispatch plumbing spread over expressions and the loop. The
  larger Phase 4 instruction set costs about 10 ns per listing item; `execute` and `value_of`
  are `#[inline(always)]` because LLVM stopped inlining them. Numbers and method in plan.md,
  Phase 4, which also replaces the fixed per-phase listing figure with a same-process A/B
  against the previous phase's build (`bench/bench_ab.py`), because run-to-run noise exceeded
  the effects; the speed targets themselves are unchanged.

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
  `cargo-fuzz` on the tokenizer and parser for panics (skipped in Phase 3: it needs nightly;
  see the Phase 3 amendments).
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
