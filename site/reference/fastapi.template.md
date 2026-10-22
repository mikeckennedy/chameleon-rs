## fastapi.template()


Decorates a FastAPI view (sync or async) to render a template with what it returns:


Usage

``` python
fastapi.template(template_file: Callable[P, Coroutine[Any, Any, Any]]) -> Callable[P, Coroutine[Any, Any, fastapi.Response]]
 
fastapi.template(template_file: Callable[P, Any]) -> Callable[P, fastapi.Response]
 
fastapi.template(template_file: str | None = None, mimetype: str = 'text/html', status_code: int = 200, content_type: str | None = None, concurrent: bool | None = None, offload: bool = False) -> TemplateDecorator
```


bare (`@template`), `@template()` or `@template('home/index.pt', mimetype=..., status_code=...)`. Place it below the route decorator. See the module docs.

`concurrent=True` (async views): native partials called with an awaitable argument render concurrently ([PageTemplate](PageTemplate.md#chameleon_rs.PageTemplate)'s [concurrent](concurrent.md#chameleon_rs.concurrent) setting, for this view; `False` turns a template's setting off, `None`, the default, keeps it). `offload=True` (async views, experimental): the page renders in a worker thread (`render_async(offload=True)`). Both apply to `async def` views only: a sync view renders with [render](flask.render.md#chameleon_rs.flask.render) and ignores them.

Typing: the decorated view keeps its parameters and is typed as returning `fastapi.Response` (a coroutine of one for an `async def` view); a response the view returns itself passes through unchanged.
