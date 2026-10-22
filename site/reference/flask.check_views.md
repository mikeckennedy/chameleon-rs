## flask.check_views()


Raises [chameleon_rs.web.DecoratorOrderError](web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError) if a route of `app` (blueprints


Usage

``` python
flask.check_views(
    app,
    ignore=(),
)
```


included) calls a view that `@template` wrapped after the route registered it. Runs by itself before the app's first request, for an app given to `configure(app=...)`.

It cannot tell the mistake from a function routed raw on purpose whose `@template` wrapper is used elsewhere (called from another view, or routed on another app): list such functions in `ignore`.
