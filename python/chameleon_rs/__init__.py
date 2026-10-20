"""
chameleon-rs: a Rust implementation of the Chameleon page template engine.

The public API mirrors `chameleon` (PageTemplate, PageTemplateFile, PageTemplateLoader, ...)
and is filled in phase by phase; see docs/plan.md.
"""

from importlib.metadata import version as _version

from chameleon_rs._native import core_version

__version__ = _version('chameleon-rs')

__all__ = ['__version__', 'core_version']
