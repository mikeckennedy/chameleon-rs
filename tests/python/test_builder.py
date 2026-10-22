"""Differential tests for the ZPT program builder (Phase 4) against the reference Chameleon.

- Inventory: every expression and assigned name in the program tree, with Chameleon's
  `Token` position, compared as a multiset with the `Value` objects in Chameleon's own
  `MacroProgram` tree, for every upstream fixture and hand-picked edge cases.
- Compile-time errors: same exception type, message, token, offset and location.
- Rendering of the features Phase 4 lowers to bytecode (define, replace, omit-tag,
  attributes, switch/case, CDATA and comment interpolation, meta:interpolation).
"""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

import chameleon
import chameleon_rs
import chameleon_rs.exc
import pytest
from chameleon import nodes
from chameleon.astutil import Node
from chameleon.utils import read_bytes
from chameleon.zpt.program import MacroProgram
from chameleon.zpt.template import BOOLEAN_HTML_ATTRIBUTES
from chameleon_rs._native import _testing

INPUTS = Path(__file__).parents[1] / 'upstream' / 'chameleon_tests' / 'inputs'

Item = tuple[str, str, int | None]


def chameleon_inventory(body: str, **options: Any) -> collections.Counter[Item]:
    """`(kind, text, pos)` for every `Value` (and `i18n:target`, code block) and assigned name
    in Chameleon's program tree, each object once."""
    mode = options.pop('mode', 'xml')
    program = MacroProgram(body, mode, '<string>', escape=mode == 'xml', **options)
    seen: set[int] = set()
    found: collections.Counter[Item] = collections.Counter()

    def add(kind: str, token: Any) -> None:
        found[kind, str(token), getattr(token, 'pos', None)] += 1

    def walk(obj: Any) -> None:
        if isinstance(obj, (list, tuple)):
            for item in obj:
                walk(item)
            return
        if not isinstance(obj, Node) or id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, nodes.Value):
            add('value', obj.value)
        elif isinstance(obj, nodes.Target):
            add('value', obj.expression)
        elif isinstance(obj, nodes.CodeBlock):
            add('value', obj.source)
        if isinstance(obj, nodes.Assignment) and not isinstance(obj, nodes.Alias) and obj.names != ['macroname']:
            for name in obj.names:
                add('name', name)
        for field in obj._fields:
            walk(getattr(obj, field, None))

    walk(program.macros)
    return found


def our_inventory(body: str, **options: Any) -> collections.Counter[Item]:
    return collections.Counter(_testing.inventory(body, **options))


def fixture_options(path: Path) -> tuple[str, dict[str, Any]]:
    """The body and builder options the upstream harness compiles a fixture with."""
    body, _, content_type = read_bytes(path.read_bytes(), 'utf-8')
    options: dict[str, Any] = {}
    if path.suffix == '.txt':
        options['mode'] = 'text'
    if (content_type or 'text/html') != 'text/xml':
        options['boolean_attributes'] = set(BOOLEAN_HTML_ATTRIBUTES)
        body = body.replace('\r\n', '\n').replace('\r', '\n')
    if 'implicit-i18n' in path.name:
        options['implicit_i18n_translate'] = True
        options['implicit_i18n_attributes'] = {'alt', 'title'}
    if 'data-attributes' in path.name:
        options['enable_data_attributes'] = True
    return body, options


FIXTURES = sorted(p for p in INPUTS.iterdir() if p.suffix in ('.pt', '.txt'))


@pytest.mark.parametrize('path', FIXTURES, ids=[p.name for p in FIXTURES])
def test_fixture_inventory(path: Path) -> None:
    body, options = fixture_options(path)
    assert our_inventory(body, **options) == chameleon_inventory(body, **options)


