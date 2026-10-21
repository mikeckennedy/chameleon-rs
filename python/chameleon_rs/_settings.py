"""Process-wide settings for templates loaded by name: ``chameleon_rs.configure()``.

One call configures the template folder and its loader for everything that loads templates by
name: the built-in ``render_partial`` and the framework integrations (``chameleon_rs.flask``).
The old per-library calls (``chameleon_partials.register_extensions``,
``chameleon_flask.global_init``) are aliases of it. A configuration is an immutable
``Settings`` object swapped in whole, so a render never sees half of one (free-threading).
"""

from __future__ import annotations

import os
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


# The current process-wide configuration (`None`: never configured, or cleared), and a counter
# bumped by every change, which keys the partial path caches.
current: Settings | None = None
generation = 0
_lock = threading.Lock()


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


def install(settings: Settings | None) -> None:
    """Makes ``settings`` the process-wide configuration (``None`` clears it)."""
    global current, generation
    with _lock:
        current = settings
        generation += 1
    from chameleon_rs import partials

    partials._configured(settings)


def configure(
    template_folder: Any = None,
    *,
    auto_reload: bool | None = None,
    restricted_namespace: bool | None = None,
) -> Settings:
    """Configures chameleon-rs for the whole process: where templates loaded by name live
    (``render_partial`` and the framework integrations look there first) and how they load.

    ``template_folder``: an existing directory. ``auto_reload``: recompile a template when its
    file changes (checked at most once per file per render for partials). ``restricted_namespace``:
    ``False`` allows attributes such as Alpine.js's ``@click`` (as chameleon-flask's option).
    A setting left out keeps its current value; the first call must give the folder. Applies
    every time it is called (there is no ``cache_init``); ``clear()`` resets it.

    Raises ``chameleon_rs.web.TemplateError`` for a missing folder.
    """
    settings = build(template_folder, auto_reload, restricted_namespace, base=current)
    install(settings)
    return settings


def clear() -> None:
    """Forgets the process-wide configuration (tests reset with it)."""
    install(None)
