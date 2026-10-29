# Ported from fastapi-chameleon (https://github.com/mikeckennedy/fastapi-chameleon at 4d070f2:
# fastapi_chameleon/engine.py and exceptions.py), modified: built into chameleon-rs on the shared
# views core (`chameleon_rs._views`), sharing `chameleon_rs.configure()`, the `chameleon_rs.web`
# exceptions and model conversion, rendering async views with `render_async`, with `error()`,
# `status_code=` and the decorator-order check; built on chameleon_rs.starlette's pieces.
"""FastAPI integration: render chameleon-rs templates from views.

Importing this module imports FastAPI (it is not imported by ``import chameleon_rs``). The API
is fastapi-chameleon's, so moving over is an import change (or none:
``chameleon_rs.compat.install()`` makes ``import fastapi_chameleon`` give this module)::

    import chameleon_rs.fastapi as chameleon

    app = fastapi.FastAPI()
    chameleon.configure(template_folder='templates', auto_reload=dev_mode, app=app)

    @app.get('/')
    @chameleon.template('home/index.pt')
    async def index():
        return {'message': 'Hello'}

The route decorator comes first (outermost) and ``@template`` under it: written the other way
round, the route registers the bare view and the template never renders. An app given to
``configure(app=...)`` (or ``global_init(..., app=...)``) checks this when it starts and raises
``chameleon_rs.web.DecoratorOrderError`` naming the view.

- ``configure(template_folder=..., auto_reload=..., restricted_namespace=..., app=None)``: the
  process-wide configuration (the same as ``chameleon_rs.configure``, so it also tells
  ``render_partial`` where templates live); ``app`` adds the startup check (settings stay
  process-wide: a FastAPI view has no app context to find per-app settings in). ``global_init``
  is fastapi-chameleon's call: it configures the views only, as it always did, and its
  ``cache_init=True`` ignores a second call.
- ``@template``, ``@template()``, ``@template('home/index.pt', mimetype=..., status_code=...)``
  (``content_type=`` is accepted too): renders what the view returns (a ``dict``, any
  ``Mapping``, a dataclass or a Pydantic-like model) and builds a ``fastapi.Response``; a
  response the view returns (any Starlette response) passes through. Bare, it renders
  ``{last module name}/{function name}.html``, or ``.pt`` when there is no ``.html`` file in the
  template folder (checked once, when the view is decorated). The view keeps its signature, so
  path and query parameters, ``Depends``, validation and the OpenAPI schema work as without
  ``@template``. An ``async def`` view renders with ``render_async`` on the event loop (values
  the view returns that are awaitable are awaited where the template uses them, without
  blocking the loop); a sync view renders with ``render`` and FastAPI runs it, render included,
  in its thread pool. ``@template(..., concurrent=True)`` fans native partials with awaitable
  arguments out; ``@template(..., offload=True)`` (experimental) renders in a worker thread.
- ``render(template_file, **data)`` and ``response(template_file, mimetype=..., status_code=...,
  **data)``; ``render_async`` and ``response_async`` are their coroutines.
- ``not_found(template_file='errors/404.pt', **data)`` and ``error(template_file,
  status_code=500, **data)`` end a decorated view with that page; ``generic_error`` is
  fastapi-chameleon's name and signature for ``error``.
- ``clear()`` forgets the process-wide configuration; ``check_views(app)`` runs the
  decorator-order check.

Errors: ``chameleon_rs.web.TemplateError`` and ``NotFound``; fastapi-chameleon's
``FastAPIChameleonException``, ``FastAPIChameleonNotFoundException`` and
``FastAPIChameleonGenericException`` are subclasses.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Coroutine, Iterable, Mapping
from typing import Any, NoReturn, Protocol, overload

import fastapi

from chameleon_rs import _settings
from chameleon_rs.starlette import StarletteIntegration, _FirstRequestCheck, _walk  # noqa: F401 - _FirstRequestCheck, _walk: names kept
from chameleon_rs.web import ErrorPage, NotFound, TemplateError

__all__ = [
    'FastAPIChameleonException',
    'FastAPIChameleonGenericException',
    'FastAPIChameleonNotFoundException',
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

# fastapi-chameleon's module state: the configured folder (or 'templates', which a bare
# `@template` falls back to when nothing was configured before the view was decorated).
template_path: str | None = None


class FastAPIChameleonException(TemplateError):
    """Base class for every error raised by fastapi-chameleon."""


class FastAPIChameleonNotFoundException(NotFound, FastAPIChameleonException):
    """Raised by ``not_found()``: the ``@template`` decorator renders ``template_file`` (with
    ``template_data``) and status 404."""

    def __init__(
        self,
        message: str | None = None,
        four04template_file: str = 'errors/404.pt',
        template_data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, four04template_file, template_data)


class FastAPIChameleonGenericException(ErrorPage, FastAPIChameleonException):
    """Raised by ``generic_error()`` / ``error()``: the ``@template`` decorator renders
    ``template_file`` (with ``template_data``) and ``status_code``."""


class _FastAPI(StarletteIntegration):
    # Starlette's part (responses, routes, the startup hooks), with fastapi-chameleon's names
    # and response class.
    name = 'fastapi'
    module = __name__
    error = FastAPIChameleonException
    response_name = 'fastapi.Response'
    route_decorators = '@app.get, @router.get, @app.api_route, ...'
    app_kind = 'FastAPI or Starlette'
    router_hint = 'an APIRouter is checked through the app that includes it'

    def make_response(self, html: str, content_type: str, status_code: int) -> fastapi.Response:
        return fastapi.Response(content=html, media_type=content_type, status_code=status_code)

    def view_context(self, args: tuple[Any, ...]) -> None:
        # fastapi-chameleon's model is the view's return value only.
        return None


_fastapi = _FastAPI()


def configure(
    template_folder: Any = None,
    *,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
    app: Any = None,
    check_order: bool = True,
) -> None:
    """Configures where templates live and how they load, for the whole process (the same as
    ``chameleon_rs.configure``: for views and for ``render_partial``). Settings left out keep
    their current values.

    ``app``: the FastAPI (or Starlette) app checks its views when it starts (in its lifespan,
    or before its first request when no lifespan runs): a view its routes registered before
    ``@template`` wrapped it (``@template`` written above ``@app.get``) raises
    ``chameleon_rs.web.DecoratorOrderError``. Sub-routers and mounted apps are included. With
    only ``app`` given, the settings are left as they are. ``check_order=False`` turns the check
    off, for an app whose routes call a function raw that is also used through a ``@template``
    wrapper not routed on this app (call ``check_views(app, ignore=[...])`` in a test instead).

    Raises ``FastAPIChameleonException`` if the folder is missing or not a directory, and
    ``TypeError`` if ``app`` is not a FastAPI or Starlette application (an ``APIRouter`` is
    checked through the app that includes it).
    """
    if app is not None:
        _fastapi.check_app(app)
        if template_folder is None and auto_reload is None and restricted_namespace is None:
            if check_order:
                _fastapi.watch_app(app)
            return
    settings = _settings.build(
        template_folder,
        auto_reload,
        restricted_namespace,
        base=_settings.views['fastapi'] or _settings.partials,
        error=FastAPIChameleonException,
    )
    _settings.install(partials_settings=settings, views_settings={'fastapi': settings})
    if app is not None and check_order:
        _fastapi.watch_app(app)


def global_init(
    template_folder: str,
    auto_reload: bool = False,
    cache_init: bool = True,
    restricted_namespace: bool = True,
    *,
    app: Any = None,
    check_order: bool = True,
) -> None:
    """fastapi-chameleon's setup call: configures the views' templates only (partials keep
    their own folder, from ``register_extensions``). With ``cache_init`` (the default) a call
    after the views' folder is configured does nothing. ``app``: check that app's views when it
    starts, as ``configure(app=...)`` does (``check_order=False`` turns the check off).

    Raises ``FastAPIChameleonException`` if the folder is missing or not a directory."""
    if app is not None:
        _fastapi.check_app(app)
    _fastapi.global_init(template_folder, auto_reload, cache_init, restricted_namespace)
    if app is not None and check_order:
        _fastapi.watch_app(app)


def clear() -> None:
    """Forgets the views' process-wide configuration, so a later ``configure()`` /
    ``global_init()`` starts afresh. Mostly useful in tests."""
    _fastapi.clear()


def check_views(app: Any, ignore: Iterable[Any] = ()) -> None:
    """Raises ``chameleon_rs.web.DecoratorOrderError`` if a route of ``app`` (sub-routers and
    mounted apps included) calls a view that ``@template`` wrapped after the route registered
    it. Runs by itself when the app starts, for an app given to ``configure(app=...)``.

    It cannot tell the mistake from a function routed raw on purpose whose ``@template``
    wrapper is used elsewhere (called from another view, or routed on another app): list such
    functions in ``ignore``."""
    _fastapi.check_app(app)
    _fastapi.check_views(app, ignore)


def _sync() -> None:
    # Called by `_settings.install`: fastapi-chameleon's `template_path` module state.
    global template_path
    template_path = _fastapi.template_path()


_settings.listeners[__name__] = _sync
_sync()  # configured before this module was imported: the legacy global already says so


def render(template_file: str, **template_data: Any) -> str:
    """Renders a template (path relative to the template folder) to a ``str``.

    Raises ``FastAPIChameleonException`` if nothing is configured.
    """
    return _fastapi.render(template_file, template_data)


async def render_async(template_file: str, **template_data: Any) -> str:
    """``render`` as a coroutine: the template's ``render_async``, which awaits awaitable
    values where the template uses them (see ``PageTemplate.render_async``)."""
    return await _fastapi.render_async(template_file, template_data)


def response(
    template_file: str, mimetype: str = 'text/html', status_code: int = 200, **template_data: Any
) -> fastapi.Response:
    """Renders a template to a ``fastapi.Response``. (As in fastapi-chameleon, every keyword
    but ``mimetype`` and ``status_code`` is the model.)"""
    html = render(template_file, **template_data)
    return fastapi.Response(content=html, media_type=mimetype, status_code=status_code)


async def response_async(
    template_file: str, mimetype: str = 'text/html', status_code: int = 200, **template_data: Any
) -> fastapi.Response:
    """``response`` as a coroutine, rendering with ``render_async``."""
    html = await render_async(template_file, **template_data)
    return fastapi.Response(content=html, media_type=mimetype, status_code=status_code)


class TemplateDecorator(Protocol):
    """What ``@template(...)`` returns: decorates a view, sync or async, into one with the same
    parameters that returns a ``fastapi.Response``."""

    @overload
    def __call__[**P](
        self, f: Callable[P, Coroutine[Any, Any, Any]], /
    ) -> Callable[P, Coroutine[Any, Any, fastapi.Response]]: ...
    @overload
    def __call__[**P](self, f: Callable[P, Any], /) -> Callable[P, fastapi.Response]: ...


@overload
def template[**P](
    template_file: Callable[P, Coroutine[Any, Any, Any]], /
) -> Callable[P, Coroutine[Any, Any, fastapi.Response]]: ...
@overload
def template[**P](template_file: Callable[P, Any], /) -> Callable[P, fastapi.Response]: ...
@overload
def template(
    template_file: str | None = None,
    mimetype: str = 'text/html',
    status_code: int = 200,
    *,
    content_type: str | None = None,
    concurrent: bool | None = None,
    offload: bool = False,
) -> TemplateDecorator: ...
def template(
    template_file: Callable[..., Any] | str | None = None,
    mimetype: str = 'text/html',
    status_code: int = 200,
    *,
    content_type: str | None = None,
    concurrent: bool | None = None,
    offload: bool = False,
) -> Callable[..., Any]:
    """Decorates a FastAPI view (sync or async) to render a template with what it returns:
    bare (``@template``), ``@template()`` or ``@template('home/index.pt', mimetype=...,
    status_code=...)``. Place it below the route decorator. See the module docs.

    ``concurrent=True`` (async views): native partials called with an awaitable argument
    render concurrently (``PageTemplate``'s ``concurrent`` setting, for this view; ``False``
    turns a template's setting off, ``None``, the default, keeps it).
    ``offload=True`` (async views, experimental): the page renders in a worker thread
    (``render_async(offload=True)``). Both apply to ``async def`` views only: a sync view
    renders with ``render`` and ignores them.

    Typing: the decorated view keeps its parameters and is typed as returning
    ``fastapi.Response`` (a coroutine of one for an ``async def`` view); a response the view
    returns itself passes through unchanged."""
    wrapped_function = None
    if callable(template_file):
        wrapped_function = template_file
        template_file = None
    chosen_type = content_type or mimetype

    def response_inner(f: Callable[..., Any]) -> Callable[..., Any]:
        return _fastapi.decorate(f, template_file, chosen_type, status_code, concurrent, offload)

    return response_inner(wrapped_function) if wrapped_function else response_inner


def not_found(
    template_file: str | None = None, *, four04template_file: str | None = None, **template_data: Any
) -> NoReturn:
    """Ends the current ``@template`` view with a 404 page: ``template_file``
    (``errors/404.pt`` by default; ``four04template_file=`` is fastapi-chameleon's name for it)
    rendered with ``template_data``, as ``text/html``.

    Raises ``FastAPIChameleonNotFoundException`` (a ``chameleon_rs.web.NotFound``), which the
    decorator turns into the response.
    """
    msg = 'The URL resulted in a 404 response.'
    chosen = template_file if template_file is not None else four04template_file
    data = template_data or None
    if chosen and chosen.strip():
        raise FastAPIChameleonNotFoundException(msg, chosen, data)
    raise FastAPIChameleonNotFoundException(msg, template_data=data)


def error(template_file: str, status_code: int = 500, **template_data: Any) -> NoReturn:
    """Ends the current ``@template`` view with an error page: ``template_file`` rendered with
    ``template_data`` and ``status_code``, as ``text/html``."""
    msg = 'The URL resulted in an error.'
    raise FastAPIChameleonGenericException(template_file, status_code, msg, template_data or None)


def generic_error(template_file: str, status_code: int, template_data: Mapping[str, Any] | None = None) -> NoReturn:
    """fastapi-chameleon's name and signature for ``error()``."""
    msg = 'The URL resulted in an error.'
    raise FastAPIChameleonGenericException(template_file, status_code, msg, template_data=template_data)


# fastapi-chameleon's submodules (`fastapi_chameleon.engine`, `fastapi_chameleon.exceptions`)
# are this module (`chameleon_rs.compat.install()` maps their names here).
engine = exceptions = sys.modules[__name__]

__author__ = 'Michael Kennedy <michael@talkpython.fm>'


def __getattr__(name: str) -> Any:
    # fastapi-chameleon's `__version__`: chameleon-rs's (read on first use, as the package's).
    if name == '__version__':
        import chameleon_rs

        return chameleon_rs.__version__
    msg = f'module {__name__!r} has no attribute {name!r}'
    raise AttributeError(msg)
