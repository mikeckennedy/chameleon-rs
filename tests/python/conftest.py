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


@pytest.fixture(autouse=True)
def slot_error_sites_like_chameleon(request):
    """Errors through slot bodies recorded as Chameleon records them, so the differential tests
    compare whole error messages. chameleon_rs's default records the slot body's own failing
    expression and drops the stale one Chameleon adds (malthe/chameleon#447); tests of that
    default are marked `own_slot_error_sites`."""
    from chameleon_rs._native import _testing

    previous = _testing.set_slot_error_sites_like_chameleon('own_slot_error_sites' not in request.keywords)
    yield
    _testing.set_slot_error_sites_like_chameleon(previous)
