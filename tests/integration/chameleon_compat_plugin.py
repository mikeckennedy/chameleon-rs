"""A pytest plugin that runs a project written for Chameleon on chameleon-rs: it installs the
compat alias (`import chameleon` gives chameleon_rs) before the project's tests import
anything. Used by `make test-integration` with `-p chameleon_compat_plugin`.

`INTEGRATION_MODE=builtin` also maps `chameleon_partials`, `chameleon_flask` and
`fastapi_chameleon` to chameleon-rs's built-ins (`compat.install()`'s default), so those projects' own suites test
the built-ins; otherwise (`original`) only `chameleon` is aliased and the original packages
run on it."""

import importlib.util
import os
import sys

import chameleon_rs.compat

if importlib.util.find_spec('chameleon') is not None:
    # Stock Chameleon must not be installed, so that every import goes through the alias.
    raise RuntimeError('stock Chameleon is installed in this environment; the integration run must not see it')

MODE = os.environ.get('INTEGRATION_MODE', 'original')
if MODE not in ('original', 'builtin'):
    raise RuntimeError(f'INTEGRATION_MODE must be original or builtin, not {MODE!r}')
chameleon_rs.compat.install(integrations=MODE == 'builtin')


def pytest_report_header(config):
    import chameleon

    lines = [f'chameleon engine: chameleon_rs via compat.install() ({chameleon.__file__}), {MODE} integrations']
    if MODE == 'builtin':
        import chameleon_partials

        assert chameleon_partials is sys.modules['chameleon_rs.partials'], chameleon_partials
        lines.append('chameleon_partials, chameleon_flask, fastapi_chameleon: chameleon-rs built-ins')
    return lines


def pytest_sessionfinish(session, exitstatus):
    # The suite really ran on what the mode says (an original package found first on the
    # path would make a builtin run test nothing).
    expected = {
        'chameleon_partials': 'chameleon_rs.partials',
        'chameleon_flask': 'chameleon_rs.flask',
        'fastapi_chameleon': 'chameleon_rs.fastapi',
    }
    for old, builtin in expected.items():
        module = sys.modules.get(old)
        if module is None:
            continue
        is_builtin = module.__name__ == builtin
        if is_builtin != (MODE == 'builtin'):
            session.exitstatus = 1
            print(f'\n{old} was {module.__name__} in the {MODE} run', file=sys.stderr)
