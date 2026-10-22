## litestar.ChameleonTemplate


A page template as Litestar's `TemplateProtocol`: `render(**context)` renders it with


Usage

``` python
litestar.ChameleonTemplate()
```


the engine's template callables bound to the context.


## Methods

| Name | Description |
|----|----|
| [render()](#render) | The page, rendered with `kwargs` as the context (Litestar passes no positional |
| [render_async()](#render_async) | [render](flask.render.md#chameleon_rs.flask.render) as a coroutine: the template's [render_async](robyn.render_async.md#chameleon_rs.robyn.render_async) (awaitable values are |

------------------------------------------------------------------------


#### render()


The page, rendered with `kwargs` as the context (Litestar passes no positional


Usage

``` python
render(*args, **kwargs)
```


arguments).


------------------------------------------------------------------------


#### render_async()


[render](flask.render.md#chameleon_rs.flask.render) as a coroutine: the template's [render_async](robyn.render_async.md#chameleon_rs.robyn.render_async) (awaitable values are


Usage

``` python
render_async(**kwargs)
```


awaited where the template uses them). Litestar's [Template](litestar.Template.md#chameleon_rs.litestar.Template) response never calls it; it is for code rendering a template itself.
