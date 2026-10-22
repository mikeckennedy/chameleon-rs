<p align="center">
  <img src="https://raw.githubusercontent.com/mikeckennedy/chameleon-rust/main/assets/chameleon-rs-slogan.png" alt="chameleon-rs: Chameleon templates, Rust speed" width="850">
</p>

# chameleon-rs

A Rust implementation of the [Chameleon](https://github.com/malthe/chameleon) page template
engine for Python, aiming to be a drop-in replacement: same template syntax, same output,
same API, much faster rendering.

**Documentation: <https://mkennedy.codes/docs/chameleon-rs/>** ·
[Changelog](https://github.com/mikeckennedy/chameleon-rust/blob/main/CHANGELOG.md) ·
[Compatibility](https://mkennedy.codes/docs/chameleon-rs/user-guide/compatibility.html) ·
[Performance](https://mkennedy.codes/docs/chameleon-rs/user-guide/performance.html)

## Install

```sh
pip install chameleon-rs        # or: uv pip install chameleon-rs
```

CPython 3.12 to 3.15, including the free-threaded builds (3.14t, 3.15t). Wheels for Linux
(glibc and musl), macOS and Windows contain the compiled engine, so no Rust toolchain is
needed. No runtime dependencies: stock Chameleon need not be installed.

## Quick start

```python
from chameleon_rs import PageTemplate, PageTemplateLoader

page = PageTemplate('<p tal:repeat="name names">Hello, ${name}!</p>')
print(page(names=['World', 'Rust']))

templates = PageTemplateLoader(['templates'], auto_reload=False)
html = templates['home/index.pt'](title='Home')   # templates/home/index.pt
```

## Why

- **Compatible.** Chameleon's own test suite, vendored unmodified, passes: 227 of the 227
  tests that pass on Chameleon. All 122 of its fixture renders are byte-identical, and every
  page of two production Flask/Quart sites renders byte-identical HTML.
- **Fast.** In synthetic benchmarks (release build, same process, interleaved with
  Chameleon): listings 6.6x faster per item, macro calls 6.9x, an app-shaped page 4.3x,
  native partials 10.9x, and templates compile about 70x faster. On a production Quart site,
  template rendering was 2.2x faster in total and whole requests 1.3x faster; under load,
  throughput rose 34%. The released wheels are profile-guided, about 10% faster again.
  [How it's measured](https://mkennedy.codes/docs/chameleon-rs/user-guide/performance.html).
- **Batteries included.** Partials, Markdown, async rendering and the Flask/Quart, FastAPI,
  Starlette, Robyn and Litestar integrations are built in (below).

## Switch an existing app

Switching an application over is one line at startup, before anything imports Chameleon:

```python
import chameleon_rs.compat

chameleon_rs.compat.install()  # `import chameleon`, `chameleon_partials`, `chameleon_flask`, `fastapi_chameleon`, `chameleon_robyn` give chameleon-rs
```

## Built in

With no extra package:

- **Partials:** every template can call `${render_partial('shared/card.pt', item=item)}`; the
  partial renders straight into the page. `chameleon_rs.configure(template_folder='templates')`
  says where templates live (partials not found there are also looked for on the calling
  template's search path and next to it; `partial_search='registered'` turns that off). `chameleon_partials`' API (`register_extensions`,
  `extend_model`, `render_partial`) keeps working. See `chameleon_rs.partials`.
- **Flask and Quart:** `chameleon_rs.flask` has chameleon-flask's API (`configure` /
  `global_init`, `@template`, `render`, `response`, `not_found`, `error`, `clear`) and imports
  Flask only when it is imported itself. Views may return a dict, any mapping, a dataclass or
  a Pydantic model.
- **FastAPI:** `chameleon_rs.fastapi` has fastapi-chameleon's API (`global_init`,
  `@template`, `render`, `response`, `not_found`, `generic_error`, `clear`, its exception
  names) plus `configure`, `error`, `render_async` and `response_async`, and imports FastAPI
  only when it is imported itself. There are no extras to install: the framework you already
  have is the one used.

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
OpenAPI docs work as without `@template`. An `async def` view renders with `render_async`
(awaitable values in the model are awaited where the template uses them, without blocking the
event loop); a sync view renders in FastAPI's thread pool, as before.

- **Starlette:** `chameleon_rs.starlette.Templates` is a drop-in for Starlette's
  `Jinja2Templates`: `TemplateResponse(request, name, context, status_code, headers,
  media_type, background)` adds `request` to the context, runs the context processors, gives
  the template `url_for(...)` and sends the debug event the test client reads
  (`response.template`, `response.context`). `await templates.TemplateResponse_async(...)`
  takes the same arguments and renders with `render_async`, so the event loop is never
  blocked. The same module has the decorator API of the Flask and FastAPI modules
  (`configure`, `@template`, `render`, `response`, `not_found`, `error`, `check_views`, ...);
  decorated endpoints' templates also get `request` and `url_for`. For a missing template `Templates`
  raises `chameleon_rs.web.TemplateNotFound` (a `ValueError`), the counterpart of
  `jinja2.TemplateNotFound` (everything else raises Chameleon's plain `ValueError`, as before); `templates.globals` is Jinja's `env.globals`.

```python
from chameleon_rs.starlette import Templates

templates = Templates(directory='templates')   # or Templates() after chameleon_rs.configure(...)

async def episode(request):
    episode = load_episode(request.path_params['number'])   # awaitable values are awaited in the template
    return await templates.TemplateResponse_async(request, 'episodes/detail.pt', {'episode': episode})

app = Starlette(routes=[Route('/episodes/{number}', episode, name='episode')])
```

- **Robyn:** `chameleon_rs.robyn` has chameleon-robyn's API (`global_init`, `@template`,
  `render`, `response`, `not_found`, `clear`, `ChameleonTemplate` for Robyn's template
  interface, the `__response_callback__` hook, its exception names) plus `configure`, `error`,
  `render_async`, `response_async` and `check_views`, and imports Robyn only when it is
  imported itself. An `async def` handler renders with `render_async` on Robyn's event loop; a
  sync handler renders where Robyn runs it, on its worker thread. Robyn has wheels for CPython
  3.10-3.14 only (no 3.15 or free-threaded build yet).

```python
from robyn import Robyn
import chameleon_rs.robyn as chameleon_robyn

app = Robyn(__file__)
chameleon_robyn.configure(template_folder='templates', app=app)

@app.get('/episodes/:number')                      # the route decorator first...
@chameleon_robyn.template('episodes/detail.pt')    # ...then @template
async def detail(number):
    episode = await db.episode(int(number))
    if episode is None:
        chameleon_robyn.not_found()
    return {'episode': episode}
```

- **Litestar:** `chameleon_rs.litestar.ChameleonEngine` is Litestar's own template engine
  interface (`TemplateConfig(engine=ChameleonEngine, directory='templates')`): templates get
  Litestar's context (`request`, `csrf_input`) and template callables (`url_for`,
  `csrf_token`, anything registered with `register_template_callable`), and a missing name, or
  one reaching outside the folder, raises Litestar's `TemplateNotFoundException`. Return
  `chameleon_rs.litestar.Template`, not `litestar.response.Template`: Litestar guesses the
  media type from the file name and serves `.pt` as `text/plain`, this subclass serves it as
  `text/html` (an explicit `media_type` still wins). Litestar renders these templates
  synchronously, on the event loop. The same module has the decorator API of the other
  modules (`configure`, `@template`, `render`, `response`, `not_found`, `error`,
  `check_views`, ...), where an `async def` handler renders with `render_async`. The
  third-party [litestar-chameleon](https://github.com/uyar/litestar-chameleon) also runs
  unchanged on chameleon-rs through `compat.install()`.

```python
from litestar import Litestar, get
from litestar.template import TemplateConfig
import chameleon_rs.litestar as chameleon_litestar
from chameleon_rs.litestar import ChameleonEngine, Template

@get('/')
async def index() -> Template:                           # Litestar's templating...
    return Template('home/index.pt', context={'title': 'Home'})

@get('/episodes/{number:int}')                           # ...or the route decorator first...
@chameleon_litestar.template('episodes/detail.pt')       # ...then @template
async def detail(number: int) -> dict:
    episode = await db.episode(number)
    if episode is None:
        chameleon_litestar.not_found()
    return {'episode': episode}

app = Litestar([index, detail], template_config=TemplateConfig(engine=ChameleonEngine, directory='templates'))
chameleon_litestar.configure(template_folder='templates', app=app)
```

A decorated handler keeps its parameters (dependency injection, validation and OpenAPI work
as without `@template`); its return annotation becomes `litestar.Response`, so Litestar does
not serialise the model as JSON, and its template also gets `request`, `url_for` and the CSRF
helpers. With no `status_code=`, the page has the route's status (201 for `@post`).

**Template names come from your code.** Names given to `@template`, `render()`,
`response()`, the error pages, Robyn's `ChameleonTemplate` and `render_partial()` are looked up
as chameleon-flask, fastapi-chameleon, chameleon-robyn and chameleon_partials look them up (`..` and absolute paths work): never
build one from request data. `chameleon_rs.starlette.Templates`, the Jinja drop-in, and
`chameleon_rs.litestar.ChameleonEngine` refuse names that reach outside their folder, as Jinja
does.

**Decorator order.** `@template` goes *below* the route decorator (`@app.get`, `@router.get`,
`@app.route`, `@blueprint.get`). Written above it, the router registers the bare view, the
template never renders and the route quietly returns JSON. An app given to `configure(...,
app=app)` (or `global_init(..., app=app)`) checks this when it starts (FastAPI and Quart: at
startup; Robyn: in `app.start()` and when its test client is built; Litestar: in an
`on_startup` hook, and `@template` written above `@get` fails at import, since it is handed
Litestar's route handler; Flask, which has no startup hook: before its first request) and
raises
`chameleon_rs.web.DecoratorOrderError` naming the view and the fix; `check_views(app)` runs the
same check on demand. One pattern looks the same as the mistake: a function routed raw on this
app whose `@template` wrapper is used elsewhere (called from another view, or routed on another
app). For that, pass `check_order=False` to `configure`/`global_init` and call
`check_views(app, ignore=[that_function])` in a test.

## Async rendering

`await template.render_async(**kw)` renders like `render()`, except that a value that is
awaitable (a coroutine, task or future) is awaited where the template uses it, without
blocking the event loop:

```python
async def latest_episodes():
    return await db.episodes.find().to_list()

html = await page.render_async(episodes=latest_episodes(), user=user)
```

```html
<li tal:repeat="e episodes">${e.title}</li>
```

- Only an expression's final value is awaited: `${episodes}` and `tal:repeat="e episodes"`
  await, `episodes.count` is an attribute of the coroutine (an error, as in a sync render).
  A condition is tested on the awaited value.
- Macros, slots, `render_partial` partials and `tal:on-error` all work across the wait; an
  error raised by an awaited value is caught by `tal:on-error` like any other; cancelling the
  task cancels the value being awaited.
- With no awaitable values the output is exactly `render()`'s (tested against every Chameleon
  fixture and the render fuzzers).
- Async iterables in `tal:repeat` are not supported (as in a sync render, they are not
  iterable): collect them first, `[x async for x in source]` in an async helper.
- `chameleon_rs.flask`, `chameleon_rs.fastapi`, `chameleon_rs.starlette`,
  `chameleon_rs.robyn` and `chameleon_rs.litestar`: async views (Flask's async views, Quart,
  FastAPI, Starlette, Robyn, Litestar) render with `render_async`; `render_async()`
  and `response_async()` are the coroutine versions of `render()` and `response()`, and
  Starlette's `Templates.TemplateResponse_async()` of `TemplateResponse()`. This also applies through `compat.install()`, and it is a
  difference from chameleon-flask: a model value that is awaitable (a coroutine, task or
  future, or a test double such as `AsyncMock()`) is awaited where the template uses it, where
  chameleon-flask, fastapi-chameleon and chameleon-robyn would have rendered the object itself.
- A subclass that overrides `render()` keeps its override: `render_async()` calls it
  (synchronously, so awaitable values are not awaited).

Opt-in concurrency (it changes the order in which helpers run):

- `@chameleon_rs.concurrent` on an async helper: its calls written as `${...}` are awaited
  concurrently with the rest of the page, each result filled in at the end.
- `PageTemplate(..., concurrent=True)` (or `@template(..., concurrent=True)` in Flask, FastAPI, Starlette, Robyn and Litestar):
  `render_partial(...)` calls of the page whose arguments are awaitable render concurrently.

Experimental: `render_async(..., offload=True)` (or `@template(..., offload=True)`) renders in a
worker thread. On free-threaded Python (3.14t) renders then run in parallel with the event loop;
on a GIL build it helps only pages dominated by long text. Measurements:
[docs/plan.md](https://github.com/mikeckennedy/chameleon-rust/blob/main/docs/plan.md), Phase 11.

## Development

```sh
make dev     # set up venv with the reference Chameleon and build the extension
make test    # Rust tests, Python tests, Chameleon's own suite on the reference engine
```

Licensed under Chameleon's license (see `LICENSE.txt` and `COPYRIGHT.txt`); this project
ports Chameleon's behavior and reuses its test suite.
