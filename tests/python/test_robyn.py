"""`chameleon_rs.robyn`: chameleon-robyn's API built in. Each differential case runs the same
handler through the original chameleon-robyn (pinned clone in temp/, on stock Chameleon) and
through `chameleon_rs.robyn`, as a sync and as an async handler, and compares the response
(type, status, every header, body) or the exception; whole apps are compared through Robyn's
`TestClient`. Then what chameleon-robyn does not have (async handlers rendering with
`render_async`, models, `error()`, the decorator-order check) and a real Robyn server on a free
local port.

Robyn ships wheels for CPython 3.10-3.14 only: on 3.15 and free-threaded builds this module is
skipped (`make test-robyn` runs it in a 3.14 venv)."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import importlib
import os
import re
import socket
import subprocess
import sys
import textwrap
import time
import types
import urllib.error
import urllib.request
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import ModuleType
from typing import Any, assert_type

import pytest

robyn = pytest.importorskip('robyn')

import chameleon_rs
import chameleon_rs.robyn as ours
import chameleon_rs.web
from robyn import Headers, Response, Robyn, SubRouter  # the types, for the typing checks
from robyn.testing import TestClient as RobynClient

ORIGINAL = Path(__file__).resolve().parents[2] / 'temp' / 'chameleon-robyn'


def load_original() -> ModuleType | None:
    if not (ORIGINAL / 'chameleon_robyn').is_dir():
        return None
    sys.path.insert(0, str(ORIGINAL))
    try:
        return importlib.import_module('chameleon_robyn')
    except ImportError:
        return None
    finally:
        sys.path.remove(str(ORIGINAL))


ORIGINAL_MODULE = load_original()


@pytest.fixture
def theirs() -> ModuleType:
    if ORIGINAL_MODULE is None:
        pytest.skip('temp/chameleon-robyn is not checked out (make integration)')
    return ORIGINAL_MODULE


TEMPLATES = {
    'home/index.pt': '<h1>Hello ${a} ${b|"-"}!</h1>',
    'errors/404.pt': '<h1>Not here ${what | "?"}</h1>',
    'errors/404_message.pt': '<h1>${message}</h1>',
    'errors/500.pt': '<h1>Broken ${code | 0} ${message | ""}</h1>',
    'test_robyn/bare.pt': '<p>bare ${a}</p>',
    'test_robyn/both.html': '<p>html ${a}</p>',
    'test_robyn/both.pt': '<p>pt ${a}</p>',
    'shared/_p.pt': '<i>${v}</i>',
}


def write_templates(folder: Path) -> None:
    for name, body in TEMPLATES.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


@pytest.fixture
def folder(tmp_path: Path) -> Any:
    write_templates(tmp_path)
    if ORIGINAL_MODULE is not None:
        ORIGINAL_MODULE.global_init(str(tmp_path), cache_init=False)
    ours.global_init(str(tmp_path), cache_init=False)
    yield tmp_path
    if ORIGINAL_MODULE is not None:
        ORIGINAL_MODULE.engine.clear()
    ours.clear()


def described(resp: Any) -> tuple[Any, ...]:
    cookies = repr(getattr(resp, 'cookies', None))
    return ('ok', type(resp).__name__, resp.status_code, resp.headers.get_headers(), resp.description, cookies)


def result(module: ModuleType, make_view: Callable[..., Callable[..., Any]], style: str, *args: Any) -> tuple[Any, ...]:
    try:
        view = make_view(module, style)
        resp = view(*args)
        if asyncio.iscoroutine(resp):
            resp = asyncio.run(resp)
    except Exception as e:  # noqa: BLE001 - comparing exceptions across implementations
        return ('raise', type(e).__name__, re.sub(r'0x[0-9a-f]+', '0x?', str(e)))
    return described(resp)


def same(make_view: Callable[..., Callable[..., Any]], theirs: ModuleType, style: str, *args: Any) -> tuple[Any, ...]:
    want = result(theirs, make_view, style, *args)
    got = result(ours, make_view, style, *args)
    assert got == want
    return got


def handler(style: str, body: Callable[..., Any]) -> Callable[..., Any]:
    """``body`` as a sync or an async handler (same name, module and parameters)."""
    if style == 'sync':

        @functools.wraps(body)
        def view(*args: Any, **kwargs: Any) -> Any:
            return body(*args, **kwargs)

        return view

    @functools.wraps(body)
    async def async_view(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0)
        return body(*args, **kwargs)

    return async_view


def explicit(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt')(handler(style, lambda a: {'a': a}))


def options(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt', content_type='application/xml', status_code=201)(
        handler(style, lambda a: {'a': a, 'b': 2})
    )


def positional_options(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt', 'text/plain', 202)(handler(style, lambda a: {'a': a}))


def not_found_default(cr: ModuleType, style: str) -> Callable[..., Any]:
    def body(a: Any) -> Any:
        cr.not_found()

    return cr.template('home/index.pt', status_code=201)(handler(style, body))


def not_found_keyword(cr: ModuleType, style: str) -> Callable[..., Any]:
    def body(a: Any) -> Any:
        cr.not_found(four04template_file='errors/404_message.pt')

    return cr.template('home/index.pt', content_type='text/plain')(handler(style, body))


def not_found_positional(cr: ModuleType, style: str) -> Callable[..., Any]:
    def body(a: Any) -> Any:
        cr.not_found('errors/500.pt')

    return cr.template('home/index.pt')(handler(style, body))


def not_found_blank(cr: ModuleType, style: str) -> Callable[..., Any]:
    def body(a: Any) -> Any:
        cr.not_found('  ')

    return cr.template('home/index.pt')(handler(style, body))


def passthrough(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt')(
        handler(style, lambda a: Response(status_code=418, description='abc', headers=Headers({'X-A': '1'})))
    )


def invalid_list(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt')(handler(style, lambda a: [a]))


def invalid_none(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt')(handler(style, lambda a: None))


def invalid_string(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt')(handler(style, lambda a: 'just a string'))


def missing_template(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/missing.pt')(handler(style, lambda a: {}))


def bare(cr: ModuleType, style: str) -> Callable[..., Any]:
    def bare(a: Any) -> Any:
        return {'a': a}

    bare.__module__ = 'tests.test_robyn'
    return cr.template(handler(style, bare))


def bare_called_html(cr: ModuleType, style: str) -> Callable[..., Any]:
    def both(a: Any) -> Any:
        return {'a': a}

    both.__module__ = 'pkg.test_robyn'
    return cr.template()(handler(style, both))


def bare_none(cr: ModuleType, style: str) -> Callable[..., Any]:
    def nothing(a: Any) -> Any:
        return {}

    return cr.template(None)(handler(style, nothing))


def dict_subclass(cr: ModuleType, style: str) -> Callable[..., Any]:
    class Model(dict):
        pass

    return cr.template('home/index.pt')(handler(style, lambda a: Model(a=a, b='sub')))


def callback(cr: ModuleType, style: str) -> Callable[..., Any]:
    def hook(resp: Any) -> None:
        resp.headers.set('X-Hook', 'called')
        resp.set_cookie('session', 'abc')

    return cr.template('home/index.pt')(handler(style, lambda a: {'a': a, '__response_callback__': hook}))


def callback_none(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt')(handler(style, lambda a: {'a': a, '__response_callback__': None}))


def callback_not_callable(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt')(handler(style, lambda a: {'a': a, '__response_callback__': 'nope'}))


def callback_invalid_return_first(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt')(handler(style, lambda a: 42))


def response_call(cr: ModuleType, style: str) -> Callable[..., Any]:
    return lambda a: cr.response('home/index.pt', status_code=203, a=a)


def response_content_type(cr: ModuleType, style: str) -> Callable[..., Any]:
    return lambda a: cr.response('home/index.pt', 'application/xhtml+xml', a=a, b=1)


def render_call(cr: ModuleType, style: str) -> Callable[..., Any]:
    return lambda a: Response(status_code=200, description=cr.engine.render('home/index.pt', a=a), headers=Headers({}))


def error_in_template(cr: ModuleType, style: str) -> Callable[..., Any]:
    return cr.template('home/index.pt')(handler(style, lambda a: {}))


def chameleon_template(cr: ModuleType, style: str) -> Callable[..., Any]:
    folder = cr.engine.template_path

    def view(a: Any) -> Any:
        return cr.ChameleonTemplate(folder).render_template('home/index.pt', a=a)

    return view


CASES = [
    explicit,
    options,
    positional_options,
    not_found_default,
    not_found_keyword,
    not_found_positional,
    not_found_blank,
    passthrough,
    invalid_list,
    invalid_none,
    invalid_string,
    missing_template,
    bare,
    bare_called_html,
    bare_none,
    dict_subclass,
    callback,
    callback_none,
    callback_not_callable,
    callback_invalid_return_first,
    response_call,
    response_content_type,
    render_call,
    error_in_template,
    chameleon_template,
]


@pytest.mark.parametrize('style', ['sync', 'async'])
@pytest.mark.parametrize('make_view', CASES, ids=[c.__name__ for c in CASES])
def test_same_as_chameleon_robyn(folder: Path, theirs: ModuleType, make_view: Callable[..., Any], style: str) -> None:
    # Async handlers: ours renders with render_async, chameleon-robyn synchronously; with no
    # awaitable value the responses are the same.
    got = same(make_view, theirs, style, '<x>')
    if make_view is error_in_template:
        assert got[:2] == ('raise', 'NameError')
    if make_view is explicit:
        assert got[2:5] == (200, {'content-type': ['text/html; charset=utf-8']}, '<h1>Hello &lt;x&gt; -!</h1>')
    if make_view is callback:
        assert got[3]['x-hook'] == ['called']
        assert 'session' in got[5]


def test_uninitialized_render_is_an_error(theirs: ModuleType) -> None:
    theirs.engine.clear()
    ours.clear()
    got = same(explicit, theirs, 'sync', 1)
    assert got == ('raise', 'ChameleonRobynException', 'You must call global_init() before rendering templates.')


def test_global_init_checks_the_folder(tmp_path: Path, theirs: ModuleType) -> None:
    for module in (theirs, ours):
        for bad in ('', str(tmp_path / 'missing')):
            with pytest.raises(module.ChameleonRobynException) as info:
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


def test_bare_name_is_decided_at_the_first_request(
    tmp_path: Path, theirs: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # chameleon-robyn: decorated before `global_init()`, looked up at each request ('templates'
    # meanwhile) and fixed once the folder is configured.
    write_templates(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    theirs.engine.clear()
    ours.clear()

    (tmp_path / 'test_robyn' / 'late.pt').write_text('<p>late pt ${a}</p>')

    def late(a: Any) -> Any:
        return {'a': a}

    late.__module__ = 'test_robyn'
    views = {m: m.template(late) for m in (theirs, ours)}
    for module, view in views.items():
        with pytest.raises(module.ChameleonRobynException):
            view(1)
    assert ours.template_path is None
    for module in views:
        module.global_init(str(tmp_path), cache_init=False)
    try:
        assert described(views[ours](1)) == described(views[theirs](1))
        assert views[ours](1).description == '<p>late pt 1</p>'
        # Fixed once the folder is known: an .html file added later is not looked for.
        (tmp_path / 'test_robyn' / 'late.html').write_text('<p>late html ${a}</p>')
        assert described(views[ours](2)) == described(views[theirs](2))
        assert views[ours](2).description == '<p>late pt 2</p>'
    finally:
        theirs.engine.clear()
        ours.clear()


def test_template_reuse_and_model_not_mutated(folder: Path) -> None:
    shared = ours.template()
    calls: list[Any] = []
    model = {'a': 1, '__response_callback__': calls.append}

    def bare(a: Any) -> Any:
        return model

    def both(a: Any) -> Any:
        return model

    bare.__module__ = both.__module__ = 'test_robyn'
    first, second = shared(bare), shared(both)
    assert (first(0).description, second(0).description) == ('<p>bare 1</p>', '<p>html 1</p>')
    assert '__response_callback__' in model
    assert len(calls) == 2


# --- whole apps through Robyn's TestClient --------------------------------------------------


def build_app(cr: ModuleType) -> Robyn:
    app = Robyn(__file__)

    @app.get('/')
    @cr.template('home/index.pt')
    # No annotations on routed handlers: Robyn reads them, and they are strings in this module.
    def index(request):
        return {'a': request.query_params.get('a', 'none')}

    @app.get('/user/:name')
    @cr.template('home/index.pt', status_code=201)
    async def user(name):
        await asyncio.sleep(0)
        return {'a': name, 'b': 'async'}

    @app.get('/missing')
    @cr.template('home/index.pt')
    def missing() -> Any:
        cr.not_found('errors/404_message.pt')

    @app.get('/teapot')
    @cr.template('home/index.pt')
    async def teapot() -> Any:
        return Response(status_code=418, description='short and stout', headers=Headers({}))

    def hook(resp: Any) -> None:
        resp.set_cookie('session', 'abc')
        resp.headers.set('X-Session', 'abc')

    @app.post('/cookie')
    @cr.template('home/index.pt')
    def cookie(body):
        return {'a': body, '__response_callback__': hook}

    @app.get('/plain')
    @cr.template('home/index.pt', content_type='text/plain')
    def plain() -> Any:
        return {'a': 'p'}

    users = SubRouter(prefix='/sub')

    @users.get('/page')
    @cr.template('home/index.pt')
    def sub_page() -> Any:
        return {'a': 'sub'}

    app.include_router(users)
    return app


REQUESTS: list[tuple[str, str, dict[str, Any]]] = [
    ('get', '/', {}),
    ('get', '/', {'query_params': {'a': '<b>'}}),
    ('get', '/user/ann', {}),
    ('get', '/missing', {}),
    ('get', '/teapot', {}),
    ('post', '/cookie', {'body': 'posted'}),
    ('get', '/plain', {}),
    ('get', '/sub/page', {}),
    ('get', '/nope', {}),
]


def test_app_same_as_chameleon_robyn(folder: Path, theirs: ModuleType) -> None:
    def responses(cr: ModuleType) -> list[tuple[Any, ...]]:
        with RobynClient(build_app(cr)) as client:
            out = []
            for method, path, kwargs in REQUESTS:
                resp = getattr(client, method)(path, **kwargs)
                out.append((resp.status_code, resp.headers.get_headers(), resp.text))
            return out

    got = responses(ours)
    assert got == responses(theirs)
    assert got[1] == (200, {'content-type': ['text/html; charset=utf-8']}, '<h1>Hello &lt;b&gt; -!</h1>')
    assert got[2][::2] == (201, '<h1>Hello ann async!</h1>')
    assert got[3][::2] == (404, '<h1>The URL resulted in a 404 response.</h1>')
    assert got[5][1]['x-session'] == ['abc']


# --- what chameleon-robyn does not have -----------------------------------------------------


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
    def legacy() -> Any:
        ours.generic_error('errors/500.pt', 401, {'code': 8, 'message': 'own'})

    @ours.template('home/index.pt')
    def named() -> Any:
        ours.not_found(template_file='errors/404_message.pt')

    def hook(resp: Any) -> None:
        resp.headers.set('X-Hook', '404')

    @ours.template('home/index.pt')
    async def hooked() -> Any:
        ours.not_found(what='x', __response_callback__=hook)

    resp = call(missing)
    assert (resp.status_code, resp.description) == (404, '<h1>Not here the page</h1>')
    resp = call(broken)
    assert (resp.status_code, resp.description) == (503, '<h1>Broken 7 The URL resulted in an error.</h1>')
    assert resp.headers.get('Content-Type') == 'text/html; charset=utf-8'
    resp = call(legacy)
    assert (resp.status_code, resp.description) == (401, '<h1>Broken 8 own</h1>')
    resp = call(named)
    assert (resp.status_code, resp.description) == (404, '<h1>The URL resulted in a 404 response.</h1>')
    resp = call(hooked)
    assert (resp.status_code, resp.description, resp.headers.get('X-Hook')) == (404, '<h1>Not here x</h1>', '404')


@dataclasses.dataclass
class Model:
    a: Any
    b: Any = 'dc'


class PydanticLike:
    def model_dump(self) -> dict[str, Any]:
        return {'a': 'pyd'}


@pytest.mark.parametrize(
    ('value', 'text'),
    [
        (Model('x'), '<h1>Hello x dc!</h1>'),
        (PydanticLike(), '<h1>Hello pyd -!</h1>'),
        (types.MappingProxyType({'a': 'mp'}), '<h1>Hello mp -!</h1>'),
    ],
)
@pytest.mark.parametrize('style', ['sync', 'async'])
def test_handlers_may_return_models(folder: Path, value: Any, text: str, style: str) -> None:
    view = ours.template('home/index.pt', mimetype='text/x-test')(handler(style, lambda: value))
    resp = call(view)
    assert (resp.description, resp.headers.get('Content-Type')) == (text, 'text/x-test; charset=utf-8')


def test_exceptions_share_a_base() -> None:
    assert issubclass(ours.ChameleonRobynException, chameleon_rs.web.TemplateError)
    assert issubclass(ours.ChameleonRobynNotFoundException, chameleon_rs.web.NotFound)
    assert issubclass(ours.ChameleonRobynNotFoundException, ours.ChameleonRobynException)
    assert issubclass(ours.ChameleonRobynGenericException, chameleon_rs.web.ErrorPage)
    e = ours.ChameleonRobynNotFoundException('m', 'x.pt')
    assert (e.template_file, e.message, e.status_code) == ('x.pt', 'm', 404)
    assert ours.ChameleonRobynNotFoundException().template_file == 'errors/404.pt'
    assert ours.engine is ours.exceptions is ours
    assert ours.__version__ == chameleon_rs.__version__


def test_partials_and_configure(tmp_path: Path) -> None:
    write_templates(tmp_path)
    (tmp_path / 'home' / 'list.pt').write_text(
        '<ul><li tal:repeat="v vs">${render_partial("shared/_p.pt", v=v)}</li></ul>'
    )
    ours.configure(str(tmp_path))
    try:

        @ours.template('home/list.pt')
        def view() -> Any:
            return {'vs': [1, 2]}

        assert call(view).description == '<ul><li><i>1</i></li>\n<li><i>2</i></li></ul>'
        assert ours.template_path == str(tmp_path)
    finally:
        chameleon_rs._settings.clear()
    assert ours.template_path is None


def test_chameleon_rs_configure_sets_the_robyn_slot(tmp_path: Path) -> None:
    write_templates(tmp_path)
    chameleon_rs.configure(str(tmp_path), restricted_namespace=False)
    try:
        assert ours.template_path == str(tmp_path)
        assert ours.render('home/index.pt', a=1) == '<h1>Hello 1 -!</h1>'
    finally:
        chameleon_rs._settings.clear()


def test_async_handlers_render_with_render_async(folder: Path) -> None:
    async def later(value: Any, delay: float = 0.0) -> Any:
        await asyncio.sleep(delay)
        return value

    @ours.template('home/index.pt')
    async def index(delay: float) -> Any:
        return {'a': later('<x>', delay), 'b': later(2)}

    assert call(index, 0.0).description == '<h1>Hello &lt;x&gt; 2!</h1>'

    async def five() -> tuple[list[Any], float]:
        start = time.perf_counter()
        resps = await asyncio.gather(*(index(0.2) for _ in range(5)))
        return resps, time.perf_counter() - start

    # Each render waits 200 ms on its value without blocking the loop: five together take
    # about 200 ms, not 1 s (headroom for a busy machine).
    resps, elapsed = asyncio.run(five())
    assert {r.description for r in resps} == {'<h1>Hello &lt;x&gt; 2!</h1>'}
    assert elapsed < 0.5, elapsed


def test_render_async_response_async_and_template_interface(folder: Path) -> None:
    async def later(value: Any) -> Any:
        await asyncio.sleep(0)
        return value

    engine = ours.ChameleonTemplate(str(folder))
    assert isinstance(engine, ours.TemplateInterface)

    async def main() -> tuple[str, Any, Any]:
        html = await ours.render_async('home/index.pt', a=later(1))
        resp = await ours.response_async('errors/500.pt', status_code=503, code=later(7))
        page = await engine.render_template_async('home/index.pt', a=later('t'))
        return html, resp, page

    html, resp, page = asyncio.run(main())
    assert html == '<h1>Hello 1 -!</h1>'
    assert (resp.status_code, resp.headers.get('Content-Type'), resp.description) == (
        503,
        'text/html; charset=utf-8',
        '<h1>Broken 7 </h1>',
    )
    assert (page.status_code, page.description) == (200, '<h1>Hello t -!</h1>')


def test_chameleon_template_refuses_names_outside_its_folder(folder: Path) -> None:
    # Security: as Robyn's `JinjaTemplate`, a request-chosen name never reaches outside.
    (folder.parent / 'secret.pt').write_text('SECRET')
    engine = ours.ChameleonTemplate(str(folder))
    names = ['../secret.pt', str(folder.parent / 'secret.pt'), 'home/../../secret.pt', 'pkg:x.pt', 'a\\b.pt']
    for name in names:
        with pytest.raises(chameleon_rs.web.TemplateNotFound, match='Template not found'):
            engine.render_template(name)
        with pytest.raises(chameleon_rs.web.TemplateNotFound, match='Template not found'):
            asyncio.run(engine.render_template_async(name))
    assert engine.render_template('home/index.pt', a=1).description == '<h1>Hello 1 -!</h1>'


def test_robyn_render_with_chameleon_template(folder: Path) -> None:
    # Robyn's own `robyn.templating.render(..., template_engine=ChameleonTemplate)`.
    pytest.importorskip('jinja2')  # robyn.templating imports Jinja2
    from robyn.templating import render

    resp = render('home/index.pt', templates_dir=str(folder), template_engine=ours.ChameleonTemplate, a='r')
    assert (resp.status_code, resp.description) == (200, '<h1>Hello r -!</h1>')


def test_concurrent_handler(folder: Path) -> None:
    (folder / 'home' / 'cards.pt').write_text(
        '<ul><tal:x repeat="i items">${render_partial("shared/_p.pt", v=fetch(i))}</tal:x></ul>'
    )

    async def fetch(i: int) -> int:
        await asyncio.sleep(0.1)
        return i * 10

    @ours.template('home/cards.pt', concurrent=True)
    async def cards() -> Any:
        return {'items': range(6), 'fetch': fetch}

    start = time.perf_counter()
    body = call(cards).description
    # Six partials each waiting 100 ms: about 100 ms together, 600 ms one after another.
    assert time.perf_counter() - start < 0.35
    assert body == '<ul>' + ''.join(f'<i>{i * 10}</i>' for i in range(6)) + '</ul>'


def test_offload_renders_in_a_worker_thread(folder: Path) -> None:
    import threading

    (folder / 'home' / 'thread.pt').write_text('<p>${where()}</p>')

    @ours.template('home/thread.pt', offload=True)
    async def view() -> Any:
        return {'where': lambda: threading.current_thread() is threading.main_thread()}

    assert call(view).description == '<p>False</p>'


# --- the decorator-order check --------------------------------------------------------------


def misordered_app(cr: Any = ours) -> tuple[Robyn, Callable[..., Any]]:
    app = Robyn(__file__)

    @cr.template('home/index.pt')
    @app.get('/wrong')
    def wrong() -> Any:
        return {'a': 'wrong'}

    @app.get('/right')
    @cr.template('home/index.pt')
    def right() -> Any:
        return {'a': 'right'}

    return app, wrong


@pytest.fixture
def no_server(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """``Robyn.start()`` without the server: ``run_processes`` records its routes."""
    started: list[Any] = []
    monkeypatch.setattr(robyn, 'run_processes', lambda *args: started.append(args[4]))
    return started


def test_misordered_handler_fails_at_start_and_in_the_test_client(folder: Path, no_server: list[Any]) -> None:
    app, _wrong = misordered_app()
    ours.configure(app=app)
    with pytest.raises(chameleon_rs.web.DecoratorOrderError) as info:
        app.start(_check_port=False)
    message = str(info.value)
    assert 'misordered_app.<locals>.wrong()' in message
    assert '(route GET /wrong)' in message
    assert 'right' not in message
    assert no_server == []
    # Still failing (the guards stay), also for the test client.
    with pytest.raises(chameleon_rs.web.DecoratorOrderError):
        app.start(_check_port=False)
    with pytest.raises(chameleon_rs.web.DecoratorOrderError):
        RobynClient(app)


def test_correct_order_starts_and_unhooks(folder: Path, no_server: list[Any]) -> None:
    app = build_app(ours)
    ours.global_init(str(folder), app=app)
    assert 'start' in vars(app) and 'get_routes' in vars(app.router)
    with RobynClient(app) as client:
        assert client.get('/').text == '<h1>Hello none -!</h1>'
    # Only start() is final: until then the route-table guard stays (routes may still come).
    assert 'start' in vars(app) and 'get_routes' in vars(app.router)
    app.start(_check_port=False)
    assert 'start' not in vars(app) and 'get_routes' not in vars(app.router)
    assert len(no_server) == 1 and any(r.route == '/sub/page' for r in no_server[0])
    # Configuring again does not hook again.
    ours.configure(app=app)
    assert 'start' not in vars(app)


def early_app() -> Robyn:
    # The reviewer's repro: the app reads its route table at import time (a route listing, a
    # sitemap) before a misordered route is added.
    app = Robyn(__file__)
    ours.configure(app=app)

    @app.get('/ok')
    @ours.template('home/index.pt')
    def ok() -> Any:
        return {'a': 1}

    assert len(app.router.get_routes()) == 1

    @ours.template('home/index.pt')
    @app.get('/wrong')
    def wrong() -> Any:
        return {'a': 2}

    return app


def test_an_early_route_listing_does_not_end_the_check(folder: Path, no_server: list[Any]) -> None:
    app = early_app()
    with pytest.raises(chameleon_rs.web.DecoratorOrderError, match='GET /wrong'):
        RobynClient(app)
    with pytest.raises(chameleon_rs.web.DecoratorOrderError, match='GET /wrong'):
        app.start(_check_port=False)
    assert no_server == []
    assert 'start' in vars(app) and 'get_routes' in vars(app.router)
    app = early_app()
    with pytest.raises(chameleon_rs.web.DecoratorOrderError, match='GET /wrong'):
        app.start(_check_port=False)


def test_misordered_on_a_subrouter_and_add_route(folder: Path) -> None:
    app = Robyn(__file__)
    users = SubRouter(prefix='/users')

    @ours.template('home/index.pt')
    @users.get('/list')
    def listing() -> Any:
        return {'a': 1}

    async def raw() -> Any:
        return {'a': 2}

    app.add_route('GET', '/raw', raw)
    ours.template('home/index.pt')(raw)
    app.include_router(users)
    with pytest.raises(chameleon_rs.web.DecoratorOrderError) as info:
        ours.check_views(app)
    assert '(route GET /users/list)' in str(info.value)
    assert '(route GET /raw)' in str(info.value)
    with pytest.raises(chameleon_rs.web.DecoratorOrderError):
        ours.check_views(users)
    ours.check_views(app, ignore=[listing, raw])


def test_escape_hatches_and_app_type(folder: Path, no_server: list[Any]) -> None:
    app, wrong = misordered_app()
    ours.configure(app=app, check_order=False)
    RobynClient(app)
    app.start(_check_port=False)
    assert len(no_server) == 1
    with pytest.raises(chameleon_rs.web.DecoratorOrderError):
        ours.check_views(app)
    ours.check_views(app, ignore=[wrong])
    # A raw handler also routed through its @template wrapper is not the mistake.
    app2 = Robyn(__file__)

    def page() -> Any:
        return {'a': 'json'}

    app2.add_route('GET', '/json', page)
    app2.add_route('GET', '/html', ours.template('home/index.pt')(page))
    ours.check_views(app2)
    with pytest.raises(TypeError, match='SubRouter'):
        ours.configure(app=SubRouter(prefix='/x'))
    with pytest.raises(TypeError):
        ours.global_init(str(folder), app=object())
    with pytest.raises(TypeError):
        ours.check_views(object())


# --- a real Robyn server --------------------------------------------------------------------

SERVER_APP = """
import asyncio, sys
from robyn import Robyn
import chameleon_rs.robyn as chameleon_robyn

