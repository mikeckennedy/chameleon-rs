## flask.template()


Decorates a Flask or Quart view (sync or async) to render a template with what it


Usage

``` python
flask.template(template_file: Callable[P, Coroutine[Any, Any, Any]]) -> Callable[P, Coroutine[Any, Any, flask.Response]]
 
flask.template(template_file: Callable[P, Any]) -> Callable[P, flask.Response]
 
flask.template(template_file: str | None = None, content_type: str = 'text/html', status_code: int = 200, mimetype: str | None = None, concurrent: bool | None = None, offload: bool = False) -> TemplateDecorator
```


returns: bare (`@template`), `@template()` or `@template('home/index.pt', content_type=..., status_code=...)`. See the module docs.

`concurrent=True` (async views): native partials called with an awaitable argument render concurrently ([PageTemplate](PageTemplate.md#chameleon_rs.PageTemplate)'s [concurrent](concurrent.md#chameleon_rs.concurrent) setting, for this view; `False` turns a template's setting off, `None`, the default, keeps it). `offload=True` (async views, experimental): the page renders in a worker thread (`render_async(offload=True)`). Both apply to `async def` views only: a sync view renders with [render](flask.render.md#chameleon_rs.flask.render) and ignores them.

Typing: the decorated view is typed as returning `flask.Response` (a coroutine of one for an `async def` view). A response the view returns itself passes through unchanged, so with Quart (or a bare werkzeug response) the object is that framework's response, not a `flask.Response`. A view counts as async when it is an `async def` function (`inspect.iscoroutinefunction`); type checkers go by its return type, so a plain function returning a coroutine is typed as async but runs as a sync view.
