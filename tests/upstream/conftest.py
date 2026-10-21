"""
Runs Chameleon's own test suite (vendored, unmodified, from malthe/chameleon at commit
90b900244abd9ce39c9a9ca70b38ed559ae2b969) against a selectable engine.

    CHAMELEON_ENGINE=python  -> the reference Chameleon (the baseline)
    CHAMELEON_ENGINE=rust    -> chameleon_rs, aliased in as the `chameleon` package

The tests import `chameleon.*`, so the Rust engine is tested by installing chameleon_rs's
compatibility package under that name before collection starts.

The Rust ratchet (expected_failures_rust.txt) is strict: a listed test that starts passing
fails the run, so the list always matches reality. The environmental failures are not strict,
because they depend on the machine: they fail on the maintainer's Mac and pass on CI runners.
"""

import os
import sys
import types
from pathlib import Path

import pytest

ENGINE = os.environ.get('CHAMELEON_ENGINE', 'python')
HERE = Path(__file__).parent

if ENGINE == 'rust':
    try:
        from chameleon_rs import compat
    except ImportError:
        pytest.exit(
            'chameleon_rs.compat is not implemented yet (see docs/plan.md, Phase 7). '
            'Run with CHAMELEON_ENGINE=python for the baseline.',
            returncode=2,
        )
    compat.install()
elif ENGINE != 'python':
    pytest.exit(f'Unknown CHAMELEON_ENGINE={ENGINE!r}; use "python" or "rust".', returncode=2)


def _alias_vendored_test_module() -> None:
    # Fixture 079 does `import:chameleon.tests.test_templates.Message` and the test's
    # translate() checks isinstance(msgid, Message). Point that dotted path at the vendored
    # module so both sides see the same class, without editing the fixture.
    import chameleon
    from chameleon_tests import test_templates

    tests_pkg = sys.modules.get('chameleon.tests')
    if tests_pkg is None:
        tests_pkg = types.ModuleType('chameleon.tests')
        sys.modules['chameleon.tests'] = tests_pkg
    chameleon.tests = tests_pkg
    tests_pkg.test_templates = test_templates
    sys.modules['chameleon.tests.test_templates'] = test_templates


_alias_vendored_test_module()

# Fail on the reference Chameleon too, in this environment; excluded from the compatibility
# score. See tests/upstream/README.md for why each one fails.
ENVIRONMENTAL_FAILURES = {
    'test_loader.py::LoadPageTests::test_load_egg',
    'test_loader.py::LoadPageTests::test_load_wheel',
    'test_loader.py::ZPTLoadPageTests::test_load_egg',
    'test_loader.py::ZPTLoadPageTests::test_load_wheel',
    'test_templates.py::TestTemplateFile::test_relative_is_expanded_to_cwd',
    'test_templates.py::TestZopePageTemplates::test_package_name_cook_check',
}


def _load_rust_expected_failures() -> dict[str, str]:
    """Reads expected_failures_rust.txt: `<test id>  # <reason>` per line."""
    failures = {}
    for line in (HERE / 'expected_failures_rust.txt').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        test_id, sep, reason = line.partition('#')
        if not sep or not reason.strip():
            raise pytest.UsageError(f'expected_failures_rust.txt: every entry needs a "# reason": {line!r}')
        failures[test_id.strip()] = reason.strip()
    return failures


def pytest_collection_modifyitems(config, items):
    rust_failures = _load_rust_expected_failures() if ENGINE == 'rust' else {}
    for item in items:
        key = item.nodeid.split('chameleon_tests/', 1)[-1]
        if key in ENVIRONMENTAL_FAILURES:
            item.add_marker(pytest.mark.xfail(reason='fails on reference Chameleon in some environments', strict=False))
        elif key in rust_failures:
            item.add_marker(pytest.mark.xfail(reason=rust_failures[key], strict=True))


def pytest_report_header(config):
    import chameleon

    return f'chameleon engine: {ENGINE} ({chameleon.__file__})'
