## starlette.Templates


Starlette's `Jinja2Templates` for chameleon-rs templates.


Usage

``` python
starlette.Templates()
```


`directory`: the template folder (or a list of folders, searched in order). Left out, the templates come from the process-wide configuration ([chameleon_rs.configure](configure.md#chameleon_rs.configure) / [configure](configure.md#chameleon_rs.configure) in this module), looked up at each render. `context_processors`: functions taking the request and returning a dict added to each template's context. `auto_reload` / `restricted_namespace` (with `directory`): the loader's options (`None`: Chameleon's defaults, `CHAMELEON_RELOAD` for reloading).


## Attributes

| Name | Description |
|----|----|
| [loader](#loader) | The [TemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) templates come from (the configured one when no directory was |

------------------------------------------------------------------------


#### loader


The [TemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) templates come from (the configured one when no directory was


`loader: TemplateLoader`


given).


## Methods

| Name | Description |
|----|----|
| [TemplateResponse()](#TemplateResponse) | Renders a template to an HTML response (Starlette's [TemplateResponse](starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse)). Renders |
| [TemplateResponse_async()](#TemplateResponse_async) | [TemplateResponse](starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse) rendering with [render_async](robyn.render_async.md#chameleon_rs.robyn.render_async): awaitable values in the context |
| [get_template()](#get_template) | The page template `name` (relative to the template folder). |

------------------------------------------------------------------------


#### TemplateResponse()


Renders a template to an HTML response (Starlette's [TemplateResponse](starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse)). Renders


Usage

``` python
TemplateResponse(
    request,
    name,
    context=None,
    status_code=200,
    headers=None,
    media_type=None,
    background=None
)
```


synchronously: in an `async def` endpoint, [TemplateResponse_async](starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse_async) does not block the event loop while the page renders.


------------------------------------------------------------------------


#### TemplateResponse_async()


[TemplateResponse](starlette.Templates.md#chameleon_rs.starlette.Templates.TemplateResponse) rendering with [render_async](robyn.render_async.md#chameleon_rs.robyn.render_async): awaitable values in the context


Usage

``` python
TemplateResponse_async(
    request,
    name,
    context=None,
    status_code=200,
    headers=None,
    media_type=None,
    background=None,
    *,
    concurrent=None,
    offload=False
)
```


are awaited where the template uses them, without blocking the event loop. `concurrent=True` fans native partials with awaitable arguments out (`None` keeps the template's setting); `offload=True` (experimental) renders in a worker thread.


------------------------------------------------------------------------


#### get_template()


The page template `name` (relative to the template folder).


Usage

``` python
get_template(name)
```


Raises [chameleon_rs.web.TemplateNotFound](web.TemplateNotFound.md#chameleon_rs.web.TemplateNotFound) (a `ValueError`) if no folder has it, or if the name could reach outside the folder (absolute, `..`, a backslash, a drive letter or a package spec), as Jinja's `FileSystemLoader` refuses such names. (Only here: [PageTemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) and the decorator API raise Chameleon's `ValueError`.)
