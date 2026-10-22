# Parts ported from Litestar (https://github.com/litestar-org/litestar, 2.25.0:
# litestar/plugins/mako.py and litestar/response/template.py, MIT), modified: `ChameleonEngine`
# renders chameleon-rs templates instead of Mako, refuses names outside its folder as Litestar's
# Jinja engine does, and `Template` serves names Litestar cannot guess (`.pt`) as text/html. The
# module API (`@template`, `render`, `response`, ...) follows fastapi-chameleon
# (https://github.com/mikeckennedy/fastapi-chameleon at 4d070f2), on the shared views core.
"""Litestar integration: render chameleon-rs templates from route handlers.

Importing this module imports Litestar (it is not imported by ``import chameleon_rs``). Two
ways to use it, which can be mixed:

``ChameleonEngine``, Litestar's own templating (``TemplateEngineProtocol``)::

    from litestar import Litestar, get
    from litestar.template import TemplateConfig
    from chameleon_rs.litestar import ChameleonEngine, Template

    @get('/')
    async def index() -> Template:
        return Template('home/index.pt', context={'title': 'Home'})

    app = Litestar([index], template_config=TemplateConfig(engine=ChameleonEngine, directory='templates'))

The template gets Litestar's context (``request``, ``csrf_input``) and the engine's template
callables (``url_for``, ``csrf_token``, ``url_for_static_asset`` and any registered with
``register_template_callable``). Use this module's ``Template``, not ``litestar.response.Template``:
Litestar guesses the media type from the template name's suffix and serves ``.pt`` as
``text/plain``; this ``Template`` serves a name Litestar cannot guess as ``text/html`` (an explicit
``media_type``, on the response or the route, still wins). Litestar renders templates
synchronously, on the event loop: ``ChameleonEngine`` cannot change that, so an ``async def``
handler wanting ``render_async`` uses the decorator API below. A missing template, or a name that
could reach outside the folder (absolute, ``..``, ...), raises Litestar's
``TemplateNotFoundException``, as its Jinja engine does.

The decorator API, the same as ``chameleon_rs.flask``, ``.fastapi``, ``.starlette`` and ``.robyn``::

    import chameleon_rs.litestar as chameleon_litestar

    @get('/')
    @chameleon_litestar.template('home/index.pt')
    async def index() -> dict:
        return {'title': 'Home'}

    app = Litestar([index])
    chameleon_litestar.configure(template_folder='templates', app=app)

The route decorator comes first (outermost) and ``@template`` under it: written the other way
round, ``@template`` is handed Litestar's route handler and raises
``chameleon_rs.web.DecoratorOrderError`` at once; an app given to ``configure(app=...)`` also
checks its routes when it starts.

- ``configure(template_folder=..., auto_reload=..., restricted_namespace=..., app=None)``: the
  process-wide configuration (the same as ``chameleon_rs.configure``: for handlers,
  ``ChameleonEngine()`` without a directory and ``render_partial``); ``app`` adds the
  decorator-order check at startup. ``global_init`` configures the handlers only
  (``cache_init=True`` ignores a second call).
- ``@template``, ``@template()``, ``@template('home/index.pt', content_type=...,
  status_code=...)`` (``mimetype=`` is accepted too): renders what the handler returns (a
  ``dict``, any ``Mapping``, a dataclass or a Pydantic-like model) into a ``litestar.Response``
  (``text/html``, with the route's status code unless ``status_code`` is given); a response the handler returns (a ``Redirect``, a ``Template``, ...) passes
  through. The template also gets what a Litestar template gets: ``request``, ``csrf_input``,
  ``url_for``, ``csrf_token`` and ``url_for_static_asset`` (the model's own keys win). The
  handler keeps its parameters for Litestar's dependency injection, validation and OpenAPI; its
  return annotation becomes ``litestar.Response`` (so Litestar does not serialise the model as
  JSON), and a ``request`` parameter is added when it has none. An ``async def`` handler
  renders with ``render_async`` on the event loop; a sync one renders with ``render`` where
  Litestar runs it (in a worker thread with ``sync_to_thread=True``).
- ``render(template_file, **data)`` and ``response(template_file, content_type=...,
  status_code=..., **data)``; ``render_async`` and ``response_async`` are their coroutines.
- ``not_found(template_file='errors/404.pt', **data)`` and ``error(template_file,
  status_code=500, **data)`` end a decorated handler with that page.
- ``clear()`` forgets the process-wide configuration; ``check_views(app)`` runs the
  decorator-order check.

Errors: ``chameleon_rs.web.TemplateError``, ``NotFound``, ``ErrorPage`` and
``DecoratorOrderError`` (also importable from here).
"""

