# Ported from chameleon-flask (https://github.com/mikeckennedy/chameleon-flask at 326644f:
# chameleon_flask/engine.py and exceptions.py), modified: built into chameleon-rs, sharing
# `chameleon_rs.configure()`, the `chameleon_rs.web` exceptions and model conversion, with
# per-app configuration and `error()`.
"""Flask and Quart integration: render chameleon-rs templates from views.

Importing this module imports Flask (it is not imported by ``import chameleon_rs``). The API is
chameleon-flask's, so moving over is an import change (or none: ``chameleon_rs.compat.install()``
makes ``import chameleon_flask`` give this module)::

    import chameleon_rs.flask as chameleon

    chameleon.configure(template_folder='templates', auto_reload=dev_mode)

    @app.get('/')
    @chameleon.template('home/index.pt')
    def index():
        return {'message': 'Hello'}

- ``configure(template_folder=..., auto_reload=..., restricted_namespace=..., app=None)``:
  the process-wide configuration (the same as ``chameleon_rs.configure``, so it also tells
  ``render_partial`` where templates live), or with ``app=`` one for that Flask app, kept in
  ``app.extensions['chameleon_rs']`` and used while it handles a request. ``global_init``
  is chameleon-flask's call: it configures the views only, as it always did (partials keep
  the folder ``register_extensions`` gave them), and its ``cache_init=True`` ignores a second
  call.
- ``@template``, ``@template()``, ``@template('home/index.pt', content_type=...,
  status_code=...)`` (``mimetype=`` is accepted too): renders what the view returns (a
  ``dict``, any ``Mapping``, a dataclass or a Pydantic-like model) and builds a
  ``flask.Response``; a response the view returns passes through. Bare, it renders
  ``{last module name}/{function name}.html``, or ``.pt`` when there is no ``.html`` file in
  the template folder (checked once, when the view is decorated). Sync and async views (Flask
  async views and Quart): an async view's page renders with ``render_async``, so values the
  view returns that are awaitable (coroutines, tasks) are awaited where the template uses
  them, without blocking the event loop. For every other value the output is the same as
  ``render``'s. ``@template(..., concurrent=True)`` fans native partials with awaitable
  arguments out; ``@template(..., offload=True)`` (experimental) renders in a worker thread.
- ``render(template_file, **data)`` and ``response(template_file, content_type=...,
  status_code=..., **data)``; ``render_async`` and ``response_async`` are their coroutines.
- ``not_found(template_file='errors/404.pt', **data)`` and ``error(template_file,
  status_code=500, **data)`` end a decorated view with that page; ``generic_error`` is
  fastapi-chameleon's name for ``error``.
- ``clear()`` forgets the process-wide configuration.

Errors: ``chameleon_rs.web.TemplateError`` and ``NotFound``; chameleon-flask's
``FlaskChameleonException`` and ``FlaskChameleonNotFoundException`` are subclasses.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping
from typing import Any, NoReturn, Protocol, overload

import flask
import werkzeug.sansio.response

from chameleon_rs import _settings, _views
from chameleon_rs.web import ErrorPage, NotFound, TemplateError

__all__ = [
    'FlaskChameleonException',
    'FlaskChameleonGenericException',
    'FlaskChameleonNotFoundException',
    'check_views',
    'clear',
    'configure',
    'error',
    'generic_error',
    'global_init',
    'not_found',
    'render',
    'render_async',
    'response',
    'response_async',
    'template',
]

EXTENSION = 'chameleon_rs'

# chameleon-flask's module state: the configured folder (or 'templates', which a bare
# `@template` falls back to when nothing was configured before the view was decorated).
template_path: str | None = None

# Fallback name matching for Response implementations that predate the shared werkzeug base class.
response_classes = {
    "<class 'flask.wrappers.Response'>",
    "<class 'quart.wrappers.response.Response'>",
    "<class 'flask.Response'>",
    "<class 'quart.response.Response'>",
    "<class 'quart.Response'>",
}


class FlaskChameleonException(TemplateError):
    """Base exception for all chameleon-flask errors."""


class FlaskChameleonNotFoundException(NotFound, FlaskChameleonException):
    """Raised by ``not_found()``: the ``@template`` decorator renders ``template_file`` (with
    ``template_data``) and status 404."""

    def __init__(
        self,
        message: str | None = None,
        four04template_file: str = 'errors/404.pt',
        template_data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, four04template_file, template_data)


class FlaskChameleonGenericException(ErrorPage, FlaskChameleonException):
    """Raised by ``error()``: the ``@template`` decorator renders ``template_file`` (with
    ``template_data``) and ``status_code``."""


class _Flask(_views.Integration):
    name = 'flask'
    module = __name__
    error = FlaskChameleonException
    response_name = 'flask.Response'
    route_decorators = '@app.route, @app.get, @blueprint.route, ...'

    def settings(self) -> _settings.Settings | None:
        """The configuration for this request: its app's (Flask's or Quart's ``current_app``),
        else the process-wide one."""
        if flask.has_app_context():
            settings = flask.current_app.extensions.get(EXTENSION)
            if settings is not None:
                return settings  # type: ignore[no-any-return]
        quart = sys.modules.get('quart')
        if quart is not None and getattr(quart, 'has_app_context', lambda: False)():
            settings = quart.current_app.extensions.get(EXTENSION)
            if settings is not None:
                return settings  # type: ignore[no-any-return]
        return _settings.views['flask']

    def is_response(self, value: Any) -> bool:
        # May be a Quart or bare werkzeug response; those types can't be named here without
        # importing Quart.
        return isinstance(value, werkzeug.sansio.response.Response) or str(type(value)) in response_classes

    def make_response(self, html: str, content_type: str, status_code: int) -> flask.Response:
        return _response(html, content_type, status_code)

    def routes(self, app: Any) -> Iterator[tuple[Any, str]]:
        rules: dict[str, list[str]] = {}
        for rule in app.url_map.iter_rules():
            methods = ','.join(sorted(set(rule.methods or ()) - {'HEAD', 'OPTIONS'}))
            rules.setdefault(rule.endpoint, []).append(f'{methods} {rule.rule}'.strip())
        for endpoint, view in list(app.view_functions.items()):
            yield view, ', '.join(rules.get(endpoint, [])) or endpoint

    def check_app(self, app: Any) -> None:
        if not (hasattr(app, 'view_functions') and hasattr(app, 'url_map') and hasattr(app, 'extensions')):
            msg = f'app= takes a Flask or Quart application, not {type(app).__name__} (a blueprint is checked through the app it is registered on)'
            raise TypeError(msg)

    def watch(self, app: Any, check: Callable[[], None]) -> None:
        # Quart: when the app starts serving (`before_serving`), and before its first request
        # (the test client does not serve). Flask has no start event: before its first request,
        # through a one-shot `wsgi_app` wrapper that puts the original back once the check passed.
        if hasattr(app, 'before_serving') and hasattr(app, 'asgi_app'):

            async def serving() -> None:
                check()

            app.before_serving(serving)
            original_asgi = app.asgi_app

            async def guard_asgi(scope: Any, receive: Any, send: Any) -> Any:
                if scope.get('type') != 'lifespan':
                    check()
                    if app.__dict__.get('asgi_app') is guard_asgi:
                        app.asgi_app = original_asgi
                return await original_asgi(scope, receive, send)

            app.asgi_app = guard_asgi
            return
        original = app.wsgi_app

        def guard(environ: Any, start_response: Any) -> Any:
            check()
            if app.__dict__.get('wsgi_app') is guard:
                app.wsgi_app = original
            return original(environ, start_response)

        app.wsgi_app = guard


_flask = _Flask()


def configure(
    template_folder: Any = None,
    *,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
    app: Any = None,
    check_order: bool = True,
) -> None:
    """Configures where templates live and how they load: for the whole process (the same as
    ``chameleon_rs.configure``: for views and for ``render_partial``), or, with ``app``, for that
    Flask app's views only (stored in ``app.extensions['chameleon_rs']``). Settings left out keep
    their current values.

    With ``app``, the app also checks its views (Flask has no startup hook: before its first
    request; Quart: when it starts serving, and before its first request): a view registered by
    its route before ``@template`` wrapped it (``@template`` written above ``@app.route``) raises
    ``chameleon_rs.web.DecoratorOrderError``. ``check_order=False`` turns that off, for an app
    whose routes call a function raw that is also used through a ``@template`` wrapper not
    routed on this app (call ``check_views(app, ignore=[...])`` in a test instead).
    Raises ``TypeError`` if ``app`` is not a Flask or Quart application.

    Raises ``FlaskChameleonException`` if the folder is missing or not a directory.
    """
    if app is not None:
        _flask.check_app(app)
        base = app.extensions.get(EXTENSION) or _settings.views['flask']
        app.extensions[EXTENSION] = _settings.build(
            template_folder, auto_reload, restricted_namespace, base=base, error=FlaskChameleonException
        )
        if check_order:
            _flask.watch_app(app)
        return
    settings = _settings.build(
        template_folder,
        auto_reload,
        restricted_namespace,
        base=_settings.views['flask'] or _settings.partials,
        error=FlaskChameleonException,
    )
    _settings.install(partials_settings=settings, views_settings={'flask': settings})


def global_init(
    template_folder: str,
    auto_reload: bool = False,
    cache_init: bool = True,
    restricted_namespace: bool = True,
    *,
    app: Any = None,
    check_order: bool = True,
) -> None:
    """chameleon-flask's setup call: configures the views' templates only (partials keep their
    own folder, from ``register_extensions``). With ``cache_init`` (the default) a call after
    the views' folder is configured does nothing. ``app``: check that app's views before its
    first request, as ``configure(app=...)`` does (the settings stay process-wide;
    ``check_order=False`` turns the check off)."""
    if app is not None:
        _flask.check_app(app)
    _flask.global_init(template_folder, auto_reload, cache_init, restricted_namespace)
    if app is not None and check_order:
        _flask.watch_app(app)


def clear() -> None:
    """Forgets the views' process-wide configuration, so a later ``configure()`` /
    ``global_init()`` starts afresh. Mostly useful in tests."""
    _flask.clear()


def check_views(app: Any, ignore: Iterable[Any] = ()) -> None:
    """Raises ``chameleon_rs.web.DecoratorOrderError`` if a route of ``app`` (blueprints
    included) calls a view that ``@template`` wrapped after the route registered it. Runs by
    itself before the app's first request, for an app given to ``configure(app=...)``.

    It cannot tell the mistake from a function routed raw on purpose whose ``@template``
    wrapper is used elsewhere (called from another view, or routed on another app): list such
    functions in ``ignore``."""
    _flask.check_app(app)
    _flask.check_views(app, ignore)


def _sync() -> None:
    # Called by `_settings.install`: chameleon-flask's `template_path` module state.
    global template_path
    template_path = _flask.template_path()


_settings.listeners[__name__] = _sync
_sync()  # configured before this module was imported: the legacy global already says so


def _current_settings() -> _settings.Settings | None:
    """The configuration for this request: its app's, else the process-wide one."""
    return _flask.settings()


