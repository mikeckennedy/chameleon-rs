## configure()


Configures chameleon-rs for the whole process (partials, framework integrations, and Markdown).


Usage

``` python
configure(
    template_folder=None,
    *,
    auto_reload=None,
    restricted_namespace=None,
    strict_tags=None,
    strict_names=None,
    partial_search=None,
    markdown=None,
    markdown_cache=None
)
```


Sets where templates loaded by name live for [render_partial](render_partial.md#chameleon_rs.render_partial) and the framework integrations (`chameleon_rs.flask`, `chameleon_rs.starlette`, `chameleon_rs.fastapi`, `chameleon_rs.robyn`, `chameleon_rs.litestar`), and how they load.

`template_folder`: an existing directory. `auto_reload`: recompile a template when its file changes (checked at most once per file per render for partials). `restricted_namespace`: `False` allows attributes such as Alpine.js's `@click` (chameleon-flask's option). `strict_tags`: `True` makes an unclosed element carrying TAL, METAL or i18n attributes a `ParseError` at its start tag (default: `CHAMELEON_STRICT_TAGS`, else off). `strict_names`: `True` makes the integrations' decorator and module APIs (`@template`, [render](flask.render.md#chameleon_rs.flask.render), [response](flask.response.md#chameleon_rs.flask.response), `ErrorPage.template_file`) refuse a template name that could reach outside the template folder (absolute, `..`, a backslash, a drive letter or package spec, a NUL, surrounding whitespace) with the loader's `ValueError('Template not found: ...')`, as for a missing file (default: `CHAMELEON_STRICT_NAMES`, else off: they accept such names, as the original libraries do). `partial_search`: `'caller'` (the default: partials are looked for in the template folder, then on the calling template's search path, then in its folder and each folder above it, never outside its template tree) or `'registered'` (chameleon_partials' lookup: the template folder only, a path not there being an error). `markdown`: the `markdown:` expression types, `True` for `markdown:` with the defaults or a dict of profiles (prefix -\> options, see the docs), `False` for none; it applies to templates compiled afterwards. `markdown_cache`: how many conversions to keep (`0`: no cache). A setting left out keeps its current value; the first call that sets the folder's settings must give the folder (`partial_search` and the Markdown settings don't need one). Applies every time it is called (there is no `cache_init`).

Raises [chameleon_rs.web.TemplateError](web.TemplateError.md#chameleon_rs.web.TemplateError) for a missing folder, `ValueError` for an unknown `partial_search` or an invalid Markdown setting, `TypeError` for a Markdown setting of the wrong type (nothing changes then).
