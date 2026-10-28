# Plan: `chameleon_rs.zope`, a page template engine for Zope and Plone

Status: planned, not started (2026-10-28). Follows issues #8 to #12 (zopyx), all closed.

## Goal

Let a Zope or Plone site render its page templates with chameleon-rs by turning it on in
ZCML, without aliasing `chameleon` process-wide and without changing any template. Zope's own
expression machinery (`zope.tales`, RestrictedPython for untrusted templates) keeps evaluating
every expression; chameleon-rs replaces the template compiler and renderer underneath it.

The module should feel like `chameleon_rs.flask` and `chameleon_rs.fastapi`: one import away,
importing the framework only when it's imported, documented on its own user-guide page. Zope's
integration point is a utility, not decorators, so the API is smaller.

## Where things stand

There are two ways to run Zope on chameleon-rs:

- **Route A, `compat.install()`.** Zope's stock engine (`Products.PageTemplates.engine.Program`)
  builds every template as a subclass of z3c.pt's `PageTemplate`, and wraps each Zope expression
  type in a `MappedExpr` custom type. Under `compat.install()` both resolve to chameleon-rs.
  Before #9 this failed. Since #9, z3c.pt's own suite passes, so Route A **may already work for
  Zope**. Nobody has tried it with Zope's suite or a real site yet.
- **Route B, an engine utility.** Register a different `IPageTemplateEngine` utility that
  compiles with chameleon-rs directly and leaves `import chameleon` alone. This is what zopyx
  built, and it rendered 11 Plone 6.2 pages byte-identically to z3c.pt once #8 and #10 were
  worked around. `chameleon_rs.zope` is this route, made into a supported module.

**Why Route B is worth having even if Route A works:**

- **Activation.** `compat.install()` must run before anything imports `chameleon`. In Zope that
  means `sitecustomize` or the WSGI entry point, before Zope and z3c.pt load. Route B is a ZCML
  include: it can be added, removed and rolled back like any other Zope configuration.
- **Coexistence.** Stock Chameleon stays installed (Zope and z3c.pt depend on it) and keeps
  serving anything that doesn't go through the utility. A site can switch over with a fallback
  path.
- **A place for Zope-specific speedups** (Phase 4): Route A can only ever run Zope's engine as
  Zope wrote it.

**If Phase 0 shows Route A works well, the result may be smaller than this plan:** a documented
recipe plus Route A in `make test-integration`, with the module deferred. Phase 0 decides.

## How Zope reaches the engine

The contract, read from the code:

- `zope.pagetemplate.pagetemplate.PageTemplate._cook` looks up the `IPageTemplateEngine` utility
  and calls `cook(source_file, text, engine, content_type)`, which returns `(program, macros)`.
- It then calls `program(context, macros, tal=..., showtal=..., strictinsert=0,
  sourceAnnotations=...)` to render. `tal=False` means "return the source" (used for
  `pt_source_file`, macro-expansion errors and the ZMI).
- **Who uses it:** Zope's `PageTemplate`, `PageTemplateFile` and `ZopePageTemplate` (through the
  web), and Five's `ViewPageTemplateFile`. That last one covers most of Plone's browser views.
  Templates built on z3c.pt directly don't go through it, and stay on stock Chameleon in Route B.
- Zope registers its engine in `Products/PageTemplates/configure.zcml`:
  `<utility component=".engine.Program" />`. Ours replaces it with an override.

Zope has two TALES engines:

- **Untrusted** (through-the-web templates): RestrictedPython `python:`, `<?python ?>` blocks
  stripped.
- **Trusted** (filesystem templates): plain Python. The engine object passed to `cook` says
  which (`engine.untrusted`).

## Design

### Layout

```
python/chameleon_rs/zope/
    __init__.py        # docstring with the API, as flask.py has; re-exports Program
    engine.py          # Program, ZopePageTemplate, MappedExpr and the context bridge
    configure.zcml     # registers nothing by itself (safe to include)
    overrides.zcml     # replaces Zope's IPageTemplateEngine utility with ours
```

**Activation**, in a site's ZCML (`site.zcml`, or a Plone add-on's `overrides.zcml`):

```xml
<includeOverrides package="chameleon_rs.zope" file="overrides.zcml" />
```

