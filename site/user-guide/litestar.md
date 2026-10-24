# Litestar

`chameleon_rs.litestar` renders templates from Litestar route handlers, two ways:

- [ChameleonEngine](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine), Litestar's own template engine interface (`TemplateEngineProtocol`), for `TemplateConfig(engine=...)` and `Template(...)` responses.
- The decorator API of the [Flask](flask.md), [FastAPI](fastapi.md), [Starlette](starlette.md) and [Robyn](robyn.md) modules: a handler returns its model and `@template` renders it, with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) for an `async def` handler.

`import chameleon_rs` doesn't import Litestar. Only `import chameleon_rs.litestar` does, and it uses the Litestar your app already has (2.x; tested with 2.25).


# ChameleonEngine: Litestar's templating


``` python
from litestar import Litestar, Request, get
from litestar.template import TemplateConfig
from litestar.testing import TestClient

from chameleon_rs.litestar import ChameleonEngine, Template


@get('/')
async def index() -> Template:
    return Template('home/index.pt', context={'title': 'Hello', 'videos': ['intro', 'async']})


@get('/videos/{slug:str}', name='video')
async def video(slug: str) -> Template:
    return Template('home/index.pt', context={'title': slug, 'videos': []})


app = Litestar(
    [index, video],
    template_config=TemplateConfig(engine=ChameleonEngine, directory=template_folder),
)

with TestClient(app) as client:
    response = client.get('/')
    print(response.headers['content-type'])
    print(response.text)
```


    INFO - 2026-10-23 23:34:10,889 - httpx - _client - HTTP Request: GET http://testserver.local/ "HTTP/1.1 200 OK"


    text/html; charset=utf-8
    <h1>Hello</h1>
    <ul><li><b>intro</b></li>
    <li><b>async</b></li></ul>
    [intro](/videos/intro)


`ChameleonEngine(directory=None, engine_instance=None, *, auto_reload=None, restricted_namespace=None)` implements Litestar's `TemplateEngineProtocol`, as its Jinja, Mako and MiniJinja engines do:

- `directory` is a folder or a list of folders, searched in order. `engine_instance` (or `ChameleonEngine.from_loader(loader)`) uses a [chameleon_rs.PageTemplateLoader](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) of your own. With neither, the engine uses the process-wide configuration, looked up at each render: `TemplateConfig(engine=ChameleonEngine())` after `chameleon_rs.configure(template_folder=...)`. To set the loader options, pass an instance: `TemplateConfig(engine=ChameleonEngine('templates', auto_reload=dev_mode))`.
- A template gets Litestar's context: what you pass in `context`, plus `request` and `csrf_input` (a hidden form field; write `${structure: csrf_input}`).
- It also gets the template callables: `url_for(route_name, **path_params)`, `csrf_token()`, `url_for_static_asset(name, file_path)`, and any you register with `engine.register_template_callable(key, fn)` (in `TemplateConfig(engine_callback=...)`, as for Jinja). A callable receives the template's context first, so it is written `fn(context, ...)` and called `${fn(...)}`. A context key of the same name wins, as over Jinja's globals.
- `Template(template_str='<p>${x}</p>', context=...)` works too ([render_string](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine.render_string)); compiled strings are cached. They compile with the loader's template options (`translate`, `restricted_namespace`, …), as [get_template](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates.get_template)'s templates do.
- A missing template raises Litestar's `TemplateNotFoundException` (a 500), as for Jinja; the loader's own lookup decides (its `default_extension` applies). So does a name that could reach outside the folder (absolute, a `..` segment, a backslash, a drive letter or a `package:` spec) or that has surrounding whitespace (Chameleon's loader would strip it; Jinja looks for that exact file), as Litestar's Jinja engine refuses it.
- `engine.get_template(name)` returns a [ChameleonTemplate](../reference/robyn.ChameleonTemplate.md#chameleon_rs.robyn.ChameleonTemplate); its `render(**context)` is what Litestar calls, and `await render_async(**context)` is there for code rendering a template itself.

A test compares it with Litestar's Jinja engine request by request (status, headers, cookies, background tasks, the callables, CSRF, errors), and its HTML with litestar-chameleon's on stock Chameleon.


## `.pt` and the media type

Litestar picks a [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) response's media type from the template name's suffix, and Python's `mimetypes` doesn't know `.pt`: with `litestar.response.Template`, a `.pt` page is served as `text/plain`, and the browser shows its source. [chameleon_rs.litestar.Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) is a subclass that serves a name Litestar can't guess as `text/html`:


``` python
from litestar.response import Template as LitestarTemplate


@get('/stock')
async def stock() -> LitestarTemplate:
    return LitestarTemplate('home/index.pt', context={'title': 'Stock', 'videos': []})


app = Litestar(
    [index, stock, video],
    template_config=TemplateConfig(engine=ChameleonEngine, directory=template_folder),
)
with TestClient(app) as client:
    print('ours: ', client.get('/').headers['content-type'])
    print('stock:', client.get('/stock').headers['content-type'])
```


    INFO - 2026-10-23 23:34:10,898 - httpx - _client - HTTP Request: GET http://testserver.local/ "HTTP/1.1 200 OK"
    INFO - 2026-10-23 23:34:10,899 - httpx - _client - HTTP Request: GET http://testserver.local/stock "HTTP/1.1 200 OK"


    ours:  text/html; charset=utf-8
    stock: text/plain; charset=utf-8


Names Litestar can guess keep their type (`.html`, `.xml`, `.txt`), and a `media_type` you give, on the [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) or on the route (`@get(..., media_type=...)`), wins. Litestar's `HTMXTemplate` and other [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) subclasses need `media_type='text/html'` for `.pt` names. (Registering the type globally, `mimetypes.add_type('text/html', '.pt')` at startup, also works, for every library in the process.)

The response class matters, not only the template: a handler annotated `-> Any` or `-> dict` that returns a [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) is served with `Content-Type: application/json` (the route's media type wins; the same with stock Litestar): annotate it `-> Template`.


## Synchronous rendering

Litestar's template protocol is synchronous: its [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) response calls the engine's [render()](../reference/flask.render.md#chameleon_rs.flask.render) while it builds the response, on the event loop. [ChameleonEngine](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine) can't change that, so a [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) response never awaits anything and a slow page blocks the loop while it renders. For an `async def` handler that should render without blocking, use the decorator API below, which renders with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async).


# The decorator API


``` python
import asyncio

import chameleon_rs.litestar as chameleon_litestar

chameleon_litestar.configure(template_folder=template_folder)


async def latest_videos():
    await asyncio.sleep(0.01)   # a database query, say
    return ['intro', 'partials']


@get('/latest')
@chameleon_litestar.template('home/index.pt')
async def latest() -> dict:
    # Unawaited: the template awaits it where it uses it.
    return {'title': 'Latest', 'videos': latest_videos()}


@get('/v/{name:str}')
@chameleon_litestar.template('home/index.pt')
async def listing(name: str, request: Request) -> dict:
    if name == 'missing':
        chameleon_litestar.not_found(path=request.url.path)   # renders errors/404.pt
    return {'title': name, 'videos': [name]}


app = Litestar([latest, listing, video])
chameleon_litestar.configure(app=app)   # the decorator-order check at startup

with TestClient(app) as client:
    print(client.get('/latest').text)
    print(client.get('/v/missing').status_code)
```


    INFO - 2026-10-23 23:34:10,919 - httpx - _client - HTTP Request: GET http://testserver.local/latest "HTTP/1.1 200 OK"
    INFO - 2026-10-23 23:34:10,920 - httpx - _client - HTTP Request: GET http://testserver.local/v/missing "HTTP/1.1 404 Not Found"


    <h1>Latest</h1>
    <ul><li><b>intro</b></li>
    <li><b>partials</b></li></ul>
    [intro](/videos/intro)
    404


It is the API of the other framework modules:

- `configure(template_folder=..., auto_reload=..., restricted_namespace=..., app=None, check_order=True)`, the same call as [chameleon_rs.configure()](../reference/configure.md#chameleon_rs.configure); `global_init(folder, auto_reload, cache_init)` configures the handlers only.
- `@template`, `@template()`, `@template('page.pt', content_type=..., status_code=...)` (`mimetype=` works too). Bare, it renders `{module}/{function}.html`, or `.pt`. The handler returns a dict, a mapping, a dataclass or a Pydantic model; a `litestar.Response` (a `Redirect`, a [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template), …) passes through (an `ASGIResponse` or another ASGI app does not: it is an invalid return). Without `status_code`, the page has the route's status code, as any Litestar response without one: 200, 201 for `@post`, or `@get(..., status_code=...)`. So a `@post` handler that re-renders a form with errors answers 201 unless you say otherwise: give it `@template('form.pt', status_code=200)` (or 400). A `@delete` route defaults to 204, which has no body, and Litestar refuses to start with a handler that returns one: use `@delete(..., status_code=200)`.
- The template gets what a Litestar template gets: `request`, `csrf_input`, `url_for`, `csrf_token` and `url_for_static_asset` (the callables of the app's [ChameleonEngine](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine) if it has one). Error pages get them too. Two small differences from Litestar's own [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) response: the model's keys override `request` and `csrf_input` (a [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) response sets those over its context), and the callables are bound to `{request, csrf_input}`, not to the whole model.
- Functions and `Controller` methods, sync or async. An `async def` handler renders with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) on the event loop: awaitable values are awaited where the template uses them. A sync handler renders with [render()](../reference/flask.render.md#chameleon_rs.flask.render) where Litestar runs it: in a worker thread with `@get(..., sync_to_thread=True)`, else on the event loop. `concurrent=True` renders `render_partial(...)` calls with awaitable arguments concurrently; `offload=True` (experimental) renders in a worker thread.
- `not_found(template_file='errors/404.pt', **data)`, `error(template_file, status_code=500, **data)` and `generic_error(template_file, status_code, template_data)` end a decorated handler with an error page.
- [render()](../reference/flask.render.md#chameleon_rs.flask.render), [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async), [response()](../reference/flask.response.md#chameleon_rs.flask.response), [response_async()](../reference/robyn.response_async.md#chameleon_rs.robyn.response_async) (a `litestar.Response`), [clear()](../reference/flask.clear.md#chameleon_rs.flask.clear), `check_views(app)`.


## The handler's signature

Litestar builds dependency injection, validation and the OpenAPI schema from the handler's signature and its return annotation. `@template` keeps the parameters (path and query parameters, `FromPath`/`FromQuery`, dependencies, `data`), with two changes Litestar reads:

- The return annotation becomes `litestar.Response`. A handler annotated `-> dict` would otherwise be a JSON route: Litestar would pick `application/json` and could apply a return DTO.
- A keyword-only `request: Request` parameter is added when the handler has none, so the template can get `request` and `url_for`. The wrapper takes it out before calling your handler; `request` is one of Litestar's reserved names, so it is not in the OpenAPI schema.

Annotations are not evaluated when `@template` runs (a forward reference to a class defined later in the module resolves when Litestar registers the route). The OpenAPI schema describes the response as `application/json`, as for any handler returning `Response`; pass `@get(..., media_type=MediaType.HTML)` for an accurate schema.

Errors are [chameleon_rs.web.TemplateError](../reference/web.TemplateError.md#chameleon_rs.web.TemplateError), [NotFound](../reference/web.NotFound.md#chameleon_rs.web.NotFound), [ErrorPage](../reference/web.ErrorPage.md#chameleon_rs.web.ErrorPage) and [DecoratorOrderError](../reference/web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError), also importable from `chameleon_rs.litestar` (a missing template here is Chameleon's `ValueError`, as in the other integrations; [ChameleonEngine](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine) raises Litestar's `TemplateNotFoundException`).

> **Warning: Template names come from your code**
>
> The names given to `@template`, [render()](../reference/flask.render.md#chameleon_rs.flask.render), [response()](../reference/flask.response.md#chameleon_rs.flask.response), [not_found()](../reference/flask.not_found.md#chameleon_rs.flask.not_found) / [error()](../reference/flask.error.md#chameleon_rs.flask.error) and [render_partial()](../reference/render_partial.md#chameleon_rs.render_partial) are looked up as the original libraries look them up, which allows `..` and absolute paths. Write them in your code; never build one from a request (a path or query parameter, a header). [ChameleonEngine](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine) refuses names that reach outside its folder, as Litestar's Jinja engine does.


# Decorator order

`@get` (or `@post`, `@route`, …) comes first, outermost, and `@template` under it. Litestar's route decorators return a route handler object, not a function, so written the other way round `@template` is handed that object; it raises [DecoratorOrderError](../reference/web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError) at once, when the module is imported, naming the function and the fix.

A route can still call the bare function another way: `get('/raw')(view)` and, separately, `template('page.pt')(view)`. An app given to `configure(app=app)` checks its routes (with routers and controllers) when it starts, in an `on_startup` hook (Uvicorn and Granian report a failed startup), or before its first request when no lifespan runs, and raises [DecoratorOrderError](../reference/web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError) naming the function, its file and line, and its route. `check_views(app)` (an app or a `Router`) runs the check on demand. `check_order=False` and `check_views(app, ignore=[...])` are the escape hatch, as for [FastAPI](fastapi.md#decorator-order). The check runs first among the startup hooks, so routes your own `on_startup` hook registers (`app.register(...)`) are not checked: call `check_views(app)` after registering them.


# litestar-chameleon

[litestar-chameleon](https://github.com/uyar/litestar-chameleon) (H. Turgut Uyar) is a third-party Chameleon engine for Litestar. It runs unchanged on chameleon-rs through [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install) (its test suite is part of `make test-integration`), with the same `.pt` media type caveat. [ChameleonEngine](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine) is the built-in alternative: it adds `url_for_static_asset`, the folder guard, [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) for `.pt`, the process-wide configuration and the decorator API, and it doesn't need stock Chameleon installed.