INVENTORY_CASES = [
    '<p tal:define="x 1;y 2;z 3; econtext_ 1">x</p>',
    '<p tal:define="(a,  b) items; global c d;; e">x</p>',
    '<p tal:repeat="(a, b) xs" tal:condition="c &amp;&amp; d">x</p>',
    '<p tal:attributes="title 1; Title 2; d" a="x" title="t">x</p>',
    '<p tal:attributes="href &amp;amp; x" class="${y}">x</p>',
    '<input checked tal:attributes="checked x; selected y" disabled="${z}"/>',
    '<p tal:switch="x"><b tal:case="1">1</b><b tal:case="default">d</b></p>',
    '<p tal:omit-tag=" x ">a</p><tal:b omit-tag="y">b</tal:b>',
    '<p tal:content="structure x" tal:on-error="text y">a</p>',
    '<p tal:replace=" text x ">a</p>',
    '<div metal:use-macro="load: x/y"><p metal:fill-slot="a" tal:content="b">c</p></div>',
    '<div metal:extend-macro="m"><b metal:fill-slot="x">${y}</b></div><i metal:fill-slot="z">q</i>',
    '<div metal:define-macro="m"><span metal:define-slot="s">${d}</span></div>',
    '<p i18n:translate="" i18n:target="lang"><b i18n:name="n">${x}</b></p>',
    '<p>é ${a} $${b} ${c | d}</p><!-- ${e} --><![CDATA[${f}]]>',
    '<?python x = 1 ?><?php ${g} ?>',
    '<p meta:interpolation="false">${a}<b meta:interpolation="true">${b}</b></p>',
    '<p data-tal-content="x" data-tal-define="y 1">z</p>',
    '<tal:block repeat="i items">${i}</tal:block>',
]


@pytest.mark.parametrize('body', INVENTORY_CASES)
def test_edge_case_inventory(body: str) -> None:
    options: dict[str, Any] = {'boolean_attributes': set(BOOLEAN_HTML_ATTRIBUTES)}
    if 'data-' in body:
        options['enable_data_attributes'] = True
    assert our_inventory(body, **options) == chameleon_inventory(body, **options)


