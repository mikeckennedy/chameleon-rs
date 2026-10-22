# Configuration

chameleon-rs has three layers of configuration:

1.  **[chameleon_rs.configure()](../reference/configure.md#chameleon_rs.configure)**: process-wide settings for templates loaded by name, used by [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) and the framework integrations.
2.  **Template options**: keyword arguments to the template classes and [PageTemplateLoader](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader), exactly as in Chameleon.
3.  **Environment variables**: Chameleon's `CHAMELEON_*` variables.


# [chameleon_rs.configure()](../reference/configure.md#chameleon_rs.configure)

``` python
import chameleon_rs

chameleon_rs.configure(
    template_folder='templates',
    auto_reload=False,
    restricted_namespace=True,
    partial_search='caller',
    markdown=True,
    markdown_cache=1024,
)
```

| Option | Default | Meaning |
|----|----|----|
| `template_folder` | none, required on the first call | An existing directory. Partials and the framework integrations load templates by name from it. |
| `auto_reload` | `False` | Recompile a template when its file changes. For partials, each file is checked at most once per render. Use it in development only. |
| `restricted_namespace` | `True` | `False` allows attributes with other namespaces or prefixes, such as Alpine.js's `@click` or Vue's `v-on:click`. |
| `partial_search` | `'caller'` | Where [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) looks: `'caller'` searches the template folder, then the calling template's search path and folders. `'registered'` searches the template folder only, as chameleon_partials does. See [Partials](partials.md#how-a-path-is-found). |
| `markdown` | none | The `markdown:` expression: `True` for the defaults, or a dict of profiles (prefix → options). `False` removes it. See [Markdown](markdown.md). |
| `markdown_cache` | `1024` | How many Markdown conversions to keep. `0` turns the cache off. |

How it behaves:

- **An option you leave out keeps its current value.** So `configure(partial_search='registered')` changes only the search mode.
- **Every call applies.** There's no `cache_init` as in chameleon_partials and chameleon-flask, so a second call with a different folder takes effect.
- **It's safe with threads.** Each configuration is an immutable object that's swapped in whole, so a render running at the same time never sees half of one. Still, call it at startup, not per request.
- **`partial_search` and the Markdown settings don't need a folder.** Every other option needs one, on the first call.
- It returns the new settings, and raises [chameleon_rs.web.TemplateError](../reference/web.TemplateError.md#chameleon_rs.web.TemplateError) for a missing folder, or `ValueError` for an unknown `partial_search` or an invalid Markdown setting (`TypeError` for one of the wrong type). A call that raises changes nothing.


``` python
import chameleon_rs

settings = chameleon_rs.configure(template_folder=template_folder, auto_reload=True)
print(settings.auto_reload, settings.restricted_namespace)

# Leaving options out keeps them: auto_reload is still True.
settings = chameleon_rs.configure(partial_search='registered')
print(settings.auto_reload)
```


    True True
    True


For tests, [chameleon_rs.partials.clear()](../reference/partials.clear.md#chameleon_rs.partials.clear) and [chameleon_rs.flask.clear()](../reference/flask.clear.md#chameleon_rs.flask.clear) forget the configuration again.

`chameleon_rs.flask.configure(..., app=app)` sets a configuration for one Flask or Quart app only. See [Flask and Quart](flask.md#one-configuration-per-app).


# Template options

The template classes take Chameleon's options as keyword arguments, or as class attributes on a subclass. [PageTemplateLoader](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) passes any extra keyword arguments on to each template it loads.

``` python
from chameleon_rs import PageTemplateFile, PageTemplateLoader

page = PageTemplateFile('templates/home.pt', auto_reload=True, encoding='utf-8')
templates = PageTemplateLoader(['templates'], '.pt', restricted_namespace=False)
```

The common ones:

| Option | Default | Meaning |
|----|----|----|
| `auto_reload` | `False` (or `CHAMELEON_RELOAD`) | File templates recompile when their file changes. |
| `encoding` | none | Lets you pass encoded byte strings as values. |
| `translate` | none | The translation function for `i18n:` attributes: `translate(msgid, domain=None, mapping=None, default=None, context=None, target_language=None)`. |
| `boolean_attributes` | the HTML set, in HTML mode | Attributes rendered as `name="name"` for a true value and dropped for a false one. |
| `strict` | `True` | Compile every expression up front. `False` only requires an expression to be valid when it's evaluated. |
| `restricted_namespace` | `True` | `False` passes unknown namespaces and prefixes through, for Alpine.js or Vue. |
| `trim_attribute_space` | `False` | Strip extra whitespace between attributes. |
| `implicit_i18n_translate` | `False` | Mark the text in every element for translation. |
| `implicit_i18n_attributes` | empty | Attribute names (lowercase) to mark for translation. |
| `on_error_handler` | none | Called with the exception in a `tal:on-error` fallback. |
| `default_expression` | `'python'` | The expression type used when there's no prefix. |
| `extra_builtins` | empty | Extra names available in every template. |
| `keep_body` / `keep_source` | `False` | Keep the template's source text, or a readable dump of its compiled program. |

The rest are also accepted, as in Chameleon: `default_marker`, `value_repr`, `tokenizer`, `debug`, `enable_data_attributes`, `enable_comment_interpolation` and `expression_types`. File templates also take `search_path`, `package_name`, `loader_class` and `prepend_relative_search_path`.

> **Note: `source` is a program dump, not Python**
>
> Chameleon compiles a template to a Python module, and `template.source` is that module's code. chameleon-rs compiles to a native program instead, so with `keep_source=True`, `source` holds a readable dump of the program. Nothing in Chameleon's own tests depends on it being Python.


# Environment variables

These are read once, when chameleon-rs is imported, as Chameleon reads them. The values `y`, `yes`, `t`, `true`, `on` and `1` (any case) count as true.

| Variable | Effect |
|----|----|
| `CHAMELEON_RELOAD` | The default for `auto_reload`: file templates reload when their file changes. |
| `CHAMELEON_EAGER` | File templates compile when they're created, not on first use. Handy in a test that checks every template in a project compiles. |
| `CHAMELEON_DEBUG` | Templates keep their body and a dump of their compiled program, and use a module loader in a temporary directory. |
| `CHAMELEON_CACHE` | A directory, which must exist. Chameleon writes the generated Python modules there. chameleon-rs checks the directory but doesn't write compiled programs to disk: compiling is fast enough not to need it. |

Unknown `CHAMELEON_*` variables are logged as warnings, as in Chameleon.
