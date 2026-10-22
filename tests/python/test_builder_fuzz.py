"""Generated templates through Chameleon's `MacroProgram` and our builder: the same builder
error (type, message, token, offset, location), or the same expressions and names at the
same positions.

The generator is biased towards the attribute languages: random subsets of TAL, METAL, i18n
and meta attributes with tricky clause values (doubled semicolons, entities, odd whitespace,
tuples of names, duplicates, empty values), nested, with `${...}` in text, attributes,
comments and CDATA. CHAMELEON_RS_FUZZ_EXAMPLES raises the example count (default 300).
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_builder import chameleon_inventory, our_inventory

EXAMPLES = int(os.environ.get('CHAMELEON_RS_FUZZ_EXAMPLES', '300'))

WS = st.sampled_from(['', ' ', '  ', '\n', '\t', '\xa0', '　'])
NAME = st.sampled_from(['a', 'b', 'x_1', 'a-b', 'econtext', '__x', 'local', 'global', 'Title', 'title', '1a'])
EXPR = st.sampled_from(
    [
        'x',
        '1',
        "'a;b'",
        'a | b',
        'string:${x}',
        'x &amp;&amp; y',
        '&lt;',
        '&#65;',
        '(1, 2)',
        '',
        ' ',
        'default',
        'attrs',
    ]
)


@st.composite
def define(draw: Any) -> str:
    names = draw(st.one_of(NAME, st.lists(NAME, min_size=1, max_size=3).map(lambda ns: '(' + ', '.join(ns) + ')')))
    scope = draw(st.sampled_from(['', 'local ', 'global ', 'global']))
    return f'{draw(WS)}{scope}{names}{draw(WS)} {draw(EXPR)}'


def joined(part: Any) -> Any:
    return st.lists(part, min_size=1, max_size=3).map(lambda ps: ';'.join(ps)) | st.lists(part, max_size=2).map(
        lambda ps: ';; '.join(ps) + ';'
    )


@st.composite
def tal_attribute(draw: Any) -> str:
    name = draw(NAME)
    return draw(st.sampled_from([f'{name} {draw(EXPR)}', draw(EXPR), f'{name}{draw(WS)}']))


i18n_attribute = st.sampled_from(['a', 'a msg', 'a, b', 'a b c', ' ', 'title t;alt'])

ATTRIBUTES = {
    'tal:define': joined(define()),
    'tal:repeat': joined(define()),
    'tal:condition': EXPR,
    'tal:content': st.sampled_from(['x', 'text x', 'structure x', ' structure  x', 'texts x', 'default']),
    'tal:replace': st.sampled_from(['x', 'structure x', '']),
    'tal:attributes': joined(tal_attribute()),
    'tal:omit-tag': st.sampled_from(['', ' ', 'x', 'not x']),
    'tal:switch': EXPR,
    'tal:case': EXPR,
    'tal:on-error': st.sampled_from(['x', 'structure x', '']),
    'tal:bogus': EXPR,
    'metal:use-macro': st.sampled_from(['m', 'load: a/b', '']),
    'metal:extend-macro': st.sampled_from(['m', '']),
    'metal:define-macro': st.sampled_from(['m', 'n', '']),
    'metal:define-slot': st.sampled_from(['s', '']),
    'metal:fill-slot': st.sampled_from(['s', ' ', 't']),
    'i18n:translate': st.sampled_from(['', 'msg']),
    'i18n:attributes': i18n_attribute,
    'i18n:name': st.sampled_from(['n', ' ', 'm']),
    'i18n:domain': st.just('d'),
    'i18n:target': EXPR,
    'meta:interpolation': st.sampled_from(['true', 'off', 'maybe']),
    'class': st.sampled_from(['c', '${x}', 'a ${x} b', '$${x}']),
    'checked': st.sampled_from(['checked', '${x}', '']),
    'data-tal-content': EXPR,
}


@st.composite
def element(draw: Any, depth: int) -> str:
    tag = draw(st.sampled_from(['p', 'div', 'tal:block', 'metal:m', 'input']))
    names = draw(st.lists(st.sampled_from(sorted(ATTRIBUTES)), max_size=4, unique=True))
    attrs = ''
    for name in names:
        value = draw(ATTRIBUTES[name]).replace('"', '&quot;')
        attrs += f'{draw(st.sampled_from([" ", "\n  "]))}{name}="{value}"'
    if depth <= 0 or draw(st.booleans()):
        return f'<{tag}{attrs}/>' if draw(st.booleans()) else f'<{tag}{attrs}>t</{tag}>'
    children = draw(st.lists(node(depth - 1), max_size=3))
    return f'<{tag}{attrs}>{"".join(children)}</{tag}>'


def node(depth: int) -> Any:
    text = st.sampled_from(['a', ' ${x} ', '\n  ', '$${y}', '<!-- ${c} -->', '<![CDATA[${d}]]>', '${a | b}'])
    return st.one_of(text, element(depth))


def describe(e: BaseException) -> tuple[Any, ...]:
    if hasattr(e, 'offset') and hasattr(e, 'location'):
        cls = next(c.__name__ for c in type(e).__mro__ if c.__name__ in CLASSES)
        return (cls, e.args[0], str(e.token), e.offset, e.location)  # ty: ignore[unresolved-attribute]
    return (type(e).__name__, str(e))


CLASSES = ('LanguageError', 'CompilationError', 'ParseError', 'TranslationError', 'ExpressionError')


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=st.lists(node(3), min_size=1, max_size=4).map(''.join), data_attributes=st.booleans())
def test_generated_templates_build_like_chameleon(body: str, data_attributes: bool) -> None:
    options: dict[str, Any] = {'boolean_attributes': {'checked'}, 'enable_data_attributes': data_attributes}
    try:
        want = chameleon_inventory(body, **options)
    except Exception as e:  # noqa: BLE001 - comparing errors across engines
        with pytest.raises(Exception) as got:
            our_inventory(body, **options)
        assert describe(got.value) == describe(e), body
        return
    assert our_inventory(body, **options) == want, body
