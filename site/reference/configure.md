## configure()


Configures chameleon-rs for the whole process: where templates loaded by name live, for


Usage

``` python
configure(
    template_folder=None,
    *,
    auto_reload=None,
    restricted_namespace=None,
    partial_search=None
)
```


[render_partial](render_partial.md#chameleon_rs.render_partial) and the framework integrations (`chameleon_rs.flask`), and how they load.

`template_folder`: an existing directory. `auto_reload`: recompile a template when its file changes (checked at most once per file per render for partials). `restricted_namespace`: `False` allows attributes such as Alpine.js's `@click` (chameleon-flask's option). `partial_search`: `'caller'` (the default: partials are looked for in the template folder, then on the calling template's search path, then in its folder and each folder above it) or `'registered'` (chameleon_partials' lookup: the template folder only, a path not there being an error). A setting left out keeps its current value; the first call that sets anything but `partial_search` must give the folder. Applies every time it is called (there is no `cache_init`).

Raises [chameleon_rs.web.TemplateError](web.TemplateError.md#chameleon_rs.web.TemplateError) for a missing folder, `ValueError` for an unknown `partial_search`.
