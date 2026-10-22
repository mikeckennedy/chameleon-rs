"""Process-wide settings for templates loaded by name: ``chameleon_rs.configure()``.

One call configures the template folder and its loader for everything that loads templates by
name: the built-in ``render_partial`` and the framework integrations (``chameleon_rs.flask``).
The old per-library calls (``chameleon_partials.register_extensions``,
``chameleon_flask.global_init``) are aliases of it. A configuration is an immutable
``Settings`` object swapped in whole, so a render never sees half of one (free-threading).
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

from chameleon_rs.web import TemplateError


class Settings:
    """A configuration: the template folder, its loader, and the loader's options."""

    __slots__ = ('auto_reload', 'loader', 'restricted_namespace', 'template_folder')

    def __init__(self, template_folder: str, auto_reload: bool, restricted_namespace: bool) -> None:
        from chameleon_rs.zpt.loader import TemplateLoader

        self.template_folder = template_folder
        self.auto_reload = auto_reload
        self.restricted_namespace = restricted_namespace
        kwargs: dict[str, Any] = {'auto_reload': auto_reload}
        if not restricted_namespace:
            kwargs['restricted_namespace'] = False
        self.loader = TemplateLoader(template_folder, **kwargs)

    def __repr__(self) -> str:
        return (
            f'Settings(template_folder={self.template_folder!r}, auto_reload={self.auto_reload!r}, '
            f'restricted_namespace={self.restricted_namespace!r})'
        )


# The process-wide configuration, one slot per consumer, as the original libraries each had
# their own: `partials` (what `render_partial` resolves against; chameleon_partials'
# `register_extensions`) and `flask` (chameleon-flask's `global_init`). `configure()` sets both,
# each alias only its own (`None`: not configured, or cleared). `generation` is bumped by every
# change and keys the partial path caches.
partials: Settings | None = None
flask: Settings | None = None
generation = 0
_lock = threading.Lock()

# Where `render_partial` looks besides the configured folder: `'folder'` (the default: only the
# configured folder, as chameleon_partials) or `'caller'` (opt-in: then the calling template's
# search path, its folder and each folder above it).
PARTIAL_SEARCH = ('folder', 'caller')
search_mode = 'folder'


def check_folder(template_folder: Any, error: type[Exception] = TemplateError) -> None:
    """chameleon_partials' and chameleon-flask's checks, with their messages."""
    if not template_folder:
        msg = 'The template_folder must be specified.'
        raise error(msg)
    if not os.path.isdir(template_folder):
        msg = f"The specified template folder must be a folder, it's not: {template_folder}"
        raise error(msg)


def build(
    template_folder: Any = None,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
    *,
    base: Settings | None = None,
    error: type[Exception] = TemplateError,
) -> Settings:
    """A configuration from ``base`` with the given settings changed (``None``: unchanged)."""
    folder = template_folder if template_folder is not None else (base.template_folder if base else None)
    check_folder(folder, error)
    if auto_reload is None:
        auto_reload = base.auto_reload if base else False
    if restricted_namespace is None:
        restricted_namespace = base.restricted_namespace if base else True
    return Settings(str(folder), bool(auto_reload), bool(restricted_namespace))


_KEEP: Any = object()


def install(*, partials_settings: Any = _KEEP, flask_settings: Any = _KEEP) -> None:
    """Sets the slots given (``None`` clears one)."""
    global partials, flask, generation
    with _lock:
        if partials_settings is not _KEEP:
            partials = partials_settings
        if flask_settings is not _KEEP:
            flask = flask_settings
        generation += 1
    from chameleon_rs import partials as partials_module

    partials_module._configured(partials)
    if 'chameleon_rs.flask' in sys.modules:
        sys.modules['chameleon_rs.flask']._sync()


def configure(
    template_folder: Any = None,
    *,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
    partial_search: str | None = None,
) -> Settings | None:
    """Configures chameleon-rs for the whole process: where templates loaded by name live, for
    ``render_partial`` and the framework integrations (``chameleon_rs.flask``), and how they load.

    ``template_folder``: an existing directory. ``auto_reload``: recompile a template when its
    file changes (checked at most once per file per render for partials). ``restricted_namespace``:
    ``False`` allows attributes such as Alpine.js's ``@click`` (chameleon-flask's option).
    ``partial_search``: ``'folder'`` (the default, as chameleon_partials: partials are looked for
    in the template folder only) or ``'caller'`` (opt-in: also on the calling template's search
    path, then in its folder and each folder above it). A setting left out keeps its current
    value; the first call that sets anything but ``partial_search`` must give the folder.
    Applies every time it is called (there is no ``cache_init``).

    Raises ``chameleon_rs.web.TemplateError`` for a missing folder, ``ValueError`` for an
    unknown ``partial_search``.
    """
    global search_mode
    if partial_search is not None:
        if partial_search not in PARTIAL_SEARCH:
            msg = f'partial_search must be one of {PARTIAL_SEARCH}, not {partial_search!r}'
            raise ValueError(msg)
        search_mode = partial_search
        if template_folder is None and auto_reload is None and restricted_namespace is None:
            install()
            return partials
    settings = build(template_folder, auto_reload, restricted_namespace, base=partials or flask)
    install(partials_settings=settings, flask_settings=settings)
    return settings


def clear() -> None:
    """Forgets the process-wide configuration, both slots and ``partial_search`` (tests reset
    with it)."""
    global search_mode
    search_mode = 'folder'
    install(partials_settings=None, flask_settings=None)