# Every compile-time error Chameleon's builder, parser and compiler can raise for a template,
# each case reaching a different check (several from the upstream suite's `test_errors`,
# `test_double_underscore_variable`, `test_compiler_internals_are_disallowed`,
# `test_syntax_error_in_strict_mode` and `test_tag_mismatch`).
ERRORS: list[tuple[str, str, dict[str, Any]]] = [
    # Upstream `test_errors`.
    ('attributes-dummy', '<tal:dummy attributes="dummy \'dummy\'" />', {}),
    ('i18n-attributes-comma', '<tal:dummy i18n:attributes="foo, bar" />', {}),
    ('repeat-key-value', '<tal:dummy repeat="key,value mydict.items()">', {}),
    (
        'duplicate-i18n-name',
        '<tal:dummy><p i18n:translate="mymsgid">\n<span i18n:name="repeat"/><span i18n:name="repeat"/>\n</p></tal:dummy>',
        {},
    ),
    ('name-outside-translation', '<tal:dummy>\n<span i18n:name="not_in_translation"/>\n</tal:dummy>', {}),
    ('double-underscore', '<div tal:define="__dummy \'foo\'">${__dummy}</div>', {}),
    ('econtext', '<d tal:define="econtext \'foo\'">${econtext}</d>', {}),
    ('rcontext', '<d tal:define="rcontext \'foo\'">${rcontext}</d>', {}),
    ('strict-syntax', "<tal:block replace='bad /// ' />", {}),
    ('tag-mismatch', '\n<div metal:use-macro="layout">\n<div metal:fill-slot="name"></dav>\n</div>\n', {}),
    # program.py
    ('bad-tal', '<p tal:foo="x">d</p>', {}),
    ('bad-metal-before-i18n', '<p i18n:bogus="x" metal:bogus="y">d</p>', {}),
    ('bad-tal-before-metal', '<p metal:bogus="y" tal:bogus="x">d</p>', {}),
    ('bad-i18n', '<p i18n:bogus="x">d</p>', {}),
    ('dynamic-attrs-on-tal', '<tal:b attributes="a b">x</tal:b>', {}),
    ('script', '<p tal:script="x">d</p>', {}),
    ('content-and-replace', '<p tal:content="x" tal:replace="y">d</p>', {}),
    ('content-and-translate', '<p tal:content="x" i18n:translate="msg">d</p>', {}),
    ('duplicate-attribute', '<p tal:attributes="a 1; b 2; a 3">d</p>', {}),
    ('duplicate-dict', '<p tal:attributes="x; y">d</p>', {}),
    ('i18n-attributes-illegal', '<p i18n:attributes="a b c">d</p>', {}),
    ('i18n-attributes-blank', '<p i18n:attributes="a; ;b">d</p>', {}),
    ('i18n-attributes-twice', '<p i18n:attributes="a;b x; a">d</p>', {}),
    ('invalid-define', '<p tal:define="a 1; b 2; c">x</p>', {}),
    ('invalid-define-names', '<p tal:define="(a,b c">x</p>', {}),
    ('case-without-switch', '<p tal:case="1">x</p>', {}),
    ('repeat-invalid', '<p tal:repeat="x">d</p>', {}),
    ('repeat-two-clauses', '<p tal:repeat="a b; c d">x</p>', {}),
    ('fill-slot-empty', '<div metal:use-macro="m"><p metal:fill-slot=" ">x</p></div>', {}),
    ('fill-slot-without-use', '<p metal:fill-slot="a">x</p>', {}),
    (
        'fill-slot-and-define-macro',
        '<div metal:use-macro="m"><p metal:fill-slot="a" metal:define-macro="b"/></div>',
        {},
    ),
    ('bad-interpolation', '<p meta:interpolation="maybe">x</p>', {}),
    ('data-prefix', '<p data-foo-bar="1">x</p>', {'enable_data_attributes': True}),
    ('entity-int', '<p tal:content="&#xzz;">x</p>', {}),
    ('entity-chr', '<p tal:content="&#x110000;">x</p>', {}),
    ('entity-in-interpolation', '<p>${&#1x;}</p>', {}),
    ('trim-none-suffix', '<p>t</p/x>', {'trim_attribute_space': True}),
    # compiler.py
    ('repeat-econtext', '<p tal:repeat="econtext x">x</p>', {}),
    ('define-third-part', '<p tal:define="x 1;y 2;z 3; econtext 1">x</p>', {}),
    ('define-tuple-name', '<p tal:define="(a, __b) x">x</p>', {}),
    ('own-switch-case', '<p tal:switch="1" tal:case="1">x</p>', {}),
    ('case-in-macro', '<div tal:switch="1"><div metal:define-macro="m"><b tal:case="1">x</b></div></div>', {}),
    ('expression-in-macro-first', '<p tal:content="1 +">x</p><div metal:define-macro="m">${2 +}</div>', {}),
    ('fallback-before-element', '<p tal:content="1 +" tal:on-error="2 +">x</p>', {}),
    ('content-before-children', '<p tal:content="1 +">${2 +}</p>', {}),
    ('replace-before-attributes', '<p tal:replace="1 +" a="${2 +}">x</p>', {}),
    ('dict-before-attributes', '<p a="${1 +}" tal:attributes="2 +">x</p>', {}),
    ('slots-before-macro', '<div metal:use-macro="1 +"><p metal:fill-slot="a">${2 +}</p></div>', {}),
    ('builder-before-expressions', '<p>${1 +}</p><p tal:case="1">x</p>', {}),
    ('omit-tag-syntax', '<p tal:omit-tag="1 +">x</p>', {}),
    ('interpolation-syntax', '<p a="${1 +}">x</p>', {}),
    ('pipe-syntax', '<p tal:content="a | 1 +">x</p>', {}),
    ('unknown-type', '<p>${foo: 1}</p>', {}),
]


def describe(e: BaseException) -> tuple[Any, ...]:
    """What must match: the class name, and for template errors the message, token,
    offset and location."""
    names = [c.__name__ for c in type(e).__mro__]
    kind = next(n for n in names if n in KNOWN)
    if hasattr(e, 'offset') and hasattr(e, 'location') and not isinstance(e, SyntaxError):
        return (kind, e.args[0], str(e.token), e.offset, e.location)  # ty: ignore[unresolved-attribute]
    return (kind, str(e))


KNOWN = (
    'ExpressionError',
    'LanguageError',
    'TranslationError',
    'ParseError',
    'CompilationError',
    'KeyError',
    'AssertionError',
    'ValueError',
    'TypeError',
    'AttributeError',
    'LookupError',
)


@pytest.mark.parametrize(('source', 'options'), [c[1:] for c in ERRORS], ids=[c[0] for c in ERRORS])
def test_compile_error_matches(source: str, options: dict[str, Any]) -> None:
    with pytest.raises(Exception) as want:
        chameleon.PageTemplate(source, **options)
    with pytest.raises(Exception) as got:
        chameleon_rs.PageTemplate(source, **options)
    if isinstance(got.value, chameleon_rs.exc.UnsupportedError):
        pytest.fail(f'unsupported: {got.value}')
    assert describe(got.value) == describe(want.value)


