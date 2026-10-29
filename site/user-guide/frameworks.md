# Web frameworks

chameleon-rs works with the Python web frameworks you're likely to use, with nothing extra to install. Each integration is a module of chameleon-rs that imports its framework only when you import it, and uses the version your app already has.

| Framework | Use | It replaces | Guide |
|----|----|----|----|
| Flask, Quart | `chameleon_rs.flask` | chameleon-flask | [Flask and Quart](flask.md) |
| FastAPI | `chameleon_rs.fastapi` | fastapi-chameleon | [FastAPI](fastapi.md) |
| Starlette | `chameleon_rs.starlette` | Starlette's `Jinja2Templates` | [Starlette](starlette.md) |
| Robyn | `chameleon_rs.robyn` | chameleon-robyn | [Robyn](robyn.md) |
| Litestar | `chameleon_rs.litestar` | Litestar's Jinja engine, litestar-chameleon | [Litestar](litestar.md) |
| Pyramid | pyramid_chameleon, unchanged | (runs through [compat.install()](../reference/compat.install.md#chameleon_rs.compat.install)) | [Pyramid](pyramid.md) |

Already using one of the libraries in the third column? [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install) makes its import give the built-in, so the app runs unchanged. See [Switching from Chameleon](switching-from-chameleon.md).


# The same page in each framework

Each example renders `templates/home/index.pt`:

``` html
<h1>${title}</h1>
<ul><li tal:repeat="v videos">${v}</li></ul>
```


- <a href="" id="tabset-1-1-tab" class="nav-link active" data-bs-toggle="tab" data-bs-target="#tabset-1-1" role="tab" aria-controls="tabset-1-1" aria-selected="true">Flask</a>
- <a href="" id="tabset-1-2-tab" class="nav-link" data-bs-toggle="tab" data-bs-target="#tabset-1-2" role="tab" aria-controls="tabset-1-2" aria-selected="false">Quart</a>
- <a href="" id="tabset-1-3-tab" class="nav-link" data-bs-toggle="tab" data-bs-target="#tabset-1-3" role="tab" aria-controls="tabset-1-3" aria-selected="false">FastAPI</a>
- <a href="" id="tabset-1-4-tab" class="nav-link" data-bs-toggle="tab" data-bs-target="#tabset-1-4" role="tab" aria-controls="tabset-1-4" aria-selected="false">Starlette</a>
- <a href="" id="tabset-1-5-tab" class="nav-link" data-bs-toggle="tab" data-bs-target="#tabset-1-5" role="tab" aria-controls="tabset-1-5" aria-selected="false">Robyn</a>
- <a href="" id="tabset-1-6-tab" class="nav-link" data-bs-toggle="tab" data-bs-target="#tabset-1-6" role="tab" aria-controls="tabset-1-6" aria-selected="false">Litestar</a>
- <a href="" id="tabset-1-7-tab" class="nav-link" data-bs-toggle="tab" data-bs-target="#tabset-1-7" role="tab" aria-controls="tabset-1-7" aria-selected="false">Pyramid</a>


``` python
import flask

import chameleon_rs.flask as chameleon

app = flask.Flask(__name__)
chameleon.configure(template_folder='templates', app=app)


@app.get('/')
@chameleon.template('home/index.pt')
def index():
    return {'title': 'Latest videos', 'videos': ['intro', 'partials']}
```


``` python
import quart

import chameleon_rs.flask as chameleon

app = quart.Quart(__name__)
chameleon.configure(template_folder='templates', app=app)


@app.get('/')
@chameleon.template('home/index.pt')
async def index():
    return {'title': 'Latest videos', 'videos': await load_videos()}
```


``` python
import fastapi

import chameleon_rs.fastapi as chameleon

app = fastapi.FastAPI()
chameleon.configure(template_folder='templates', app=app)


@app.get('/')
@chameleon.template('home/index.pt')
async def index():
    return {'title': 'Latest videos', 'videos': await load_videos()}
```


``` python
from starlette.applications import Starlette
from starlette.routing import Route

from chameleon_rs.starlette import Templates

templates = Templates(directory='templates')


async def index(request):
    model = {'title': 'Latest videos', 'videos': await load_videos()}
    return await templates.TemplateResponse_async(request, 'home/index.pt', model)


app = Starlette(routes=[Route('/', index)])
```


``` python
from robyn import Robyn

import chameleon_rs.robyn as chameleon

app = Robyn(__file__)
chameleon.configure(template_folder='templates', app=app)


@app.get('/')
@chameleon.template('home/index.pt')
async def index():
    return {'title': 'Latest videos', 'videos': await load_videos()}
```


``` python
from litestar import Litestar, get

import chameleon_rs.litestar as chameleon


@get('/')
@chameleon.template('home/index.pt')
async def index() -> dict:
    return {'title': 'Latest videos', 'videos': await load_videos()}


app = Litestar([index])
chameleon.configure(template_folder='templates', app=app)
```


``` python
import chameleon_rs.compat

chameleon_rs.compat.install()   # first, before pyramid_chameleon is imported

from pyramid.config import Configurator
from pyramid.view import view_config


@view_config(route_name='home', renderer='templates/home/index.pt')
def index(request):
    return {'title': 'Latest videos', 'videos': ['intro', 'partials']}


def main(global_config, **settings):
    with Configurator(settings=settings) as config:
        config.include('pyramid_chameleon')
        config.add_route('home', '/')
        config.scan()
    return config.make_wsgi_app()
```


# What the integrations share

Flask, FastAPI, Starlette, Robyn and Litestar have the same decorator API, so what you learn on one carries over:

- **`configure(template_folder=..., auto_reload=..., app=app)`** is the same call as [chameleon_rs.configure()](../reference/configure.md#chameleon_rs.configure): one setup for views, partials and Markdown. Every call applies. `global_init(...)` is the original libraries' name and still works.
- **`@template('page.pt')`** renders what the view returns: a `dict`, any mapping, a dataclass or a Pydantic model. A framework response passes through unchanged, for redirects. `status_code=` and `content_type=` set the response; bare `@template` picks `{module}/{function}.html` or `.pt`.
- **[not_found()](../reference/flask.not_found.md#chameleon_rs.flask.not_found) and `error(template_file, status_code=500)`** end a view with an error page.
- **[render()](../reference/flask.render.md#chameleon_rs.flask.render) and [response()](../reference/flask.response.md#chameleon_rs.flask.response)**, with [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) and [response_async()](../reference/robyn.response_async.md#chameleon_rs.robyn.response_async), render without the decorator: an email body, a feed, an HTMX fragment.
- **`check_views(app)`** and **[clear()](../reference/flask.clear.md#chameleon_rs.flask.clear)** for tests.
- **Errors** share one base class, [chameleon_rs.web.TemplateError](../reference/web.TemplateError.md#chameleon_rs.web.TemplateError), whichever module raised them.


## Async views

An `async def` view renders with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async): a model value that is awaitable is awaited where the template uses it, and the render doesn't block the event loop while it waits. A plain `def` view renders with [render()](../reference/flask.render.md#chameleon_rs.flask.render), where the framework runs it. See [Async rendering](async.md) for the details and the opt-in concurrency settings.


## What a template gets

| Integration | Besides the model |
|----|----|
| Flask, Quart, FastAPI, Robyn | Nothing: the template gets what the view returns, as with the original libraries. Return `request` in the model if the template needs it. |
| Starlette | `request` and `url_for`, as with `Jinja2Templates`, plus its context processors with [Templates](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates). |
| Litestar | `request`, `csrf_input`, `url_for`, `csrf_token`, `url_for_static_asset` and your registered template callables. |
| Pyramid | `request`, `context`, `view` and `renderer_name`. |

Every template also has [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) built in, and `markdown:` once it's turned on.


## Decorator order

`@template` goes below the route decorator:

``` python
@app.get('/')                   # first: the route...
@chameleon.template('page.pt')  # ...then @template
def index(): ...
```

Written the other way round, the router registers the bare view, the template never renders and the route quietly returns the view's dict as JSON. Pass `app=` to [configure()](../reference/configure.md#chameleon_rs.configure) and the mistake fails loudly with a [DecoratorOrderError](../reference/web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError) that names the view and the fix: when the app starts (FastAPI, Starlette, Quart, Robyn, Litestar) or on the first request (Flask, which has no startup hook). Each guide has the details.

> **Warning: Template names come from your code**
>
> The names given to `@template`, [render()](../reference/flask.render.md#chameleon_rs.flask.render), [response()](../reference/flask.response.md#chameleon_rs.flask.response), the error pages and [render_partial()](../reference/render_partial.md#chameleon_rs.render_partial) are looked up as the original libraries look them up, which allows `..` and absolute paths. Write them in your code; never build one from a request (a path or query parameter, a header). Starlette's [Templates](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates) and Litestar's [ChameleonEngine](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine) refuse names that reach outside their folder, as Jinja does.
