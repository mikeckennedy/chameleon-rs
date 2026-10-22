"""`chameleon_rs.starlette`: `Templates`, Starlette's `Jinja2Templates` for chameleon-rs, and
the decorator API shared with `chameleon_rs.flask` / `.fastapi`.

`Templates` is compared with Starlette's own `Jinja2Templates` where the behaviour does not
depend on the engine (status, headers, media type, background tasks, the debug event, context
processors, `url_for`), on equivalent templates, through Starlette's test client; its HTML is
compared with stock Chameleon's for the same template and variables."""

from __future__ import annotations

import asyncio
import threading
import time
import warnings
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import CoroutineType
from typing import Any, assert_type

import pytest

pytest.importorskip('starlette')
pytest.importorskip('httpx')
jinja2 = pytest.importorskip('jinja2')

import chameleon_rs
import chameleon_rs.starlette as ours
import chameleon_rs.web
from chameleon_rs.template import PageTemplateFile
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.endpoints import HTTPEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Host, Mount, Route, Router
from starlette.templating import Jinja2Templates

with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    from starlette.testclient import TestClient

# The same pages for Jinja2 and for chameleon-rs: equal output for these variables.
PAGES = {
    'page.html': (
        '<h1>Hello {{ a }} at {{ request.url.path }} {{ site }}</h1><a href="{{ url_for(\'item\', n=3) }}">i</a>',
        '<h1>Hello ${a} at ${request.url.path} ${site}</h1><a href="${url_for(\'item\', n=3)}">i</a>',
    ),
    'bare.html': ('<p>{{ request.method }} {{ site }}</p>', '<p>${request.method} ${site}</p>'),
}