def _page(template_file: str) -> Any:
    return _flask.page(template_file)


def render(template_file: str, **template_data: Any) -> str:
    """Renders a template (path relative to the template folder) to a ``str``.

    Raises ``FlaskChameleonException`` if nothing is configured.
    """
    return _flask.render(template_file, template_data)


async def render_async(template_file: str, **template_data: Any) -> str:
    """``render`` as a coroutine: the template's ``render_async``, which awaits awaitable
    values where the template uses them (see ``PageTemplate.render_async``)."""
    return await _flask.render_async(template_file, template_data)


def response(
    template_file: str,
    content_type: str = 'text/html',
    status_code: int = 200,
    **template_data: Any,
) -> flask.Response:
    """Renders a template to a ``flask.Response``. (Unlike ``@template``, ``mimetype`` is not a
    keyword here: as in chameleon-flask, every other keyword is the model.)"""
    html = render(template_file, **template_data)
    return _response(html, content_type, status_code)


async def response_async(
    template_file: str,
    content_type: str = 'text/html',
    status_code: int = 200,
    **template_data: Any,
) -> flask.Response:
    """``response`` as a coroutine, rendering with ``render_async``."""
    html = await render_async(template_file, **template_data)
    return _response(html, content_type, status_code)


def _response(html: str, content_type: str, status_code: int) -> flask.Response:
    # Flask encodes the `str` as UTF-8: a text type says so, as Flask's own `render_template`
    # responses do (`text/html; charset=utf-8`), so browsers never have to guess the encoding.
    # chameleon-flask sent `text/html` alone. A type that names a charset is kept as given.
    if content_type.startswith('text/') and 'charset' not in content_type.lower():
        content_type = f'{content_type}; charset=utf-8'
    return flask.Response(response=html, content_type=content_type, status=status_code)


