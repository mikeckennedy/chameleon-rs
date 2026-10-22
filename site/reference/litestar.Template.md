## litestar.template()


Decorates a Litestar route handler function (sync or async; a `Controller` method too)


Usage

``` python
litestar.template(template_file: Callable[P, Coroutine[Any, Any, Any]]) -> Callable[P, Coroutine[Any, Any, Response[Any]]]
 
litestar.template(template_file: Callable[P, Any]) -> Callable[P, Response[Any]]
 
litestar.template(template_file: str | None = None, content_type: str = 'text/html', status_code: int | None = None, mimetype: str | None = None, concurrent: bool | None = None, offload: bool = False) -> TemplateDecorator
```


to render a template with what it returns: bare (`@template`), `@template()` or `@template('home/index.pt', content_type=..., status_code=...)`. Place it below the route decorator. `status_code=None` (the default): the route's, as for any Litestar response without one (200, 201 for `@post`, or `@get(..., status_code=...)`). The template also gets `request`, `csrf_input`, `url_for`, `csrf_token` and `url_for_static_asset`. See the module docs.

`concurrent=True` (async handlers): native partials called with an awaitable argument render concurrently (`None`, the default, keeps the template's setting). `offload=True` (async handlers, experimental): the page renders in a worker thread.

Typing: the decorated handler keeps its parameters and is typed as returning a `litestar.Response` (a coroutine of one for an `async def` handler).
