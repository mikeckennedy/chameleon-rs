<p align="center">
  <img src="https://raw.githubusercontent.com/mikeckennedy/chameleon-rust/main/assets/chameleon-rs-slogan.png" alt="chameleon-rs: Chameleon templates, Rust speed" width="850">
</p>

# chameleon-rs

A Rust implementation of the [Chameleon](https://github.com/malthe/chameleon) page template
engine for Python, aiming to be a drop-in replacement: same template syntax, same output,
same API, much faster rendering.

Status: early development. See [docs/plan.md](docs/plan.md) for the build plan and
[docs/architecture.md](docs/architecture.md) for the design decisions.

Switching an application over is one line at startup, before anything imports Chameleon:

```python
import chameleon_rs.compat

chameleon_rs.compat.install()  # `import chameleon`, `chameleon_partials`, `chameleon_flask`, `fastapi_chameleon` give chameleon-rs
```

Built in, with no extra package:

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
  decorated endpoints' templates also get `request` and `url_for`.

```python
from chameleon_rs.starlette import Templates

templates = Templates(directory='templates')   # or Templates() after chameleon_rs.configure(...)

async def episode(request):
    episode = load_episode(request.path_params['number'])   # awaitable values are awaited in the template
    return await templates.TemplateResponse_async(request, 'episodes/detail.pt', {'episode': episode})

app = Starlette(routes=[Route('/episodes/{number}', episode, name='episode')])
```

**Decorator order.** `@template` goes *below* the route decorator (`@app.get`, `@router.get`,
`@app.route`, `@blueprint.get`). Written above it, the router registers the bare view, the
template never renders and the route quietly returns JSON. An app given to `configure(...,
app=app)` (or `global_init(..., app=app)`) checks this when it starts (FastAPI and Quart: at
startup; Flask, which has no startup hook: before its first request) and raises
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
- `chameleon_rs.flask`, `chameleon_rs.fastapi` and `chameleon_rs.starlette`: async views
  (Flask's async views, Quart, FastAPI, Starlette) render with `render_async`; `render_async()`
  and `response_async()` are the coroutine versions of `render()` and `response()`, and
  Starlette's `Templates.TemplateResponse_async()` of `TemplateResponse()`. This also applies through `compat.install()`, and it is a
  difference from chameleon-flask: a model value that is awaitable (a coroutine, task or
  future, or a test double such as `AsyncMock()`) is awaited where the template uses it, where
  chameleon-flask and fastapi-chameleon would have rendered the object itself.
- A subclass that overrides `render()` keeps its override: `render_async()` calls it
  (synchronously, so awaitable values are not awaited).

Opt-in concurrency (it changes the order in which helpers run):

- `@chameleon_rs.concurrent` on an async helper: its calls written as `${...}` are awaited
  concurrently with the rest of the page, each result filled in at the end.
- `PageTemplate(..., concurrent=True)` (or `@template(..., concurrent=True)` in Flask, FastAPI and Starlette):
  `render_partial(...)` calls of the page whose arguments are awaitable render concurrently.

Experimental: `render_async(..., offload=True)` (or `@template(..., offload=True)`) renders in a
worker thread. On free-threaded Python (3.14t) renders then run in parallel with the event loop;
on a GIL build it helps only pages dominated by long text. Measurements: `docs/plan.md`, Phase 11.

## Development

```sh
make dev     # set up venv with the reference Chameleon and build the extension
make test    # Rust tests, Python tests, Chameleon's own suite on the reference engine
```

Licensed under Chameleon's license (see `LICENSE.txt` and `COPYRIGHT.txt`); this project
ports Chameleon's behavior and reuses its test suite.