TEMPLATES = {
    'home/index.pt': '<h1>Hello ${a} ${b|"-"}!</h1>',
    'home/req.pt': '<p>${request.url.path} ${url_for("page", n=2)} ${a}</p>',
    'errors/404.pt': '<h1>Not here ${what | "?"}</h1>',
    'errors/500.pt': '<h1>Broken ${code | 0}</h1>',
    'test_starlette/bare.pt': '<p>bare ${a}</p>',
    'shared/_p.pt': '<i>${v}</i>',
    'partial.pt': '<div>${render_partial("shared/_p.pt", v=a)}</div>',
    'md.pt': '<div>${markdown: text}</div>',
    'rich.html': (
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


def site(request: Request) -> dict[str, Any]:
    return {'site': 'S' + request.url.path[:2]}


# --- Templates against Jinja2Templates ------------------------------------------------------


def templates_app(templates: Any, call: Callable[..., Any], ran: list[str]) -> Starlette:
    async def page(request: Request) -> Any:
        return await call(templates, request, 'page.html', {'a': request.query_params.get('a', 'x<&>y')})

    async def status(request: Request) -> Any:
        return await call(
            templates,
            request,
            'page.html',
            {'a': 1},
            status_code=201,
            headers={'x-test': 'yes'},
            media_type='text/plain',
        )

    async def background(request: Request) -> Any:
        return await call(templates, request, 'page.html', {'a': 2}, background=BackgroundTask(ran.append, 'bg'))

    async def bare(request: Request) -> Any:
        return await call(templates, request, 'bare.html')

    async def override(request: Request) -> Any:
        # The context processors run after the context is set: their keys win.
        return await call(templates, request, 'page.html', {'a': 3, 'site': 'mine'})

    async def positional(request: Request) -> Any:
        return await call(templates, request, 'page.html', {'a': 4}, 202, {'x-p': '1'}, 'text/x-test')

    async def item(request: Request) -> Any:
        return JSONResponse({})

    return Starlette(
        routes=[
            Route('/', page),
            Route('/status', status),
            Route('/background', background),
            Route('/bare', bare, methods=['GET', 'POST']),
            Route('/override', override),
            Route('/positional', positional),
            Route('/items/{n}', item, name='item'),
        ]
    )


async def call_sync(templates: Any, *args: Any, **kwargs: Any) -> Any:
    return templates.TemplateResponse(*args, **kwargs)


async def call_async(templates: Any, *args: Any, **kwargs: Any) -> Any:
    return await templates.TemplateResponse_async(*args, **kwargs)


REQUESTS = [
    ('get', '/'),
    ('get', '/?a=q%3Ct'),
    ('get', '/status'),
    ('get', '/background'),
    ('get', '/bare'),
    ('post', '/bare'),
    ('head', '/bare'),
    ('get', '/override'),
    ('get', '/positional'),
]


def responses(app: Starlette) -> list[tuple[Any, ...]]:
    out = []
    with TestClient(app) as client:
        for method, url in REQUESTS:
            r = client.request(method, url)
            context = {k: v for k, v in r.context.items() if k != 'request'}
            request = r.context['request']
            out.append(
                (
                    r.status_code,
                    sorted(r.headers.items()),
                    r.text,
                    sorted(r.context),
                    context,
                    type(request).__name__,
                    request.url.path,
                    Path(r.template.filename).name,
                    r.template.name,  # Starlette's documented test assertion
                )
            )
    return out


@pytest.mark.parametrize('call', [call_sync, call_async], ids=['TemplateResponse', 'TemplateResponse_async'])
def test_templates_same_as_jinja2templates(folder: Path, jinja_folder: Path, call: Callable[..., Any]) -> None:
    ran: dict[str, list[str]] = {'jinja': [], 'ours': []}
    want = responses(
        templates_app(Jinja2Templates(directory=jinja_folder, context_processors=[site]), call_sync, ran['jinja'])
    )
    got = responses(templates_app(ours.Templates(directory=folder, context_processors=[site]), call, ran['ours']))
    assert got == want
    assert ran['ours'] == ran['jinja'] == ['bg']
    assert [status for status, *_ in got] == [200, 200, 201, 200, 200, 200, 200, 200, 202]
    assert got[0][2] == '<h1>Hello x&lt;&amp;&gt;y at / S/</h1><a href="http://testserver/items/3">i</a>'


def test_templates_context_is_the_callers_dict(folder: Path, jinja_folder: Path) -> None:
    # As Starlette's: the context passed in is the response's (request added to it), and
    # `url_for` is the template's, not the context's.
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b''})
    for templates in (Jinja2Templates(directory=jinja_folder), ours.Templates(directory=folder)):
        context: dict[str, Any] = {'site': 's'}
        resp = templates.TemplateResponse(request, 'bare.html', context)
        assert resp.context is context
        assert context == {'site': 's', 'request': request}
        assert resp.body == b'<p>GET s</p>'
        assert resp.media_type == 'text/html'
    # A `url_for` of the caller's is used as given.
    resp = ours.Templates(directory=folder).TemplateResponse(
        request, 'page.html', {'a': 1, 'site': 's', 'url_for': lambda name, **kw: f'/{name}/{kw["n"]}'}
    )
    assert resp.body == b'<h1>Hello 1 at / s</h1><a href="/item/3">i</a>'


def test_html_same_as_stock_chameleon(folder: Path) -> None:
    import chameleon

    stock = chameleon.PageTemplateLoader(str(folder))
    templates = ours.Templates(directory=folder)
    seen: list[str] = []

    def variables(request: Request) -> dict[str, Any]:
        return {'items': ['a', '<b>', 3], 'title': 'T "q" & <x>', 'html': '<em>raw</em>'}

    async def rich(request: Request) -> Any:
        url_for = request.url_for
        seen.append(stock['rich.html'](request=request, url_for=url_for, **variables(request)))
        if request.query_params.get('async'):
            return await templates.TemplateResponse_async(request, 'rich.html', variables(request))
        return templates.TemplateResponse(request, 'rich.html', variables(request))

    async def item(request: Request) -> Any:
        return JSONResponse({})

    app = Starlette(routes=[Route('/', rich), Route('/items/{n}', item, name='item')])
    with TestClient(app) as client:
        for url in ('/', '/?q=1%3C2', '/?async=1&q=x'):
            assert client.get(url).text == seen[-1]
    assert '<li class="odd">&lt;b&gt;</li>' in seen[0]
    assert 'q=1&lt;2' in seen[1]


