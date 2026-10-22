## starlette.template()


Decorates a Starlette endpoint (a function, or a method of an `HTTPEndpoint` class;


Usage

``` python
starlette.template(template_file: Callable[P, Coroutine[Any, Any, Any]]) -> Callable[P, Coroutine[Any, Any, starlette.responses.Response]]
 
starlette.template(template_file: Callable[P, Any]) -> Callable[P, starlette.responses.Response]
 
starlette.template(template_file: str | None = None, content_type: str = 'text/html', status_code: int = 200, mimetype: str | None = None, concurrent: bool | None = None, offload: bool = False) -> TemplateDecorator
```


sync or async) to render a template with what it returns: bare (`@template`), `@template()` or `@template('home/index.pt', content_type=..., status_code=...)`. The template also gets `request` and `url_for`. See the module docs.

`concurrent=True` (async endpoints): native partials called with an awaitable argument render concurrently (`None`, the default, keeps the template's setting). `offload=True` (async endpoints, experimental): the page renders in a worker thread.

Typing: the decorated endpoint keeps its parameters and is typed as returning a Starlette `Response` (a coroutine of one for an `async def` endpoint).
