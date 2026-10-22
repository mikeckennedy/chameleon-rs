## litestar.check_views()


Raises [chameleon_rs.web.DecoratorOrderError](web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError) if a route of `app` (a Litestar app or


Usage

``` python
litestar.check_views(
    app,
    ignore=(),
)
```


a `Router`, controllers included) calls a handler function that `@template` wrapped without going through the wrapper. Runs by itself when the app starts, for an app given to `configure(app=...)`.

It cannot tell the mistake from a function routed raw on purpose whose `@template` wrapper is used elsewhere (called from another handler, or routed on another app): list such functions in `ignore`.