def test_templates_from_the_configuration(folder: Path) -> None:
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b''})
    templates = ours.Templates()
    ours.clear()
    chameleon_rs._settings.clear()
    with pytest.raises(chameleon_rs.web.TemplateError, match='without a directory'):
        templates.TemplateResponse(request, 'home/index.pt', {'a': 1})
    # Looked up at each render: configured after the object was made.
    chameleon_rs.configure(template_folder=str(folder))
    assert templates.TemplateResponse(request, 'home/index.pt', {'a': 1}).body == b'<h1>Hello 1 -!</h1>'
    assert templates.get_template('home/index.pt') is templates.loader['home/index.pt']
    with pytest.raises(TypeError, match='loader options'):
        ours.Templates(auto_reload=True)


def test_templates_take_several_folders_and_loader_options(
    folder: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    other = tmp_path_factory.mktemp('other')
    (other / 'only.pt').write_text('<p @click="go">${a}</p>')
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b''})
    templates = ours.Templates(directory=[other, str(folder)], restricted_namespace=False, auto_reload=True)
    assert templates.TemplateResponse(request, 'only.pt', {'a': 1}).body == b'<p @click="go">1</p>'
    assert templates.TemplateResponse(request, 'home/index.pt', {'a': 2}).body == b'<h1>Hello 2 -!</h1>'
    (other / 'only.pt').write_text('<p>${a}!</p>')
    assert templates.TemplateResponse(request, 'only.pt', {'a': 3}).body == b'<p>3!</p>'


def test_old_style_call_is_a_clear_error(folder: Path) -> None:
    with pytest.raises(TypeError, match='request first'):
        ours.Templates(directory=folder).TemplateResponse('page.html', {'request': None})  # ty: ignore[invalid-argument-type]


def test_render_partial_and_markdown_in_templates(folder: Path) -> None:
    chameleon_rs.configure(markdown=True)
    ours.clear()
    chameleon_rs._settings.install(partials_settings=None)  # no folder registered anywhere
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b''})
    templates = ours.Templates(directory=folder)
    assert templates.TemplateResponse(request, 'partial.pt', {'a': '<x>'}).body == b'<div><i>&lt;x&gt;</i></div>'
    md = templates.TemplateResponse(request, 'md.pt', {'text': 'Hi *there*'}).body
    assert md == b'<div><p>Hi <em>there</em></p>\n</div>'


def test_async_templates_do_not_block_the_loop(folder: Path) -> None:
    templates = ours.Templates(directory=folder)
    (folder / 'cards.pt').write_text(
        '<ul><tal:x repeat="i items">${render_partial("shared/_p.pt", v=fetch(i))}</tal:x></ul>'
    )

    async def slow(value: Any) -> Any:
        await asyncio.sleep(0.05)
        return value

    async def fetch(i: int) -> int:
        await asyncio.sleep(0.05)
        return i * 10

    async def main() -> tuple[int, list[bytes], float, bytes]:
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b''})
        ticks = 0
        stop = False

        async def tick() -> None:
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.002)

        ticker = asyncio.create_task(tick())
        start = time.perf_counter()
        pages = await asyncio.gather(
            *(
                templates.TemplateResponse_async(request, 'home/index.pt', {'a': slow(n), 'b': slow('<b>')})
                for n in range(5)
            )
        )
        elapsed = time.perf_counter() - start
        cards = await templates.TemplateResponse_async(
            request, 'cards.pt', {'items': range(6), 'fetch': fetch}, concurrent=True
        )
        stop = True
        await ticker
        return ticks, [bytes(p.body) for p in pages], elapsed, bytes(cards.body)

    ticks, bodies, elapsed, cards = asyncio.run(main())
    assert bodies == [f'<h1>Hello {n} &lt;b&gt;!</h1>'.encode() for n in range(5)]
    assert elapsed < 0.2  # five renders waiting 2 x 50 ms each, together
    assert ticks >= 10  # the loop kept running while they waited
    assert cards == b'<ul>' + b''.join(f'<i>{i * 10}</i>'.encode() for i in range(6)) + b'</ul>'