def test_non_strict_mode_defers_syntax_errors() -> None:
    # Chameleon raises the error when the expression is reached (the upstream test).
    body = "<tal:block replace='bad /// ' />"
    template = chameleon_rs.PageTemplate(body, strict=False)
    with pytest.raises(chameleon_rs.exc.ExpressionError) as error:
        template()
    assert body[error.value.offset :].startswith('bad ///')


class Html:
    def __init__(self, s: str) -> None:
        self.s = s

    def __html__(self) -> str:
        return self.s


class Weird:
    """`==` returns a truthy non-bool."""

    def __eq__(self, other: object) -> Any:
        return 1

    __hash__ = object.__hash__


RENDERS: list[tuple[str, str, dict[str, Any]]] = [
    # tal:define
    ('define', '<p tal:define="a 1; b a + 1">${a}${b}</p>', {}),
    ('define-restore', '<p tal:define="x 1">${x}</p>${x}', {'x': 'outer'}),
    ('define-delete', '<p tal:define="x 1">${x}</p>${x | "gone"}', {}),
    ('define-global', '<p tal:define="global g 1">${g}</p>${g}', {}),
    ('define-global-then-local', '<p tal:define="global g 1; g 2">${g}</p>${g}', {}),
    ('define-tuple', '<p tal:define="(a, b) (1, 2)">${a}${b}</p>', {}),
    ('define-same-name-twice', '<p tal:define="a 1; a a + 1">${a}</p>${a}', {'a': 0}),
    ('define-semicolons', "<p tal:define=\"s 'a;; b'; t '&amp;'\">${s}${t}</p>", {}),
    ('define-rcontext', '<p tal:define="global g 7">${rcontext["g"]}</p>', {}),
    ('define-on-repeat', '<i tal:repeat="x xs" tal:define="y x * 2">${y}</i>', {'xs': [1, 2]}),
    ('define-before-condition', '<i tal:define="c 0" tal:condition="c">no</i>|', {}),
    ('define-attrs', '<p a="1" b="${x}" tal:define="d attrs">${d}</p>', {'x': 2}),
    ('attrs-in-content', '<p class="c" tal:content="attrs[\'class\']">x</p>', {}),
    ('attrs-outside', '${attrs}', {'attrs': 'name'}),
    ('attrs-dynamic', '<p tal:attributes="t attrs" a="1">x</p>', {}),
    ('attrs-fallback', '<p q="1">${[k for k in attrs]}</p>', {}),
    ('define-unpack-error', '<p tal:define="(a, b) (1, 2, 3)">x</p>', {}),
    # tal:replace
    ('replace', '<p tal:replace="x">y</p>', {'x': '<b>'}),
    ('replace-structure', '<p tal:replace="structure x">y</p>', {'x': '<b>'}),
    ('replace-none', '<p tal:replace="None">y</p>|', {}),
    ('replace-default', '<p tal:replace="default">y ${z}</p>', {'z': 1}),
    ('replace-with-attributes', '<p tal:replace="default" tal:attributes="a 1">y</p>', {}),
    ('replace-repeat', '<i tal:repeat="x xs" tal:replace="x">y</i>', {'xs': [1, 2]}),
    # tal:omit-tag
    ('omit-empty', '<p tal:omit-tag="">a</p>', {}),
    ('omit-true', '<p tal:omit-tag="x" a="${x}">a</p>', {'x': 1}),
    ('omit-false', '<p tal:omit-tag="x" a="${x}">a</p>', {'x': 0}),
    ('omit-empty-tag', '<br tal:omit-tag="x"/>|<br tal:omit-tag="not x"/>', {'x': 1}),
    ('omit-on-tal', '<tal:b omit-tag="x">a</tal:b>', {'x': 0}),
    ('omit-content', '<p tal:omit-tag="x" tal:content="y">a</p>', {'x': 0, 'y': 'c'}),
    ('omit-repeat', '<p tal:repeat="x xs" tal:omit-tag="x">${x}</p>', {'xs': [0, 1, 0]}),
    ('omit-whitespace-expr', '<p tal:omit-tag="  ">a</p>', {}),
    # tal:attributes
    ('attributes', '<a tal:attributes="href x; title y">t</a>', {'x': '/a&b', 'y': '"q"'}),
    ('attributes-override', '<a href="/old" tal:attributes="href x">t</a>', {'x': '/new'}),
    ('attributes-none', '<a href="/old" tal:attributes="href None">t</a>', {}),
    ('attributes-default', '<a href="/old" tal:attributes="href default">t</a>', {}),
    ('attributes-default-new', '<a tal:attributes="href default">t</a>', {}),
    ('attributes-quotes', '<a href=\'/old\' tal:attributes="href x">t</a>', {'x': 'a\'b"c'}),
    ('attributes-unquoted', '<a href=old tal:attributes="href x">t</a>', {'x': 'a&b'}),
    ('attributes-valueless', '<a href tal:attributes="href x">t</a>', {'x': 'v'}),
    ('attributes-case', '<a HREF="/old" tal:attributes="href x">t</a>', {'x': '/n'}),
    ('attributes-quirk', '<p tal:attributes="title 1; Title 2" a="x" b="y">x</p>', {}),
    ('attributes-quirk-empty', '<p tal:attributes="title 1; Title 2">x</p>', {}),
    ('attributes-int', '<a tal:attributes="n 5; f 1.5">t</a>', {}),
    ('attributes-html', '<a tal:attributes="h x">t</a>', {'x': Html('<b>')}),
    ('attributes-entities', '<a tal:attributes="t \'&amp;amp;\'">t</a>', {}),
    ('attributes-semicolons', '<a tal:attributes="t \'a;;b\'; u 1">t</a>', {}),
    ('attributes-false', '<a tal:attributes="t x">t</a>', {'x': False}),
    ('attributes-boolean-true', '<input tal:attributes="checked x"/>', {'x': 'yes'}),
    ('attributes-boolean-false', '<input checked="checked" tal:attributes="checked x"/>', {'x': ''}),
    ('attributes-boolean-default', '<input checked="on" tal:attributes="checked default"/>', {}),
    ('attributes-boolean-valueless', '<input checked tal:attributes="checked x"/>', {'x': 1}),
    ('attributes-boolean-none', '<input checked tal:attributes="checked None"/>', {}),
    ('boolean-interp-true', '<input checked="${x}" disabled="${y}">', {'x': 'yes', 'y': ''}),
    ('boolean-interp-zero', '<input checked="${x}">', {'x': 0}),
    ('boolean-interp-parts', '<input checked="${x}${y}" selected="a${y}">', {'x': 0, 'y': None}),
    ('boolean-interp-html-empty', '<input checked="${x}">', {'x': Html('')}),
    ('boolean-interp-default', '<input checked="${default}">', {}),
    ('boolean-xml', '<?xml version="1.0"?>\n<input checked="${x}"/>', {'x': 'yes'}),
    ('dict', '<p tal:attributes="x">t</p>', {'x': {'a': 1, 'b': '<"', 'c': None}}),
    ('dict-boolean', '<p tal:attributes="x">t</p>', {'x': {'checked': 0, 'selected': 2, 'a': 1}}),
    ('dict-filters-before', '<p a="1" b="${y}" tal:attributes="x" c="3">t</p>', {'x': {'a': 'A', 'c': 'C'}, 'y': 2}),
    ('dict-filters-dynamic', '<p tal:attributes="a 1; x">t</p>', {'x': {'a': 'A'}}),
    ('dict-two', '<p tal:attributes="x; b 2" c="3">t</p>', {'x': {'b': 'B', 'c': 'C', 'd': 'D'}}),
    ('dict-html', '<p tal:attributes="x">t</p>', {'x': {'h': Html('<i>')}}),
    ('dict-not-a-dict', '<p tal:attributes="x">t</p>', {'x': None}),
    ('dict-omitted', '<p tal:omit-tag="1" tal:attributes="x">t</p>', {'x': None}),
    ('dict-filter-not-iterable', '<p a="1" tal:attributes="x">t</p>', {'x': 5}),
    # Found by the render fuzz: a `None` value never consults the filter.
    ('boolean-none-skips-filter', '<p checked="${e}${n}" tal:attributes="x"></p>', {'x': 1, 'e': '', 'n': None}),
    # tal:switch / tal:case
    ('switch', '<p tal:switch="x"><b tal:case="1">a</b><b tal:case="2">b</b></p>', {'x': 2}),
    ('switch-default', '<p tal:switch="x"><b tal:case="1">a</b><b tal:case="default">d</b></p>', {'x': 3}),
    ('switch-first-wins', '<p tal:switch="x"><b tal:case="1">a</b><b tal:case="1">b</b></p>', {'x': 1}),
    ('switch-default-after-match', '<p tal:switch="1"><b tal:case="1">a</b><b tal:case="default">d</b></p>', {}),
    ('switch-weird-eq', '<p tal:switch="x"><b tal:case="2">a</b></p>', {'x': Weird()}),
    ('switch-nested', '<p tal:switch="1"><b tal:case="1"><i tal:switch="2"><u tal:case="2">x</u></i></b></p>', {}),
    (
        'switch-in-repeat',
        '<i tal:repeat="x xs" tal:switch="x"><b tal:case="1">one</b><b tal:case="default">${x}</b></i>',
        {'xs': [1, 2]},
    ),
    ('case-default-in-body', '<p tal:switch="1"><b tal:case="1">${default is not None}</b></p>', {}),
    ('case-on-repeat', '<p tal:switch="1"><b tal:repeat="x xs" tal:case="x">${x}</b></p>', {'xs': [1, 2]}),
    # Interpolation in CDATA, comments, processing instructions, meta:interpolation.
    ('cdata', '<![CDATA[ ${x} & ${y} ]]>', {'x': '<b>', 'y': 1}),
    ('cdata-lone', '<![CDATA[${x}]]>', {'x': '<b>'}),
    ('comment-multi', '<!-- a ${x} b ${y} -->', {'x': '<', 'y': None}),
    ('comment-disabled', '<!-- ${x} -->', {'x': 1}),
    ('processing-instruction', '<?php ${x} ?>', {'x': '<'}),
    ('meta-interpolation', '<p meta:interpolation="false">${x}<b meta:interpolation="on">${x}</b></p>${x}', {'x': 1}),
    ('meta-interpolation-attrs', '<p meta:interpolation="false" a="${x}"><!-- ${x} --></p>', {'x': 1}),
    ('trim', '<p\n   a="1"\n   b="${x}"  >t</p\n >', {'x': 2}),
    ('windows-newlines', '<span id="s"\r\n      class="foo"\r\n      tal:content="x"/>', {'x': 'bar'}),
    ('data-attributes', '<p data-tal-content="x" data-tal-attributes="a 1">y</p>', {'x': 'c'}),
    ('text-template-like', '<p>${x}</p>', {'x': '<'}),
]