from __future__ import annotations

import html
import inspect
import os
import sys
import threading
from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping, Sequence
from functools import partial, wraps
from mimetypes import guess_type
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, overload

from litestar import Litestar, Request, Response
from litestar.enums import MediaType
from litestar.exceptions import ImproperlyConfiguredException, TemplateNotFoundException
from litestar.handlers import BaseRouteHandler
from litestar.response import Template as _LitestarTemplate
from litestar.routes import HTTPRoute
from litestar.template.base import (
    TemplateCallableType,
    TemplateEngineProtocol,
    TemplateProtocol,
    csrf_token,
    url_for,
    url_for_static_asset,
)
from litestar.utils.sync import AsyncCallable

from chameleon_rs import _settings, _views
from chameleon_rs.template import PageTemplate, PageTemplateFile
from chameleon_rs.web import DecoratorOrderError, ErrorPage, NotFound, TemplateError
from chameleon_rs.zpt.loader import TemplateLoader

if TYPE_CHECKING:
    from litestar.background_tasks import BackgroundTask, BackgroundTasks
    from litestar.datastructures import Cookie
    from litestar.response.base import ASGIResponse
    from litestar.types import TypeEncodersMap

__all__ = [
    'ChameleonEngine',
    'ChameleonTemplate',
    'DecoratorOrderError',
    'ErrorPage',
    'NotFound',
    'Template',
    'TemplateError',
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

# The folder a bare `@template` looks in (as the other modules: the configured folder, or
# 'templates' when nothing was configured before the handler was decorated).
template_path: str | None = None

type TemplateContext = Mapping[str, Any]

# The callables every Litestar template engine registers (litestar/plugins/jinja.py, mako.py).
_DEFAULT_CALLABLES: dict[str, Callable[..., Any]] = {
    'url_for_static_asset': url_for_static_asset,
    'csrf_token': csrf_token,
    'url_for': url_for,
}


def _variables(context: Mapping[str, Any], callables: Mapping[str, Callable[..., Any]]) -> dict[str, Any]:
    """A template's variables: the context, with each template callable bound to it (called as
    ``url_for('name', ...)``, it receives the context first, as Litestar's callables expect). The
    context's own keys win, as over Jinja's globals."""
    variables = dict(context)
    for key, template_callable in callables.items():
        if key not in variables:
            variables[key] = partial(template_callable, variables)
    return variables


# --- ChameleonEngine: Litestar's TemplateEngineProtocol ----------------------------------------


class ChameleonTemplate(TemplateProtocol):
    """A page template as Litestar's ``TemplateProtocol``: ``render(**context)`` renders it with
    the engine's template callables bound to the context."""

    def __init__(self, template: PageTemplateFile, template_callables: Mapping[str, Callable[..., Any]]) -> None:
        self.template = template
        self.template_callables = template_callables

    def render(self, *args: Any, **kwargs: Any) -> str:
        """The page, rendered with ``kwargs`` as the context (Litestar passes no positional
        arguments)."""
        if args:
            msg = 'ChameleonTemplate.render() takes the context as keyword arguments'
            raise TypeError(msg)
        return self.template.render(encoding='utf-8', **_variables(kwargs, self.template_callables))

    async def render_async(self, **kwargs: Any) -> str:
        """``render`` as a coroutine: the template's ``render_async`` (awaitable values are
        awaited where the template uses them). Litestar's ``Template`` response never calls it;
        it is for code rendering a template itself."""
        return await self.template.render_async(encoding='utf-8', **_variables(kwargs, self.template_callables))


class ChameleonEngine(TemplateEngineProtocol[ChameleonTemplate, TemplateContext]):
    """Litestar's ``TemplateEngineProtocol`` for chameleon-rs templates:
    ``TemplateConfig(engine=ChameleonEngine, directory='templates')``.

    ``directory``: the template folder (or a list of folders, searched in order).
    ``engine_instance``: a ``chameleon_rs.PageTemplateLoader`` to use instead (or
    ``ChameleonEngine.from_loader(loader)``). Neither: the templates come from the process-wide
    configuration (``chameleon_rs.configure`` / ``configure`` in this module), looked up at each
    render (``TemplateConfig(engine=ChameleonEngine())``). ``auto_reload`` /
    ``restricted_namespace`` (with ``directory``): the loader's options (``None``: Chameleon's
    defaults, ``CHAMELEON_RELOAD`` for reloading); pass an instance to set them
    (``TemplateConfig(engine=ChameleonEngine('templates', auto_reload=True))``).

    Registers Litestar's ``url_for``, ``csrf_token`` and ``url_for_static_asset``, as its Jinja
    and Mako engines do."""

    def __init__(
        self,
        directory: str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = None,
        engine_instance: TemplateLoader | None = None,
        *,
        auto_reload: bool | None = None,
        restricted_namespace: bool | None = None,
    ) -> None:
        if directory is not None and engine_instance is not None:
            msg = 'You must provide either a directory or a chameleon_rs PageTemplateLoader, not both.'
            raise ImproperlyConfiguredException(msg)
        self._loader: TemplateLoader | None = engine_instance
        if directory is None and (auto_reload is not None or restricted_namespace is not None):
            msg = 'auto_reload and restricted_namespace are loader options: pass a directory, or configure() them'
            raise TypeError(msg)
        if directory is not None:
            if isinstance(directory, (str, os.PathLike)):
                search_path: Any = os.fspath(directory)
            else:
                search_path = [os.fspath(folder) for folder in directory]
            options: dict[str, Any] = {}
            if auto_reload is not None:
                options['auto_reload'] = auto_reload
            if restricted_namespace is not None:
                options['restricted_namespace'] = restricted_namespace
            self._loader = TemplateLoader(search_path, **options)
        # Replaced whole when a callable is registered (templates already handed out keep theirs).
        self.template_callables: Mapping[str, Callable[..., Any]] = dict(_DEFAULT_CALLABLES)
        # Compiled `render_string` templates by (loader, string), guarded by the lock (sync
        # handlers render in worker threads; free-threaded builds).
        self._strings: dict[tuple[TemplateLoader | None, str], PageTemplate] = {}
        self._strings_lock = threading.Lock()

    @classmethod
    def from_loader(cls, loader: TemplateLoader) -> ChameleonEngine:
        """An engine over an existing ``chameleon_rs.PageTemplateLoader`` (as Litestar's
        ``JinjaTemplateEngine.from_environment``)."""
        return cls(engine_instance=loader)

    @property
    def loader(self) -> TemplateLoader:
        """The ``TemplateLoader`` templates come from (the configured one when the engine was
        given neither a directory nor a loader)."""
        if self._loader is not None:
            return self._loader
        settings = _settings.views['litestar']
        if settings is None:
            msg = 'ChameleonEngine() without a directory needs chameleon_rs.configure(template_folder=...) first.'
            raise TemplateError(msg)
        return settings.loader

    def get_template(self, template_name: str) -> ChameleonTemplate:
        """The page template ``template_name`` (relative to the template folder).

        Raises Litestar's ``TemplateNotFoundException`` if no folder has it, or if the name
        could reach outside the folder (absolute, ``..``, a backslash, a drive letter or a
        package spec), as Litestar's Jinja engine refuses such names."""
        if _views.outside_folder(template_name):
            raise TemplateNotFoundException(template_name=template_name)
        try:
            page = self.loader[template_name]
        except ValueError as exc:
            # Only the loader's own lookup failure (its `default_extension` and search path
            # applied); other errors pass through.
            if not _views.is_not_found(exc):
                raise
            raise TemplateNotFoundException(template_name=template_name) from None
        return ChameleonTemplate(page, self.template_callables)

    def render_string(self, template_string: str, context: Mapping[str, Any]) -> str:
        """Renders a template given as a string (``Template(template_str=...)``) with
        ``context`` and the template callables, compiled with the loader's template options
        (``translate``, ``restricted_namespace``, ...), as Jinja's ``from_string`` uses its
        environment. Compiled templates are kept for reuse (up to 128, the oldest dropped first),
        per loader: a later ``configure()`` (a new loader) compiles them afresh."""
        if self._loader is not None:
            loader: TemplateLoader | None = self._loader
        else:
            settings = _settings.views['litestar']
            loader = settings.loader if settings is not None else None
        key = (loader, template_string)
        with self._strings_lock:
            page = self._strings.get(key)
        if page is None:
            page = PageTemplate(template_string, **(loader.kwargs if loader is not None else {}))
            with self._strings_lock:
                while len(self._strings) >= 128:
                    self._strings.pop(next(iter(self._strings)))
                self._strings[key] = page
        return page.render(encoding='utf-8', **_variables(context, self.template_callables))

    def register_template_callable[**P, T](
        self, key: str, template_callable: TemplateCallableType[TemplateContext, P, T]
    ) -> None:
        """Makes ``template_callable`` a variable ``key`` of every template; called from a
        template, it receives the template's context first (``url_for(context, name)``)."""
        self.template_callables = {**self.template_callables, key: template_callable}


def _guessed_type(template_name: str) -> str | None:
    """The media type Litestar's ``Template`` guesses from the name's suffixes, if any."""
    for suffix in PurePath(template_name).suffixes:
        guessed = guess_type(f'name{suffix}')[0]
        if guessed:
            return guessed
    return None


class Template(_LitestarTemplate):
    """Litestar's ``Template`` response, served as ``text/html`` when Litestar cannot guess the
    media type from the template's name (``.pt``, which Litestar serves as ``text/plain``). An
    explicit ``media_type`` (here or on the route handler) wins, and names Litestar can guess
    (``.html``, ``.xml``, ``.txt``) keep their type."""

    __slots__ = ()

    def to_asgi_response(
        self,
        app: Litestar | None,
        request: Request[Any, Any, Any],
        *,
        background: BackgroundTask | BackgroundTasks | None = None,
        cookies: Iterable[Cookie] | None = None,
        encoded_headers: Iterable[tuple[bytes, bytes]] | None = None,
        headers: dict[str, str] | None = None,
        is_head_response: bool = False,
        media_type: MediaType | str | None = None,
        status_code: int | None = None,
        type_encoders: TypeEncodersMap | None = None,
    ) -> ASGIResponse:
        if not (self.media_type or media_type) and self.template_name and _guessed_type(self.template_name) is None:
            media_type = MediaType.HTML
        return super().to_asgi_response(
            app,
            request,
            background=background,
            cookies=cookies,
            encoded_headers=encoded_headers,
            headers=headers,
            is_head_response=is_head_response,
            media_type=media_type,
            status_code=status_code,
            type_encoders=type_encoders,
        )


# --- The decorator API (the other modules' module API) -----------------------------------------


def _request_context(request: Any) -> dict[str, Any] | None:
    """What a Litestar template gets besides its context (``Template.create_template_context``
    and the engine's callables): ``request``, ``csrf_input``, and the template callables of the
    app's ``ChameleonEngine`` (Litestar's defaults otherwise), bound to these variables."""
    if not isinstance(request, Request):
        return None
    base: dict[str, Any] = {'request': request}
    base['csrf_input'] = f'<input type="hidden" name="_csrf_token" value="{html.escape(csrf_token(base))}" />'
    engine = request.app.template_engine
    callables = engine.template_callables if isinstance(engine, ChameleonEngine) else _DEFAULT_CALLABLES
    return _variables(base, callables)


def _annotations(f: Callable[..., Any]) -> dict[str, Any]:
    """The handler's annotations, unevaluated where they cannot be evaluated yet (Litestar
    resolves them when the route registers)."""
    if sys.version_info >= (3, 14):
        import annotationlib

        return dict(annotationlib.get_annotations(f, format=annotationlib.Format.FORWARDREF))
    return dict(getattr(f, '__annotations__', None) or {})


def _signature(f: Callable[..., Any]) -> inspect.Signature:
    if sys.version_info >= (3, 14):
        import annotationlib

        return inspect.signature(f, annotation_format=annotationlib.Format.FORWARDREF)
    return inspect.signature(f)


def _handler_signature(f: Callable[..., Any]) -> tuple[inspect.Signature, dict[str, Any], bool]:
    """The wrapper's signature and annotations for Litestar: the handler's parameters, plus a
    keyword-only ``request`` when it has none (Litestar injects the request by that name; the
    wrapper takes it out before calling the handler), returning ``litestar.Response``. Returns
    whether ``request`` was added."""
    signature = _signature(f)
    annotations = _annotations(f)
    params = list(signature.parameters.values())
    added = 'request' not in signature.parameters
    if added:
        request = inspect.Parameter('request', inspect.Parameter.KEYWORD_ONLY, annotation=Request)
        at = next((i for i, p in enumerate(params) if p.kind is inspect.Parameter.VAR_KEYWORD), len(params))
        params.insert(at, request)
        annotations['request'] = Request
    annotations['return'] = Response
    return signature.replace(parameters=params, return_annotation=Response), annotations, added


def _handler_function(handler: Any) -> Any:
    """The function a route handler calls: through Litestar's ``AsyncCallable`` (a sync handler
    run in a thread) and a bound method (a ``Controller``'s handler)."""
    try:
        fn = handler.fn
    except ImproperlyConfiguredException:
        return None
    if isinstance(fn, AsyncCallable):
        fn = fn.func
    return getattr(fn, '__func__', fn)


class _FirstRequestCheck:
    """The app's ``asgi_handler`` until the decorator-order check passed: runs it before the first
    request (an app served without lifespan events), then puts the real handler back."""

    def __init__(self, owner: Any, app: Any, run: Callable[[], None]) -> None:
        self.owner = owner
        self.app = app
        self.run = run

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope['type'] in ('http', 'websocket'):
            self.run()
            if self.owner.asgi_handler is self:
                self.owner.asgi_handler = self.app
        await self.app(scope, receive, send)


class _StartupCheck:
    """The ``on_startup`` hook running the decorator-order check (how an app is known to be
    watched: a ``Litestar`` app cannot be weakly referenced)."""

    def __init__(self, run: Callable[[], None]) -> None:
        self.run = run

    def __call__(self) -> None:
        self.run()


class _Litestar(_views.Integration):
    name = 'litestar'
    module = __name__
    error = TemplateError
    response_name = 'litestar.Response'
    route_decorators = '@get, @post, @route, ..., or a route handler class'

    def is_response(self, value: Any) -> bool:
        return isinstance(value, Response)

    def make_response(self, html: str, content_type: str, status_code: int) -> Response[str]:
        # 0: the route's status code (Litestar: 200, 201 for POST, or `@get(status_code=...)`),
        # as a Litestar response without one.
        return Response(content=html, status_code=status_code or None, media_type=content_type)

    def decorate(
        self,
        f: Callable[..., Any],
        template_file: str | None,
        content_type: str,
        status_code: int,
        concurrent: bool | None = None,
        offload: bool = False,
    ) -> Callable[..., Any]:
        """Wraps a handler (sync or async) to render its template with what it returns, with
        Litestar's template variables, and the signature Litestar reads."""
        if isinstance(f, BaseRouteHandler):
            function = _handler_function(f)
            msg = (
                f'@template must be placed below the route decorator ({self.route_decorators}) on '
                f'{_views._describe(function) if function is not None else repr(f)}: it was given '
                'the route handler, so its template would never render. Put the route decorator first '
                '(outermost) and @template under it.'
            )
            raise DecoratorOrderError(msg)
        view_template = self.view_template(f, template_file)
        signature, annotations, added = _handler_signature(f)

        @wraps(f)
        def sync_view_method(*args: Any, **kwargs: Any) -> Any:
            extra = _request_context(kwargs.pop('request', None) if added else kwargs.get('request'))
            try:
                value = f(*args, **kwargs)
                return self.render_response(view_template, value, content_type, status_code, extra)
            except ErrorPage as page:
                return self.error_response(page, extra)

        @wraps(f)
        async def async_view_method(*args: Any, **kwargs: Any) -> Any:
            extra = _request_context(kwargs.pop('request', None) if added else kwargs.get('request'))
            try:
                value = await f(*args, **kwargs)
                return await self.render_response_async(
                    view_template, value, content_type, status_code, concurrent, offload, extra
                )
            except ErrorPage as page:
                return await self.error_response_async(page, extra)

        wrapper = async_view_method if inspect.iscoroutinefunction(f) else sync_view_method
        wrapper.__signature__ = signature  # ty: ignore[invalid-assignment] - read by inspect.signature
        wrapper.__annotations__ = annotations
        _views._remember(f, wrapper)
        return wrapper

    # --- The decorator-order check ----------------------------------------------------------

    def routes(self, app: Any) -> Iterator[tuple[Any, str]]:
        for route in app.routes:
            if isinstance(route, HTTPRoute):
                for handler in route.route_handlers:
                    methods = ','.join(sorted(handler.http_methods))
                    yield _handler_function(handler), f'{methods} {route.path}'
            else:
                handler = getattr(route, 'route_handler', None)
                if handler is not None:
                    kind = 'WEBSOCKET' if 'websocket' in type(route).__name__.lower() else 'ASGI'
                    yield _handler_function(handler), f'{kind} {route.path}'

    def check_app(self, app: Any) -> None:
        if not isinstance(app, Litestar):
            msg = f'app= takes a Litestar application, not {type(app).__name__} (a Router is checked through the app that registers it)'
            raise TypeError(msg)

    def watch_app(self, app: Any) -> None:
        """Runs ``check_views(app)`` when the app starts (an ``on_startup`` hook, first in the
        list), or before its first request when no lifespan runs; once per app."""
        self.check_app(app)
        if any(isinstance(hook, _StartupCheck) for hook in app.on_startup):
            return
        passed = False

        def run() -> None:
            nonlocal passed
            if not passed:
                self.check_views(app)
                passed = True

        app.on_startup.insert(0, _StartupCheck(run))
        app.asgi_handler = _FirstRequestCheck(app, app.asgi_handler, run)


_litestar = _Litestar()


def configure(
    template_folder: Any = None,
    *,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
    app: Litestar | None = None,
    check_order: bool = True,
) -> None:
    """Configures where templates live and how they load, for the whole process (the same as
    ``chameleon_rs.configure``: for handlers, ``ChameleonEngine()`` without a directory and
    ``render_partial``). Settings left out keep their current values.

    ``app``: the Litestar app checks its handlers when it starts (an ``on_startup`` hook, or
    before its first request when no lifespan runs): a handler its routes call without the
    ``@template`` wrapper that wraps it raises ``chameleon_rs.web.DecoratorOrderError``. With
    only ``app`` given, the settings are left as they are. ``check_order=False`` turns the check
    off, for an app whose routes call a function raw that is also used through a ``@template``
    wrapper not routed on this app (call ``check_views(app, ignore=[...])`` in a test instead).

    Raises ``chameleon_rs.web.TemplateError`` if the folder is missing or not a directory, and
    ``TypeError`` if ``app`` is not a Litestar application.
    """
    if app is not None:
        _litestar.check_app(app)
        if template_folder is None and auto_reload is None and restricted_namespace is None:
            if check_order:
                _litestar.watch_app(app)
            return
    settings = _settings.build(
        template_folder,
        auto_reload,
        restricted_namespace,
        base=_settings.views['litestar'] or _settings.partials,
        error=TemplateError,
    )
    _settings.install(partials_settings=settings, views_settings={'litestar': settings})
    if app is not None and check_order:
        _litestar.watch_app(app)


def global_init(
    template_folder: str,
    auto_reload: bool = False,
    cache_init: bool = True,
    restricted_namespace: bool = True,
    *,
    app: Litestar | None = None,
    check_order: bool = True,
) -> None:
    """The other integrations' setup call, for Litestar: configures the handlers' templates only
    (partials keep their own folder). With ``cache_init`` (the default) a call after the
    handlers' folder is configured does nothing. ``app``: check that app's handlers when it
    starts, as ``configure(app=...)`` does.

    Raises ``chameleon_rs.web.TemplateError`` if the folder is missing or not a directory."""
    if app is not None:
        _litestar.check_app(app)
    _litestar.global_init(template_folder, auto_reload, cache_init, restricted_namespace)
    if app is not None and check_order:
        _litestar.watch_app(app)


def clear() -> None:
    """Forgets the handlers' process-wide configuration, so a later ``configure()`` /
    ``global_init()`` starts afresh. Mostly useful in tests."""
    _litestar.clear()


def check_views(app: Any, ignore: Iterable[Any] = ()) -> None:
    """Raises ``chameleon_rs.web.DecoratorOrderError`` if a route of ``app`` (a Litestar app or
    a ``Router``, controllers included) calls a handler function that ``@template`` wrapped
    without going through the wrapper. Runs by itself when the app starts, for an app given to
    ``configure(app=...)``.

    It cannot tell the mistake from a function routed raw on purpose whose ``@template``
    wrapper is used elsewhere (called from another handler, or routed on another app): list
    such functions in ``ignore``."""
    if not hasattr(app, 'routes'):
        msg = f'check_views() takes a Litestar application or Router, not {type(app).__name__}'
        raise TypeError(msg)
    _litestar.check_views(app, ignore)


def _sync() -> None:
    # Called by `_settings.install`: the `template_path` module state.
    global template_path
    template_path = _litestar.template_path()


_settings.listeners[__name__] = _sync
_sync()  # configured before this module was imported: the global already says so


def render(template_file: str, **template_data: Any) -> str:
    """Renders a template (path relative to the template folder) to a ``str``.

    Raises ``chameleon_rs.web.TemplateError`` if nothing is configured.
    """
    return _litestar.render(template_file, template_data)


async def render_async(template_file: str, **template_data: Any) -> str:
    """``render`` as a coroutine: the template's ``render_async``, which awaits awaitable
    values where the template uses them (see ``PageTemplate.render_async``)."""
    return await _litestar.render_async(template_file, template_data)


def response(
    template_file: str, content_type: str = 'text/html', status_code: int | None = None, **template_data: Any
) -> Response[str]:
    """Renders a template into a ``litestar.Response`` (every keyword but ``content_type`` and
    ``status_code`` is the model). ``status_code=None``: the route's (200, 201 for ``@post``)."""
    return _litestar.make_response(render(template_file, **template_data), content_type, status_code or 0)


async def response_async(
    template_file: str, content_type: str = 'text/html', status_code: int | None = None, **template_data: Any
) -> Response[str]:
    """``response`` as a coroutine, rendering with ``render_async``."""
    html = await render_async(template_file, **template_data)
    return _litestar.make_response(html, content_type, status_code or 0)


class TemplateDecorator(Protocol):
    """What ``@template(...)`` returns: decorates a handler, sync or async, into one with the
    same parameters that returns a ``litestar.Response``."""

    @overload
    def __call__[**P](
        self, f: Callable[P, Coroutine[Any, Any, Any]], /
    ) -> Callable[P, Coroutine[Any, Any, Response[Any]]]: ...
    @overload
    def __call__[**P](self, f: Callable[P, Any], /) -> Callable[P, Response[Any]]: ...


@overload
def template[**P](
    template_file: Callable[P, Coroutine[Any, Any, Any]], /
) -> Callable[P, Coroutine[Any, Any, Response[Any]]]: ...
@overload
def template[**P](template_file: Callable[P, Any], /) -> Callable[P, Response[Any]]: ...
@overload
def template(
    template_file: str | None = None,
    content_type: str = 'text/html',
    status_code: int | None = None,
    *,
    mimetype: str | None = None,
    concurrent: bool | None = None,
    offload: bool = False,
) -> TemplateDecorator: ...
def template(
    template_file: Callable[..., Any] | str | None = None,
    content_type: str = 'text/html',
    status_code: int | None = None,
    *,
    mimetype: str | None = None,
    concurrent: bool | None = None,
    offload: bool = False,
) -> Callable[..., Any]:
    """Decorates a Litestar route handler function (sync or async; a ``Controller`` method too)
    to render a template with what it returns: bare (``@template``), ``@template()`` or
    ``@template('home/index.pt', content_type=..., status_code=...)``. Place it below the route
    decorator. ``status_code=None`` (the default): the route's, as for any Litestar response
    without one (200, 201 for ``@post``, or ``@get(..., status_code=...)``). The template also gets ``request``, ``csrf_input``, ``url_for``, ``csrf_token``
    and ``url_for_static_asset``. See the module docs.

    ``concurrent=True`` (async handlers): native partials called with an awaitable argument
    render concurrently (``None``, the default, keeps the template's setting).
    ``offload=True`` (async handlers, experimental): the page renders in a worker thread.

    Typing: the decorated handler keeps its parameters and is typed as returning a
    ``litestar.Response`` (a coroutine of one for an ``async def`` handler)."""
    wrapped_function = None
    if callable(template_file):
        wrapped_function = template_file
        template_file = None
    content_type = mimetype or content_type

    def response_inner(f: Callable[..., Any]) -> Callable[..., Any]:
        return _litestar.decorate(f, template_file, content_type, status_code or 0, concurrent, offload)

    return response_inner(wrapped_function) if wrapped_function else response_inner


def not_found(template_file: str = 'errors/404.pt', **template_data: Any) -> NoReturn:
    """Ends the current ``@template`` handler with a 404 page: ``template_file`` rendered with
    ``template_data``, as ``text/html``.

    Raises ``chameleon_rs.web.NotFound``, which the decorator turns into the response."""
    raise NotFound('The URL resulted in a 404 response.', template_file, template_data or None)


def error(template_file: str, status_code: int = 500, **template_data: Any) -> NoReturn:
    """Ends the current ``@template`` handler with an error page: ``template_file`` rendered
    with ``template_data`` and ``status_code``, as ``text/html``.

    Raises ``chameleon_rs.web.ErrorPage``, which the decorator turns into the response."""
    raise ErrorPage(template_file, status_code, 'The URL resulted in an error.', template_data or None)


def generic_error(template_file: str, status_code: int, template_data: Mapping[str, Any] | None = None) -> NoReturn:
    """fastapi-chameleon's name and signature for ``error()``."""
    raise ErrorPage(template_file, status_code, 'The URL resulted in an error.', template_data)
