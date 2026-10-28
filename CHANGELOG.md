# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`pathlib.Path` wherever a path string works** (malthe/chameleon#342, reported by
  markkuleinio): a single `Path` as `search_path` (Chameleon raises "'PosixPath' object is not
  iterable"), `Path` entries in a search path, and `loader[Path('page.pt')]`. Any
  `os.PathLike` is accepted; a list or tuple of strings is still kept as the loader's own
  `search_path`, as in Chameleon.
- **Zope's `repeat.item.first(name)` and `repeat.item.last(name)`** (malthe/chameleon#144,
  reported by pbauer): whether the item starts or ends a run of items with the same `name`
  attribute (or key, for a mapping), for headings and rules in a sorted sequence:
  `<h2 tal:condition="repeat.item.first('category')">`. Without a name, whole items compare.
  Chameleon never implemented them (its docs suggest `itertools.groupby`). `RepeatItem` takes
  the repeated items as an optional third argument; one made with Chameleon's two arguments
  raises a `TypeError` from `first` and `last` only.

## [0.1.53] - 2026-10-27

Less shared reference counting per render on free-threaded Python.

### Changed

- **Less shared reference counting per render on free-threaded Python,** from Kitewing's
  profile of a page served over HTTP. Each of these updated a count every rendering thread
  shares:
  - `chameleon_rs.tal.RepeatDict` (one per looping partial) is now a native class. Chameleon's
    keeps the dict's bound `__getitem__` and `__setitem__`, which reference `dict`'s own slot
    descriptors; creating them from six threads ran no faster than from one. It keeps
    Chameleon's name, module, subclassing, errors and `copy.copy`.
  - The check for `repeat` and `Markup` values took a reference to each value's type on every
    attribute access and every emitted value.
  - Calling a builtin such as `len` took a reference to it (builtin functions are not
    immortal).
  - The VM cloned the running program's `Arc` at every frame switch, twice per partial call; it
    now holds each program once per render.
  - The partials' `_prepare` imported the template module on every call, a lookup in
    `sys.modules` that takes that dict's lock on free-threaded Python.

## [0.1.52] - 2026-10-27

Free-threaded scaling for pages with partials, and three bugs found on the way.

### Changed

- **Pages with partials scale across threads on free-threaded Python.** Every partial call
  copied the template's render defaults (`__translate`, `__decode`, ...) from one dict shared by
  every thread, and on free-threaded CPython reading a dict takes its lock: six threads
  rendering a page with 52 partials did 2.4× the work of one. A loop or a mention of `repeat`
  now only gives a scope a fresh `repeat`. The defaults are copied in only where a template
  can see them (it names `econtext`, `rcontext` or a default, has names only known at render
  time, or calls a macro); an error message still lists them, as Chameleon's does. Switching
  between a page's and its partials' frames no longer takes references to shared objects, and
  on free-threaded builds the string constants of templates read from files are interned, as
  CPython does for code constants (never those of a template made from a string, which may be
  one per request).
  At six threads a page with partials now scales 3.7× (37,500 renders a second where it was
  22,900), and a flat template shared by every thread 4.3×.

### Fixed

- **A partial whose code read a render default by its plain name** (`translate`, `decode`,
  `on_error_handler`, which expressions read as `econtext['__translate']` and so on) raised
  `KeyError` when it had no loop.
- **An async render of a macro recursing until `tal:on-error` caught the `RecursionError`
  rendered fewer levels than Chameleon** (936 against 991 at the default limit): it kept a fixed
  reserve of 64 frames. It now measures the stack as a sync render does, and stops where
  `render()` called in place of `await render_async(...)` would.
- **Litestar's `render_string` cache could lose an entry** when two threads compiled the same
  string at once: the second evicted the oldest template before storing the key the first had
  stored. The second now keeps the first one's template.

## [0.1.51] - 2026-10-23

Fixes from two reviews: for memory leaks, GC problems and anything that could crash or wedge
a web worker, then for the memory, time and cache lifetimes a small input could take.

### Security

- **Renders nested through Python could crash the process on Python 3.12 and 3.13.** A value
  whose `__html__`, or a helper, renders another template (a comment thread rendering its
  replies) nests as deep as the data goes. Before 3.14, CPython counts only Python frames, so
  on a worker thread with a small stack (512 KiB to 2 MiB) the native stack overflowed at 68 to
  268 levels (SIGBUS) instead of raising `RecursionError`. Every render now checks the stack
  left when it starts.
- **A deep `metal:fill-slot` body could crash a compile on a small thread stack.** Copying the
  compiled slot body recursed without a guard; it now runs on a stack of its own when needed.
- **The decorator and module APIs compiled and cached every spelling of a template name
  forever** (`./sub/p.pt`, `sub//p.pt`, an absolute path): with names built from a request,
  the cache grew without bound. They now load one template per file. The names remembered for
  that are bounded (1024 per loader), names of missing files are never remembered, and a name
  seen before costs no `stat`.
- **A small Markdown value could make a conversion take hundreds of megabytes.** Every row of a
  table gets as many cells as its header, and comrak's own limit is per table: four sparse
  tables of 700 columns and 700 one-cell rows, 22 KB of text, made two million cells (500 MB
  peak, 20 MB of HTML). Text whose tables would make more than 100,000 cells, or more cells
  than it has bytes, now converts with tables off (their rows show as paragraphs), checked
  before parsing.
- **Nested namespace declarations took memory quadratic in the depth.** Every start tag
  copied its parent's prefix map: 2,000 nested `xmlns:` declarations (45 KB of template)
  took 330 MB before the compile rejected the depth. A tag now shares its parent's map, or
  stores only what it adds, and lookups while parsing cost the same at any depth.
