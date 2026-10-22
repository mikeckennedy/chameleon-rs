## fastapi.configure()


Configures where templates live and how they load, for the whole process (the same as


Usage

``` python
fastapi.configure(
    template_folder=None,
    *,
    auto_reload=None,
    restricted_namespace=None,
    app=None,
    check_order=True
)
```


[chameleon_rs.configure](configure.md#chameleon_rs.configure): for views and for [render_partial](render_partial.md#chameleon_rs.render_partial)). Settings left out keep their current values.

`app`: the FastAPI (or Starlette) app checks its views when it starts (in its lifespan, or before its first request when no lifespan runs): a view its routes registered before `@template` wrapped it (`@template` written above `@app.get`) raises [chameleon_rs.web.DecoratorOrderError](web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError). Sub-routers and mounted apps are included. With only `app` given, the settings are left as they are. `check_order=False` turns the check off, for an app whose routes call a function raw that is also used through a `@template` wrapper not routed on this app (call `check_views(app, ignore=[...])` in a test instead).

Raises `FastAPIChameleonException` if the folder is missing or not a directory, and `TypeError` if `app` is not a FastAPI or Starlette application (an `APIRouter` is checked through the app that includes it).
