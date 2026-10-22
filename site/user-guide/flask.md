# Flask and Quart

`chameleon_rs.flask` renders templates from Flask and Quart views. It has chameleon-flask's API, so moving an app over is an import change, or no change at all with [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install).

`import chameleon_rs` doesn't import Flask. Only `import chameleon_rs.flask` does.


# A first view


``` python
import flask

import chameleon_rs.flask as chameleon_flask

app = flask.Flask(__name__)
chameleon_flask.configure(template_folder=template_folder, auto_reload=dev_mode)


@app.get('/')
@chameleon_flask.template('home/index.pt')
def index():
    return {'message': 'Hello', 'videos': ['intro', 'partials']}
```


The view returns the template's model, and `@template` renders it into a `flask.Response`:


``` python
client = app.test_client()
print(client.get('/').text)
```


    <h1>Hello</h1>
    <ul><li>[intro](/videos/intro)</li>
    <li>[partials](/videos/partials)</li></ul>


[configure()](../reference/configure.md#chameleon_rs.configure) here is the same call as [chameleon_rs.configure()](../reference/configure.md#chameleon_rs.configure), so it also tells [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) where templates live.


# What a view can return

- **A model:** a `dict`, any other mapping, a dataclass instance (its fields, shallowly) or a Pydantic model (anything with a callable `model_dump()`).
- **A response:** a `flask.Response`, a Quart response or a werkzeug response passes through unchanged. Use it for redirects.

Anything else raises `chameleon_rs.flask.FlaskChameleonException`.


# The `@template` decorator

``` python
@chameleon_flask.template('home/index.pt')                            # a template path
@chameleon_flask.template('feed.pt', content_type='application/xml')  # or mimetype=
@chameleon_flask.template('home/created.pt', status_code=201)
@chameleon_flask.template                                             # no arguments
```

With no path, it renders `{module}/{function}.html`, where `{module}` is the last part of the view's module name. If there's no `.html` file in the template folder, it uses `{module}/{function}.pt`. It checks once, when the view is decorated.

It works on sync views, Flask's async views and Quart views alike.


# Error pages

Inside a decorated view, [not_found()](../reference/flask.not_found.md#chameleon_rs.flask.not_found) and [error()](../reference/flask.error.md#chameleon_rs.flask.error) stop the view and render an error page:


``` python
app = flask.Flask(__name__)


@app.get('/videos/<name>')
@chameleon_flask.template('home/index.pt')
def video(name):
    if name != 'intro':
        chameleon_flask.not_found(path=flask.request.path)   # renders errors/404.pt
    return {'message': name, 'videos': [name]}


response = app.test_client().get('/videos/missing')
print(response.status_code, response.text)
```


    404 <h1>Not found: /videos/missing</h1>


- `not_found(template_file='errors/404.pt', **data)` renders the page with status 404.
- `error(template_file, status_code=500, **data)` renders any error page.
- `generic_error(template_file, status_code, template_data)` is fastapi-chameleon's name for [error()](../reference/flask.error.md#chameleon_rs.flask.error).

They raise [chameleon_rs.web.NotFound](../reference/web.NotFound.md#chameleon_rs.web.NotFound) and [chameleon_rs.web.ErrorPage](../reference/web.ErrorPage.md#chameleon_rs.web.ErrorPage), which the decorator turns into the response.


# Rendering without the decorator

``` python
html = chameleon_flask.render('emails/welcome.pt', user=user)            # a str
resp = chameleon_flask.response('feed.pt', content_type='application/xml', items=items)
```

[render()](../reference/flask.render.md#chameleon_rs.flask.render) and [response()](../reference/flask.response.md#chameleon_rs.flask.response) work anywhere, such as in a view that builds its own response or in a background job. Both raise `FlaskChameleonException` if nothing has been configured.


# One configuration per app

Pass `app=` to give one Flask or Quart app its own template folder. It's stored in `app.extensions['chameleon_rs']` and used while that app handles a request. Other apps, and code outside a request, use the process-wide configuration.

``` python
chameleon_flask.configure(template_folder='admin/templates', app=admin_app)
```


# Quart

The same module works with Quart, with no extra install:

``` python
import quart

import chameleon_rs.flask as chameleon_flask

app = quart.Quart(__name__)
chameleon_flask.configure(template_folder=template_folder)


@app.get('/')
@chameleon_flask.template('home/index.pt')
async def index():
    videos = await load_videos()
    return {'message': 'Hello', 'videos': videos}
```

> **Note: Renders are synchronous for now**
>
> An async view awaits your code, then renders the template synchronously, as chameleon-flask does. Async rendering, which doesn't block the event loop, is in progress.


# Coming from chameleon-flask

| chameleon-flask | chameleon-rs |
|----|----|
| `global_init(folder, auto_reload=False, cache_init=True)` | Still works. It configures the views only, as before, and ignores a second call. [configure()](../reference/configure.md#chameleon_rs.configure) sets views and partials together, and every call applies. |
| `@template`, [render](../reference/flask.render.md#chameleon_rs.flask.render), [response](../reference/flask.response.md#chameleon_rs.flask.response), [not_found](../reference/flask.not_found.md#chameleon_rs.flask.not_found) | Same. [not_found](../reference/flask.not_found.md#chameleon_rs.flask.not_found) also takes the template as `template_file=`, as well as `four04template_file=`. |
| -- | [error()](../reference/flask.error.md#chameleon_rs.flask.error), any status code. |
| `FlaskChameleonException`, `FlaskChameleonNotFoundException` | Same, now subclasses of [chameleon_rs.web.TemplateError](../reference/web.TemplateError.md#chameleon_rs.web.TemplateError) and [NotFound](../reference/web.NotFound.md#chameleon_rs.web.NotFound). `except chameleon_rs.web.TemplateError` catches them all. |
| Views return a `dict` | Also mappings, dataclasses and Pydantic models. |
