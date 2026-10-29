<p align="center">
  <img src="https://mkennedy-shared.nyc3.digitaloceanspaces.com/github/chameleon-rs-slogan.png" alt="chameleon-rs: Chameleon templates, Rust speed" width="850">
</p>

# chameleon-rs

**Chameleon templates, modernized. Several times faster.**

chameleon-rs is [Chameleon](https://github.com/malthe/chameleon)'s template language on a new
engine written in Rust. Your existing templates and code run unchanged, with byte-identical
output, several times faster. It also adds what a modern Python web app expects: async
rendering that never blocks the event loop on I/O, built-in integrations for Flask/Quart,
FastAPI, Starlette, Robyn and Litestar, partials, Markdown, and support for free-threaded
Python. No runtime dependencies.

**Documentation: <https://mkennedy.codes/docs/chameleon-rs/>** ·
**Source: <https://github.com/mikeckennedy/chameleon-rs>** ·
[Changelog](https://github.com/mikeckennedy/chameleon-rs/blob/main/CHANGELOG.md) ·
[Compatibility](https://mkennedy.codes/docs/chameleon-rs/user-guide/compatibility.html) ·
[Performance](https://mkennedy.codes/docs/chameleon-rs/user-guide/performance.html)

## Install

```sh
pip install chameleon-rs        # or: uv pip install chameleon-rs
```

CPython 3.12 to 3.15, including the free-threaded builds (3.14t, 3.15t). Wheels for Linux
(glibc and musl), macOS and Windows contain the compiled engine, so no Rust toolchain is
needed. Stock Chameleon need not be installed.

## Already on Chameleon?

Switching an application is one line at startup, before anything imports Chameleon:

```python
import chameleon_rs.compat

chameleon_rs.compat.install()
```

From then on `import chameleon` gives chameleon-rs, and so do `chameleon_partials`,
`chameleon_flask`, `fastapi_chameleon` and `chameleon_robyn`. Your templates and the code
around them stay as they are.

- **Same output.** Chameleon's own test suite, vendored unmodified, passes: 227 of the 227
  tests that pass on Chameleon. All 122 of its fixture renders are byte-identical, and every
  page of two production Flask/Quart sites renders byte-identical HTML.
  [Known differences](https://mkennedy.codes/docs/chameleon-rs/user-guide/compatibility.html).
- **Check your own app first.** Shadow mode (`chameleon_rs.shadow.install()`) renders a sample
  of real traffic with both engines and reports any difference, while Chameleon keeps serving.
  [Switching from Chameleon](https://mkennedy.codes/docs/chameleon-rs/user-guide/switching-from-chameleon.html).

## Starting fresh?

Templates are Chameleon's: HTML with `tal:` attributes and `${...}` expressions, so a template
is still valid HTML that an editor or browser can open.

```python
from chameleon_rs import PageTemplate

page = PageTemplate('<p tal:repeat="name names">Hello, ${name}!</p>')
print(page(names=['World', 'Rust']))
```

In a web app, point chameleon-rs at a template folder and decorate your views. With FastAPI:

```python
import fastapi
import chameleon_rs.fastapi as fastapi_chameleon

app = fastapi.FastAPI()
fastapi_chameleon.configure(template_folder='templates', app=app)

@app.get('/episodes/{number}')                      # the route decorator first...
@fastapi_chameleon.template('episodes/detail.pt')   # ...then @template
async def detail(number: int):
    episode = await db.episode(number)
    if episode is None:
        fastapi_chameleon.not_found()
    return {'episode': episode}
```

The view keeps its signature, so path and query parameters, `Depends`, validation and the
OpenAPI docs work as without `@template`.
[Getting started](https://mkennedy.codes/docs/chameleon-rs/user-guide/getting-started.html).

## What's new compared with Chameleon

- **Errors where the mistake is.** Forget the `</div>` after a `tal:condition` and Chameleon
  quietly renders what the condition should have hidden. Forget the `</ul>` after a
  `tal:repeat` and you get a `NameError` lines away from the real cause. With
  `strict_tags=True` (or `CHAMELEON_STRICT_TAGS=1`) an unclosed element carrying `tal:`,
  `metal:` or `i18n:` attributes is a `ParseError` that points at that tag and names the
  missing end tag. Opt-in, so the default stays Chameleon's.
  [Catching unclosed tags](https://mkennedy.codes/docs/chameleon-rs/user-guide/strict-tags.html).
- **Async rendering.** `await template.render_async(**model)` awaits awaitable model values
  where the template uses them, without blocking the event loop. Macros, slots, partials and
  `tal:on-error` work across the wait, and with no awaitables the output is exactly
  `render()`'s. Opt-in concurrency: `@chameleon_rs.concurrent` helpers and
  `concurrent=True` partials run at the same time as the rest of the page.
  [Async rendering](https://mkennedy.codes/docs/chameleon-rs/user-guide/async.html).
- **Framework integrations.** Built in, with nothing extra to install (table below). Pyramid's
  pyramid_chameleon and Zope/Plone's z3c.pt and `Products.PageTemplates` run unchanged through
  `compat.install()`. Async
  views render with `render_async`, and a `@template` written above its route decorator raises
  an error naming the fix instead of quietly returning JSON.
- **Partials.** Any template can call `${render_partial('shared/card.pt', item=item)}`; the
  partial renders natively inside the page, 10.9x faster than chameleon_partials.
  [Partials](https://mkennedy.codes/docs/chameleon-rs/user-guide/partials.html).
- **HTMX.** Because partials are built in, HTMX works natively: the page includes a fragment
  with `render_partial`, and the HTMX view returns the same template on its own.
  [HTMX](https://mkennedy.codes/docs/chameleon-rs/user-guide/htmx.html).
- **Markdown.** `${markdown: episode.notes}` converts Markdown to HTML while the page renders:
  CommonMark plus the GitHub extensions (tables, task lists, footnotes, ...), converted in
  Rust, with raw HTML escaped by default. It's about 26x faster than mistune, a pure-Python
  converter, on a 5 KB document, and repeat conversions come from a cache in well under a
  microsecond. Opt-in with `chameleon_rs.configure(markdown=True)`;
  named profiles (`md-comment:`) give different settings to different content.
  [Markdown](https://mkennedy.codes/docs/chameleon-rs/user-guide/markdown.html).
- **One place for settings.** `chameleon_rs.configure(...)` sets the template folder,
  `auto_reload`, partial lookup and Markdown for the whole process, thread-safely.
  [Configuration](https://mkennedy.codes/docs/chameleon-rs/user-guide/configuration.html).
- **Free-threaded Python.** Wheels for 3.14t and 3.15t that keep the GIL off.
  `render_async(..., offload=True)` (experimental) renders in a worker thread, in parallel
  with the event loop on a free-threaded build.
- **Long-standing Chameleon issues fixed.** `pathlib.Path` works wherever a path string does
  ([#342](https://github.com/malthe/chameleon/issues/342)); Zope's
  `repeat.item.first('category')` and `last('category')` group a sorted sequence
  ([#144](https://github.com/malthe/chameleon/issues/144));
  `<input disabled tal:attributes="disabled True">` renders `disabled="disabled"`, not
  `disableddisabled` ([#429](https://github.com/malthe/chameleon/issues/429));
  `repeat.item` stays right in a recursive macro
  ([#177](https://github.com/malthe/chameleon/issues/177)); a macro call no longer resets
  a local that shadows a global ([#445](https://github.com/malthe/chameleon/issues/445)); and
  errors inside `metal:fill-slot` point at the failing expression in the fill, not the page's
  `metal:use-macro` ([#447](https://github.com/malthe/chameleon/issues/447),
  [#291](https://github.com/malthe/chameleon/issues/291),
  [#259](https://github.com/malthe/chameleon/issues/259)).
- **Typed.** PEP 561 type information for the whole API, including the native module.
- **No runtime dependencies.** The engine, the Markdown converter and the integrations are in
  the wheel; each integration imports its framework only when you import it.

## Frameworks

| Framework | Module | Replaces | Docs |
|---|---|---|---|
| Flask, Quart | `chameleon_rs.flask` | chameleon-flask | [Flask and Quart](https://mkennedy.codes/docs/chameleon-rs/user-guide/flask.html) |
| FastAPI | `chameleon_rs.fastapi` | fastapi-chameleon | [FastAPI](https://mkennedy.codes/docs/chameleon-rs/user-guide/fastapi.html) |
| Starlette | `chameleon_rs.starlette` | Starlette's `Jinja2Templates` (`Templates` is a drop-in) | [Starlette](https://mkennedy.codes/docs/chameleon-rs/user-guide/starlette.html) |
| Robyn | `chameleon_rs.robyn` | chameleon-robyn | [Robyn](https://mkennedy.codes/docs/chameleon-rs/user-guide/robyn.html) |
| Litestar | `chameleon_rs.litestar` | Litestar's `TemplateConfig` engine (`ChameleonEngine`); litestar-chameleon | [Litestar](https://mkennedy.codes/docs/chameleon-rs/user-guide/litestar.html) |
| Pyramid | pyramid_chameleon, unchanged, through `compat.install()` | — | [Pyramid](https://mkennedy.codes/docs/chameleon-rs/user-guide/pyramid.html) |

[Web frameworks](https://mkennedy.codes/docs/chameleon-rs/user-guide/frameworks.html) shows the
same page in each one. Each module keeps the API of the library it replaces and adds the same
decorator API (`configure`, `@template`, `render`, `response`, `not_found`, `error`,
`render_async`, `response_async`, `check_views`).

**Template names come from your code.** The decorator modules and `render_partial()` look names
up as the original libraries do (`..` and absolute paths work), so never build one from
request data. Starlette's `Templates` and Litestar's `ChameleonEngine` refuse names that reach
outside their folder, as Jinja does. `configure(strict_names=True)` (or
`CHAMELEON_STRICT_NAMES=1`) makes the decorator modules refuse them too, as "not found";
off by default, as in the original libraries. Either way, every spelling of one file
(`./a.pt`, `a//a.pt`, an absolute path) shares one cached template.

## Performance

![chameleon-rs speedup over Chameleon: native partials 10.9x, macro calls 6.9x, listings 6.6x, app-shaped page 4.3x, template compilation about 70x; on a production Quart site, template rendering 2.2x, throughput 1.34x, whole requests 1.3x](https://mkennedy-shared.nyc3.digitaloceanspaces.com/github/chamelon-rs-perf-graph-performance.svg)

In synthetic benchmarks (release build, same process, interleaved with Chameleon): listings
6.6x faster per item, macro calls 6.9x, an app-shaped page 4.3x, native partials 10.9x, and
templates compile about 70x faster. On a production Quart site, template rendering was 2.2x
faster in total and whole requests 1.3x faster; under load, throughput rose 34%. The released
wheels are profile-guided, about 10% faster again.
[How it's measured](https://mkennedy.codes/docs/chameleon-rs/user-guide/performance.html).

## Development

```sh
make dev     # set up venv with the reference Chameleon and build the extension
make test    # Rust tests, Python tests, Chameleon's own suite on both engines
```

Licensed under Chameleon's license (see `LICENSE.txt` and `COPYRIGHT.txt`); this project
ports Chameleon's behavior and reuses its test suite.
