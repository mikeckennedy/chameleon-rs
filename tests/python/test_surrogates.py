"""Lone surrogates in the template source (plan.md, Phase 8; architecture.md, Phase 8
amendments): Chameleon renders them in text, attributes, `string:` and translations, and its
Python expressions fail with `UnicodeEncodeError`. chameleon-rs maps them onto a private-use
block the source does not use and back; every case here is compared with Chameleon.
"""

from __future__ import annotations

import re
from typing import Any

import chameleon
import chameleon_rs
import pytest
from chameleon_rs import exc

S = '\ud800'
T = '\udfff'


ADDRESS = re.compile(r'0x[0-9a-fA-F]+')


def canon(text: str) -> str:
    """Object addresses and the package name differ between the engines (as in test_errors)."""
    return ADDRESS.sub('0x?', text).replace('chameleon_rs.', 'chameleon.')


def tr(msgid: Any, mapping: Any = None, default: Any = None, **kw: Any) -> str:
    return f'[{msgid!r}|{mapping!r}|{default!r}]'


def outcome(engine: Any, body: str, kwargs: dict[str, Any], text: bool = False, **config: Any) -> tuple[str, str]:
    cls = engine.PageTextTemplate if text else engine.PageTemplate
    try:
        return 'ok', cls(body, **config)(**kwargs)
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        return type(e).__name__, canon(str(e))


CASES: list[tuple[str, str, dict[str, Any]]] = [
    ('text', f'<p>a{S}b{T}c</p>', {}),
    ('attribute', f'<p title="a{S}b">x</p>', {}),
    ('attribute name', f'<p data-{S}="1">x</p>', {}),
    ('comment', f'<!-- c{S} --><p/>', {}),
    ('cdata', f'<![CDATA[ c{S} ]]><p/>', {}),
    ('script', f'<script>var a = "{S}";</script>', {}),
    ('processing instruction', f'<?xml-stylesheet a="{S}"?><p/>', {}),
    ('doctype', f'<!DOCTYPE html "{S}"><p/>', {}),
    ('string content', f'<p tal:content="string:a{S}b">x</p>', {}),
    ('string interpolation', f'<p>${{string:a{S}}}</p>', {}),
    ('string with names', f'<p tal:content="string:{S}${{x}}$x$${S}">x</p>', {'x': 'v'}),
    ('string define', f'<p tal:define="x string:{S}" tal:content="x">x</p>', {}),
    ('string attributes', f'<p tal:attributes="title string:t{S}">x</p>', {}),
    ('string replace', f'<p tal:replace="string:r{S}">x</p>', {}),
    ('string structure', f'<p tal:content="structure string:<b>{S}</b>">x</p>', {}),
    ('string pipe', f'<p tal:content="nothing | string:p{S}">x</p>', {}),
    ('text after expression', f'<p>${{x}}{S}</p>', {'x': 1}),
    ('attribute with expression', f'<p title="${{x}}{S}">x</p>', {'x': 1}),
    ('attribute default', f'<p title="d{S}" tal:attributes="title default">x</p>', {}),
    ('attribute dict', f'<p tal:attributes="d" data-{S}="1" title="t{S}">x</p>', {'d': {'title': None, 'x': 'y'}}),
    ('macro name', f'<p metal:define-macro="m{S}">x</p>', {}),
    ('on-error', f'<p tal:on-error="string:E{S}">{S}${{1/0}}</p>', {}),
    ('values too', f'<p>{S}${{x}}</p>', {'x': '\udc80'}),
    ('private use characters kept', f'<p>{S}</p>', {}),
    ('private use characters in every aligned block', f'<p>{S}</p>', {}),
    # Python expressions: `UnicodeEncodeError` from the parser, with its position.
    ('python content', f'<p tal:content="\'a{S}\'">x</p>', {}),
    ('python interpolation', f'<p>${{"a{S}"}}</p>', {}),
    ('python condition', f'<p tal:condition="\'{S}\'">x</p>', {}),
    ('python define', f'<p tal:define="x \'{S}\'">x</p>', {}),
    ('python repeat', f'<p tal:repeat="x \'{S}\'">x</p>', {}),
    ('python attribute', f'<p tal:attributes="title \'{S}\'">x</p>', {}),
    ('python in string', f'<p tal:content="string:${{\'{S}\'}}">x</p>', {}),
    ('not', f'<p tal:condition="not: \'{S}\'">x</p>', {}),
    ('exists', f'<p tal:condition="exists: \'{S}\'">x</p>', {}),
    ('structure', f'<p tal:content="structure \'{S}\'">x</p>', {}),
    ('code block', f'<?python x = "{S}" ?><p>${{x}}</p>', {}),
    ('python with whitespace', f'<p tal:content="  \'ab{S}\'  ">x</p>', {}),
    # Character references to lone surrogates.
    ('entity in text', '<p title="a&#xD800;b">x&#xD800;y</p>', {}),
    ('entity in string', '<p tal:content="string:a&#xD800;&#56320;">x</p>', {}),
    ('entity in python', '<p tal:content="python:\'&#xD800;\'">x</p>', {}),
    ('entity in interpolation', '<p>${"&#xD800;"}</p>', {}),
    ('entity and literal', f'<p tal:content="string:{S}&#xDBFF;">x</p>', {}),
    ('entity with Unicode digits', '<p tal:content="string:&#\u0665\u0665\u0662\u0669\u0666;">x</p>', {}),
    ('entity with an underscore', '<p tal:content="string:&#5_5296;&#xd800;">x</p>', {}),
    ('decimal entity that is not a surrogate', '<p tal:content="string:&#55;&#57344;">x</p>', {}),
    # Errors elsewhere in a template with lone surrogates: the message shows the source.
    ('syntax error', f'<p>{S} ${{1 +}}</p>', {}),
    ('render error', f'<p>{S}\n${{y}}</p>', {}),
    ('render error in string', f'<p tal:content="string:{S}${{y}}">x</p>', {}),
    ('missing macro', f'<p metal:use-macro="nothing">{S}</p>', {}),
]

