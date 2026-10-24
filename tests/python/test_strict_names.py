"""Template names in the integrations' decorator and module APIs (`@template`, `render()`,
`response()`, `ErrorPage.template_file`, all through `_views.Integration.page`).

Always: every spelling of one file (`./a.pt`, `a//a.pt`, an absolute path) loads one cached
template, and the names remembered for that are bounded. Opt-in (`strict_names`, chameleon-rs
only): names that could reach outside the template folder are "not found"."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip('starlette')
pytest.importorskip('flask')

import chameleon_rs
import chameleon_rs._settings
import chameleon_rs._views
import chameleon_rs.fastapi
import chameleon_rs.flask
import chameleon_rs.web

MODULES = [chameleon_rs.fastapi, chameleon_rs.flask]


@pytest.fixture
def folder(tmp_path: Path) -> Iterator[Path]:
    templates = tmp_path / 'templates'
    (templates / 'sub').mkdir(parents=True)
    (templates / 'sub' / 'p.pt').write_text('<p>${x}</p>')
    (tmp_path / 'outside.pt').write_text('<p>outside</p>')
    try:
        yield templates
    finally:
        chameleon_rs._settings.clear()


def spellings(folder: Path) -> list[str]:
    names = []
    for i in range(200):
        names.append('./' * (i % 10) + 'sub/' + '/' * (i // 10) + 'p.pt')
    names.append(str(folder / 'sub' / 'p.pt'))
    names.append(f'{folder}/sub/./p.pt')
    return names


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__)
def test_every_spelling_loads_one_template(folder: Path, module: Any) -> None:
    chameleon_rs.configure(template_folder=str(folder))
    names = spellings(folder)
    assert len(set(names)) == 202
    for name in names:
        assert module.render(name, x=1) == '<p>1</p>'
    settings = chameleon_rs._settings.views[module.__name__.rpartition('.')[2]]
    assert settings is not None
    loader = settings.loader
    assert len(loader.registry) == 1
    # And again from the memo, with no new entries.
    for name in names:
        assert module.render(name, x=2) == '<p>2</p>'
    assert len(loader.registry) == 1


def test_a_remembered_name_costs_no_stat(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chameleon_rs.configure(template_folder=str(folder))
    assert chameleon_rs.fastapi.render('./sub/p.pt', x=1) == '<p>1</p>'

    def no_stat(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('stat on the hot path')

    monkeypatch.setattr(chameleon_rs._views.os, 'stat', no_stat)
    assert chameleon_rs.fastapi.render('./sub/p.pt', x=2) == '<p>2</p>'


def test_names_of_missing_files_are_not_remembered(folder: Path) -> None:
    settings = chameleon_rs.configure(template_folder=str(folder))
    assert settings is not None
    for i in range(3000):
        with pytest.raises(ValueError, match=f'Template not found: nope{i}.pt.'):
            chameleon_rs.fastapi.render(f'nope{i}.pt')
    assert len(settings.loader.__dict__.get('_rs_names', {})) == 0
    assert len(settings.loader.registry) == 0


def test_remembered_names_are_bounded(folder: Path) -> None:
    settings = chameleon_rs.configure(template_folder=str(folder))
    assert settings is not None
    for i in range(3000):
        assert chameleon_rs.flask.render('./' * (i % 3) + 'sub/' + '/' * i + 'p.pt', x=i) == f'<p>{i}</p>'
    assert 0 < len(settings.loader.__dict__['_rs_names']) <= chameleon_rs._views._NAMES_LIMIT
    assert len(settings.loader.registry) == 1


def test_missing_template_error_is_unchanged(folder: Path) -> None:
    chameleon_rs.configure(template_folder=str(folder))
    with pytest.raises(ValueError, match=r'^Template not found: nope\.pt\.$') as info:
        chameleon_rs.fastapi.render('nope.pt')
    assert type(info.value) is ValueError
    # A directory is passed on to the loader, which loads it as it did (and fails rendering).
    with pytest.raises(OSError, match='sub'):
        chameleon_rs.fastapi.render('sub')


def test_default_keeps_the_original_lookup(folder: Path) -> None:
    settings = chameleon_rs.configure(template_folder=str(folder))
    assert settings is not None
    assert settings.strict_names is False
    assert chameleon_rs.fastapi.render('../outside.pt') == '<p>outside</p>'
    assert chameleon_rs.flask.render(str(folder.parent / 'outside.pt')) == '<p>outside</p>'
    assert chameleon_rs.flask.render('sub/../../outside.pt') == '<p>outside</p>'
    # Three spellings of the outside file, one template.
    assert len(settings.loader.registry) == 1


BAD_NAMES = [
    '../outside.pt',
    'sub/../../outside.pt',
    '/etc/hosts',
    'sub\\p.pt',
    'C:/templates/sub/p.pt',
    'chameleon_rs:templates/p.pt',
    'sub/p.pt\0',
    ' sub/p.pt',
    'sub/p.pt ',
    '',
]


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__)
@pytest.mark.parametrize('name', BAD_NAMES)
def test_strict_names_refuses_names_outside_the_folder(folder: Path, module: Any, name: str) -> None:
    chameleon_rs.configure(template_folder=str(folder), strict_names=True)
    with pytest.raises(ValueError, match='^Template not found: ') as info:
        module.render(name)
    # The loader's own error for a missing template, so `except ValueError` keeps working.
    assert type(info.value) is ValueError
    assert chameleon_rs._views.is_not_found(info.value)
    # The absolute path of a file in the folder is refused too, as Jinja does.
    with pytest.raises(ValueError, match='^Template not found: '):
        module.render(str(folder / 'sub' / 'p.pt'))
    # Names inside the folder still load, in any spelling.
    assert module.render('./sub//p.pt', x=1) == '<p>1</p>'


def test_strict_names_covers_response_and_error_pages(folder: Path) -> None:
    chameleon_rs.configure(template_folder=str(folder), strict_names=True)
    with pytest.raises(ValueError, match='^Template not found: '):
        chameleon_rs.fastapi.response('../outside.pt')

    @chameleon_rs.fastapi.template('../outside.pt')
    def view() -> dict[str, Any]:
        return {}

    with pytest.raises(ValueError, match='^Template not found: '):
        view()

    @chameleon_rs.fastapi.template('sub/p.pt')
    def errors() -> dict[str, Any]:
        raise chameleon_rs.web.ErrorPage('../outside.pt', 500)

    with pytest.raises(ValueError, match='^Template not found: '):
        errors()


def test_strict_names_is_a_runtime_check_only(folder: Path) -> None:
    # Not a loader option: templates compile as they did, with the same digest.
    settings = chameleon_rs.configure(template_folder=str(folder), strict_names=True)
    assert settings is not None
    assert 'strict_names' not in settings.loader.kwargs
    assert settings.loader['sub/p.pt'](x=1) == '<p>1</p>'


def test_configure_round_trip(folder: Path) -> None:
    settings = chameleon_rs.configure(template_folder=str(folder))
    assert settings is not None
    assert settings.strict_names is False
    assert 'strict_names' not in repr(settings)
    on = chameleon_rs.configure(strict_names=True)
    assert on is not None
    assert on.strict_names is True
    assert repr(on).endswith(', strict_names=True)')
    # Left out, it keeps its value, and a framework's own configure() starts from it.
    kept = chameleon_rs.configure(auto_reload=True)
    assert kept is not None
    assert kept.strict_names is True
    chameleon_rs.fastapi.configure(template_folder=str(folder))
    integration = chameleon_rs._settings.views['fastapi']
    assert integration is not None
    assert integration.strict_names is True
    with pytest.raises(ValueError, match='^Template not found: '):
        chameleon_rs.fastapi.render('../outside.pt')
    both = chameleon_rs.configure(strict_tags=True)
    assert repr(both).endswith(', strict_tags=True, strict_names=True)')
    off = chameleon_rs.configure(strict_names=False, strict_tags=False)
    assert off is not None
    assert off.strict_names is False
    assert chameleon_rs.fastapi.render('../outside.pt') == '<p>outside</p>'


@pytest.mark.parametrize(('value', 'expected'), [('1', True), ('yes', True), ('0', False)])
def test_environment_variable_sets_the_default(folder: Path, value: str, expected: bool) -> None:
    code = (
        'import chameleon_rs, chameleon_rs.fastapi, sys\n'
        f'settings = chameleon_rs.configure(template_folder={str(folder)!r})\n'
        'print(settings.strict_names)\n'
        'try:\n'
        "    chameleon_rs.fastapi.render('../outside.pt')\n"
        "    print('loaded')\n"
        'except ValueError as e:\n'
        '    print(e)\n'
        # A configure() argument wins over the variable.
        'print(chameleon_rs.configure(strict_names=False).strict_names)\n'
    )
    env = {**os.environ, 'CHAMELEON_STRICT_NAMES': value}
    out = subprocess.run([sys.executable, '-P', '-c', code], env=env, capture_output=True, text=True, check=True)
    lines = out.stdout.splitlines()
    assert lines[0] == str(expected)
    assert lines[1] == ('Template not found: ../outside.pt.' if expected else 'loaded')
    assert lines[2] == 'False'
    # Not reported as an unknown variable (`make test` sets other `CHAMELEON_*` ones).
    assert 'CHAMELEON_STRICT_NAMES' not in out.stderr
