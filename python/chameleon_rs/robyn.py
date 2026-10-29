# Ported from chameleon-robyn (https://github.com/mikeckennedy/chameleon-robyn at ab4f6e8:
# chameleon_robyn/engine.py and exceptions.py), modified: built into chameleon-rs on the shared
# views core (`chameleon_rs._views`), sharing `chameleon_rs.configure()`, the `chameleon_rs.web`
# exceptions and model conversion, rendering async handlers with `render_async`, with `error()`,
# data for error pages and the decorator-order check.
"""Robyn integration: render chameleon-rs templates from route handlers.

Importing this module imports Robyn (it is not imported by ``import chameleon_rs``). The API is
chameleon-robyn's, so moving over is an import change (or none: ``chameleon_rs.compat.install()``
makes ``import chameleon_robyn`` give this module)::

    import chameleon_rs.robyn as chameleon

    app = Robyn(__file__)
    chameleon.configure(template_folder='templates', auto_reload=dev_mode, app=app)

    @app.get('/')
    @chameleon.template('home/index.pt')
    async def index(request):
        return {'message': 'Hello'}

The route decorator comes first (outermost) and ``@template`` under it: written the other way
round, the route registers the bare handler and the template never renders. An app given to
``configure(app=...)`` (or ``global_init(..., app=...)``) checks this when it starts
(``app.start()``) and when a ``robyn.testing.TestClient`` is built for it, and raises
``chameleon_rs.web.DecoratorOrderError`` naming the handler.

- ``configure(template_folder=..., auto_reload=..., restricted_namespace=..., app=None)``: the
  process-wide configuration (the same as ``chameleon_rs.configure``, so it also tells
  ``render_partial`` where templates live); ``app`` adds the startup check (settings stay
  process-wide: a Robyn handler has no app to find per-app settings in). ``global_init`` is
  chameleon-robyn's call: it configures the handlers only, as it always did, and its
  ``cache_init=True`` ignores a second call.
- ``@template``, ``@template()``, ``@template('home/index.pt', content_type=...,
  status_code=...)`` (``mimetype=`` is accepted too): renders what the handler returns (a
  ``dict``, any ``Mapping``, a dataclass or a Pydantic-like model) into a ``robyn.Response``
  with ``Content-Type: {content_type}; charset=utf-8``; a ``robyn.Response`` the handler
  returns passes through. A ``__response_callback__`` in the model is not a template variable:
  it is called with the response before it is returned (to set cookies, headers). Bare, it
  renders ``{last module name}/{function name}.html``, or ``.pt`` when there is no ``.html``
  file in the template folder, decided at the first request once the folder is configured (so
  handlers may be decorated before ``global_init()``). The handler keeps its signature, so
  Robyn passes it the same request parts. An ``async def`` handler renders with
  ``render_async`` on Robyn's event loop (values the handler returns that are awaitable are
  awaited where the template uses them); a sync handler renders with ``render`` where Robyn
  runs it, on its worker thread. ``@template(..., concurrent=True)`` fans native partials with
  awaitable arguments out; ``@template(..., offload=True)`` (experimental) renders in a worker
  thread.
- ``render(template_file, **data)`` and ``response(template_file, content_type=...,
  status_code=..., **data)``; ``render_async`` and ``response_async`` are their coroutines.
- ``not_found(template_file='errors/404.pt', **data)`` and ``error(template_file,
  status_code=500, **data)`` end a decorated handler with that page, rendered with ``message``
  and the data; ``generic_error`` is fastapi-chameleon's name and signature for ``error``.
- ``ChameleonTemplate(directory, auto_reload=False, encoding='utf-8',
  restricted_namespace=True)``: Robyn's template-engine interface (``render_template(name,
  **data)``, as Robyn's ``JinjaTemplate``), with its own loader; ``render_template_async`` is
  its coroutine.
- ``clear()`` forgets the process-wide configuration; ``check_views(app)`` runs the
  decorator-order check.

Errors: ``chameleon_rs.web.TemplateError`` and ``NotFound``; chameleon-robyn's
``ChameleonRobynException`` and ``ChameleonRobynNotFoundException`` are subclasses.
"""

from __future__ import annotations

import inspect
import os
import sys
import weakref
from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping
from functools import wraps
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, overload, runtime_checkable