I18N_CASES: list[tuple[str, str, dict[str, Any]]] = [
    ('static', f'<p i18n:translate="">hello {S} world</p>', {}),
    ('msgid', f'<p i18n:translate="id{S}">hello</p>', {}),
    ('attribute', f'<p title="t{S}" i18n:attributes="title">x</p>', {}),
    ('attribute msgid', f'<p title="t" i18n:attributes="title m{S}">x</p>', {}),
    ('name', f'<p i18n:translate="">a{S} <b i18n:name="n{S}">${{x}}</b> c</p>', {'x': 'v'}),
    ('name static', f'<p i18n:translate="">x <i i18n:name="n">y{S}</i></p>', {}),
    ('stream with a value surrogate', f'<p i18n:translate="">{S}<b i18n:name="n">${{x}}</b></p>', {'x': '\udc00'}),
    ('domain and context', f'<p i18n:domain="d{S}" i18n:context="c{S}" i18n:translate="">x</p>', {}),
    ('content', f'<p tal:content="string:c{S}" i18n:translate="">x</p>', {}),
    # Stream text keeps what values wrote: a private-use character, a lone surrogate.
    (
        'name with a private-use value',
        f'<p>{S}<i i18n:translate="">hi <b i18n:name="n">${{v}}</b></i></p>',
        {'v': '\ue001x'},
    ),
    ('value private-use in content', f'<p>{S}<i i18n:translate="">hi ${{v}}</i></p>', {'v': '\ue000'}),
    ('value surrogate, no source surrogate', '<p><i i18n:translate="">hi ${v}</i></p>', {'v': '\ud801'}),
    ('value surrogate in a name', '<p i18n:translate="">a <b i18n:name="n">${v}</b></p>', {'v': '\udc02'}),
    ('capture', '<p tal:attributes="title v" i18n:translate="">${v}</p>', {'v': '\ud803'}),
]


@pytest.mark.parametrize(('name', 'body', 'kwargs'), CASES, ids=[c[0] for c in CASES])
def test_like_chameleon(name: str, body: str, kwargs: dict[str, Any]) -> None:
    assert outcome(chameleon_rs, body, kwargs) == outcome(chameleon, body, kwargs)


