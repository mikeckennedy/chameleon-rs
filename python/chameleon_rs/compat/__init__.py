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


def _public_modules() -> list[str]:
    """chameleon-rs's public modules (relative names): each is Chameleon's module of the same
    name. Private ones (`_native`, `_rewrite`, ...) and this package are not aliased."""
    package = importlib.import_module('chameleon_rs')
    names = []
    for info in pkgutil.walk_packages(package.__path__, 'chameleon_rs.'):
        relative = info.name.removeprefix('chameleon_rs.')
        parts = relative.split('.')
        if parts[0] == 'compat' or any(part.startswith('_') for part in parts):
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


def install() -> ModuleType:
    """Makes ``import chameleon`` (and its submodules) resolve to chameleon-rs. Stock
    Chameleon modules already imported are replaced, with a ``RuntimeWarning`` naming them
    (code that imported them keeps its references). Idempotent; returns the module now known
    as ``chameleon``."""
    top = importlib.import_module('chameleon_rs')
    evicted = []
    for name in [name for name in sys.modules if name == 'chameleon' or name.startswith('chameleon.')]:
        module = sys.modules[name]
        if module is None or getattr(module, '__name__', '').startswith('chameleon_rs'):
            continue
        if name == 'chameleon.tests' or name.startswith('chameleon.tests.'):
            # (A test harness's own module, as the upstream suite's conftest registers.)
            continue
        evicted.append(name)
        del sys.modules[name]
    if evicted:
        warnings.warn(
            'chameleon_rs.compat.install() replaced modules of stock Chameleon already imported: '
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
    return top


def installed() -> bool:
    """Whether ``import chameleon`` currently gives chameleon-rs."""
    return getattr(sys.modules.get('chameleon'), '__name__', None) == 'chameleon_rs'
