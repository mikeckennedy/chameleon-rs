"""
chameleon-rs: a Rust implementation of the Chameleon page template engine.

The public API mirrors `chameleon` (PageTemplate, PageTemplateFile, PageTemplateLoader, ...).
`chameleon_rs.compat.install()` makes `import chameleon` resolve to this package, so code
written for Chameleon runs unchanged.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from chameleon_rs._native import core_version
from chameleon_rs.exc import TemplateError
from chameleon_rs.template import PageTemplate, PageTemplateFile, PageTextTemplate, PageTextTemplateFile
from chameleon_rs.zpt.loader import TemplateLoader as PageTemplateLoader

try:
    __version__ = _version('chameleon-rs')
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = '0+unknown'

__all__ = [
    'PageTemplate',
    'PageTemplateFile',
    'PageTemplateLoader',
    'PageTextTemplate',
    'PageTextTemplateFile',
    'TemplateError',
    '__version__',
    'core_version',
]