RENDER_OPTIONS = {
    'comment-disabled': {'enable_comment_interpolation': False},
    'trim': {'trim_attribute_space': True},
    'data-attributes': {'enable_data_attributes': True},
}


@pytest.mark.parametrize(('source', 'variables'), [c[1:] for c in RENDERS], ids=[c[0] for c in RENDERS])
def test_render_matches(source: str, variables: dict[str, Any], request: pytest.FixtureRequest) -> None:
    options = RENDER_OPTIONS.get(request.node.callspec.id, {})

    def run(engine: Any) -> Any:
        try:
            return engine.PageTemplate(source, **options)(**variables)
        except Exception as e:  # noqa: BLE001 - comparing exception types across engines
            return e

    want, got = run(chameleon), run(chameleon_rs)
    if isinstance(got, chameleon_rs.exc.UnsupportedError):
        pytest.fail(f'unsupported: {got}')
    if isinstance(want, Exception):
        assert isinstance(got, Exception), f'reference raised {want!r}, we rendered {got!r}'
        names = {c.__name__ for c in type(want).__mro__}
        assert type(got).__name__ in names, f'reference raised {want!r}, we raised {got!r}'
        return
    assert not isinstance(got, Exception), f'we raised {got!r}, reference rendered {want!r}'
    assert got == want


