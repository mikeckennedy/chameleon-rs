# Ported from chameleon_partials (https://github.com/mikeckennedy/chameleon_partials,
# chameleon_partials/__init__.py at f54ca1a: `register_extensions`, `HTML`, `render_partial`,
# `extend_model`, `PartialsException`), modified: built into chameleon-rs, `render_partial`
# is in every template without being passed in the model and renders as a native frame of the
# caller's render; paths not in the registered folder also resolve against the calling
# template's folders (unless `partial_search='registered'`).
"""Built-in partials (D10): ``render_partial(path, **kwargs)`` in any template renders another
template file with ``kwargs`` as its model, straight into the page.

A path is looked for in the folder given to ``chameleon_rs.configure(template_folder=...)`` (or
to the alias ``register_extensions``), as chameleon_partials does; then, by default, on the
calling template's search path (the folders of the ``PageTemplateLoader`` that loaded it), then
in its own folder and each folder above it, up to the configured folder (partials' or a framework
integration's, whatever it is named) or search path entry that holds it (a template in neither: up to the nearest ``templates`` folder of a Python
package, as a Pyramid app has, else its own folder only). These searches never leave the
folder searched: a ``..`` or absolute path out of it is not found. Nor, in the registered
folder, is a package spec (``pkg:path``), as chameleon_partials refuses from 0.2.1. Found nowhere is
``ValueError('Template not found: <path>.')``. With ``chameleon_rs.configure(
partial_search='registered')`` only the registered folder is searched, exactly as
chameleon_partials (and with no folder registered a call raises ``PartialsException``): the
default differs only where chameleon_partials raises. A renderer with no template (the global
``chameleon_rs.render_partial``) always uses the registered folder only. Resolutions are cached
until the configuration changes.

The partial sees only its keyword arguments and ``render_partial`` (chameleon_partials'
semantics), and is rendered as with ``encoding='utf-8'``. Called from Python,
``render_partial(path, **kwargs)`` returns an ``HTML`` object (its ``__html__`` gives the
markup, so a template inserts it unescaped). This module is also chameleon_partials' API: with ``chameleon_rs.compat.install()``,
``import chameleon_partials`` gives this module, and ``register_extensions``,
``extend_model``, ``render_partial``, ``HTML`` and ``PartialsException`` keep working. A
``chameleon_partials.render_partial`` from the original package, passed in a model, also
renders natively.
"""

from __future__ import annotations

import os
import types
import weakref
from typing import Any

from chameleon_rs import _settings
from chameleon_rs.web import TemplateError

__all__ = [
    'HTML',
    'PartialRenderer',
    'PartialsException',
    'clear',
    'extend_model',
    'register_extensions',
    'render_partial',
]

# chameleon_partials' module state: true once a template folder is configured. Code written
# for chameleon_partials resets it (its test suite does), so it is read on every call.
has_registered_extensions: bool = False
template_path: str | None = None


class PartialsException(TemplateError):
    """Raised when partials are configured or used incorrectly: registering a folder that does
    not exist, or rendering a partial from Python before any folder is configured. Errors from
    the templates themselves (``ValueError`` for a missing file) propagate unchanged."""


class HTML:
    """Rendered markup, as ``render_partial`` returns it from Python: ``__html__`` gives the
    text (``html_text``), so a template inserts it without escaping."""

    __slots__ = ('html_text',)

    def __init__(self, html_text: str) -> None:
        self.html_text: str = html_text

    def __html__(self) -> str:
        return self.html_text


class PartialRenderer:
    """The ``render_partial`` callable: ``chameleon_rs.render_partial`` (no calling template)
    or a template's own (its ``render_partial`` builtin, the calling template for
    ``partial_search='caller'``). Written as a whole ``${render_partial(...)}``
    expression it is not called at all: the partial renders as a native frame."""

    __slots__ = ('__weakref__', '_template')

    def __init__(self, template: Any = None) -> None:
        self._template = None if template is None else weakref.ref(template)

    @property
    def __name__(self) -> str:  # type: ignore[override]
        # As chameleon_partials' function is named (render errors show it).
        return 'render_partial'

    @property
    def template(self) -> Any:
        """The template this ``render_partial`` resolves relative to (``None``: none)."""
        return None if self._template is None else self._template()

    def __call__(self, template_file: str, **template_data: Any) -> HTML:
        template = resolve(self.template, template_file)
        if 'render_partial' not in template_data:
            template_data['render_partial'] = getattr(template, '_partial_renderer', self)
        return HTML(template.render(encoding='utf-8', **template_data))

    def __repr__(self) -> str:
        template = self.template
        return '<render_partial>' if template is None else f'<render_partial of {template!r}>'


