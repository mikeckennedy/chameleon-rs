## flask.configure()


Configures where templates live and how they load: for the whole process (the same as


Usage

``` python
flask.configure(
    template_folder=None,
    *,
    auto_reload=None,
    restricted_namespace=None,
    app=None,
    check_order=True
)
```


[chameleon_rs.configure](configure.md#chameleon_rs.configure): for views and for [render_partial](render_partial.md#chameleon_rs.render_partial)), or, with `app`, for that Flask app's views only (stored in `app.extensions['chameleon_rs']`). Settings left out keep their current values.

With `app`, the app also checks its views (Flask has no startup hook: before its first request; Quart: when it starts serving, and before its first request): a view registered by its route before `@template` wrapped it (`@template` written above `@app.route`) raises [chameleon_rs.web.DecoratorOrderError](web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError). `check_order=False` turns that off, for an app whose routes call a function raw that is also used through a `@template` wrapper not routed on this app (call `check_views(app, ignore=[...])` in a test instead). Raises `TypeError` if `app` is not a Flask or Quart application.

Raises `FlaskChameleonException` if the folder is missing or not a directory.