from robyn import Headers, Response, status_codes

from chameleon_rs import _settings, _views
from chameleon_rs.template import _render_async
from chameleon_rs.web import ErrorPage, NotFound, TemplateError, TemplateNotFound
from chameleon_rs.zpt.loader import TemplateLoader

__all__ = [
    'ChameleonRobynException',
    'ChameleonRobynGenericException',
    'ChameleonRobynNotFoundException',
    'ChameleonTemplate',
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

# chameleon-robyn's module state: the configured folder (`None` until configured; a bare
# `@template` looks in 'templates' meanwhile, and decides for good once a folder is set).
template_path: str | None = None

# The model key chameleon-robyn reserves for a function called with the response.
RESPONSE_CALLBACK = '__response_callback__'


class ChameleonRobynException(TemplateError):
    """Base exception for all chameleon-robyn errors (bad configuration, invalid return types, etc.)."""


class ChameleonRobynNotFoundException(NotFound, ChameleonRobynException):
    """Raised by ``not_found()``: the ``@template`` decorator renders ``template_file`` with a
    404 status, passing ``message`` (and ``template_data``) to the template."""

    def __init__(
        self,
        message: str | None = None,
        four04template_file: str = 'errors/404.pt',
        template_data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, four04template_file, template_data)


class ChameleonRobynGenericException(ErrorPage, ChameleonRobynException):
    """Raised by ``error()``: the ``@template`` decorator renders ``template_file`` (with
    ``message`` and ``template_data``) and ``status_code``."""


def _headers(content_type: str) -> Headers:
    return Headers({'Content-Type': f'{content_type}; charset=utf-8'})


class _Robyn(_views.Integration):
    name = 'robyn'
    module = __name__
    error = ChameleonRobynException
    response_name = 'Response'
    route_decorators = '@app.get, @app.post, @router.get, app.add_route, ...'

    def is_response(self, value: Any) -> bool:
        return isinstance(value, Response)

    def make_response(self, html: str, content_type: str, status_code: int) -> Response:
        return Response(status_code=status_code, description=html, headers=_headers(content_type))

    # --- Rendering: chameleon-robyn's `__response_callback__` and error-page model ------------

    def _model(self, value: Any) -> tuple[Mapping[str, Any], Callable[[Response], Any] | None]:
        """The model and its response callback (popped from a copy: the handler's dict may be
        shared)."""
        model = self.model(value)
        if RESPONSE_CALLBACK not in model:
            return model, None
        model = dict(model)
        callback = model.pop(RESPONSE_CALLBACK)
        if callback is not None and not callable(callback):
            msg = f'{RESPONSE_CALLBACK} must be callable, got {type(callback)}.'
            raise self.error(msg)
        return model, callback

    def render_response(
        self,
        template_file: str,
        value: Any,
        content_type: str,
        status_code: int,
        extra: Mapping[str, Any] | None = None,
    ) -> Any:
        if self.is_response(value):
            return value
        model, callback = self._model(value)
        resp = self.make_response(self.render(template_file, model), content_type, status_code)
        if callback is not None:
            callback(resp)
        return resp

    async def render_response_async(
        self,
        template_file: str,
        value: Any,
        content_type: str,
        status_code: int,
        concurrent: bool | None = None,
        offload: bool = False,
        extra: Mapping[str, Any] | None = None,
    ) -> Any:
        if self.is_response(value):
            return value
        model, callback = self._model(value)
        page = self.page(template_file)
        if concurrent is None:
            concurrent = page.concurrent
        html = await _render_async(page, 'utf-8', dict(model), bool(concurrent), offload)
        resp = self.make_response(html, content_type, status_code)
        if callback is not None:
            callback(resp)
        return resp

    def error_response(self, page: ErrorPage, extra: Mapping[str, Any] | None = None) -> Any:
        # chameleon-robyn renders its 404 page with `{'message': ...}`; data given to
        # `not_found()` / `error()` is added (and wins).
        return self.render_response(page.template_file, _error_model(page), 'text/html', page.status_code)

    async def error_response_async(self, page: ErrorPage, extra: Mapping[str, Any] | None = None) -> Any:
        return await self.render_response_async(page.template_file, _error_model(page), 'text/html', page.status_code)

    # --- The decorator: the bare name is decided at the first request ------------------------

    def decorate(
        self,
        f: Callable[..., Any],
        template_file: str | None,
        content_type: str,
        status_code: int,
        concurrent: bool | None = None,
        offload: bool = False,
    ) -> Callable[..., Any]:
        """Wraps a handler (sync or async) to render its template with what it returns."""
        resolve = _resolver(f, template_file)

        @wraps(f)
        def sync_view_method(*args: Any, **kwargs: Any) -> Any:
            try:
                value = f(*args, **kwargs)
                return self.render_response(resolve(), value, content_type, status_code)
            except ErrorPage as page:
                return self.error_response(page)

        @wraps(f)
        async def async_view_method(*args: Any, **kwargs: Any) -> Any:
            try:
                value = await f(*args, **kwargs)
                return await self.render_response_async(
                    resolve(), value, content_type, status_code, concurrent, offload
                )
            except ErrorPage as page:
                return await self.error_response_async(page)

        wrapper = async_view_method if inspect.iscoroutinefunction(f) else sync_view_method
        _views._remember(f, wrapper)
        return wrapper

    # --- The decorator-order check ----------------------------------------------------------

    def routes(self, app: Any) -> Iterator[tuple[Any, str]]:
        # The app's route table (an included SubRouter's routes are copied into it). Read
        # through the class's `get_routes`: the instance's may be the startup guard below.
        router = app.router
        for route in type(router).get_routes(router):
            method = str(route.route_type).removeprefix('HttpMethod.')
            yield route.function.handler, f'{method} {route.route}'

    def check_app(self, app: Any) -> None:
        if not (callable(getattr(app, 'start', None)) and _has_routes(app)):
            msg = f'app= takes a Robyn application, not {type(app).__name__} (a SubRouter is checked through the app that includes it)'
            raise TypeError(msg)

    def watch_app(self, app: Any) -> None:
        """Checks ``app``'s handlers when it starts (``app.start()``) and whenever its route
        table is read with routes added since the last check (``robyn.testing.TestClient(app)``
        reads it; so may the app's own code, at import time, before every route exists).

        Robyn has one startup handler per app (a second ``startup_handler`` replaces the first)
        and runs it in each server process, so the check runs in ``start()`` instead, in the
        main process before the server starts. Only ``start()`` passing is final: then both
        guards remove themselves. Until then the ``get_routes`` guard stays, and costs a length
        comparison per call once the routes it saw passed."""
        self.check_app(app)
        if app in _views._watched:
            return
        _views._watched[app] = True
        app_ref = weakref.ref(app)
        router = app.router
        # The number of routes the last check passed with (Robyn only ever appends routes).
        checked = -1

        def check() -> None:
            nonlocal checked
            current = app_ref()
            if current is None:
                return
            count = len(type(router).get_routes(router))
            if count != checked:
                self.check_views(current)
                checked = count

        unguard_routes = _guard(router, 'get_routes', check)

        def check_and_unguard() -> None:
            check()
            unguard_routes()

        _guard(app, 'start', check_and_unguard, once=True)


def _has_routes(app: Any) -> bool:
    return callable(getattr(getattr(app, 'router', None), 'get_routes', None))


def _guard(obj: Any, attribute: str, check: Callable[[], None], *, once: bool = False) -> Callable[[], None]:
    """Replaces the bound method ``obj.attribute`` with one that runs ``check`` first (with
    ``once``, it puts the original back once ``check`` passed). Returns the function that puts
    the original back."""
    had_own = attribute in vars(obj)
    original = getattr(obj, attribute)

    def unguard() -> None:
        if vars(obj).get(attribute) is guarded:
            if had_own:
                setattr(obj, attribute, original)
            else:
                vars(obj).pop(attribute, None)

    def guarded(*args: Any, **kwargs: Any) -> Any:
        check()
        if once:
            unguard()
        return original(*args, **kwargs)

    setattr(obj, attribute, guarded)
    return unguard


def _error_model(page: ErrorPage) -> dict[str, Any]:
    data = page.template_data
    return {'message': page.message, **data} if data else {'message': page.message}


def _resolver(f: Callable[..., Any], template_file: str | None) -> Callable[[], str]:
    """chameleon-robyn's template name: ``template_file``, or for a bare ``@template``
    ``{last module name}/{function name}.html``, else ``.pt``, looked for in the configured
    folder (``templates`` until one is configured) at each request until a folder is
    configured, then fixed."""
    resolved = template_file if isinstance(template_file, str) and template_file else None

    def resolve() -> str:
        nonlocal resolved
        if resolved is not None:
            return resolved
        module_name = f.__module__
        if '.' in module_name:
            module_name = module_name.split('.')[-1]
        view = f.__name__  # ty: ignore[unresolved-attribute] - handlers are functions
        candidate = f'{module_name}/{view}.html'
        folder = template_path or 'templates'
        if not os.path.exists(os.path.join(folder, candidate)):
            candidate = f'{module_name}/{view}.pt'
        if template_path:
            resolved = candidate
        return candidate

    return resolve


_robyn = _Robyn()


def configure(
    template_folder: Any = None,
    *,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
    app: Any = None,
    check_order: bool = True,
) -> None:
    """Configures where templates live and how they load, for the whole process (the same as
    ``chameleon_rs.configure``: for handlers and for ``render_partial``). Settings left out keep
    their current values.

    ``app``: the Robyn app checks its handlers when it starts (``app.start()``, before the
    server runs) and when a ``robyn.testing.TestClient`` is built for it: a handler its routes
    registered before ``@template`` wrapped it (``@template`` written above ``@app.get``)
    raises ``chameleon_rs.web.DecoratorOrderError``. Routes of included ``SubRouter``\\s are
    covered. With only ``app`` given, the settings are left as they are. ``check_order=False``
    turns the check off, for an app whose routes call a function raw that is also used through
    a ``@template`` wrapper not routed on this app (call ``check_views(app, ignore=[...])`` in a
    test instead).

    Raises ``ChameleonRobynException`` if the folder is missing or not a directory, and
    ``TypeError`` if ``app`` is not a Robyn application (a ``SubRouter`` is checked through the
    app that includes it).
    """
    if app is not None:
        _robyn.check_app(app)
        if template_folder is None and auto_reload is None and restricted_namespace is None:
            if check_order:
                _robyn.watch_app(app)
            return
    settings = _settings.build(
        template_folder,
        auto_reload,
        restricted_namespace,
        base=_settings.views['robyn'] or _settings.partials,
        error=ChameleonRobynException,
    )
    _settings.install(partials_settings=settings, views_settings={'robyn': settings})
    if app is not None and check_order:
        _robyn.watch_app(app)


def global_init(
    template_folder: str,
    auto_reload: bool = False,
    cache_init: bool = True,
    restricted_namespace: bool = True,
    *,
    app: Any = None,
    check_order: bool = True,
) -> None:
    """chameleon-robyn's setup call: configures the handlers' templates only (partials keep
    their own folder, from ``register_extensions``). With ``cache_init`` (the default) a call
    after the handlers' folder is configured does nothing. ``restricted_namespace=False``
    allows attributes such as Alpine.js's ``@click``. ``app``: check that app's handlers when it
    starts, as ``configure(app=...)`` does (``check_order=False`` turns the check off).

    Raises ``ChameleonRobynException`` if the folder is missing or not a directory."""
    if app is not None:
        _robyn.check_app(app)
    _robyn.global_init(template_folder, auto_reload, cache_init, restricted_namespace)
    if app is not None and check_order:
        _robyn.watch_app(app)


def clear() -> None:
    """Forgets the handlers' process-wide configuration, so a later ``configure()`` /
    ``global_init()`` starts afresh. Mostly useful in tests."""
    _robyn.clear()


def check_views(app: Any, ignore: Iterable[Any] = ()) -> None:
    """Raises ``chameleon_rs.web.DecoratorOrderError`` if a route of ``app`` (a Robyn app, with
    the routes of the SubRouters it includes, or a SubRouter) calls a handler that ``@template``
    wrapped after the route registered it. Runs by itself when the app starts, for an app given
    to ``configure(app=...)``.

    It cannot tell the mistake from a function routed raw on purpose whose ``@template``
    wrapper is used elsewhere (called from another handler, or routed on another app): list
    such functions in ``ignore``."""
    if not _has_routes(app):
        msg = f'check_views() takes a Robyn application or SubRouter, not {type(app).__name__}'
        raise TypeError(msg)
    _robyn.check_views(app, ignore)


def _sync() -> None:
    # Called by `_settings.install`: chameleon-robyn's `template_path` module state.
    global template_path
    template_path = _robyn.template_path()


_settings.listeners[__name__] = _sync
_sync()  # configured before this module was imported: the legacy global already says so


def render(template_file: str, **template_data: Any) -> str:
    """Renders a template (path relative to the template folder) to a ``str`` (no response):
    for emails, middleware, error handlers.

    Raises ``ChameleonRobynException`` if nothing is configured.
    """
    return _robyn.render(template_file, template_data)


async def render_async(template_file: str, **template_data: Any) -> str:
    """``render`` as a coroutine: the template's ``render_async``, which awaits awaitable
    values where the template uses them (see ``PageTemplate.render_async``)."""
    return await _robyn.render_async(template_file, template_data)


def response(
    template_file: str, content_type: str = 'text/html', status_code: int = 200, **template_data: Any
) -> Response:
    """Renders a template into a ``robyn.Response`` with ``Content-Type: {content_type};
    charset=utf-8``. (As in chameleon-robyn, every other keyword is the model.)"""
    html = render(template_file, **template_data)
    return Response(status_code=status_code, description=html, headers=_headers(content_type))


async def response_async(
    template_file: str, content_type: str = 'text/html', status_code: int = 200, **template_data: Any
) -> Response:
    """``response`` as a coroutine, rendering with ``render_async``."""
    html = await render_async(template_file, **template_data)
    return Response(status_code=status_code, description=html, headers=_headers(content_type))


class TemplateDecorator(Protocol):
    """What ``@template(...)`` returns: decorates a handler, sync or async, into one with the
    same parameters that returns a ``robyn.Response``."""

    @overload
    def __call__[**P](
        self, f: Callable[P, Coroutine[Any, Any, Any]], /
    ) -> Callable[P, Coroutine[Any, Any, Response]]: ...
    @overload
    def __call__[**P](self, f: Callable[P, Any], /) -> Callable[P, Response]: ...


@overload
def template[**P](
    template_file: Callable[P, Coroutine[Any, Any, Any]], /
) -> Callable[P, Coroutine[Any, Any, Response]]: ...
@overload
def template[**P](template_file: Callable[P, Any], /) -> Callable[P, Response]: ...
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
    """Decorates a Robyn route handler (sync or async) to render a template with what it
    returns: bare (``@template``), ``@template()`` or ``@template('home/index.pt',
    content_type=..., status_code=...)``. Place it below the route decorator. See the module
    docs.

    ``concurrent=True`` (async handlers): native partials called with an awaitable argument
    render concurrently (``PageTemplate``'s ``concurrent`` setting, for this handler; ``False``
    turns a template's setting off, ``None``, the default, keeps it).
    ``offload=True`` (async handlers, experimental): the page renders in a worker thread
    (``render_async(offload=True)``). Both apply to ``async def`` handlers only: a sync handler
    renders with ``render`` and ignores them.

    Typing: the decorated handler keeps its parameters and is typed as returning
    ``robyn.Response`` (a coroutine of one for an ``async def`` handler); a response the
    handler returns itself passes through unchanged."""
    wrapped_function = None
    if callable(template_file):
        wrapped_function = template_file
        template_file = None
    chosen_type = mimetype or content_type

    def response_inner(f: Callable[..., Any]) -> Callable[..., Any]:
        return _robyn.decorate(f, template_file, chosen_type, status_code, concurrent, offload)

    return response_inner(wrapped_function) if wrapped_function else response_inner


def not_found(
    template_file: str | None = None, *, four04template_file: str | None = None, **template_data: Any
) -> NoReturn:
    """Ends the current ``@template`` handler with a 404 page: ``template_file``
    (``errors/404.pt`` by default; ``four04template_file=`` is chameleon-robyn's name for it)
    rendered as ``text/html`` with ``message`` (``'The URL resulted in a 404 response.'``) and
    ``template_data``.

    Raises ``ChameleonRobynNotFoundException`` (a ``chameleon_rs.web.NotFound``), which the
    decorator turns into the response.
    """
    msg = 'The URL resulted in a 404 response.'
    chosen = template_file if template_file is not None else four04template_file
    data = template_data or None
    if chosen and chosen.strip():
        raise ChameleonRobynNotFoundException(msg, chosen, data)
    raise ChameleonRobynNotFoundException(msg, template_data=data)


def error(template_file: str, status_code: int = 500, **template_data: Any) -> NoReturn:
    """Ends the current ``@template`` handler with an error page: ``template_file`` rendered as
    ``text/html`` with ``message`` (``'The URL resulted in an error.'``), ``template_data`` and
    ``status_code``."""
    msg = 'The URL resulted in an error.'
    raise ChameleonRobynGenericException(template_file, status_code, msg, template_data or None)


def generic_error(template_file: str, status_code: int, template_data: Mapping[str, Any] | None = None) -> NoReturn:
    """fastapi-chameleon's name and signature for ``error()``."""
    msg = 'The URL resulted in an error.'
    raise ChameleonRobynGenericException(template_file, status_code, msg, template_data)


@runtime_checkable
class TemplateInterface(Protocol):
    """Protocol matching Robyn's ``TemplateInterface`` without importing Jinja2 (as
    ``robyn.templating`` does)."""

    def render_template(self, *args: Any, **kwargs: Any) -> Response: ...


if TYPE_CHECKING:
    # Typed as Robyn's abstract class, so it type-checks as `robyn.templating.render(...,
    # template_engine=ChameleonTemplate)`; at runtime that module would import Jinja2.
    from robyn.templating import TemplateInterface as _EngineBase
else:
    _EngineBase = TemplateInterface


class ChameleonTemplate(_EngineBase):
    """Chameleon templates through Robyn's ``TemplateInterface``, as its ``JinjaTemplate``:
    owns its own loader and needs no ``global_init()``.

    ``directory``: the template folder. ``auto_reload``: recompile a template when its file
    changes (development). ``encoding``: the output encoding. ``restricted_namespace=False``
    allows Alpine.js/htmx-style attributes (``@click``, ``:class``)."""

    def __init__(
        self,
        directory: str,
        auto_reload: bool = False,
        encoding: str = 'utf-8',
        restricted_namespace: bool = True,
    ) -> None:
        self.loader = TemplateLoader(directory, auto_reload=auto_reload, restricted_namespace=restricted_namespace)
        self.encoding = encoding

    def render_template(self, template_name: str, **kwargs: Any) -> Response:
        """Renders ``template_name`` (relative to the directory) with ``kwargs`` as its model
        into a 200 ``robyn.Response`` (``text/html; charset=utf-8``). A name that could reach
        outside the directory raises ``TemplateNotFound``, as Robyn's ``JinjaTemplate`` refuses
        one (``_views.outside_folder``)."""
        rendered = self._template(template_name).render(encoding=self.encoding, **kwargs)
        return Response(status_code=status_codes.HTTP_200_OK, description=rendered, headers=_headers('text/html'))

    def _template(self, template_name: str) -> Any:
        if _views.outside_folder(template_name):
            msg = f'Template not found: {template_name}.'
            raise TemplateNotFound(msg)
        spec = _views.known_spelling(self.loader, template_name)
        if spec is None:
            # A missing file is `TemplateNotFound` too, as Robyn's `JinjaTemplate` raises.
            msg = f'Template not found: {template_name}.'
            raise TemplateNotFound(msg)
        return self.loader[spec]

    async def render_template_async(self, template_name: str, **kwargs: Any) -> Response:
        """``render_template`` as a coroutine, rendering with ``render_async`` (awaitable
        values are awaited where the template uses them, without blocking the event loop)."""
        rendered = await self._template(template_name).render_async(encoding=self.encoding, **kwargs)
        return Response(status_code=status_codes.HTTP_200_OK, description=rendered, headers=_headers('text/html'))


# chameleon-robyn's submodules (`chameleon_robyn.engine`, `chameleon_robyn.exceptions`) are this
# module (`chameleon_rs.compat.install()` maps their names here).
engine = exceptions = sys.modules[__name__]

__author__ = 'Michael Kennedy <michael@talkpython.fm>'


def __getattr__(name: str) -> Any:
    # chameleon-robyn's `__version__`: chameleon-rs's (read on first use, as the package's).
    if name == '__version__':
        import chameleon_rs

        return chameleon_rs.__version__
    msg = f'module {__name__!r} has no attribute {name!r}'
    raise AttributeError(msg)
