## litestar.configure()


Configures where templates live and how they load, for the whole process (the same as


Usage

``` python
litestar.configure(
    template_folder=None,
    *,
    auto_reload=None,
    restricted_namespace=None,
    app=None,
    check_order=True
)
```


[chameleon_rs.configure](configure.md#chameleon_rs.configure): for handlers, [ChameleonEngine()](litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine) without a directory and [render_partial](render_partial.md#chameleon_rs.render_partial)). Settings left out keep their current values.

`app`: the Litestar app checks its handlers when it starts (an `on_startup` hook, or before its first request when no lifespan runs): a handler its routes call without the `@template` wrapper that wraps it raises [chameleon_rs.web.DecoratorOrderError](web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError). With only `app` given, the settings are left as they are. `check_order=False` turns the check off, for an app whose routes call a function raw that is also used through a `@template` wrapper not routed on this app (call `check_views(app, ignore=[...])` in a test instead).

Raises [chameleon_rs.web.TemplateError](web.TemplateError.md#chameleon_rs.web.TemplateError) if the folder is missing or not a directory, and `TypeError` if `app` is not a Litestar application.
