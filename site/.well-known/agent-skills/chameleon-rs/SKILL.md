---
name: chameleon-rs
description: >
  A Rust implementation of the Chameleon page template engine, compatible with Chameleon's syntax and API. Use when writing Python code that uses the chameleon_rs package.
license: LicenseRef-Chameleon
compatibility: Requires Python >=3.12.
---

# chameleon-rs

A Rust implementation of the Chameleon page template engine, compatible with Chameleon's syntax and API.

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
| Render Chameleon templates from Flask or Quart views | `chameleon_rs.flask: configure(folder), then @template('page.pt') on the view` |

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

Process-wide settings for templates loaded by name.

- `configure`: Configures chameleon-rs for the whole process: where templates loaded by name live, for
- `core_version`: Version of the Rust core, for bug reports and for checking which build is loaded

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

### Errors and web helpers

Exceptions, and the pieces shared by the framework integrations.

- `TemplateError`: An error raised by Chameleon
- `web.TemplateError`
- `web.ErrorPage`
- `web.NotFound`
- `web.model_of`

### Compatibility

Make `import chameleon` resolve to chameleon-rs, or check both engines side by side.

- `compat.install`
- `shadow.install`
- `shadow.uninstall`
- `shadow.ShadowDifference`

## Gotchas

1. chameleon_rs.compat.install() must run before anything imports chameleon, chameleon_partials or chameleon_flask; a module already imported keeps stock Chameleon's objects.
2. render_partial looks in the configured template folder, then on the calling template's search path and in its folder and each folder above it. chameleon_rs.configure(partial_search='registered') restores chameleon_partials' lookup (configured folder only).
3. render_partial is a builtin in every template; you don't need extend_model() or a BeforeRender subscriber (both still work).
4. configure() applies on every call (there is no cache_init); the first call must give template_folder.
5. template.source does not exist as Python code: with keep_source=True it is a dump of the compiled program.
6. Custom expression types that return Python AST work through a bridge at fallback speed; types that depend on Chameleon's compiler internals are not supported.

## Best practices

- Call chameleon_rs.configure(template_folder=..., auto_reload=dev_mode) once at startup, before the first render.
- To switch an existing app, add `import chameleon_rs.compat; chameleon_rs.compat.install()` as the first lines of the entry point, and leave the rest of the code unchanged.
- Prefer ${render_partial('shared/card.pt', item=item)} as a whole expression: it renders as a native frame of the page, with no Python call.
- Build template folder paths with (Path(__file__).parent / 'templates').as_posix().
- In tests, reset with chameleon_rs.partials.clear() (or chameleon_rs.flask.clear()) between configurations.

## What chameleon-rs is

A Rust implementation of the Chameleon page template engine (`import chameleon_rs`, PyPI
`chameleon-rs`). Same template language (TAL, METAL, i18n, `${...}`), same Python API, the
same output byte for byte, and faster rendering. It has no runtime dependencies, and stock
Chameleon doesn't need to be installed.

## Switching an existing Chameleon app

Add these lines at the very top of the app's entry point, above every other import:

```python
import chameleon_rs.compat

chameleon_rs.compat.install()
```

After that, `import chameleon`, `import chameleon_partials` and `import chameleon_flask` give
chameleon-rs's modules. Don't change templates or other code. If `chameleon` was imported
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
