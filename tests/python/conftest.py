"""Test-wide setup: Chameleon's (and chameleon_rs's) `CHAMELEON_*` settings are read from the
environment when `chameleon.config` is imported, so a developer's shell (for example
`CHAMELEON_EAGER=1`) would change what the tests see. They are cleared before anything imports
either engine; tests that need one set it in a subprocess (`test_api.test_environment_settings`)."""

import os

for _name in ('CHAMELEON_EAGER', 'CHAMELEON_DEBUG', 'CHAMELEON_CACHE', 'CHAMELEON_RELOAD'):
    os.environ.pop(_name, None)
