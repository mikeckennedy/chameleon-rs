# Ported from Chameleon's src/chameleon/config.py, modified: type annotations; documents how
# chameleon-rs uses each setting.
"""Settings read from ``CHAMELEON_*`` environment variables when this module is imported, as
Chameleon reads them:

- ``CHAMELEON_EAGER``: file templates compile when they are created, not on first use.
- ``CHAMELEON_DEBUG``: templates keep their body and a readable dump of the compiled program
  (``source``), and use a ``ModuleLoader`` in a temporary directory as their ``loader``.
- ``CHAMELEON_CACHE``: a directory, which must exist. Chameleon stores generated Python
  modules there; chameleon-rs validates it and uses it for the templates' ``loader``, but
  compiled programs are not written to disk (docs/architecture.md, D11).
- ``CHAMELEON_RELOAD``: file templates reload when their file changes (``auto_reload``).

Values ``y``, ``yes``, ``t``, ``true``, ``on`` and ``1`` (any case) are true. Unknown
``CHAMELEON_*`` variables are logged as warnings.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger('chameleon.config')
environment = {k[10:]: v for (k, v) in ((j.lower(), x) for (j, x) in os.environ.items()) if k.startswith('chameleon_')}

# Define which values are read as true
TRUE = ('y', 'yes', 't', 'true', 'on', '1')

# If eager parsing is enabled, templates are parsed upon instantiation, rather than when first
# called upon; this mode is useful for verifying validity of templates across a project
EAGER_PARSING = environment.pop('eager', 'false').lower() in TRUE

# Debug mode is mostly useful for debugging the template engine itself.
DEBUG_MODE = environment.pop('debug', 'false').lower() in TRUE

# If a cache directory is specified, Chameleon persists template source code on disk.
path = environment.pop('cache', None)
CACHE_DIRECTORY: str | None
if path is not None:  # pragma: no cover
    CACHE_DIRECTORY = os.path.abspath(path)
    if not os.path.exists(CACHE_DIRECTORY):
        raise ValueError(f'Cache directory does not exist: {CACHE_DIRECTORY}.')
    log.info('directory cache: %s.', CACHE_DIRECTORY)
else:
    CACHE_DIRECTORY = None

# When auto-reload is enabled, templates are reloaded on file change.
AUTO_RELOAD = environment.pop('reload', 'false').lower() in TRUE

for key in environment:
    log.warning('unknown environment variable set: "CHAMELEON_%s".', key.upper())

# This is the slice length of the expression displayed in the formatted exception string
SOURCE_EXPRESSION_MARKER_LENGTH = 60
