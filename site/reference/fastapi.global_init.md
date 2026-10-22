## fastapi.global_init()


fastapi-chameleon's setup call: configures the views' templates only (partials keep


Usage

``` python
fastapi.global_init(
    template_folder,
    auto_reload=False,
    cache_init=True,
    restricted_namespace=True,
    *,
    app=None,
    check_order=True
)
```


their own folder, from [register_extensions](partials.register_extensions.md#chameleon_rs.partials.register_extensions)). With `cache_init` (the default) a call after the views' folder is configured does nothing. `app`: check that app's views when it starts, as `configure(app=...)` does (`check_order=False` turns the check off).

Raises `FastAPIChameleonException` if the folder is missing or not a directory.