def test_templates_in_a_fastapi_app(folder: Path) -> None:
    fastapi = pytest.importorskip('fastapi')
    app = fastapi.FastAPI()
    templates = ours.Templates(directory=folder)

    @app.get('/p/{n}', name='page')
    async def page(request: Request, n: int) -> Any:
        return await templates.TemplateResponse_async(request, 'home/req.pt', {'a': n})

    with TestClient(app) as client:
        r = client.get('/p/5')
    assert r.text == '<p>/p/5 http://testserver/p/2 5</p>'
    assert r.template.filename.endswith('req.pt')


# --- The decorator API ----------------------------------------------------------------------


def endpoints_app() -> Starlette:
    @ours.template('home/req.pt')
    async def page(request: Request) -> Any:
        return {'a': request.path_params['n']}

    @ours.template('home/index.pt', status_code=201)
    def sync(request: Request) -> Any:
        return {'a': request.query_params.get('a', 's'), 'b': threading.current_thread().name.startswith('AnyIO')}

    @ours.template('home/index.pt', 'text/plain')
    async def errors(request: Request) -> Any:
        n = int(request.path_params['n'])
        if n == 0:
            ours.not_found(what='page')
        if n == 1:
            ours.error('errors/500.pt', 503, code=7)
        if n == 2:
            ours.generic_error('errors/500.pt', 409, {'code': 'c'})
        if n == 3:
            return JSONResponse({'raw': True}, status_code=202)
        return {'a': n, 'request': 'mine'}

    class Endpoint(HTTPEndpoint):
        @ours.template('home/req.pt')
        async def get(self, request: Request) -> Any:
            return {'a': 'cls'}

        @ours.template('home/index.pt', mimetype='application/xhtml+xml')
        def post(self, request: Request) -> Any:
            return {'a': 'posted'}

    @ours.template
    async def bare(request: Request) -> Any:
        return {'a': 'b'}

    return Starlette(
        routes=[
            Route('/p/{n}', page, name='page'),
            Route('/sync', sync),
            Route('/e/{n}', errors),
            Route('/cls', Endpoint),
            Route('/bare', bare),
        ]
    )


def test_decorated_endpoints(folder: Path) -> None:
    with TestClient(endpoints_app()) as client:
        got = [
            (r.status_code, r.headers['content-type'], r.text)
            for r in (
                client.get('/p/5'),
                client.get('/sync?a=%3Cq'),
                client.get('/e/0'),
                client.get('/e/1'),
                client.get('/e/2'),
                client.get('/e/3'),
                client.get('/e/4'),
                client.get('/cls'),
                client.post('/cls'),
                client.get('/bare'),
            )
        ]
    assert got == [
        (200, 'text/html; charset=utf-8', '<p>/p/5 http://testserver/p/2 5</p>'),
        (201, 'text/html; charset=utf-8', '<h1>Hello &lt;q True!</h1>'),
        (404, 'text/html; charset=utf-8', '<h1>Not here page</h1>'),
        (503, 'text/html; charset=utf-8', '<h1>Broken 7</h1>'),
        (409, 'text/html; charset=utf-8', '<h1>Broken c</h1>'),
        (202, 'application/json', '{"raw":true}'),
        (200, 'text/plain; charset=utf-8', '<h1>Hello 4 -!</h1>'),
        (200, 'text/html; charset=utf-8', '<p>/cls http://testserver/p/2 cls</p>'),
        (200, 'application/xhtml+xml', '<h1>Hello posted -!</h1>'),
        (200, 'text/html; charset=utf-8', '<p>bare b</p>'),
    ]


def test_async_endpoints_render_on_the_loop_and_sync_ones_in_the_threadpool(folder: Path) -> None:
    seen: dict[str, int] = {}

    async def later(value: Any) -> Any:
        seen['await'] = threading.get_ident()
        await asyncio.sleep(0)
        return value

    @ours.template('home/index.pt')
    async def on_loop(request: Request) -> Any:
        seen['async'] = threading.get_ident()
        return {'a': later('<x>'), 'b': later(2)}

    @ours.template('home/index.pt')
    def in_pool(request: Request) -> Any:
        seen['sync'] = threading.get_ident()
        return {'a': 's'}

    app = Starlette(routes=[Route('/async', on_loop), Route('/sync', in_pool)])
    with TestClient(app) as client:
        assert client.get('/async').text == '<h1>Hello &lt;x&gt; 2!</h1>'
        assert client.get('/sync').text == '<h1>Hello s -!</h1>'
    assert seen['await'] == seen['async'] != seen['sync']


