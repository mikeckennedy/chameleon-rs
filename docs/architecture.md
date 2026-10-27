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
  from `code`); the partial registry (Phase 9). *(Superseded in Phase 9: the registry is
  gone, the built-in and chameleon_partials' function both run natively, and the scope is
  chameleon_partials' whenever the partial can observe it; see the Phase 9 amendments.)*

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
  element stay visible after its parent closes. Each tag keeps its prefix map (`ns_map`) as
  an `Arc` of what it adds to its parent's: a tag that declares nothing shares its parent's
  map, and one that does stores only its own declarations, so maps take memory linear in the
  declarations. A copy per tag (Chameleon's dict copy) made `n` nested declarations take
  `n²/2` entries before the compile could reject the depth (a 2026-10-23 review: 2,000
  levels, 45 KB, 330 MB). While parsing, lookups go through a scope with a stack of values
  per prefix, kept level for level with the namespace stack, so a lookup costs the same at
  any depth; a proptest checks every tag's map against a copy per tag. Chameleon shares the
  dict, which differs only if an end tag carried `xmlns` attributes, which `iter_xml` never
  produces. A namespace URI remembers whether it
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

## Phase 5 amendments (2026-10-20)

The expression layer is real; these are the decisions it pinned down, and the places where
it changes earlier interfaces. Every behaviour below was checked by running the reference.

- **D1, `ExprContext.engine`.** Chameleon compiles each site with an `ExpressionEngine`
  configured by the node (`char_escape`, `default`, `default_marker`, `literal_false`), and
  expression types that convert their sub-expressions to text (`string:`, `load:`, a custom
  type's `assign_text`) use that engine's conversion. So `${string:${x}}` in text escapes `x`
  twice, `tal:attributes="title string:a${default}b"` puts the static attribute value in the
  middle, and in an interpolated boolean attribute a false sub-expression becomes `None`. The
  core now passes `Engine { escape, marker, default, literal_false }`: `escape` is the
  interpolation's `Escape` (none for CDATA and text mode, and for every non-interpolation
  site), `marker`/`default` are set for attribute values (`default` is the static value in
  `tal:attributes`, none for interpolated attributes), `literal_false` is false only in
  interpolated boolean attributes.
- **D1, new trait methods.** `deferred(message, token)`: in non-strict mode a syntax error
  compiles to an expression that raises `ExpressionError` with Chameleon's token, code-point
  offset and location when evaluated (a fresh exception each time); for an interpolation it
  is the only part, so nothing of the interpolation is evaluated first. `Resolved::Deferred`
  is gone and nothing stops at `UnsupportedError` for it any more. `free_vars(id)`: see D9.
- **D9, free variables, as built.** `FreeVars::{Dynamic, Known { reads, writes }}`, recorded
  per site by `ValueId` in the resolve `Table` (an interpolation's is the union of its
  parts'). `chameleon-py` computes them: fast-path trees by walking them (template builtins
  and `__translate`-style keys count as reads, the `__loader` builtin does not), fallback
  functions in the Python rewriter (every `getname`/`get` is a read, every rewritten store a
  write, `econtext`/`rcontext` used as values make it `Dynamic`), custom types are `Dynamic`,
  and `string:` with sub-expressions also reads `__decode`, `__translate` and
  `target_language` (its conversion calls them). The table is dropped after lowering; Phase 11
  copies what concurrent evaluation needs into the `Program`, and last-use analysis can walk
  the tree with the table. `chameleon_core::testing::free_variables` and
  `_native._testing.free_variables` expose them for tests.
- **TALES as built** (`chameleon-py`: `compiler.rs`, `lower.rs`, `strexpr.rs`, `custom.rs`).
  `ExpressionParser` uses the template's `expression_types` and `default_expression`, which
  `chameleon_rs.tales.native_types` classifies: Chameleon's own types (ours or, by qualified
  name, `chameleon.tales`'s; `partial(StringExpr, braces_required=...)`,
  `partial(ProxyExpr, name, ignore_prefix=...)`) compile natively, anything else is a custom
  type. Pipes split at unescaped `|` (`\|` is a literal), a prefixed part takes the rest
  (unless `ignore_prefix` is off, as for `load:`), an empty expression is
  `ExpressionError("No input:")`. Pipes fall through on `AttributeError`, `NameError`,
  `LookupError`, `TypeError`, `ValueError`; `exists:` catches the same minus `ValueError` and
  returns the int `1` or `0`; `not:` returns a bool; `structure:` wraps in
  `chameleon_rs.utils.Markup`; `import:` calls `resolve_dotted` (cached) at render time;
  `string:` is a port of `Interpolator` with braces optional and no entity decoding,
  including its retry quirk (a failed `${...}` candidate is searched again minus its last
  character, which can match a later `$name`: `string:${1 + $b}` with `b='B'` is `'B1 + B}'`);
  `load:` is `ProxyExpr('__loader', ignore_prefix=False)`: each alternative is such a string,
  passed to whatever the name resolves to.
- **Template builtins.** `template`, `macros` and `nothing` (plus `__loader` in file
  templates and the class's `extra_builtins`) are names known at compile time whose values the
  render passes in (never stored in the program or in fallback globals, D4). As in
  Chameleon's render function, where they are parameters, they shadow Python builtins of the
  same name and a render keyword overrides them (`get(name, builtin)`); a `__` one is the
  value itself. Phase 4 had none of them (`${nothing}` was a `NameError`).
- **The fallback, refined.** A `def` taking `(econtext, rcontext, default marker, template
  builtins)`. It binds `translate`, `decode` and `on_error_handler` as locals from
  `econtext['__...']` when it uses them, as Chameleon's render function does, so a
  comprehension may rebind them; its globals have `__builtins__` as the builtins *dict* and
  `__default` as `'__default__'`, as Chameleon's generated module does. When CPython rejects
  the rewritten tree (a walrus whose target became `econtext['y']`), the code is unparsed and
  compiled from source, which raises the same `SyntaxError` Chameleon raises at compile time
  (Phase 2 raised `TypeError`).
- **Constants are merged template-wide.** Chameleon compiles a template into one module, and
  CPython's compiler keeps one object per equal constant (`_PyCode_ConstantKey`), folds unary
  operations on constants: `${x is 1.5}` with `x` defined as `1.5` elsewhere in the template
  is `True`, and so is `${'ab' * 2 is 'abab'}`. A per-template constant cache
  (`_rewrite.constant`, tuple items included) is shared by the fast path and the fallback.
  For folding, the fast path does not reimplement CPython's rules and size limits: a unary or
  binary operation, subscript or tuple whose operands lowered to literals is compiled (never
  run) with the running interpreter, and if the code is a single constant load, that
  constant is used (`_rewrite.fold`). So `10 ** 10 ** 10` and `1 / 0`, which CPython leaves
  unfolded, stay runtime operations, and the result is exact for each Python version. Found
  by the expression fuzzer.
- **`eval` split by frequency.** With every node kind inline, `eval`'s stack frame reached
  912 bytes, paid by every recursive call (the listing measured 5-9 ns slower than Phase 4).
  `eval` now handles the census's first kinds (names, builtins, attributes, constants, calls
  with up to two positional arguments, `not`, subscripts, `[]`, the fallback) with a 288-byte
  frame; the rest go to an out-of-line `eval_more`, and the TALES types to a cold `eval_rare`.
  A test pins `Expr` at 56 bytes.
- **D4 revisit, re-measured with the real expression layer** (`bench/bench_d4.py`): a
  fallback-only build renders the listing in 783-845 ns per item and the partial listing in
  963-1,067 ns (both targets missed); the fast path saves 31-59 ns per expression (median
  33-38). D4 stands; its criterion is unchanged.
- **Fast path additions:** slices (`slice(lower, upper, step)`, as `BINARY_SLICE` builds it),
  template builtins, the folding above. Comprehensions, lambdas, f-strings, starred arguments,
  `**` in calls and dicts, walrus and `__` names other than template builtins still take the
  fallback: 15 of the corpus's 4,836 expressions (0.3%).
- **Custom expression types (D11), the bridge** (`chameleon_rs._bridge`). The type runs at
  compile time exactly as Chameleon calls it, with an engine whose
  `parse(s).assign_value` / `assign_text` / `assign_bool(target)` emit a call to a
  sub-expression the Rust compiler compiles natively (full TALES, the site's engine conversion,
  `char_escape` honoured). The statements get Chameleon's name rewriting (including its
  scoping: a `def` or an import binds for good, nested lambdas do not see outer parameters)
  and are compiled into a function at fallback speed. Chameleon's node types `Symbol`,
  `Builtin`, `Static`, `Comment` and `TokenRef` are recognized by class name. The code goes
  through source like Chameleon's codegen, so expression contexts in the type's AST do not
  matter. An `ExpressionError` from the type drives the Interpolator's retry. Types that
  subclass Chameleon's classes to override `translate` are still out of scope (D11).
- **`Scope`, two fixes found by the fuzzer:** iteration is lazy, like Chameleon's generator
  (`[q for q in econtext]` raises `RuntimeError: dictionary changed size during iteration`;
  ours iterated a snapshot), and `KeyError`/`NameError` carry the key as their one argument
  (`econtext[None]` raised `KeyError()`).
- **`PageTemplateFile`, minimal** (for `load:` and the corpus): compiled on first use under a
  lock, `auto_reload` by mtime, the template's directory prepended to `search_path`, package
  resources, and Chameleon's `TemplateLoader` ported verbatim (`chameleon_rs.loader`). Phase 7
  completes the loader surface (`PageTemplateLoader`, `default_extension` across classes,
  `CHAMELEON_*` settings).
- **Known deviations, by design:**
  - Messages that print a type *fully qualified* (`cannot use 'chameleon.utils.Scope' as a
    dict key`) name ours `chameleon_rs._native.Scope`. The usual short form matches: module init
    sets the native classes' `__name__` to `Scope` and `ImportableMarker` (what Chameleon's
    scope and default marker are called), which rewrites `tp_name` (`unsupported operand
    type(s) for +: 'Scope' and 'int'`) and leaves `__module__` / `__qualname__` alone; a test
    pins it.
  - A `SyntaxError` from a fallback names `<template expression>`, not Chameleon's generated
    module.
  - `yield` / `await` in an expression: Chameleon's whole render function becomes a generator
    and renders nothing; ours evaluates to a generator. Not worth reproducing.
  - `__` names other than template builtins, `__builtins__` and `__default` are
    `NameError`s: Chameleon's generated locals (`__stream`, `__append`, ...) are not exposed.
    The fallback function's own names (`__rs_*` parameters and globals, `__lookup_attr`) are
    rewritten to raise `NameError` when read, as they do in Chameleon (an assignment to one,
    which only a walrus can make, is not guarded).
