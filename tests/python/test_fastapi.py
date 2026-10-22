"""`chameleon_rs.fastapi`: fastapi-chameleon's API built in. Each differential case runs the
same view through the original fastapi-chameleon (pinned clone in temp/, on stock Chameleon)
and through `chameleon_rs.fastapi`, and compares the response (type, status, content type,
body) or the exception; whole FastAPI apps are compared through the test client. Then what
fastapi-chameleon does not have: async views rendering with `render_async`, models, `error()`,
and the decorator-order check at startup."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import importlib
import re
import sys
import threading
import types
import warnings
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import ModuleType
from typing import Any, assert_type

import pytest

fastapi = pytest.importorskip('fastapi')
pytest.importorskip('httpx')

import chameleon_rs
import chameleon_rs.fastapi as ours
import chameleon_rs.web
from exprtest import exception_name
from fastapi import Response  # the type, for the typing checks

# Starlette 1.x warns that its test client should use httpx2; the integration venv and this
# one use httpx.
with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    from fastapi.testclient import TestClient

ORIGINAL = Path(__file__).resolve().parents[2] / 'temp' / 'fastapi-chameleon'


def load_original() -> ModuleType | None:
    if not (ORIGINAL / 'fastapi_chameleon').is_dir():
        return None
    sys.path.insert(0, str(ORIGINAL))
    try:
        return importlib.import_module('fastapi_chameleon')
    except ImportError:
        return None
    finally:
        sys.path.remove(str(ORIGINAL))


ORIGINAL_MODULE = load_original()


@pytest.fixture
def theirs() -> ModuleType:
    if ORIGINAL_MODULE is None:
        pytest.skip('temp/fastapi-chameleon is not checked out (make integration)')
    return ORIGINAL_MODULE


TEMPLATES = {
    'home/index.pt': '<h1>Hello ${a} ${b|"-"}!</h1>',
    'errors/404.pt': '<h1>Not here ${what | "?"}</h1>',
    'errors/500.pt': '<h1>Broken ${code | 0}</h1>',
    'test_fastapi/bare.pt': '<p>bare ${a}</p>',
    'test_fastapi/both.html': '<p>html ${a}</p>',
    'test_fastapi/both.pt': '<p>pt ${a}</p>',
    'shared/_p.pt': '<i>${v}</i>',
}


@pytest.fixture
def folder(tmp_path: Path) -> Any:
    for name, body in TEMPLATES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    if ORIGINAL_MODULE is not None:
        ORIGINAL_MODULE.global_init(str(tmp_path), cache_init=False)
    ours.global_init(str(tmp_path), cache_init=False)
    yield tmp_path
    if ORIGINAL_MODULE is not None:
        ORIGINAL_MODULE.engine.clear()
    ours.clear()


def described(resp: Any) -> tuple[Any, ...]:
    return ('ok', type(resp).__name__, resp.status_code, resp.headers.get('content-type'), bytes(resp.body))


def result(module: ModuleType, make_view: Callable[[ModuleType], Callable[..., Any]], *args: Any) -> tuple[Any, ...]:
    try:
        view = make_view(module)
        resp = view(*args)
        if asyncio.iscoroutine(resp):
            resp = asyncio.run(resp)
    except Exception as e:  # noqa: BLE001 - comparing exceptions across implementations
        return ('raise', exception_name(e), re.sub(r'0x[0-9a-f]+', '0x?', str(e)))
    return described(resp)


def same(make_view: Callable[[ModuleType], Callable[..., Any]], theirs: ModuleType, *args: Any) -> tuple[Any, ...]:
    want = result(theirs, make_view, *args)
    got = result(ours, make_view, *args)
    assert got == want
    return got


def explicit(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        return {'a': a}

    return view


def mimetype_positional(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt', 'text/plain')
    def view(a: Any) -> Any:
        return {'a': a, 'b': 2}

    return view


def mimetype_keyword(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt', mimetype='application/xhtml+xml')
    def view(a: Any) -> Any:
        return {'a': a}

    return view


def async_view(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    async def view(a: Any) -> Any:
        return {'a': a}

    return view


def not_found_default(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        fc.not_found()

    return view


def not_found_keyword(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    async def view(a: Any) -> Any:
        fc.not_found(four04template_file='errors/500.pt')

    return view


def not_found_positional_blank(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        fc.not_found('  ')

    return view


def generic_error_data(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        fc.generic_error('errors/500.pt', 401, template_data={'code': a})

    return view


def generic_error_async(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    async def view(a: Any) -> Any:
        fc.generic_error('errors/500.pt', fastapi.status.HTTP_403_FORBIDDEN)

    return view


def passthrough(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        return fastapi.Response(content='abc', status_code=418)

    return view


def redirect(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    async def view(a: Any) -> Any:
        return fastapi.responses.RedirectResponse('/elsewhere')

    return view


def html_response(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        return fastapi.responses.HTMLResponse('<b>direct</b>')

    return view


def invalid(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        return [a]

    return view


def returns_none(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    async def view(a: Any) -> Any:
        return None

    return view


def missing_template(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/missing.pt')
    def view(a: Any) -> Any:
        return {}

    return view


def bare(fc: ModuleType) -> Callable[..., Any]:
    def bare(a: Any) -> Any:
        return {'a': a}

    bare.__module__ = 'tests.test_fastapi'
    return fc.template(bare)


def bare_called(fc: ModuleType) -> Callable[..., Any]:
    def both(a: Any) -> Any:
        return {'a': a}

    both.__module__ = 'pkg.test_fastapi'
    return fc.template()(both)


def bare_none(fc: ModuleType) -> Callable[..., Any]:
    def nothing_here(a: Any) -> Any:
        return {'a': a}

    nothing_here.__module__ = 'test_fastapi'
    return fc.template(None)(nothing_here)


def response_call(fc: ModuleType) -> Callable[..., Any]:
    return lambda a: fc.response('home/index.pt', status_code=203, a=a)


def response_mimetype(fc: ModuleType) -> Callable[..., Any]:
    return lambda a: fc.response('home/index.pt', 'text/plain', a=a, b='m')


def render_call(fc: ModuleType) -> Callable[..., Any]:
    def view(a: Any) -> Any:
        return fastapi.Response(fc.engine.render('home/index.pt', a=a))

    return view


def error_in_template(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        return {}

    return view


CASES = [
    explicit,
    mimetype_positional,
    mimetype_keyword,
    async_view,
    not_found_default,
    not_found_keyword,
    not_found_positional_blank,
    generic_error_data,
    generic_error_async,
    passthrough,
    redirect,
    html_response,
    invalid,
    returns_none,
    missing_template,
    bare,
    bare_called,
    bare_none,
    response_call,
    response_mimetype,
    render_call,
    error_in_template,
]


@pytest.mark.parametrize('make_view', CASES, ids=[c.__name__ for c in CASES])
def test_same_as_fastapi_chameleon(folder: Path, theirs: ModuleType, make_view: Callable[..., Any]) -> None:
    got = same(make_view, theirs, '<x>')
    if make_view is error_in_template:
        assert got[:2] == ('raise', 'NameError')
    if make_view in (invalid, returns_none):
        assert got[:2] == ('raise', 'FastAPIChameleonException')
    if make_view is missing_template:
        assert got[:2] == ('raise', 'ValueError')


def _async_copy(f: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(f)
    async def view(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0)
        value = f(*args, **kwargs)
        return await value if asyncio.iscoroutine(value) else value

    return view


def as_async(make_view: Callable[[ModuleType], Callable[..., Any]]) -> Callable[[ModuleType], Callable[..., Any]]:
    """The case with every view the decorator gets made an async view."""

    def make(fc: ModuleType) -> Callable[..., Any]:
        real = fc.template

        def template(*args: Any, **kwargs: Any) -> Any:
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return real(_async_copy(args[0]))
            decorator = real(*args, **kwargs)
            return lambda f: decorator(_async_copy(f))

        proxy = types.SimpleNamespace(**{n: getattr(fc, n) for n in dir(fc) if not n.startswith('__')})
        proxy.template = template
        return make_view(proxy)  # ty: ignore[invalid-argument-type] - a stand-in module

    return make


@pytest.mark.parametrize('make_view', CASES, ids=[c.__name__ for c in CASES])
def test_async_views_same_as_fastapi_chameleon(folder: Path, theirs: ModuleType, make_view: Callable[..., Any]) -> None:
    # Every case as an async view: ours renders with render_async, fastapi-chameleon renders
    # synchronously; with no awaitable value the responses are the same.
    same(as_async(make_view), theirs, '<x>')


def test_uninitialized_render_is_an_error(theirs: ModuleType) -> None:
    theirs.engine.clear()
    ours.clear()
    got = same(explicit, theirs, 1)
    assert got == ('raise', 'FastAPIChameleonException', 'You must call global_init() before rendering templates.')


def test_global_init_checks_the_folder(tmp_path: Path, theirs: ModuleType) -> None:
    for module in (theirs, ours):
        for bad in ('', str(tmp_path / 'missing')):
            with pytest.raises(module.exceptions.FastAPIChameleonException) as info:
                module.global_init(bad, cache_init=False)
            assert 'template_folder' in str(info.value) or 'must be a folder' in str(info.value)
    with pytest.raises(chameleon_rs.web.TemplateError):
        ours.configure(str(tmp_path / 'missing'))


def test_global_init_cache_init(folder: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    other = tmp_path_factory.mktemp('other')
    ours.global_init(str(other))  # ignored: already configured
    assert ours.template_path == str(folder)
    ours.global_init(str(other), cache_init=False)
    assert ours.template_path == str(other)
    ours.configure(str(folder))  # always applies
    assert ours.template_path == str(folder)
    ours.clear()
    assert ours.template_path is None
    assert ours.engine is ours.exceptions is ours


def test_settings_slots_are_separate(folder: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    # As the original libraries: fastapi-chameleon's `global_init` configures its own views
    # only; `chameleon_rs.configure()` configures every integration and the partials.
    import chameleon_rs.flask

    other = tmp_path_factory.mktemp('slots')
    chameleon_rs.flask.clear()
    chameleon_rs.partials.clear()
    ours.global_init(str(other), cache_init=False)
    assert chameleon_rs.flask.template_path is None
    assert chameleon_rs.partials.template_path is None
    chameleon_rs.configure(template_folder=str(folder))
    assert ours.template_path == chameleon_rs.flask.template_path == chameleon_rs.partials.template_path
    ours.clear()
    assert ours.template_path is None
    assert chameleon_rs.flask.template_path == str(folder)
    chameleon_rs._settings.clear()


# --- Whole apps -----------------------------------------------------------------------------


def build_app(fc: ModuleType) -> Any:
    app = fastapi.FastAPI()
    router = fastapi.APIRouter()

    def token(t: str = 'none') -> str:
        return t

    @app.get('/items/{n}')
    @fc.template('home/index.pt')
    async def item(n: int, t: str = fastapi.Depends(token)) -> dict[str, Any]:
        if n == 0:
            fc.not_found()
        if n == 1:
            fc.generic_error('errors/500.pt', 409, {'code': 'c'})
        return {'a': n, 'b': t}

    @app.get('/sync/{name}')
    @fc.template('home/index.pt', 'text/html')
    def sync(name: str, q: int = 3) -> dict[str, Any]:
        return {'a': name, 'b': q}

    @router.post('/form')
    @fc.template('home/index.pt')
    async def form(payload: dict[str, Any]) -> Any:
        if payload.get('raw'):
            return fastapi.responses.JSONResponse(payload, status_code=202)
        return payload

    app.include_router(router, prefix='/r')
    return app


REQUESTS = [
    ('get', '/items/5', None),
    ('get', '/items/5?t=tok', None),
    ('get', '/items/0', None),
    ('get', '/items/1', None),
    ('get', '/items/x', None),
    ('get', '/sync/ab%3Cc', None),
    ('get', '/sync/ab?q=nope', None),
    ('post', '/r/form', {'a': 'p', 'b': '<&>'}),
    ('post', '/r/form', {'raw': True}),
    ('post', '/r/form', None),
    ('get', '/missing', None),
]


def test_apps_same_as_fastapi_chameleon(folder: Path, theirs: ModuleType) -> None:
    results = []
    for module in (theirs, ours):
        with TestClient(build_app(module)) as client:
            results.append(
                [
                    (r.status_code, r.headers.get('content-type'), r.text)
                    for r in (client.request(method, url, json=body) for method, url, body in REQUESTS)
                ]
            )
    assert results[0] == results[1]
    statuses = [status for status, _, _ in results[1]]
    assert statuses == [200, 200, 404, 409, 422, 200, 422, 200, 202, 422, 404]


def test_openapi_is_the_views_own(folder: Path, theirs: ModuleType) -> None:
    # The decorated view keeps its signature: parameters, dependencies, validation and the
    # schema are the view's (and the same as with fastapi-chameleon).
    assert build_app(ours).openapi() == build_app(theirs).openapi()

    def plain(fc: ModuleType) -> Any:
        app = fastapi.FastAPI()

        @app.get('/sync/{name}')
        def sync(name: str, q: int = 3) -> dict[str, Any]:
            return {'a': name, 'b': q}

        return app

    decorated = fastapi.FastAPI()

    @decorated.get('/sync/{name}')
    @ours.template('home/index.pt')
    def sync(name: str, q: int = 3) -> dict[str, Any]:
        return {'a': name, 'b': q}

    assert decorated.openapi() == plain(ours).openapi()


def test_async_views_render_on_the_loop_and_sync_views_in_the_threadpool(folder: Path) -> None:
    app = fastapi.FastAPI()
    seen: dict[str, int] = {}

    async def later(value: Any) -> Any:
        seen['await'] = threading.get_ident()
        await asyncio.sleep(0)
        return value

    @app.get('/async')
    @ours.template('home/index.pt')
    async def on_loop() -> Any:
        seen['async'] = threading.get_ident()
        # Awaitable values are awaited where the template uses them (render_async).
        return {'a': later('<x>'), 'b': later(2)}

    @app.get('/sync')
    @ours.template('home/index.pt')
    def in_pool() -> Any:
        seen['sync'] = threading.get_ident()
        return {'a': 's'}

    with TestClient(app) as client:
        assert client.get('/async').text == '<h1>Hello &lt;x&gt; 2!</h1>'
        assert client.get('/sync').text == '<h1>Hello s -!</h1>'
    assert seen['await'] == seen['async'] != seen['sync']


def test_render_async_and_response_async(folder: Path) -> None:
    async def later(value: Any) -> Any:
        await asyncio.sleep(0)
        return value

    async def main() -> tuple[str, Any]:
        html = await ours.render_async('home/index.pt', a=later(1))
        resp = await ours.response_async('errors/500.pt', status_code=503, code=later(7))
        return html, resp

    html, resp = asyncio.run(main())
    assert html == '<h1>Hello 1 -!</h1>'
    assert (resp.status_code, resp.headers['content-type'], resp.body) == (
        503,
        'text/html; charset=utf-8',
        b'<h1>Broken 7</h1>',
    )
    assert ours.render('home/index.pt', a=1) == '<h1>Hello 1 -!</h1>'


def call(view: Callable[..., Any], *args: Any) -> Any:
    resp = view(*args)
    return asyncio.run(resp) if asyncio.iscoroutine(resp) else resp


def test_not_found_and_error_take_data(folder: Path) -> None:
    @ours.template('home/index.pt')
    def missing() -> Any:
        ours.not_found(what='the page')

    @ours.template('home/index.pt')
    async def broken() -> Any:
        ours.error('errors/500.pt', 503, code=7)

    @ours.template('home/index.pt')
    def named() -> Any:
        ours.not_found(template_file='errors/500.pt')

    @ours.template('home/index.pt')
    def shared() -> Any:
        raise chameleon_rs.web.NotFound(template_data={'what': 'web'})

    for view, status, body in [
        (missing, 404, b'<h1>Not here the page</h1>'),
        (broken, 503, b'<h1>Broken 7</h1>'),
        (named, 404, b'<h1>Broken 0</h1>'),
        (shared, 404, b'<h1>Not here web</h1>'),
    ]:
        resp = call(view)
        assert (resp.status_code, resp.body) == (status, body)


@dataclasses.dataclass
class Model:
    a: Any
    b: Any = 'dc'


def test_views_may_return_models(folder: Path) -> None:
    pydantic = pytest.importorskip('pydantic')

    class Page(pydantic.BaseModel):
        a: str

    for value, body in [
        (Model('x'), b'<h1>Hello x dc!</h1>'),
        (Page(a='p'), b'<h1>Hello p -!</h1>'),
        (types.MappingProxyType({'a': 'mp'}), b'<h1>Hello mp -!</h1>'),
    ]:

        @ours.template('home/index.pt', content_type='text/x-test', status_code=299)
        def view() -> Any:
            return value  # noqa: B023 - called at once

        resp = call(view)
        assert (resp.body, resp.headers['content-type'], resp.status_code) == (body, 'text/x-test; charset=utf-8', 299)


def test_exceptions_share_a_base() -> None:
    assert issubclass(ours.FastAPIChameleonException, chameleon_rs.web.TemplateError)
    assert issubclass(ours.FastAPIChameleonNotFoundException, chameleon_rs.web.NotFound)
    assert issubclass(ours.FastAPIChameleonNotFoundException, ours.FastAPIChameleonException)
    assert issubclass(ours.FastAPIChameleonGenericException, chameleon_rs.web.ErrorPage)
    e = ours.FastAPIChameleonNotFoundException('m', 'x.pt')
    assert (e.template_file, e.message, e.status_code) == ('x.pt', 'm', 404)
    g = ours.FastAPIChameleonGenericException('e.pt', 401, 'm', template_data={'k': 1})
    assert (g.template_file, g.status_code, g.message, g.template_data) == ('e.pt', 401, 'm', {'k': 1})


def test_partials_and_concurrent_views(folder: Path) -> None:
    import time

    (folder / 'home' / 'cards.pt').write_text(
        '<ul><tal:x repeat="i items">${render_partial("shared/_p.pt", v=fetch(i))}</tal:x></ul>'
    )
    app = fastapi.FastAPI()

    async def fetch(i: int) -> int:
        await asyncio.sleep(0.05)
        return i * 10

    @app.get('/')
    @ours.template('home/cards.pt', concurrent=True)
    async def cards() -> Any:
        return {'items': range(6), 'fetch': fetch}

    with TestClient(app) as client:
        start = time.perf_counter()
        body = client.get('/').text
        assert time.perf_counter() - start < 0.25
    assert body == '<ul>' + ''.join(f'<i>{i * 10}</i>' for i in range(6)) + '</ul>'


# --- The decorator-order check --------------------------------------------------------------


def misordered_app(**configure: Any) -> tuple[Any, Callable[..., Any]]:
    app = fastapi.FastAPI()
    ours.configure(app=app, **configure)
    router = fastapi.APIRouter()

    @app.get('/fine')
    @ours.template('home/index.pt')
    def fine() -> Any:
        return {'a': 1}

    @ours.template('home/index.pt')
    @router.get('/detail/{n}')
    def detail(n: int) -> Any:
        return {'a': n}

    app.include_router(router, prefix='/r')
    return app, detail


def test_misordered_decorators_fail_at_startup(folder: Path) -> None:
    app, _ = misordered_app()
    with pytest.raises(chameleon_rs.web.DecoratorOrderError) as info, TestClient(app):
        pass
    message = str(info.value)
    assert message.startswith('@template must be placed below the route decorator (@app.get, @router.get')
    assert 'on misordered_app.<locals>.detail() at ' in message
    assert '(route GET /r/detail/{n})' in message
    assert 'fine' not in message


def test_misordered_decorators_fail_before_the_first_request_without_lifespan(folder: Path) -> None:
    app, _ = misordered_app()
    client = TestClient(app)  # not entered: no lifespan events
    for _ in range(2):
        with pytest.raises(chameleon_rs.web.DecoratorOrderError):
            client.get('/fine')


def test_correct_order_starts_and_serves(folder: Path) -> None:
    app = fastapi.FastAPI()
    ours.configure(app=app)
    ours.configure(app=app)  # watching twice checks once

    @app.get('/')
    @ours.template('home/index.pt')
    async def index() -> Any:
        return {'a': 1}

    stack: list[Any] = []
    with TestClient(app) as client:
        assert client.get('/').text == '<h1>Hello 1 -!</h1>'
        stack.append(app.middleware_stack)
    # The first-request check took itself out of the stack.
    assert type(stack[0]).__name__ != '_FirstRequestCheck'
    plain = TestClient(app)
    assert plain.get('/').text == '<h1>Hello 1 -!</h1>'


def test_views_registered_raw_are_not_flagged(folder: Path) -> None:
    app = fastapi.FastAPI()
    ours.global_init(str(folder), app=app)

    @app.get('/json')
    def raw() -> dict[str, int]:
        return {'a': 1}

    # The same function registered raw on purpose and, decorated, on another route.
    def both() -> dict[str, int]:
        return {'a': 2}

    app.get('/both.json')(both)
    app.get('/both.html')(ours.template('home/index.pt')(both))

    with TestClient(app) as client:
        assert client.get('/json').json() == {'a': 1}
        assert client.get('/both.json').json() == {'a': 2}
        assert client.get('/both.html').text == '<h1>Hello 2 -!</h1>'


def test_check_views_finds_mounted_apps_and_wrapped_views(folder: Path) -> None:
    app = fastapi.FastAPI()
    sub = fastapi.FastAPI()

    def login_required(f: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return f(*args, **kwargs)

        return wrapper

    @app.get('/ok')
    @login_required
    @ours.template('home/index.pt')
    def guarded() -> Any:
        return {'a': 1}

    @ours.template('home/index.pt')
    @login_required
    @sub.get('/inner')
    def inner() -> Any:
        return {'a': 2}

    app.mount('/sub', sub)
    with pytest.raises(chameleon_rs.web.DecoratorOrderError) as info:
        ours.check_views(app)
    message = str(info.value)
    assert 'inner() at ' in message
    assert '(route GET /sub/inner)' in message
    assert 'guarded' not in message
    ours.check_views(fastapi.FastAPI())  # nothing to flag


def test_import_does_not_check_unwatched_apps(folder: Path) -> None:
    # Without configure(app=...) nothing is hooked into the app.
    app = fastapi.FastAPI()

    @ours.template('home/index.pt')
    @app.get('/x')
    def x() -> Any:
        return {'a': 1}

    with TestClient(app) as client:
        assert client.get('/x').json() == {'a': 1}


# --- Typing (checked by ty, not run) --------------------------------------------------------


def _typing_checks() -> None:
    @ours.template('home/index.pt')
    def view(n: int, name: str = 'x') -> dict[str, Any]:
        return {'a': n}

    @ours.template
    async def aview(n: int) -> dict[str, Any]:
        return {'a': n}

    @ours.template('home/index.pt', 'text/plain', 201, concurrent=True)
    async def bview() -> Any:
        return {}

    assert_type(view(1, name='y'), Response)
    assert_type(aview(1), Coroutine[Any, Any, Response])
    assert_type(bview(), Coroutine[Any, Any, Response])
    assert_type(ours.response('x.pt', 'text/plain', 200, a=1), Response)


# --- Review fixes ---------------------------------------------------------------------------


def test_bound_methods_routed_raw_and_decorated_are_not_flagged(folder: Path) -> None:
    class Views:
        def page(self) -> dict[str, int]:
            return {'a': 1}

    views = Views()
    app = fastapi.FastAPI()
    app.get('/api')(views.page)
    app.get('/html')(ours.template('home/index.pt')(views.page))
    ours.check_views(app)

    misordered = fastapi.FastAPI()
    misordered.get('/html')(views.page)
    ours.template('home/index.pt')(views.page)
    with pytest.raises(chameleon_rs.web.DecoratorOrderError, match=r'Views\.page\(\)'):
        ours.check_views(misordered)


def test_a_wrapper_used_elsewhere_needs_the_escape_hatch(folder: Path) -> None:
    # Routed raw here, its @template wrapper called from another view: indistinguishable from
    # the mistake at runtime, so `ignore=` or `check_order=False`.
    def data() -> dict[str, int]:
        return {'a': 2}

    html = ours.template('home/index.pt')(data)

    def make() -> Any:
        app = fastapi.FastAPI()
        app.get('/api')(data)

        @app.get('/html')
        def page() -> Any:
            return html()

        return app

    with pytest.raises(chameleon_rs.web.DecoratorOrderError):
        ours.check_views(make())
    ours.check_views(make(), ignore=[data])
    ours.check_views(make(), ignore=[html])
    app = make()
    ours.configure(app=app, check_order=False)
    with TestClient(app) as client:
        assert client.get('/html').text == '<h1>Hello 2 -!</h1>'
    app = make()
    ours.global_init(str(folder), app=app, check_order=False)
    assert app not in chameleon_rs._views._watched


def test_the_check_runs_once(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real = chameleon_rs._views.Integration.check_views

    def counting(self: Any, app: Any, ignore: Any = ()) -> None:
        calls.append(app)
        real(self, app, ignore)

    monkeypatch.setattr(chameleon_rs._views.Integration, 'check_views', counting)
    app = fastapi.FastAPI()
    ours.configure(app=app)

    @app.get('/')
    @ours.template('home/index.pt')
    def index() -> Any:
        return {'a': 1}

    with TestClient(app) as client:
        for _ in range(3):
            client.get('/')
    plain = TestClient(app)
    for _ in range(3):
        plain.get('/')
    assert len(calls) == 1


def test_app_must_be_an_application(folder: Path) -> None:
    router = fastapi.APIRouter()
    with pytest.raises(TypeError, match='FastAPI or Starlette application, not APIRouter'):
        ours.configure(app=router)
    with pytest.raises(TypeError, match='FastAPI or Starlette application'):
        ours.check_views(router)
    with pytest.raises(TypeError):
        ours.global_init(str(folder), app=router)


def test_global_init_checks_the_folder_before_hooking_the_app(tmp_path: Path) -> None:
    app = fastapi.FastAPI()
    with pytest.raises(ours.FastAPIChameleonException):
        ours.global_init(str(tmp_path / 'missing'), cache_init=False, app=app)
    assert app not in chameleon_rs._views._watched
    with pytest.raises(ours.FastAPIChameleonException):
        ours.configure(str(tmp_path / 'missing'), app=app)
    assert app not in chameleon_rs._views._watched


def test_a_user_lifespan_and_its_state_pass_through(folder: Path) -> None:
    import contextlib

    events = []

    @contextlib.asynccontextmanager
    async def lifespan(app: Any) -> Any:
        events.append('start')
        yield {'greeting': 'hi'}
        events.append('end')

    app = fastapi.FastAPI(lifespan=lifespan)

    @app.get('/')
    @ours.template('home/index.pt')
    def index(request: fastapi.Request) -> Any:
        return {'a': request.state.greeting}

    ours.configure(app=app)
    with TestClient(app) as client:
        assert client.get('/').text == '<h1>Hello hi -!</h1>'
    assert events == ['start', 'end']


def test_module_imported_after_configure_knows_the_folder(folder: Path) -> None:
    import subprocess

    code = (
        'import chameleon_rs, chameleon_rs.compat\n'
        f'chameleon_rs.configure({str(folder)!r})\n'
        'chameleon_rs.compat.install()\n'
        'import fastapi_chameleon, chameleon_rs.flask\n'
        'print(fastapi_chameleon.template_path == chameleon_rs.flask.template_path == '
        f'{str(folder)!r})\n'
        'print(fastapi_chameleon.__author__, fastapi_chameleon.__version__ == chameleon_rs.__version__)\n'
    )
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=False)
    assert result.stdout == 'True\nMichael Kennedy <michael@talkpython.fm> True\n', result.stderr


def test_reimporting_does_not_pile_up_listeners() -> None:
    before = dict(chameleon_rs._settings.listeners)
    importlib.reload(ours)
    importlib.reload(ours)
    assert chameleon_rs._settings.listeners.keys() == before.keys()