def test_render_and_response(folder: Path) -> None:
    async def later(value: Any) -> Any:
        return value

    assert ours.render('home/index.pt', a=1) == '<h1>Hello 1 -!</h1>'
    resp = ours.response('home/index.pt', 'text/plain', 203, a=2)
    assert (resp.status_code, resp.media_type, resp.body) == (203, 'text/plain', b'<h1>Hello 2 -!</h1>')
    assert asyncio.run(ours.render_async('home/index.pt', a=later(3))) == '<h1>Hello 3 -!</h1>'
    resp = asyncio.run(ours.response_async('home/index.pt', status_code=404, a=later(4)))
    assert (resp.status_code, resp.body) == (404, b'<h1>Hello 4 -!</h1>')
    ours.clear()
    with pytest.raises(chameleon_rs.web.TemplateError, match='global_init'):
        ours.render('home/index.pt', a=1)


def test_invalid_return_value(folder: Path) -> None:
    @ours.template('home/index.pt')
    def view(request: Request) -> Any:
        return 5

    with pytest.raises(chameleon_rs.web.TemplateError, match=r'expected a dict or starlette\.responses\.Response'):
        view(Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b''}))


def test_settings_slots(folder: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    import chameleon_rs.fastapi

    other = tmp_path_factory.mktemp('slots')
    chameleon_rs._settings.clear()
    ours.global_init(str(other))
    ours.global_init(str(folder))  # cache_init: ignored
    assert ours.template_path == str(other)
    assert chameleon_rs.fastapi.template_path is None
    assert chameleon_rs.partials.template_path is None
    chameleon_rs.configure(template_folder=str(folder))
    assert ours.template_path == chameleon_rs.fastapi.template_path == chameleon_rs.partials.template_path
    ours.configure(template_folder=str(other))
    assert ours.template_path == chameleon_rs.partials.template_path == str(other)
    assert chameleon_rs.fastapi.template_path == str(folder)
    with pytest.raises(chameleon_rs.web.TemplateError, match='must be a folder'):
        ours.configure(template_folder=str(other / 'missing'))


# --- The decorator-order check --------------------------------------------------------------


def test_misordered_endpoint_fails_at_startup_and_on_the_first_request(folder: Path) -> None:
    def make() -> Starlette:
        app = Starlette()
        ours.configure(app=app)

        async def fine(request: Request) -> Any:
            return {'a': 1}

        async def detail(request: Request) -> Any:
            return {'a': 2}

        app.add_route('/fine', ours.template('home/index.pt')(fine))
        app.add_route('/detail', detail)
        ours.template('home/index.pt')(detail)  # wrapped after the route registered it
        return app

    with pytest.raises(chameleon_rs.web.DecoratorOrderError) as info, TestClient(make()):
        pass
    message = str(info.value)
    assert message.startswith('@template must be placed below the route decorator (app.add_route, Route(...)')
    assert '.detail() at ' in message
    assert '(route GET /detail)' in message
    assert 'fine' not in message
    client = TestClient(make())  # not entered: no lifespan events
    for _ in range(2):
        with pytest.raises(chameleon_rs.web.DecoratorOrderError):
            client.get('/fine')


def test_check_covers_mount_host_router_and_endpoint_classes(folder: Path) -> None:
    raw = {}
    for name in ('mounted', 'routed', 'hosted', 'sub', 'method'):

        async def view(*args: Any) -> Any:
            return {'a': 1}

        view.__qualname__ = name
        raw[name] = view

    class Page(HTTPEndpoint):
        get = raw['method']

        @ours.template('home/index.pt')
        async def post(self, request: Request) -> Any:
            return {'a': 2}

    router = Router(routes=[Route('/r', raw['routed'])])
    app = Starlette(
        routes=[
            Mount('/m', routes=[Route('/x', raw['mounted'], methods=['POST'])]),
            Mount('/router', app=router),
            Host('api.example.com', app=Router(routes=[Route('/h', raw['hosted'])])),
            Mount('/sub', app=Starlette(routes=[Route('/s', raw['sub'])])),
            Route('/page', Page),
        ]
    )
    ours.check_views(app)  # nothing wrapped yet
    for function in raw.values():
        ours.template('home/index.pt')(function)
    with pytest.raises(chameleon_rs.web.DecoratorOrderError) as info:
        ours.check_views(app)
    lines = str(info.value).splitlines()
    wheres = [line.split('(route ')[1].split('):')[0] for line in lines]
    assert wheres == ['POST /m/x', 'GET /router/r', 'GET /h', 'GET /sub/s', 'GET /page (Page.get)']
    with pytest.raises(chameleon_rs.web.DecoratorOrderError, match='routed'):
        ours.check_views(router)  # a Router by itself
    ours.check_views(app, ignore=list(raw.values()))


def test_correct_order_and_endpoint_classes_start(folder: Path) -> None:
    app = endpoints_app()
    ours.configure(app=app)
    ours.configure(app=app)  # watching twice checks once
    with TestClient(app) as client:
        assert client.get('/cls').status_code == 200
    plain = TestClient(app)
    assert plain.get('/bare').text == '<p>bare b</p>'


def test_escape_hatches(folder: Path) -> None:
    async def data(request: Request) -> Any:
        return {'a': 2}

    html = ours.template('home/index.pt')(data)

    async def page(request: Request) -> Any:
        return await html(request)

    def make(**configure: Any) -> Starlette:
        app = Starlette(routes=[Route('/api', data), Route('/html', page)])
        ours.configure(app=app, **configure)
        return app

    with pytest.raises(chameleon_rs.web.DecoratorOrderError), TestClient(make()):
        pass
    with TestClient(make(check_order=False)) as client:
        assert client.get('/html').text == '<h1>Hello 2 -!</h1>'
    ours.check_views(make(check_order=False), ignore=[data])


def test_app_must_be_an_application(folder: Path) -> None:
    with pytest.raises(TypeError, match='takes a Starlette application, not Router'):
        ours.configure(app=Router())
    with pytest.raises(TypeError, match='application or router'):
        ours.check_views(object())


def test_a_user_lifespan_and_its_state_pass_through(folder: Path) -> None:
    import contextlib

    @contextlib.asynccontextmanager
    async def lifespan(app: Any) -> Any:
        yield {'greeting': 'hi'}

    @ours.template('home/index.pt')
    async def index(request: Request) -> Any:
        return {'a': request.state.greeting}

    app = Starlette(routes=[Route('/', index)], lifespan=lifespan)
    ours.configure(app=app)
    with TestClient(app) as client:
        assert client.get('/').text == '<h1>Hello hi -!</h1>'


# --- Review fixes: names outside the folder, TemplateNotFound, globals, error pages ----------


def blank_request() -> Request:
    return Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b''})


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
    ],
)
def test_templates_refuse_names_outside_the_folder(folder: Path, name: str) -> None:
    secret = folder.parent / 'secret'
    secret.mkdir(exist_ok=True)
    (secret / 's.pt').write_text('<p>secret ${1 + 1}</p>')
    if name == 'SECRET_ABSOLUTE':
        name = str(secret / 's.pt')
    templates = ours.Templates(directory=folder)
    with pytest.raises(chameleon_rs.web.TemplateNotFound, match='Template not found'):
        templates.TemplateResponse(blank_request(), name)
    with pytest.raises(chameleon_rs.web.TemplateNotFound):
        asyncio.run(templates.TemplateResponse_async(blank_request(), name))
    with pytest.raises(chameleon_rs.web.TemplateNotFound):
        templates.get_template(name)


