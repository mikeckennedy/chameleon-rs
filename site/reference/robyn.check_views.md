## robyn.check_views()


Raises [chameleon_rs.web.DecoratorOrderError](web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError) if a route of `app` (a Robyn app, with


Usage

``` python
robyn.check_views(
    app,
    ignore=(),
)
```


the routes of the SubRouters it includes, or a SubRouter) calls a handler that `@template` wrapped after the route registered it. Runs by itself when the app starts, for an app given to `configure(app=...)`.

It cannot tell the mistake from a function routed raw on purpose whose `@template` wrapper is used elsewhere (called from another handler, or routed on another app): list such functions in `ignore`.
