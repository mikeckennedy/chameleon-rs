---
name: chameleon-rs
description: >
  Chameleon templates, modernized: a Rust engine that runs Chameleon templates unchanged, several times faster, with async rendering and built-in framework integrations. Use when writing Python code that uses the chameleon_rs package.
license: LicenseRef-Chameleon
compatibility: Requires Python >=3.12.
---

# chameleon-rs

Chameleon templates, modernized: a Rust engine that runs Chameleon templates unchanged, several times faster, with async rendering and built-in framework integrations.

## Installation

```bash
pip install chameleon-rs
```

## When to use what

| Need | Use |
|------|-----|
| Run an existing Chameleon app on chameleon-rs with no code changes | `chameleon_rs.compat.install() at the top of the entry point` |
| Render a template string | `chameleon_rs.PageTemplate(source)(**model)` |
| Load templates by name from folders | `chameleon_rs.PageTemplateLoader([folder])['page.pt'](**model)` |
| Insert a reusable HTML fragment | `${render_partial('shared/fragment.pt', key=value)}` |
| Get a rendered fragment as a string from Python (HTMX, email, tests) | `chameleon_rs.render_partial(path, **model).html_text` |
| Serve HTMX: a full page on first load, one fragment on an HTMX request | `Put the fragment in its own template, include it with ${render_partial(...)}; the view returns response('fragment.pt', ...) when the HX-Request header is set (and HX-History-Restore-Request is not), or @template('fragment.pt') on an HTMX-only route` |
| Render Chameleon templates from Flask or Quart views | `chameleon_rs.flask: configure(folder), then @template('page.pt') on the view` |
| Render Chameleon templates from FastAPI views | `chameleon_rs.fastapi: configure(folder, app=app), then @template('page.pt') below @app.get` |
| Replace Starlette's Jinja2Templates | `chameleon_rs.starlette.Templates(directory=folder); await templates.TemplateResponse_async(request, 'page.pt', context) in async endpoints` |
| Render Chameleon templates from Robyn handlers | `chameleon_rs.robyn: configure(folder, app=app), then @template('page.pt') below @app.get (Robyn has wheels for Python 3.10-3.14)` |
| Render without blocking the event loop | `await template.render_async(**model); in the framework integrations, an async def view with @template does this` |
| Run several slow async helpers of one page at the same time | `@chameleon_rs.concurrent on the helper, called as a whole ${helper(...)}; or PageTemplate(..., concurrent=True) / @template(..., concurrent=True) for partials with awaitable arguments` |
| Convert Markdown to HTML in a template | `chameleon_rs.configure(markdown=True), then ${markdown: post.body}; profiles such as {'md-trusted': {'raw_html': 'allow'}} for different content` |
| Render Chameleon templates from Pyramid views | `pyramid_chameleon unchanged (config.include('pyramid_chameleon'), renderer='templates/page.pt'), with chameleon_rs.compat.install() before it is imported` |
| Use Chameleon templates as Litestar's template engine | `TemplateConfig(engine=chameleon_rs.litestar.ChameleonEngine, directory=folder); return chameleon_rs.litestar.Template('page.pt', context=...) (served as text/html; renders synchronously)` |
| Render Chameleon templates from async Litestar handlers without blocking the loop | `chameleon_rs.litestar: configure(folder, app=app), then @template('page.pt') below @get` |

## API overview

### Templates

Compile and render templates from strings, files and folders.

