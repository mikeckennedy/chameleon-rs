"""Installs the compat alias in the child processes of an integration run, as
chameleon_compat_plugin does in the test process: pyramid_chameleon's precompile tests start a
multiprocessing pool, whose workers start a fresh interpreter (spawn on macOS, forkserver on
Linux) and import `chameleon`. The plugin puts this folder on their PYTHONPATH."""

import os

import chameleon_rs.compat

chameleon_rs.compat.install(integrations=os.environ.get('INTEGRATION_MODE', 'original') == 'builtin')
