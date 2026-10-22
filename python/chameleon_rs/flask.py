# Ported from chameleon-flask (https://github.com/mikeckennedy/chameleon-flask at 326644f:
# chameleon_flask/engine.py and exceptions.py), modified: built into chameleon-rs, sharing
# `chameleon_rs.configure()`, the `chameleon_rs.web` exceptions and model conversion, with
# per-app configuration and `error()`.
"""Flask and Quart integration: render chameleon-rs templates from views.

Importing this module imports Flask (it is not imported by ``import chameleon_rs``). The API is
chameleon-flask's, so moving over is an import change (or none: ``chameleon_rs.compat.install()``
makes ``import chameleon_flask`` give this module)::

    import chameleon_rs.flask as chameleon_flask

    chameleon_flask.configure(template_folder='templates', auto_reload=dev_mode)

    @app.get('/')
    @chameleon_flask.template('home/index.pt')
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

import inspect
import os
import sys
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any, NoReturn

import flask
import werkzeug.sansio.response

from chameleon_rs import _settings
from chameleon_rs.template import _render_async
from chameleon_rs.web import ErrorPage, NotFound, TemplateError, model_of

__all__ = [
    'FlaskChameleonException',
    'FlaskChameleonGenericException',
    'FlaskChameleonNotFoundException',
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


def configure(
    template_folder: Any = None,
    *,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
    app: Any = None,
) -> None:
    """Configures where templates live and how they load: for the whole process (the same as
    ``chameleon_rs.configure``: for views and for ``render_partial``), or, with ``app``, for that
    Flask app's views only (stored in ``app.extensions['chameleon_rs']``). Settings left out keep
    their current values.

    Raises ``FlaskChameleonException`` if the folder is missing or not a directory.
    """
    if app is not None:
        base = app.extensions.get(EXTENSION) or _settings.flask
        app.extensions[EXTENSION] = _settings.build(
            template_folder, auto_reload, restricted_namespace, base=base, error=FlaskChameleonException
        )
        return
    settings = _settings.build(
        template_folder,
        auto_reload,
        restricted_namespace,
        base=_settings.flask or _settings.partials,
        error=FlaskChameleonException,
    )
    _settings.install(partials_settings=settings, flask_settings=settings)


def global_init(
    template_folder: str, auto_reload: bool = False, cache_init: bool = True, restricted_namespace: bool = True
) -> None:
    """chameleon-flask's setup call: configures the views' templates only (partials keep their
    own folder, from ``register_extensions``). With ``cache_init`` (the default) a call after
    the views' folder is configured does nothing."""
    if _settings.flask is not None and cache_init:
        return
    _settings.check_folder(template_folder, FlaskChameleonException)
    _settings.install(
        flask_settings=_settings.Settings(str(template_folder), bool(auto_reload), bool(restricted_namespace))
    )


def clear() -> None:
    """Forgets the views' process-wide configuration, so a later ``configure()`` /
    ``global_init()`` starts afresh. Mostly useful in tests."""
    _settings.install(flask_settings=None)


def _sync() -> None:
    # Called by `_settings.install`: chameleon-flask's `template_path` module state.
    global template_path
    current = _settings.flask
    template_path = None if current is None else current.template_folder


def _current_settings() -> _settings.Settings | None:
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
    return _settings.flask


def _page(template_file: str) -> Any:
    settings = _current_settings()
    if settings is None:
        msg = 'You must call global_init() before rendering templates.'
        raise FlaskChameleonException(msg)
    return settings.loader[template_file]


def render(template_file: str, **template_data: Any) -> str:
    """Renders a template (path relative to the template folder) to a ``str``.

    Raises ``FlaskChameleonException`` if nothing is configured.
    """
    return _page(template_file).render(encoding='utf-8', **template_data)  # type: ignore[no-any-return]


async def render_async(template_file: str, **template_data: Any) -> str:
    """``render`` as a coroutine: the template's ``render_async``, which awaits awaitable
    values where the template uses them (see ``PageTemplate.render_async``)."""
    return await _page(template_file).render_async(encoding='utf-8', **template_data)  # type: ignore[no-any-return]