@pytest.mark.parametrize(('name', 'body', 'kwargs'), I18N_CASES, ids=[c[0] for c in I18N_CASES])
def test_translations_like_chameleon(name: str, body: str, kwargs: dict[str, Any]) -> None:
    expected = outcome(chameleon, body, kwargs, translate=tr)
    assert outcome(chameleon_rs, body, kwargs, translate=tr) == expected


@pytest.mark.parametrize('body', [f'a{S} ${{x}} b{T}', f'${{"{S}"}}', f'a&#xD800; ${{string:{S}}}'])
def test_text_templates_like_chameleon(body: str) -> None:
    assert outcome(chameleon_rs, body, {'x': 1}, text=True) == outcome(chameleon, body, {'x': 1}, text=True)


@pytest.mark.parametrize('strict', [True, False])
def test_not_strict_like_chameleon(strict: bool) -> None:
    body = f'<p>{S}${{1 +}}</p><p tal:content="string:{S}">x</p>'
    assert outcome(chameleon_rs, body, {}, strict=strict) == outcome(chameleon, body, {}, strict=strict)


def macros(engine: Any, use_surrogates: bool, define_surrogates: bool) -> tuple[str, str]:
    u = S if use_surrogates else 'u'
    d = T if define_surrogates else 'd'
    lib = engine.PageTemplate(
        f'<div metal:define-macro="m">{d}<i metal:define-slot="s">default{d}</i>'
        f'<b tal:content="string:x{d}">x</b></div>'
    )
    page = engine.PageTemplate(
        f'<section>{u}<div metal:use-macro="lib.macros[\'m\']"><em metal:fill-slot="s">fill{u}${{v}}</em></div>'
        f'<div metal:use-macro="lib.macros[\'m\']"/>{u}</section>'
    )
    try:
        return 'ok', page(lib=lib, v='\udc01')
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        return type(e).__name__, canon(str(e))


@pytest.mark.parametrize('use_surrogates', [True, False])
@pytest.mark.parametrize('define_surrogates', [True, False])
def test_macros_across_templates_like_chameleon(use_surrogates: bool, define_surrogates: bool) -> None:
    expected = macros(chameleon, use_surrogates, define_surrogates)
    assert macros(chameleon_rs, use_surrogates, define_surrogates) == expected


def test_macro_of_a_surrogate_template_inside_a_translation_like_chameleon() -> None:
    def run(engine: Any) -> str:
        lib = engine.PageTemplate(f'<b metal:define-macro="m">x{S}y</b>')
        page = engine.PageTemplate('<p i18n:translate="">hi <i metal:use-macro="lib.macros[\'m\']"/></p>', translate=tr)
        return page(lib=lib)

    assert run(chameleon_rs) == run(chameleon)


def test_error_token_like_chameleon() -> None:
    body = f'<p>{S}</p>\n<p tal:content="x.y">{S}</p>'

    def record(engine: Any) -> Any:
        try:
            engine.PageTemplate(body)(x=None)
        except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
            return type(e).__name__, canon(str(e))
        return None

    assert record(chameleon_rs) == record(chameleon)


def test_no_free_private_use_block_is_refused() -> None:
    # Private-use characters every 0x700 code points leave no 2,048 free: compiling refuses
    # the lone surrogate (Chameleon renders it).
    used = ''.join(chr(c) for c in range(0xE000, 0xF900, 0x700))
    body = f'<p>{used}ab{S}c</p>'
    with pytest.raises(exc.UnsupportedError) as e:
        chameleon_rs.PageTemplate(body)
    assert e.value.offset == 3 + len(used) + 2
    assert 'U+D800' in str(e.value)
    # One more free code point between two of them is enough.
    assert chameleon_rs.PageTemplate(f'<p>{S}</p>')() == f'<p>{S}</p>'


def test_programs_with_and_without_surrogates_share_a_render() -> None:
    # A partial-style call: one program's output (with surrogates) inside another's.
    inner = chameleon_rs.PageTemplate(f'<i>{S}</i>')
    outer = chameleon_rs.PageTemplate('<p>${structure: inner()}</p>')
    assert outer(inner=inner) == f'<p><i>{S}</i></p>'
