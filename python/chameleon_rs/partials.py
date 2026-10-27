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
import posixpath
import stat
import types
import weakref
from collections.abc import Callable
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
    key = _spelling(template_file)
    hit = cache.get(key)
    if hit is not None and hit[0] == generation and (hit[2] is None or _same_file(hit[1], *hit[2])):
        return hit[1]
    template, path = _find(caller, template_file)
    if len(cache) >= _CACHE_LIMIT:
        # Spellings that reach one file (`A.pt`, `a.PT` on a case-insensitive filesystem) share
        # its template, but each would keep an entry here: bounded, for names from requests.
        cache.clear()
    # A template that reloads is checked against the files on each hit (`_same_file`).
    cache[key] = (generation, template, (path, _file_id(path)) if getattr(template, 'auto_reload', False) else None)
    return template


def _same_file(template: Any, path: str, identity: tuple[int, int] | None) -> bool:
    """Whether ``path`` (the file a name was found as) and ``template``'s own file are still the
    file ``identity`` they were. Hard links part when a deployment replaces one of them (a new
    file renamed over it): the name must not keep another name's template, nor the template
    read the other file."""
    filename = getattr(template, 'filename', None)
    return (
        identity is not None
        and _file_id(path) == identity
        # One `stat` when they are the same name, the usual case.
        and (not isinstance(filename, str) or filename == path or _file_id(filename) == identity)
    )


_CACHE_LIMIT = 1024
_GLOBAL_CACHE: dict[str, tuple[tuple[int, bool, bool], Any, tuple[str, tuple[int, int] | None] | None]] = {}

# Templates found on a caller's search path or folders (`partial_search='caller'`), shared by
# every caller that finds the same file with the same template class and (hashable) settings.
# Weak: a sibling lives as long as a caller's `_partial_cache` holds it. Its key holds plain
# settings by value and the class and other settings by `id` (`_setting_key`), never the
# objects themselves: a strong map kept every template ever configured (its `translate`
# function, say), a sibling for each file a deployment replaced, and, through a setting or a
# class attribute referring back to its caller, the caller too, for the life of the process.
# An unhashable setting (a dict, which can change after a sibling compiled with it) is not
# shared.
_SIBLINGS: weakref.WeakValueDictionary[Any, Any] = weakref.WeakValueDictionary()

# Settings a sibling's key holds by value; any other is held by `id`.
_PLAIN = (str, int, float, bool, bytes, type(None))
_BY_ID = object()


def _spelling(template_file: str) -> str:
    """The one spelling of a path its lookups and caches use: ``./a//b/../c.pt`` is ``a/c.pt``.
    Each spelling would otherwise compile and keep its own copy of the same file (a name built
    from request data could grow the caches without bound)."""
    spec = template_file.strip()
    return posixpath.normpath(spec) if spec else spec


def _find(caller: Any, template_file: str) -> tuple[Any, str]:
    """The template for ``template_file``, and the path it was found as."""
    spec = template_file.strip()
    name = _spelling(spec)
    settings = _settings.partials if has_registered_extensions else None
    search = caller is not None and _settings.search_mode == 'caller'
    if settings is not None:
        try:
            if not _in_folder(name, settings.template_folder):
                raise _not_found(spec)
            identity = _file_id(os.path.join(os.path.abspath(settings.template_folder), name))
            if identity is None:
                raise _not_found(spec)
            # One template per file, whatever the spelling: the first one seen loads it.
            folder = os.path.abspath(settings.template_folder)
            first = remembered_spelling(settings.loader, identity, name, lambda f: _file_id(os.path.join(folder, f)))
            return settings.loader.load(first), os.path.join(folder, name)
        except ValueError:
            if not search:
                raise
    for folder, root in _folders(caller, settings):
        path = os.path.normpath(os.path.join(folder, name))
        # Never outside the root the folder belongs to: no `..` or absolute path out of it.
        if _inside(path, root):
            identity = _file_id(path)
            if identity is not None:
                return _sibling(caller, path, identity), path
    raise _not_found(spec)


