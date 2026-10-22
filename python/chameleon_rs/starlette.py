# Parts ported from Starlette (https://github.com/encode/starlette, 1.9.0: starlette/templating.py,
# BSD-3-Clause), modified: `Templates` renders chameleon-rs templates instead of Jinja2, with an
# async `TemplateResponse_async`, and `url_for` passed as a template variable. The module API
# (`@template`, `render`, `response`, ...) follows fastapi-chameleon
# (https://github.com/mikeckennedy/fastapi-chameleon at 4d070f2), on the shared views core.
"""Starlette integration: render chameleon-rs templates from endpoints.

Importing this module imports Starlette (it is not imported by ``import chameleon_rs``). Two
ways to use it, which can be mixed:

``Templates``, a drop-in for Starlette's ``Jinja2Templates``::

    from chameleon_rs.starlette import Templates

    templates = Templates(directory='templates')

    async def homepage(request):
        return templates.TemplateResponse(request, 'home/index.pt', {'title': 'Home'})

    async def slow_page(request):  # renders with render_async: never blocks the event loop
        return await templates.TemplateResponse_async(request, 'home/index.pt', {'items': load()})

``TemplateResponse(request, name, context=None, status_code=200, headers=None, media_type=None,
background=None)`` is Starlette's: ``request`` is added to the context (unless it has one), the
context processors run, the template can call ``url_for(name, **path_params)``, and the response
sends the ``http.response.debug`` event, so Starlette's ``TestClient`` gives
``response.template`` (the page template, with Jinja's ``name``) and ``response.context``.
``TemplateResponse_async`` takes the same arguments and renders with ``render_async``.
``templates.globals`` is Jinja's ``env.globals``. A missing template raises
``chameleon_rs.web.TemplateNotFound``; so does a name that could reach outside the folder
(absolute, ``..``, ...), as Jinja refuses it. (The decorator API below looks names up as the
original libraries do: template names must come from code, never from a request.)

The decorator API, the same as ``chameleon_rs.flask`` and ``chameleon_rs.fastapi``::

    import chameleon_rs.starlette as chameleon_starlette

    @chameleon_starlette.template('home/index.pt')
    async def homepage(request):
        return {'title': 'Home'}

    app = Starlette(routes=[Route('/', homepage)])
    chameleon_starlette.configure(template_folder='templates', app=app)

- ``configure(template_folder=..., auto_reload=..., restricted_namespace=..., app=None)``: the
  process-wide configuration (the same as ``chameleon_rs.configure``: for views, ``Templates()``
  without a directory and ``render_partial``); ``app`` adds the decorator-order check at
  startup. ``global_init`` configures the views only (``cache_init=True`` ignores a second call).
- ``@template``, ``@template()``, ``@template('home/index.pt', content_type=...,
  status_code=...)`` (``mimetype=`` is accepted too): renders what the endpoint returns (a
  ``dict``, any ``Mapping``, a dataclass or a Pydantic-like model) as a ``HTMLResponse``; a
  response the endpoint returns passes through. The template also gets ``request`` and
  ``url_for`` (unless the model has them). Function endpoints and the methods of an
  ``HTTPEndpoint`` class, sync or async: an ``async def`` endpoint renders with ``render_async``
  on the event loop, a sync one with ``render`` in Starlette's thread pool.
- ``render(template_file, **data)`` and ``response(template_file, content_type=...,
  status_code=..., **data)``; ``render_async`` and ``response_async`` are their coroutines.
- ``not_found(template_file='errors/404.pt', **data)`` and ``error(template_file,
  status_code=500, **data)`` end a decorated endpoint with that page.
- ``clear()`` forgets the process-wide configuration; ``check_views(app)`` runs the
  decorator-order check.

Errors: ``chameleon_rs.web.TemplateError``, ``TemplateNotFound``, ``NotFound``, ``ErrorPage``
and ``DecoratorOrderError`` (also importable from here).
"""

from __future__ import annotations

import contextlib
import inspect
import os
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterable, Iterator, Mapping, Sequence
from os import PathLike
from typing import Any, NoReturn, Protocol, overload

import starlette.responses
import starlette.routing
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.types import Receive, Scope, Send