class TemplateDecorator(Protocol):
    """What ``@template(...)`` returns: decorates a view, sync or async, into one that returns
    a ``flask.Response``."""

    @overload
    def __call__[**P](
        self, f: Callable[P, Coroutine[Any, Any, Any]], /
    ) -> Callable[P, Coroutine[Any, Any, flask.Response]]: ...
    @overload
    def __call__[**P](self, f: Callable[P, Any], /) -> Callable[P, flask.Response]: ...


@overload
def template[**P](
    template_file: Callable[P, Coroutine[Any, Any, Any]], /
) -> Callable[P, Coroutine[Any, Any, flask.Response]]: ...
@overload
def template[**P](template_file: Callable[P, Any], /) -> Callable[P, flask.Response]: ...
@overload
def template(
    template_file: str | None = None,
    content_type: str = 'text/html',
    status_code: int = 200,
    *,
    mimetype: str | None = None,
    concurrent: bool | None = None,
    offload: bool = False,
) -> TemplateDecorator: ...
def template(
    template_file: Callable[..., Any] | str | None = None,
    content_type: str = 'text/html',
    status_code: int = 200,
    *,
    mimetype: str | None = None,
    concurrent: bool | None = None,
    offload: bool = False,
) -> Callable[..., Any]:
    """Decorates a Flask or Quart view (sync or async) to render a template with what it
    returns: bare (``@template``), ``@template()`` or ``@template('home/index.pt',
    content_type=..., status_code=...)``. See the module docs.

    ``concurrent=True`` (async views): native partials called with an awaitable argument
    render concurrently (``PageTemplate``'s ``concurrent`` setting, for this view; ``False``
    turns a template's setting off, ``None``, the default, keeps it).
    ``offload=True`` (async views, experimental): the page renders in a worker thread
    (``render_async(offload=True)``). Both apply to ``async def`` views only: a sync view
    renders with ``render`` and ignores them.

    Typing: the decorated view is typed as returning ``flask.Response`` (a coroutine of one for
    an ``async def`` view). A response the view returns itself passes through unchanged, so with
    Quart (or a bare werkzeug response) the object is that framework's response, not a
    ``flask.Response``. A view counts as async when it is an ``async def`` function
    (``inspect.iscoroutinefunction``); type checkers go by its return type, so a plain
    function returning a coroutine is typed as async but runs as a sync view."""
    wrapped_function = None
    if callable(template_file):
        wrapped_function = template_file
        template_file = None
    content_type = mimetype or content_type

    def response_inner(f: Callable[..., Any]) -> Callable[..., Any]:
        return _flask.decorate(f, template_file, content_type, status_code, concurrent, offload)

    return response_inner(wrapped_function) if wrapped_function else response_inner


