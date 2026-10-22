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