from chameleon_rs import _settings, _views
from chameleon_rs.template import PageTemplateFile, _render_async
from chameleon_rs.web import DecoratorOrderError, ErrorPage, NotFound, TemplateError, TemplateNotFound
from chameleon_rs.zpt.loader import TemplateLoader

__all__ = [
    'DecoratorOrderError',
    'ErrorPage',
    'NotFound',
    'TemplateError',
    'TemplateNotFound',
    'Templates',
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

# The folder a bare `@template` looks in (as chameleon_rs.flask / .fastapi: the configured
# folder, or 'templates' when nothing was configured before the endpoint was decorated).
template_path: str | None = None

# The HTTP methods an `HTTPEndpoint` class answers with a method of that name.
_ENDPOINT_METHODS = ('get', 'head', 'post', 'put', 'patch', 'delete', 'options', 'query')


# --- Shared with chameleon_rs.fastapi (FastAPI is Starlette) ----------------------------------


class _FirstRequestCheck:
    """The app's middleware stack until the decorator-order check passed: runs it before the
    first request (an app served without lifespan events, or a test client used without
    ``with``), then puts the real stack back. ``app`` is the stack, as middleware name it."""

    def __init__(self, owner: Any, app: Any, run: Callable[[], None]) -> None:
        self.owner = owner
        self.app = app
        self.run = run

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope['type'] in ('http', 'websocket'):
            self.run()
            if self.owner.middleware_stack is self:
                self.owner.middleware_stack = self.app
        await self.app(scope, receive, send)


def _endpoint_views(route: Any, endpoint: Any, path: str) -> Iterator[tuple[Any, str]]:
    """The views a route calls: its endpoint, or each handler method of an ``HTTPEndpoint``
    class (``GET /path (Page.get)``)."""
    if isinstance(route, starlette.routing.WebSocketRoute):
        yield endpoint, f'WEBSOCKET {path}'
        return
    if inspect.isclass(endpoint):
        for name in _ENDPOINT_METHODS:
            handler = getattr(endpoint, name, None)
            if callable(handler):
                yield handler, f'{name.upper()} {path} ({endpoint.__name__}.{name})'
        return
    methods = getattr(route, 'methods', None)
    kind = ','.join(sorted(set(methods) - {'HEAD'})) if methods else 'ANY'
    yield endpoint, f'{kind} {path}'


def _walk(routes: Iterable[Any], prefix: str, seen: set[int]) -> Iterator[tuple[Any, str]]:
    """Every endpoint of these routes and the routes they mount (sub-applications, routers
    mounted with ``Mount``, ``Host``), with ``METHODS /path``."""
    for route in routes:
        if id(route) in seen:
            continue
        seen.add(id(route))
        path = prefix + str(getattr(route, 'path', '') or '')
        endpoint = getattr(route, 'endpoint', None)
        if endpoint is not None:
            yield from _endpoint_views(route, endpoint, path)
        sub = _sub_routes(route)
        if sub:
            yield from _walk(sub, path, seen)
        # A router included into another (FastAPI 0.13x and later include lazily: the app's
        # routes hold the included router, not copies of its routes).
        included = getattr(route, 'original_router', None)
        if included is not None:
            include_prefix = getattr(getattr(route, 'include_context', None), 'prefix', '') or ''
            yield from _walk(getattr(included, 'routes', ()), prefix + str(include_prefix), seen)


def _sub_routes(route: Any) -> Any:
    """The routes a route mounts: its own (``Mount``, ``Host``), or those of the app it mounts
    found through a chain of middleware (``Mount('/g', app=GZipMiddleware(Starlette(...)))``),
    each middleware's ``app`` followed a bounded number of times."""
    sub = getattr(route, 'routes', None)
    if sub or not isinstance(route, (starlette.routing.Mount, starlette.routing.Host)):
        return sub
    app = getattr(route, 'app', None)
    for _ in range(32):
        if app is None:
            return None
        sub = getattr(app, 'routes', None)
        if sub:
            return sub
        app = getattr(app, 'app', None)
    return None


class StarletteIntegration(_views.Integration):
    """Starlette's part of the views core (``chameleon_rs.fastapi`` subclasses it)."""

    name = 'starlette'
    module = __name__
    error = TemplateError
    response_name = 'starlette.responses.Response'
    route_decorators = 'app.add_route, Route(...), @app.route (0.x), a route decorator of your own'
    app_kind = 'Starlette'
    router_hint = 'a Router is checked through the app that mounts it'

    def is_response(self, value: Any) -> bool:
        return isinstance(value, starlette.responses.Response)

    def make_response(self, html: str, content_type: str, status_code: int) -> starlette.responses.Response:
        return HTMLResponse(content=html, status_code=status_code, media_type=content_type)

    def view_context(self, args: tuple[Any, ...]) -> dict[str, Any] | None:
        # The request a Starlette endpoint (or an `HTTPEndpoint` method) was called with, and
        # `url_for` on it, as `Templates` gives them.
        for arg in args:
            if isinstance(arg, Request):
                return {'request': arg, 'url_for': _url_for(arg)}
        return None

    def routes(self, app: Any) -> Iterator[tuple[Any, str]]:
        yield from _walk(getattr(app, 'routes', ()), '', set())

    def check_app(self, app: Any) -> None:
        if not (hasattr(app, 'router') and hasattr(app, 'build_middleware_stack') and hasattr(app, 'routes')):
            msg = f'app= takes a {self.app_kind} application, not {type(app).__name__} ({self.router_hint})'
            raise TypeError(msg)

    def watch(self, app: Any, check: Callable[[], None]) -> None:
        # At startup, inside the lifespan (so a server reports a failed startup and exits),
        # and before the first request when no lifespan ran.
        passed = False

        def run() -> None:
            nonlocal passed
            if not passed:
                check()
                passed = True

        router = app.router
        original_lifespan = router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan(asgi_app: Any) -> AsyncGenerator[Any]:
            run()
            async with original_lifespan(asgi_app) as state:
                yield state

        router.lifespan_context = lifespan
        original_build = app.build_middleware_stack

        def build_middleware_stack() -> Any:
            stack = original_build()
            return stack if passed else _FirstRequestCheck(app, stack, run)

        app.build_middleware_stack = build_middleware_stack
        if getattr(app, 'middleware_stack', None) is not None:
            app.middleware_stack = _FirstRequestCheck(app, app.middleware_stack, run)


_starlette = StarletteIntegration()


def _url_for(request: Request) -> Callable[..., Any]:
    """``url_for(name, /, **path_params)`` for templates, as Starlette's Jinja2 global."""

    def url_for(name: str, /, **path_params: Any) -> Any:
        return request.url_for(name, **path_params)

    return url_for


# --- Templates: Starlette's Jinja2Templates for chameleon-rs ----------------------------------


def _check_name(name: str) -> None:
    """Refuses a name that could load a template outside the folder, as Jinja's
    ``FileSystemLoader`` does (``_views.outside_folder``)."""
    if _views.outside_folder(name):
        msg = f'Template not found: {name}.'
        raise TemplateNotFound(msg)


class _DebugTemplate:
    """The template in the ``http.response.debug`` event (``response.template`` in tests): the
    page template, plus Jinja's ``name`` (the name it was loaded by). Every other attribute is
    the page template's."""

    __slots__ = ('_page', 'name')

    def __init__(self, page: PageTemplateFile, name: str) -> None:
        self._page = page
        self.name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._page, attr)

    def __repr__(self) -> str:
        return f'<{type(self._page).__name__} {self.name!r}>'