def not_found(
    template_file: str | None = None, *, four04template_file: str | None = None, **template_data: Any
) -> NoReturn:
    """Ends the current ``@template`` view with a 404 page: ``template_file``
    (``errors/404.pt`` by default; ``four04template_file=`` is chameleon-flask's name for it)
    rendered with ``template_data``, as ``text/html``.

    Raises ``FlaskChameleonNotFoundException`` (a ``chameleon_rs.web.NotFound``), which the
    decorator turns into the response.
    """
    msg = 'The URL resulted in a 404 response.'
    chosen = template_file if template_file is not None else four04template_file
    data = template_data or None
    if chosen and chosen.strip():
        raise FlaskChameleonNotFoundException(msg, chosen, data)
    raise FlaskChameleonNotFoundException(msg, template_data=data)


def error(template_file: str, status_code: int = 500, **template_data: Any) -> NoReturn:
    """Ends the current ``@template`` view with an error page: ``template_file`` rendered with
    ``template_data`` and ``status_code``, as ``text/html``."""
    msg = 'The URL resulted in an error.'
    raise FlaskChameleonGenericException(template_file, status_code, msg, template_data or None)


def generic_error(template_file: str, status_code: int, template_data: Mapping[str, Any] | None = None) -> NoReturn:
    """fastapi-chameleon's name and signature for ``error()``."""
    msg = 'The URL resulted in an error.'
    raise FlaskChameleonGenericException(template_file, status_code, msg, template_data)


# chameleon-flask's submodules (`chameleon_flask.engine`, `chameleon_flask.exceptions`) are
# this module (`chameleon_rs.compat.install()` maps their names here).
engine = exceptions = sys.modules[__name__]

__author__ = 'Michael Kennedy <michael@talkpython.fm>'


def __getattr__(name: str) -> Any:
    # chameleon-flask's `__version__`: chameleon-rs's (read on first use, as the package's).
    if name == '__version__':
        import chameleon_rs

        return chameleon_rs.__version__
    msg = f'module {__name__!r} has no attribute {name!r}'
    raise AttributeError(msg)