def response(
    template_file: str,
    content_type: str = 'text/html',
    status_code: int = 200,
    **template_data: Any,
) -> flask.Response:
    """Renders a template to a ``flask.Response``. (Unlike ``@template``, ``mimetype`` is not a
    keyword here: as in chameleon-flask, every other keyword is the model.)"""
    html = render(template_file, **template_data)
    return flask.Response(response=html, content_type=content_type, status=status_code)


async def response_async(
    template_file: str,
    content_type: str = 'text/html',
    status_code: int = 200,
    **template_data: Any,
) -> flask.Response:
    """``response`` as a coroutine, rendering with ``render_async``."""
    html = await render_async(template_file, **template_data)
    return flask.Response(response=html, content_type=content_type, status=status_code)


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
    (``render_async(offload=True)``)."""
    wrapped_function = None
    if callable(template_file):
        wrapped_function = template_file
        template_file = None
    content_type = mimetype or content_type

    def response_inner(f: Callable[..., Any]) -> Callable[..., Any]:
        global template_path

        if not template_path:
            template_path = 'templates'

        if isinstance(template_file, str) and template_file:
            view_template: str = template_file
        else:
            # The default naming scheme: template_folder/module_name/function_name.html|.pt
            module = f.__module__
            if '.' in module:
                module = module.split('.')[-1]
            view = f.__name__
            view_template = f'{module}/{view}.html'

            if not os.path.exists(os.path.join(template_path, view_template)):
                view_template = f'{module}/{view}.pt'

        def build_response(response_val: Any) -> flask.Response:
            return _render_response(view_template, response_val, content_type, status_code)

        @wraps(f)
        def sync_view_method(*args: Any, **kwargs: Any) -> flask.Response:
            try:
                return build_response(f(*args, **kwargs))
            except ErrorPage as page:
                return _error_response(page)

        @wraps(f)
        async def async_view_method(*args: Any, **kwargs: Any) -> flask.Response:
            try:
                return await _render_response_async(
                    view_template, await f(*args, **kwargs), content_type, status_code, concurrent, offload
                )
            except ErrorPage as page:
                return await _error_response_async(page)

        if inspect.iscoroutinefunction(f):
            return async_view_method
        return sync_view_method

    return response_inner(wrapped_function) if wrapped_function else response_inner


def _is_response(response_val: Any) -> bool:
    # May be a Quart or bare werkzeug response; those types can't be named here without
    # importing Quart.
    return isinstance(response_val, werkzeug.sansio.response.Response) or str(type(response_val)) in response_classes


def _model(response_val: Any) -> Mapping[str, Any]:
    model = model_of(response_val)
    if model is None:
        msg = f'Invalid return type {type(response_val)}, we expected a dict or flask.Response as the return value.'
        raise FlaskChameleonException(msg)
    return model


def _render_response(
    template_file: str, response_val: Any, content_type: str, status_code: int = 200
) -> flask.Response:
    if _is_response(response_val):
        return response_val  # type: ignore[no-any-return]
    html = render(template_file, **_model(response_val))
    return flask.Response(response=html, content_type=content_type, status=status_code)


async def _render_response_async(
    template_file: str,
    response_val: Any,
    content_type: str,
    status_code: int = 200,
    concurrent: bool | None = None,
    offload: bool = False,
) -> flask.Response:
    if _is_response(response_val):
        return response_val  # type: ignore[no-any-return]
    model = dict(_model(response_val))
    page = _page(template_file)
    if concurrent is None:
        concurrent = page.concurrent
    html = await _render_async(page, 'utf-8', model, concurrent, offload)
    return flask.Response(response=html, content_type=content_type, status=status_code)


def _error_response(page: ErrorPage) -> flask.Response:
    # As chameleon-flask's 404 path: text/html, the page's status, its data as the model.
    html = render(page.template_file, **(page.template_data or {}))
    return flask.Response(response=html, content_type='text/html', status=page.status_code)


async def _error_response_async(page: ErrorPage) -> flask.Response:
    html = await render_async(page.template_file, **(page.template_data or {}))
    return flask.Response(response=html, content_type='text/html', status=page.status_code)


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