def test_text_template() -> None:
    body = 'Hello ${name}! <b>${x}</b> $${y}\n'
    want = chameleon.PageTextTemplate(body)(name='<you>', x='&')
    assert chameleon_rs.PageTextTemplate(body)(name='<you>', x='&') == want


@pytest.mark.parametrize(
    ('source', 'variables'),
    [
        # `__quote(value, '"', '&quot;', None, None)`: the marker is an ordinary object there.
        ('<p tal:content="python: d.update(a=default)"/><b tal:attributes="d"/>', {'d': {}}),
        ('<p tal:define="d python: {\'a\': 1, \'b\': None}" tal:attributes="d"/>', {}),
    ],
)
def test_marker_in_attribute_dictionary(source: str, variables: dict[str, Any]) -> None:
    want = chameleon.PageTemplate(source)(**{k: dict(v) for k, v in variables.items()})
    assert chameleon_rs.PageTemplate(source)(**{k: dict(v) for k, v in variables.items()}) == want


ATTRS_IDENTITY = [
    # One dict per element, shared by its expressions and by every render (Chameleon's
    # compile-time `Static`): mutations persist into the next render.
    '<p a="1">${attrs.setdefault("n", []).append(1) or len(attrs["n"])}</p>',
    '<p a="1" tal:define="x attrs">${x is attrs}</p>',
    '<p a="1">${[k for k in attrs] is not None}${attrs is attrs}</p>',
    '<p>${attrs.setdefault("n", []).append(1) or len(attrs["n"])}</p><i>${len(attrs.get("n", []))}</i>',
    '<p a="1">${id(attrs) == id(attrs)}</p><p a="1" tal:define="global first attrs">${first is attrs}</p>',
    '<i tal:repeat="x [1, 2]">${attrs.setdefault("n", []).append(x) or attrs["n"]}</i>',
]


