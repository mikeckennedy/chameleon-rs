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

folder = Path(__file__).parent / 'templates'
chameleon_rs.configure(template_folder=folder, auto_reload=dev_mode, strict_tags=True)  # once, at startup
templates = PageTemplateLoader(folder, strict_tags=True)

html = templates['home/index.pt'](title='Home', videos=videos)
```

`strict_tags=True` (or `CHAMELEON_STRICT_TAGS=1` in the environment) catches unclosed elements
with TAL/METAL/i18n attributes and misspelled attribute names (such as `tal:deifne`) at compile
time instead of silently ignoring them. `strict_names=True` (or `CHAMELEON_STRICT_NAMES=1`)
makes the framework integrations' `@template`, `render` and `response` reject template names
that could escape `template_folder`.

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
import chameleon_rs.flask as chameleon

chameleon.configure(template_folder=folder, auto_reload=dev_mode)

@app.get('/')
@chameleon.template('home/index.pt')
def index():
    return {'message': 'Hello'}   # a dict, mapping, dataclass or Pydantic model
```

`chameleon.not_found()` and `chameleon.error(template, status_code)` end a
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
- Pyramid and Zope / Plone: no module; `pyramid_chameleon`, `z3c.pt` and
  `Products.PageTemplates` run unchanged after `chameleon_rs.compat.install()`.

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

## Known differences and additions over Chameleon

- `strict_tags=True` (`CHAMELEON_STRICT_TAGS=1`) reports unclosed tags carrying TAL/METAL/i18n
  attributes and unknown `tal:`/`metal:`/`i18n:` attributes as `ParseError` at the offending tag.
- `chameleon_rs.template.HTML5_BOOLEAN_ATTRIBUTES` provides the full WHATWG HTML5 boolean
  attribute set (`hidden`, `required`, `novalidate`, `inert`, ...) for `boolean_attributes=...`.
- `repeat.item.first('attr')` and `repeat.item.last('attr')` detect group boundaries in
  `tal:repeat` loops.
- `template.source` is a dump of the compiled program, not Python code.
- Custom expression types that return Python AST work through a bridge, at the speed of an
  ordinary Python call.
- `CHAMELEON_CACHE` writes nothing to disk; compiling is fast enough not to need it.
- `chameleon.nodes`, `chameleon.program`, `chameleon.zpt.program` and `chameleon.benchmark`
  don't exist.