def test_jinja_refuses_parent_names_too(jinja_folder: Path) -> None:
    with pytest.raises(jinja2.TemplateNotFound):
        Jinja2Templates(directory=jinja_folder).get_template('../x.html')


def test_names_inside_the_folder_still_load(folder: Path) -> None:
    templates = ours.Templates(directory=folder)
    for name in ('home/index.pt', './home/index.pt', 'home/./index.pt'):
        assert templates.TemplateResponse(blank_request(), name, {'a': 1}).body == b'<h1>Hello 1 -!</h1>'


@pytest.mark.parametrize('name', [' bare.html', 'bare.html ', '\tbare.html'])
def test_names_with_surrounding_whitespace_are_not_found_as_in_jinja(
    folder: Path, jinja_folder: Path, name: str
) -> None:
    # Chameleon's loader strips names; Jinja looks for that exact file, as `Templates` does.
    with pytest.raises(jinja2.TemplateNotFound):
        Jinja2Templates(directory=jinja_folder).get_template(name)
    with pytest.raises(chameleon_rs.web.TemplateNotFound):
        ours.Templates(directory=folder).get_template(name)


def test_decorator_render_keeps_the_original_libraries_lookup(folder: Path) -> None:
    # Owner's rule: render()/@template/render_partial resolve names as chameleon-flask,
    # fastapi-chameleon and chameleon_partials do (no guard): names must not come from requests.
    (folder.parent / 'outside.pt').write_text('<p>outside</p>')
    assert ours.render('../outside.pt') == '<p>outside</p>'


