## web.DecoratorOrderError


A view was registered with its framework before `@template` wrapped it (`@template`


Usage

``` python
web.DecoratorOrderError()
```


written above `@app.get` / `@app.route` / `@router.get`), so the route calls the bare view and the template never renders. Raised when the app starts (or on its first request), for apps given to `configure(app=...)` or `global_init(..., app=...)`, and by `check_views(app)`.