- **A long run of whitespace made compiling quadratic.** 64K spaces after an attribute took
  8 s: the attribute search, and the `<meta>` charset sniffing (which Chameleon shares, for a
  run anywhere in the template), each rescanned the rest of the run from every space. Both
  are linear now: 256K spaces compile in 2 ms.

### Added

- **`strict_names`** (opt-in: `configure(strict_names=True)` or `CHAMELEON_STRICT_NAMES=1`):
  the framework integrations' decorator and module APIs (`@template`, `render`, `response`,
  their async forms, `ErrorPage`'s template) refuse template names that could reach outside
  the template folder (absolute paths, `..`, backslashes, drive letters, `package:path` specs,
  NUL, surrounding whitespace) with the `ValueError("Template not found: ...")` a missing
  template raises. Off by default, as in the original libraries.
- **`CHAMELEON_CONCURRENT_LIMIT`** (default 64): one asynchronous render starts at most this
  many tasks for `@concurrent` calls and concurrent partials; further calls are awaited in
  place, so a `tal:repeat` over request-sized data can't fan out without bound.

### Fixed

- **A `tal:on-error` inside a `metal:fill-slot` kept text Chameleon drops**, when the
  macro's slot sits inside an `i18n:translate` or `i18n:name`. Chameleon writes a text node
  with its `${...}` expressions (and an interpolated attribute value) as one string, so when
  an expression fails, the text before it in that node was never written. chameleon-rs wrote
  it first: `<i>a<i>E</i>` where Chameleon gives `<i><i>E</i>`. The last of the slot-body
  known differences of this kind; the render fuzzer now generates handlers in such bodies.
- **A cancellation during an async render's cleanup was swallowed.** When a `@concurrent` call
  failed, the render cancelled the others and waited for their `finally` blocks; a
  `CancelledError` arriving then (a client disconnect, `asyncio.timeout`, `wait_for`) was
  replaced by the template's error, so a disconnect became a logged 500 and a timeout never
  fired. Cancellation now wins, with the template error as its context.
- **A failed `@concurrent` call no longer waits for slower calls before it in the page**: the
  render ends as soon as one fails, as a `TaskGroup` does.
- **A `StopIteration` raised in an async render** (`next()` on an exhausted iterator) made
  `await render_async()` return the exception's arguments as if they were the page. It is now
  a `RuntimeError`, as in any coroutine (PEP 479).
- **Rendering error messages could fail and lose the original message**: the source line was
  read with the locale's encoding, so a template file that isn't UTF-8 (or a non-UTF-8 locale)
  made `str()` of the error raise. The file is now read as UTF-8 with replacement characters,
  and a failure while formatting falls back to the original message.
- **A macro with a `<?python ?>` block called in a long `tal:repeat` was quadratic**: each
  call's namespace was kept until the end of the render and searched on every lookup (128k
  items took 1.3 s and 62 MB at 64k). Namespaces are dropped when their frame returns: 0.85
  µs per item at any size.
- Cancelled `@concurrent` calls and concurrent partials' arguments no longer leave "coroutine
  was never awaited" warnings.
- **The Litestar integration made a reference cycle per render** (the template callables were
  bound to the dict that held them), so each request and its model lived until the next
  garbage collection. The callables now get a copy of the context, as Litestar's Mako engine
  does.
- **Each distinct name a template used stayed in memory for the life of the process**:
  interned immortally, by PyO3 and by CPython's parser. Bare names and dotted paths
  (`${title}`, `${user.name}`) now compile without the parser, which is also faster, and names
  are interned with `sys.intern`, so they are freed with the last template using them on
  CPython 3.13 and later with the GIL (3.12 and free-threaded builds intern only immortally).
  This matters only for templates generated at runtime.
- **No more `PanicException`s from a dict changed while chameleon-rs reads it** (a key whose
  `__hash__` changes the scope, or another thread on a free-threaded build): the scope's copy,
  the defaults and the partials' error context read a snapshot, and the macro, slot, async
  step, tokenizer and element parser entry points turn any panic into `InternalError`. A
  `PanicException` is a `BaseException`, so `except Exception` handlers missed it.
- A render's saved error is visible to the garbage collector while its cancelled tasks are
  joined; a second awaiter of a suspended render gets `RuntimeError("coroutine is being
  awaited already")`; an error looking up a delegate's `throw` is raised, as in CPython.
- **Templates found next to a caller (sibling partials) were kept for the life of the
  process,** with the settings in their cache key (a `translate` function per template, say),
  and one more for each file a deployment replaced. They now live as long as a template that
  uses them, and the cache holds settings by value only when they are plain data (strings,
  numbers), so a setting or a template subclass that refers back to its template doesn't
  keep it either. A setting that can't be hashed (a dict such as `extra_builtins`) still
  isn't shared, so a sibling never keeps contents the dict has since changed.
- **A `@concurrent` call's own cancellation waited for every other call.** A helper raising
  `CancelledError` (or awaiting a cancelled future) ended the render only once the others
  finished, never if one hung. It now ends it at once, as a failure does.
- **Two names of one file (a hard link) stayed together after a deployment replaced one.**
  `b.pt` loaded as `a.pt`, the first name seen of its file, and rendered `a.pt`'s new content
  after `a.pt` was replaced. A remembered name is now checked to still name that file, in
  the integrations, the Jinja-shaped engines and the partials folder (with `auto_reload`).
- The README said async rendering never blocks the event loop; it never blocks it on I/O. A
  page that is all CPU work holds the loop while it renders (see "Offloading a render to a
  thread").

### Changed

- The Jinja-shaped engines' `get_template()` (Starlette, Litestar, Robyn) remembers the
  names it found when templates don't reload, as the decorator API does: a name seen before
  costs no `stat` (0.5 µs, from 2.3 µs).

## [0.1.50] - 2026-10-22

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
