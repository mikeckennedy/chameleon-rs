# chameleon-rs

A Rust implementation of the [Chameleon](https://github.com/malthe/chameleon) page template
engine for Python, aiming to be a drop-in replacement: same template syntax, same output,
same API, much faster rendering.

Status: early development. See [docs/plan.md](docs/plan.md) for the build plan and
[docs/architecture.md](docs/architecture.md) for the design decisions.

Switching an application over is one line at startup, before anything imports Chameleon:

```python
import chameleon_rs.compat

chameleon_rs.compat.install()  # `import chameleon`, `chameleon_partials`, `chameleon_flask` give chameleon-rs
```

Built in, with no extra package:

- **Partials:** every template can call `${render_partial('shared/card.pt', item=item)}`; the
  partial renders straight into the page. `chameleon_rs.configure(template_folder='templates')`
  says where templates live (partials not found there are also looked for on the calling
  template's search path and next to it; `partial_search='registered'` turns that off). `chameleon_partials`' API (`register_extensions`,
  `extend_model`, `render_partial`) keeps working. See `chameleon_rs.partials`.
- **Flask and Quart:** `chameleon_rs.flask` has chameleon-flask's API (`configure` /
  `global_init`, `@template`, `render`, `response`, `not_found`, `error`, `clear`) and imports
  Flask only when it is imported itself. Views may return a dict, any mapping, a dataclass or
  a Pydantic model.

## Async rendering

`await template.render_async(**kw)` renders like `render()`, except that a value that is
awaitable (a coroutine, task or future) is awaited where the template uses it, without
blocking the event loop:

```python
async def latest_episodes():
    return await db.episodes.find().to_list()

html = await page.render_async(episodes=latest_episodes(), user=user)
```

```html
<li tal:repeat="e episodes">${e.title}</li>
```

- Only an expression's final value is awaited: `${episodes}` and `tal:repeat="e episodes"`
  await, `episodes.count` is an attribute of the coroutine (an error, as in a sync render).
  A condition is tested on the awaited value.
- Macros, slots, `render_partial` partials and `tal:on-error` all work across the wait; an
  error raised by an awaited value is caught by `tal:on-error` like any other; cancelling the
  task cancels the value being awaited.
- With no awaitable values the output is exactly `render()`'s (tested against every Chameleon
  fixture and the render fuzzers).
- Async iterables in `tal:repeat` are not supported (as in a sync render, they are not
  iterable): collect them first, `[x async for x in source]` in an async helper.
- `chameleon_rs.flask`: async views (Flask's async views, Quart) render with
  `render_async`; `render_async()` and `response_async()` are the coroutine versions of
  `render()` and `response()`.

Opt-in concurrency (it changes the order in which helpers run):

- `@chameleon_rs.concurrent` on an async helper: its calls written as `${...}` are awaited
  concurrently with the rest of the page, each result filled in at the end.
- `PageTemplate(..., concurrent=True)` (or `@template(..., concurrent=True)` in Flask):
  `render_partial(...)` calls of the page whose arguments are awaitable render concurrently.

Experimental: `render_async(..., offload=True)` (or `@template(..., offload=True)`) renders in a
worker thread. On free-threaded Python (3.14t) renders then run in parallel with the event loop;
on a GIL build it helps only pages dominated by long text. Measurements: `docs/plan.md`, Phase 11.

## Development

```sh
make dev     # set up venv with the reference Chameleon and build the extension
make test    # Rust tests, Python tests, Chameleon's own suite on the reference engine
```

Licensed under Chameleon's license (see `LICENSE.txt` and `COPYRIGHT.txt`); this project
ports Chameleon's behavior and reuses its test suite.
