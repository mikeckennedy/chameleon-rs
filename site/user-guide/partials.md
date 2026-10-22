# Partials

A partial is a template file rendered inside another template, with its own small model. Use them for anything you repeat: a card in a listing, a form field, a table row, a fragment you also return on its own to HTMX.

chameleon-rs has partials built in. Every template can call [render_partial](../reference/render_partial.md#chameleon_rs.render_partial), with nothing to register or pass in:

``` html

  ${render_partial('shared/partials/video_card.pt', video=video, show_date=True)}

```

`render_partial(path, **kwargs)` renders the template at `path` with `kwargs` as its variables, and inserts the result unescaped. Written as a whole `${...}` expression, as above, it doesn't make a Python call at all: the partial renders as a native frame of the page. That makes a listing of partials about 10x faster than Chameleon with chameleon_partials.


# Tell chameleon-rs where templates live

``` python
import chameleon_rs

chameleon_rs.configure(template_folder=template_folder)
```

Call it once at startup. See [Configuration](configuration.md) for its other options.


# How a path is found

By default, [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) looks for the path in these places, in order:

1.  The configured template folder.
2.  The calling template's search path: the folders of the [PageTemplateLoader](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) that loaded it.
3.  The calling template's own folder, then each folder above it.

If the path isn't found anywhere, the render raises `ValueError('Template not found: <path>.')`.

So a page can use partials that sit next to it, with a short path:

``` text
templates/
├── shared/
│   └── badge.pt
└── admin/users/
    ├── list.pt      ${render_partial('row.pt', user=user)}
    └── row.pt       ${render_partial('shared/badge.pt', label='admin')}
```


``` python
import chameleon_rs
from chameleon_rs import PageTemplateLoader

chameleon_rs.configure(template_folder=template_folder)
templates = PageTemplateLoader([template_folder])

print(templates['admin/users/list.pt'](users=['ada', 'grace']))
```


    <table>
      <tr><td>ada</td><td>admin</td></tr><tr><td>grace</td><td>admin</td></tr>
    </table>


`row.pt` isn't in the template folder's root, so it's found next to `list.pt`. `shared/badge.pt` is found in the template folder.

Each resolution is cached until the configuration changes, so the search happens once per path and calling template.


## chameleon_partials' lookup: `partial_search='registered'`

chameleon_partials resolves every path against the registered folder only, and treats any other path as an error. To get exactly that behaviour:

``` python
chameleon_rs.configure(partial_search='registered')
```

The default only differs from chameleon_partials where chameleon_partials would raise, so templates that work there work the same way here.


# What a partial sees

A partial sees only the keyword arguments you pass it, plus [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) itself. It doesn't see the calling template's variables. Pass what it needs explicitly:

``` html
${render_partial('shared/user_card.pt', user=user, is_admin=is_admin)}
```


# Calling it from Python

[chameleon_rs.render_partial](../reference/render_partial.md#chameleon_rs.render_partial) renders a partial from Python code: in a view that returns an HTMX fragment, in an email builder, or in a test. It returns an [HTML](../reference/partials.HTML.md#chameleon_rs.partials.HTML) object. Its `html_text` attribute is the markup, and its `__html__` method means a template inserts it without escaping:


``` python
fragment = chameleon_rs.render_partial('shared/badge.pt', label='new')
print(type(fragment).__name__, repr(fragment.html_text))
```


    HTML 'new'


From Python there's no calling template, so the path is looked up in the configured folder only. If no folder is configured, it raises [chameleon_rs.partials.PartialsException](../reference/partials.PartialsException.md#chameleon_rs.partials.PartialsException).


# chameleon_partials' API

`chameleon_rs.partials` is also chameleon_partials' API, so code written for it keeps working. With [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install), `import chameleon_partials` gives this module.

| chameleon_partials | chameleon-rs |
|----|----|
| `register_extensions(folder, auto_reload=False, cache_init=True)` | Still works. Configures partials only. [chameleon_rs.configure()](../reference/configure.md#chameleon_rs.configure) configures partials and the framework integrations. |
| `render_partial(path, **model)` | Same, and also a builtin in every template. |
| `extend_model(model)` | Still works, but isn't needed: every template already has [render_partial](../reference/render_partial.md#chameleon_rs.render_partial). |
| [PartialsException](../reference/partials.PartialsException.md#chameleon_rs.partials.PartialsException) | Same. Also a subclass of [chameleon_rs.web.TemplateError](../reference/web.TemplateError.md#chameleon_rs.web.TemplateError). |
| [HTML](../reference/partials.HTML.md#chameleon_rs.partials.HTML) | Same (`html_text`, `__html__`). |
| `has_registered_extensions = False` (test reset) | Still works. [chameleon_rs.partials.clear()](../reference/partials.clear.md#chameleon_rs.partials.clear) is the direct call. |

> **Note: Reserved names**
>
> As in chameleon_partials, a partial renders as if called with `encoding='utf-8'`, so don't use `encoding` as a model name. `translate`, `target_language` and `repeat` also have special meanings in any Chameleon template.