# A render error lists its arguments as `<{type name} {__name__!r} at ...>`: chameleon_partials'
# `render_partial` is a function, so the type is named as functions are (as `_native` names
# `Scope`); `__qualname__` stays `PartialRenderer`.
# (The class's own name: the `__name__` property above is the instances'.)
PartialRenderer.__name__ = 'function'  # ty: ignore[invalid-assignment]

render_partial = PartialRenderer()


def register_extensions(template_folder: str, auto_reload: bool = False, cache_init: bool = True) -> None:
    """chameleon_partials' setup call: configures the folder ``render_partial`` uses (only
    that; ``chameleon_rs.configure`` also configures the framework integrations). A second call
    is ignored unless ``cache_init`` is false. Raises ``PartialsException`` for a missing
    folder."""
    if has_registered_extensions and cache_init:
        return
    _settings.check_folder(template_folder, PartialsException)
    _settings.install(partials_settings=_settings.Settings(str(template_folder), bool(auto_reload), True))


def extend_model(model: dict[str, Any] | None) -> dict[str, Any]:
    """Adds ``render_partial`` to a view model (chameleon_partials' helper; not needed with the
    built-in, which every template has). Returns the model (a new dict for ``None``)."""
    if model is None:
        model = {}
    if not isinstance(model, dict):
        msg = 'The model must be a dictionary.'
        raise PartialsException(msg)
    model['render_partial'] = render_partial
    return model


def clear() -> None:
    """Forgets the folder partials use, for tests."""
    _settings.install(partials_settings=None)


def _configured(settings: _settings.Settings | None) -> None:
    # Called by `_settings.install`.
    global has_registered_extensions, template_path
    has_registered_extensions = settings is not None
    template_path = None if settings is None else settings.template_folder


# --- Resolution -------------------------------------------------------------------------


def resolve(caller: Any, template_file: str) -> Any:
    """The template ``render_partial(template_file)`` names when called from ``caller`` (a
    template, or ``None`` from Python). Cached per caller and path until the configuration
    changes."""
    search = caller is not None and _settings.search_mode == 'caller'
    if not has_registered_extensions and not search:
        msg = 'You must call register_extensions() before this function can be used.'
        raise PartialsException(msg)
    # The flag too: code written for chameleon_partials resets it by hand.
    generation = (_settings.generation, has_registered_extensions, search)
    cache = None if caller is None else caller.__dict__.get('_partial_cache')
    if cache is None:
        cache = _GLOBAL_CACHE if caller is None else caller.__dict__.setdefault('_partial_cache', {})
    key = template_file
    hit = cache.get(key)
    if hit is not None and hit[0] == generation:
        return hit[1]
    template = _find(caller, template_file)
    cache[key] = (generation, template)
    return template


_GLOBAL_CACHE: dict[str, tuple[tuple[int, bool, bool], Any]] = {}

# Templates found on a caller's search path or folders (`partial_search='caller'`), shared by
# every caller that finds the same file with the same template class and (hashable) settings.
_SIBLINGS: dict[Any, Any] = {}


def _find(caller: Any, template_file: str) -> Any:
    spec = template_file.strip()
    settings = _settings.partials if has_registered_extensions else None
    search = caller is not None and _settings.search_mode == 'caller'
    if settings is not None:
        try:
            if not _in_folder(spec, settings.template_folder):
                raise _not_found(spec)
            return settings.loader.load(spec)
        except ValueError:
            if not search:
                raise
    for folder, root in _folders(caller, settings):
        path = os.path.normpath(os.path.join(folder, spec))
        # Never outside the root the folder belongs to: no `..` or absolute path out of it.
        if _inside(path, root) and os.path.isfile(path):
            return _sibling(caller, path)
    raise _not_found(spec)


def _not_found(spec: str) -> ValueError:
    # The loader's own error for a missing file.
    return ValueError(f'Template not found: {spec}.')


def _in_folder(spec: str, folder: str) -> bool:
    """Whether ``spec`` names a file inside ``folder``: not absolute, not a package spec
    (``pkg:path``, which the loader would read from that package), and no ``..`` out of it
    (one that stays inside is fine). Lexical, so a symlink inside the folder is followed."""
    if not spec or os.path.isabs(spec) or ':' in spec or '\0' in spec:
        return False
    root = os.path.abspath(folder)
    return _inside(os.path.normpath(os.path.join(root, spec)), root)


