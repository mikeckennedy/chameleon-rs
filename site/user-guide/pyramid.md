# Pyramid

Chameleon grew up with Pyramid, and [pyramid_chameleon](https://github.com/Pylons/pyramid_chameleon) is how most Pyramid apps render `.pt` templates. chameleon-rs has no Pyramid module of its own because it doesn't need one: pyramid_chameleon runs unchanged on chameleon-rs through [chameleon_rs.compat.install()](../reference/compat.install.md#chameleon_rs.compat.install). Your views, `renderer=` names, templates and settings stay as they are.

> **Note: Not run when the docs are built**
>
> The examples on this page are checked by hand against Pyramid 2.1 and pyramid_chameleon 0.3, not run when the docs are built, as the other framework pages are.


# Switch an app over

Install chameleon-rs next to Pyramid and pyramid_chameleon:

``` bash
uv pip install chameleon-rs pyramid pyramid_chameleon
```

pyramid_chameleon depends on stock Chameleon, so pip installs that too. That's fine: once the alias is installed, nothing imports it.

Then install the alias where your app starts. With `pserve`, that's the top of the package whose `main()` your `.ini` file names (`use = egg:myapp`), above every other import:

``` python
# myapp/__init__.py
import chameleon_rs.compat

chameleon_rs.compat.install()

from pyramid.config import Configurator


def main(global_config, **settings):
    with Configurator(settings=settings) as config:
        config.include('pyramid_chameleon')
        config.add_route('home', '/')
        config.scan('.views')
    return config.make_wsgi_app()
```

`config.include('pyramid_chameleon')` imports pyramid_chameleon, which imports [chameleon](../reference/shadow.ShadowDifference.md#chameleon_rs.shadow.ShadowDifference.chameleon), and gets chameleon-rs. Every `.pt` (and `.txt`) renderer in the app now renders on chameleon-rs.

Other entry points need the same two lines first: a `conftest.py` for your tests, and any script that loads the app without `pserve` (`pshell` and `prequest` go through `main()`, so they're covered).


# Views and templates

Nothing changes in a view:

``` python
# myapp/views.py
from pyramid.view import view_config


@view_config(route_name='home', renderer='templates/home.pt')
def home(request):
    return {'title': 'Latest videos', 'videos': ['intro', 'partials']}
```

Or in its template:

``` html
<!-- myapp/templates/home.pt -->
<html metal:use-macro="load: layout.pt">
  <main metal:fill-slot="content">
    <h1>${title}</h1>
    <div tal:repeat="video videos">${render_partial('shared/video.pt', video=video)}</div>
    <p>${request.path}</p>
  </main>
</html>
```

What pyramid_chameleon provides keeps working:

- **Renderer names**: relative names (`templates/home.pt`), asset specs (`myapp:templates/home.pt`) and absolute paths.
- **Macro renderers**: `renderer='templates/fragments#count.pt'` renders only the `count` macro of `fragments.pt`, handy for HTMX fragments.
- **Text templates**: `.txt` renderers use [PageTextTemplateFile](../reference/PageTextTemplateFile.md#chameleon_rs.PageTextTemplateFile).
- **The system values**: `request`, `context`, `view` and `renderer_name` in every template.
- **Settings**: `pyramid.reload_templates` turns on `auto_reload`, and `debug_templates` works as before.
- **Translation**: pyramid_chameleon's translator, so `i18n:translate` uses Pyramid's localizer.
- **`pyramid.renderers.render()` and `render_to_response()`** with a `.pt` name.


# Partials

[render_partial](../reference/render_partial.md#chameleon_rs.render_partial) is built into every template. A partial path is looked for next to the calling template and in the folders above it, so `shared/video.pt` above is found with no setup.

To render partials from Python as well (an HTMX view returning one fragment, say), tell chameleon-rs where the templates live once, in `main()`:

``` python
from pathlib import Path

import chameleon_rs

chameleon_rs.configure(template_folder=(Path(__file__).parent / 'templates').as_posix())
```

Then `chameleon_rs.render_partial('shared/video.pt', video=v).html_text` returns the markup.

If your app added [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) with chameleon_partials and a `BeforeRender` subscriber, the subscriber can go. With the alias it still works, and calls the built-in. See [Partials](partials.md) and [Migrating](migrating.md).


# Rendering is synchronous

Pyramid is a WSGI framework: views and renderers run synchronously, in your server's worker threads. pyramid_chameleon calls the template's [render()](../reference/flask.render.md#chameleon_rs.flask.render), so the async features ([Async rendering](async.md)) don't apply. You still get the speed: rendering is several times faster, and templates compile about 70x faster, so the first request after a restart or a template change is quick too.


# Precompiling isn't needed

pyramid_chameleon's `pyramid-chameleon-precompile` script fills `CHAMELEON_CACHE` with compiled Python modules, to save compile time at startup. chameleon-rs doesn't write a cache (see [Compatibility](compatibility.md#known-differences)), and compiling 100 templates takes about 10 ms, so drop the script from your deployment. If you keep it, its worker processes need the alias too: they import [chameleon](../reference/shadow.ShadowDifference.md#chameleon_rs.shadow.ShadowDifference.chameleon) without running your app's `main()`.


# Compatibility

pyramid_chameleon's own test suite passes on chameleon-rs, unmodified: it runs in chameleon-rs's integration tests on every change, together with the suites of the other framework libraries. See [Compatibility](compatibility.md).