- `PageTemplate`: Constructor for the page template language, rendered by the Rust VM
- `PageTemplateFile`: File-based constructor: the template is read from ``filename`` (or, with
- `PageTextTemplate`: Text-based template class: only ``${...}`` interpolation, no markup, nothing escaped
- `PageTextTemplateFile`: File-based text template. Rendering returns ``bytes``, encoded with the template's
- `PageTemplateLoader`

### PageTemplate Methods

Methods for the PageTemplate class

- `PageTemplate.__getitem__`
- `PageTemplate.builtins`
- `PageTemplate.parse`
- `PageTemplate.include`
- `PageTemplate.render`
- `PageTemplate.render_async`
- `PageTemplate.digest`

### Configuration

Process-wide settings for templates loaded by name, partials and Markdown.

- `configure`: Configures chameleon-rs for the whole process: where templates loaded by name live, for
- `core_version`: Version of the Rust core, for bug reports and for checking which build is loaded

### Async

Opt-in concurrency for render_async. See also PageTemplate.render_async.

- `concurrent`: Marks an async helper so that an asynchronous render awaits its calls concurrently

### Partials

Render one template inside another. Also chameleon_partials' API.

- `render_partial`: The ``render_partial`` callable: ``chameleon_rs.render_partial`` (no calling template)
- `partials.register_extensions`
- `partials.extend_model`
- `partials.clear`
- `partials.PartialRenderer`
- `partials.HTML`
- `partials.PartialsException`

### Flask and Quart

chameleon-flask's API, built in. Imports Flask only when imported itself.

- `flask.configure`
- `flask.global_init`
- `flask.template`
- `flask.render`
- `flask.response`
- `flask.not_found`
- `flask.error`
- `flask.clear`
- `flask.check_views`

### FastAPI

fastapi-chameleon's API, built in. Imports FastAPI only when imported itself.

- `fastapi.configure`
- `fastapi.global_init`
- `fastapi.template`
- `fastapi.render`
- `fastapi.render_async`
- `fastapi.response`
- `fastapi.response_async`
- `fastapi.not_found`
- `fastapi.error`
- `fastapi.generic_error`
- `fastapi.clear`
- `fastapi.check_views`

### Starlette

Jinja2Templates' API for chameleon-rs, and the decorator API. Imports Starlette only when imported itself.

- `starlette.Templates`
- `starlette.configure`
- `starlette.global_init`
- `starlette.template`
- `starlette.render`
- `starlette.render_async`
- `starlette.response`
- `starlette.response_async`
- `starlette.not_found`
- `starlette.error`
- `starlette.generic_error`
- `starlette.clear`
- `starlette.check_views`

### Robyn

chameleon-robyn's API, built in. Imports Robyn only when imported itself.

- `robyn.configure`
- `robyn.global_init`
- `robyn.template`
- `robyn.render`
- `robyn.render_async`
- `robyn.response`
- `robyn.response_async`
- `robyn.not_found`
- `robyn.error`
- `robyn.generic_error`
- `robyn.clear`
- `robyn.check_views`
- `robyn.ChameleonTemplate`

### Litestar

Litestar's template engine interface for chameleon-rs, and the decorator API. Imports Litestar only when imported itself.

- `litestar.ChameleonEngine`
- `litestar.ChameleonTemplate`
- `litestar.Template`
- `litestar.configure`
- `litestar.global_init`
- `litestar.template`
- `litestar.render`
- `litestar.render_async`
- `litestar.response`
- `litestar.response_async`
- `litestar.not_found`
- `litestar.error`
- `litestar.generic_error`
- `litestar.clear`
- `litestar.check_views`

### Errors and web helpers

Exceptions, and the pieces shared by the framework integrations.

- `TemplateError`: An error raised by Chameleon
- `web.TemplateError`
- `web.ErrorPage`
- `web.NotFound`
- `web.TemplateNotFound`
- `web.DecoratorOrderError`
- `web.model_of`

### Compatibility

Make `import chameleon` resolve to chameleon-rs, or check both engines side by side.

- `compat.install`
- `shadow.install`
- `shadow.uninstall`
- `shadow.ShadowDifference`

## Gotchas

1. chameleon_rs.compat.install() must run before anything imports chameleon, chameleon_partials, chameleon_flask, fastapi_chameleon or chameleon_robyn; a module already imported keeps stock Chameleon's objects.
2. render_partial looks in the configured template folder, then on the calling template's search path and in its folder and each folder above it. chameleon_rs.configure(partial_search='registered') restores chameleon_partials' lookup (configured folder only).
3. render_partial is a builtin in every template; you don't need extend_model() or a BeforeRender subscriber (both still work).
4. configure() applies on every call (there is no cache_init); the first call must give template_folder.
5. template.source does not exist as Python code: with keep_source=True it is a dump of the compiled program.
6. Custom expression types that return Python AST work through a bridge at fallback speed; types that depend on Chameleon's compiler internals are not supported.
7. render_async awaits only an expression's final value: ${episodes} and tal:repeat="e episodes" await a coroutine, ${episodes.count} and ${len(episodes)} do not.
8. markdown: expressions exist only after chameleon_rs.configure(markdown=True) (or a dict of profiles), called before the templates that use them compile.
9. Pyramid has no chameleon_rs module: pyramid_chameleon runs unchanged after chameleon_rs.compat.install() at the top of the package whose main() builds the app.

## Best practices

- Call chameleon_rs.configure(template_folder=..., auto_reload=dev_mode) once at startup, before the first render.
- To switch an existing app, add `import chameleon_rs.compat; chameleon_rs.compat.install()` as the first lines of the entry point, and leave the rest of the code unchanged.
- Prefer ${render_partial('shared/card.pt', item=item)} as a whole expression: it renders as a native frame of the page, with no Python call.
- Build template folder paths with (Path(__file__).parent / 'templates').as_posix().
- In tests, reset with chameleon_rs.partials.clear() (or chameleon_rs.flask.clear(), chameleon_rs.fastapi.clear(), chameleon_rs.starlette.clear(), chameleon_rs.robyn.clear(), chameleon_rs.litestar.clear()) between configurations.
- Put @template below the route decorator (@app.get, @router.get, @app.route, Litestar's @get); pass app= to configure() so a misordered view fails at startup with DecoratorOrderError.
- With Litestar, return chameleon_rs.litestar.Template (not litestar.response.Template) for .pt templates: Litestar guesses the media type from the suffix and serves .pt as text/plain.

## What chameleon-rs is

Chameleon's template language on a new engine written in Rust (`import chameleon_rs`, PyPI
`chameleon-rs`). Same template language (TAL, METAL, i18n, `${...}`), same Python API, the
same output byte for byte, several times faster. It adds async rendering (`render_async`),
built-in Flask/Quart, FastAPI, Starlette, Robyn and Litestar integrations, built-in partials,
`markdown:` expressions, and free-threaded Python support. It has no runtime dependencies, and
stock Chameleon doesn't need to be installed.

## Switching an existing Chameleon app

Add these lines at the very top of the app's entry point, above every other import:

```python
import chameleon_rs.compat

chameleon_rs.compat.install()
```

After that, `import chameleon`, `import chameleon_partials`, `import chameleon_flask`,
`import fastapi_chameleon` and `import chameleon_robyn` give chameleon-rs's modules. Don't change templates or other code. If `chameleon` was imported
before `install()`, objects taken from it stay stock Chameleon objects, so move the call
earlier.

## A new app

```python
from pathlib import Path

import chameleon_rs
from chameleon_rs import PageTemplateLoader

folder = (Path(__file__).parent / 'templates').as_posix()
chameleon_rs.configure(template_folder=folder, auto_reload=dev_mode)  # once, at startup
templates = PageTemplateLoader([folder])

html = templates['home/index.pt'](title='Home', videos=videos)
```

Every template has `render_partial` built in, with no `extend_model` and no `BeforeRender`
subscriber:

```html
<div tal:repeat="video videos">
  ${render_partial('shared/video_card.pt', video=video)}
</div>
```

A partial sees only the keyword arguments passed to it. A path is looked for in the
configured folder, then next to the calling template and in the folders above it.

## Flask and Quart

```python
import chameleon_rs.flask as chameleon_flask

chameleon_flask.configure(template_folder=folder, auto_reload=dev_mode)

@app.get('/')
@chameleon_flask.template('home/index.pt')
def index():
    return {'message': 'Hello'}   # a dict, mapping, dataclass or Pydantic model
```

`chameleon_flask.not_found()` and `chameleon_flask.error(template, status_code)` end a
decorated view with an error page. `chameleon_rs.render_partial(path, **model).html_text`
returns a fragment as a string, for HTMX responses.

## Other frameworks

`chameleon_rs.fastapi`, `chameleon_rs.starlette`, `chameleon_rs.robyn` and
`chameleon_rs.litestar` have the same decorator API (`configure(template_folder=..., app=app)`,
`@template('page.pt')` below the route decorator, `not_found`, `error`, `render`, `response`,
`render_async`, `response_async`). Also:

- Starlette: `chameleon_rs.starlette.Templates(directory=...)` is a drop-in for
  `Jinja2Templates`; use `await templates.TemplateResponse_async(request, name, context)` in
  async endpoints.
- Litestar: `TemplateConfig(engine=chameleon_rs.litestar.ChameleonEngine, directory=...)`, and
  return `chameleon_rs.litestar.Template` (serves `.pt` as `text/html`).
- Pyramid: no module; pyramid_chameleon runs unchanged after `chameleon_rs.compat.install()`.

## Async rendering

`await template.render_async(**model)` awaits an awaitable model value (coroutine, task,
future) where the template uses it, without blocking the event loop. Only an expression's final
value is awaited: `${episodes}` and `tal:repeat="e episodes"` await, `${episodes.count}` does
not. An `async def` view with `@template` renders this way in every integration. Opt-in:
`@chameleon_rs.concurrent` helpers and `concurrent=True` partials run concurrently.

## Markdown

```python
chameleon_rs.configure(markdown=True)   # at startup, before templates compile
```

```html
<article>${markdown: post.body}</article>
```

Raw HTML in the text is escaped by default; a profile such as
`configure(markdown={'markdown': {}, 'md-trusted': {'raw_html': 'allow'}})` adds an
`md-trusted:` prefix for trusted content.

## Known differences from Chameleon

- `template.source` is a dump of the compiled program, not Python code.
- Custom expression types that return Python AST work through a bridge, at the speed of an
  ordinary Python call.
- `CHAMELEON_CACHE` writes nothing to disk; compiling is fast enough not to need it.
- `chameleon.nodes`, `chameleon.program`, `chameleon.zpt.program` and `chameleon.benchmark`
  don't exist.


## Resources

- [Full documentation](https://mkennedy.codes/docs/chameleon-rs/)
- [llms.txt](llms.txt) — Indexed API reference for LLMs
- [llms-full.txt](llms-full.txt) — Comprehensive documentation for LLMs
- [Source code](https://github.com/mikeckennedy/chameleon-rust)