def _folders(caller: Any, settings: _settings.Settings | None) -> list[tuple[str, str]]:
    """``(folder, root)`` pairs: the caller's search path (without its own folder, which comes
    next), each its own root; then its folder and each one above it, up to the outermost root
    (a configured folder, partials' or an integration's, or a search path entry) that holds it. A caller in no root climbs
    to its package's ``templates`` folder at most (``_package_templates``): the walk never
    leaves the template tree."""
    paths = [
        os.path.abspath(p) for p in getattr(caller, '_partial_paths', ()) if isinstance(p, str) and not _is_package(p)
    ]
    folders = [(p, p) for p in paths]
    filename = getattr(caller, 'filename', None)
    if isinstance(filename, str) and os.path.isabs(filename) and getattr(caller, 'package_name', None) is None:
        here = os.path.dirname(os.path.normpath(filename))
        roots = [*paths, *_configured_folders(settings)]
        holding = [r for r in roots if _inside(here, r)]
        root = min(holding, key=len) if holding else _package_templates(here)
        while True:
            folders.append((here, root))
            if here == root:
                break
            here = os.path.dirname(here)
    return folders


def _configured_folders(settings: _settings.Settings | None) -> list[str]:
    # Every folder configured, whatever it is named: partials' own and each integration's
    # (some setups fill only their own slot).
    slots = [settings, _settings.partials, *_settings.views.values()]
    return [os.path.abspath(s.template_folder) for s in slots if s is not None]


def _package_templates(here: str) -> str:
    """The root for a caller in no configured root (a Pyramid renderer, say): the nearest
    folder named ``templates`` at or above ``here`` whose parent is a Python package (has an
    ``__init__.py``), so the walk stays in the app; otherwise ``here`` (its own folder only)."""
    folder = here
    while True:
        parent = os.path.dirname(folder)
        if os.path.basename(folder) == 'templates':
            return folder if os.path.isfile(os.path.join(parent, '__init__.py')) else here
        if parent == folder or os.path.isfile(os.path.join(folder, '__init__.py')):
            # The filesystem root, or the package itself, passed with no `templates` folder.
            return here
        folder = parent


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        # Different drives (Windows), or one path relative.
        return False


def _is_package(path: str) -> bool:
    # `package:folder` entries (package resources) are not searched for partials.
    return not os.path.isabs(path) and ':' in path


def _sibling(caller: Any, path: str) -> Any:
    cls = type(caller)
    config = getattr(caller, '_partial_config', {})
    paths = getattr(caller, '_partial_paths', [])
    try:
        key: Any = (path, cls, tuple(paths), tuple(sorted(config.items())))
        hash(key)
    except TypeError:
        # Unhashable settings: not shared (the caller's own cache keeps it).
        return cls(path, search_path=list(paths), **config)
    template = _SIBLINGS.get(key)
    if template is None:
        template = _SIBLINGS.setdefault(key, cls(path, search_path=list(paths), **config))
    return template


# --- Native frames (called by the extension, once per call site or template per render) ---


def _target(func: Any, template_file: Any) -> Any:
    """The template a native ``render_partial`` frame renders for ``func(template_file, ...)``,
    or ``None`` when ``func`` must be called as Python. A renderer resolves as calling it would
    (relative to its own template, not the caller: the global one, passed in a model, has none)."""
    if type(func) is PartialRenderer:
        return resolve(func.template, template_file)
    return _legacy_target(func, template_file)


def _legacy_target(func: Any, template_file: Any) -> Any:
    # chameleon_partials' own function (the original package, rendering through the compat
    # alias): its module state, read as the function itself reads it.
    if not isinstance(func, types.FunctionType) or func.__module__ != 'chameleon_partials':
        return None
    if func.__name__ != 'render_partial':
        return None
    module = func.__globals__
    if not module.get('has_registered_extensions'):
        return None
    loader = module.get('__templates')
    if loader is None:
        return None
    folder = module.get('template_path')
    if not isinstance(template_file, str) or not isinstance(folder, str):
        return None
    if not _in_folder(template_file.strip(), folder):
        # A name outside its folder: called as Python, so the function decides (it refuses
        # one from 0.2.1 on), never loaded here around its check.
        return None
    return loader[template_file]


def _prepare(template: Any) -> tuple[Any, Any, Any] | None:
    """What ``template.render(encoding='utf-8', ...)`` would use, in its order (defaults, then
    ``cook_check()``): ``(program, builtins, defaults)``, or ``None`` when the template cannot
    render as a native frame (another engine's, a text template, a ``_render`` function)."""
    from chameleon_rs.template import PageTemplate, PageTextTemplateFile

    if not isinstance(template, PageTemplate) or isinstance(template, PageTextTemplateFile):
        return None
    defaults = template._encoded_defaults('utf-8', None)
    template.cook_check()
    if '_render' in template.__dict__ or type(template).render is not PageTemplate.render:
        return None
    program, builtins = template._compiled
    return program, builtins, defaults