app = Robyn(__file__)
chameleon_robyn.configure(template_folder=sys.argv[1], app=app)

async def later(value):
    await asyncio.sleep(0.3)
    return value

@app.get('/')
@chameleon_robyn.template('home/index.pt')
def index(query_params):
    return {'a': query_params.get('a', 'sync')}

@app.get('/slow')
@chameleon_robyn.template('home/index.pt')
async def slow():
    return {'a': later('slow'), 'b': later('async')}

@app.get('/gone')
@chameleon_robyn.template('home/index.pt')
async def gone():
    chameleon_robyn.not_found(what='gone')

if sys.argv[3] == 'misordered':
    @chameleon_robyn.template('home/index.pt')
    @app.get('/wrong')
    def wrong():
        return {'a': 'wrong'}

app.start(host='127.0.0.1', port=int(sys.argv[2]), _check_port=False)
"""


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        if not 15001 <= port <= 15005:
            return port


def fetch(port: int, path: str) -> tuple[int, str | None, str]:
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=10) as r:
            return r.status, r.headers.get('Content-Type'), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Content-Type'), e.read().decode()


def start_server(tmp_path: Path, mode: str) -> tuple[subprocess.Popen[str], int]:
    write_templates(tmp_path)
    script = tmp_path / 'server_app.py'
    script.write_text(textwrap.dedent(SERVER_APP))
    port = free_port()
    env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
    proc = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path), str(port), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    return proc, port


def stop(proc: subprocess.Popen[str]) -> str:
    if proc.poll() is None:
        proc.kill()
    out, _ = proc.communicate(timeout=10)
    return out


@pytest.mark.skipif(sys.platform == 'win32', reason='spawns a Robyn server')
def test_real_server(tmp_path: Path) -> None:
    proc, port = start_server(tmp_path, 'ok')
    try:
        for _ in range(150):
            try:
                fetch(port, '/')
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(stop(proc))
                time.sleep(0.1)
        assert fetch(port, '/?a=query') == (200, 'text/html; charset=utf-8', '<h1>Hello query -!</h1>')
        assert fetch(port, '/gone') == (404, 'text/html; charset=utf-8', '<h1>Not here gone</h1>')
        # Async handlers render with render_async on Robyn's event loop: four requests, each
        # rendering two values that take 300 ms each (600 ms a request, 2.4 s one after
        # another), finish together.
        import concurrent.futures

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            bodies = list(pool.map(lambda _: fetch(port, '/slow'), range(4)))
        elapsed = time.perf_counter() - start
        assert bodies == [(200, 'text/html; charset=utf-8', '<h1>Hello slow async!</h1>')] * 4
        assert elapsed < 1.3, elapsed
    finally:
        stop(proc)


@pytest.mark.skipif(sys.platform == 'win32', reason='spawns a Robyn server')
def test_real_server_misordered_fails_to_start(tmp_path: Path) -> None:
    proc, _port = start_server(tmp_path, 'misordered')
    try:
        proc.wait(timeout=60)
    finally:
        out = stop(proc)
    assert proc.returncode != 0
    assert 'DecoratorOrderError' in out
    assert '(route GET /wrong)' in out


# --- typing ---------------------------------------------------------------------------------


def test_decorated_handler_types(folder: Path) -> None:
    @ours.template('home/index.pt')
    def view(a: int, *, name: str = 'x') -> dict[str, Any]:
        return {'a': a}

    @ours.template('home/index.pt')
    async def aview(a: int) -> Any:
        return {'a': a}

    @ours.template
    def bare(a: int) -> dict[str, Any]:
        return {'a': a}

    @ours.template(content_type='text/plain', concurrent=True)
    async def bview() -> Any:
        return {}

    assert_type(view(1, name='y'), Response)
    assert_type(bare(1), Response)
    for coroutine in (aview(1), bview()):
        assert_type(coroutine, Coroutine[Any, Any, Response])
        coroutine.close()
    assert_type(ours.response('home/index.pt', 'text/plain', 200, a=1), Response)
    assert_type(ours.ChameleonTemplate(str(folder)).render_template('home/index.pt', a=1), Response)
    assert isinstance(view(1), Response)
    assert isinstance(asyncio.run(aview(1)), Response)


def test_every_spelling_of_a_name_is_one_template(folder: Path) -> None:
    # Security: each spelling once compiled and cached its own copy (memory without bound).
    engine = ours.ChameleonTemplate(str(folder))
    for name in ['home/index.pt', './home/index.pt', 'home//index.pt', './/home/./index.pt']:
        assert engine.render_template(name, a=1).description == '<h1>Hello 1 -!</h1>'
    assert len(engine.loader.registry) == 1


def test_folders_and_missing_files_are_template_not_found(folder: Path) -> None:
    engine = ours.ChameleonTemplate(str(folder))
    for name in ['home', 'home/', '.', 'nope.pt']:
        with pytest.raises(chameleon_rs.web.TemplateNotFound):
            engine.render_template(name)