**Imports:** importing `chameleon_rs.zope` imports Zope (`zope.pagetemplate`, `zope.tales`,
`AccessControl`, `Products.PageTemplates`). `import chameleon_rs` never does. No extra is needed:
Zope is already installed wherever this module is used, the same as Flask for
`chameleon_rs.flask`. It must not import stock `chameleon` anywhere. Check this with a test that
fails if `chameleon` (the stock package) is in `sys.modules` after a render.

### `engine.py`

A port of `Products/PageTemplates/engine.py` (Zope, ZPL 2.1), with the z3c.pt parts it relies
on. Every name resolves to chameleon-rs, not `chameleon`.

- **`Program`**, which provides `IPageTemplateEngine` and implements `IPageTemplateProgram`:
  - `cook(source_file, text, engine, content_type)`:
    - Strips `<?python ?>` blocks when `engine.untrusted`, logging as Zope does.
    - Builds the expression types: `MappedExprType(engine, name)` for every type of a
      `ZopeBaseEngine`, `engine.types` otherwise.
    - Handles the `file:` prefix CMFCore uses.
    - Returns `(Program(template, engine), template.macros)`.
  - `__call__(context, macros, tal=True, **options)`:
    - `tal=False` returns the body.
    - Otherwise it builds the render keywords from `context.vars`, Zope's repeat wrapper,
      `__zt_engine__`, `__zt_context__` and `default`, then renders.
    - Other options are accepted and ignored, as Zope's `Program` does.