def _file_id(path: str) -> tuple[int, int] | None:
    """The file at ``path`` (device and inode), or ``None`` if there is no regular file: one
    identity for every spelling that opens it (case, Unicode form, `..` through a symlink)."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISREG(info.st_mode) else None


def remembered_spelling(owner: Any, identity: tuple[int, int], name: str, current: Callable[[str], Any]) -> str:
    """The first spelling seen of the file ``identity`` (``_rs_spellings`` on ``owner``), if it
    still names that file (``current(first)``); otherwise ``name``, remembered from now on. Two
    names of one file (hard links, or spellings on a case-insensitive filesystem) part when a
    deployment replaces one of them (a new file, atomically renamed over it): the other would
    otherwise keep loading the replacement. Each replaced file leaves its old identity behind:
    past ``_CACHE_LIMIT`` entries (or twice those left last time) the ones no file has any more
    are dropped. The others stay, as the templates the loader keeps for them do: forgetting them
    made each new spelling of a file compile it again."""
    spellings = owner.__dict__.setdefault('_rs_spellings', {})
    first = spellings.get(identity)
    if first == name:
        return name
    if first is None or current(first) != identity:
        if len(spellings) >= owner.__dict__.get('_rs_spellings_limit', _CACHE_LIMIT):
            for key, spelled in list(spellings.items()):
                if current(spelled) != key:
                    spellings.pop(key, None)
            owner.__dict__['_rs_spellings_limit'] = max(_CACHE_LIMIT, 2 * len(spellings))
        spellings[identity] = first = name
    return first  # type: ignore[no-any-return]


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
    return _inside(os.path.join(root, spec), root)


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
        roots = [*paths, *_configured_folders(settings), _package_templates(here)]
        # The outermost root that holds it (its own folder, at least: the package fallback).
        root = min((r for r in roots if _inside(here, r)), key=lambda r: len(_key(r)))
        while True:
            folders.append((here, root))
            parent = os.path.dirname(here)
            if _key(here) == _key(root) or parent == here:
                break
            here = parent
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


def _key(path: str) -> str:
    """A path as comparisons see it: absolute, normalized, case-folded where the platform
    folds (Windows), and with a leading `//` as `/`, as `commonpath` gives it."""
    return os.path.commonpath([os.path.normcase(os.path.abspath(path))])


def _inside(path: str, root: str) -> bool:
    try:
        root_key = _key(root)
        return os.path.commonpath([_key(path), root_key]) == root_key
    except ValueError:
        # Different drives (Windows).
        return False


def _is_package(path: str) -> bool:
    # `package:folder` entries (package resources) are not searched for partials.
    return not os.path.isabs(path) and ':' in path


def _sibling(caller: Any, path: str, identity: tuple[int, int]) -> Any:
    cls = type(caller)
    config = getattr(caller, '_partial_config', {})
    paths = getattr(caller, '_partial_paths', [])
    try:
        # By the file, not the path's spelling: the first spelling seen builds the template.
        settings = tuple(sorted((k, _setting_key(v)) for k, v in config.items()))
        key: Any = (identity, (_BY_ID, id(cls)), tuple(_setting_key(p) for p in paths), settings)
    except TypeError:
        # Unhashable settings or search path entries: not shared (the caller's own cache keeps
        # it).
        return cls(path, search_path=list(paths), **config)
    template = _SIBLINGS.get(key)
    # One built by a hard link that has since been replaced (the file `identity` is gone from
    # its path) is not this file's any more.
    if template is not None and _file_id(getattr(template, 'filename', path)) == identity:
        return template
    template = cls(path, search_path=list(paths), **config)
    try:
        # What the key holds by `id`, alive while the entry is (the class is the template's
        # own): no other object can take an `id` the key still has.
        template.__dict__['_rs_settings'] = (tuple(paths), tuple(config.values()))
        _SIBLINGS[key] = template
    except (AttributeError, TypeError):
        # A template class without a `__dict__` or weak references: not shared.
        pass
    return template


def _setting_key(value: Any) -> Any:
    if type(value) in _PLAIN:
        return value
    if type(value) is tuple:
        return tuple(_setting_key(v) for v in value)
    # Raises `TypeError` for an unhashable value, as keying by the value itself did.
    hash(value)
    return (_BY_ID, id(value))


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
    name_of = module.get('_template_name')
    if callable(name_of):
        # 0.2.1 on: its own check and spelling (one template per file).
        name = name_of(template_file, folder)
    else:
        name = template_file if _in_folder(template_file.strip(), folder) else None
    if name is None:
        # A name outside its folder or missing: called as Python, so the function decides (it
        # refuses one from 0.2.1 on), never loaded here around its check.
        return None
    return loader[name]


# The template classes `_prepare` checks for, imported on first use.
_PageTemplate: Any = None
_PageTextTemplateFile: Any = None


def _prepare(template: Any) -> tuple[Any, Any, Any] | None:
    """What ``template.render(encoding='utf-8', ...)`` would use, in its order (defaults, then
    ``cook_check()``): ``(program, builtins, defaults)``, or ``None`` when the template cannot
    render as a native frame (another engine's, a text template, a ``_render`` function)."""
    global _PageTemplate, _PageTextTemplateFile
    if _PageTemplate is None:
        # `template` imports this module. An import statement here would look the module up in
        # `sys.modules` on every call, a dict every thread shares (a lock on free-threaded Python).
        from chameleon_rs.template import PageTemplate, PageTextTemplateFile

        _PageTemplate, _PageTextTemplateFile = PageTemplate, PageTextTemplateFile
    PageTemplate, PageTextTemplateFile = _PageTemplate, _PageTextTemplateFile
    if not isinstance(template, PageTemplate) or isinstance(template, PageTextTemplateFile):
        return None
    defaults = template._encoded_defaults('utf-8', None)
    template.cook_check()
    if '_render' in template.__dict__ or type(template).render is not PageTemplate.render:
        return None
    program, builtins = template._compiled
    return program, builtins, defaults