@pytest.mark.parametrize('source', ATTRS_IDENTITY)
def test_attrs_is_one_dict_per_element(source: str) -> None:
    want, got = chameleon.PageTemplate(source), chameleon_rs.PageTemplate(source)
    assert [got(), got()] == [want(), want()]


class Nested:
    """`__html__` returning an object whose own `__html__` returns 0."""

    class Zero:
        def __html__(self) -> int:
            return 0

    def __html__(self) -> Any:
        return self.Zero()


class _HtmlOf:
    """`__html__` returning a fixed object."""

    def __init__(self, value: Any) -> None:
        self.value = value

    def __html__(self) -> Any:
        return self.value


@pytest.mark.parametrize(
    'value',
    [
        Nested(),
        _HtmlOf(_HtmlOf('')),
        _HtmlOf(_HtmlOf(0.0)),
        _HtmlOf(_HtmlOf(None)),
        _HtmlOf(_HtmlOf('x')),
        _HtmlOf(_HtmlOf(())),
        _HtmlOf(''),
        _HtmlOf(b''),
        _HtmlOf(0),
        _HtmlOf([]),
        _HtmlOf([0]),
    ],
    ids=repr,
)
@pytest.mark.parametrize('source', ['<input checked="${x}">', '<input checked="a${x}">', '<input checked="${x}${x}">'])
def test_boolean_attribute_tests_the_converted_object(source: str, value: Any) -> None:
    # Chameleon tests the final converted object, not whether anything was written (the
    # Phase 4 approximation): `0` from a nested `__html__` writes `checked="0"`.
    def render(engine: Any) -> Any:
        try:
            return engine.PageTemplate(source)(x=value)
        except Exception as e:  # noqa: BLE001 - comparing exception types
            return type(e).__name__

    assert render(chameleon_rs) == render(chameleon)


def test_attribute_dictionary_membership_is_hashed() -> None:
    # `name in set(...)`: an unhashable name raises TypeError in both engines.
    source = '<p tal:attributes="d"/>'
    with pytest.raises(TypeError):
        chameleon.PageTemplate(source)(d=_Unhashable())
    with pytest.raises(TypeError):
        chameleon_rs.PageTemplate(source)(d=_Unhashable())


class _Unhashable:
    def items(self) -> list[tuple[Any, Any]]:
        return [([], 'x')]


@pytest.mark.parametrize('cls', ['PageTemplate', 'PageTextTemplate'])
@pytest.mark.parametrize(
    'source',
    ['<!-- ${x}😀', '<!--${x}éab', '<!--${x}é', '<!--${', '<!--${x}😀😀😀', '<!--é${x}é-->', '<!-- ${x} -->'],
)
def test_comment_interpolation_slices_code_points(cls: str, source: str) -> None:
    # `node[4:-3]` and `node[-3:]` are code points: an unclosed comment ending in multi-byte
    # text once sliced a char in two (a panic, `InternalError`), or overlapped the prefix.
    assert getattr(chameleon_rs, cls)(source)(x=1) == getattr(chameleon, cls)(source)(x=1)
