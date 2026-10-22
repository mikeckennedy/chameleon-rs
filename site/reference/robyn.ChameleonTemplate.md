## robyn.ChameleonTemplate


Chameleon templates through Robyn's `TemplateInterface`, as its `JinjaTemplate`:


Usage

``` python
robyn.ChameleonTemplate()
```


owns its own loader and needs no [global_init()](flask.global_init.md#chameleon_rs.flask.global_init).

`directory`: the template folder. `auto_reload`: recompile a template when its file changes (development). `encoding`: the output encoding. `restricted_namespace=False` allows Alpine.js/htmx-style attributes (`@click`, `:class`).


## Methods

| Name | Description |
|----|----|
| [render_template()](#render_template) | Renders `template_name` (relative to the directory) with `kwargs` as its model |
| [render_template_async()](#render_template_async) | [render_template](robyn.ChameleonTemplate.md#chameleon_rs.robyn.ChameleonTemplate.render_template) as a coroutine, rendering with [render_async](robyn.render_async.md#chameleon_rs.robyn.render_async) (awaitable |

------------------------------------------------------------------------


#### render_template()


Renders `template_name` (relative to the directory) with `kwargs` as its model


Usage

``` python
render_template(template_name, **kwargs)
```


into a 200 `robyn.Response` (`text/html; charset=utf-8`).


------------------------------------------------------------------------


#### render_template_async()


[render_template](robyn.ChameleonTemplate.md#chameleon_rs.robyn.ChameleonTemplate.render_template) as a coroutine, rendering with [render_async](robyn.render_async.md#chameleon_rs.robyn.render_async) (awaitable


Usage

``` python
render_template_async(template_name, **kwargs)
```


values are awaited where the template uses them, without blocking the event loop).
