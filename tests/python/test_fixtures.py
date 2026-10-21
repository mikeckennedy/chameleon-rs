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

# Phase 6b: macros, slots and i18n (fixtures using `load:` macros also need the loader).
LATER = {
    '012-translation.pt': 'i18n:translate',
    '015-translation-nested.pt': 'i18n:translate',
    '016-explicit-translation.pt': 'i18n:translate on tal:content / tal:replace',
    '018-translation-nested-dynamic.pt': 'i18n:translate',
    '021-translation-domain.pt': 'i18n:domain',
    '032-master-template.pt': 'metal:define-macro',
    '033-use-macro-trivial.pt': 'metal:use-macro',
    '034-use-template-as-macro.pt': 'metal:use-macro',
    '035-use-macro-with-fill-slot.pt': 'metal:use-macro',
    '036-use-macro-inherits-dynamic-scope.pt': 'metal:use-macro',
    '037-use-macro-local-variable-scope.pt': 'metal:use-macro',
    '038-use-macro-globals.pt': 'metal:use-macro',
    '040-macro-using-template-symbol.pt': 'metal:define-macro',
    '041-translate-nested-names.pt': 'i18n:translate',
    '042-use-macro-fill-footer.pt': 'metal:use-macro',
    '043-macro-nested-dynamic-vars.pt': 'metal:define-macro',
    '046-extend-macro.pt': 'metal:define-macro',
    '047-use-extended-macro.pt': 'metal:use-macro',
    '048-use-extended-macro-fill-original.pt': 'metal:use-macro',
    '050-define-macro-and-use-not-extend.pt': 'metal:define-macro',
    '051-use-non-extended-macro.pt': 'metal:use-macro',
    '052-i18n-domain-inside-filled-slot.pt': 'metal:define-macro',
    '060-macro-with-multiple-same-slots.pt': 'metal:define-macro',
    '061-fill-one-slot-but-two-defined.pt': 'metal:use-macro',
    '065-use-macro-in-fill.pt': 'metal:use-macro',
    '066-load-expression.pt': 'metal:use-macro',
    '069-translation-domain-and-macro.pt': 'metal:use-macro',
    '070-translation-domain-and-use-macro.pt': 'i18n:domain',
    '075-nested-macros.pt': 'metal:define-macro',
    '076-nested-macro-override.pt': 'metal:use-macro',
    '077-i18n-attributes.pt': 'i18n:domain',
    '079-implicit-i18n.pt': 'i18n:translate',
    '081-load-spec.pt': 'metal:use-macro',
    '082-load-spec-computed.pt': 'metal:use-macro',
    '083-template-dict-to-macro.pt': 'metal:use-macro',
    '085-nested-translation.pt': 'i18n:domain',
    '086-self-closing.pt': 'metal:use-macro',
    '089-load-fallback.pt': 'metal:use-macro',
    '120-translation-context.pt': 'i18n:translate',
    '121-translation-comment.pt': 'i18n:translate',
    '124-translation-target.pt': '`default` inside i18n:target',
    '125-macro-translation-ordering.pt': 'metal:define-macro',
    '128-translation-macro-name.pt': 'metal:define-macro',
    '238-macroname.pt': 'metal:define-macro',
    'multinode-implicit-i18n.pt': 'Implicit translation (implicit_i18n_translate)',
}


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
    options = {
        'strict': False,
        'implicit_i18n_translate': implicit,
        'implicit_i18n_attributes': ('alt', 'title') if implicit else (),
        'enable_data_attributes': 'data-attributes' in filename,
        'keep_source': True,
    }
    if engine == 'chameleon':
        module: Any = chameleon.zpt.template
        loader = chameleon.loader.TemplateLoader(INPUTS)
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
