"""`chameleon_rs.litestar`: `ChameleonEngine`, Litestar's `TemplateEngineProtocol` for chameleon-rs,
its `Template` response (`.pt` served as text/html), and the decorator API shared with the other
framework modules.

The engine is compared with Litestar's own Jinja engine where the behaviour does not depend on
the engine (status, headers, cookies, background tasks, the template callables, errors), on
equivalent templates, through Litestar's test client; its HTML with litestar-chameleon's on stock
Chameleon for the same template and context."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
import sys
import threading
import time
import types
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, assert_type

import pytest

pytest.importorskip('litestar')
httpx = pytest.importorskip('httpx')
jinja2 = pytest.importorskip('jinja2')

import chameleon_rs
import chameleon_rs._settings
import chameleon_rs.litestar as ours
import chameleon_rs.web
from litestar import Controller, Litestar, MediaType, Request, Response, Router, get, post
from litestar.background_tasks import BackgroundTask
from litestar.config.csrf import CSRFConfig
from litestar.datastructures import Cookie
from litestar.di import Provide
from litestar.exceptions import ImproperlyConfiguredException, TemplateNotFoundException
from litestar.response import Redirect
from litestar.response import Template as LitestarTemplate
from litestar.template import TemplateConfig
from litestar.testing import TestClient

# Older Litestar 2.x: the Jinja engine in `contrib`, no `FromPath` / `FromQuery` (before 2.25).
try:
    from litestar.plugins.jinja import JinjaTemplateEngine
except ImportError:  # pragma: no cover - Litestar before 2.22
    from litestar.contrib.jinja import JinjaTemplateEngine
if TYPE_CHECKING:
    from litestar.params import FromPath, FromQuery
else:
    try:
        from litestar.params import FromPath, FromQuery
    except ImportError:  # pragma: no cover - Litestar before 2.25
        FromPath = FromQuery = None
from test_api import run_python

# Litestar 2.25 deprecates parameters declared without `FromPath` / `FromQuery` (most tests keep
# the older style, which every Litestar 2.x accepts; `test_annotated_parameters` uses the new).
pytestmark = pytest.mark.filterwarnings('ignore::DeprecationWarning:litestar')

# The same pages for Jinja and for chameleon-rs: equal output for these contexts.
PAGES = {
    'page.html': (
        (
            '<h1>Hello {{ a }} {{ request.method }} <a href="{{ url_for(\'item\', n=3) }}">i</a>'
            ' [{{ csrf_token() }}] {{ csrf_input|safe }}</h1>'
        ),
        (
            '<h1>Hello ${a} ${request.method} <a href="${url_for(\'item\', n=3)}">i</a>'
            ' [${csrf_token()}] ${structure: csrf_input}</h1>'
        ),
    ),
    'greet.html': ('<p>{{ greet("bob") }}</p>', '<p>${greet("bob")}</p>'),
}
STRINGS = ('<b>{{ x }} {{ url_for("item", n=1) }}</b>', '<b>${x} ${url_for("item", n=1)}</b>')

TEMPLATES = {
    'home/index.pt': '<h1>Hello ${a} ${b|"-"}!</h1>',
    'home/req.pt': '<p>${request.url.path} ${url_for("item", n=2)} ${a} [${csrf_token()}] ${structure: csrf_input}</p>',
    'errors/404.pt': '<h1>Not here ${what | "?"} ${url_for("item", n=4)}</h1>',
    'errors/500.pt': '<h1>Broken ${code | 0}</h1>',
    'test_litestar/bare.pt': '<p>bare ${a}</p>',
    'shared/_p.pt': '<i>${v}</i>',
    'partial.pt': '<div>${render_partial("shared/_p.pt", v=a)}</div>',
    'plain.txt': 'text ${a}',
    'feed.xml': '<feed>${a}</feed>',
    'rich.pt': (
        '<html><body><ul><li tal:repeat="i items" class="${\'odd\' if repeat.i.odd else \'even\'}">${i}</li></ul>'
        "<p tal:condition=\"request.query_params.get('q')\">q=${request.query_params['q']}</p>"
        '<a href="${url_for(\'item\', n=7)}" title="${title}">seven</a>'
        '<div tal:replace="structure html"/><span tal:content="title" tal:attributes="data-x title"/>'
        '</body></html>'
    ),
}


@pytest.fixture
def folder(tmp_path: Path) -> Any:
    for name, body in TEMPLATES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    for name, (_, body) in PAGES.items():
        (tmp_path / name).write_text(body)
    ours.global_init(str(tmp_path), cache_init=False)
    yield tmp_path
    ours.clear()
    chameleon_rs._settings.clear()


@pytest.fixture
def jinja_folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp('jinja')
    for name, (body, _) in PAGES.items():
        (folder / name).write_text(body)
    return folder


def require_litestar_chameleon() -> Any:
    """litestar-chameleon (installed by `make dev`), or a skip; under `make test`
    (`CHAMELEON_RS_REQUIRE_LITESTAR_CHAMELEON=1`) its absence fails the test instead."""
    try:
        import litestar_chameleon
    except ImportError:
        if os.environ.get('CHAMELEON_RS_REQUIRE_LITESTAR_CHAMELEON') == '1':
            pytest.fail('litestar-chameleon is not installed in this environment: run `make dev`')
        pytest.skip('litestar-chameleon is not installed')
    return litestar_chameleon


def greet(context: Any, name: str) -> str:
    return f'hi {name} {context["a"]}'


@get('/items/{n:int}', name='item', sync_to_thread=False)
def item(n: int) -> str:
    return str(n)


# --- ChameleonEngine against Litestar's Jinja engine ------------------------------------------


def native_app(config: TemplateConfig[Any], template_str: str, ran: list[str], **options: Any) -> Litestar:
    @get('/')
    async def page(a: str = 'x<&>y') -> LitestarTemplate:
        return LitestarTemplate('page.html', context={'a': a})

    @get('/status')
    async def status() -> LitestarTemplate:
        return LitestarTemplate(
            'page.html', context={'a': 1}, status_code=201, headers={'x-test': 'yes'}, media_type='text/plain'
        )

    @get('/cookies')
    async def cookies() -> LitestarTemplate:
        return LitestarTemplate('page.html', context={'a': 2}, cookies=[Cookie(key='k', value='v')])

    @get('/background')
    async def background() -> LitestarTemplate:
        return LitestarTemplate('page.html', context={'a': 3}, background=BackgroundTask(ran.append, 'bg'))

    @get('/string')
    async def string() -> LitestarTemplate:
        return LitestarTemplate(template_str=template_str, context={'x': '<x>'})

    @get('/greet')
    async def greeting() -> LitestarTemplate:
        return LitestarTemplate('greet.html', context={'a': 5})

    @get('/missing')
    async def missing() -> LitestarTemplate:
        return LitestarTemplate('nope.html', context={})

    @post('/form')
    async def form() -> LitestarTemplate:
        return LitestarTemplate('page.html', context={'a': 'posted'})

    return Litestar(
        [page, status, cookies, background, string, greeting, missing, form, item],
        template_config=config,
        **options,
    )


def engines(folder: Path, jinja_folder: Path) -> list[tuple[TemplateConfig[Any], str]]:
    def callback(engine: Any) -> None:
        engine.register_template_callable('greet', greet)

    return [
        (TemplateConfig(engine=JinjaTemplateEngine, directory=jinja_folder, engine_callback=callback), STRINGS[0]),
        (TemplateConfig(engine=ours.ChameleonEngine, directory=folder, engine_callback=callback), STRINGS[1]),
    ]


def test_engine_matches_jinja(folder: Path, jinja_folder: Path) -> None:
    results = []
    for config, template_str in engines(folder, jinja_folder):
        ran: list[str] = []
        with TestClient(native_app(config, template_str, ran)) as client:
            out = []
            for method, url in [
                ('GET', '/'),
                ('GET', '/?a=hello'),
                ('GET', '/status'),
                ('GET', '/cookies'),
                ('GET', '/background'),
                ('GET', '/string'),
                ('GET', '/greet'),
                ('GET', '/missing'),
                ('POST', '/form'),
            ]:
                r = client.request(method, url)
                out.append((url, r.status_code, dict(r.headers), r.text))
            results.append((out, ran))
    assert results[0] == results[1]
    out, ran = results[1]
    assert ran == ['bg']
    assert out[0][1:] == (
        200,
        {'content-type': 'text/html; charset=utf-8', 'content-length': '114'},
        (
            '<h1>Hello x&lt;&amp;&gt;y GET <a href="/items/3">i</a> [] '
            '<input type="hidden" name="_csrf_token" value="" /></h1>'
        ),
    )
    assert out[5][3] == '<b>&lt;x&gt; /items/1</b>'
    assert out[6][3] == '<p>hi bob 5</p>'
    assert out[7][1] == 500


def test_csrf_token_and_input(folder: Path, jinja_folder: Path) -> None:
    for config, template_str in engines(folder, jinja_folder):
        app = native_app(config, template_str, [], csrf_config=CSRFConfig(secret='s' * 32))
        with TestClient(app) as client:
            text = client.get('/').text
            token = text.split('[')[1].split(']')[0]
            assert token
            assert f'name="_csrf_token" value="{token}"' in text


def test_engine_html_matches_litestar_chameleon_on_stock_chameleon(folder: Path) -> None:
    litestar_chameleon = require_litestar_chameleon()
    import chameleon

    assert not getattr(chameleon, '__name__', '').startswith('chameleon_rs')

    @get('/')
    async def rich(q: str = '') -> ours.Template:
        return ours.Template(
            'rich.pt', context={'items': ['a', 'b<', 'c'], 'title': 'T "q" <&>', 'html': '<em>raw</em>'}
        )

    bodies = []
    for engine in (litestar_chameleon.ChameleonTemplateEngine, ours.ChameleonEngine):
        app = Litestar([rich, item], template_config=TemplateConfig(engine=engine, directory=folder))
        with TestClient(app) as client:
            bodies.append([(r.headers['content-type'], r.text) for r in (client.get('/'), client.get('/?q=yes'))])
    assert bodies[0] == bodies[1]
    assert 'q=yes' in bodies[1][1][1]


def test_litestar_chameleon_runs_on_chameleon_rs_through_compat(folder: Path) -> None:
    require_litestar_chameleon()
    result = run_python(f"""
        import sys
        import chameleon_rs, chameleon_rs.compat
        chameleon_rs.compat.install()
        from pathlib import Path
        from litestar import Litestar, get
        from litestar.response import Template
        from litestar.template import TemplateConfig
        from litestar.testing import TestClient
        from litestar_chameleon import ChameleonTemplateEngine
        import chameleon

        @get('/')
        async def index() -> Template:
            return Template('partial.pt', context={{'a': 'hi <b>'}}, media_type='text/html')

        app = Litestar([index], template_config=TemplateConfig(engine=ChameleonTemplateEngine, directory=Path({str(folder)!r})))
        with TestClient(app) as client:
            r = client.get('/')
        print(r.status_code, r.headers['content-type'], r.text)
        print(chameleon is chameleon_rs, type(app.template_engine.engine).__module__)
    """)
    assert result.stdout == (
        '200 text/html; charset=utf-8 <div><i>hi &lt;b&gt;</i></div>\nTrue chameleon_rs.zpt.loader\n'
    ), result.stderr


# --- The engine's API ------------------------------------------------------------------------


@pytest.mark.parametrize(
    'name',
    [
        '../secret/s.pt',
        'home/../../secret/s.pt',
        ' ../secret/s.pt',
        'SECRET_ABSOLUTE',
        '/etc/hosts',
        '..\\secret\\s.pt',
        'home\\index.pt',
        'C:/secret/s.pt',
        'chameleon_rs:zpt/loader.py',
        '',
        'nope.pt',
    ],
)
def test_engine_refuses_names_outside_the_folder(folder: Path, name: str) -> None:
    secret = folder.parent / 'secret'
    secret.mkdir(exist_ok=True)
    (secret / 's.pt').write_text('<p>secret ${1 + 1}</p>')
    if name == 'SECRET_ABSOLUTE':
        name = str(secret / 's.pt')
    with pytest.raises(TemplateNotFoundException) as info:
        ours.ChameleonEngine(folder).get_template(name)
    assert info.value.detail == f'Template {name} not found.'


def test_jinja_engine_refuses_parent_names_too(jinja_folder: Path) -> None:
    with pytest.raises(TemplateNotFoundException) as info:
        JinjaTemplateEngine(jinja_folder).get_template('../x.html')
    assert info.value.detail == 'Template ../x.html not found.'


def test_names_inside_the_folder_still_load(folder: Path) -> None:
    engine = ours.ChameleonEngine(folder)
    for name in ('home/index.pt', './home/index.pt', 'home/./index.pt'):
        assert engine.get_template(name).render(a=1) == '<h1>Hello 1 -!</h1>'


@pytest.mark.parametrize('name', [' home/index.pt', 'home/index.pt ', '\thome/index.pt'])
def test_names_with_surrounding_whitespace_are_not_found_as_in_jinja(
    folder: Path, jinja_folder: Path, name: str
) -> None:
    # Chameleon's loader strips names; Jinja looks for that exact file, as the engine does.
    (jinja_folder / 'home').mkdir(exist_ok=True)
    (jinja_folder / 'home' / 'index.pt').write_text('<h1>{{ a }}</h1>')
    assert JinjaTemplateEngine(jinja_folder).get_template('home/index.pt')
    for engine in (JinjaTemplateEngine(jinja_folder), ours.ChameleonEngine(folder)):
        with pytest.raises(TemplateNotFoundException):
            engine.get_template(name)


def test_the_loaders_own_lookup_decides(folder: Path) -> None:
    # A loader with a default extension: `home/index` is `home/index.pt`.
    engine = ours.ChameleonEngine(engine_instance=chameleon_rs.PageTemplateLoader(str(folder), default_extension='.pt'))
    assert engine.get_template('home/index').render(a=1) == '<h1>Hello 1 -!</h1>'
    with pytest.raises(TemplateNotFoundException) as info:
        engine.get_template('home/nope')
    assert info.value.detail == 'Template home/nope not found.'
    # Only the lookup's "not found" becomes TemplateNotFoundException: a template that fails to
    # compile (when it first renders) raises its own error.
    (folder / 'broken.pt').write_text('<p tal:content="1 +">x</p>')
    with pytest.raises(Exception) as broken:
        ours.ChameleonEngine(folder).get_template('broken.pt').render()
    assert not isinstance(broken.value, TemplateNotFoundException)
    assert 'Template not found' not in str(broken.value)


def test_engine_construction(folder: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    other = tmp_path_factory.mktemp('other')
    (other / 'only.pt').write_text('<p>other ${a}</p>')
    # Several folders, searched in order; str or Path.
    engine = ours.ChameleonEngine([str(other), folder])
    assert engine.get_template('only.pt').render(a=1) == '<p>other 1</p>'
    assert engine.get_template('home/index.pt').render(a=2) == '<h1>Hello 2 -!</h1>'
    # A loader of one's own.
    loader = chameleon_rs.PageTemplateLoader(str(folder))
    assert ours.ChameleonEngine.from_loader(loader).loader is loader
    assert ours.ChameleonEngine(engine_instance=loader).loader is loader
    with pytest.raises(ImproperlyConfiguredException):
        ours.ChameleonEngine(folder, loader)
    # Loader options.
    engine = ours.ChameleonEngine(folder, auto_reload=True, restricted_namespace=False)
    assert engine.loader.kwargs == {'auto_reload': True, 'restricted_namespace': False}
    with pytest.raises(TypeError, match='loader options'):
        ours.ChameleonEngine(auto_reload=True)
    # Neither: the configuration, read at each render.
    engine = ours.ChameleonEngine()
    assert engine.get_template('home/index.pt').render(a=3) == '<h1>Hello 3 -!</h1>'
    chameleon_rs.configure(template_folder=str(other))
    assert engine.get_template('only.pt').render(a=4) == '<p>other 4</p>'
    ours.clear()
    with pytest.raises(chameleon_rs.web.TemplateError, match='needs chameleon_rs.configure'):
        engine.get_template('only.pt')
    # As Litestar builds it, and as an instance.
    assert isinstance(
        TemplateConfig(engine=ours.ChameleonEngine, directory=folder).engine_instance, ours.ChameleonEngine
    )
    instance = ours.ChameleonEngine(folder)
    assert TemplateConfig(engine=instance).engine_instance is instance


def test_template_callables(folder: Path) -> None:
    engine = ours.ChameleonEngine(folder)
    assert set(engine.template_callables) == {'url_for', 'csrf_token', 'url_for_static_asset'}
    (folder / 'calls.pt').write_text('<p>${greet("ann")} ${url_for("x")}</p>')
    template = engine.get_template('calls.pt')
    engine.register_template_callable('greet', greet)
    # A template already handed out keeps the callables it had; a new one has the new callable.
    assert 'greet' not in template.template_callables
    # The context's own keys win over the callables (Jinja's globals are below its context).
    page = engine.get_template('calls.pt')
    assert page.render(a=1, url_for=lambda name: f'mine:{name}') == '<p>hi ann 1 mine:x</p>'
    with pytest.raises(TypeError, match='keyword'):
        page.render({'a': 1})


def test_a_render_leaves_no_reference_cycle_holding_its_context(folder: Path) -> None:
    # The callables were bound to the variables dict that held them: the context (the model,
    # the request) lived until the next garbage collection.
    import gc
    import weakref

    class Model:
        pass

    (folder / 'plain.pt').write_text('<p>${model.__class__.__name__}</p>')
    page = ours.ChameleonEngine(folder).get_template('plain.pt')
    model = Model()
    ref = weakref.ref(model)
    gc.disable()
    try:
        assert page.render(model=model) == '<p>Model</p>'
        del model
        assert ref() is None
    finally:
        gc.enable()


def test_render_async_awaits_values(folder: Path) -> None:
    page = ours.ChameleonEngine(folder).get_template('home/index.pt')

    async def later() -> str:
        await asyncio.sleep(0.01)
        return 'later'

    assert asyncio.run(page.render_async(a=later())) == '<h1>Hello later -!</h1>'


def test_render_string_caches_and_uses_loader_options(folder: Path) -> None:
    engine = ours.ChameleonEngine(folder, restricted_namespace=False)
    assert engine.render_string('<p x-on:click="go" @click="go">${a}</p>', {'a': 1}) == (
        '<p x-on:click="go" @click="go">1</p>'
    )
    engine.render_string('<i>${a}</i>', {'a': 1})
    first = engine._strings[engine.loader, '<i>${a}</i>']
    engine.render_string('<i>${a}</i>', {'a': 2})
    assert engine._strings[engine.loader, '<i>${a}</i>'] is first
    for n in range(200):
        engine.render_string(f'<i>{n}</i>', {})
    assert len(engine._strings) == 128


def test_render_string_compiles_with_the_loaders_options(folder: Path) -> None:
    # Every template option of the loader (here `translate`), as `get_template`'s templates.
    (folder / 'tr.pt').write_text('<p i18n:translate="">hello</p>')
    loader = chameleon_rs.PageTemplateLoader(str(folder), translate=lambda msgid, **kw: 'BONJOUR')
    engine = ours.ChameleonEngine.from_loader(loader)
    assert engine.get_template('tr.pt').render() == '<p>BONJOUR</p>'
    assert engine.render_string('<p i18n:translate="">hello</p>', {}) == '<p>BONJOUR</p>'


def test_render_string_cache_follows_the_configuration(folder: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    engine = ours.ChameleonEngine()
    assert engine.render_string('<p>${a}</p>', {'a': 1}) == '<p>1</p>'
    first = engine._strings[engine.loader, '<p>${a}</p>']
    # A later configure() makes a new loader: the string is compiled again, with its options.
    chameleon_rs.configure(template_folder=str(tmp_path_factory.mktemp('other')), restricted_namespace=False)
    assert engine.render_string('<p>${a}</p>', {'a': 2}) == '<p>2</p>'
    second = engine._strings[engine.loader, '<p>${a}</p>']
    assert second is not first
    assert len(engine._strings) == 2


def test_render_string_is_thread_safe(folder: Path) -> None:
    import concurrent.futures

    engine = ours.ChameleonEngine(folder)

    def work(offset: int) -> list[str]:
        return [engine.render_string(f'<i>${{a}} {n % 150}</i>', {'a': offset}) for n in range(300)]

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(work, range(8)))
    assert results[3][7] == '<i>3 7</i>'
    assert len(engine._strings) == 128


def test_a_string_compiled_by_two_threads_at_once_is_kept_once(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two threads that miss the same string both compile it. The second to finish must use the
    # first one's template, not evict another to store its own (the full cache shrank: the
    # thread-safety test above saw 127 under load).
    engine = ours.ChameleonEngine(folder)
    for n in range(128):
        engine.render_string(f'<i>{n}</i>', {})
    compile_template = ours.PageTemplate

    def other_thread_finishes_first(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(ours, 'PageTemplate', compile_template)
        engine.render_string('<i>new</i>', {})
        return compile_template(*args, **kwargs)

    monkeypatch.setattr(ours, 'PageTemplate', other_thread_finishes_first)
    assert engine.render_string('<i>new</i>', {}) == '<i>new</i>'
    kept = [string for _, string in engine._strings]
    assert len(kept) == 128
    assert kept[0] == '<i>1</i>'
    assert kept[-1] == '<i>new</i>'


def test_partials_render_inside_engine_templates(folder: Path) -> None:
    chameleon_rs._settings.clear()
    assert ours.ChameleonEngine(folder).get_template('partial.pt').render(a='p') == '<div><i>p</i></div>'


# --- The media type: Template serves .pt as text/html ---------------------------------------


def test_template_media_type(folder: Path) -> None:
    @get('/ours')
    async def ours_pt() -> ours.Template:
        return ours.Template('home/index.pt', context={'a': 1})

    @get('/stock')
    async def stock_pt() -> LitestarTemplate:
        return LitestarTemplate('home/index.pt', context={'a': 1})

    @get('/text')
    async def text() -> ours.Template:
        return ours.Template('plain.txt', context={'a': 1})

    @get('/xml')
    async def xml() -> ours.Template:
        return ours.Template('feed.xml', context={'a': 1})

    @get('/explicit')
    async def explicit() -> ours.Template:
        return ours.Template('home/index.pt', context={'a': 1}, media_type=MediaType.TEXT)

    @get('/route', media_type='application/xhtml+xml')
    async def route() -> ours.Template:
        return ours.Template('home/index.pt', context={'a': 1})

    @get('/string')
    async def string() -> ours.Template:
        return ours.Template(template_str='<p>${a}</p>', context={'a': 1})

    app = Litestar(
        [ours_pt, stock_pt, text, xml, explicit, route, string],
        template_config=TemplateConfig(engine=ours.ChameleonEngine, directory=folder),
    )
    with TestClient(app) as client:
        types_ = {url: client.get(url).headers['content-type'] for url in ('/ours', '/stock', '/text', '/xml')}
        types_ |= {url: client.get(url).headers['content-type'] for url in ('/explicit', '/route', '/string')}
        assert client.get('/ours').text == '<h1>Hello 1 -!</h1>'
    assert types_ == {
        '/ours': 'text/html; charset=utf-8',
        '/stock': 'text/plain; charset=utf-8',  # Litestar's guess: the reason for ours.Template
        '/text': 'text/plain; charset=utf-8',
        '/xml': 'application/xml',
        '/explicit': 'text/plain; charset=utf-8',
        '/route': 'application/xhtml+xml',
        '/string': 'text/html; charset=utf-8',
    }


# --- The decorator API -----------------------------------------------------------------------


@dataclasses.dataclass
class Model:
    a: int
    b: str


def service() -> str:
    return 'svc'


class Pages(Controller):
    path = '/ctrl'

    @get('/{n:int}')
    @ours.template('home/index.pt')
    async def detail(self, n: int) -> dict[str, Any]:
        return {'a': n, 'b': 'ctrl'}


def decorator_app(**options: Any) -> Litestar:
    @get('/sync/{n:int}', sync_to_thread=False)
    @ours.template('home/index.pt')
    def sync_view(n: int, b: str = 'q', c: int = 0) -> dict[str, Any]:
        return {'a': n, 'b': b}

    @get('/async/{n:int}')
    @ours.template('home/index.pt')
    async def async_view(n: int, svc: str) -> dict[str, Any]:
        return {'a': n, 'b': svc}

    @get('/req')
    @ours.template('home/req.pt')
    async def req(request: Request) -> dict[str, Any]:
        return {'a': request.method}

    @get('/injected', sync_to_thread=True)
    @ours.template('home/req.pt')
    def injected() -> dict[str, Any]:
        return {'a': 'no request param'}

    @get('/model-wins')
    @ours.template('home/req.pt')
    async def model_wins() -> dict[str, Any]:
        return {'a': 1, 'url_for': lambda name, n: f'mine{n}'}

    @get('/missing/{n:int}')
    @ours.template('home/index.pt')
    async def missing(n: int) -> dict[str, Any]:
        if n == 1:
            ours.not_found(what='thing')
        if n == 2:
            ours.error('errors/500.pt', 503, code=42)
        ours.generic_error('errors/500.pt', 502, {'code': 7})

    @get('/sync-missing', sync_to_thread=False)
    @ours.template('home/index.pt')
    def sync_missing() -> dict[str, Any]:
        ours.not_found()

    @get('/redirect')
    @ours.template('home/index.pt')
    async def redirect() -> Any:
        return Redirect('/sync/1')

    @get('/native')
    @ours.template('home/index.pt')
    async def native() -> Any:
        return ours.Template('home/index.pt', context={'a': 'native'})

    @get('/plain', sync_to_thread=False)
    @ours.template('home/index.pt', 'text/plain', 201)
    def plain() -> Model:
        return Model(5, 'm')

    @get('/mimetype', sync_to_thread=False)
    @ours.template('home/index.pt', mimetype='application/xhtml+xml', status_code=202)
    def mimetype() -> dict[str, Any]:
        return {'a': 6}

    @get('/bare', sync_to_thread=False)
    @ours.template
    def bare() -> dict[str, Any]:
        return {'a': 'b'}

    @post('/form')
    @ours.template('home/index.pt')
    async def form(data: dict[str, Any]) -> dict[str, Any]:
        return {'a': data['x']}

    return Litestar(
        [sync_view, async_view, req, injected, model_wins, missing, sync_missing, redirect, native]
        + [plain, mimetype, bare, form, item, Pages],
        dependencies={'svc': Provide(service, sync_to_thread=False)},
        # The engine without a directory: the configured folder.
        template_config=TemplateConfig(engine=ours.ChameleonEngine()),
        **options,
    )


def test_decorated_handlers(folder: Path) -> None:
    with TestClient(decorator_app()) as client:

        def get_(url: str, **kwargs: Any) -> tuple[int, str, str]:
            r = client.get(url, **kwargs)
            return r.status_code, r.headers.get('content-type', ''), r.text

        html = 'text/html; charset=utf-8'
        assert get_('/sync/3') == (200, html, '<h1>Hello 3 q!</h1>')
        assert get_('/sync/3?b=<x>') == (200, html, '<h1>Hello 3 &lt;x&gt;!</h1>')
        assert get_('/sync/3?c=x')[0] == 400  # Litestar's validation of a query parameter
        assert get_('/sync/x')[0] == 404  # and of the path's
        assert get_('/async/4') == (200, html, '<h1>Hello 4 svc!</h1>')
        assert get_('/req') == (
            200,
            html,
            '<p>/req /items/2 GET [] <input type="hidden" name="_csrf_token" value="" /></p>',
        )
        assert get_('/injected')[2] == (
            '<p>/injected /items/2 no request param [] <input type="hidden" name="_csrf_token" value="" /></p>'
        )
        assert get_('/model-wins')[2].startswith('<p>/model-wins mine2 1')
        assert get_('/missing/1') == (404, html, '<h1>Not here thing /items/4</h1>')
        assert get_('/missing/2') == (503, html, '<h1>Broken 42</h1>')
        assert get_('/missing/3') == (502, html, '<h1>Broken 7</h1>')
        assert get_('/sync-missing') == (404, html, '<h1>Not here ? /items/4</h1>')
        r = client.get('/redirect', follow_redirects=False)
        assert (r.status_code, r.headers['location']) == (302, '/sync/1')
        assert get_('/native') == (200, html, '<h1>Hello native -!</h1>')
        assert get_('/plain') == (201, 'text/plain; charset=utf-8', '<h1>Hello 5 m!</h1>')
        assert get_('/mimetype') == (202, 'application/xhtml+xml', '<h1>Hello 6 -!</h1>')
        assert get_('/bare') == (200, html, '<p>bare b</p>')
        assert get_('/ctrl/8') == (200, html, '<h1>Hello 8 ctrl!</h1>')
        r = client.post('/form', json={'x': 'posted'})
        assert (r.status_code, r.text) == (201, '<h1>Hello posted -!</h1>')


@pytest.mark.skipif(FromPath is None, reason='FromPath / FromQuery are new in Litestar 2.25')
def test_annotated_parameters(folder: Path) -> None:
    @get('/{n:int}', sync_to_thread=False)
    @ours.template('home/index.pt')
    def view(n: FromPath[int], b: FromQuery[str] = 'q') -> dict[str, Any]:
        return {'a': n, 'b': b}

    with TestClient(Litestar([view])) as client:
        assert client.get('/3?b=x').text == '<h1>Hello 3 x!</h1>'
        assert client.get('/3?b=x&n=1').status_code == 200
        assert client.get('/z').status_code == 404


def test_invalid_return_and_not_configured(folder: Path) -> None:
    @ours.template('home/index.pt')
    def view() -> Any:
        return 42

    with pytest.raises(chameleon_rs.web.TemplateError, match=r'Invalid return type .*litestar.Response'):
        view()
    ours.clear()
    with pytest.raises(chameleon_rs.web.TemplateError, match='global_init'):
        ours.render('home/index.pt')


def test_csrf_in_decorated_templates(folder: Path) -> None:
    with TestClient(decorator_app(csrf_config=CSRFConfig(secret='s' * 32))) as client:
        text = client.get('/req').text
    token = text.split('[')[1].split(']')[0]
    assert token
    assert f'value="{token}"' in text


def test_decorated_templates_use_the_apps_engine_callables(folder: Path) -> None:
    (folder / 'calls.pt').write_text('<p>${greet("x")}</p>')

    @get('/')
    @ours.template('calls.pt')
    async def index() -> dict[str, Any]:
        return {'a': 'model'}

    config = TemplateConfig(
        engine=ours.ChameleonEngine,
        directory=folder,
        engine_callback=lambda engine: engine.register_template_callable('greet', greet),
    )
    with TestClient(Litestar([index], template_config=config)) as client:
        # Bound to Litestar's request context (`request`, `csrf_input`), not the model: `greet`
        # reads `context['a']`.
        assert client.get('/').status_code == 500
    (folder / 'calls.pt').write_text('<p>${greet("x")}</p>')
    greet_request = lambda context, name: f'{name} {context["request"].url.path}'
    config = TemplateConfig(
        engine=ours.ChameleonEngine,
        directory=folder,
        engine_callback=lambda engine: engine.register_template_callable('greet', greet_request),
    )
    with TestClient(Litestar([index], template_config=config)) as client:
        assert client.get('/').text == '<p>x /</p>'


def where() -> str:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return f'thread {threading.get_ident()}'
    return f'loop {threading.get_ident()}'


def test_where_handlers_render(folder: Path) -> None:
    (folder / 'where.pt').write_text('${where()}|${handler}')

    @get('/async')
    @ours.template('where.pt')
    async def on_loop() -> dict[str, Any]:
        return {'where': where, 'handler': where()}

    @get('/thread', sync_to_thread=True)
    @ours.template('where.pt')
    def in_thread() -> dict[str, Any]:
        return {'where': where, 'handler': where()}

    @get('/sync', sync_to_thread=False)
    @ours.template('where.pt')
    def on_loop_sync() -> dict[str, Any]:
        return {'where': where, 'handler': where()}

    with TestClient(Litestar([on_loop, in_thread, on_loop_sync])) as client:
        async_render, async_handler = client.get('/async').text.split('|')
        thread_render, thread_handler = client.get('/thread').text.split('|')
        sync_render, sync_handler = client.get('/sync').text.split('|')
    assert async_render == async_handler and async_render.startswith('loop ')
    assert thread_render == thread_handler and thread_render.startswith('thread ')
    assert sync_render == sync_handler == async_render


def test_async_handlers_do_not_block_the_loop(folder: Path) -> None:
    async def slow(value: str) -> str:
        await asyncio.sleep(0.2)
        return value

    @get('/{n:int}')
    @ours.template('home/index.pt')
    async def index(n: int) -> dict[str, Any]:
        return {'a': slow(str(n)), 'b': slow('b')}

    async def run() -> tuple[list[str], float]:
        # httpx's transport: Litestar's AsyncTestClient sends one request at a time.
        transport = httpx.ASGITransport(app=Litestar([index]))
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            start = time.perf_counter()
            responses = await asyncio.gather(*(client.get(f'/{n}') for n in range(5)))
            return [r.text for r in responses], time.perf_counter() - start

    texts, elapsed = asyncio.run(run())
    assert texts == [f'<h1>Hello {n} b!</h1>' for n in range(5)]
    # Serially: 5 x 2 x 0.2 s. Concurrently: the five renders wait together (2 x 0.2 s).
    assert elapsed < 1.0


def test_signature_for_litestar(folder: Path) -> None:
    def view(n: int, b: str = 'q') -> dict[str, Any]:
        return {}

    decorated = ours.template('home/index.pt')(view)
    signature = inspect.signature(decorated)
    assert list(signature.parameters) == ['n', 'b', 'request']
    assert signature.parameters['request'].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters['request'].annotation is Request
    assert signature.return_annotation is Response
    assert decorated.__annotations__ == {'n': 'int', 'b': 'str', 'request': Request, 'return': Response}
    assert decorated.__wrapped__ is view  # ty: ignore[unresolved-attribute]
    # The handler's own `request` is kept, not added again; **kwargs stay last.

    def with_request(request: Request, **extra: Any) -> dict[str, Any]:
        return {}

    assert list(inspect.signature(ours.template('home/index.pt')(with_request)).parameters) == ['request', 'extra']

    def with_kwargs(n: int, **extra: Any) -> dict[str, Any]:
        return {}

    assert list(inspect.signature(ours.template('home/index.pt')(with_kwargs)).parameters) == ['n', 'request', 'extra']


def test_called_directly_without_a_request(folder: Path) -> None:
    @ours.template('home/index.pt')
    def view(n: int) -> dict[str, Any]:
        return {'a': n}

    resp = view(3)
    assert isinstance(resp, Response)
    assert resp.content == '<h1>Hello 3 -!</h1>'


def test_forward_references_resolve_when_the_route_registers(folder: Path) -> None:
    # Annotations naming a class defined after the handler (no `from __future__ import
    # annotations`): Litestar resolves them when the app is built, so @template must not
    # evaluate them.
    module = types.ModuleType('litestar_forward_refs')
    sys.modules[module.__name__] = module
    try:
        exec(  # noqa: S102 - a module whose source has no `from __future__ import annotations`
            'from typing import Any\n'
            'from litestar import get\n'
            'import chameleon_rs.litestar as ours\n'
            '@get("/{n:int}", sync_to_thread=False)\n'
            '@ours.template("home/index.pt")\n'
            'def view(n: int, later: Later | None = None) -> dict[str, Any]:\n'
            '    return {"a": n}\n'
            'class Later:\n'
            '    pass\n',
            module.__dict__,
        )
        with TestClient(Litestar([module.view])) as client:
            assert client.get('/5').text == '<h1>Hello 5 -!</h1>'
    finally:
        del sys.modules[module.__name__]


def test_openapi_parameters_are_the_handlers(folder: Path) -> None:
    @get('/plain/{n:int}', sync_to_thread=False)
    def plain(n: int, q: str = 'z', svc: str = 'x') -> dict[str, Any]:
        return {}

    @get('/html/{n:int}', sync_to_thread=False)
    @ours.template('home/index.pt')
    def html(n: int, q: str = 'z', svc: str = 'x') -> dict[str, Any]:
        return {}

    with TestClient(Litestar([plain, html])) as client:
        paths = client.get('/schema/openapi.json').json()['paths']
    assert paths['/html/{n}']['get']['parameters'] == paths['/plain/{n}']['get']['parameters']
    assert [p['name'] for p in paths['/html/{n}']['get']['parameters']] == ['n', 'q', 'svc']


def test_module_render_and_response(folder: Path) -> None:
    assert ours.render('home/index.pt', a=1) == '<h1>Hello 1 -!</h1>'
    assert asyncio.run(ours.render_async('home/index.pt', a=asyncio.sleep(0, 2))) == '<h1>Hello 2 -!</h1>'
    resp = ours.response('home/index.pt', 'text/plain', 201, a=3)
    assert (resp.content, resp.media_type, resp.status_code) == ('<h1>Hello 3 -!</h1>', 'text/plain', 201)
    resp = asyncio.run(ours.response_async('home/index.pt', a=4))
    # No status code: the route's (as `litestar.Response`).
    assert (resp.content, resp.media_type, resp.status_code) == ('<h1>Hello 4 -!</h1>', 'text/html', None)

    @post('/', sync_to_thread=False)
    def created() -> Response[str]:
        return ours.response('home/index.pt', a=5)

    @get('/', status_code=202, sync_to_thread=False)
    @ours.template('home/index.pt')
    def accepted() -> dict[str, Any]:
        return {'a': 6}

    with TestClient(Litestar([created, accepted])) as client:
        assert (client.post('/').status_code, client.get('/').status_code) == (201, 202)


def test_settings_slots(folder: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    other = tmp_path_factory.mktemp('other')
    assert ours.template_path == str(folder)
    ours.global_init(str(other))  # cache_init: ignored
    assert ours.template_path == str(folder)
    ours.configure(template_folder=str(other))
    slot, partials = chameleon_rs._settings.views['litestar'], chameleon_rs._settings.partials
    assert slot is not None and partials is not None
    assert slot.template_folder == partials.template_folder == str(other)
    assert chameleon_rs._settings.views['starlette'] is None
    ours.clear()
    assert ours.template_path is None
    chameleon_rs.configure(template_folder=str(folder))
    assert ours.template_path == str(folder)
    with pytest.raises(chameleon_rs.web.TemplateError, match='must be a folder'):
        ours.configure(template_folder=str(folder / 'nope'))


# --- The decorator order ---------------------------------------------------------------------


def test_misordered_decorators_fail_at_decoration(folder: Path) -> None:
    with pytest.raises(
        chameleon_rs.web.DecoratorOrderError, match=r'route decorator .* on .*index\(\) at .*test_litestar'
    ):

        @ours.template('home/index.pt')
        @get('/')
        async def index() -> dict[str, Any]:
            return {}


async def raw_view() -> dict[str, Any]:
    return {'a': 'raw'}


def misordered_app() -> tuple[Litestar, Any]:
    # A route given the function before @template wrapped it.
    route = get('/raw')(raw_view)
    wrapped = ours.template('home/index.pt')(raw_view)
    return Litestar([route]), wrapped


def startup_error(client: TestClient[Any]) -> BaseException:
    with pytest.raises(BaseException) as info, client:
        pass
    error = info.value
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    return error


def test_misordered_route_fails_at_startup(folder: Path) -> None:
    app, _ = misordered_app()
    ours.configure(app=app)
    ours.configure(app=app)  # watched once
    assert sum(isinstance(hook, ours._StartupCheck) for hook in app.on_startup) == 1
    assert isinstance(app.on_startup[0], ours._StartupCheck)
    error = startup_error(TestClient(app))
    assert isinstance(error, chameleon_rs.web.DecoratorOrderError)
    assert 'raw_view() at' in str(error) and '(route GET /raw)' in str(error)


def test_misordered_route_fails_at_the_first_request_without_lifespan(folder: Path) -> None:
    app, _ = misordered_app()
    ours.global_init(str(folder), app=app)
    client = TestClient(app)
    for _ in range(2):
        with pytest.raises(chameleon_rs.web.DecoratorOrderError):
            client.get('/raw')


def test_correct_order_passes_and_unhooks(folder: Path) -> None:
    app = decorator_app()
    ours.configure(app=app)
    guard = app.asgi_handler
    assert isinstance(guard, ours._FirstRequestCheck)
    client = TestClient(app)
    assert client.get('/sync/1').text == '<h1>Hello 1 q!</h1>'
    assert app.asgi_handler is guard.app
    with TestClient(app) as client:
        assert client.get('/ctrl/2').text == '<h1>Hello 2 ctrl!</h1>'


def test_escape_hatches(folder: Path) -> None:
    app, _ = misordered_app()
    ours.configure(app=app, check_order=False)
    assert not app.on_startup
    with TestClient(app) as client:
        assert client.get('/raw').json() == {'a': 'raw'}
    ours.check_views(app, ignore=[raw_view])
    with pytest.raises(chameleon_rs.web.DecoratorOrderError):
        ours.check_views(app)


def test_check_views_covers_routers_and_controllers(folder: Path) -> None:
    router = Router('/r', route_handlers=[get('/raw')(raw_view)])
    ours.template('home/index.pt')(raw_view)
    with pytest.raises(chameleon_rs.web.DecoratorOrderError, match=r'\(route GET /r/raw\)'):
        ours.check_views(Litestar([router, Pages]))
    ours.check_views(Litestar([Pages]))


def test_app_must_be_an_application(folder: Path) -> None:
    with pytest.raises(TypeError, match='takes a Litestar application, not Router'):
        ours.configure(app=Router('/r', route_handlers=[]))  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match='application or Router'):
        ours.check_views(object())


def test_exports(folder: Path) -> None:
    assert ours.DecoratorOrderError is chameleon_rs.web.DecoratorOrderError
    assert ours.NotFound is chameleon_rs.web.NotFound
    assert issubclass(ours.Template, LitestarTemplate)


# --- Typing (checked by ty, not run) --------------------------------------------------------


def _typing_checks() -> None:
    @ours.template('home/index.pt')
    def view(n: int) -> dict[str, Any]:
        return {'a': n}

    @ours.template
    async def aview(n: int) -> dict[str, Any]:
        return {'a': n}

    @ours.template('home/index.pt', 'text/plain', 201, concurrent=True)
    async def bview() -> Any:
        return {}

    assert_type(view(1), Response[Any])
    assert_type(aview(1), Coroutine[Any, Any, Response[Any]])
    assert_type(bview(), Coroutine[Any, Any, Response[Any]])
    assert_type(ours.response('x.pt', 'text/plain', 200, a=1), Response[str])
    engine = ours.ChameleonEngine('templates')
    assert_type(engine.get_template('x.pt'), ours.ChameleonTemplate)
    assert_type(engine.get_template('x.pt').render(a=1), str)
    assert_type(ours.Template('x.pt'), ours.Template)
    callback: Callable[[ours.ChameleonEngine], None] = lambda e: e.register_template_callable('g', greet)
    assert_type(
        TemplateConfig(engine=ours.ChameleonEngine, directory='t', engine_callback=callback),
        TemplateConfig[ours.ChameleonEngine],
    )


SPELLINGS = ['home/index.pt', './home/index.pt', 'home//index.pt', './/home/./index.pt', 'home/./././index.pt']


def test_every_spelling_of_a_name_is_one_template(folder: Path) -> None:
    # Security: each spelling once compiled and cached its own copy (memory without bound).
    engine = ours.ChameleonEngine(folder)
    pages = {id(engine.get_template(name).template) for name in SPELLINGS}
    assert len(pages) == 1
    assert len(engine.loader.registry) == 1


def test_case_spellings_and_folders(folder: Path) -> None:
    engine = ours.ChameleonEngine(folder)
    for name in ['home', 'home/', '.']:
        with pytest.raises(TemplateNotFoundException):
            engine.get_template(name)
    probe = folder / 'CaseProbe'
    probe.write_text('')
    insensitive = (folder / 'caseprobe').exists()
    probe.unlink()
    if insensitive:
        for name in ['home/index.pt', 'HOME/index.pt', 'home/INDEX.PT']:
            engine.get_template(name)
        assert len(engine.loader.registry) == 1
