## robyn.global_init()


chameleon-robyn's setup call: configures the handlers' templates only (partials keep


Usage

``` python
robyn.global_init(
    template_folder,
    auto_reload=False,
    cache_init=True,
    restricted_namespace=True,
    *,
    app=None,
    check_order=True
)
```


their own folder, from [register_extensions](partials.register_extensions.md#chameleon_rs.partials.register_extensions)). With `cache_init` (the default) a call after the handlers' folder is configured does nothing. `restricted_namespace=False` allows attributes such as Alpine.js's `@click`. `app`: check that app's handlers when it starts, as `configure(app=...)` does (`check_order=False` turns the check off).

Raises `ChameleonRobynException` if the folder is missing or not a directory.
