# API Reference


The public API of chameleon-rs. It mirrors Chameleon's (PageTemplate, PageTemplateFile, PageTemplateLoader, …), plus async rendering, built-in partials, process-wide configuration (including Markdown) and the Flask/Quart, FastAPI, Starlette, Robyn and Litestar integrations.


## Templates


Compile and render templates from strings, files and folders.


[PageTemplate](PageTemplate.md#chameleon_rs.PageTemplate)  
Constructor for the page template language, rendered by the Rust VM.

[PageTemplateFile](PageTemplateFile.md#chameleon_rs.PageTemplateFile)  
File-based constructor: the template is read from `filename` (or, with

[PageTextTemplate](PageTextTemplate.md#chameleon_rs.PageTextTemplate)  
Text-based template class: only `${...}` interpolation, no markup, nothing escaped.

[PageTextTemplateFile](PageTextTemplateFile.md#chameleon_rs.PageTextTemplateFile)  
File-based text template. Rendering returns `bytes`, encoded with the template's

[PageTemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader)  


## PageTemplate Methods


Methods for the PageTemplate class


[PageTemplate.__getitem__()](PageTemplate.__getitem__.md#chameleon_rs.PageTemplate.__getitem__)  

[PageTemplate.engine](PageTemplate.engine.md#chameleon_rs.PageTemplate.engine)  
Chameleon's expression engine factory for this template's expression types.

[PageTemplate.expression_parser](PageTemplate.expression_parser.md#chameleon_rs.PageTemplate.expression_parser)  
Chameleon's parser for this template's `expression_types` and `default_expression`.

[PageTemplate.builtins](PageTemplate.builtins.md#chameleon_rs.PageTemplate.builtins)  
The template builtins: names every expression can use (overridden by render keywords).

[PageTemplate.parse()](PageTemplate.parse.md#chameleon_rs.PageTemplate.parse)  

[PageTemplate.include](PageTemplate.include.md#chameleon_rs.PageTemplate.include)  
The whole template as a macro (`metal:use-macro="template"`).

[PageTemplate.render()](PageTemplate.render.md#chameleon_rs.PageTemplate.render)  
Render template to string.

[PageTemplate.render_async()](PageTemplate.render_async.md#chameleon_rs.PageTemplate.render_async)  
Render template to string, awaiting awaitable values (D9).

[PageTemplate.digest()](PageTemplate.digest.md#chameleon_rs.PageTemplate.digest)  


## Configuration


Process-wide settings for templates loaded by name, partials and Markdown.


[configure()](configure.md#chameleon_rs.configure)  
Configures chameleon-rs for the whole process: where templates loaded by name live, for

[core_version()](core_version.md#chameleon_rs.core_version)  
Version of the Rust core, for bug reports and for checking which build is loaded.


## Async


Opt-in concurrency for render_async. See also PageTemplate.render_async.


[concurrent()](concurrent.md#chameleon_rs.concurrent)  
Marks an async helper so that an asynchronous render awaits its calls concurrently


## Partials


Render one template inside another. Also chameleon_partials' API.


[render_partial](render_partial.md#chameleon_rs.render_partial)  

[partials.register_extensions()](partials.register_extensions.md#chameleon_rs.partials.register_extensions)  
chameleon_partials' setup call: configures the folder `render_partial` uses (only

[partials.extend_model()](partials.extend_model.md#chameleon_rs.partials.extend_model)  
Adds `render_partial` to a view model (chameleon_partials' helper; not needed with the

[partials.clear()](partials.clear.md#chameleon_rs.partials.clear)  
Forgets the folder partials use, for tests.

[partials.PartialRenderer](partials.PartialRenderer.md#chameleon_rs.partials.PartialRenderer)  
The `render_partial` callable: `chameleon_rs.render_partial` (no calling template)

[partials.HTML](partials.HTML.md#chameleon_rs.partials.HTML)  
Rendered markup, as `render_partial` returns it from Python: `__html__` gives the

[partials.PartialsException](partials.PartialsException.md#chameleon_rs.partials.PartialsException)  
Raised when partials are configured or used incorrectly: registering a folder that does


## Flask and Quart


chameleon-flask's API, built in. Imports Flask only when imported itself.


[flask.configure()](flask.configure.md#chameleon_rs.flask.configure)  
Configures where templates live and how they load: for the whole process (the same as

[flask.global_init()](flask.global_init.md#chameleon_rs.flask.global_init)  
chameleon-flask's setup call: configures the views' templates only (partials keep their

[flask.template()](flask.template.md#chameleon_rs.flask.template)  
Decorates a Flask or Quart view (sync or async) to render a template with what it

[flask.render()](flask.render.md#chameleon_rs.flask.render)  
Renders a template (path relative to the template folder) to a `str`.

[flask.response()](flask.response.md#chameleon_rs.flask.response)  
Renders a template to a `flask.Response`. (Unlike `@template`, `mimetype` is not a

[flask.not_found()](flask.not_found.md#chameleon_rs.flask.not_found)  
Ends the current `@template` view with a 404 page: `template_file`

[flask.error()](flask.error.md#chameleon_rs.flask.error)  
Ends the current `@template` view with an error page: `template_file` rendered with

[flask.clear()](flask.clear.md#chameleon_rs.flask.clear)  
Forgets the views' process-wide configuration, so a later `configure()` /

[flask.check_views()](flask.check_views.md#chameleon_rs.flask.check_views)  
Raises `chameleon_rs.web.DecoratorOrderError` if a route of `app` (blueprints


## FastAPI


fastapi-chameleon's API, built in. Imports FastAPI only when imported itself.


[fastapi.configure()](fastapi.configure.md#chameleon_rs.fastapi.configure)  
Configures where templates live and how they load, for the whole process (the same as

[fastapi.global_init()](fastapi.global_init.md#chameleon_rs.fastapi.global_init)  
fastapi-chameleon's setup call: configures the views' templates only (partials keep

[fastapi.template()](fastapi.template.md#chameleon_rs.fastapi.template)  
Decorates a FastAPI view (sync or async) to render a template with what it returns:

[fastapi.render()](fastapi.render.md#chameleon_rs.fastapi.render)  
Renders a template (path relative to the template folder) to a `str`.

[fastapi.render_async()](fastapi.render_async.md#chameleon_rs.fastapi.render_async)  
`render` as a coroutine: the template's `render_async`, which awaits awaitable

[fastapi.response()](fastapi.response.md#chameleon_rs.fastapi.response)  
Renders a template to a `fastapi.Response`. (As in fastapi-chameleon, every keyword

[fastapi.response_async()](fastapi.response_async.md#chameleon_rs.fastapi.response_async)  
`response` as a coroutine, rendering with `render_async`.

[fastapi.not_found()](fastapi.not_found.md#chameleon_rs.fastapi.not_found)  
Ends the current `@template` view with a 404 page: `template_file`

[fastapi.error()](fastapi.error.md#chameleon_rs.fastapi.error)  
Ends the current `@template` view with an error page: `template_file` rendered with

[fastapi.generic_error()](fastapi.generic_error.md#chameleon_rs.fastapi.generic_error)  
fastapi-chameleon's name and signature for `error()`.

[fastapi.clear()](fastapi.clear.md#chameleon_rs.fastapi.clear)  
Forgets the views' process-wide configuration, so a later `configure()` /

[fastapi.check_views()](fastapi.check_views.md#chameleon_rs.fastapi.check_views)  
Raises `chameleon_rs.web.DecoratorOrderError` if a route of `app` (sub-routers and


## Starlette


Jinja2Templates' API for chameleon-rs, and the decorator API. Imports Starlette only when imported itself.


[starlette.Templates](starlette.Templates.md#chameleon_rs.starlette.Templates)  
Starlette's `Jinja2Templates` for chameleon-rs templates.

[starlette.configure()](starlette.configure.md#chameleon_rs.starlette.configure)  
Configures where templates live and how they load, for the whole process (the same as

[starlette.global_init()](starlette.global_init.md#chameleon_rs.starlette.global_init)  
fastapi-chameleon's setup call, for Starlette: configures the views' templates only

[starlette.template()](starlette.template.md#chameleon_rs.starlette.template)  
Decorates a Starlette endpoint (a function, or a method of an `HTTPEndpoint` class;

[starlette.render()](starlette.render.md#chameleon_rs.starlette.render)  
Renders a template (path relative to the template folder) to a `str`.

[starlette.render_async()](starlette.render_async.md#chameleon_rs.starlette.render_async)  
`render` as a coroutine: the template's `render_async`, which awaits awaitable

[starlette.response()](starlette.response.md#chameleon_rs.starlette.response)  
Renders a template to an `HTMLResponse` (every keyword but `content_type` and

[starlette.response_async()](starlette.response_async.md#chameleon_rs.starlette.response_async)  
`response` as a coroutine, rendering with `render_async`.

[starlette.not_found()](starlette.not_found.md#chameleon_rs.starlette.not_found)  
Ends the current `@template` endpoint with a 404 page: `template_file` rendered with

[starlette.error()](starlette.error.md#chameleon_rs.starlette.error)  
Ends the current `@template` endpoint with an error page: `template_file` rendered

[starlette.generic_error()](starlette.generic_error.md#chameleon_rs.starlette.generic_error)  
fastapi-chameleon's name and signature for `error()`.

[starlette.clear()](starlette.clear.md#chameleon_rs.starlette.clear)  
Forgets the views' process-wide configuration, so a later `configure()` /

[starlette.check_views()](starlette.check_views.md#chameleon_rs.starlette.check_views)  
Raises `chameleon_rs.web.DecoratorOrderError` if a route of `app` (a Starlette app


## Robyn


chameleon-robyn's API, built in. Imports Robyn only when imported itself.


[robyn.configure()](robyn.configure.md#chameleon_rs.robyn.configure)  
Configures where templates live and how they load, for the whole process (the same as

[robyn.global_init()](robyn.global_init.md#chameleon_rs.robyn.global_init)  
chameleon-robyn's setup call: configures the handlers' templates only (partials keep

[robyn.template()](robyn.template.md#chameleon_rs.robyn.template)  
Decorates a Robyn route handler (sync or async) to render a template with what it

[robyn.render()](robyn.render.md#chameleon_rs.robyn.render)  
Renders a template (path relative to the template folder) to a `str` (no response):

[robyn.render_async()](robyn.render_async.md#chameleon_rs.robyn.render_async)  
`render` as a coroutine: the template's `render_async`, which awaits awaitable

[robyn.response()](robyn.response.md#chameleon_rs.robyn.response)  
Renders a template into a `robyn.Response` with \`\`Content-Type: {content_type};

[robyn.response_async()](robyn.response_async.md#chameleon_rs.robyn.response_async)  
`response` as a coroutine, rendering with `render_async`.

[robyn.not_found()](robyn.not_found.md#chameleon_rs.robyn.not_found)  
Ends the current `@template` handler with a 404 page: `template_file`

[robyn.error()](robyn.error.md#chameleon_rs.robyn.error)  
Ends the current `@template` handler with an error page: `template_file` rendered as

[robyn.generic_error()](robyn.generic_error.md#chameleon_rs.robyn.generic_error)  
fastapi-chameleon's name and signature for `error()`.

[robyn.clear()](robyn.clear.md#chameleon_rs.robyn.clear)  
Forgets the handlers' process-wide configuration, so a later `configure()` /

[robyn.check_views()](robyn.check_views.md#chameleon_rs.robyn.check_views)  
Raises `chameleon_rs.web.DecoratorOrderError` if a route of `app` (a Robyn app, with

[robyn.ChameleonTemplate](robyn.ChameleonTemplate.md#chameleon_rs.robyn.ChameleonTemplate)  
Chameleon templates through Robyn's `TemplateInterface`, as its `JinjaTemplate`:


## Litestar


Litestar's template engine interface for chameleon-rs, and the decorator API. Imports Litestar only when imported itself.


[litestar.ChameleonEngine](litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine)  
Litestar's `TemplateEngineProtocol` for chameleon-rs templates:

[litestar.ChameleonTemplate](litestar.ChameleonTemplate.md#chameleon_rs.litestar.ChameleonTemplate)  
A page template as Litestar's `TemplateProtocol`: `render(**context)` renders it with

[litestar.Template](litestar.Template.md#chameleon_rs.litestar.Template)  
Litestar's `Template` response, served as `text/html` when Litestar cannot guess the

[litestar.configure()](litestar.configure.md#chameleon_rs.litestar.configure)  
Configures where templates live and how they load, for the whole process (the same as

[litestar.global_init()](litestar.global_init.md#chameleon_rs.litestar.global_init)  
The other integrations' setup call, for Litestar: configures the handlers' templates only

[litestar.template()](litestar.template.qmd#chameleon_rs.litestar.template)  
Decorates a Litestar route handler function (sync or async; a `Controller` method too)

[litestar.render()](litestar.render.md#chameleon_rs.litestar.render)  
Renders a template (path relative to the template folder) to a `str`.

[litestar.render_async()](litestar.render_async.md#chameleon_rs.litestar.render_async)  
`render` as a coroutine: the template's `render_async`, which awaits awaitable

[litestar.response()](litestar.response.md#chameleon_rs.litestar.response)  
Renders a template into a `litestar.Response` (every keyword but `content_type` and

[litestar.response_async()](litestar.response_async.md#chameleon_rs.litestar.response_async)  
`response` as a coroutine, rendering with `render_async`.

[litestar.not_found()](litestar.not_found.md#chameleon_rs.litestar.not_found)  
Ends the current `@template` handler with a 404 page: `template_file` rendered with

[litestar.error()](litestar.error.md#chameleon_rs.litestar.error)  
Ends the current `@template` handler with an error page: `template_file` rendered

[litestar.generic_error()](litestar.generic_error.md#chameleon_rs.litestar.generic_error)  
fastapi-chameleon's name and signature for `error()`.

[litestar.clear()](litestar.clear.md#chameleon_rs.litestar.clear)  
Forgets the handlers' process-wide configuration, so a later `configure()` /

[litestar.check_views()](litestar.check_views.md#chameleon_rs.litestar.check_views)  
Raises `chameleon_rs.web.DecoratorOrderError` if a route of `app` (a Litestar app or


## Errors and web helpers


Exceptions, and the pieces shared by the framework integrations.


[TemplateError](TemplateError.md#chameleon_rs.TemplateError)  
An error raised by Chameleon.

[web.TemplateError](web.TemplateError.md#chameleon_rs.web.TemplateError)  
A template integration was configured or used incorrectly: no template folder

[web.ErrorPage](web.ErrorPage.md#chameleon_rs.web.ErrorPage)  
Raised inside a decorated view to answer with an error page: the decorator renders

[web.NotFound](web.NotFound.md#chameleon_rs.web.NotFound)  
Raised by `not_found()`: the decorator renders `template_file` (`errors/404.pt`

[web.TemplateNotFound](web.TemplateNotFound.md#chameleon_rs.web.TemplateNotFound)  
No template of that name, or a name reaching outside the template folder: raised by

[web.DecoratorOrderError](web.DecoratorOrderError.md#chameleon_rs.web.DecoratorOrderError)  
A view was registered with its framework before `@template` wrapped it (`@template`

[web.model_of()](web.model_of.md#chameleon_rs.web.model_of)  
The template model a view returned: a `dict` as is; any other `Mapping` as a


## Compatibility


Make `import chameleon` resolve to chameleon-rs, or check both engines side by side.


[compat.install()](compat.install.md#chameleon_rs.compat.install)  
Makes `import chameleon` (and its submodules) resolve to chameleon-rs and, with

[shadow.install()](shadow.install.md#chameleon_rs.shadow.install)  
Starts shadow mode: from now on, a `sample_rate` share of the outermost renders of

[shadow.uninstall()](shadow.uninstall.md#chameleon_rs.shadow.uninstall)  
Stops shadow mode, restoring stock Chameleon's `render` methods.

[shadow.ShadowDifference](shadow.ShadowDifference.md#chameleon_rs.shadow.ShadowDifference)  
One render whose outcome differs between the engines.
