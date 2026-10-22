## litestar.global_init()


The other integrations' setup call, for Litestar: configures the handlers' templates only


Usage

``` python
litestar.global_init(
    template_folder,
    auto_reload=False,
    cache_init=True,
    restricted_namespace=True,
    *,
    app=None,
    check_order=True
)
```


(partials keep their own folder). With `cache_init` (the default) a call after the handlers' folder is configured does nothing. `app`: check that app's handlers when it starts, as `configure(app=...)` does.

Raises [chameleon_rs.web.TemplateError](web.TemplateError.md#chameleon_rs.web.TemplateError) if the folder is missing or not a directory.
