"""A pytest plugin that runs a project written for Chameleon on chameleon-rs: it installs the
compat alias (`import chameleon` gives chameleon_rs) before the project's tests import
anything. Used by `make test-integration` with `-p chameleon_compat_plugin`."""

import importlib.util

import chameleon_rs.compat

if importlib.util.find_spec('chameleon') is not None:
    # Stock Chameleon must not be installed, so that every import goes through the alias.
    raise RuntimeError('stock Chameleon is installed in this environment; the integration run must not see it')

chameleon_rs.compat.install()


def pytest_report_header(config):
    import chameleon

    return f'chameleon engine: chameleon_rs via compat.install() ({chameleon.__file__})'
