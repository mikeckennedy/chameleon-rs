from importlib.metadata import version

import chameleon_rs


def test_native_module_loads():
    assert chameleon_rs.core_version() == version('chameleon-rs')
    assert chameleon_rs.__version__ == version('chameleon-rs')
