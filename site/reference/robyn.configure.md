## robyn.configure()


Configures where templates live and how they load, for the whole process (the same as


Usage

``` python
robyn.configure(
    template_folder=None,
    *,
    auto_reload=None,
    restricted_namespace=None,
    app=None,
    check_order=True
)
```


[chameleon_rs.configure](configure.md#chameleon_rs.configure): for handlers and for [render_partial](render_partial.md#chameleon_rs.render_partial)). Settings left out keep their current values.

`app`: the Robyn app checks its handlers when it starts (`app.start()`, before the server runs) and when a `robyn.testing.TestClient` is built for it: a handler its routes registered before `@template` wrapped it (`@template` written above `@app.get`) raises [chameleon_rs.web.DecoratorOrderError](web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError). Routes of included `SubRouter`are covered. With only `app` given, the settings are left as they are. `check_order=False` turns the check off, for an app whose routes call a function raw that is also used through a `@template` wrapper not routed on this app (call `check_views(app, ignore=[...])` in a test instead).

Raises `ChameleonRobynException` if the folder is missing or not a directory, and `TypeError` if `app` is not a Robyn application (a `SubRouter` is checked through the app that includes it).