- **`<?python ?>` code blocks stay `UnsupportedError`, moved to Phase 6a.** They are not
  expressions: they bind render-function locals, and Chameleon's visitor then treats every name
  a block defines (including a `def`'s parameters) as local in all expressions compiled after
  it. That needs per-render local state in the VM, which Phase 6a builds for `on-error`. One
  upstream fixture uses them, no corpus template does.

## Phase 6a amendments (2026-10-20)

The VM now runs every core feature; these are the decisions that made Chameleon's behaviour
exact, and the places where they change D2, D3 and D8. Every quirk below was found or checked
by running the reference.

- **D3 item 2, `tal:on-error` as built.** `Try { handler, name }` / `EndTry { end }` around the
  body, the fallback after it. A runtime error unwinds (`unwind_error` in `vm.rs`): the
  innermost `Try` entry of the *current frame* that catches it (`Runtime::is_catchable`:
  `Exception`, so `BaseException`s pass) wins; every unwind entry above it is **dropped
  without restoring** (`Define` and `Loop` backups too, so a failed body's defines and loop
  names leak, as Chameleon's try-without-finally does: the plan's test
  `<div tal:on-error="x"><p tal:define="x 2"><b tal:repeat="i [7]">${i}${1/0}</b></p></div>|${x}|${i}`
  renders `<div>2</div>|2|7`), then `Runtime::catch` binds `error` (an `ErrorInfo` with the
  current token's line and column; without a token, Chameleon's `__tokens[None]` `KeyError`)
  and calls the scope's `__on_error_handler`; an error from that is unwound further from the
  handler's position. Otherwise the frame records the error (below) and is popped, and its
  caller is tried.
- **One fallback mark per frame, not per `Try`.** Chameleon's generated code stores the stream
  length in a single `__fallback` local per render function, so once a nested `on-error` has
  started, an outer handler truncates only to the *inner* mark:
  `<p tal:on-error="string:A">a<b tal:on-error="string:x">B</b>C${1/0}</p>` renders
  `<p>a<p>A</p>`, and a failing inner fallback leaves the outer element's start tag in place.
  D3's `Try{out_mark}` became `Frame::fallback`. (Phase 6b: Chameleon's `len(__stream)` inside
  an `i18n:name` is the name's own stream; revisit when translation marks land.)
- **The fallback element's quirk.** Chameleon's fallback keeps only the static attributes,
  still filtered by dictionaries the fallback never evaluates, and its compiler then crashes:
  `AttributeError: 'NoneType' object has no attribute '_fields'` at compile time (not a render
  `TypeError`, as Phase 4 guessed). Reproduced in `resolve`.
- **D8, error tracking as built.** Chameleon assigns `__token` before each expression (only
  for sources that are `Token`s, and again for the sub-expressions `not:`, `structure:`, a
  prefixed pipe alternative and each `${}` of `string:` parse; not for `exists:`), and each
  render function's `except` appends `(token, line, column, filename, exc)` to
  `rcontext['__error__']`. Nothing is resolved on the hot path: the VM records the last two
  expressions each frame started (`ErrorSite`, one `u64` store per evaluation, saved in the
  frame when another becomes active) and passes them to `Runtime::catch` / `record_error`; the
  runtime then picks the token (an expression whose source has no position, such as a
  processing instruction's, sets none, so the one before counts). Nested tokens are kept in a
  `RenderCtx` cell tagged with their site, which only expressions containing them clear (a
  measured +8 ns per listing item when every evaluation stored its token, about 0 now). Token
  spans (byte range, line, column) are resolved at compile time; the text is Chameleon's
  re-slice of the source at the token's position for its length (`tal:define="a 1; b 1/0"`
  reports `" 1/"`). `chameleon_core::throw` resumes a suspended render by raising at the
  suspended instruction (Phase 11).
- **D8, the formatted `RenderError`.** When the render fails and `rcontext['__error__']` is
  non-empty, the native render calls `chameleon_rs.exc.render_error`, a port of
  `BaseTemplate.render`'s handler: a dynamic subclass of the error's class and `RenderError`
  (`create_formatted_exception`, ported) whose `__str__` is the ported `ExceptionFormatter`
  (expression, filename, location, the source line for files, the arguments through the
  template's `value_repr`), with the original as `__context__` and its traceback; an error that
  already carries a formatter gets these entries appended; `RecursionError` passes through
  unformatted. The messages are compared whole (addresses normalized) by the tests and the
  render fuzz.
- **Recursion.** `VmError::Recursion` is gone: the frame limit raises
  `Runtime::recursion_error()` (`RecursionError`) through the same unwinding, so an
  `on-error` can catch it, as Chameleon's `except Exception` catches Python's.
- **Global `tal:repeat`.** Chameleon assigns each item to the scope and `rcontext` (a chained
  assignment, so a tuple target unpacks the item twice), takes no backups, but still emits the
  restore code, which reads a backup variable that was never assigned: every global loop that
  finishes raises `NameError: name '__backup_<name>_<id>' is not defined` (`<id>` is
  `id(names)` in Chameleon, the interned name's address here). Reproduced through
  `Runtime::missing_backup`; `LoopInfo` carries the visibility and `assign` takes it.
- **`<?python ?>` code blocks.** Chameleon inlines the block's statements into the render
  function, rewritten like expressions (an assignment stores in the scope), and its one
  visitor per template keeps every name a block defines (a `def`, its parameters, an import)
  as a plain name in every expression compiled afterwards. Ours: `ExprCompiler::compile_code`
  compiles the block (`_rewrite.compile_code`) to a code object, and `Op::Exec` runs it with
  `exec` in a **per-render namespace** for its program (`Expr::Ns`, `RenderCtx::namespaces`)
  holding the render function's locals (`translate`, `decode`, `on_error_handler`) and what
  blocks define; once a template has such names, its later expressions skip the fast path and
  those that read one are evaluated with `eval` in that namespace. A `NameError` for a name the
  render function binds is turned into Chameleon's `UnboundLocalError`. Known differences: a
  namespace is per program and render, so two frames of the same program (a recursive partial)
  share it; `default`/`attrs` aliases are not applied inside a block.
- **Boolean attributes, the Phase 4 approximation fixed.** A boolean attribute's lone
  interpolation now goes through `Stage::Bool`: the runtime converts the value as Chameleon's
  second stage does and answers `Emitted::True` (write the name), writes a false result that is
  not `None` as text (`checked="0"` for an `__html__` chain ending in `0`), or nothing. Mixed
  interpolations are always strings, so their written-or-empty test stays.
- **`tal:content` without a default** (an `on-error` fallback): `Emit { stage: Content }`, so
  the default marker is an ordinary object there.
- **Unpacking messages:** `cannot unpack non-iterable int object`, as CPython's (Phase 2's
  carried item for the loop target; walrus targets remain Phase 8's).

## Phase 6b amendments (2026-10-21)

Macros, slots and translation run; these are the decisions that made them exact, and the
places where they change D2, D3, D8 and D11. Every behaviour below was checked by running the
reference (the differential tests and the fuzzer compare whole outputs and messages).

- **D2, the `Runtime` trait split** (the item carried from Phase 6a): `Values` (`Value`,
  `Error`, `clone_value`), `Eval` (`activate`, `eval`, `emit`, `is_default`, `truthy`,
  `is_false`), `Names` (`lookup`, `set_null`, `define`, `restore`, `text_value`), `Loops`,
  `Errors` (`is_catchable`, `catch`, `record_error` with defaults, `recursion_error`,
  `frame_budget`), `Attrs`, `Macros` (`macro_call`, `copy_scope`, `after_macro`, `fill_slot`,
  `pop_slot`, `slot_call`) and `Translate` (`i18n_changed`, `translate`, `quote_value`,
  `convert_value`); `Runtime` is all of them, with a blanket impl. `activate` takes an
  `Active { code, scope, serial, i18n }`.
- **D3, programs with several functions.** Chameleon compiles a template into a module with a
  render function per macro, the main one, and a closure per fill-slot body. The lowering
  does the same: the main function first (instruction 0, so `VmState::new` is unchanged), then
  each macro, each followed by the fill-slot bodies defined in it (lowered as functions of
  their own, with the slot table and open translations of where they were defined).
  `Program::macros()` lists the entry points with Chameleon's mangled names (`render_<name>`:
  every non-word character replaced; `macros['a-b']` replaces only `-`, so `a.b` is never
  found, as in Chameleon; of names that mangle alike, the last definition wins).
- **D3 item 4, macro frames as built.** `Op::UseMacro` evaluates the macro expression, then
  `Macros::macro_call` takes `__macro.include`: a native `Macro` (program, entry, and its
  template's builtins, so a macro of another template sees that template's `template`,
  `macros` and `__loader`) becomes a frame with a copy of the scope; anything else is called as
  Chameleon calls it (`include(stream, econtext.copy(), rcontext, domain, context,
  target_language)`) and its stream written. `macroname` is a local define around the call,
  fills are registered before the expression is evaluated, and the caller runs
  `econtext.update(rcontext)` when the frame returns (so a macro's globals come back, and
  overwrite the caller's locals of the same name, as in Chameleon). The element where a
  `metal:define-macro` stood is `Op::CallMacro`, which also resets the caller's `__token` (an
  error caught around it is then a `KeyError`). A template used as a macro is its main function
  (`template.include` is a `Macro`). `Macro` and `SlotFill` are callable with Chameleon's
  signatures, so Chameleon's templates can use our macros and slots and ours theirs (a
  Chameleon fill appends through its closure to the stream it was defined with: when we were
  called by Chameleon, what it appended there is moved into our output).
- **D3 item 5 and D11, slot stacks.** D11 planned native slot stacks with no `__slot_*` keys
  in the scope. They are in the scope after all: `econtext['__slot_<name>']` deques of native
  `SlotFill` objects, because their semantics depend on the scope copies (an `extend-macro`
  finds the stack with `getname` through whatever the copies share, appends to the left, and
  the macro pops the right) and user code can see them (`econtext` listings match Chameleon's).
  Each function pops its slots when it starts (`Op::PopSlots`: the define-slots of the function
  and of the fill bodies defined in it, mangled, any error giving `None`); a fill captures the
  i18n settings and those popped slots of its defining function, as the closure's default
  arguments and free variables do. A slot body's frame records no errors (its function has no
  handler), so an error in it is recorded by the macro's frame with the macro's token.
- **D3, per-frame state.** Each frame has its own error token pair, `__fallback` mark, code-block
  namespace (keyed by a frame serial passed through `activate`: the Phase 6a item; a recursive
  partial or macro no longer shares one), i18n settings, slots, and nesting depth. The less
  common state (i18n settings, slots, a slot body's stream) is a boxed `extra`, reused from a
  small pool: a native partial's frame stays as small as in Phase 6a, and a free on macOS's
  allocator costs more than the rest of a macro call. Partials push through their own
  `Action::Partial`.
- **D3 item 7, recursion.** The fixed `MAX_FRAMES = 500` became a budget: Chameleon's macros
  are Python calls (one interpreter frame per macro or slot level, two for a template used as a
  macro, four for a chameleon_partials render), so the budget is the recursion limit less the
  frames on the stack when the render started, asked once per render (`Errors::frame_budget`)
  when nesting first passes 64. Calls carry their cost. Measured equal to Chameleon at the
  default limit and with a raised one; `MAX_FRAMES` (100,000) remains a memory guard. The
  message is CPython's (`maximum recursion depth exceeded`); for this, `PageTemplate.__call__`
  is a method of its own again (Chameleon's adds a frame).
- **i18n settings are render-function locals** (`__i18n_domain`, `__i18n_context`,
  `target_language`): `i18n:domain`/`context`/`target` back them up and restore them as unwind
  entries (an error caught by `on-error` keeps the inner values, there being no `finally`),
  macros get the caller's, and a fill gets those where it was defined. `default` in
  `i18n:target` is `getname('target_language')` (Chameleon's `Alias`).
- **The translate protocol.** Each site calls `translate` with Chameleon's exact arguments, in
  order: `i18n:translate` passes `msgid, mapping=, default=, domain=, context=,
  target_language=` with the local target language; `tal:content` with `i18n:translate=""`
  (and an on-error fallback on such an element) passes `value, default=None, ...`; translated
  attributes pass `msgid or value, default=value, ...` and implicit interpolation `msgid,
  [mapping=,] ...` with `getname('target_language')`, because Chameleon's name rewriting
  reaches that code; conversion of a non-string (`__quote`/`__convert`) uses the local target
  language, the second conversion stage (inline `emit_convert`) the scope's. A test captures
  every call with both engines and compares them.
- **Translation streams are output marks** (D3 item 6 as designed): `TranslateBegin` and
  `NameBegin` push marks; `NameEnd` takes its text as the name's mapping value (names declared
  but not run map to `''`) and writes `${name}`; `TranslateEnd` takes the stream, normalizes it
  (`re.sub(r'\s+', ' ', ...).strip()` with Python's whitespace), and translates (no call when
  the message id is empty and none was given). Implicit translation follows the Interpolator:
  translated only when every expression is a simple name (`RE_NAME`, a trailing newline
  allowed), one constant node without a mapping, the message id with `${expression}` per
  expression; the values are the quoted parts. Translated attribute values are computed as
  values (`Op::TrValue`, `CaptureBegin/End` for mixed interpolations) and written with `%s`.
- **Chameleon's stream quirks reproduced:** a slot body writes with its defining function's
  `__append`, so a fill whose define-slot is inside a translation in the macro lands *before*
  the translation's result (moved there when the slot returns); a name an error left unfinished,
  caught inside the translation, maps to its stream, a list; an outer handler cuts the streams
  opened inside its body. **Not reproduced** (Phase 8 may revisit): when the one `__fallback`
  register was set inside a translation stream and is used by a handler outside it, Chameleon
  deletes from the outer stream by the inner stream's *item count*; we cut at the inner
  stream's start, both while it is open and after it closed (closing a translation, name or
  capture moves a fallback mark that points into it to its start, so a handler never cuts into
  the translated text: the architect's review found the stale mark, which could split a
  multi-byte character; pinned by `test_fallback_register_set_inside_a_closed_translation`).
  A stream's text that is not UTF-8 is an internal error, except with lone surrogates
  (`surrogatepass`), which become U+FFFD in the message id. Item granularity also shows in an unfinished name's list (one piece here),
  and in a translation function returning a non-`str` for an `i18n:translate` element
  (Chameleon fails at the final `''.join` with the item's index; we raise the `TypeError` at
  once, as Phase 6a already does for `tal:content`). A macro called from inside a slot body
  writes to the slot's stream here, to the define-slot site's stream in Chameleon (Phase 8:
  now as in Chameleon, see the Phase 8 amendments).
- **`yield`** (the Phase 5 deviation, now reproduced): an expression or code block whose
  code yields (outside nested functions; in a comprehension, only its first iterable counts)
  makes its function, main, macro or slot body, a generator in Chameleon, so the function
  renders nothing; the lowering replaces such a function's body with a return. `await` remains
  the `SyntaxError` both raise.

## Phase 7 amendments (2026-10-21)

The Python API and the compat layer. Every behaviour below was checked against the reference
(`tests/python/test_api.py` compares outputs, messages and objects with Chameleon's).

- **D7, the class layout as built.** `chameleon_rs.template` mirrors Chameleon's
  `template.py` plus `zpt/template.py`: `BaseTemplate` (`write`, `cook(body)`, `cook_check`,
  `render` calling `self._render(stream, econtext, rcontext, ...)`, `digest`, `keep_body`,
  `keep_source`, `debug`, `loader`, `value_repr`), `BaseTemplateFile` (`filename` property,
  `auto_reload`, `mtime` including zip resources, `read`, `post_init_hook`,
  `CHAMELEON_EAGER`), `PageTemplate`, `PageTemplateFile`, `PageTextTemplate(File)`, `Macros`.
  Subclasses that override `cook` or set `_render` on an instance work as in Chameleon (the
  upstream tests do both). `cook` compiles a native `Program` and does not set `_render`: a
  `PageTemplate` renders natively unless `'_render' in self.__dict__` after `cook_check()`
  (pyramid_chameleon's `macro=` renderer sets it in `cook`, so the check must follow
  compiling: the review found the first render of such a file template rendering the whole
  page), in which case it takes Chameleon's stream protocol (`BaseTemplate.render`, with its
  error formatting). A compilation is stored as one `(program, builtins)` attribute, so a
  render or macro lookup never pairs one compilation's program with another's builtins. The render
  defaults (`__translate`, `__decode`, `__on_error_handler`) are cached and recomputed when
  `translate`, `encoding` or `on_error_handler` changes (Chameleon reads them each render).
- **Thread-safe cooking (the Phase 5 item).** `BaseTemplateFile.cook_check` compiles under a
  per-template `RLock`, double-checked, so one thread compiles while others wait; with
  `auto_reload`, a changed mtime is re-read under the lock, so a thread that read the time
  before another reloaded the file cannot set `_v_last_read` back and recompile (Chameleon has
  no lock; its unlocked check could also cook twice). A render never sees a half-updated
  template: it reads the compilation once. The lock is per template, so a `cook` override
  that compiles another template while a second thread's override compiles this one (say
  each looks up the other's macros) can deadlock; Chameleon's unlocked cooking cannot. No
  known code does this; such overrides should not touch other templates. The loader's cache
  stores with `setdefault`, so threads loading the same spec at once share one template.
- **Configuration.** Every keyword of Chameleon's is accepted and honoured: the program
  options (Phase 4), `translate`, `encoding`, `on_error_handler`, `value_repr`,
  `extra_builtins`, `expression_types`/`default_expression`, `debug`, `keep_source`,
  `keep_body`, the loader keywords, and two that needed native support:
  - `default_marker`: an AST node as in Chameleon (`Symbol(obj)`, `Builtin`, `Constant`;
    anything else is used as the object). A `Symbol`'s object must be importable, as
    Chameleon's generated module imports it: `TypeError` without a `__name__`, `ImportError`
    if its module lacks it (the imported object is used). The `Program` holds its marker (`default_marker=`),
    compiles `default` to it and renders with it. A render uses the marker of the template it
    starts from; a macro of another template with a different custom marker sees the
    caller's (Chameleon: its own). Only custom markers are affected.
  - `tokenizer`: Chameleon passes its tokens to the parser. The core's parser takes byte spans
    of one source (`chameleon_core::compile_tokens`), so the template compiles the tokens
    joined, with their lengths (`token_lengths=`); for tokenizers that return slices of the
    body (all of Chameleon's) that is the body itself. Positions in errors are then into the
    joined tokens (Chameleon keeps each token's own position and source).
  - `debug` / `CHAMELEON_DEBUG` give the template a `ModuleLoader` in a temporary directory and
    keep the body and `source`; `source` is a dump of the program (D11), not Python code.
    `CHAMELEON_CACHE` is validated and sets the loader as in Chameleon, but nothing is written
    to it (D11). `CHAMELEON_EAGER` and `CHAMELEON_RELOAD` behave as in Chameleon; unknown
    `CHAMELEON_*` variables are logged.
- **D8, compile errors as Chameleon's.** `TemplateError(msg, token)` is Chameleon's class
  (vendored): `token` is a `chameleon_rs.tokenize.Token` (code-point `pos`, `source`,
  `filename`), and `offset`, `location`, `filename` are properties of it. The native compiler
  builds the token from the core's position (a plain `str` for positionless tokens) and
  `cook` sets its filename, as Chameleon's `_cook` does, so `str(error)` is Chameleon's whole
  message including the `- Source:` lines (the Phase 6a item), for strings, files and
  non-strict render-time `ExpressionError`s. `msg` is no longer an attribute (Chameleon has
  none; it is `args[0]`).
- **Lone surrogates in the template source (the Phase 3 item): refused, for now** (superseded:
  rendered since Phase 8, see the Phase 8 amendments). Chameleon
  renders them in text, attribute values and `string:` expressions (a Python expression with
  one fails there too, `UnicodeEncodeError` from `compile`); the core's source is UTF-8 text
  and cannot hold them. Compiling raises `UnsupportedError` (a `CompilationError`) with a
  token at the first surrogate, instead of a bare `UnicodeEncodeError`. A template read from a
  file can never contain them (decoding is strict); values with lone surrogates still render
  (`surrogatepass`, D6). The planned fix (the review's): map each surrogate one-to-one onto a
  2,048-code-point block of the BMP Private Use Area that the source does not use (both are 3
  UTF-8 bytes, so offsets are unchanged), and map back wherever source text leaves the core:
  static text (rewritten to `surrogatepass` bytes in the program's text table), every place
  the VM turns static text into a `str` (`text_str`: defines, translation message ids,
  names, defaults, attribute names), Python expression and `string:` sources, error tokens.
  That touches the core's text API and some twenty call sites in the VM and the runtime, so it
  was deferred (plan.md, Phase 8).
- **Render-function preamble.** A macro or template called through Chameleon's `include`
  signature from outside a native render raises `KeyError` when the scope lacks
  `__translate`, `__decode` or `__on_error_handler`, as Chameleon's render functions read
  them first.
- **D7, the compat package, decided per module.** `chameleon_rs.compat.install()` puts
  `chameleon_rs` in `sys.modules` as `chameleon` and each of its modules as `chameleon.<same
  name>` (`MODULES`), replacing stock Chameleon's entries if any; it never imports stock
  Chameleon, and the package imports only the standard library (a test blocks every
  third-party import; another installs the alias with stock Chameleon blocked):
  - **Backed by the Rust implementation:** `tokenize.iter_xml` (the native tokenizer's
    ranges, `_native.tokenize_ranges`), `parser.ElementParser` (the native parser over the
    stream's tokens, `_native.parse_elements`; the items' strings are slices of the original
    tokens, so a stream from several sources or of plain strings gives what Chameleon gives),
    `utils.Scope` and `utils.lookup_attr` (native), `tales.DEFAULT_MARKER` (the native
    marker), `compiler.ExpressionEvaluator` (each expression compiles to a small native
    program, evaluated in the caller's `Scope` itself, so its writes stay visible; errors are
    raised unformatted, `econtext['_result']` is set; `rcontext` is not used; a plain dict,
    which Chameleon rejects, is copied into a new scope), and the template classes and
    loaders.
  - **Vendored verbatim** (Chameleon's license header noting the change of import names):
    `astutil`, `codegen` (custom expression types build their AST with them), `namespaces`,
    `metal`, `types`; and inside ported modules, `tales.TalesExpr` (with `resolve_global`,
    `transform_attribute`; z3c.pt subclasses it, and a subclass's piped alternatives run
    through the AST bridge, tested against Chameleon) and `tal`'s `split_parts`,
    `parse_attributes`, `parse_substitution`, `parse_defines`, `prepare_attributes`
    (differential-tested).
  - **Ported** (Chameleon's code, adapted): `tokenize.Token`, `exc`, `config`, `loader`
    (`TemplateLoader`, `MemoryLoader`, `ModuleLoader`: the latter two only as objects code
    may inspect), `zpt.loader.TemplateLoader` (= `PageTemplateLoader`), `i18n`
    (`fast_translate` when zope.i18n is installed, `parse_attributes`), the helpers of
    `parser`, `tal` and `utils`.
  - **Not provided:** code-generation internals with no counterpart (`chameleon.nodes`,
    `chameleon.program`, `chameleon.zpt.program`, `chameleon.benchmark`):
    `ModuleNotFoundError`, as is any other `chameleon.X` that is not aliased: a meta-path
    finder stops the import system from searching chameleon-rs's directory under the
    `chameleon` name, which would load a second copy of a module (`chameleon.compat`) or of
    the native extension (`chameleon._native`, with different `Scope` and `Program` types).
    Every public module of chameleon-rs is aliased (found with `pkgutil`), as the same object;
    private ones are not. Stock Chameleon modules imported before `install()` are replaced with
    a `RuntimeWarning` naming them. `compiler.ExpressionEngine` holds its configuration but
    `parse` raises `NotImplementedError` (custom types get the bridge's engine, D11).
- **The parser keeps namespace token positions.** Chameleon's `ns_map` keys are slices of the
  `xmlns:` attribute names (the first declaration's, for a prefix redeclared later) and its
  `ns_attrs` keys hold the declaring attribute's value token. `chameleon_core`'s `Namespaces`
  and `NsAttr` now record those spans, so the compat `ElementParser`'s items equal
  Chameleon's token for token (the Phase 3 differential tests compared these keys as plain
  strings; they now compare positions too).
- **Type names (the carried item) stay ours.** A render error's message prints classes the way
  Python does: `chameleon_rs.tal.ErrorInfo`'s default `repr` (`${error}` in an `on-error`
  handler) and the few messages that print the scope's qualified name name chameleon-rs's
  modules (`chameleon_rs._native.Scope`), where Chameleon prints `chameleon.tal.ErrorInfo` /
  `chameleon.utils.Scope`. With the compat alias these are the same classes under
  `chameleon.*`, so `isinstance` and imports work; the text differs only in the module name
  (and the object address, which differs anyway).
- **Upstream harness.** With `CHAMELEON_ENGINE=rust` the suite runs on `compat.install()`. As
  the harness already maps `chameleon.tests.test_templates` to the vendored module, it maps
  resources of the package `chameleon` to a directory whose `tests` is the vendored suite
  (fixtures 081, 082 and 089 load `chameleon:tests/inputs/hello_world.pt`, a file stock
  Chameleon ships). Result: all 227 pass; `expected_failures_rust.txt` is empty. Both test
  harnesses now clear `CHAMELEON_EAGER`, `_DEBUG`, `_CACHE` and `_RELOAD` before importing
  either engine: the six "environmental" upstream failures on the maintainer's Mac were
  `CHAMELEON_EAGER=1` in the shell, and they now pass on both engines (still non-strict
  xfails, as their outcome depends on the environment).

## Phase 8 amendments (2026-10-21)

Real-site verification. Every behaviour below was checked against the reference.

- **Lone surrogates in the template source: rendered, as in Chameleon** (the Phase 3 and Phase 7
  item; `tests/python/test_surrogates.py`, 68 cases, all compared with Chameleon). As built:
  - `chameleon_rs.template` maps each lone surrogate one to one onto a window of 2,048 code
    points of the BMP Private Use Area (U+E000-U+F8FF) that neither the source's characters nor
    its numeric character references use (any window, not three fixed blocks, so a source with
    one or two private-use characters always has one), and passes the window's start as
    `Program(surrogate_base=...)`. Both take 3 UTF-8 bytes, so byte and code-point offsets are
    unchanged. A source that also has a numeric reference that may name a surrogate gets a
    window too. With no free window, compiling raises `UnsupportedError` as before.
  - The core (`chameleon_core::surrogates`): `Program::set_surrogate_base` rewrites the static
    text arena, in place, to the surrogates' `surrogatepass` bytes (WTF-8), which is what the
    VM writes; `text_str` reads a UTF-8 copy kept only for such programs (the mapped text, for
    names, message ids and defaults). The VM sets the output's surrogate flag when a frame of
    such a program becomes active (so the final `str`, and a translation stream, are decoded
    with `surrogatepass`). Text taken back from the output (a translation's content and
    default, an `i18n:name`'s value, a capture) stays WTF-8 (`surrogates::Wtf8Buf`, normalized
    as such) and the runtime decodes it with `surrogatepass` (`Msgid::Stream`, `NameValue`,
    `Names::stream_value`), so values' lone surrogates and private-use characters, and static
    text of other programs, reach the translation function unchanged (this replaced a first
    version that round-tripped stream text through the window, and the earlier U+FFFD; the
    architect's review found both wrong). A character reference to a surrogate
    (`string:a&#xD800;`) decodes into the window (`Options::surrogate_base`); the Python side
    reads numeric references as Chameleon does (`\d`/`\w` and `int()`, Unicode digits and
    underscores included) both to detect them and to keep the window clear of their code points.
  - The runtime converts every piece of *static* source text it hands to Python through
    `convert::source_str`, which maps the window back: `text_value` (`macroname`), message ids
    from `i18n:translate` and `i18n:attributes`, mapping keys, `i18n:domain`/`context`, slot keys,
    attribute filters and dictionary exclusions, `tal:attributes` defaults. The compiler does
    the same for `string:` text parts (joined by Python, so the surrogates survive), custom
    expression sources, code blocks, `import:` names, and error tokens (compile errors,
    deferred `ExpressionError`s, `rcontext['__error__']`).
  - Python expressions: a source with a lone surrogate is parsed from the restored string, so
    `ast.parse` raises Chameleon's exact `UnicodeEncodeError` ("'utf-8' codec can't encode
    character '\ud800' in position N: surrogates not allowed", N within the stripped
    expression), at compile time, unwrapped; the structural pre-check is skipped for it.
  - Cost: none for templates without lone surrogates (one flag test per frame activation;
    `text_str` tests an `Option`). The A/B is in plan.md.
- **Slot bodies and macro streams: the Phase 6b deviation is gone.** Chameleon's fill
  function writes with its defining function's `__append` but passes the define-slot site's
  `__stream` to the macros and slots it calls, and its `tal:on-error` truncates that
  `__stream`. The VM now moves a slot body's own output to its stream not only when the body
  returns but also just before it calls a macro or a slot (`flush_fill`, unless the body has a
  translation, name or capture stream of its own open, which the callee then writes to), and
  restarts the body's output when that call returns or fails (`Extra::flushed`: only after a
  flush that happened; the architect's review found an unconditional restart orphaning output
  when no flush had happened). A handler in the body that cuts the output also clamps the
  body's start. The callee's output stays in the define-slot site's stream, so a slot defined
  inside a slot body and filled by the caller orders as in Chameleon too.
  Marks inside the moved output move to the end of the output (they belong to the define-slot
  site's stream), so a handler in the body keeps what the body already appended, as in
  Chameleon (`<q><q>E</q>`). Pinned by the former strict xfail and 17 variations in
  `test_macros.py` (two calls, repeated calls, fills inside the called macro, `i18n:name`, no
  translation, `template.macros`, handlers on and inside the body, nested macros, slots
  defined in slot bodies, the body's own translation or capture around the call); the render
  fuzzer now generates macros whose slot sits inside a translation and fill bodies that
  define slots or call macros. Nothing on the hot path
  changed: the work happens at macro calls made from a slot body.
- **Slot bodies: two known differences remain** (strict xfails). A handler in a slot body
  whose output belongs to another stream than the define-slot site's cuts only that site's
  stream (as Chameleon; the body's own output is kept, what streams opened in the handler's
  body held is dropped: `fill_catch`), but Chameleon appends a text node and the expressions in
  it as one string, so when such an expression fails the text before it was never appended;
  chameleon-rs wrote it first (`test_handler_in_a_slot_body_drops_the_failing_text_node`, fixed
  2026-10-23: see the Phase 12 amendments). And Chameleon's
  conversion helpers (`__convert`, which calls `translate` for a value that is not a string,
  a number or `__html__`) are closures of the render function, so in a slot body they use the
  defining function's i18n domain and context, not ones set inside the body; chameleon-rs uses
  the body's (`test_conversion_in_a_slot_body_ignores_its_i18n_context`, present at Phase 7
  too, found by the extended fuzzer; the fuzzer keeps `i18n:domain`/`context` out of slot
  bodies). Phase 12 fixed the second (see the Phase 12 amendments).
- **Shadow mode** (`chameleon_rs.shadow`, opt-in): `install(serve='chameleon' | 'rust',
  sample_rate=1.0, report=None)` wraps stock Chameleon's `PageTemplate.render` (and
  `PageTextTemplateFile.render`), so it needs stock Chameleon importable as `chameleon` (not the
  compat alias). An outermost render of a sampled stock template is repeated with a
  chameleon-rs twin (same file or source, same settings; built once per template, weakly
  keyed), and the served engine's outcome is returned or raised. Inside the twin's render,
  stock templates rendered by `render_partial` are rendered by their own twins, and stock
  templates passed as render arguments are replaced by their twins, so the twin's output is
  chameleon-rs's throughout. A difference in output, or in the exception (type and message,
  object addresses and the package name aside), goes to `report` as a `ShadowDifference`
  (whose fields hold both full outputs: personal data). The default report logs a warning per
  distinct (file, diff) at most once per `log_interval` (600 s) with a count of suppressed
  repeats, the diff's lines cut at 200 characters and the diff at 4 KB, and each output's length
  and SHA-256. Nothing from the shadow reaches the request: the other engine's failure, a Rust
  panic (`PanicException` is a `BaseException`) or an exception whose `str()` fails included,
  and a failing report is logged; `KeyboardInterrupt` and `SystemExit` propagate. Zero cost
  when off (nothing is patched until `install`); `uninstall()` restores the originals. Limits,
  documented in the module: each sample doubles the render's latency in the request's thread
  (under one-thread asyncio servers such as Granian ST the loop is blocked for both), so
  `sample_rate` should stay well below 1; arguments are used twice (an iterator is exhausted by
  the first render, callables run twice); string templates cooked before `install` are not
  shadowed, and those cooked after have their source kept while they live.
- **Real-site harness** (`scripts/realsite.py`): crawls an application through a private
  *adapter* (outside this repository: app setup, routing table, parameter values, an
  in-process GET client), one engine per process, with `PYTHONHASHSEED=0`, the clock frozen
  (freezegun), `random` and `os.urandom` (hence `secrets`, `uuid4`, CSRF tokens) seeded per URL,
  and every outbound connection refused; then compares pages byte for byte and writes minimal
  diffs. `--timing N` re-renders each request's outermost template N times in place
  (median), `--shadow` crawls stock Chameleon with shadow mode on. It refuses to write pages or
  reports inside this repository.

## Phase 10 amendments (2026-10-21)

Performance pass. Each fast path below reproduces what the Python it replaces does, and falls
back to that Python whenever its preconditions do not hold. Measurements: plan.md, Phase 10.

- **Scope sharing (D5).** Compiling marks the functions (macros, slot bodies) that only read
  their scope: no `tal:define`, `tal:repeat`, `tal:on-error`, code block, macro call or slot
  fill, and every expression's free variables known with no writes
  (`Program::mark_scope_sharing`, `shares_scope(entry)`). A call to such a function passes
  the caller's scope itself instead of `econtext.copy()`. Equivalence: the copy holds the
  caller's local items and shares its root, so every lookup gives the same value, and neither
  side is written while the callee runs, as long as no user code holds a scope object: an
  alias (`tal:define="ctx econtext"`, then `ctx.__setitem__(...)` in the body, a method call
  that is not an `econtext` expression) would write the shared scope where Chameleon writes
  the caller's and reads the copy. So the render context records when a scope reaches user
  code (`RenderCtx::scope_exposed`: `econtext` as a value, a code block's namespace, a custom
  expression type, a fallback whose free variables are unknown, a render with a given scope
  (`render_scope`), a macro or slot body called from outside through `include`), and from then
  on every call copies (`Macros::may_share_scope`; checked at `UseMacro`, `CallMacro` and
  `DefineSlot`). The architect's review found the alias case; tests in `test_macros.py` (the
  alias across templates and within one, through a code block, in a slot body, and a foreign
  `include`).
- **`PageTemplate.render` natively** (`_native.render_page`, D7): the same steps in the same
  order (defaults, `cook_check()`, the `_render` protocol in Python via `_render_protocol`,
  then the scope from the keywords, the missing defaults and a fresh `RepeatDict`). Defaults
  for an `encoding` or `translate` argument are cached (`_encoded_defaults`), so successive
  renders share the closures (unobservable within a render).
- **`repeat` (D2).** An exact `chameleon_rs.tal.RepeatDict` is called natively
  (`list(iterable)`, `RepeatItem(iter, len)`, stored with `self[key] = ...`). `lookup_attr`
  computes `repeat.name` (for names not starting with `_`, present keys) and the
  `RepeatItem` descriptors `index`, `number`, `start`, `end`, `odd`, `even`, `parity` for the
  exact classes with Python arithmetic and the same `callableint`/`callablestr` results;
  subclasses, other names, missing keys and iterators without `__length_hint__` run the Python
  code (`crate::repeat`). The classes stay Python (identity, subclassing, pickling unchanged).
- **Conversions (D6).** `structure:`'s exact `Markup` is written from its own text (its
  `__html__` is `str(self)`); with the default translation function (`simple_translate`,
  checked by identity, so an `encoding` wrapper or a user function takes the call), a
  non-string value converts by reading its `default` and `mapping` attributes natively, once
  each, a true `mapping` being interpolated by `chameleon_rs.i18n._interpolate` with the values
  read (the regex substitution itself); exact `callableint`/`callablestr` skip the attribute reads (their classes have
  none). Escaping finds gate and replaced characters eight bytes at a time (SWAR in safe
  Rust; a property test checks it against the byte rule); the output buffer gets an eighth
  of headroom over the last size.
- **Method calls (D4).** `obj.name()` / `obj.name(c1, c2)` with constant arguments use
  `PyObject_VectorcallMethod` (no bound method) only when `obj` is an exact `str`, `dict`,
  `list`, `tuple`, `int`, `float`, `bytes`, `set` or `frozenset` (no `__getattr__`, no
  properties, reads without effects). Constants cannot fail or have effects, so looking the
  method up after them changes nothing; an `AttributeError` when `obj` has no attribute
  `name` takes `lookup_attr`'s `__getitem__` fallback, otherwise it came from the call. Every
  other receiver is looked up once with `lookup_attr` and called, exactly as before (the
  first version used the method call for all receivers, which ran `__getattr__` three times
  where Chameleon runs it once; found in review).
- **Caches compare by identity.** The cached render defaults (`_defaults_key` in
  `render_page`, `_encoded_defaults` for an `encoding`/`translate` argument) are reused only
  when the translation function, encoding and settings are the same objects: two translators
  that compare equal (one per language) must each be used (found in review; the Phase 7 key
  compared with `!=`).
- **Notes.** The `Markup` fast path does not look at an instance attribute `__html__` (only
  reachable by assigning one on a `structure:` result inside the render). `render_page`
  requires `_render_defaults` to return a `dict`, as ours does. Two Python-expression
  differences predate Phase 10 and stay: `eval('econtext')` and `'econtext' in locals()` in a
  fallback expression (Chameleon's render function has `econtext` as a local).
- **Startup.** `chameleon_rs.__version__` is resolved on first access (module
  `__getattr__`); `importlib.metadata` is imported only when used.
- **PGO** (D12): `make build-pgo` (instrumented build, `bench/pgo_train.py`, `llvm-profdata`
  from rustup's `llvm-tools`, optimised build); about 10% faster on every benchmark; a CI
  job builds and tests it; the build fails if LLVM reports the profile out of date (a stale
  profile silently loses the gain), and its `RUSTFLAGS` replace any configured rustflags. Release wheels (Phase 12) should be built this way. No
  `chameleon-capi` crate was needed: no measured gap required `unsafe`.

## Phase 9 amendments (2026-10-21)

Built-in partials and the Flask/Quart integration. The reference for partials is
chameleon_partials at `f54ca1a` on Chameleon (its `render_partial` calls
`loader[path].render(encoding='utf-8', **kwargs)` with `render_partial` added to `kwargs`);
`tests/python/test_partials.py` compares outputs and whole error messages with it, and
`tests/python/test_flask.py` responses and exceptions with chameleon-flask at `326644f`.

- **D10 as built: a partial is a render of its own, run as a frame.** `${render_partial(path,
  k=v)}` as a whole emitted expression (`Expr::Partial`, the Phase 2 lowering) checks what
  `render_partial` is (the scope, then the template builtins, as the name lookup would): the
  built-in (`chameleon_rs.partials.PartialRenderer`, exact type) or chameleon_partials' own
  function (a function named `render_partial` from module `chameleon_partials`, read through
  its module state: registered flag and `__templates` loader) run natively; anything else is
  called as before, and an unbound name is the `NameError` it always was. Evaluation order is
  Python's: the name, the path, the keyword values, then resolution (so a failing keyword
  value wins over a missing file). A native call pushes a frame (`Step::Call`, as the
  prototype) whose *unit* is the partial's render: its template's builtins (`template`,
  `macros`, `__loader`, its own `render_partial`), the render defaults of
  `encoding='utf-8'` (`_encoded_defaults`, so `__decode` decodes bytes as chameleon_partials
  does), and a fresh `rcontext`, all used by every frame the partial calls (macros, slot
  bodies) until it returns. The core marks such frames `Kind::Partial` and tells the runtime
  (`Eval::partial_entered`, `partial_returned`, `Errors::partial_failed`); the runtime keeps a
  stack of units (`chameleon-py/src/partials.rs`), and an activation switches unit when the
  innermost unit's serial changed (one integer compare otherwise). Consequences, each as in
  chameleon_partials: a global define in a partial stays in its `rcontext` (it does not reach
  the page through a later `econtext.update(rcontext)`); errors recorded in a partial are its
  own, and an error that ends the partial is formatted there (`BaseTemplate.render`'s
  handler: the partial's errors, its `econtext` and `rcontext`, its template's `value_repr`),
  so the page's handler then extends that formatter with its own frames (a `RecursionError`
  passes unformatted); a `tal:on-error` around the call catches the formatted error.
- **The partial's scope.** chameleon_partials' scope is the keywords, `render_partial`, the
  four defaults and a fresh `RepeatDict`. Building all of it costs about as much as the rest
  of a small partial, so a program that cannot observe it gets only the keywords: a program
  is `full_scope` if an expression mentions `repeat`, `econtext`, `rcontext`,
  `target_language` or a `__` default, has unknown free variables (fallbacks that use the
  scope as a value, code blocks, custom types), loops, or calls a macro (whose template may).
  Otherwise the defaults are read from the unit (`scope_or_default`), and `render_partial`
  from the builtins, where it is the same object chameleon_partials would put in the scope
  (the partial template's own renderer). An error message's argument list is rebuilt in
  chameleon_partials' order (keywords, `render_partial`, defaults, `repeat`, then defines).
  A keyword named like a default or `repeat` wins, as `setdefault` does there.
- **Path resolution** (`chameleon_rs.partials.resolve`, Python, cached per renderer template
  and path until the configuration changes): the registered partials folder (its loader, so
  `spec` handling and the `Template not found: x.` `ValueError` are Chameleon's), then the
  calling template's search path (the `search_path` it was created with, so a
  `PageTemplateLoader`'s folders), then its own folder and each folder above it, up to the
  outermost root holding it: a configured folder (partials' or any integration's slot) or a
  search path entry; in none, the nearest `templates` folder whose parent has an
  `__init__.py` (Pyramid's renderers), else its own folder only. **Security review fix
  (2026-10-22):** this walk once went up to `/`, so a sanitised name (`etc/hosts`) or a file
  planted in any parent folder resolved and compiled; every candidate is now also lexically
  confined to its root (no `..` or absolute path out of it);
  package-resource entries are not searched, and a file found there becomes a template of the
  caller's class with its settings (shared between callers when the settings are hashable).
  **A deliberate difference from chameleon_partials, approved by Michael (2026-10-21):** the
  caller-folder search only runs where chameleon_partials raises (no folder registered, or the
  path not in it), so it never changes a page that rendered under the original; it turns that
  error into a render. `configure(partial_search='registered')` restores chameleon_partials'
  lookup exactly (the registered folder only; unregistered is its `PartialsException`);
  `'caller'` is the default. A renderer resolves relative to *its own* template, whether called or run as
  a frame (review fix 3): a template's builtin is bound to that template, the global
  `chameleon_rs.render_partial` (passed in a model by `extend_model`) to none, as calling it
  would. Called from Python, the renderer renders through `template.render(encoding='utf-8',
  ...)` and returns `HTML` (`html_text`, `__html__`, chameleon_partials' class, no `repr` of its
  own).
- **`translate=` and `encoding=` keywords** (review fix 2) change chameleon_partials' render
  itself (its translation function; a second `encoding` is its `TypeError`), so a call with
  either is lowered to the ordinary Python call, not a native frame.
- **Per render, once:** a call site's resolution (keyed by the site, the function object and
  the path, identity first, then string equality for computed paths) and a template's
  preparation (`_prepare`: the defaults, `cook_check()`, the compilation) are made on first
  use in a render and reused for the rest of it, so `auto_reload` checks a partial's file once
  per render, not per call. Templates that cannot run as frames (another engine's, a text file
  template, which returns bytes, a `_render` function, an overridden `render`) are called as
  Python, with the arguments already evaluated.
- **Settings** (`chameleon_rs.configure(template_folder=..., auto_reload=...,
  restricted_namespace=..., partial_search=...)`, `_settings.py`): immutable `Settings` objects
  (folder, options, one `PageTemplateLoader`) in one slot per consumer, as the original
  libraries each had their own state (review fix 1, the blocker: a shared slot made
  `global_init` a no-op after `register_extensions` and dropped its `restricted_namespace`):
  `partials` (what `render_partial` resolves against) and `flask` (the views'). `configure()`
  and `chameleon_rs.flask.configure()` set both; the aliases only their own, each with
  `cache_init` checked against its own slot: `chameleon_partials.register_extensions(folder,
  auto_reload, cache_init)` (its loader has chameleon_partials' options:
  `restricted_namespace=False` given to Flask does not reach partials; `has_registered_extensions`
  and `template_path` stay module state that code may reset) and `chameleon_flask.global_init(...)`
  (`render` without it is chameleon-flask's "You must call global_init()" error, whatever
  partials have). Slots are swapped under a lock with a generation counter that keys the
  resolution caches; settings left out keep their values.
- **`render_partial` is a template builtin** (`PageTemplate._builtins`, overridable by a render
  keyword or `extra_builtins`), so `template.builtins` lists it. In chameleon_partials' setup
  the page's `econtext` holds it (the model has it); with the unbound built-in it does not,
  which only `econtext` listings and a page error's argument list show.
- **Flask/Quart** (`chameleon_rs.flask`, chameleon-flask's code adapted): imports Flask only
  when imported; `configure(..., app=app)` stores a `Settings` in `app.extensions
  ['chameleon_rs']`, used while that app (Flask's `current_app`, or Quart's when Quart is
  imported) handles a request; `template` keeps chameleon-flask's name lookup (decided when the
  view is decorated, `templates` when nothing is configured) and response pass-through;
  `not_found(template_file=..., **data)` (old keyword and positional kept), `error(template_file,
  status_code=500, **data)` and fastapi-chameleon's `generic_error`; error pages render as
  `text/html` with the page's status (chameleon-flask's 404 rule). Models: `dict`, any
  `Mapping`, a dataclass instance (its fields, shallow) or an object with a callable
  `model_dump()` (Pydantic, not imported). Exceptions: `chameleon_rs.web.TemplateError` (base),
  `ErrorPage` and `NotFound`; `FlaskChameleonException`, `FlaskChameleonNotFoundException`,
  `FlaskChameleonGenericException` and `PartialsException` are subclasses. Async views render
  synchronously until Phase 11's `render_async`.
- **compat** (D7): `install()` also maps `chameleon_partials` (at once) and `chameleon_flask`,
  `chameleon_flask.engine`, `chameleon_flask.exceptions` (on first import, through a
  meta-path finder, so Flask is imported only then) to the built-ins, evicting modules already
  imported under those names with the same warning; `install(integrations=False)` maps only
  `chameleon` (the original packages then run on chameleon-rs, and their `render_partial`
  passed in a model still renders natively). `flask`, `partials` and `web` are chameleon-rs's
  own modules, not aliased under `chameleon.`. The `partials=` prototype registry is removed.
- **Markers and names.** A partial frame uses its own template's `default_marker`
  (`Cx::marker`, from the unit, as builtins and defaults are). `PartialRenderer`'s type is named
  `function` (as `_native` renames `Scope`), so a render error's argument list prints it as
  chameleon_partials' function. The one known difference: with the unbound builtin, the page's
  `econtext` (and an error's argument list) lacks `render_partial`, which chameleon_partials'
  setup puts in the model.
- **Phase 11 note:** the unit stack lives in the `RenderCtx` as `Bound` references: fine for a
  synchronous render (and for releasing the GIL around one), but a render that suspends needs
  the units in the `VmState` as `Py<...>`.

## Phase 11 amendments (2026-10-21)

`render_async`, opt-in concurrency, and the render-offload experiment. The reference is
still Chameleon: with no awaitable value an async render must give exactly what `render`
gives, which `tests/python/test_async.py` checks against Chameleon for every upstream fixture
and the three render fuzzers; a generated-template fuzzer puts `aw(v)` (a coroutine) at every
value position and compares with Chameleon's render where `aw(v)` is `v`.

- **D9 as built: `RenderAwaitable`.** `PageTemplate.render_async` is an `async def` awaiting
  `_native.render_page_async(...)` (the same setup as `render_page`: defaults, `cook_check`,
  scope; a template with its own `_render` renders synchronously). The awaitable is a
  non-frozen pyclass: PyO3's borrow flag (atomic on free-threaded builds) makes a second call
  while one runs `ValueError('coroutine already executing')`, so no `Mutex` is needed. It
  implements `__await__` (itself), `__next__`, `send`, `throw` (one-argument and legacy
  three-argument forms), `close`, `__traverse__` and `__clear__`. Each step binds the saved
  state, runs the VM (`run`, or `throw` to raise at the suspended instruction), and saves it.
- **What is awaited.** `PyRuntime::eval` of an async render (`PyRuntime<true>`, a separate
  instantiation) checks the expression's final value: not for exact `str`, `int`, `bool`,
  `float`, `list`, `tuple`, `dict`, `bytes` or `None`; yes when a class of `type(value).__mro__`
  defines `__await__` in its own namespace (what fills the slot `await` uses: not an instance
  attribute, not a metaclass's; answers cached per type for the render) or the value is a
  generator flagged `CO_ITERABLE_COROUTINE` (`@types.coroutine`). Every VM site that evaluates
  an expression already handled `Step::Suspend` (Phase 2 design): content, attributes,
  conditions, defines, repeat sources, switch/case, omit-tag, macro names, i18n names and
  attributes, on-error handlers. Values computed inside an expression are not awaited
  (`coro().title` is the coroutine's attribute), nor is a native partial's keyword argument
  until the partial writes it. A value is awaited once: an awaitable result is a value.
- **Delegation (PEP 380).** `await_iter`: `type(v).__await__(v)` (a non-iterator result is
  `TypeError("__await__() returned non-iterator of type ...")`), or the iterable coroutine
  itself. `send(None)` calls the delegate's `__next__`, other values its `send`; `throw`
  delegates to the delegate's `throw` (absent: raised in the render), and `GeneratorExit`
  closes the delegate and is raised in the render; `StopIteration.value` resumes the VM
  (`VmState::resume_with`), any other exception is raised at the suspended instruction
  (`chameleon_core::throw`), so `tal:on-error` catches an awaited value's `Exception` and
  cancellation (`CancelledError`, a `BaseException`) passes through. `send` of a non-`None`
  value to a fresh render is a `TypeError`; a finished render raises `RuntimeError('cannot
  reuse already awaited coroutine')`; `close()` closes the delegate and drops the state; a
  render dropped while suspended drops its references (the delegate's own finalizer closes
  it).
- **State across suspension (the Phase 2 and Phase 9 carried items).** The VM state was
  already `Py`-only. The render-wide state (`RenderCtx`: `rcontext`, the partial unit stack and
  pending unit, the render's partial resolutions and prepared targets, code-block namespaces,
  the nested error token, the scope-exposed flag) holds `Bound` references on the sync path;
  `RenderCtx::save` / `restore` convert it to and from `SavedRender` (`Py`, shared targets
  converted once each) at each suspension, instead of moving the unit stack into `VmState`:
  the sync path keeps its `Bound` fast path and pays nothing, and a suspension pays a few
  reference moves. `__traverse__` visits the VM state (`VmState::for_each_value`), the saved
  render state, the program, scope and delegate, and in the Fill phase the tasks' entries;
  `__clear__` drops them. Each reference is visited once: a prepared partial target is shared
  (`Arc`) by units, call sites and the render's list but holds one reference to each object,
  so `SavedPartials::traverse` dedupes by address (review fix: visiting it once per holder
  over-counted, which can make the collector clear live objects). A test checks, at every
  suspension of a render inside nested partials and a foreign macro and in the Fill phase,
  that no referent is visited more often than it is referenced. PyO3 skips the
  traversal of an object borrowed mutably (a running render), which only delays collection.
- **Recursion budget.** An async render's frames are not on the interpreter's stack, but its
  caller awaits it: while it runs, the stack is the event loop's, then the coroutines awaiting
  it, then `render_async` and `_render_async`. `frame_budget` walks `sys._getframe` there as in
  a sync render, with one frame less reserve for `_render_async`, so a macro recursing until
  `tal:on-error` catches the `RecursionError` renders as many levels as `render` called in
  place of `await render_async(...)`. (It was once the recursion limit less a fixed 64, which
  rendered fewer levels than Chameleon; the async tests now render Chameleon's reference at
  the same depth.)
- **Errors.** As in a sync render, an error that ends the render is formatted with the
  render's `rcontext['__error__']`; in an async render a non-`Exception` (cancellation) passes
  through unformatted, both at the top and out of a native partial (`partial_failed`): the
  formatted class would subclass `RenderError`, an `Exception`, which `tal:on-error` catches.
- **Async iterables in `tal:repeat`: not supported (decided).** An async iterable is not an
  iterable, so the loop raises the `TypeError` a sync render raises; a helper that collects it
  (`[x async for x in source]`) returns an awaitable, which the loop awaits. Supporting
  `async for` would need a suspending `Loops::next` (the VM requires `next` not to suspend, so
  a loop's side effects stay atomic) and would make `repeat.x.length` unknown.
- **Opt-in concurrency (D9's placeholders).** Two triggers, both opt-in because helper code then
  runs while the render goes on:
  - `@chameleon_rs.concurrent` marks an async helper (its calls return
    `chameleon_rs._async.ConcurrentCall`, exact type checked). In `Op::Emit` (a `${...}` in text,
    or a part of an attribute value), a value the render would suspend on is offered to
    `Eval::defer_value`, which starts it as a task (`loop.create_task`) and returns a
    placeholder; the runtime keeps the frame context (scope, builtins, marker, i18n domain,
    context and target language) and the error token.
  - `PageTemplate(..., concurrent=True)` (per view: `@template(..., concurrent=True)`): a
    native partial of the render's own unit called with an awaitable keyword argument is
    offered to `Eval::defer_partial` (after a cheap `wants_defer`), which starts a separate
    `RenderAwaitable` of the partial (its own unit: builtins, `encoding='utf-8'` defaults, fresh
    `rcontext`) as a task.
  The VM offers only where `VmState::can_defer()`: no `Try`, `Translate`, `Name`, `Capture` or
  boolean `Attr` entry anywhere on the unwind stack, so nothing written after can truncate or
  capture the placeholder (slot bodies move text, placeholders with it). Not deferred:
  `tal:content`/`tal:replace` (the value may select the default content), whole attribute
  values, values inside a partial (fan-out is one level). A placeholder is U+10FFFD, 40 hex
  digits (the render's nonce and the entry id) and U+10FFFD again. The nonce is 128 bits from two
  `RandomState` (OS-seeded `SipHash`) hashers over a counter, so output text cannot be made to
  equal a placeholder (a predictable nonce let a value equal to one be swapped with deferred
  content in another context: review fix); filling fails with an internal error if a
  placeholder occurs twice or is missing. When the VM finishes, the
  tasks are awaited in document order (they ran meanwhile), each value converted
  (`convert::emit` in its saved frame context) or partial output taken as is, and the output
  filled in one pass (`Output::fill_placeholders`). An error ends the render (a value's error
  recorded at its expression and formatted; a partial's is its own render's formatted error):
  the other tasks are cancelled and awaited (`asyncio.gather(..., return_exceptions=True)`)
  before the error is raised, as a `TaskGroup` does, so their `finally` blocks have run; the
  same when the render is cancelled. A closed or dropped render cancels them (nothing can be
  awaited then).
  Error messages do not list the enclosing macros' frames (the error surfaces after they
  returned). "Start as soon as the free variables exist" (hoisting a call ahead of its
  position) is deferred: the call starts where the render reaches it.
- **Subclasses.** `render_async` honours a subclass's own `render` (it calls it; with
  `offload`, in the worker), synchronously, so default behaviour matches what `render` does for
  that class. The protocol follows coroutines: `send(non-None)` on a fresh render is a
  `TypeError` that leaves it usable; `throw(instance, value)` is a `TypeError`.
- **chameleon_rs.flask.** Async views render with `render_async` (decided as the default:
  with no awaitable value the output is `render`'s, shown by running every chameleon-flask
  case as an async view against chameleon-flask's own async handling); `render_async()` and
  `response_async()`; `@template(..., concurrent=True)` (`None`, the default, keeps the
  template's setting, `False` turns it off). A deliberate difference from chameleon-flask (also
  through `compat.install()`): an awaitable model value (a Future, an `AsyncMock()`) is awaited
  where the template uses it instead of rendered as an object.
- **The sync path pays nothing.** `PyRuntime<const ASYNC: bool>`: an async render is a separate
  instantiation of the VM, so the synchronous one has no awaitable check (a runtime flag cost
  the listing about 9 ns per item in the A/B); the VM's deferral paths (`defer_emitted`,
  `defer_partial`) are out of line, behind a `wants_defer` that is `false` at compile time in
  the sync instantiation. An async render without awaitable values costs about 4 ns per
  expression (the check; answers per type are cached for the render) and about 270 ns per
  render (the awaitable, saving the state, `StopIteration`).
- **Offload experiment (Michael: wait and see): kept, opt-in, experimental.**
  `render_async(..., offload=True)` (keyword-only, so not a template variable there;
  `@template(..., offload=True)` in Flask) runs the synchronous `render` in the loop's default
  executor. And `convert::write_str` escapes or copies a `str` of 64 KiB or more detached from
  the interpreter (`Python::detach`, out of line): the only pure-Rust stretch long enough to be
  worth a GIL release (the engine's other native work is sub-microsecond and interleaved with
  CPython calls; building the final `str` needs the interpreter). Measured (plan.md, Phase 11
  results): on 3.14t, +50% throughput and event-loop lag p99 from ~10 ms to ~0.15 ms with
  report-heavy traffic; on the GIL build no gain for CPython-heavy pages (-2 to -4%, worse
  short-request p99) but +43% and lag p99 1.8 ms to 0.2 ms for pages dominated by long text.
  So it stays opt-in: worth it on free-threaded builds, and on GIL builds only for text-heavy
  pages (transcripts). Not done: gzip in Rust (the frameworks compress; nothing measured
  justifies it).

## Phase 11b amendments (2026-10-21)

The `markdown:` expression type (plan.md, Phase 11b). It is opt-in and has no Chameleon
counterpart, so the reference is the CommonMark and GFM specs, and the rule that nothing
changes while it isn't configured.

- **Where the code lives.** `chameleon-core/src/markdown.rs`, behind the `markdown` cargo
  feature (on in `chameleon-py`, so in the wheel): `Settings` (the options) and `Profile`
  (settings plus the comrak `Options` built from them, once). comrak 0.57, default features
  off (no syntect, no clap). `chameleon-py/src/markdown.rs`: the frozen pyclass
  `MarkdownProfile` (name, id, `Profile`), the conversion a render runs, and the cache.
- **Compiled in, not looked up.** `configure(markdown=...)` validates the settings and builds
  one `MarkdownProfile` per prefix (`_settings.markdown_profiles`, replaced whole).
  `tales.native_types` appends `('markdown', profile)` for each prefix the template class
  doesn't define itself, so an application's own `markdown` type wins. The compiler turns
  it into `Expr::Markdown(profile, inner)`, `inner` parsed as `structure:`'s is
  (`typed_ref`: an ordinary expression, pipes included). A render never reads settings; a
  template keeps the profiles in effect when it compiled.
- **Evaluation.** `None` and `''` give empty `Markup`; the `default` marker passes through
  (the element keeps its content); other non-strings go through `str()`; lone surrogates
  become U+FFFD (one each, through UTF-16); the result is `MarkdownMarkup` (a `Markup`), emitted unescaped in content like
  `structure:`, and escaped like a string in an attribute value (its own quoted `href` would
  close the attribute). In an async render an awaitable value is awaited and then converted, as for
  `structure:` (D9: the expression's final value becomes a coroutine doing both, which the
  render awaits; wrapping the awaitable itself once wrote its `repr` and never ran it). A panic in comrak is caught
  (`catch_unwind`) and raised as `RuntimeError` naming the profile: an `Exception`
  frameworks handle, and not one a TALES `|` falls back on.
- **Options through comrak's formatter hook, not by rewriting HTML.** `images: false`
  renders an image's children in plain mode (its alt text); `inline` skips the tags of a
  lone top-level paragraph; `classes` arms a pending ` class="..."` when a node starts, and a
  wrapping writer inserts it before the `>` (or ` />`) of that element's opening tag. It fails
  closed: the next non-whitespace output must be the element's tag start (`<p`, matched across
  comrak's split writes such as `<h` then `2`) followed by a tag boundary, or the class is
  dropped; and whatever is still pending when the node's formatter call returns is dropped
  (an image keeps it until its exit, where ` />` is written). So a class never lands on raw
  HTML, a closing tag or another element (the first version did: a paragraph in a tight list
  writes no `<p>`, and its class went to `</li>`; found in the architect's review).
  `tests/markdown_spec.rs` checks it with a unique class on every element over every spec
  example, four option combinations. `heading_ids` sets `header_id_prefix` and `header_id_prefix_in_href` (the anchor
  link points at the prefixed id).
- **A table cell budget.** comrak gives every row of a table as many cells as its header, and
  its own limit (`MAX_AUTOCOMPLETED_CELLS`, 500,000) is per table: four sparse 700-column
  tables in 22 KB made two million cells, about 500 MB (a 2026-10-23 review). Before parsing,
  `table_cells` bounds the cells comrak could create, per run of lines between blank ones
  (a table ends at a blank line): the most `-` runs at the end of any line there (a
  delimiter row has one per cell, after whatever container markers start its line) times
  the run's lines. Past 100,000 cells, or one per byte of text if that is more, the text
  converts with tables off. Proptests check the bound is never below comrak's count. A limit
  on the output would come too late: the cells are allocated while parsing.
- **The cache.** A process-wide map behind a `std::sync::Mutex` (locked with
  `lock_py_attached`, recovered if poisoned: no update stops halfway), key (profile id,
  `FxHash` of the UTF-8), entry (text, `Markup`, size, last use). A hit compares the text, so a
  collision only costs a miss; it returns the cached `Markup` object. Python's own `hash()`
  was tried first: it is cached per object, but costs about 1 µs for 5 KB on a string seen
  for the first time, which per-request Markdown is. Limits: `markdown_cache` entries (1,024
  by default) and 64 MiB of text plus HTML; over either it evicts by last use down to 7/8 of
  both. Texts over 64 KiB aren't cached. Objects leaving the cache are dropped after the lock
  is released. Conversions of 4 KB or more detach from the interpreter.
  `configure(markdown=...)` and `markdown_cache=` empty it; the native size is set before the
  Python settings, so a failure changes nothing.
- **Licenses.** `finl_unicode` (through comrak) is Unicode-DFS-2016, added to `deny.toml`
  (Unicode-3.0's predecessor, already allowed in its newer form). The spec test data (the
  CommonMark spec is CC BY-SA 4.0) is excluded from the sdist; the wheel never had it.
- **Not done.** A per-profile length limit: hostile input can be slow (a table with
  thousands of columns takes seconds), so the docs say to limit length on input and to turn
  tables off for comment profiles.

## Phase 12 amendments (2026-10-21)

Release blockers. Each behaviour checked against Chameleon's generated code first.

- **A slot body's output before its own stream moves before a call.** A slot body writes with
  its defining function's `__append`, so in Chameleon what it wrote before opening a
  translation, name or capture of its own is in the defining function's stream at once; a
  macro called inside that stream writes into it. `flush_fill` used to do nothing when the
  body had such a stream open, so the body's earlier output stayed in place until the body
  returned, and anything else that wrote to the defining function's stream during the call
  (another fill of the same function, taken by the call: the extend-macro case the Phase 10
  fuzzer found) landed before it. Now `flush_fill` moves the output between the body's start
  and its first open stream (`move_to_stream(stream, start, end)`), and the body's own output
  starts at that stream; `flushed` stays unset (the call writes into the body's stream, so
  nothing restarts when it returns).
- **Moved slot body output is kept aside, not inserted** (after the architect's review). Output
  a slot body moves to its stream belongs before the first translation or name opened since;
  inserting it there moved everything written into that stream since, on every flush, so a
  body looping over macro calls inside such a slot was quadratic (16,000 rows: 375 ms against
  Chameleon's 29 ms; it was so since Phase 6b). Now `move_to_stream` cuts the body's output
  out (`Output::cut`, marks in the cut range collapse to its start, later ones move back) and
  appends it to that stream's entry (`OpenTranslation::before`, `Unwind::Name::before`);
  `TranslateEnd`/`NameEnd` write it at the stream's mark, before the stream's result. When an
  error abandons such a stream (`unwind_error`, before a handler runs or a frame is left), the kept text is
  first inserted at its mark with every later mark shifted (`place_before_from`), which is
  exactly where the old in-place move had put it, so the handler's truncation and
  `unfinished_names` see what they saw before. The same page now renders in 5 ms, linear
  (`test_slot_body_output_moved_before_a_translation_scales_linearly` compares it with the
  page without the translation).
- **Conversions in a slot body use the render function's i18n settings** (the Phase 8 known
  difference `test_conversion_in_a_slot_body_ignores_its_i18n_context` is gone). Chameleon's
  `__convert` and `__quote` are closures of the render function (main or macro), so a value
  they translate gets that function's `__i18n_domain`, `__i18n_context` and
  `target_language`, while the body's inline conversion and its `translate` calls use the
  body's own locals (its `i18n:domain`, `i18n:context`, `i18n:target`). A body starts with its
  render function's settings as they were where the fill was defined, and that function is
  suspended in the call that runs the body, so the helpers' settings only need keeping when
  they can differ: `Extra::helpers` is `None` (use the frame's own) until the body changes its
  own (`VmState::keep_helpers`, before `I18nSet`/`Target` in a slot body), or comes from the
  fill (`Fill::helpers`) for a fill defined in a slot body, which belongs to the same render
  function as that body, or from `VmState::with_helpers` for a fill called from another
  engine (whose frame is not a `Kind::Fill`, so a fill defined in it takes the frame's helpers
  whenever it has some: found in review). The runtime gets them through `Translate::i18n_changed`'s new `helpers` argument,
  also on activation, but only once a frame of the render has had some (`helpers_any`, so
  other renders pay one test per activation); it keeps them (`FrameCx::helpers`) only when
  they differ from the frame's own, and `convert::translate` uses them for the helper
  conversions (`Target::Local`); deferred values and custom expressions carry them too.
  **Not modelled:** Chameleon reads the function's settings through closure cells, so a fill
  no macro took, left in the scope's slot stack and taken later by any macro call (directly,
  or through another macro that calls the one with the slot) while the function's settings
  differ (domain, context or `i18n:target`; the body need not set anything), sees the settings
  at that later call; chameleon-rs uses those where it was defined
  (`test_conversion_in_a_left_over_slot_body_uses_its_functions_settings_at_the_call`, strict
  xfails for each of those variants). A first version tracked the function's frame (an id per render and frame)
  to read its live settings; it cost 15 to 40 ns per slot call in the macro A/B, for a case
  that needs a fill no macro consumed.
- **Fixed (2026-10-23):** `test_handler_in_a_slot_body_drops_the_failing_text_node`. In a
  slot body whose output goes to another stream than the define-slot site's (the slot sits in
  a translation or name), a handler in the body keeps what the body wrote before the failing
  expression, as Chameleon does, but Chameleon writes in larger pieces: a text node and its
  expressions, an interpolated attribute, a comment are each one `__append`, so the failing
  piece's text before the expression was never written (`<q>` against our `<q>Q`; ` title="a`
  likewise). Slot bodies are now lowered with `Op::PartsBegin`/`Op::PartsEnd` around each text
  node or plain attribute value of several parts: `PartsBegin` records the output mark (with
  the frame's serial), and `fill_catch` cuts back to it before keeping the body's output, so
  the failing piece is dropped as in Chameleon. Only slot bodies get the two instructions (the
  only frames whose handler keeps their output); other code is unchanged. A lone `${x}` needs
  no mark (nothing of it is written before it fails), and comments match already (`<!--` is
  its own append in Chameleon too). The render fuzzer now puts handlers in such slot bodies.
- **Known difference of the same family** (pre-existing, found in review, a strict xfail:
  `test_i18n.test_handler_on_a_name_maps_it_to_its_appends`): a `tal:on-error` on an
  `i18n:name` element maps the name, in Chameleon, to its stream, the list of its appends
  (`['<q>r', '<z>', 's', '</z>']`), where chameleon-rs has one piece (`['<q>r<z>s</z>']`); in
  a macro that fails partway inside it, Chameleon never appended the failing text node
  (`['<q>r', '<p>']`) where chameleon-rs keeps its text (`<q>r<p>x`).
- **Compile errors found after the visit** (a separate fix, 2026-10-22). An `on-error` fallback
  whose static attribute keeps a `tal:attributes` filter makes Chameleon's compiler use `None`
  as an AST node; that fails (`AttributeError: 'NoneType' object has no attribute '_fields'`)
  only when the generated module is processed, after every expression compiled, so any
  expression or language error of the template wins. The resolver keeps that error
  (`Resolver::late_error`) and raises it when the pass ends without another.

## Integrations amendments (2026-10-22): the shared views core and FastAPI

- **Shared core** (`chameleon_rs._views`, private, standard library only). An `Integration`
  object per framework module holds everything a `@template` view does that does not depend on
  the framework: the template name of a bare `@template` (the original libraries' rule, against
  the framework module's `template_path` global, which it sets to `templates` when unset), the
  sync wrapper (`render`) and the async wrapper (`render_async`, with the view's `concurrent` /
  `offload`), the error pages (any `chameleon_rs.web.ErrorPage`, rendered as `text/html` with
  the page's status and data), response pass-through and model conversion (`web.model_of`,
  with the library's "expected a dict or X as the return value" message), `global_init` /
  `clear` on its settings slot, and the decorator-order check. The framework subclass supplies
  `is_response`, `make_response(html, content_type, status_code)`, `settings()` (Flask: the
  app's from `current_app`, else the slot), `routes(app)` and `watch(app, check)`, plus its
  exception class and the names used in messages. Public functions stay in each framework
  module with the original library's signature (Flask's `template(template_file,
  content_type, status_code)`, FastAPI's `template(template_file, mimetype)`), so typing
  overloads and positional calls are each library's. `chameleon_rs.flask` moved onto it with no
  change of behaviour (the chameleon-flask differential tests, its suite on the built-in, and
  every case as an async view are unchanged).
- **Settings slots are generic.** `_settings` has `partials` and `views`, a dict keyed by
  `VIEW_SLOTS = ('flask', 'fastapi')`, replaced whole on change. `chameleon_rs.configure()`
  sets every slot; `chameleon_rs.flask.configure()` / `chameleon_rs.fastapi.configure()` set
  the partials and their own slot; each library's `global_init` (with its `cache_init` checked
  against its own slot), `clear()` and chameleon_partials' `register_extensions` set only their
  own, as the original packages each had their own module state. Integrations register a
  listener (`_settings.listeners`) that keeps their legacy `template_path` global in step.
- **`chameleon_rs.fastapi`**: fastapi-chameleon's API (`global_init`, `template`, `render`,
  `response(template_file, mimetype, status_code, **data)`, `not_found`, `generic_error`,
  `clear`, the module aliases `engine` / `exceptions`, `FastAPIChameleonException`,
  `FastAPIChameleonNotFoundException`, `FastAPIChameleonGenericException` as subclasses of the
  `web` exceptions), plus `configure`, `error`, `render_async`, `response_async`,
  `status_code=` / `content_type=` / `concurrent=` / `offload=` on `@template`, `check_views`.
  The wrapper is `functools.wraps`'d, so FastAPI's `inspect.signature` follows `__wrapped__`
  (and `get_typed_signature` resolves annotations in the view's own module through
  `inspect.unwrap`): path and query parameters, `Depends`, validation (422) and the OpenAPI
  schema are the view's (a test compares the schema with fastapi-chameleon's and with the
  undecorated view's). An `async def` view gets an `async def` wrapper that renders with
  `render_async` on the loop; a sync view gets a sync wrapper, which FastAPI runs (render
  included) in its thread pool, as before. Responses: any Starlette response passes through;
  pages are `fastapi.Response(content, media_type, status_code)`, as fastapi-chameleon's. No
  per-app settings: a FastAPI view has no app context to look settings up in without a
  middleware or an injected `Request` parameter, so `configure(app=...)` adds the startup
  check and applies settings process-wide (open for the owner). `compat.install()` maps
  `fastapi_chameleon` (with `engine` and `exceptions`) to it, lazily like `chameleon_flask`.
- **Decorator order** (owner's requirement: fail loudly at startup). `@template` written
  above the route decorator means the router registered the bare view, so the wrapper never
  runs and the route answers with the view's dict as JSON (FastAPI and Flask both do that
  quietly). `@template` records
  every function it wraps (with its `__wrapped__` chain) and its wrappers, in weak sets.
  `check_views(app)` lists the app's routes (FastAPI/Starlette: `app.routes`, recursing into
  `Mount`/`Host` and into lazily included routers, FastAPI 0.13x+ `_IncludedRouter.original_router`
  with its prefix; Flask/Quart: `app.view_functions` with the URL rules, so blueprints are
  included) and flags a route whose view (or a function in its `__wrapped__` chain, above any
  `@template` wrapper) is a function `@template` wrapped, unless some route of the app calls a
  wrapper of that function (a view registered raw on purpose and also decorated on another
  route). The error, `web.DecoratorOrderError` (a `TemplateError`), names each view, its file
  and line, its route and the fix. `configure(app=...)` and `global_init(..., app=...)` hook
  the check into the app once (`_views._watched`):
  - **FastAPI/Starlette:** `app.router.lifespan_context` is wrapped, so the check runs inside
    the lifespan startup (Uvicorn/Granian report a failed startup and exit; a `TestClient`
    entered with `with` raises it); and `app.build_middleware_stack` is wrapped so the built
    stack sits behind a `_FirstRequestCheck` that runs the check on the first HTTP/WebSocket
    request when no lifespan ran (a server with lifespan off, a test client not entered,
    a mounted sub-app, whose lifespan Starlette never runs) and then puts the real stack back.
    Raising from `build_middleware_stack` itself was rejected: it runs on the lifespan scope
    too, and Uvicorn's `lifespan="auto"` treats an exception there as "lifespan unsupported"
    and serves anyway. `_FirstRequestCheck.app` is the real stack, so FastAPI's
    OpenTelemetry detection walks through it.
  - **Flask:** there is no start event, so the earliest reliable hook is the first request:
    `app.wsgi_app` is replaced by a one-shot guard that runs the check and, once it passed,
    puts the original back (if nothing wrapped it since), so later requests pay nothing; a
    misordered app fails every request with the error.
  - **Quart:** `before_serving` (the app fails to start) plus the same one-shot guard on
    `asgi_app` for HTTP/WebSocket scopes (Quart's test client does not serve).
  Review fixes: the check is memoized per app (a middleware wrapped around `wsgi_app` after
  `configure(app=...)`, such as ProxyFix, keeps the Flask guard in place, which then costs one
  flag test per request instead of a scan); views are compared by `_views._key` (a bound
  method by its function and instance, since `obj.method` is a new equal object each time,
  anything else by identity); `check_views(app, ignore=[...])` and `configure(app=...,
  check_order=False)` (and `global_init`'s) are the escape hatch for a function routed raw whose
  `@template` wrapper is used elsewhere (called from another view, routed on another app),
  which looks exactly like the mistake at runtime; `app=` must be an application (`TypeError`
  for an `APIRouter` or a blueprint); `global_init` validates the folder before hooking the
  app; listeners are keyed by module name and each integration syncs `template_path` when
  imported (it was stale when imported after `chameleon_rs.configure()`, e.g. lazily through
  `compat.install()`). Callables that cannot be weakly referenced are remembered by identity.
  Detecting at decoration time was considered and left out: when `@template` wraps a function
  some route already has, that is either the mistake or a view also registered raw on purpose
  (JSON on one route, HTML on another), and the two can only be told apart once all routes
  exist. Apps not given to `configure(app=...)` are not checked (`check_views(app)` can be
  called directly, e.g. in a test).
- **No extras** (owner's decision): no `[project.optional-dependencies]`. Each framework module
  imports its framework at the top of that submodule only; `import chameleon_rs`,
  `compat.install()` and every other module never import Flask, Quart, FastAPI, Starlette or
  Pydantic (`test_api.test_no_runtime_dependencies` blocks third-party imports and checks).
- **Typing.** `chameleon_rs.fastapi.template` has flask's overload shape: the decorated view
  keeps its parameters (`ParamSpec`) and returns `fastapi.Response` (a coroutine of one for an
  `async def` view); `tests/python/test_fastapi.py` asserts the types with `assert_type`, which
  ty checks.

## Integrations amendments: Starlette (2026-10-22)

- **`chameleon_rs.starlette`**, two layers. `Templates` is Starlette's `Jinja2Templates` for
  chameleon-rs: `TemplateResponse(request, name, context, status_code, headers, media_type,
  background)` with Starlette 1.x's behaviour (request set on the caller's dict, context
  processors, `url_for`, the `http.response.debug` event), its loader from `directory` (one
  folder or several) or, without one, the process-wide `starlette` settings slot read at each
  render. `TemplateResponse_async` (same arguments, `concurrent` / `offload` keywords) renders
  with `render_async`; the sync method stays sync for parity (default behaviour matches
  Starlette; async is opt-in by name). `url_for` is passed as a template variable but kept out
  of `response.context`, as Jinja's global is. `response.template` is the page template, with Jinja's
  `.name` (see the review fixes). The decorator layer is the Flask/FastAPI module API on
  `_views`, with Flask's `template()` signature.
- **Starlette pieces shared with FastAPI.** `StarletteIntegration` (in `chameleon_rs.starlette`)
  is the Starlette part of `_views.Integration`: `is_response` (any Starlette response),
  `make_response` (`HTMLResponse`), `routes` (the walk over `Route`, `WebSocketRoute`, `Mount`,
  `Host`, `Router`, sub-apps, FastAPI's lazily included routers, and `HTTPEndpoint` classes
  expanded into their handler methods), `check_app`, and `watch` (the lifespan wrapper plus the
  `_FirstRequestCheck` middleware-stack guard). `chameleon_rs.fastapi._FastAPI` subclasses it
  with fastapi-chameleon's names, `fastapi.Response` and the FastAPI wording of the `TypeError`.
  The FastAPI module therefore imports `chameleon_rs.starlette` (FastAPI depends on Starlette);
  the Starlette module never imports FastAPI.
- **`view_context(args)`** on `_views.Integration`: template variables the framework adds to a
  view's model from the view's arguments, the model's own keys winning (`{**extra, **model}`).
  Starlette returns `request` and `url_for` when an argument is a `Request` (function endpoints
  get it first, `HTTPEndpoint` methods second); Flask and FastAPI return `None`, so their
  models are unchanged.
- **Review fixes.** `Templates.get_template` refuses names that reach outside the folder
  (absolute, `..`, backslash, drive letter, `package:` spec) with
  `chameleon_rs.web.TemplateNotFound`, as Jinja's `split_template_path` does; the decorator
  paths, error pages and partials deliberately keep the original libraries' lookup (owner's
  rule: default behaviour matches them; documented as "names come from your code").
  `TemplateNotFound(TemplateError, ValueError, LookupError)` is raised by `Templates` only, for
  a missing template (checked against its folders before loading); the loaders and the other
  integrations keep Chameleon's plain `ValueError`. Error pages get `view_context` too (`{**extra, **data}`). The debug event's
  template is a `_DebugTemplate` proxy adding Jinja's `name`. `Templates.globals` mirrors
  `env.globals` (`{**globals, **context}`, `url_for` there by default). The route walk follows
  `.app` through middleware below `Mount` / `Host`.
- **Settings:** `VIEW_SLOTS = ('flask', 'starlette', 'fastapi')`;
  `chameleon_rs.starlette.configure()` sets the partials and its slot, `global_init` / `clear`
  only its slot. Settings are process-wide, as FastAPI's.

## Integrations amendments: Robyn (2026-10-22)

- **`chameleon_rs.robyn`** is chameleon-robyn's API on `_views`, with `_Robyn(Integration)`
  supplying `is_response` (`robyn.Response` only, as the original), `make_response`
  (`Response(status_code, description=html, headers=Headers({'Content-Type':
  f'{content_type}; charset=utf-8'}))`), `routes` and `watch`. Where chameleon-robyn differs
  from the other libraries, the subclass overrides the shared path rather than changing it for
  everyone: `render_response` / `render_response_async` pop the `__response_callback__` hook
  from a copy of the model (validated callable, called with the response), error pages render
  through the same path with `{'message': page.message, **data}` (chameleon-robyn's 404 model,
  so a callback in the data works on error pages too), and `decorate` resolves a bare
  `@template` name per request until a folder is configured, then fixes it (chameleon-robyn
  decides at the first request, so handlers may be decorated before `global_init()`; the
  other modules decide at decoration). The wrappers are `functools.wraps`'d: Robyn injects
  request parts by `inspect.signature`, which follows `__wrapped__`.
- **Execution model** (probed with a real server): `async def` handlers run on the asyncio loop
  of Robyn's main thread, so they render with `render_async`; sync handlers are called from
  Robyn's Rust worker thread with no running loop, so they render with `render` there, as
  chameleon-robyn does (not moved to a thread pool: default behaviour unchanged).
- **Decorator order.** Robyn's route decorators return Robyn's own wrapper of the handler and
  store it in `Route.function.handler`; `routes(app)` walks `type(app.router).get_routes(...)`
  (class method, bypassing the guard below) and the shared `__wrapped__` walk does the rest.
  `include_router` copies a SubRouter's routes into the app's table, so they are covered.
  `_Robyn.watch_app` installs two instance-attribute guards: `app.start` (the check runs in
  the main process before `run_processes`, so the app fails to start) and
  `app.router.get_routes` (read by `start()` and by `robyn.testing.TestClient.__init__`, which
  never starts the app). Only `start()` passing is final: it removes both guards. The
  `get_routes` guard re-checks whenever the route count changed since the last passing check
  (Robyn only appends routes), so an early read by the app's own code (a route listing at
  import time) does not end the check for routes added later (review fix; a plain memo would
  have). Rejected:
  `app.startup_handler`, a single slot per app (a later user call would replace ours, ours
  would replace theirs) run inside each server process, after the server is up.
- **No wheel everywhere.** Robyn 0.88.0 has wheels for CPython 3.10-3.14 only. It stays out of
  the dev dependency group; `make dev` installs it where a wheel exists, and its sources are
  unpacked into `temp/robyn-typing` for ty (`extra-paths`), so typing is checked on every
  Python; `make test-robyn` runs the Robyn tests in a 3.14 venv when `venv` cannot install
  Robyn. Tests skip cleanly without Robyn (`importorskip`; `test_typing.NEEDS`).
- **Settings:** `VIEW_SLOTS = ('flask', 'starlette', 'fastapi', 'robyn')`; process-wide, as
  FastAPI's (a handler has no app to find per-app settings in).
- **Deliberate small differences** (beyond the extras): the wrappers catch any
  `chameleon_rs.web.ErrorPage`, as the other modules' do, so another integration's
  `not_found()` (`chameleon_rs.fastapi.not_found`) raised inside a Robyn handler renders its
  page where chameleon-robyn lets the exception propagate. `ChameleonTemplate` derives from
  Robyn's `robyn.templating.TemplateInterface` for type checkers only (`TYPE_CHECKING`), so it
  type-checks as `template_engine=`; at runtime it derives from the local protocol, as
  chameleon-robyn's, since `robyn.templating` imports Jinja2.
- **One Robyn pin** (`ROBYN_VERSION` in the Makefile) for the dev install, the `test-robyn`
  and integration venvs, and the ty snapshot (`temp/robyn-typing`, unpacked from a wheel of
  that release by `scripts/fetch_robyn_typing.py`, standard library only, so no 3.14
  interpreter is needed; `make lint` depends on it).

## Integrations amendments: Litestar (2026-10-22)

- **`chameleon_rs.litestar`**, two layers (owner approved both). `ChameleonEngine` implements
  Litestar's `TemplateEngineProtocol[ChameleonTemplate, Mapping[str, Any]]`, modelled on its
  Mako engine: `__init__(directory, engine_instance, *, auto_reload, restricted_namespace)`
  (Litestar's `TemplateConfig.to_engine` calls `engine(directory=..., engine_instance=None)`),
  `get_template`, `render_string` (compiled strings cached, 128, with the loader's
  `restricted_namespace`), `register_template_callable`, `from_loader`. With neither a directory
  nor a loader it reads the new `litestar` settings slot at each render (as Starlette's
  `Templates()`). Template callables (`url_for`, `csrf_token`, `url_for_static_asset` by
  default) are bound to the render's context with `functools.partial`, as Mako's engine does;
  a context key of the same name wins, as over Jinja's globals (litestar-chameleon lets the
  callable win; documented). The callables mapping is replaced whole on registration
  (free-threading). `get_template` raises Litestar's `TemplateNotFoundException` for a missing
  file and for any name `_views.outside_folder` refuses (the Starlette guard, moved to the core
  so both Jinja-shaped APIs share it); the decorator API keeps the original libraries' lookup.
- **Media type.** Litestar's `Template.to_asgi_response` guesses from the name's suffixes with
  `mimetypes` and falls back to `text/plain`; `.pt` is unknown. The engine cannot fix that (the
  response chooses), so `chameleon_rs.litestar.Template` subclasses the response and passes
  `media_type=text/html` to `super().to_asgi_response` only when neither the response nor the
  route gave one and no suffix guesses. Rejected: `mimetypes.add_type` at import (a global side
  effect on every library; documented as an opt-in alternative) and setting `media_type` in
  `__init__` (it would override a route-level `media_type`).
- **Sync only, natively.** Litestar's template protocol and `Template` response are synchronous
  (`to_asgi_response` renders while building the response, on the loop). An async `Template`
  (rendering in a custom `ASGIResponse`) was not built: it leans on internals 3.0 is moving.
  Async rendering is the decorator layer's.
- **Decorator layer** on `_views` (`_Litestar(Integration)`), overriding `decorate` because
  Litestar reads the handler's signature: the wrapper gets `__signature__` and `__annotations__`
  with the handler's parameters, return annotation `litestar.Response` (else a `-> dict` handler
  is a JSON route: `media_type` JSON and a possible return DTO) and, when the handler has none,
  a keyword-only `request: Request` (Litestar injects reserved names by name; not in OpenAPI),
  which the wrapper pops before calling the handler. Annotations are read without evaluating
  (`annotationlib` FORWARDREF on 3.14+, raw `__annotations__` before), so forward references
  resolve when the route registers (Litestar evaluates them in the handler's module, found
  through `__module__`). The template gets Litestar's template context: `request`,
  `csrf_input` and the app's `ChameleonEngine` callables (Litestar's three otherwise), bound to
  that context (not the model); the model's keys win, error pages included. Pages have no
  status code unless `status_code=` is given (`Response(status_code=None)`: the route's, 201 for
  `@post`), unlike the other modules' 200: a Litestar response without a status takes the
  route's.
- **Decorator order.** Litestar's route decorators return a `BaseRouteHandler` object, so
  `@template` above `@get` is detected at decoration and raises `DecoratorOrderError` at import
  (unambiguous, unlike the other frameworks). For routes given the bare function another way,
  `configure(app=...)` inserts an `on_startup` hook first in `app.on_startup` (the lifespan
  fails; the test client raises it inside an `ExceptionGroup`) and wraps `app.asgi_handler` in a
  one-shot first-request guard for apps served without lifespan. `routes(app)` walks
  `app.routes` (`HTTPRoute.route_handlers`, websocket/ASGI routes' `route_handler`), unwrapping
  Litestar's `AsyncCallable` (`sync_to_thread`) and bound `Controller` methods. A `Litestar` app
  cannot be weakly referenced (`__slots__`), so "already watched" is the presence of the hook,
  not `_views._watched`.
- **Settings:** `VIEW_SLOTS` gains `litestar`; process-wide, as the others.
- **litestar-chameleon** (third party) is not mapped by `compat.install()` (there is no
  built-in with its API); its suite runs on the compat alias in `make test-integration`
  (original mode only, `src/` on the path), and `test_litestar.py` compares `ChameleonEngine`'s
  HTML with it on stock Chameleon. It is installed in `venv` with `--no-deps` by `make dev`,
  not in the dev group: `maturin develop` installs the dev group, and the integration venv must
  not get stock Chameleon.
- **Litestar in the dev group** (`litestar>=2.25,<3`, pure Python; msgspec has 3.15 wheels), so
  ty types the module on every Python; no pin file needed.
- **Review fixes** (Rust Architect review, no blockers): `render_string` compiles with the
  loader's template options (`loader.kwargs`: `translate`, `restricted_namespace`, ...), its
  cache is keyed by (loader, string), so a later `configure()` (a new loader) never serves a
  string compiled under the old options, and reads and writes it under a lock. `get_template`
  lets the loader's own lookup decide (its `default_extension`, search path): after the
  `outside_folder` guard it catches only the lookup's `ValueError` (`_views.is_not_found`: exactly
  `ValueError`, message `Template not found: `), no separate `isfile`. `outside_folder` no longer
  strips: a name with surrounding whitespace is refused (Jinja looks for that exact file,
  Chameleon's loader strips), for Starlette's `Templates` too (`isfile` on the unstripped name).
  litestar-chameleon is installed into venv from the same pinned clone as its suite (`--no-deps`),
  and `make test-python` sets `CHAMELEON_RS_REQUIRE_LITESTAR_CHAMELEON=1` so its comparisons fail
  instead of skipping when it is missing.

## Error-reporting amendments: `strict_tags` (2026-10-22)

- **The problem.** Chameleon's `ElementParser` leaves a start tag with no end tag as a
  `start_tag` item, which the program builder visits as an element with no children. TAL on it
  covers nothing after it, so the template compiles and the mistake surfaces elsewhere: a
  `tal:condition` that hides nothing, or a `NameError` for a `tal:repeat` variable lines
  further down. This is most of "Chameleon reports errors far from the cause".
- **The option.** `Options::strict_tags` (default false, so the default stays Chameleon's): in
  `zpt.rs`, an `Item::StartTag` whose element is in a TAL/METAL/i18n/meta namespace, or whose
  resolved attributes (after `data-tal-*` conversion) include one, is a `ParseError` before
  the element is visited. HTML void elements outside those namespaces are exempt
  (case-insensitive). Unclosed elements without template attributes stay allowed: they render
  the same either way. The error's token is the tag name, not the whole tag, because
  `TemplateError.__str__` prints the source line only for a nonzero column and an unindented
  tag starts at column 0.
- **Plumbing.** A template attribute (`PROGRAM_OPTIONS`, so loaders and keyword arguments
  pass it), defaulting to `CHAMELEON_STRICT_TAGS` (`config.STRICT_TAGS`), and a `Settings`
  field set by `chameleon_rs.configure(strict_tags=...)` and inherited by `build()`, so the
  framework integrations' own `configure()` functions keep their original libraries'
  signatures. The cache digest includes it only when it is on.
- **Checked against the corpus:** all 229 Talk Python templates compile with it on.

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
  `structure:` and `markdown:` of an awaitable make their final value a coroutine that awaits
  it and then converts (`_async._then`), so `${structure: coro()}` renders the result.
  Async iterables in `repeat`: not supported (Phase 11; see its amendments). Nested renders that go through Python
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
in a model is recognized and routed to the native path. *(As built in Phase 9: resolution is
the registered folder, then (by default, an owner-approved difference that only turns
chameleon_partials' "not found" errors into renders) the caller's search path and walking up
from its folder; `configure(partial_search='registered')` gives chameleon_partials' lookup
exactly. See the Phase 9 amendments.)*

## D11. Known, deliberate differences

- `template.source` (generated Python source) does not exist; with `keep_source=True` we expose
  a readable dump of the compiled program. Upstream tests only print it in failure messages.
- Slot fill functions are kept natively, so the scope has no `__slot_*` keys. *(Reversed in
  Phase 6b: the stacks are in the scope, as in Chameleon; see the Phase 6b amendments.)*
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
