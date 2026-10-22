"""`chameleon_rs.flask` (Phase 9): chameleon-flask's API built in. Each case runs the same view
through the original chameleon-flask (pinned clone in temp/, on stock Chameleon) and through
`chameleon_rs.flask`, and compares the response (body, status, headers) or the exception."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import importlib
import re
import sys
import types
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

flask = pytest.importorskip('flask')

import chameleon_rs
import chameleon_rs.flask as ours
import chameleon_rs.web

ORIGINAL = Path(__file__).resolve().parents[2] / 'temp' / 'chameleon-flask'


def load_original() -> ModuleType | None:
    if not (ORIGINAL / 'chameleon_flask').is_dir():
        return None
    sys.path.insert(0, str(ORIGINAL))
    try:
        return importlib.import_module('chameleon_flask')
    finally:
        sys.path.remove(str(ORIGINAL))


ORIGINAL_MODULE = load_original()


@pytest.fixture
def theirs() -> ModuleType:
    if ORIGINAL_MODULE is None:
        pytest.skip('temp/chameleon-flask is not checked out (make integration)')
    return ORIGINAL_MODULE


TEMPLATES = {
    'home/index.pt': '<h1>Hello ${a} ${b|"-"}!</h1>${render_partial("shared/_p.pt", v=a) if 0 else ""}',
    'errors/404.pt': '<h1>Not here ${what | "?"}</h1>',
    'errors/500.pt': '<h1>Broken ${code | 0}</h1>',
    'test_flask/bare.pt': '<p>bare ${a}</p>',
    'test_flask/both.html': '<p>html ${a}</p>',
    'test_flask/both.pt': '<p>pt ${a}</p>',
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


def result(module: ModuleType, make_view: Callable[[ModuleType], Callable[..., Any]], *args: Any) -> tuple[Any, ...]:
    try:
        view = make_view(module)
        resp = view(*args)
        if asyncio.iscoroutine(resp):
            resp = asyncio.run(resp)
    except Exception as e:  # noqa: BLE001 - comparing exceptions across implementations
        return ('raise', type(e).__name__, re.sub(r'0x[0-9a-f]+', '0x?', str(e)))
    return ('ok', type(resp).__name__, resp.status_code, resp.headers.get('Content-Type'), resp.get_data(as_text=True))


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


def options(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt', content_type='text/plain', status_code=201)
    def view(a: Any) -> Any:
        return {'a': a, 'b': 2}

    return view


def async_view(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt', status_code=202)
    async def view(a: Any) -> Any:
        return {'a': a}

    return view


def not_found_default(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt', status_code=201)
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


def passthrough(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        return flask.Response(response='abc', status=418)

    return view


def redirect(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        return flask.redirect('/elsewhere')

    return view


def invalid(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        return [a]

    return view


def missing_template(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/missing.pt')
    def view(a: Any) -> Any:
        return {}

    return view


def bare(fc: ModuleType) -> Callable[..., Any]:
    @fc.template
    def bare(a: Any) -> Any:
        return {'a': a}

    bare.__module__ = 'tests.test_flask'
    return fc.template(bare.__wrapped__)


def bare_called(fc: ModuleType) -> Callable[..., Any]:
    def both(a: Any) -> Any:
        return {'a': a}

    both.__module__ = 'pkg.test_flask'
    return fc.template()(both)


def response_call(fc: ModuleType) -> Callable[..., Any]:
    return lambda a: fc.response('home/index.pt', status_code=203, a=a)


def render_call(fc: ModuleType) -> Callable[..., Any]:
    def view(a: Any) -> Any:
        return flask.Response(fc.engine.render('home/index.pt', a=a))

    return view


def error_in_template(fc: ModuleType) -> Callable[..., Any]:
    @fc.template('home/index.pt')
    def view(a: Any) -> Any:
        return {}

    return view


CASES = [
    explicit,
    options,
    async_view,
    not_found_default,
    not_found_keyword,
    not_found_positional_blank,
    passthrough,
    redirect,
    invalid,
    missing_template,
    bare,
    bare_called,
    response_call,
    render_call,
    error_in_template,
]


@pytest.mark.parametrize('make_view', CASES, ids=[c.__name__ for c in CASES])
def test_same_as_chameleon_flask(folder: Path, theirs: ModuleType, make_view: Callable[..., Any]) -> None:
    got = same(make_view, theirs, '<x>')
    if make_view is error_in_template:
        assert got[:2] == ('raise', 'NameError')


def test_uninitialized_render_is_an_error(theirs: ModuleType) -> None:
    theirs.engine.clear()
    ours.clear()
    got = same(explicit, theirs, 1)
    assert got == ('raise', 'FlaskChameleonException', 'You must call global_init() before rendering templates.')


def test_global_init_checks_the_folder(tmp_path: Path, theirs: ModuleType) -> None:
    for module in (theirs, ours):
        for bad in ('', str(tmp_path / 'missing')):
            with pytest.raises(module.FlaskChameleonException) as info:
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


# --- what chameleon-flask does not have -----------------------------------------------------


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
        ours.generic_error('errors/500.pt', 401, {'code': 8})

    @ours.template('home/index.pt')
    def named() -> Any:
        ours.not_found(template_file='errors/500.pt')

    resp = call(missing)
    assert (resp.status_code, resp.get_data(as_text=True)) == (404, '<h1>Not here the page</h1>')
    resp = call(broken)
    assert (resp.status_code, resp.get_data(as_text=True)) == (503, '<h1>Broken 7</h1>')
    resp = call(legacy)
    assert (resp.status_code, resp.get_data(as_text=True)) == (401, '<h1>Broken 8</h1>')
    resp = call(named)
    assert (resp.status_code, resp.get_data(as_text=True)) == (404, '<h1>Broken 0</h1>')


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
        (__import__('types').MappingProxyType({'a': 'mp'}), '<h1>Hello mp -!</h1>'),
    ],
)
def test_views_may_return_models(folder: Path, value: Any, text: str) -> None:
    @ours.template('home/index.pt')
    def view() -> Any:
        return value

    assert call(view).get_data(as_text=True) == text


def test_pydantic_model(folder: Path) -> None:
    pydantic = pytest.importorskip('pydantic')

    class Page(pydantic.BaseModel):
        a: str

    @ours.template('home/index.pt', mimetype='text/x-test')
    def view() -> Any:
        return Page(a='p')

    resp = call(view)
    assert (resp.get_data(as_text=True), resp.content_type) == ('<h1>Hello p -!</h1>', 'text/x-test')


def test_exceptions_share_a_base() -> None:
    assert issubclass(ours.FlaskChameleonException, chameleon_rs.web.TemplateError)
    assert issubclass(ours.FlaskChameleonNotFoundException, chameleon_rs.web.NotFound)
    assert issubclass(ours.FlaskChameleonNotFoundException, ours.FlaskChameleonException)
    assert issubclass(chameleon_rs.partials.PartialsException, chameleon_rs.web.TemplateError)
    e = ours.FlaskChameleonNotFoundException('m', 'x.pt')
    assert (e.template_file, e.message, e.status_code) == ('x.pt', 'm', 404)


def test_partials_in_flask_views(folder: Path) -> None:
    (folder / 'home' / 'list.pt').write_text(
        '<ul><li tal:repeat="v vs">${render_partial("shared/_p.pt", v=v)}</li></ul>'
    )

    @ours.template('home/list.pt')
    def view() -> Any:
        return {'vs': [1, 2]}

    assert call(view).get_data(as_text=True) == '<ul><li><i>1</i></li>\n<li><i>2</i></li></ul>'


def test_per_app_configuration(folder: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    other = tmp_path_factory.mktemp('app')
    (other / 'home').mkdir()
    (other / 'home' / 'index.pt').write_text('<p>app ${a}</p>')
    app = flask.Flask('a')
    ours.configure(str(other), app=app)
    assert app.extensions['chameleon_rs'].template_folder == str(other)

    @app.get('/')
    @ours.template('home/index.pt')
    def index() -> Any:
        return {'a': 1}

    assert app.test_client().get('/').get_data(as_text=True) == '<p>app 1</p>'
    # Outside the app: the process-wide folder.
    assert ours.render('home/index.pt', a=1) == '<h1>Hello 1 -!</h1>'


def test_flask_app_with_async_view(folder: Path) -> None:
    app = flask.Flask('b')

    @app.get('/<int:n>')
    @ours.template('home/index.pt')
    async def index(n: int) -> Any:
        await asyncio.sleep(0)
        if n == 0:
            ours.not_found()
        return {'a': n}

    client = app.test_client()
    assert client.get('/3').get_data(as_text=True) == '<h1>Hello 3 -!</h1>'
    resp = client.get('/0')
    assert (resp.status_code, resp.get_data(as_text=True)) == (404, '<h1>Not here ?</h1>')


def test_async_views_render_with_render_async(folder: Path) -> None:
    # A value the view returns that is awaitable is awaited where the template uses it.
    app = flask.Flask('c')

    async def later(value: Any) -> Any:
        await asyncio.sleep(0)
        return value

    @app.get('/')
    @ours.template('home/index.pt')
    async def index() -> Any:
        return {'a': later('<x>'), 'b': later(2)}

    assert app.test_client().get('/').get_data(as_text=True) == '<h1>Hello &lt;x&gt; 2!</h1>'


def test_render_async_and_response_async(folder: Path) -> None:
    async def later(value: Any) -> Any:
        await asyncio.sleep(0)
        return value

    async def main() -> tuple[str, Any]:
        html = await ours.render_async('home/index.pt', a=later(1))
        with flask.Flask('d').app_context():
            resp = await ours.response_async('errors/500.pt', status_code=503, code=later(7))
        return html, resp

    html, resp = asyncio.run(main())
    assert html == '<h1>Hello 1 -!</h1>'
    assert (resp.status_code, resp.content_type, resp.get_data(as_text=True)) == (
        503,
        'text/html',
        '<h1>Broken 7</h1>',
    )


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
def test_async_views_same_as_chameleon_flask(folder: Path, theirs: ModuleType, make_view: Callable[..., Any]) -> None:
    # Every case as an async view: ours renders with render_async, chameleon-flask renders
    # synchronously; with no awaitable value the responses are the same.
    same(as_async(make_view), theirs, '<x>')


def test_concurrent_view(folder: Path) -> None:
    (folder / 'home' / 'cards.pt').write_text(
        '<ul><tal:x repeat="i items">${render_partial("shared/_p.pt", v=fetch(i))}</tal:x></ul>'
    )
    app = flask.Flask('e')

    async def fetch(i: int) -> int:
        await asyncio.sleep(0.05)
        return i * 10

    @app.get('/')
    @ours.template('home/cards.pt', concurrent=True)
    async def cards() -> Any:
        return {'items': range(6), 'fetch': fetch}

    import time

    start = time.perf_counter()
    body = app.test_client().get('/').get_data(as_text=True)
    assert time.perf_counter() - start < 0.2
    assert body == '<ul>' + ''.join(f'<i>{i * 10}</i>' for i in range(6)) + '</ul>'


def test_concurrent_false_turns_a_page_setting_off(folder: Path) -> None:
    import time

    (folder / 'home' / 'cards2.pt').write_text(
        '<ul><tal:x repeat="i items">${render_partial("shared/_p.pt", v=fetch(i))}</tal:x></ul>'
    )
    app = flask.Flask('f')
    page = ours._page('home/cards2.pt')
    page.concurrent = True

    async def fetch(i: int) -> int:
        await asyncio.sleep(0.03)
        return i

    @app.get('/')
    @ours.template('home/cards2.pt', concurrent=False)
    async def cards() -> Any:
        return {'items': range(4), 'fetch': fetch}

    start = time.perf_counter()
    body = app.test_client().get('/').get_data(as_text=True)
    assert time.perf_counter() - start >= 0.03 * 4
    assert body == '<ul>' + ''.join(f'<i>{i}</i>' for i in range(4)) + '</ul>'


def test_quart(folder: Path) -> None:
    quart = pytest.importorskip('quart')
    app = quart.Quart('q')

    @app.get('/')
    @ours.template('home/index.pt')
    async def index() -> Any:
        return {'a': 'q'}

    async def get() -> tuple[int, str]:
        resp = await app.test_client().get('/')
        return resp.status_code, await resp.get_data(as_text=True)

    assert asyncio.run(get()) == (200, '<h1>Hello q -!</h1>')


# --- The decorator-order check --------------------------------------------------------------


def misordered_flask_app() -> Any:
    app = flask.Flask('misordered')
    ours.configure(app=app)
    blueprint = flask.Blueprint('shop', __name__)

    @app.get('/fine')
    @ours.template('home/index.pt')
    def fine() -> Any:
        return {'a': 1}

    @ours.template('home/index.pt')
    @blueprint.get('/detail/<int:n>')
    def detail(n: int) -> Any:
        return {'a': n}

    app.register_blueprint(blueprint, url_prefix='/shop')
    return app


def test_misordered_decorators_fail_before_the_first_request(folder: Path) -> None:
    app = misordered_flask_app()
    client = app.test_client()
    for _ in range(2):  # every request, until the app is fixed
        with pytest.raises(chameleon_rs.web.DecoratorOrderError) as info:
            client.get('/fine')
    message = str(info.value)
    assert message.startswith('@template must be placed below the route decorator (@app.route, @app.get')
    assert 'on misordered_flask_app.<locals>.detail() at ' in message
    assert '(route GET /shop/detail/<int:n>)' in message
    assert 'fine' not in message
    with pytest.raises(chameleon_rs.web.DecoratorOrderError):
        ours.check_views(app)


def test_correct_order_serves_and_unhooks(folder: Path) -> None:
    app = flask.Flask('ordered')
    original = app.wsgi_app
    ours.configure(app=app)
    ours.configure(app=app)  # watching twice checks once

    @app.get('/')
    @ours.template('home/index.pt')
    def index() -> Any:
        return {'a': 1}

    @app.get('/raw')
    def raw() -> Any:
        return 'raw'

    # The same function registered raw on purpose and, decorated, on another route.
    def both() -> Any:
        return {'a': 2}

    app.add_url_rule('/both.json', 'both_json', both)
    app.add_url_rule('/both.html', 'both_html', ours.template('home/index.pt')(both))

    client = app.test_client()
    assert client.get('/').get_data(as_text=True) == '<h1>Hello 1 -!</h1>'
    assert app.wsgi_app == original  # the one-shot check took itself out
    assert client.get('/raw').get_data(as_text=True) == 'raw'
    assert client.get('/both.json').get_json() == {'a': 2}
    assert client.get('/both.html').get_data(as_text=True) == '<h1>Hello 2 -!</h1>'


def test_global_init_app_checks_and_keeps_process_settings(folder: Path) -> None:
    app = flask.Flask('legacy')
    ours.global_init(str(folder), app=app)
    assert 'chameleon_rs' not in app.extensions

    @ours.template('home/index.pt')
    @app.route('/x')
    def x() -> Any:
        return {'a': 1}

    with pytest.raises(chameleon_rs.web.DecoratorOrderError):
        app.test_client().get('/x')


def test_quart_misordered_fails_at_startup_and_first_request(folder: Path) -> None:
    quart = pytest.importorskip('quart')

    def make() -> Any:
        app = quart.Quart('q-misordered')
        ours.configure(app=app)

        @ours.template('home/index.pt')
        @app.get('/d')
        async def d() -> Any:
            return {'a': 'q'}

        return app

    async def serve(app: Any) -> None:
        async with app.test_app():
            pass

    # Quart reports a failed startup as its LifespanError, with the error's text.
    with pytest.raises(Exception, match='@template must be placed below the route decorator'):
        asyncio.run(serve(make()))

    async def get(app: Any) -> Any:
        return await app.test_client().get('/d')

    with pytest.raises(chameleon_rs.web.DecoratorOrderError):
        asyncio.run(get(make()))


def test_quart_correct_order(folder: Path) -> None:
    quart = pytest.importorskip('quart')
    app = quart.Quart('q-ordered')
    ours.configure(app=app)

    @app.get('/')
    @ours.template('home/index.pt')
    async def index() -> Any:
        return {'a': 'q'}

    async def run() -> tuple[int, str]:
        async with app.test_app() as test_app:
            resp = await test_app.test_client().get('/')
            return resp.status_code, await resp.get_data(as_text=True)

    assert asyncio.run(run()) == (200, '<h1>Hello q -!</h1>')
