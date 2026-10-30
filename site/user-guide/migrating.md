# Migrating from the helper libraries

chameleon-rs builds in what chameleon_partials, chameleon-flask, fastapi-chameleon and chameleon-robyn provide, and has its own Litestar engine. With [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install) you don't have to change anything: the old imports give the built-ins (see [Switching from Chameleon](switching-from-chameleon.md) for Chameleon itself). This page is for when you want to move to the new names, and drop the old setup code that's no longer needed.


# At a glance

| Before | After |
|----|----|
| `from chameleon import PageTemplateLoader` | `from chameleon_rs import PageTemplateLoader` |
| `chameleon_partials.register_extensions(folder, auto_reload=dev)` and `chameleon_flask.global_init(folder, auto_reload=dev)` | One call: `chameleon_rs.configure(template_folder=folder, auto_reload=dev)` |
| `return chameleon_partials.extend_model(model)` in every view | `return model`: [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) is in every template |
| A Pyramid `BeforeRender` subscriber that adds [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) | Remove it |
| `import chameleon_flask` | `import chameleon_rs.flask as chameleon` (or `as chameleon_flask`) |
| `import fastapi_chameleon` | `import chameleon_rs.fastapi as chameleon` (or `as fastapi_chameleon`) |
| `import chameleon_robyn` | `import chameleon_rs.robyn as chameleon` (or `as chameleon_robyn`) |
| `from litestar_chameleon import ChameleonTemplateEngine` | `from chameleon_rs.litestar import ChameleonEngine`, and return [chameleon_rs.litestar.Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) |
| `chameleon_partials.render_partial(path, **m).html_text` | `chameleon_rs.render_partial(path, **m).html_text` |
| `except chameleon_partials.PartialsException`, `except chameleon_flask.exceptions.FlaskChameleonException` | Still work. `except chameleon_rs.web.TemplateError` catches both |
| `has_registered_extensions = False` in test teardown | [chameleon_rs.partials.clear()](../reference/partials.clear.md#chameleon_rs.partials.clear) |


# chameleon_partials

``` python
# Before
import chameleon_partials

chameleon_partials.register_extensions(template_folder, auto_reload=dev_mode)


def index(request):
    model = {'videos': videos}
    return chameleon_partials.extend_model(model)
```

``` python
# After
import chameleon_rs

chameleon_rs.configure(template_folder=template_folder, auto_reload=dev_mode)


def index(request):
    return {'videos': videos}
```

Templates don't change. `${render_partial('shared/partials/video.pt', video=v)}` works as before, and runs natively.

Things to know:

- **Every [configure()](../reference/configure.md#chameleon_rs.configure) call applies.** [register_extensions](../reference/partials.register_extensions.md#chameleon_rs.partials.register_extensions) ignored a second call unless you passed `cache_init=False`. [register_extensions](../reference/partials.register_extensions.md#chameleon_rs.partials.register_extensions) itself still behaves the old way.
- **Partial paths can also be relative to the calling template.** A path that isn't in the template folder is looked for next to the calling template, where chameleon_partials would raise. Templates that worked before render exactly the same. `configure(partial_search='registered')` turns the extra lookup off.
- **Configuring is thread-safe.** Configuration is swapped in whole, so a render never sees half of one.


# chameleon-flask

``` python
# Before
import chameleon_flask

chameleon_flask.global_init(template_folder, auto_reload=dev_mode)
```

``` python
# After
import chameleon_rs.flask as chameleon

chameleon.configure(template_folder=template_folder, auto_reload=dev_mode)
```

Views, `@template`, [render](../reference/flask.render.md#chameleon_rs.flask.render), [response](../reference/flask.response.md#chameleon_rs.flask.response) and [not_found](../reference/flask.not_found.md#chameleon_rs.flask.not_found) stay the same. To leave existing call sites as they are, import it `as chameleon_flask` instead. New in chameleon-rs:

- [configure()](../reference/configure.md#chameleon_rs.configure) also sets the partials folder, so you need only one setup call.
- `error(template_file, status_code=500, **data)` renders any error page.
- Views can return mappings, dataclasses and Pydantic models, not only `dict`.
- `configure(..., app=app)` gives one app its own configuration.

See [Flask and Quart](flask.md).


# fastapi-chameleon

``` python
import fastapi_chameleon
```

becomes

``` python
import chameleon_rs.fastapi as chameleon
```

Import it `as fastapi_chameleon` to leave existing call sites as they are, or keep `import fastapi_chameleon` unchanged with [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install). Every name keeps working ([global_init](../reference/flask.global_init.md#chameleon_rs.flask.global_init), `@template`, [render](../reference/flask.render.md#chameleon_rs.flask.render), [response](../reference/flask.response.md#chameleon_rs.flask.response), [not_found](../reference/flask.not_found.md#chameleon_rs.flask.not_found), [generic_error](../reference/robyn.generic_error.md#chameleon_rs.robyn.generic_error), [clear](../reference/flask.clear.md#chameleon_rs.flask.clear), the exception classes), and its test suite passes unmodified on the built-in. Differences:

- Async views render with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async): an awaitable model value is awaited where the template uses it.
- `configure(..., app=app)` (or `global_init(..., app=app)`) checks at startup that every `@template` sits below its route decorator.
- [error()](../reference/flask.error.md#chameleon_rs.flask.error), [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async), [response_async()](../reference/robyn.response_async.md#chameleon_rs.robyn.response_async), and `status_code=` on `@template`.

See [FastAPI](fastapi.md).


# chameleon-robyn

``` python
import chameleon_robyn
```

becomes

``` python
import chameleon_rs.robyn as chameleon
```

Import it `as chameleon_robyn` to leave existing call sites as they are, or keep `import chameleon_robyn` unchanged with [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install). Every name keeps working ([global_init](../reference/flask.global_init.md#chameleon_rs.flask.global_init), `@template`, [render](../reference/flask.render.md#chameleon_rs.flask.render), [response](../reference/flask.response.md#chameleon_rs.flask.response), [not_found](../reference/flask.not_found.md#chameleon_rs.flask.not_found), [clear](../reference/flask.clear.md#chameleon_rs.flask.clear), [ChameleonTemplate](../reference/robyn.ChameleonTemplate.md#chameleon_rs.robyn.ChameleonTemplate), `__response_callback__`, the exception classes), and its test suite passes unmodified on the built-in. Differences:

- Async handlers render with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async): an awaitable model value is awaited where the template uses it.
- `configure(..., app=app)` (or `global_init(..., app=app)`) checks in `app.start()` (and when Robyn's test client is built) that every `@template` sits below its route decorator.
- [error()](../reference/flask.error.md#chameleon_rs.flask.error), [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async), [response_async()](../reference/robyn.response_async.md#chameleon_rs.robyn.response_async), data for [not_found()](../reference/flask.not_found.md#chameleon_rs.flask.not_found), and [ChameleonTemplate.render_template_async()](../reference/robyn.ChameleonTemplate.md#chameleon_rs.robyn.ChameleonTemplate.render_template_async).

See [Robyn](robyn.md).


# litestar-chameleon

[litestar-chameleon](https://github.com/uyar/litestar-chameleon) is a third-party package, not built in: it keeps working as it is with [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install). To move to the built-in engine instead:

``` python
from litestar_chameleon import ChameleonTemplateEngine
from litestar.response import Template
```

becomes

``` python
from chameleon_rs.litestar import ChameleonEngine as ChameleonTemplateEngine
from chameleon_rs.litestar import Template   # .pt served as text/html
```

Differences: `url_for_static_asset` is registered too, a name outside the template folder is refused, a context key wins over a template callable of the same name (as for Jinja), the loader options are [ChameleonEngine](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine) arguments (`CHAMELEON_RELOAD` rather than `CHAMELEON_AUTO_RELOAD`), and stock Chameleon need not be installed. See [Litestar](litestar.md).


# Timing renders

Instrumentation that replaces [PageTemplate.render](../reference/PageTemplate.render.md#chameleon_rs.PageTemplate.render) to time every render (common in apps that log slow templates) changes how async renders run on chameleon-rs. [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) honours a [render()](../reference/flask.render.md#chameleon_rs.flask.render) that differs from the built-in one, because a subclass's override may add defaults or post-process the output, and a monkeypatched [render](../reference/flask.render.md#chameleon_rs.flask.render) looks exactly like that override. So with the patch in place, every [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async), including those of async `@template` views, calls your [render](../reference/flask.render.md#chameleon_rs.flask.render) synchronously, and awaitable model values are not awaited.

``` python
# Makes every render_async() render synchronously:
original = PageTemplate.render

def timed_render(self, *args, **kwargs):
    start = time.perf_counter()
    try:
        return original(self, *args, **kwargs)
    finally:
        log_render(self.filename, time.perf_counter() - start)

PageTemplate.render = timed_render
```

Wrapping [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) as well doesn't help: the original [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) still sees the replaced [render](../reference/flask.render.md#chameleon_rs.flask.render). Instead:

- **Time the request or the view**, in middleware or around the call that renders. The template's share is then the view's time minus its data loading.
- **Use a sampling profiler** such as py-spy for a breakdown by template and helper, with no code change.
- **Keep the patch only in apps that render synchronously.** There it changes nothing but the timing.

The same applies to a subclass that overrides [render()](../reference/flask.render.md#chameleon_rs.flask.render): its [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) renders synchronously through the override. See [Compatibility](compatibility.md#render_async-and-a-replaced-render).


# Pitfalls

- **Install the alias first.** [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install) must run before the first `import chameleon`, including imports inside your framework integration. Put it in the entry point itself, above every other import.
- **A patched [PageTemplate.render](../reference/PageTemplate.render.md#chameleon_rs.PageTemplate.render) makes async renders synchronous.** See [Timing renders](#timing-renders).
- **Don't mix shadow mode with the alias.** Shadow mode needs stock Chameleon as [chameleon](../reference/shadow.ShadowDifference.md#chameleon_rs.shadow.ShadowDifference.chameleon).
- **`template.source` isn't Python.** Code that read the generated module (rare, mostly debugging tools) gets a program dump instead. See [Compatibility](compatibility.md#known-differences).
