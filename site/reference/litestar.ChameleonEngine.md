## litestar.ChameleonEngine


Litestar's `TemplateEngineProtocol` for chameleon-rs templates:


Usage

``` python
litestar.ChameleonEngine()
```


`TemplateConfig(engine=ChameleonEngine, directory='templates')`.

`directory`: the template folder (or a list of folders, searched in order). `engine_instance`: a [chameleon_rs.PageTemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) to use instead (or `ChameleonEngine.from_loader(loader)`). Neither: the templates come from the process-wide configuration ([chameleon_rs.configure](configure.md#chameleon_rs.configure) / [configure](configure.md#chameleon_rs.configure) in this module), looked up at each render (`TemplateConfig(engine=ChameleonEngine())`). `auto_reload` / `restricted_namespace` (with `directory`): the loader's options (`None`: Chameleon's defaults, `CHAMELEON_RELOAD` for reloading); pass an instance to set them (`TemplateConfig(engine=ChameleonEngine('templates', auto_reload=True))`).

Registers Litestar's `url_for`, `csrf_token` and `url_for_static_asset`, as its Jinja and Mako engines do.


## Attributes

| Name | Description |
|----|----|
| [loader](#loader) | The [TemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) templates come from (the configured one when the engine was |

------------------------------------------------------------------------


#### loader


The [TemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) templates come from (the configured one when the engine was


`loader: TemplateLoader`


given neither a directory nor a loader).


## Methods

| Name | Description |
|----|----|
| [from_loader()](#from_loader) | An engine over an existing [chameleon_rs.PageTemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) (as Litestar's |
| [get_template()](#get_template) | The page template `template_name` (relative to the template folder). |
| [register_template_callable()](#register_template_callable) | Makes `template_callable` a variable `key` of every template; called from a |
| [render_string()](#render_string) | Renders a template given as a string (`Template(template_str=...)`) with |

------------------------------------------------------------------------


#### from_loader()


An engine over an existing [chameleon_rs.PageTemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) (as Litestar's


Usage

``` python
from_loader(loader)
```


`JinjaTemplateEngine.from_environment`).


------------------------------------------------------------------------


#### get_template()


The page template `template_name` (relative to the template folder).


Usage

``` python
get_template(template_name)
```


Raises Litestar's `TemplateNotFoundException` if no folder has it, or if the name could reach outside the folder (absolute, `..`, a backslash, a drive letter or a package spec), as Litestar's Jinja engine refuses such names.


------------------------------------------------------------------------


#### register_template_callable()


Makes `template_callable` a variable `key` of every template; called from a


Usage

``` python
register_template_callable(key, template_callable)
```


template, it receives the template's context first (`url_for(context, name)`).


------------------------------------------------------------------------


#### render_string()


Renders a template given as a string (`Template(template_str=...)`) with


Usage

``` python
render_string(template_string, context)
```


`context` and the template callables, compiled with the loader's template options (`translate`, `restricted_namespace`, …), as Jinja's `from_string` uses its environment. Compiled templates are kept for reuse (up to 128, the oldest dropped first), per loader: a later [configure()](configure.md#chameleon_rs.configure) (a new loader) compiles them afresh.