def test_missing_template_is_template_not_found(folder: Path) -> None:

    with pytest.raises(chameleon_rs.web.TemplateNotFound) as info:
        ours.Templates(directory=folder).TemplateResponse(blank_request(), 'nope.pt')
    assert isinstance(info.value, ValueError)
    assert isinstance(info.value, LookupError)
    assert isinstance(info.value, chameleon_rs.web.TemplateError)
    assert str(info.value) == 'Template not found: nope.pt.'
    assert ours.TemplateNotFound is chameleon_rs.web.TemplateNotFound
    # From the configuration too.
    chameleon_rs.configure(template_folder=str(folder))
    with pytest.raises(chameleon_rs.web.TemplateNotFound):
        ours.Templates().get_template('nope.pt')


def test_only_templates_raise_template_not_found(folder: Path) -> None:
    # Everything else raises Chameleon's plain ValueError, with its message, as before.
    import chameleon

    with pytest.raises(ValueError, match='nope') as stock:
        chameleon.PageTemplateLoader(str(folder))['nope.pt']
    with pytest.raises(ValueError, match='nope') as native:
        chameleon_rs.PageTemplateLoader(str(folder))['nope.pt']
    assert type(native.value) is type(stock.value) is ValueError
    assert str(native.value) == str(stock.value) == 'Template not found: nope.pt.'
    with pytest.raises(ValueError, match='nope') as rendered:
        ours.render('nope.pt')
    assert type(rendered.value) is ValueError
    (folder / 'loads.pt').write_text('<p tal:define="t load: nope.pt">${t}</p>')
    messages = []
    for engine in (chameleon, chameleon_rs):
        with pytest.raises(ValueError) as info:
            engine.PageTemplateLoader(str(folder))['loads.pt']()
        assert not isinstance(info.value, chameleon_rs.web.TemplateNotFound)
        messages.append(str(info.value).splitlines()[0])
    assert messages[0] == messages[1] == 'Template not found: nope.pt.'


def test_templates_globals(folder: Path) -> None:
    templates = ours.Templates(directory=folder)
    assert set(templates.globals) == {'url_for'}
    templates.globals['b'] = 'G'
    assert templates.TemplateResponse(blank_request(), 'home/index.pt', {'a': 1}).body == b'<h1>Hello 1 G!</h1>'
    # The context wins over globals, as in Jinja; globals are not in the response's context.
    resp = templates.TemplateResponse(blank_request(), 'home/index.pt', {'a': 1, 'b': 'c'})
    assert resp.body == b'<h1>Hello 1 c!</h1>'
    assert 'url_for' not in resp.context
    templates.globals['url_for'] = lambda name, **kw: f'/custom/{name}'
    resp = templates.TemplateResponse(blank_request(), 'page.html', {'a': 1, 'site': 's'})
    assert b'href="/custom/item"' in resp.body


