"""Level 2 (plan.md): every upstream fixture (`tests/upstream/chameleon_tests/inputs`) rendered
by Chameleon and by chameleon_rs, as Chameleon's own `test_pt_files` / `test_txt_files` feed
them (non-strict mode, the same keyword arguments, translate function and target languages),
compared byte for byte (stricter than the upstream suite's doctest-style comparison with the
expected output files).

Fixtures that need a feature of a later phase are listed in `LATER` with the feature
chameleon_rs reports as unsupported; the list is strict: a listed fixture must still raise
`UnsupportedError` (so it is removed when it starts rendering), and any other fixture must
render identically.
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path
from typing import Any

import chameleon.loader
import chameleon.zpt.template
import chameleon_rs
import chameleon_rs.exc
import chameleon_rs.loader
import pytest

UPSTREAM = Path(__file__).parent.parent / 'upstream'
sys.path.insert(0, str(UPSTREAM))

from chameleon_tests import test_templates as upstream

ROOT = upstream.ROOT

INPUTS = os.path.join(ROOT, 'inputs')
OUTPUTS = os.path.join(ROOT, 'outputs')

# Every fixture renders since Phase 6b (macros, slots, i18n), including those that load
# other templates with `load:` and the `load` keyword (the file loader of Phase 5).
LATER: dict[str, str] = {}


def fixtures() -> list[tuple[str, str | None]]:
    """`find_files`: each input with every target language its outputs name."""
    found = []
    for filename in sorted(os.listdir(INPUTS)):
        name, ext = os.path.splitext(filename)
        if ext not in ('.pt', '.txt'):
            continue
        for output in sorted(glob.glob(os.path.join(OUTPUTS, f'{name.split("-", 1)[0]}*{ext}'))):
            basename = os.path.basename(os.path.splitext(output)[0])
            found.append((filename, basename.split('-')[1] if '-' in basename else None))
    return found


class Literal:
    def __init__(self, s: str) -> None:
        self.s = s

    def __html__(self) -> str:
        return self.s

    def __str__(self) -> str:
        raise RuntimeError(f'{self.s!r} is a literal.')


def render(engine: str, filename: str, language: str | None) -> Any:
    """`TestZopePageTemplates.execute` for one engine."""
    path = os.path.join(INPUTS, filename)
    text = filename.endswith('.txt')
    implicit = 'implicit-i18n' in filename
    options: dict[str, Any] = {
        'strict': False,
        'implicit_i18n_translate': implicit,
        'implicit_i18n_attributes': ('alt', 'title') if implicit else (),
        'enable_data_attributes': 'data-attributes' in filename,
        'keep_source': True,
    }
    if engine == 'chameleon':
        module: Any = chameleon.zpt.template
        loader: Any = chameleon.loader.TemplateLoader(INPUTS)
    else:
        module = chameleon_rs
        loader = chameleon_rs.loader.TemplateLoader(INPUTS)
    factory = module.PageTextTemplateFile if text else module.PageTemplateFile
    template = factory(path, **options)
    params: dict[str, Any] = {}
    if not text:
        params = {
            'literal': Literal('<div>Hello world!</div>'),
            'content': '<div>Hello world!</div>',
            'message': upstream.Message(),
            'load': loader.bind(module.PageTemplateFile),
        }
    params.update(translate=upstream.TestZopePageTemplates.translate, target_language=language)
    template.cook_check()
    return template.render(**params)


@pytest.mark.parametrize(('filename', 'language'), fixtures(), ids=lambda v: str(v))
def test_fixture_renders_like_chameleon(filename: str, language: str | None) -> None:
    if filename in LATER:
        with pytest.raises(chameleon_rs.exc.UnsupportedError) as unsupported:
            render('chameleon_rs', filename, language)
        assert str(unsupported.value).startswith(LATER[filename]), unsupported.value
        pytest.skip(f'later phase: {LATER[filename]}')
    want = render('chameleon', filename, language)
    got = render('chameleon_rs', filename, language)
    assert type(got) is type(want)
    assert got == want


def test_the_skip_list_names_real_fixtures() -> None:
    names = {f for f, _ in fixtures()}
    assert set(LATER) <= names
