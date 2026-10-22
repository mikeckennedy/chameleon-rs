## flask.global_init()


chameleon-flask's setup call: configures the views' templates only (partials keep their


Usage

``` python
flask.global_init(
    template_folder,
    auto_reload=False,
    cache_init=True,
    restricted_namespace=True,
    *,
    app=None,
    check_order=True
)
```


own folder, from [register_extensions](partials.register_extensions.md#chameleon_rs.partials.register_extensions)). With `cache_init` (the default) a call after the views' folder is configured does nothing. `app`: check that app's views before its first request, as `configure(app=...)` does (the settings stay process-wide; `check_order=False` turns the check off).
