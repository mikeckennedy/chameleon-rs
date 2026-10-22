# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.47] - not yet released

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
  Paths not in the configured folder are also looked for next to the calling template and in
  the folders above it, but never outside its template tree (the configured folder, a loader
  search path, or a package's `templates` folder). A name that would leave the folder (an
  absolute path, a `..` out of it, a `package:path` spec) is not found, as in
  chameleon_partials 0.2.1. `chameleon_rs.robyn.ChameleonTemplate` refuses such names as
  Robyn's `JinjaTemplate` does.
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
