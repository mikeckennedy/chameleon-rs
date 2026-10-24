# Parts ported from chameleon-flask (https://github.com/mikeckennedy/chameleon-flask at 326644f:
# chameleon_flask/engine.py) and fastapi-chameleon (https://github.com/mikeckennedy/fastapi-chameleon
# at 4d070f2: fastapi_chameleon/engine.py), modified: one core shared by the framework modules,
# rendering async views with `render_async`, and the decorator-order check.
"""The framework integrations' shared core (private).

Everything a ``@template`` view does that does not depend on the framework lives here: the
template name of a bare ``@template`` (chameleon-flask's and fastapi-chameleon's rule), the
sync and async view wrappers (an ``async def`` view renders with ``render_async``, a sync one
with ``render``), the error pages (``chameleon_rs.web.ErrorPage`` / ``NotFound``), response
pass-through and model conversion (``chameleon_rs.web.model_of``), and the check that no view
was registered with its router before ``@template`` wrapped it.

A framework module (``chameleon_rs.flask``, ``chameleon_rs.starlette``, ``chameleon_rs.fastapi``,
which builds on Starlette's, ``chameleon_rs.robyn``, ``chameleon_rs.litestar``) supplies an ``Integration``: how to recognise its response objects,
how to build an HTML response, where its settings live, what it adds to a view's model, how to
list an app's routes and how to run a check when the app starts.
Imports only the standard library (the framework modules import their framework).
"""

from __future__ import annotations

import inspect
import os
import stat
import sys
import weakref
from collections.abc import Callable, Iterable, Iterator, Mapping
from functools import wraps
from typing import Any

from chameleon_rs import _settings
from chameleon_rs.template import _render_async
from chameleon_rs.web import DecoratorOrderError, ErrorPage, TemplateError, model_of