def test_error_pages_of_decorated_endpoints_get_request_and_url_for(folder: Path) -> None:
    (folder / 'errors' / 'layout404.pt').write_text(
        '<h1>${request.url.path} not here, see ${url_for("home")} ${what}</h1>'
    )

    @ours.template('home/index.pt')
    async def home(request: Request) -> Any:
        return {'a': 1}

    @ours.template('home/index.pt')
    async def missing(request: Request) -> Any:
        ours.not_found('errors/layout404.pt', what='x')

    @ours.template('home/index.pt')
    def broken(request: Request) -> Any:
        ours.error('errors/layout404.pt', 410, what='y', request='data wins')

    app = Starlette(routes=[Route('/', home, name='home'), Route('/m', missing), Route('/b', broken)])
    with TestClient(app) as client:
        r = client.get('/m')
        assert (r.status_code, r.text) == (404, '<h1>/m not here, see http://testserver/ x</h1>')
        with pytest.raises(TypeError, match='string indices'):  # the page's data wins: `request` is a str
            client.get('/b')


def test_debug_template_is_a_view_of_the_page(folder: Path) -> None:
    templates = ours.Templates(directory=folder)
    resp = templates.TemplateResponse(blank_request(), 'home/index.pt', {'a': 1})
    page = templates.get_template('home/index.pt')
    assert resp.template.name == 'home/index.pt'
    assert resp.template.filename == page.filename
    assert resp.template.render(a=2) == '<h1>Hello 2 -!</h1>'
    assert not hasattr(page, 'name')  # the cached template is not changed
    assert repr(resp.template) == "<PageTemplateFile 'home/index.pt'>"


def test_check_walks_through_middleware(folder: Path) -> None:
    from starlette.middleware.gzip import GZipMiddleware

    async def raw(request: Request) -> Any:
        return {'a': 1}

    app = Starlette(routes=[Mount('/g', app=GZipMiddleware(GZipMiddleware(Starlette(routes=[Route('/z', raw)]))))])
    ours.check_views(app)
    ours.template('home/index.pt')(raw)
    with pytest.raises(chameleon_rs.web.DecoratorOrderError, match=r'\(route GET /g/z\)'):
        ours.check_views(app)


# --- Typing (checked by ty, not run) --------------------------------------------------------


def _typing_checks() -> None:
    @ours.template('home/index.pt')
    def view(request: Request) -> dict[str, Any]:
        return {'a': 1}

    @ours.template
    async def aview(request: Request) -> dict[str, Any]:
        return {'a': 1}

    @ours.template('home/index.pt', 'text/plain', 201, concurrent=True)
    async def bview(request: Request) -> Any:
        return {}

    request = Request({'type': 'http'})
    assert_type(view(request), Response)
    assert_type(aview(request), Coroutine[Any, Any, Response])
    assert_type(bview(request), Coroutine[Any, Any, Response])
    assert_type(ours.response('x.pt', 'text/plain', 200, a=1), Response)
    templates = ours.Templates(directory='templates')
    assert_type(templates.TemplateResponse(request, 'x.pt'), ours._TemplateResponse)
    assert_type(templates.get_template('x.pt'), PageTemplateFile)
    assert_type(templates.TemplateResponse_async(request, 'x.pt'), CoroutineType[Any, Any, ours._TemplateResponse])


SPELLINGS = ['home/index.pt', './home/index.pt', 'home//index.pt', './/home/./index.pt', 'home/./././index.pt']


def test_every_spelling_of_a_name_is_one_template(folder: Path) -> None:
    # Security: each spelling once compiled and cached its own copy (memory without bound).
    templates = ours.Templates(directory=folder)
    assert len({id(templates.get_template(name)) for name in SPELLINGS}) == 1
    assert len(templates.loader.registry) == 1


def test_case_spellings_and_folders(folder: Path) -> None:
    templates = ours.Templates(directory=folder)
    for name in ['home', 'home/', '.']:
        with pytest.raises(chameleon_rs.web.TemplateNotFound):
            templates.get_template(name)
    probe = folder / 'CaseProbe'
    probe.write_text('')
    insensitive = (folder / 'caseprobe').exists()
    probe.unlink()
    if insensitive:
        names = ['home/index.pt', 'HOME/index.pt', 'home/INDEX.PT', 'Home/Index.pt']
        assert len({id(templates.get_template(name)) for name in names}) == 1
        assert len(templates.loader.registry) == 1
