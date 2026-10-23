# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`strict_tags`: template errors reported where the mistake is.** Chameleon treats an
  unclosed start tag as an empty element, so a `tal:condition` missing its `</div>` hides
  nothing, and a `tal:repeat` missing its `</ul>` fails with a `NameError` on a later line.
  With `strict_tags=True` (on a template or loader, in `chameleon_rs.configure()`, or through
  `CHAMELEON_STRICT_TAGS=1`), an unclosed element carrying `tal:`, `metal:` or `i18n:`
  attributes, or a `<tal:...>`/`<metal:...>` element, is a `ParseError` pointing at its start
  tag. HTML void elements (`<input>`, `<img>`, ...) are allowed. Off by default, as in
  Chameleon; every template of two production sites compiles with it on.

## [0.1.49] - 2026-10-22

Fixes from a second, independent review of the 0.1.48 changes.

### Security

- **Render data could replace a code block's own `def`, parameter or import named like a
  builtin** (`def filter(...)` in a `<?python ?>` block, data named `filter`). A regression in
  0.1.48's code-block fix; it now matches Chameleon again.
- **A partial lookup could loop forever**, hanging the worker, when the calling template's
  path started with `//` and the configured folder's with `/` (or, on Windows, they differed in
  case). A folder configured as `//...` made every partial "not found".
- **On case-insensitive filesystems (macOS, Windows), each spelling of a name compiled and kept
  its own template** (`INDEX.pt`, `index.PT`): 3,000 spellings took 360 MB. Templates are now
  cached by file, in `render_partial` and the Jinja-style `get_template` APIs.
- **More deep nesting crashes fixed:** `chameleon_rs.parser.ElementParser`, and compiles on
  small thread stacks or with most of the stack already used, overflowed the native stack.
  Compiling now also checks the stack actually left (with the `stacker` crate).

### Fixed

- A directory name given to `render_partial` or a Jinja-style `get_template` is "not found",
  rather than an `IsADirectoryError`. `chameleon_rs.robyn.ChameleonTemplate` raises
  `TemplateNotFound` for a missing file too.
- With nothing configured, a template loaded by a `PageTemplateLoader` below a package's
  `templates` folder finds partials elsewhere in that folder again (a Pyramid layout).
- A `@concurrent` helper wrapped in `structure:` or `markdown:` runs concurrently again.

## [0.1.48] - 2026-10-22

Fixes from a security review of the whole codebase. Each one has a regression test.

### Security

- **`render_partial` could load files outside the template folder.** The search through the
  calling template's folders went up to the filesystem root, so a name with no `..` (such as
  `etc/hosts`) could render a system file, and a file planted in any parent folder could be
  compiled and run as a template. The search now stops at the root of the caller's template
  tree: the configured folder (for partials or any framework integration), a loader search
  path, or with nothing configured, a Python package's `templates` folder. Names that leave
  the configured folder (an absolute path, a `..` out of it, a `package:path` spec) are not
  found, as in chameleon_partials 0.2.1, which makes the same change.
- **`markdown:` in an attribute value allowed script injection.** Markdown's own quoted
  `href="..."` closed the surrounding attribute, so a link in user text could add an event
  handler. In an attribute value the result is now escaped like any string; element content
  is unchanged.
- **Wide Markdown tables took quadratic time**: a 64 KB table of 16,000 columns took 10 s to
  convert, blocking an event loop for that long. Table cells now render in linear time
  (0.06 s), with the same output.
- **Deeply nested templates crashed the process** (a native stack overflow, SIGSEGV) where
  Chameleon raises `RecursionError`. Compiling now raises `RecursionError` past a 1 MiB stack
  budget: at least as deep as Chameleon goes at the default recursion limit, and safe on a
  2 MiB thread.
- **Render data could replace Python builtins inside `<?python ?>` blocks** (passing
  `len=...`). Code blocks now call builtins directly, as Chameleon's generated code does.
- **`chameleon_rs.robyn.ChameleonTemplate` had no path check.** It now refuses names outside
  its folder, as Robyn's `JinjaTemplate` and the Starlette and Litestar drop-ins do.
- **One template could fill the template cache many times over**: each spelling of a name
  (`./big.pt`, `.//big.pt`) compiled and kept its own copy. Every spelling is now one cached
  template, in partials and in the Jinja-style `get_template` APIs.
- The Markdown cache counted HTML by its UTF-8 length; a Python `str` with one emoji takes 4
  bytes a character, so the cache could hold 4 times its byte limit. It now counts the
  string's real size.
- Filling the placeholders of `@concurrent` calls was quadratic in their number (160,000
  calls: 2.2 s). It is now linear (0.3 s).

### Fixed

- An object whose `__html__` attribute is `None` raised `TypeError`; it now falls back to
  `str()`, as in Chameleon.
- An unclosed comment with `${...}` ending in multi-byte text raised `InternalError`; comments
  are sliced by code point, as Chameleon does.
- In `render_async`, `${structure: coro()}` and `markdown:` of an awaitable wrote the
  coroutine's `repr` and never ran it. The value is now awaited, then converted.

### Changed

- Flask text responses name their charset (`text/html; charset=utf-8`), as Flask's own
  `render_template` responses and the other integrations do. chameleon-flask sent the bare
  type.
- The user guide has a **Security** page: where escaping stops (attribute names from data,
  unquoted attributes, CDATA, `<script>`, URLs) and which lookups accept names from requests.

## [0.1.47] - 2026-10-22

