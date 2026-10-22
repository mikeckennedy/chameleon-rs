## flask.global_init()


chameleon-flask's setup call: configures the views' templates only (partials keep their


Usage

``` python
flask.global_init(
    template_folder,
    auto_reload=False,
    cache_init=True,
    restricted_namespace=True
)
```


own folder, from [register_extensions](partials.register_extensions.md#chameleon_rs.partials.register_extensions)). With `cache_init` (the default) a call after the views' folder is configured does nothing.
