## starlette.configure()


Configures where templates live and how they load, for the whole process (the same as


Usage

``` python
starlette.configure(
    template_folder=None,
    *,
    auto_reload=None,
    restricted_namespace=None,
    app=None,
    check_order=True
)
```


[chameleon_rs.configure](configure.md#chameleon_rs.configure): for views, [Templates()](starlette.Templates.md#chameleon_rs.starlette.Templates) without a directory and [render_partial](render_partial.md#chameleon_rs.render_partial)). Settings left out keep their current values.

`app`: the Starlette app checks its views when it starts (in its lifespan, or before its first request when no lifespan runs): a view its routes registered before `@template` wrapped it raises [chameleon_rs.web.DecoratorOrderError](web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError). Mounted apps and routers, `Host` and `HTTPEndpoint` classes are included. With only `app` given, the settings are left as they are. `check_order=False` turns the check off, for an app whose routes call a function raw that is also used through a `@template` wrapper not routed on this app (call `check_views(app, ignore=[...])` in a test instead).

Raises [chameleon_rs.web.TemplateError](web.TemplateError.md#chameleon_rs.web.TemplateError) if the folder is missing or not a directory, and `TypeError` if `app` is not a Starlette application.
