# API Reference


The public API of chameleon-rs. It mirrors Chameleon's (PageTemplate, PageTemplateFile, PageTemplateLoader, …), plus built-in partials, process-wide configuration and the Flask/Quart integration.


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

[PageTemplate.builtins](PageTemplate.builtins.md#chameleon_rs.PageTemplate.builtins)  
The template builtins: names every expression can use (a render keyword of the

[PageTemplate.parse()](PageTemplate.parse.md#chameleon_rs.PageTemplate.parse)  

[PageTemplate.include](PageTemplate.include.md#chameleon_rs.PageTemplate.include)  
The whole template as a macro (`metal:use-macro="template"`): Chameleon's

[PageTemplate.render()](PageTemplate.render.md#chameleon_rs.PageTemplate.render)  
Render template to string.

[PageTemplate.render_async()](PageTemplate.render_async.md#chameleon_rs.PageTemplate.render_async)  
Render template to string, awaiting awaitable values (D9).

[PageTemplate.digest()](PageTemplate.digest.md#chameleon_rs.PageTemplate.digest)  


## Configuration


Process-wide settings for templates loaded by name.


[configure()](configure.md#chameleon_rs.configure)  
Configures chameleon-rs for the whole process: where templates loaded by name live, for

[core_version()](core_version.md#chameleon_rs.core_version)  
Version of the Rust core, for bug reports and for checking which build is loaded.


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
