"""Test-wide setup: Chameleon's (and chameleon_rs's) `CHAMELEON_*` settings are read from the
environment when `chameleon.config` is imported, so a developer's shell (for example
`CHAMELEON_EAGER=1`) would change what the tests see. They are cleared before anything imports
either engine; tests that need one set it in a subprocess (`test_api.test_environment_settings`)."""

import os

for _name in ('CHAMELEON_EAGER', 'CHAMELEON_DEBUG', 'CHAMELEON_CACHE', 'CHAMELEON_RELOAD'):
    os.environ.pop(_name, None)


import pytest


@pytest.fixture
def configured(tmp_path):
    """`tmp_path` as the template folder for `render_partial` (built-in partials resolve against
    the configured folder, as chameleon_partials does); cleared afterwards."""
    import chameleon_rs
    import chameleon_rs._settings

    chameleon_rs.configure(template_folder=str(tmp_path))
    yield tmp_path
    chameleon_rs._settings.clear()
