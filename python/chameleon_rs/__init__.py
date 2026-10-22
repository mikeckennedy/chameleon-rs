"""
chameleon-rs: a Rust implementation of the Chameleon page template engine.

The public API mirrors `chameleon` (PageTemplate, PageTemplateFile, PageTemplateLoader, ...).
`chameleon_rs.compat.install()` makes `import chameleon` resolve to this package, so code
written for Chameleon runs unchanged.

Every template can call `render_partial(path, **kwargs)` (built-in partials, see
`chameleon_rs.partials`); `chameleon_rs.configure(template_folder=...)` says where templates
loaded by name live, for partials and for the framework integrations (`chameleon_rs.flask`).
"""

from typing import Any

from chameleon_rs._async import concurrent
from chameleon_rs._native import core_version
from chameleon_rs._settings import MarkdownExtensions, MarkdownOptions, MarkdownSetting, configure
from chameleon_rs.exc import TemplateError
from chameleon_rs.template import PageTemplate, PageTemplateFile, PageTextTemplate, PageTextTemplateFile
from chameleon_rs.zpt.loader import TemplateLoader as PageTemplateLoader

from chameleon_rs.partials import render_partial  # isort: skip - after the template classes


def __getattr__(name: str) -> Any:
    # `__version__` is read from the installed metadata on first use: importing
    # `importlib.metadata` and finding the distribution cost ~6 ms of every app's startup.
    if name != '__version__':
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    from importlib.metadata import PackageNotFoundError, version

    try:
        value = version('chameleon-rs')
    except PackageNotFoundError:  # pragma: no cover - running from a source tree
        value = '0+unknown'
    globals()['__version__'] = value
    return value


__all__ = [
    'MarkdownExtensions',
    'MarkdownOptions',
    'MarkdownSetting',
    'PageTemplate',
    'PageTemplateFile',
    'PageTemplateLoader',
    'PageTextTemplate',
    'PageTextTemplateFile',
    'TemplateError',
    '__version__',
    'concurrent',
    'configure',
    'core_version',
    'render_partial',
]
