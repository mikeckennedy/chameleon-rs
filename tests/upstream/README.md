# Upstream Chameleon test suite

`chameleon_tests/` is Chameleon's own test suite and fixtures, copied unmodified from
[malthe/chameleon](https://github.com/malthe/chameleon) at commit
`90b900244abd9ce39c9a9ca70b38ed559ae2b969` (4.6.0-8-g90b9002). It is covered by Chameleon's
license (see `LICENSE.txt` and `COPYRIGHT.txt` at the repository root).

`conftest.py` selects the engine under test:

```sh
make test-upstream        # reference Chameleon (baseline)
make test-upstream-rust   # chameleon_rs, aliased in as `chameleon`
```

## Baseline

On Python 3.15.0 (macOS arm64), the reference Chameleon passes **227 of 233** tests. The six
failures below are environmental (found in Phase 7: on the maintainer's Mac they came from
`CHAMELEON_EAGER=1` in the shell, which `conftest.py` now clears, and they pass there too) (they fail for stock Chameleon on the maintainer's Mac but pass on
CI runners, so they are non-strict xfails) and are excluded from
the compatibility score:

| Test | Why it fails on the reference engine |
|---|---|
| `test_loader.py::LoadPageTests::test_load_egg` | Zip-resource mtime returns a timestamp; the test expects `None` |
| `test_loader.py::LoadPageTests::test_load_wheel` | Same as above |
| `test_loader.py::ZPTLoadPageTests::test_load_egg` | Same as above |
| `test_loader.py::ZPTLoadPageTests::test_load_wheel` | Same as above |
| `test_templates.py::TestTemplateFile::test_relative_is_expanded_to_cwd` | `FileNotFoundError` raised from `read()` before the `OSError` check the test expects |
| `test_templates.py::TestZopePageTemplates::test_package_name_cook_check` | Depends on how the installed `setuptools` package is laid out |

The compatibility target is that chameleon_rs passes at least 95% of the 227 (216 tests),
run unmodified. See `docs/plan.md` for how the score is defined.

## chameleon_rs

Since Phase 7 (2026-10-21), chameleon_rs passes **all 227** of them, unmodified
(`expected_failures_rust.txt` is empty), installed as `chameleon` by
`chameleon_rs.compat.install()`. The harness adapts two things the suite assumes about stock
Chameleon's package: `chameleon.tests.test_templates` is the vendored test module (fixture 079
imports it), and resources of the package `chameleon` (fixtures 081, 082 and 089 load
`chameleon:tests/inputs/hello_world.pt`) are looked up in a directory whose `tests` is this
suite.