class _UrlFor:
    """``Templates.globals['url_for']`` by default: ``request.url_for`` for the request being
    rendered (Jinja's global is a ``pass_context`` function)."""

    def __repr__(self) -> str:
        return '<url_for of the request>'


_URL_FOR = _UrlFor()


class _TemplateResponse(HTMLResponse):
    """A rendered page that sends the ``http.response.debug`` event (Starlette's
    ``TestClient`` sets ``response.template`` and ``response.context`` from it)."""

    def __init__(
        self,
        template: Any,
        context: dict[str, Any],
        content: str,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        self.template = template
        self.context = context
        super().__init__(content, status_code, headers, media_type, background)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = self.context.get('request', {})
        extensions = request.get('extensions', {})
        if 'http.response.debug' in extensions:
            await send({'type': 'http.response.debug', 'info': {'template': self.template, 'context': self.context}})
        await super().__call__(scope, receive, send)


class Templates:
    """Starlette's ``Jinja2Templates`` for chameleon-rs templates.

    ``directory``: the template folder (or a list of folders, searched in order). Left out, the
    templates come from the process-wide configuration (``chameleon_rs.configure`` /
    ``configure`` in this module), looked up at each render. ``context_processors``: functions
    taking the request and returning a dict added to each template's context.
    ``auto_reload`` / ``restricted_namespace`` (with ``directory``): the loader's options
    (``None``: Chameleon's defaults, ``CHAMELEON_RELOAD`` for reloading)."""

    def __init__(
        self,
        directory: str | PathLike[str] | Sequence[str | PathLike[str]] | None = None,
        *,
        context_processors: list[Callable[[Request], dict[str, Any]]] | None = None,
        auto_reload: bool | None = None,
        restricted_namespace: bool | None = None,
    ) -> None:
        self.context_processors = context_processors or []
        # Jinja's `env.globals`: variables of every template, below the context.
        self.globals: dict[str, Any] = {}
        self.globals.setdefault('url_for', _URL_FOR)
        self._loader: TemplateLoader | None = None
        if directory is None:
            if auto_reload is not None or restricted_namespace is not None:
                msg = 'auto_reload and restricted_namespace are loader options: pass a directory, or configure() them'
                raise TypeError(msg)
            return
        if isinstance(directory, (str, PathLike)):
            search_path: Any = str(directory)
        else:
            search_path = [str(folder) for folder in directory]
        options: dict[str, Any] = {}
        if auto_reload is not None:
            options['auto_reload'] = auto_reload
        if restricted_namespace is not None:
            options['restricted_namespace'] = restricted_namespace
        self._loader = TemplateLoader(search_path, **options)

    @property
    def loader(self) -> TemplateLoader:
        """The ``TemplateLoader`` templates come from (the configured one when no directory was
        given)."""
        if self._loader is not None:
            return self._loader
        settings = _settings.views['starlette']
        if settings is None:
            msg = 'Templates() without a directory needs chameleon_rs.configure(template_folder=...) first.'
            raise TemplateError(msg)
        return settings.loader

    def get_template(self, name: str) -> PageTemplateFile:
        """The page template ``name`` (relative to the template folder).

        Raises ``chameleon_rs.web.TemplateNotFound`` (a ``ValueError``) if no folder has it,
        or if the name could reach outside the folder (absolute, ``..``, a backslash, a drive
        letter or a package spec), as Jinja's ``FileSystemLoader`` refuses such names. (Only
        here: ``PageTemplateLoader`` and the decorator API raise Chameleon's ``ValueError``.)"""
        _check_name(name)
        loader = self.loader
        spec = name.strip()
        if not any(os.path.isfile(os.path.join(folder, spec)) for folder in loader.search_path):
            msg = f'Template not found: {name}.'
            raise TemplateNotFound(msg)
        return loader[name]

    def _context(self, request: Request, context: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        """The response's context (Starlette's: ``request`` set unless given, then the context
        processors) and the template's variables (``globals`` with the context over them, the
        default ``url_for`` bound to the request)."""
        if isinstance(request, str):
            msg = 'TemplateResponse() takes the request first: TemplateResponse(request, name, context)'
            raise TypeError(msg)
        context = context or {}
        context.setdefault('request', request)
        for context_processor in self.context_processors:
            context.update(context_processor(request))
        variables = {**self.globals, **context}
        if variables.get('url_for') is _URL_FOR:
            variables['url_for'] = _url_for(context['request'])
        return context, variables

    def TemplateResponse(
        self,
        request: Request,
        name: str,
        context: dict[str, Any] | None = None,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> _TemplateResponse:
        """Renders a template to an HTML response (Starlette's ``TemplateResponse``). Renders
        synchronously: in an ``async def`` endpoint, ``TemplateResponse_async`` does not block
        the event loop while the page renders."""
        context, variables = self._context(request, context)
        page = self.get_template(name)
        html = page.render(encoding='utf-8', **variables)
        return _TemplateResponse(
            _DebugTemplate(page, name), context, html, status_code, headers, media_type, background
        )

    async def TemplateResponse_async(
        self,
        request: Request,
        name: str,
        context: dict[str, Any] | None = None,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
        *,
        concurrent: bool | None = None,
        offload: bool = False,
    ) -> _TemplateResponse:
        """``TemplateResponse`` rendering with ``render_async``: awaitable values in the context
        are awaited where the template uses them, without blocking the event loop.
        ``concurrent=True`` fans native partials with awaitable arguments out (``None`` keeps
        the template's setting); ``offload=True`` (experimental) renders in a worker thread."""
        context, variables = self._context(request, context)
        page = self.get_template(name)
        if concurrent is None:
            concurrent = page.concurrent
        html = await _render_async(page, 'utf-8', variables, bool(concurrent), offload)
        return _TemplateResponse(
            _DebugTemplate(page, name), context, html, status_code, headers, media_type, background
        )


# --- The module API (chameleon_rs.flask's and chameleon_rs.fastapi's) -------------------------


def configure(
    template_folder: Any = None,
    *,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
    app: Any = None,
    check_order: bool = True,
) -> None:
    """Configures where templates live and how they load, for the whole process (the same as
    ``chameleon_rs.configure``: for views, ``Templates()`` without a directory and
    ``render_partial``). Settings left out keep their current values.

    ``app``: the Starlette app checks its views when it starts (in its lifespan, or before its
    first request when no lifespan runs): a view its routes registered before ``@template``
    wrapped it raises ``chameleon_rs.web.DecoratorOrderError``. Mounted apps and routers,
    ``Host`` and ``HTTPEndpoint`` classes are included. With only ``app`` given, the settings
    are left as they are. ``check_order=False`` turns the check off, for an app whose routes
    call a function raw that is also used through a ``@template`` wrapper not routed on this
    app (call ``check_views(app, ignore=[...])`` in a test instead).

    Raises ``chameleon_rs.web.TemplateError`` if the folder is missing or not a directory, and
    ``TypeError`` if ``app`` is not a Starlette application.
    """
    if app is not None:
        _starlette.check_app(app)
        if template_folder is None and auto_reload is None and restricted_namespace is None:
            if check_order:
                _starlette.watch_app(app)
            return
    settings = _settings.build(
        template_folder,
        auto_reload,
        restricted_namespace,
        base=_settings.views['starlette'] or _settings.partials,
        error=TemplateError,
    )
    _settings.install(partials_settings=settings, views_settings={'starlette': settings})
    if app is not None and check_order:
        _starlette.watch_app(app)


def global_init(
    template_folder: str,
    auto_reload: bool = False,
    cache_init: bool = True,
    restricted_namespace: bool = True,
    *,
    app: Any = None,
    check_order: bool = True,
) -> None:
    """fastapi-chameleon's setup call, for Starlette: configures the views' templates only
    (partials keep their own folder). With ``cache_init`` (the default) a call after the views'
    folder is configured does nothing. ``app``: check that app's views when it starts, as
    ``configure(app=...)`` does.

    Raises ``chameleon_rs.web.TemplateError`` if the folder is missing or not a directory."""
    if app is not None:
        _starlette.check_app(app)
    _starlette.global_init(template_folder, auto_reload, cache_init, restricted_namespace)
    if app is not None and check_order:
        _starlette.watch_app(app)


def clear() -> None:
    """Forgets the views' process-wide configuration, so a later ``configure()`` /
    ``global_init()`` starts afresh. Mostly useful in tests."""
    _starlette.clear()


def check_views(app: Any, ignore: Iterable[Any] = ()) -> None:
    """Raises ``chameleon_rs.web.DecoratorOrderError`` if a route of ``app`` (a Starlette app
    or a ``Router``; mounted apps, ``Host`` and ``HTTPEndpoint`` classes included) calls a view
    that ``@template`` wrapped after the route registered it. Runs by itself when the app
    starts, for an app given to ``configure(app=...)``.

    It cannot tell the mistake from a function routed raw on purpose whose ``@template``
    wrapper is used elsewhere (called from another view, or routed on another app): list such
    functions in ``ignore``."""
    if not hasattr(app, 'routes'):
        msg = f'check_views() takes a Starlette application or router, not {type(app).__name__}'
        raise TypeError(msg)
    _starlette.check_views(app, ignore)


def _sync() -> None:
    # Called by `_settings.install`: the `template_path` module state.
    global template_path
    template_path = _starlette.template_path()


_settings.listeners[__name__] = _sync
_sync()  # configured before this module was imported: the global already says so


def render(template_file: str, **template_data: Any) -> str:
    """Renders a template (path relative to the template folder) to a ``str``.

    Raises ``chameleon_rs.web.TemplateError`` if nothing is configured.
    """
    return _starlette.render(template_file, template_data)


async def render_async(template_file: str, **template_data: Any) -> str:
    """``render`` as a coroutine: the template's ``render_async``, which awaits awaitable
    values where the template uses them (see ``PageTemplate.render_async``)."""
    return await _starlette.render_async(template_file, template_data)


def response(
    template_file: str, content_type: str = 'text/html', status_code: int = 200, **template_data: Any
) -> starlette.responses.Response:
    """Renders a template to an ``HTMLResponse`` (every keyword but ``content_type`` and
    ``status_code`` is the model)."""
    return _starlette.make_response(render(template_file, **template_data), content_type, status_code)


async def response_async(
    template_file: str, content_type: str = 'text/html', status_code: int = 200, **template_data: Any
) -> starlette.responses.Response:
    """``response`` as a coroutine, rendering with ``render_async``."""
    html = await render_async(template_file, **template_data)
    return _starlette.make_response(html, content_type, status_code)


class TemplateDecorator(Protocol):
    """What ``@template(...)`` returns: decorates an endpoint, sync or async, into one with the
    same parameters that returns a Starlette ``Response``."""

    @overload
    def __call__[**P](
        self, f: Callable[P, Coroutine[Any, Any, Any]], /
    ) -> Callable[P, Coroutine[Any, Any, starlette.responses.Response]]: ...
    @overload
    def __call__[**P](self, f: Callable[P, Any], /) -> Callable[P, starlette.responses.Response]: ...


@overload
def template[**P](
    template_file: Callable[P, Coroutine[Any, Any, Any]], /
) -> Callable[P, Coroutine[Any, Any, starlette.responses.Response]]: ...
@overload
def template[**P](template_file: Callable[P, Any], /) -> Callable[P, starlette.responses.Response]: ...
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
    """Decorates a Starlette endpoint (a function, or a method of an ``HTTPEndpoint`` class;
    sync or async) to render a template with what it returns: bare (``@template``),
    ``@template()`` or ``@template('home/index.pt', content_type=..., status_code=...)``. The
    template also gets ``request`` and ``url_for``. See the module docs.

    ``concurrent=True`` (async endpoints): native partials called with an awaitable argument
    render concurrently (``None``, the default, keeps the template's setting).
    ``offload=True`` (async endpoints, experimental): the page renders in a worker thread.

    Typing: the decorated endpoint keeps its parameters and is typed as returning a Starlette
    ``Response`` (a coroutine of one for an ``async def`` endpoint)."""
    wrapped_function = None
    if callable(template_file):
        wrapped_function = template_file
        template_file = None
    content_type = mimetype or content_type

    def response_inner(f: Callable[..., Any]) -> Callable[..., Any]:
        return _starlette.decorate(f, template_file, content_type, status_code, concurrent, offload)

    return response_inner(wrapped_function) if wrapped_function else response_inner


def not_found(template_file: str = 'errors/404.pt', **template_data: Any) -> NoReturn:
    """Ends the current ``@template`` endpoint with a 404 page: ``template_file`` rendered with
    ``template_data``, as ``text/html``.

    Raises ``chameleon_rs.web.NotFound``, which the decorator turns into the response."""
    raise NotFound('The URL resulted in a 404 response.', template_file, template_data or None)


def error(template_file: str, status_code: int = 500, **template_data: Any) -> NoReturn:
    """Ends the current ``@template`` endpoint with an error page: ``template_file`` rendered
    with ``template_data`` and ``status_code``, as ``text/html``.

    Raises ``chameleon_rs.web.ErrorPage``, which the decorator turns into the response."""
    raise ErrorPage(template_file, status_code, 'The URL resulted in an error.', template_data or None)


def generic_error(template_file: str, status_code: int, template_data: Mapping[str, Any] | None = None) -> NoReturn:
    """fastapi-chameleon's name and signature for ``error()``."""
    raise ErrorPage(template_file, status_code, 'The URL resulted in an error.', template_data)
