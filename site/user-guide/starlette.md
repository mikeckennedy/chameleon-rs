# Starlette

`chameleon_rs.starlette` renders templates from Starlette endpoints, two ways:

- [Templates](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates), a drop-in for Starlette's `Jinja2Templates`, with an async [TemplateResponse_async](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse_async) that never blocks the event loop.
- The decorator API of the [Flask](flask.md) and [FastAPI](fastapi.md) modules: an endpoint returns its model and `@template` renders it.

`import chameleon_rs` doesn't import Starlette. Only `import chameleon_rs.starlette` does, and it uses the Starlette your app already has (1.x, and 0.46 or later).


# Templates: Jinja2Templates for chameleon-rs


``` python
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from chameleon_rs.starlette import Templates


def site(request):
    return {'site': 'Talk Python'}


templates = Templates(directory=template_folder, context_processors=[site])


async def index(request):
    return templates.TemplateResponse(request, 'home/index.pt', {'title': 'Hello', 'videos': ['intro', 'async']})


async def video(request):
    return templates.TemplateResponse(request, 'home/index.pt', {'title': request.path_params['name'], 'videos': []})


app = Starlette(routes=[Route('/', index), Route('/videos/{name}', video, name='video')])

with TestClient(app) as client:
    response = client.get('/')
    print(response.text)
    print(response.template.name, sorted(response.context))
```


    <h1>Hello</h1>
    <ul><li><b>intro</b></li>
    <li><b>async</b></li></ul>
    [Talk Python](http://testserver/videos/intro)
    home/index.pt ['request', 'site', 'title', 'videos']


`TemplateResponse(request, name, context=None, status_code=200, headers=None, media_type=None, background=None)` behaves as Starlette's:

- `request` is added to the context (unless the context has one), then each context processor runs and its dict updates the context.
- The template can call `url_for(name, **path_params)`, which is `request.url_for`.
- `templates.globals` is Jinja's `env.globals`: variables every template gets, below the context (`templates.globals['site_name'] = ...`). `url_for` is one of them, so it can be replaced there.
- The response is an `HTMLResponse`; `status_code`, `headers`, `media_type` and `background` are passed to it.
- It sends Starlette's `http.response.debug` event, so with the test client `response.context` is the context and `response.template` the page template, with Jinja's `name`: Starlette's documented `assert response.template.name == 'index.html'` works. (Other attributes are the page template's: `filename` is its full path.)
- A missing template raises [chameleon_rs.web.TemplateNotFound](../reference/web.TemplateNotFound.md#chameleon_rs.web.TemplateNotFound) (also importable from `chameleon_rs.starlette`): catch it where you caught `jinja2.TemplateNotFound`. It is a `ValueError` and a `LookupError`. Only [Templates](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates) raises it: [PageTemplateLoader](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader), the decorator API and [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) raise Chameleon's plain `ValueError`.
- A name that could reach outside the template folder (absolute, a `..` segment, a backslash, a drive letter or a `package:` spec) is refused with [TemplateNotFound](../reference/web.TemplateNotFound.md#chameleon_rs.web.TemplateNotFound), as Jinja's `FileSystemLoader` refuses it.

A test compares it with `Jinja2Templates` request by request (status, headers, body, context, background tasks), and its HTML with stock Chameleon's.

`directory` can be a list of folders, searched in order, and [Templates](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates) also takes `auto_reload=` and `restricted_namespace=` (the loader's options). Without `directory`, it uses the process-wide configuration, looked up at each render:

``` python
chameleon_rs.configure(template_folder='templates', auto_reload=dev_mode)
templates = Templates()
```

`templates.get_template(name)` returns the page template, and `templates.loader` the loader. There is no `env=` (a Jinja `Environment`): Jinja's environment settings have no chameleon-rs equivalent. `templates.env.globals[...]` becomes `templates.globals[...]`; for the rest, use [chameleon_rs.configure()](../reference/configure.md#chameleon_rs.configure) (Markdown, partial lookup) or the loader options.


## Differences from older Starlette

Starlette 1.x's signature is the one implemented, request first. On Starlette 0.46 and later 0.x, `Jinja2Templates` also accepts the old `TemplateResponse(name, {'request': request})` form with a `DeprecationWarning`; [Templates](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates) raises a `TypeError` saying the request comes first. And 0.x adds `request` to a `context={}` you pass in (1.x, and [Templates](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates), give an empty context a new dict, leaving yours untouched).


# Rendering without blocking the event loop

Jinja's [TemplateResponse](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse) renders on the event loop: while a big page renders, nothing else runs. [TemplateResponse_async](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse_async) takes the same arguments and renders with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async):


``` python
import asyncio


async def latest_videos():
    await asyncio.sleep(0.01)   # a database query, say
    return ['intro', 'partials']


async def home(request):
    # Unawaited: the template awaits it where it uses it.
    return await templates.TemplateResponse_async(
        request, 'home/index.pt', {'title': 'Latest', 'videos': latest_videos()}
    )


app = Starlette(routes=[Route('/', home), Route('/videos/{name}', video, name='video')])
print(TestClient(app).get('/').text)
```


    <h1>Latest</h1>
    <ul><li><b>intro</b></li>
    <li><b>partials</b></li></ul>
    [Talk Python](http://testserver/videos/intro)


A model value that is awaitable (a coroutine, task or future) is awaited where the template uses it. `TemplateResponse_async(..., concurrent=True)` renders `render_partial(...)` calls with awaitable arguments concurrently; `offload=True` (experimental) renders in a worker thread. With no awaitable values the HTML is exactly [TemplateResponse](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse)'s.

[TemplateResponse_async](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse_async) is a coroutine: call it with `await` from an `async def` endpoint. A sync endpoint uses [TemplateResponse](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse) (Starlette runs sync endpoints in its thread pool).

The name keeps Starlette's [TemplateResponse](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse) and adds the `_async` of [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async), so moving an endpoint over is `await templates.TemplateResponse_async(...)` in place of `templates.TemplateResponse(...)`.


# The decorator API


``` python
import chameleon_rs.starlette as chameleon

chameleon.configure(template_folder=template_folder)


@chameleon.template('home/index.pt')
async def listing(request):
    name = request.path_params['name']
    if name == 'missing':
        chameleon.not_found(path=request.url.path)   # renders errors/404.pt
    return {'title': name, 'videos': [name], 'site': 'TP'}


app = Starlette(routes=[Route('/v/{name}', listing), Route('/videos/{name}', video, name='video')])
chameleon.configure(app=app)   # the decorator-order check at startup

with TestClient(app) as client:
    print(client.get('/v/intro').text)
    print(client.get('/v/missing').status_code)
```


    <h1>intro</h1>
    <ul><li><b>intro</b></li></ul>
    [TP](http://testserver/videos/intro)
    404


It is the API of `chameleon_rs.flask` and `chameleon_rs.fastapi`:

- `configure(template_folder=..., auto_reload=..., restricted_namespace=..., app=None, check_order=True)`, the same call as [chameleon_rs.configure()](../reference/configure.md#chameleon_rs.configure); `global_init(folder, auto_reload, cache_init)` configures the views only.
- `@template`, `@template()`, `@template('page.pt', content_type=..., status_code=...)` (`mimetype=` works too). Bare, it renders `{module}/{function}.html`, or `.pt`. The endpoint returns a dict, a mapping, a dataclass or a Pydantic model; a Starlette response passes through. The template also gets `request` and `url_for` (unless the model has them).
- Function endpoints and the methods of an `HTTPEndpoint` class, sync or async. An `async def` endpoint renders with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) on the event loop; a sync one renders with [render()](../reference/flask.render.md#chameleon_rs.flask.render) in Starlette's thread pool. `concurrent=` and `offload=` as above.
- `not_found(template_file='errors/404.pt', **data)`, `error(template_file, status_code=500, **data)` and `generic_error(template_file, status_code, template_data)` end a decorated endpoint with an error page. The error page also gets `request` and `url_for` (the data you pass wins), so a layout that links with `url_for` works there too.
- [render()](../reference/flask.render.md#chameleon_rs.flask.render), [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async), [response()](../reference/flask.response.md#chameleon_rs.flask.response), [response_async()](../reference/robyn.response_async.md#chameleon_rs.robyn.response_async), [clear()](../reference/flask.clear.md#chameleon_rs.flask.clear), `check_views(app)`.

Errors are [chameleon_rs.web.TemplateError](../reference/web.TemplateError.md#chameleon_rs.web.TemplateError), [NotFound](../reference/web.NotFound.md#chameleon_rs.web.NotFound), [ErrorPage](../reference/web.ErrorPage.md#chameleon_rs.web.ErrorPage) and [DecoratorOrderError](../reference/web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError), also importable from `chameleon_rs.starlette` (a missing template here is Chameleon's `ValueError`, as in the other integrations).

> **Warning: Template names come from your code**
>
> The names given to `@template`, [render()](../reference/flask.render.md#chameleon_rs.flask.render), [response()](../reference/flask.response.md#chameleon_rs.flask.response), [not_found()](../reference/flask.not_found.md#chameleon_rs.flask.not_found) / [error()](../reference/flask.error.md#chameleon_rs.flask.error) and [render_partial()](../reference/render_partial.md#chameleon_rs.render_partial) are looked up as the original libraries look them up, which allows `..` and absolute paths. Write them in your code; never build one from a request (a path or query parameter, a header). [chameleon_rs.starlette.Templates](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates), the Jinja drop-in, refuses names that reach outside its folder, as Jinja does.


# Decorator order

Starlette routes are usually lists (`Route('/', endpoint)`), where the order can't go wrong. It can with `app.add_route()`, Starlette 0.x's `@app.route`, or a route decorator of your own: a route registered before `@template` wrapped its endpoint calls the bare endpoint. An app given to `configure(app=app)` checks this inside its lifespan startup (Uvicorn and Granian report a failed startup), or before its first request when no lifespan runs, and raises [DecoratorOrderError](../reference/web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError) naming the endpoint, its file and line, its route and the fix. Mounted apps and routers (`Mount`, `Host`, `Router`, also behind middleware such as `Mount('/g', app=GZipMiddleware(sub_app))`) and `HTTPEndpoint` classes are checked too, and `check_views(app)` (or a `Router`) runs the check on demand. `check_order=False` and `check_views(app, ignore=[...])` are the escape hatch, as for [FastAPI](fastapi.md#decorator-order).
