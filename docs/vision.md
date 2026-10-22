# chameleon-rs vision

Ideas we intend to pursue, or might, that aren't designed yet. Nothing here is a commitment.
When an idea is designed, it moves into [architecture.md](architecture.md) as a decision and
into [plan.md](plan.md) as work, and its entry here shrinks to a link.

The rule from the architecture still holds for everything below: default behaviour matches
Chameleon exactly, and new capabilities are opt-in.

## 1. Built-in partials, and settings to override their defaults

> **Status (2026-10-21):** built in Phase 9 (architecture.md, Phase 9 amendments). Settings went
> to a module-level `chameleon_rs.configure(template_folder=..., auto_reload=...,
> restricted_namespace=...)`, process-wide, as an immutable object swapped whole (safe under
> free-threading); `register_extensions` sets the partials' folder (it is an alias). Where
> partials live, decided by Michael: the registered folder, then the calling template's search
> path, then walking up from its folder (`partial_search='caller'`, the default; it only turns
> chameleon_partials' "not found" errors into renders), or chameleon_partials' lookup exactly with
> `configure(partial_search='registered')`. Still open below: a `partials/` prefix
> convention, a partials-only reload override, renaming or turning off `render_partial`,
> `CHAMELEON_*` environment variables, and per-loader settings.

Decided so far (D10, Phase 9): `render_partial(path, **kwargs)` works in every template with
no setup, with chameleon_partials' semantics. A partial renders as a native frame straight into
the caller's buffer. The Phase 2 prototype costs 618–640 ns per item against the 0.8 µs budget.

Not yet designed: how an app changes the defaults. chameleon_partials takes its settings in
`register_extensions(template_folder, auto_reload, cache_init)`. We want zero setup to be
the normal case, with an optional way to say:

- **Where partials live.** Defaults to the loader's search path, then walking up from the
  calling template's folder. You might want to pin one root folder, or add a `partials/`
  prefix convention.
- **Reload behaviour.** Inherit the loader's `auto_reload` by default, with an override for
  partials only.
- **Caching.** How resolved paths and compiled partials are cached, and how tests clear the
  cache. That covers what `cache_init=False` does today.
- **The name.** `render_partial` by default; maybe an alias, or turning it off for apps that
  define their own.
- **Migration.** Should a leftover `register_extensions(...)` call carry its settings over to
  the built-in partials, or be ignored, or warn?

Open questions:

- Where do the settings go? Candidates are `PageTemplateLoader(...)` keyword arguments, a
  module-level `configure()`, and `CHAMELEON_*` environment variables like Chameleon's other
  settings. Probably the first plus the third.
- Do settings apply per loader or process-wide? chameleon_partials is process-wide and
  unlocked, which isn't a good fit for free-threaded Python.

## 2. Built-in framework integrations: Flask/Quart, FastAPI and Robyn

> **Status (2026-10-21):** the Flask/Quart part is built (Phase 9, `chameleon_rs.flask`, per the table
> below except `render_async`/`response_async`, which wait for Phase 11). FastAPI and Robyn remain
> vision items with the same API; `chameleon_rs.web` already holds the shared exceptions and model
> conversion.

Same idea as partials: what chameleon-flask and fastapi-chameleon provide today becomes part of
chameleon-rs, with no separate package and less setup. chameleon-robyn has the same shape and
is included too.

Both libraries have nearly the same API:

- `global_init(template_folder, auto_reload, cache_init)`
- `@template('page.pt')` (or bare `@template`, which finds the template from the view's name)
- `render()`
- `response()`
- `not_found()` and `generic_error()`
- `clear()`

A view returns a dict; the decorator renders it and builds the framework's response, or passes
a response object through unchanged.

Ideas:

- **Modules:** `chameleon_rs.flask` and `chameleon_rs.fastapi`, keeping the same function
  names and signatures so moving over is a one-line import change. Possibly shims so the old
  `import chameleon_flask` / `import fastapi_chameleon` keep working, like the `chameleon`
  compat alias.
- **No new dependencies.** The framework is already in the user's app. These modules import it
  only when they are imported themselves, so `import chameleon_rs` never pulls in Flask or
  FastAPI. This follows the no-runtime-dependencies rule, so no extras are needed.
- **Settings in one place.** One configuration call covers the template folder, auto-reload,
  partials, markdown settings and so on, instead of a `global_init` per library (see
  section 1's settings question).
- **FastAPI and async.** The `@template` decorator could await `render_async`, so a slow
  template never blocks the event loop under Granian or Uvicorn. This is the integration point
  where async rendering pays off with no template changes.
- **Native responses.** Rendering could go straight to the response body with no extra copies,
  and `not_found()` / error pages could use precompiled templates.
- **Tests.** Phase 7 already runs both libraries' test suites against the compat alias. The
  built-in versions would have to pass the same suites.

Leanings from Michael (2026-10-21):

- **Template folder and name lookup:** use chameleon-flask's rules exactly:
  - The folder is the one given to `global_init`, or `templates` relative to the working
    directory when it was never called.
  - A bare `@template` looks for `{last module segment}/{function name}.html`, then the same
    with `.pt`.
- **Async:** yes. Flask async views and **Quart** are both supported, as chameleon-flask does
  today. Async views would render with `render_async`.

API direction, from an interview with Michael (2026-10-21). Still a vision, not a plan: modernise
the API and keep every old name and signature working as an alias, so moving over stays a
one-line import change.

| Today (chameleon-flask / fastapi-chameleon / chameleon-robyn) | Chosen direction | Why |
|---|---|---|
| `global_init(template_folder, auto_reload, cache_init[, restricted_namespace])`, once per library | `configure(template_folder=..., auto_reload=..., ...)`, one call for templates, partials, markdown and the integration; `global_init` stays as an alias | One place for every setting |
| `cache_init=True` (a second `global_init` is a no-op unless `False`) | `configure()` always applies; tests call `clear()` to reset. The `global_init` alias still accepts and honours `cache_init` | The flag is hard to understand; `clear()` already exists |
| `mimetype=` (FastAPI) vs `content_type=` (Flask) | `content_type=` everywhere; `mimetype=` still accepted, with no warning | Same keyword everywhere, matching the HTTP header |
| `@template(..., status_code=)` in Flask only | `status_code=` in every framework | Parity |
| `not_found(four04template_file='errors/404.pt')`, empty model | `not_found(template_file='errors/404.pt', **template_data)`; the old keyword and positional calls still work | Clearer name, and the 404 page can get data |
| `generic_error(template_file, status_code, template_data)` in FastAPI only | `error(template_file, status_code, **template_data)` everywhere; `generic_error` stays as an alias with today's signature | Parity, and data passed like `render()` |
| Views must return a `dict` | Any `Mapping`, dataclasses (shallow field mapping) and Pydantic models (duck-typed through `model_dump()`, without importing pydantic) | Fewer conversions in views; the zero-dependency rule holds |
| `render()` / `response()` are sync only | `@template` picks `render_async` automatically for async views (FastAPI, Quart, Flask async), plus explicit `render_async()` / `response_async()` | Async views never block the event loop |
| Per-library exceptions (`FlaskChameleonException`, `FastAPIChameleonException`, the not-found variants) | Shared `chameleon_rs.web.TemplateError` / `NotFound`; the old names become subclasses | One way to catch errors; old `except` clauses keep working |
| Module-level global state (one template folder per process) | Process-wide default as today, plus optional `configure(app=app, ...)` stored in Flask's `app.extensions` / FastAPI's `app.state` | Several apps or test fixtures per process; safer under free-threading |
| `import fastapi_chameleon` / `import chameleon_flask` / `import chameleon_partials` | `chameleon_rs.compat.install()` also maps these old module names to the built-ins, like the `chameleon` alias | Existing apps switch with one line at startup |
| chameleon-robyn | Included: `chameleon_rs.robyn` with the same API | Its suite already passes on the compat layer; same shape |

## 3. Async rendering

Decided so far (D9, D3): `render_async()` is an `async def` on the template class. The VM can
pause and resume, so an expression whose final value is awaitable suspends the render instead
of blocking the event loop. Cancellation works. Phase 1 measured this: 100 concurrent renders
took 45–50 ms, and cancelled renders stopped cleanly.

Still speculative:

- **Concurrent slow expressions.** Mark a helper (a decorator, or template syntax) so it starts
  as soon as its inputs exist. A placeholder holds its spot in the output until the value
  arrives. This changes evaluation order, so it's strictly opt-in.
- **Partial fan-out.** Built-in partials only see their keyword arguments, so several of them
  can render at once.
- **Async iterables in `tal:repeat`.** Whether `async for` sources are allowed, and what a
  loop over one means for `repeat.length`.
- **Streaming.** Send finished chunks of the page while later parts are still waiting. This
  needs a framework hook (ASGI) and has to settle what happens to output already sent when
  an error occurs later.
- **Granian single-threaded asyncio** is the deployment we design for. The success measure is
  event-loop lag under 1 ms while an async render is in flight.

## 4. Memory saver mode

An opt-in mode that releases objects as early as possible during a render, so peak memory
drops on large pages. It costs a little speed. Large pages are the target: long listings, big
query results, and many partials per page.

The idea is to drop our reference the moment the template is done with a value, so CPython's
reference counting frees it mid-render instead of at the end.

- **Last-use analysis at compile time.** We already compute each expression's free variables
  (D9). Compute where each `tal:define` name and loop variable is last used, and emit a
  "clear" instruction after that point. The instruction sets the name to `None` or deletes it
  from the scope.
- **Loop items.** Chameleon builds the loop's whole list up front and holds it until the loop
  ends. In saver mode the VM owns that list and replaces each item with `None` once its
  iteration is done, so item *n* can be freed while item *n+1* renders. This only helps when
  nothing else holds the items, for example rows from a generator or a query.
- **Intermediate values.** Release converted values and temporary strings straight after
  they're written to the output, instead of keeping them in frame slots.
- **Partials.** A partial's fresh scope and its keyword arguments are released when its frame
  returns. That likely needs no extra mode; check it.
- **The output buffer itself** can't shrink, because the page is the result. Streaming
  (section 3) is the real fix for very large output.

Where it must turn itself off, because it would change behaviour:

- When a name might be read in a way we can't see at compile time: `econtext`, macros and
  slots (scopes are copied into another template), fallback expressions that reach names
  dynamically, `<?python ?>` blocks, and `tal:on-error` handlers.
- When a value might be needed after its last visible use, for example a global define read
  by a later macro. If in doubt, keep the value. The mode must never change output.

How we'd prove it: a benchmark that reports peak memory (`tracemalloc` and RSS) and time on a
large listing, with and without the mode. It ships only if the memory win is clear and the
time cost stays small (say under 5%). All differential tests run in both modes.

## 5. Profiling and optimisation once it's finished

Phase 10 is a performance pass before release, aimed at the plan's speed targets. This section
is about what comes after: once the engine is complete and matches Chameleon, profile it on
real workloads and keep making it faster, without giving up any compatibility.

- **Two kinds of speed.** Steady-state render speed is the main goal. First-render speed also
  matters: right after a deploy or reload, the first request to each page waits while its
  templates (and their macros and partials) are read, parsed and compiled, and that request
  can be noticeably slow. We time both, separately, and optimise both.
- **Profile real pages, not just the benchmarks.** Render actual Talk Python and Talk Python
  Training pages under `make build-profiling` with Instruments or py-spy. Find where the time
  goes: expressions, conversion, escaping, the VM loop, partials, or the Python wrapper.
- **Candidates** (some are already listed in Phase 10):
  - Caching attribute chains per call site.
  - Interned key caches.
  - SIMD escaping.
  - Fewer temporary objects during conversion.
  - Moving `render()` setup into Rust.
  - Profile-guided optimisation (PGO) for the release wheels.
  - The `chameleon-capi` crate (D12), but only for a gap we've measured.
- **First-request time after a reload.**
  - Measure it the way users feel it: start the app, time the first request to each real page,
    and break the time into reading the file, tokenizing, parsing, building the program,
    compiling expressions (CPython's `ast` is likely the largest part), and the first render.
    Compare with Chameleon, whose median compile is 3.5 ms per template.
  - Ideas:
    - Make parsing and compiling faster.
    - Cache compiled programs on disk, like Chameleon's `CHAMELEON_CACHE`, so a reload skips
      parsing for unchanged files.
    - Optionally warm templates in the background at startup, so the first request finds them
      ready.
    - Make the `auto_reload` mtime checks cheap.
  - Track it in CI alongside steady-state speed.
- **Free-threaded scaling.** Measure throughput on 3.15t as threads are added, and remove any
  contention we find.
- **Guard against regressions.** Track the benchmarks over time (criterion plus the `bench/`
  scripts) and fail CI on a real slowdown.
- **The rule:** every optimisation must keep all differential tests and the real-site diff
  identical. Speed never buys a difference in output.

## 6. A `markdown:` expression type

The idea: `${markdown: episode.notes}`, or `tal:content="markdown: episode.notes"`, converts
the value from Markdown to HTML at render time and inserts it as markup.

- **It fits an existing mechanism.** Chameleon already lets apps register custom expression
  types, and Phase 5 builds that bridge. `markdown:` could be a built-in one, opt-in because
  Chameleon has nothing like it.
- **Speed.** A Rust Markdown library (pulldown-cmark or comrak) would convert in-process with
  no Python calls. If a value hasn't changed, its HTML could be cached, keyed by the string.
- **Matching the site's existing output.** Talk Python's pages may already convert Markdown in
  Python with a particular library and particular extensions. Switching libraries could change
  the HTML, so the converter and its options need to be chosen deliberately, maybe made
  pluggable.
- **One-time configuration.** Michael's picture is a single setup call at app startup, and
  every `markdown:` expression uses those settings. The settings would cover:
  - **Features:** tables, footnotes, strikethrough, task lists, autolinks, heading IDs,
    smart punctuation.
  - **Styles:** CSS classes added to the generated elements (for example `table`, `pre`,
    `code`), a wrapper element, and code highlighting.
  - **Pass-through options:** handed unchanged to the chosen converter, so a site can mirror
    the configuration of the Python Markdown setup it already has.
  - **Named profiles** (maybe): several configurations selected in the template, for example
    `markdown[comments]: text`.

  Configuration is read once, so rendering never parses settings. This fits the
  configuration story in section 1: loader keyword arguments or a module-level `configure()`,
  plus environment variables.
- **Safety.** Markdown can contain raw HTML. Decide whether the output is trusted like
  `structure:` or sanitised. That could be one opt-in for trusted content and another for
  untrusted input, such as comments.
- **Open questions:**
  - A prefix (`markdown:`) or a filter-style syntax?
  - Is inline Markdown (no wrapping `<p>`) a separate mode?
  - How does it combine with `|` fallbacks and `structure`?

## 7. Documentation

We'll need real docs, built with Great Docs and hosted on Michael's site. A first outline:

- Install, and the one-line switch from Chameleon (`chameleon_rs.compat.install()`).
- Compatibility: what's identical, the known differences (D11), and how we measure it.
- What's new: async rendering, built-in partials and their settings, concurrent expressions,
  memory saver mode.
- Performance: the benchmark numbers with their methods, and how to reproduce them.
- A migration guide from Chameleon and chameleon_partials.
- An API reference generated from the Python classes and the `.pyi` stubs.

Open: the docs site's URL, whether the docs live in this repo (`docs/site/`) or on their own,
and when to start. The API is stable enough to document after Phase 7.

## 8. Icon and logo

We want one for the README, the docs site and PyPI. Directions to explore:

- A chameleon whose skin pattern is gears, or Rust's oxidised orange.
- A chameleon's curled tail drawn as `${ }` or a template bracket.
- Keep the mark simple enough to work as a 16 px favicon.

Constraint: don't use the Rust logo itself. The Rust Foundation's trademark policy limits how
it can be used in other projects' logos, so allude to Rust (colour, gears) rather than copying
its mark.