- **`ZopePageTemplate(chameleon_rs.PageTemplate)`**, what z3c.pt's `PageTemplate` plus Zope's
  `ZtPageTemplate` add up to:
  - `boolean_attributes = HTML5_BOOLEAN_ATTRIBUTES` (#11), or none for `text/xml`.
  - `enable_comment_interpolation = False`.
  - `value_repr = repr`.
  - `keep_body = True`.
  - `encoding = 'utf-8'`.
  - Builtins `nothing`, `modules` (Zope's `SecureModuleImporter`) and `tales`
    (`ExpressionEvaluator(self.engine, builtins)`).
  - Request-based language negotiation, and the `i18n.Message` type.
  - **Not** `render_partial`. See *Security*.
- **`MappedExpr` and `MappedExprType`:**
  - Zope's port, unchanged in behavior: a compile cache keyed by engine, type and text; a call
    to the compiled `zope.tales` expression with a context wrapper; the guard against `${}`
    characters in path expressions.
  - It raises `chameleon_rs.exc.ExpressionError`. Since #8, Chameleon's class would also drive
    the retry, but this module never imports it.
  - The `attrs` workaround Zope keeps for malthe/chameleon#323 has to be checked against
    chameleon-rs. It may be unnecessary, but keep it unless a test shows otherwise.
- **The context bridge** (`_C2ZContextWrapperBase`, `_with_vars_from_chameleon`,
  `Name2KeyError`): a port. It wraps our `Scope`, which already raises `NameError`/`KeyError` as
  Chameleon's does.
- **`RepeatDictWrapper` and `RepeatItem`:**
  - Subclass `chameleon_rs.tal.RepeatDict`, a native class that keeps Chameleon's subclassing
    since 0.1.53.
  - Keep Zope's `AccessControl` declarations.
  - Check that `InitializeClass` works on a subclass of a native class.

### Security

This is where Zope differs from every framework we support so far. Our threat model says
**templates are trusted** (memory: threat-model-templates-trusted). In Zope that isn't so:
`ZopePageTemplate` objects are edited through the web by users who must not get more than
RestrictedPython allows. Route B keeps every expression in `zope.tales`, so RestrictedPython
still guards all the Python. What we have to make sure of is that **chameleon-rs adds no way
around it**:

1. **Template builtins:** no `render_partial`, and nothing else beyond the ones Zope gives.
   `ZopePageTemplate` builds its own builtins and doesn't call `super()._builtins()`.
2. **Expression types:** only the engine's types.
   - No `load:`, no `import:`, no `markdown:`, even if `chameleon_rs.configure(markdown=True)`
     is set process-wide.
   - Check how `native_types` adds configured Markdown profiles. It adds them for prefixes the
     template class doesn't define, so an untrusted template would get them. Zope templates
     must opt out.
3. **No direct Python evaluation:**
   - Code blocks are stripped for untrusted engines, as Zope does.
   - Audit every place the native compiler evaluates Python itself rather than through a type:
     `tal:define` names, `repeat`, i18n, the `default`/`attrs` aliases, error formatting, the
     `strict=False` deferred errors.
   - The audit's result goes into the architecture doc.
4. **Macros under RestrictedPython:**
   - Zope marks Chameleon's `Macros` as `__allow_access_to_unprotected_subobjects__`.
   - Our `Macro` is a frozen native class and can't take attributes. Find out whether guarded
     access reaches `Macro` itself, through `use-macro` on a path from untrusted code. If it
     does, the macros need a Python wrapper or a declaration on the native class.
5. **Rendered errors:** our `RenderError` shows variable values (`value_repr`). Zope sets
   `value_repr = repr`; make sure nothing private leaks into an error a TTW user sees that
   Zope's engine wouldn't show.

A short security review of the finished module is part of Phase 2's gate.

### Errors and output

- Zope code catches Chameleon's errors in a few places (`PTRuntimeError: Compilation failed`
  comes from `_cook` catching everything). Grep Zope, Five and Plone for `chameleon.exc` and
  check that our exceptions satisfy every catch. Our `ExpressionError` and `TemplateError`
  subclass the same bases. Route B can't rely on `chameleon.exc` names being ours.
- **Output** must match Zope's stock engine byte for byte, as the reporter measured on Plone
  pages. Differences we already know of: none after #8, #10 and #11.

### Performance: what to expect

With `MappedExpr`, **every expression** calls into `zope.tales` at render time, through the AST
bridge. That's what stock Zope does with Chameleon too.
- **What chameleon-rs speeds up:** the template structure (TAL statements, macros, slots,
  attribute handling, escaping, output).
- **What it doesn't:** expression evaluation (path traversal, RestrictedPython).

Expect a real but bounded gain on Plone pages, and measure it before saying anything public
about speed. Phase 3 measures; Phase 4 is where larger gains would come from.

## Phases

### Phase 0: measure Route A (half a day)

1. Make a venv with Zope (pinned), its test dependencies and stock Chameleon. Run Zope's
   `Products.PageTemplates` and `Products.Five` browser page template tests on stock Chameleon
   for the baseline.
2. Run the same tests under `compat.install()` (alias through `sitecustomize`, as the z3c.pt
   suite does).
3. Write down every failure with its cause. Fix the ones that are chameleon-rs bugs, as
   separate issues: they matter to both routes.

**Exit:** a short report in this plan. If Route A passes, decide with Michael whether Route B
still goes ahead (see *Open questions*).

#### Phase 0 results (2026-10-28)

**Setup:**
- Zope 6.3, from the `6.3` tag (`c9225db`), with its own `constraints.txt` pins (Chameleon
  4.6.0, z3c.pt 5.2).
- Python 3.14.10, in a throwaway venv `temp/venv-zope`.
- chameleon-rs from main plus the two fixes below.
- `compat.install()` loaded at startup through `tests/integration/child_alias`, as for z3c.pt.

| Suite | Stock Chameleon | `compat.install()`, before fixes | After fixes |
|---|---|---|---|
| `Products.PageTemplates` | 285 tests, all pass (1 skipped) | 19 failures | all pass (1 skipped) |
| `Products.Five` | 62, all pass | all pass | all pass |
| All of Zope 6.3 | 1,736, all pass (1 skipped) | not run | all pass (1 skipped), same as stock |

**Two chameleon-rs bugs found and fixed.** Neither is Zope-specific, and neither touches the
native path the other frameworks use:

1. **The bridge changed a custom type's AST in place** (18 of the 19 failures). Lowering
   `Symbol`/`Static` nodes used `ast.NodeTransformer`, which replaces child nodes inside the
   type's own objects.
   - Zope's untrusted `python:` hands every expression the same `Static` node per builtin. The
     first compile replaced its `Symbol` with a name bound only in that compile, and the next
     expression that used it failed with `NameError: __rs_symbol_2`.
   - Lowering now copies nodes instead (Chameleon's code generator leaves them alone).
   - Only custom expression types go through this code.
2. **Error reports ignored the template's `value_repr`** when the template's builtins have no
   `template` entry. z3c.pt replaces the builtins, and Zope passes its own `template` render
   variable. Zope sets `value_repr = repr`.
   - The native render's error handler now finds the rendering template through a reserved
     builtins entry (`__rs_template`). It's added after compiling, so expressions can't see it.
   - This affects only the text of error reports.

**Conclusion: Route A works for Zope itself.** Plone wasn't tested (no Plone install here). That
is Phase 3's job with zopyx, and it can now start with plain `compat.install()`. The decision on
Route B (the module) comes after this review: see *Open questions*.

### Phase 1: `engine.py` and ZCML (1 to 2 days)

1. Port `Program`, `MappedExpr`, the context bridge and the repeat wrappers. Write
   `ZopePageTemplate` with z3c.pt's settings.
2. Add `configure.zcml` and `overrides.zcml`. Check that the ZCML files ship in the wheel (they
   sit in the Python source tree, which maturin packages, but verify with `make check-wheels`).
3. **Harness:** Zope's PageTemplates and Five tests, unmodified, with our utility registered in
   place of Zope's (a conftest or a test layer that loads `overrides.zcml`). Stock Chameleon
   stays installed.
4. **Our own tests:** `tests/python/test_zope.py`, skipped when Zope isn't installed, covering
   `cook` (trusted and untrusted), rendering, macros through path expressions, `tal=False`, the
   repeat wrapper and the "no stock `chameleon` imported" check.

**Exit:** Zope's suites pass on our engine, or every failure is explained and accepted by
Michael.

### Phase 2: security review (half a day)

Work through the *Security* list. Each item gets a test (for example: a TTW template can't call
`render_partial`, can't use `markdown:`, and `<?python ?>` is stripped). Then a review of the
module by the Rust Architect agent or a security-focused pass, findings addressed.

**Exit:** all items closed or documented, with tests.

### Phase 3: Plone check and measurement (with zopyx)

1. Ask zopyx to run their 11-page comparison and the `plone.app.layout`, `plone.app.z3cform` and
   `Products.CMFPlone` suites with `chameleon_rs.zope` active, with no workarounds, on a build
   from main.
2. Measure render time per page against stock Zope, with the same pages. Our bench harness
   can't run Plone, so this is zopyx's numbers plus a Zope-only benchmark of our own (a few
   templates through `Program`).

**Exit:** byte-identical pages and green suites on Plone, and numbers we can state honestly.

### Phase 4 (later, optional): native expressions for trusted templates

For filesystem templates on the trusted engine, some Zope types could compile natively instead
of through `MappedExpr`, where the semantics are provably the same:
- `string:` with only Python-free substitutions;
- `not:` and `exists:` over a native inner expression;
- possibly z3c.pt-style `python:`, the optimization noted in #9.

`path:` stays in `zope.tales` (Zope traversal). **Never for untrusted templates.** This is a
separate decision after Phase 3's numbers.

### Docs and release (with Phase 1 to 3)

- **New page:** `user_guide/zope.qmd`, covering activation, what it changes, what stays on stock
  Chameleon, security notes and known differences.
- A row for Zope/Plone in the frameworks tables (`user_guide/frameworks.qmd`, README), added
  only after Phase 3 confirms a real site.
- Compatibility guide: Zope's test suites in the integration-suite row.
- **CHANGELOG** and architecture doc: a new decision entry for the Zope engine and the security
  audit.
- **Tests:** `make test-integration` gains the Zope suites (a separate venv `temp/venv-zope`
  with stock Chameleon, since Route B coexists with it), plus Route A's run if Phase 0 makes it
  worth keeping.

## Licensing

- `engine.py` ports Zope's `Products/PageTemplates/engine.py` and parts of z3c.pt's
  `pagetemplate.py`. Both are under ZPL 2.1.
- The file starts with a header naming both sources and saying it was modified, as our other
  ports do.
- `COPYRIGHT.txt` already credits the Zope Foundation. Check that ZPL 2.1 needs nothing more
  for a port inside our package (it shouldn't: attribution and the disclaimer).

## Open questions for Michael

1. **If Route A passes in Phase 0,** do we still build Route B? The case for it is activation
   by ZCML, coexistence with stock Chameleon and room for Phase 4. The case against it is one
   more module to maintain for a framework few people start new projects on.
2. **Test cost:** Zope's suites need a Zope install in a separate venv (no Plone). Fine in
   `make test-integration`, or a separate `make test-zope` that `make test` doesn't run?
3. **Python versions:** decided, 3.14. Zope 6.3 supports 3.11 to 3.15, and Phase 0 ran on
   3.14.10 with no problems.
4. **The reporter:** coordinate Phase 3 with zopyx through the GitHub issues, or a new tracking
   issue for the Zope engine?
