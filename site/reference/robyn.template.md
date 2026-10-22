## robyn.template()


Decorates a Robyn route handler (sync or async) to render a template with what it


Usage

``` python
robyn.template(template_file: Callable[P, Coroutine[Any, Any, Any]]) -> Callable[P, Coroutine[Any, Any, Response]]
 
robyn.template(template_file: Callable[P, Any]) -> Callable[P, Response]
 
robyn.template(template_file: str | None = None, content_type: str = 'text/html', status_code: int = 200, mimetype: str | None = None, concurrent: bool | None = None, offload: bool = False) -> TemplateDecorator
```


returns: bare (`@template`), `@template()` or `@template('home/index.pt', content_type=..., status_code=...)`. Place it below the route decorator. See the module docs.

`concurrent=True` (async handlers): native partials called with an awaitable argument render concurrently ([PageTemplate](PageTemplate.md#chameleon_rs.PageTemplate)'s [concurrent](concurrent.md#chameleon_rs.concurrent) setting, for this handler; `False` turns a template's setting off, `None`, the default, keeps it). `offload=True` (async handlers, experimental): the page renders in a worker thread (`render_async(offload=True)`). Both apply to `async def` handlers only: a sync handler renders with [render](flask.render.md#chameleon_rs.flask.render) and ignores them.

Typing: the decorated handler keeps its parameters and is typed as returning `robyn.Response` (a coroutine of one for an `async def` handler); a response the handler returns itself passes through unchanged.
