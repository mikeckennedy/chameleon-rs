"""Run code written for Chameleon on chameleon-rs.

``install()`` registers chameleon-rs's modules under Chameleon's names in ``sys.modules``,
so that ``from chameleon import PageTemplateLoader`` (or ``chameleon.zpt.template``,
``chameleon.tales``, ``chameleon.utils.Scope``, ...) gives chameleon-rs's objects. Call it
before anything imports ``chameleon``::

    import chameleon_rs.compat

    chameleon_rs.compat.install()

    from chameleon import PageTemplateLoader  # chameleon-rs's

Stock Chameleon does not need to be installed (and is never imported). Each module of
Chameleon maps to the chameleon-rs module of the same name, the same object (``chameleon.X``
is ``chameleon_rs.X``; ``chameleon`` is ``chameleon_rs``), which implements it natively or
vendors Chameleon's pure-Python code; see ``MODULES`` and docs/architecture.md (D7).
Anything else under ``chameleon.`` raises ``ModuleNotFoundError``: Chameleon's
code-generation internals with no counterpart here (``NOT_PROVIDED``), and chameleon-rs's
private modules (``chameleon._native`` would load the extension a second time).

It also maps the libraries chameleon-rs has built in (``INTEGRATIONS``):
``import chameleon_partials`` gives ``chameleon_rs.partials`` and ``import chameleon_flask``
(with ``chameleon_flask.engine`` and ``chameleon_flask.exceptions``) gives
``chameleon_rs.flask``, imported only when the old name is (Flask is not imported before).
``install(integrations=False)`` maps only ``chameleon``, to run the original packages.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import pkgutil
import sys
import warnings
from collections.abc import Sequence
from types import ModuleType

# Chameleon modules that are code-generation internals with no counterpart in chameleon-rs.
NOT_PROVIDED = frozenset({'nodes', 'program', 'zpt.program', 'benchmark'})

# chameleon-rs's own public modules that are not Chameleon's (not aliased under `chameleon.`).
OWN_MODULES = frozenset({'flask', 'partials', 'web'})

# Libraries built into chameleon-rs: old top-level name -> (module, submodule names that are
# the same module).
INTEGRATIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    'chameleon_partials': ('chameleon_rs.partials', ()),
    'chameleon_flask': ('chameleon_rs.flask', ('engine', 'exceptions')),
}


def _public_modules() -> list[str]:
    """chameleon-rs's public modules (relative names): each is Chameleon's module of the same
    name. Private ones (`_native`, `_rewrite`, ...) and this package are not aliased."""
    package = importlib.import_module('chameleon_rs')
    names = []
    for info in pkgutil.walk_packages(package.__path__, 'chameleon_rs.'):
        relative = info.name.removeprefix('chameleon_rs.')
        parts = relative.split('.')
        if parts[0] in ('compat', *OWN_MODULES) or any(part.startswith('_') for part in parts):
            continue
        names.append(relative)
    return sorted(names)


MODULES: tuple[str, ...] = tuple(_public_modules())


class _Unprovided(importlib.abc.MetaPathFinder):
    """While the alias is installed, a ``chameleon.X`` that is not aliased is not searched for
    in chameleon-rs's directory (it would load a second copy of a module, or of the native
    extension): it is a ``ModuleNotFoundError``."""

    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname.startswith('chameleon.') and installed():
            relative = fullname.removeprefix('chameleon.')
            reason = 'a Chameleon internal with no counterpart' if relative in NOT_PROVIDED else 'not provided'
            msg = f'No module named {fullname!r} (chameleon-rs compat: {reason})'
            raise ModuleNotFoundError(msg, name=fullname)
        return None


_FINDER = _Unprovided()


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """While installed with ``integrations``, the old names in ``INTEGRATIONS`` import the
    built-in module (on first import, so Flask is imported only when ``chameleon_flask`` is)."""

    enabled = False

    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if self.enabled and fullname in INTEGRATIONS:
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        module_name, submodules = INTEGRATIONS[spec.name]
        module = importlib.import_module(module_name)
        for sub in submodules:
            sys.modules[f'{spec.name}.{sub}'] = module
        return module

    def exec_module(self, module: ModuleType) -> None:
        pass


_ALIAS = _Alias()


def _is_old(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + '.') for p in prefixes)


def install(*, integrations: bool = True) -> ModuleType:
    """Makes ``import chameleon`` (and its submodules) resolve to chameleon-rs and, with
    ``integrations`` (the default), ``import chameleon_partials`` / ``import chameleon_flask``
    to the built-ins (see the module docs). Modules already imported under these names are
    replaced, with a ``RuntimeWarning`` naming them (code that imported them keeps its
    references). Idempotent; returns the module now known as ``chameleon``."""
    top = importlib.import_module('chameleon_rs')
    old = ('chameleon', *INTEGRATIONS) if integrations else ('chameleon',)
    evicted = []
    for name in [name for name in sys.modules if _is_old(name, old)]:
        module = sys.modules[name]
        if module is None or getattr(module, '__name__', '').startswith('chameleon_rs'):
            continue
        if name.split('.')[0] in INTEGRATIONS and not integrations:
            continue
        if name == 'chameleon.tests' or name.startswith('chameleon.tests.'):
            # (A test harness's own module, as the upstream suite's conftest registers.)
            continue
        evicted.append(name)
        del sys.modules[name]
    if evicted:
        warnings.warn(
            'chameleon_rs.compat.install() replaced modules already imported: '
            + ', '.join(sorted(evicted))
            + '. Objects imported from them before stay stock Chameleon objects.',
            RuntimeWarning,
            stacklevel=2,
        )
    sys.modules['chameleon'] = top
    for name in MODULES:
        sys.modules[f'chameleon.{name}'] = importlib.import_module(f'chameleon_rs.{name}')
    if _FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _FINDER)
    _ALIAS.enabled = integrations
    if _ALIAS not in sys.meta_path:
        sys.meta_path.insert(0, _ALIAS)
    if integrations:
        sys.modules['chameleon_partials'] = importlib.import_module('chameleon_rs.partials')
    return top


def installed() -> bool:
    """Whether ``import chameleon`` currently gives chameleon-rs."""
    return getattr(sys.modules.get('chameleon'), '__name__', None) == 'chameleon_rs'