class Integration:
    """A framework's part of the integration; the framework module subclasses it once."""

    # The settings slot (`_settings.VIEW_SLOTS`) and the module whose `template_path` global
    # keeps the original library's module state.
    name: str = ''
    module: str = ''
    # The integration's base exception (`FlaskChameleonException`, ...): its messages are the
    # original library's.
    error: type[TemplateError] = TemplateError
    # As the "expected a dict or ... as the return value" message names the response type.
    response_name: str = ''
    # As the decorator-order message names the route decorators.
    route_decorators: str = ''

    def settings(self) -> _settings.Settings | None:
        """The configuration for the current render (the process-wide one by default)."""
        return _settings.views[self.name]

    def is_response(self, value: Any) -> bool:
        """Whether a view returned a response of its own (passed through unchanged)."""
        raise NotImplementedError

    def make_response(self, html: str, content_type: str, status_code: int) -> Any:
        """The framework's response for a rendered page."""
        raise NotImplementedError

    def view_context(self, args: tuple[Any, ...]) -> Mapping[str, Any] | None:
        """Template variables the framework adds to a view's model, from the view's arguments
        (Starlette: ``request`` and ``url_for``); the model's own keys win. ``None``: none."""
        return None

    def routes(self, app: Any) -> Iterable[tuple[Any, str]]:
        """Every view the app's routes call, with a description of the route (``GET /path``),
        sub-routers, blueprints and mounted apps included."""
        raise NotImplementedError

    def watch(self, app: Any, check: Callable[[], None]) -> None:
        """Arranges for ``check`` to run when the app starts, before it handles a request."""
        raise NotImplementedError

    # --- Settings ---------------------------------------------------------------------------

    def global_init(
        self, template_folder: str, auto_reload: bool, cache_init: bool, restricted_namespace: bool
    ) -> None:
        """The original libraries' setup call: this integration's slot only, a second call
        ignored with ``cache_init``."""
        if _settings.views[self.name] is not None and cache_init:
            return
        _settings.check_folder(template_folder, self.error)
        settings = _settings.Settings(str(template_folder), bool(auto_reload), bool(restricted_namespace))
        _settings.install(views_settings={self.name: settings})

    def clear(self) -> None:
        _settings.install(views_settings={self.name: None})

    def template_path(self) -> str | None:
        """The legacy module global (``None`` when not configured)."""
        current = _settings.views[self.name]
        return None if current is None else current.template_folder

    # --- Rendering --------------------------------------------------------------------------

    def page(self, template_file: str) -> Any:
        settings = self.settings()
        if settings is None:
            msg = 'You must call global_init() before rendering templates.'
            raise self.error(msg)
        loader = settings.loader
        # The hot path: a name seen before costs two dict lookups and the loader's own, no stat.
        # Only names that passed the `strict_names` check are remembered, and each loader
        # belongs to one `Settings`, so a hit needs no check.
        names = loader.__dict__.get('_rs_names')
        if names is not None and type(template_file) is str:
            spec = names.get(template_file)
            if spec is not None:
                return loader[spec]
        if settings.strict_names and outside_folder(template_file):
            # The loader's own "not found" (a plain `ValueError`, as for a missing file), so
            # code that catches that keeps working.
            name = template_file.strip() if isinstance(template_file, str) else template_file
            msg = f'Template not found: {name}.'
            raise ValueError(msg)
        return loader[view_spelling(loader, template_file)]

    def render(self, template_file: str, template_data: Mapping[str, Any]) -> str:
        return self.page(template_file).render(encoding='utf-8', **template_data)  # type: ignore[no-any-return]

    async def render_async(self, template_file: str, template_data: Mapping[str, Any]) -> str:
        return await self.page(template_file).render_async(encoding='utf-8', **template_data)  # type: ignore[no-any-return]

    def model(self, value: Any, extra: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        model = model_of(value)
        if model is None:
            msg = f'Invalid return type {type(value)}, we expected a dict or {self.response_name} as the return value.'
            raise self.error(msg)
        if extra:
            return {**extra, **model}
        return model

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
        html = self.render(template_file, self.model(value, extra))
        return self.make_response(html, content_type, status_code)

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
        model = dict(self.model(value, extra))
        page = self.page(template_file)
        if concurrent is None:
            concurrent = page.concurrent
        html = await _render_async(page, 'utf-8', model, bool(concurrent), offload)
        return self.make_response(html, content_type, status_code)

    def error_response(self, page: ErrorPage, extra: Mapping[str, Any] | None = None) -> Any:
        # As the original libraries' error paths: text/html, the page's status, its data as the
        # model (with the framework's `view_context`, whose keys the data's override).
        html = self.render(page.template_file, _error_model(page, extra))
        return self.make_response(html, 'text/html', page.status_code)

    async def error_response_async(self, page: ErrorPage, extra: Mapping[str, Any] | None = None) -> Any:
        html = await self.render_async(page.template_file, _error_model(page, extra))
        return self.make_response(html, 'text/html', page.status_code)

    # --- The decorator ----------------------------------------------------------------------

    def view_template(self, f: Callable[..., Any], template_file: str | None) -> str:
        """The template a view renders: ``template_file``, or for a bare ``@template``
        ``{last module name}/{function name}.html``, or ``.pt`` when there is no ``.html`` file
        in the template folder (checked once, when the view is decorated)."""
        # The framework module's `template_path` global, set to the default as the original
        # libraries did.
        state = vars(sys.modules[self.module])
        if not state.get('template_path'):
            state['template_path'] = 'templates'
        if isinstance(template_file, str) and template_file:
            return template_file
        module_name = f.__module__
        if '.' in module_name:
            module_name = module_name.split('.')[-1]
        view = f.__name__  # ty: ignore[unresolved-attribute] - views are functions
        name = f'{module_name}/{view}.html'
        if not os.path.exists(os.path.join(state['template_path'], name)):
            name = f'{module_name}/{view}.pt'
        return name

    def decorate(
        self,
        f: Callable[..., Any],
        template_file: str | None,
        content_type: str,
        status_code: int,
        concurrent: bool | None = None,
        offload: bool = False,
    ) -> Callable[..., Any]:
        """Wraps a view (sync or async) to render its template with what it returns."""
        view_template = self.view_template(f, template_file)

        @wraps(f)
        def sync_view_method(*args: Any, **kwargs: Any) -> Any:
            try:
                value = f(*args, **kwargs)
                return self.render_response(view_template, value, content_type, status_code, self.view_context(args))
            except ErrorPage as page:
                return self.error_response(page, self.view_context(args))

        @wraps(f)
        async def async_view_method(*args: Any, **kwargs: Any) -> Any:
            try:
                value = await f(*args, **kwargs)
                return await self.render_response_async(
                    view_template, value, content_type, status_code, concurrent, offload, self.view_context(args)
                )
            except ErrorPage as page:
                return await self.error_response_async(page, self.view_context(args))

        wrapper = async_view_method if inspect.iscoroutinefunction(f) else sync_view_method
        _remember(f, wrapper)
        return wrapper

    # --- The decorator-order check ----------------------------------------------------------

    def check_views(self, app: Any, ignore: Iterable[Any] = ()) -> None:
        """Raises ``DecoratorOrderError`` naming each view of ``app`` that a route registered
        before ``@template`` wrapped it. ``ignore``: views not to flag (the function a route
        calls, or its ``@template`` wrapper)."""
        problems = misordered(self.routes(app), ignore)
        if not problems:
            return
        lines = [
            f'@template must be placed below the route decorator ({self.route_decorators}) on '
            f'{_describe(view)} (route {where}): the route was registered with the undecorated view, '
            'so its template never renders. Put the route decorator first (outermost) and @template under it.'
            for view, where in problems
        ]
        raise DecoratorOrderError('\n'.join(lines))

    def check_app(self, app: Any) -> None:
        """Raises ``TypeError`` unless ``app`` is an application this integration can watch."""
        raise NotImplementedError

    def watch_app(self, app: Any) -> None:
        """Runs ``check_views(app)`` when the app starts (once per app, however often this is
        called; once the check passed, it does nothing)."""
        self.check_app(app)
        if app in _watched:
            return
        _watched[app] = True
        app_ref = weakref.ref(app)
        passed = False

        def check() -> None:
            nonlocal passed
            if passed:
                return
            current = app_ref()
            if current is not None:
                self.check_views(current)
            passed = True

        self.watch(app, check)


def outside_folder(name: object) -> bool:
    """Whether a template name could load a file outside the template folder, as Jinja's
    ``FileSystemLoader`` refuses (its ``split_template_path``), and more: empty, an absolute
    path, a ``..`` segment, a backslash, a drive letter or a package spec (``:``), a NUL. The
    guard of the Jinja-shaped APIs (``chameleon_rs.starlette.Templates``,
    ``chameleon_rs.litestar.ChameleonEngine``); the decorator APIs keep the original
    libraries' lookup (template names come from code) unless ``strict_names`` is on. Not
    stripped: a name with surrounding whitespace is refused (Jinja looks for that exact file; Chameleon's loader would strip it)."""
    spec = name if isinstance(name, str) else ''
    return (
        not spec
        or spec != spec.strip()
        or spec.startswith('/')
        or os.path.isabs(spec)
        or '\\' in spec
        or ':' in spec
        or '\0' in spec
        or any(piece == '..' for piece in spec.split('/'))
    )


def canonical(name: str) -> str:
    """``name`` without ``.`` and empty segments (``./a//b.pt`` is ``a/b.pt``), as Jinja's
    ``split_template_path`` drops them: one cached template per file, however a request spells
    its name (each spelling would otherwise compile and keep its own copy)."""
    return '/'.join(piece for piece in name.split('/') if piece not in {'', '.'})


def spelling(loader: Any, name: str) -> str | None:
    """The name to load ``name`` by, or ``None`` if no folder of ``loader`` has a regular file
    of that name (a directory is not a template). Every spelling of one file (``./a//b.pt``,
    and on a case-insensitive filesystem ``A.PT``) loads by the first one seen, so the loader's
    cache holds one template per file, however requests spell it. The file is found as the
    loader finds it: its ``default_extension`` added to a name without a dot, its folders in
    order, an absolute path as it is (``..`` segments are kept: the filesystem resolves them).
    A package spec (``pkg:path``) is the caller's to leave to the loader."""
    absolute = os.path.isabs(name)
    if not absolute:
        name = canonical(name)
    extension = getattr(loader, 'default_extension', None)
    filename = name + extension if extension is not None and '.' not in name else name
    for folder in [''] if absolute else loader.search_path:
        try:
            info = os.stat(os.path.join(folder, filename) if folder else filename)
        except (OSError, ValueError):
            continue
        if not stat.S_ISREG(info.st_mode):
            return None
        spellings = loader.__dict__.setdefault('_rs_spellings', {})
        return spellings.setdefault((info.st_dev, info.st_ino), name)  # type: ignore[no-any-return]
    return None


# At most this many names are remembered per loader by `view_spelling` (then they start over):
# names built from request data must not grow it without bound. As `partials._CACHE_LIMIT`.
_NAMES_LIMIT = 1024


def view_spelling(loader: Any, name: Any) -> Any:
    """The name ``Integration.page`` loads ``name`` by: its file's one spelling (``spelling``),
    so ``./a.pt``, ``a.pt`` and an absolute path to the same file share one compiled template.
    Anything that is not a regular file found that way (a missing file, a package spec, a
    directory, not a string) is passed on as given, so the loader resolves it, or raises, as
    it always did. Only names that reached a file are remembered (``_rs_names`` on the loader,
    bounded by ``_NAMES_LIMIT``), so names of missing files never grow it."""
    if not isinstance(name, str):
        return name
    # As the loader strips its spec.
    spec = name.strip()
    if not spec or '\0' in spec or (':' in spec and not os.path.isabs(spec)):
        return name
    found = spelling(loader, spec)
    if found is None:
        return name
    names = loader.__dict__.setdefault('_rs_names', {})
    if len(names) >= _NAMES_LIMIT:
        names.clear()
    names[name] = found
    return found


def is_not_found(exc: ValueError) -> bool:
    """Whether ``exc`` is the loader's "no such template" (``chameleon_rs.loader``'s
    ``Template not found: {spec}.``), not an error from compiling the template."""
    return type(exc) is ValueError and str(exc).startswith('Template not found: ')


def _error_model(page: ErrorPage, extra: Mapping[str, Any] | None) -> Mapping[str, Any]:
    data = page.template_data or {}
    return {**extra, **data} if extra else data


# --- Views wrapped by @template, for the decorator-order check ------------------------------

# Every function @template wrapped, with the functions it wraps in turn (`__wrapped__`), and
# the wrappers @template made. Weak: views defined in tests come and go. Membership is by
# equality, so a bound method (a new object each time `obj.method` is read) matches.
# Callables that cannot be weakly referenced (instances of classes without `__weakref__`) are
# kept by identity in `_strong_originals`.
_originals: weakref.WeakSet[Any] = weakref.WeakSet()
_wrappers: weakref.WeakSet[Any] = weakref.WeakSet()
_strong_originals: dict[int, Any] = {}
# The apps whose start is watched (`watch_app`), so watching twice does nothing.
_watched: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()


def _chain(f: Any) -> Iterator[Any]:
    """``f`` and what it wraps (``__wrapped__``), in order."""
    seen: set[int] = set()
    while f is not None and id(f) not in seen and len(seen) < 100:
        seen.add(id(f))
        yield f
        f = getattr(f, '__wrapped__', None)


def _key(f: Any) -> Any:
    """What identifies a view: a bound method by its function and instance (reading
    ``obj.method`` makes a new, equal object each time), anything else by identity."""
    func = getattr(f, '__func__', None)
    owner = getattr(f, '__self__', None)
    if func is not None and owner is not None:
        return ('method', id(func), id(owner))
    return id(f)


def _remember(f: Any, wrapper: Any) -> None:
    for g in _chain(f):
        try:
            _originals.add(g)
        except TypeError:
            _strong_originals[id(g)] = g
    _wrappers.add(wrapper)


def _is_original(f: Any) -> bool:
    if _strong_originals.get(id(f)) is f:
        return True
    try:
        return f in _originals
    except TypeError:
        return False


def _is_wrapper(f: Any) -> bool:
    try:
        return f in _wrappers
    except TypeError:
        return False


def misordered(routes: Iterable[tuple[Any, str]], ignore: Iterable[Any] = ()) -> list[tuple[Any, str]]:
    """The routes whose view is a function ``@template`` wrapped after the route registered it:
    a route calls such a function itself, not through a ``@template`` wrapper, and no route of
    the app calls the wrapper (a view registered raw on purpose and also decorated, registered
    again, is not flagged). Views in ``ignore`` (or wrapped by a wrapper in it) are not flagged."""
    routes = list(routes)
    covered: set[Any] = set()
    for view in ignore:
        covered.update(_key(h) for h in _chain(view))
    for endpoint, _where in routes:
        for g in _chain(endpoint):
            if _is_wrapper(g):
                covered.update(_key(h) for h in _chain(getattr(g, '__wrapped__', None)))
                break
    problems = []
    for endpoint, where in routes:
        for g in _chain(endpoint):
            if _is_wrapper(g):
                break
            if _is_original(g) and _key(g) not in covered:
                problems.append((g, where))
                break
    return problems


def _describe(view: Any) -> str:
    """``detail()`` with where it is defined."""
    name = getattr(view, '__qualname__', None) or getattr(view, '__name__', None) or repr(view)
    code = getattr(view, '__code__', None)
    if code is None:
        return f'{name}()'
    return f'{name}() at {code.co_filename}:{code.co_firstlineno}'