The first release of chameleon-rs: a Rust implementation of the
[Chameleon](https://github.com/malthe/chameleon) page template engine, meant as a drop-in
replacement. Documentation: <https://mkennedy.codes/docs/chameleon-rs/>.

### Added

- **The engine.** Templates compile to a native program that a Rust VM runs; Python runs only
  where a template calls into your code. Chameleon's template language (TAL, METAL, i18n,
  `${...}`, every expression type) and Python API (`PageTemplate`, `PageTemplateFile`,
  `PageTextTemplate`, `PageTextTemplateFile`, `PageTemplateLoader`, their options,
  `CHAMELEON_*` environment variables, the same exceptions and error messages) are kept.
- **One-line switch.** `chameleon_rs.compat.install()` makes `import chameleon` (and
  `chameleon_partials`, `chameleon_flask`, `fastapi_chameleon`, `chameleon_robyn`) give
  chameleon-rs. Stock Chameleon need not be installed. Shadow mode (`chameleon_rs.shadow`)
  renders a sample of production traffic with both engines and reports any difference.
- **Built-in partials.** `${render_partial('shared/card.pt', item=item)}` works in every
  template and renders natively inside the page; chameleon_partials' API keeps working.
- **`chameleon_rs.configure()`**: one process-wide call for the template folder,
  `auto_reload`, partial lookup (`partial_search`) and Markdown settings, applied on every
  call and thread-safe.
- **Async rendering.** `await template.render_async(**model)` awaits awaitable model values
  where the template uses them, without blocking the event loop (macros, slots, partials and
  `tal:on-error` work across the wait). With no awaitables the output is exactly `render()`'s.
  Opt-in: `@chameleon_rs.concurrent` helpers and `concurrent=True` partial fan-out run
  concurrently; `render_async(..., offload=True)` renders in a worker thread (experimental).
- **`markdown:` expressions** (opt-in, `configure(markdown=True)`): CommonMark plus the GitHub
  extensions, converted in Rust, with no Markdown package to install.
- **Framework integrations**, each importing its framework only when imported itself, with no
  extras to install:
  - `chameleon_rs.flask` (Flask and Quart): chameleon-flask's API.
  - `chameleon_rs.fastapi`: fastapi-chameleon's API.
  - `chameleon_rs.starlette`: `Templates`, a drop-in for Starlette's `Jinja2Templates`, and the
    decorator API.
  - `chameleon_rs.robyn`: chameleon-robyn's API.
  - `chameleon_rs.litestar`: `ChameleonEngine` for Litestar's `TemplateConfig`, and the
    decorator API.

  All of them render async views with `render_async`, and `configure(..., app=app)` fails at
  startup when a `@template` sits above its route decorator (`DecoratorOrderError`).
- **Typing.** The package is PEP 561 typed (`py.typed`, `_native.pyi`) and checks clean with
  `ty`.
- **Python and platforms.** CPython 3.12 to 3.15, including the free-threaded builds (3.14t,
  3.15t; the extension does not re-enable the GIL). Wheels for Linux (manylinux and musllinux,
  x86_64 and aarch64), macOS (arm64 and x86_64) and Windows (x64 and arm64), built with
  profile-guided optimization (musllinux and Windows arm64: where the build can make a
  profile, else plain). No runtime dependencies.

### Compatibility

- Chameleon's own test suite, vendored unmodified: 227 of the 227 tests that pass on
  Chameleon pass on chameleon-rs (the other 6 depend on the environment on both engines).
- All 122 fixture renders of that suite are byte-identical.
- Differential fuzzing of templates, partials, macros/slots/i18n and expressions finds no
  differences.
- The test suites of fastapi-chameleon, chameleon-flask, chameleon-robyn, chameleon_partials,
  litestar-chameleon and pyramid_chameleon pass unmodified on chameleon-rs. As in Chameleon, a
  macro that a template's new source no longer defines (a reload) still renders its last
  version, which pyramid_chameleon's macro renderer relies on.
- Every page of two production Flask/Quart sites renders byte-identical HTML.

### Performance

Release build without PGO, Python 3.15 on an Apple M-series Mac, both engines in the same
process and interleaved (`bench/bench_ab.py`, `bench/bench_macros.py`,
`bench/bench_skeleton.py`; method in `docs/plan.md`, "How speed is measured"). The released
wheels are profile-guided, about 10% faster again.

| Scenario | chameleon-rs | Chameleon | Speedup |
|---|---|---|---|
| Listing, per item | 431 ns | 2,835 ns | 6.6x |
| Macro call filling a slot, per item | 676 ns | 4,648 ns | 6.9x |
| App-shaped page | 69.6 µs | 301 µs | 4.3x |
| Native `render_partial` listing vs chameleon_partials, per item | 654–680 ns | 7,300 ns | 10.9x |
| Compile one template, median | 0.053 ms | 3.8 ms | 72x |

On a production Quart site (every page, PGO wheel, same data, median of 5): template
rendering 2.2x faster in total (per-page median 2.9x), whole requests 1.3x faster; under a
20-user load test on one single-threaded worker, throughput +34% and p99 latency 255 ms to
195 ms. See [Performance](https://mkennedy.codes/docs/chameleon-rs/user-guide/performance.html).

### Known differences

Listed on the [Compatibility](https://mkennedy.codes/docs/chameleon-rs/user-guide/compatibility.html#known-differences)
page: `template.source` is a dump of the compiled program, not Python; custom expression types
run through a bridge (types relying on Chameleon's compiler internals are not supported);
`CHAMELEON_CACHE` writes nothing; Chameleon's code-generation modules do not exist; a few
error-handler corner cases inside slot bodies; built-in partials also look next to the calling
template (`configure(partial_search='registered')` turns it off); awaitable model values are
awaited in async views; and a replaced `render()` makes `render_async()` render synchronously.
