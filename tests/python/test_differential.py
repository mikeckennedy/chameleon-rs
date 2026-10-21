"""Differential tests: chameleon_rs.PageTemplate against the reference chameleon.PageTemplate.

Every case renders the same template with the same variables through both engines and
requires byte-identical output, or the same exception type when the reference raises.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

import chameleon
import chameleon_rs
import pytest


class StrEnum(str, enum.Enum):
    A = 'a<b'


class Html:
    def __html__(self) -> str:
        return '<b>raw & "html"</b>'


class HtmlNone:
    def __html__(self) -> None:
        return None


class WithDefault:
    default = 'DEF<&>'


class Plain:
    def __str__(self) -> str:
        return 'plain <str> & "q"'


class StrSub(str):
    pass


class Item:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class GetItemOnly:
    def __getitem__(self, key: str) -> str:
        if key == 'present':
            return 'from getitem'
        raise KeyError(key)


@dataclass
class Video:
    id: str
    title: str
    author: str
    views: int


VIDEOS = [Video(f'v{i}', f'Title {i} & "<b>"', f'Author {i % 3}', 1_000_000 - i * 37) for i in range(4)]


CASES: list[tuple[str, str, dict[str, Any]]] = [
    # Static markup.
    ('static', '<div class="a">\n  <p>hi</p>\n</div>', {}),
    ('static-quotes', '<p a="1" b=\'2\' c d=e>x</p>', {}),
    ('static-dollars', '<p a="$$" b="$${x}">$$ $${x} $$$${x} $x ${</p>', {'x': 1}),
    ('end-tag-space', '<p >a</p ><b\n>c</b\n>', {}),
    ('empty-tags', '<br/><br /><img src="x"/>', {}),
    ('unclosed', '<p>unclosed <b>bold</p>', {}),
    ('lone-lt', 'a < b <p>x</p>', {}),
    ('doctype', '<!DOCTYPE html>\n<html><p>${x}</p></html>', {'x': 1}),
    ('doctype-subset', '<!DOCTYPE a [ <!ENTITY b "c"> ]><a/>', {}),
    ('xml-declaration', '<?xml version="1.0" encoding="utf-8"?>\n<a>$$</a>', {}),
    ('processing-instruction', '<?php echo $$x; ?><p tal:repeat="i [1, 2]">${i}</p>', {}),
    ('cdata', '<![CDATA[ <b> $$ ]]>', {}),
    ('unclosed-bracket', '<p>a</p\n<b x="1"', {}),
    ('xmlns-scope', '<a xmlns:foo="u"><b></a><c foo:bar="1">${x}</c>', {'x': 2}),
    ('entities-static', '<p title="&amp;">&lt;&nbsp;</p>', {}),
    ('comment', '<!-- plain --><p>x</p>', {}),
    ('comment-interpolated', '<!-- ${x} -->', {'x': '<&>'}),
    ('comment-hidden', '<!--! hidden --><!--? shown ${x} -->', {'x': 1}),
    ('xmlns-tal', '<p xmlns:tal="http://xml.zope.org/namespaces/tal" tal:content="1">d</p>', {}),
    ('unicode', '<p title="é ${x}">ü ${x} 中文</p>', {'x': 'ß€'}),
    # Interpolation in text.
    ('text-str', '<p>${x}</p>', {'x': 'a<b>&c'}),
    ('text-nul', '<p>${x}</p>', {'x': 'a\0b'}),
    ('text-nul-escaped', '<p>${x}</p>', {'x': 'a&\0b'}),
    ('text-quotes', '<p>${x}</p>', {'x': '"\'"'}),
    ('text-none', '<p>${x}|${x}${x}</p>', {'x': None}),
    ('text-multi', '<p>a ${x} b ${y} c</p>', {'x': 1, 'y': '<'}),
    ('text-braces', '<p>${"}"} ${ {"a": 1}["a"] }</p>', {}),
    ('text-empty-braces', '<p>${}</p>', {}),
    ('text-entities-in-expr', '<p>${x &gt; 1} ${x &lt; 1 and 1} ${"&amp;"}</p>', {'x': 3}),
    ('text-invalid-expr', '<p>${x &amp;&amp; 1}</p>', {'x': 3}),
    ('text-pipe', '<p>${y | z | 1} ${x.a | 2} ${"a\\|b"}</p>', {'x': 1}),
    ('text-python-prefix', '<p>${python: 5} ${y | python: 4}</p>', {}),
    ('text-nested-quotes', "<p>${'it''s'} ${\"a\" 'b'}</p>", {}),
    ('text-multiline', '<p>${x +\n 1}</p>', {'x': 1}),
    # Conversion of values.
    ('int', '<p>${x}</p>', {'x': 42}),
    ('negative-int', '<p>${x}</p>', {'x': -7}),
    ('big-int', '<p>${x}</p>', {'x': 2**70}),
    ('huge-int', '<p>${x}</p>', {'x': -(10**30)}),
    ('float', '<p>${x} ${y} ${z}</p>', {'x': 1.5, 'y': 1e16, 'z': float('inf')}),
    ('bool', '<p a="${x}" b="${y}">${x} ${y}</p>', {'x': True, 'y': False}),
    ('bytes', '<p a="${x}">${x}</p>', {'x': b'x&y<"'}),
    ('bytes-utf8', '<p>${x}</p>', {'x': 'é'.encode()}),
    ('html', '<p a="${x}">${x}</p>', {'x': Html()}),
    ('html-none', '<p a="${x}" b="[${x}]">${x}|</p>', {'x': HtmlNone()}),
    ('str-enum', '<p a="${x}">${x}</p>', {'x': StrEnum.A}),
    ('str-subclass', '<p a="${x}">${x}</p>', {'x': StrSub('a<"b')}),
    ('translate-default-attr', '<p a="${x}">${x}</p>', {'x': WithDefault()}),
    ('plain-object', '<p a="${x}">${x}</p>', {'x': Plain()}),
    ('surrogates', '<p a="${x}">${x}</p>', {'x': '\ud800<\udfff'}),
    ('list', '<p>${x}</p>', {'x': [1, '<']}),
    ('dict', '<p>${x}</p>', {'x': {'a': '<'}}),
    # Attributes.
    ('attr-double', '<a href="${x}">t</a>', {'x': 'a&\'"b\0<>'}),
    ('attr-single', "<a href='${x}'>t</a>", {'x': 'a&\'"b<>'}),
    ('attr-bare', '<a href=${x}>t</a>', {'x': 'a&b'}),
    ('attr-bare-plain', '<a href=${x}>t</a>', {'x': 'ab'}),
    ('attr-none', '<a href="${x}" title="t">t</a>', {'x': None}),
    ('attr-none-multi', '<a href="x${x}y">t</a>', {'x': None}),
    ('attr-default', '<a href="${default}" b="${default}y">t</a>', {}),
    ('attr-multi', '<a class="a ${x} b ${y}">t</a>', {'x': '<', 'y': 2}),
    ('attr-valueless', '<input checked class="${x}">', {'x': 'c'}),
    ('attr-entities-in-expr', '<p a="${x &gt; 1}">t</p>', {'x': 2}),
    ('attr-false', '<p a="${x}">t</p>', {'x': False}),
    ('attr-zero', '<p a="${x}">t</p>', {'x': 0}),
    # tal:content
    ('content', '<p tal:content="x">d</p>', {'x': 'a&\0b<'}),
    ('content-none', '<p tal:content="x">d</p>', {'x': None}),
    ('content-default', '<p tal:content="default">keep <b>${y}</b></p>', {'y': 'me'}),
    ('content-default-cond', '<p tal:content="x or default">keep</p>', {'x': ''}),
    ('content-structure', '<p tal:content="structure x">d</p>', {'x': '<b>&'}),
    ('content-text-key', '<p tal:content="text x">d</p>', {'x': 'a<'}),
    ('content-empty-tag', '<p tal:content="x"/>', {'x': 'z'}),
    ('content-attrs', '<p class="a" tal:content="x" id="b">d</p>', {'x': 'q'}),
    ('content-int', '<p tal:content="x">d</p>', {'x': -5}),
    ('content-float', '<p tal:content="x">d</p>', {'x': 1.0}),
    ('content-bool', '<p tal:content="x">d</p>', {'x': True}),
    ('content-bytes', '<p tal:content="x">d</p>', {'x': b'a<'}),
    ('content-html', '<p tal:content="x">d</p>', {'x': Html()}),
    ('content-entities', '<p tal:content="x &gt; 1">d</p>', {'x': 3}),
    ('content-block', '<div><tal:b content="x">d</tal:b></div>', {'x': 'inner'}),
    # tal:condition
    ('condition-false', '<p tal:condition="x">yes</p><p tal:condition="not x">no</p>', {'x': 0}),
    ('condition-true', '<p tal:condition="x">yes</p>', {'x': [1]}),
    ('condition-content', '<p tal:condition="x" tal:content="y">d</p>', {'x': 0, 'y': 1}),
    ('condition-block', '<tal:b condition="x">shown</tal:b>', {'x': 1}),
    # tal:repeat
    ('repeat', '<ul>\n  <li tal:repeat="i items">${i}</li>\n</ul>', {'items': [1, 2, 3]}),
    ('repeat-indent-text', '<ul>\n  ab <li tal:repeat="i items">${i}</li>\n</ul>', {'items': [1, 2, 3]}),
    ('repeat-empty', '<p tal:repeat="x items">${x}</p>|', {'items': []}),
    ('repeat-none', '<p tal:repeat="x None">${x}</p>|', {}),
    ('repeat-tuple', '<p tal:repeat="x ()">${x}</p>|', {}),
    ('repeat-generator', '<p tal:repeat="x (i * 2 for i in items)">${x}</p>', {'items': [1, 2]}),
    ('repeat-string', '<i tal:repeat="c s">${c}</i>', {'s': 'abc'}),
    ('repeat-unpack', '<p tal:repeat="(a, b) items">${a}${b}</p>|', {'items': [(1, 2), (3, 4)]}),
    ('repeat-restore', '<p>${i}</p><p tal:repeat="i items">${i}</p>${i}', {'items': [1], 'i': 5}),
    (
        'repeat-nested',
        '<div>\n  <p tal:repeat="i items">\n    <b tal:repeat="j items">${i}${j}</b>\n  </p>\n</div>',
        {'items': [1, 2]},
    ),
    (
        'repeat-siblings',
        '<div>\n<p tal:repeat="i items">${i}</p><p tal:repeat="j items">${j}</p></div>',
        {'items': [1, 2]},
    ),
    ('repeat-block', '<div>\n  <tal:b repeat="i items">${i}</tal:b>\n</div>', {'items': [1, 2]}),
    ('repeat-block-prefixed', '<div>\n  <tal:b tal:repeat="i items">${i}</tal:b>\n</div>', {'items': [1, 2]}),
    ('repeat-content', '<div tal:repeat="x items" tal:content="x">d</div>', {'items': [0, None, 'a']}),
    ('repeat-condition-inside', '<p tal:repeat="x items"><b tal:condition="x">${x}</b></p>', {'items': [0, 1]}),
    (
        'repeat-variable',
        (
            '<ul>\n  <li tal:repeat="i items">${repeat.i.index} ${repeat.i.number} ${repeat.i.even} '
            '${repeat.i.odd} ${repeat.i.start} ${repeat.i.end} ${repeat.i.length} ${repeat["i"].index()} '
            '${repeat.i.parity}</li>\n</ul>'
        ),
        {'items': 'abc'},
    ),
    ('repeat-shadow-builtin', '<p tal:repeat="len items">${len}</p>${len("ab")}', {'items': [7]}),
    (
        'repeat-custom-repeat',
        '<p tal:repeat="x items">${x}</p>',
        {'items': [1], 'repeat': chameleon.tal.RepeatDict({})},
    ),
    # A user-supplied `repeat`: any iterable, any length object, unpacked like Python does.
    ('repeat-user-iterable', '<p tal:repeat="i xs">${i}</p>|', {'xs': [1, 2], 'repeat': lambda k, it: (list(it), 2)}),
    ('repeat-user-list-pair', '<p tal:repeat="i xs">${i}</p>|', {'xs': [1, 2], 'repeat': lambda k, it: [iter(it), 2]}),
    ('repeat-user-negative', '<p tal:repeat="i xs">${i}</p>|', {'xs': [1, 2], 'repeat': lambda k, it: (iter(it), -1)}),
    ('repeat-user-float', '<p tal:repeat="i xs">${i}</p>|', {'xs': [1, 2], 'repeat': lambda k, it: (iter(it), 2.5)}),
    ('repeat-user-long', '<p tal:repeat="i xs">${i}</p>|', {'xs': [1, 2], 'repeat': lambda k, it: (iter(it), 5)}),
    ('repeat-user-none-empty', '<p tal:repeat="i xs">${i}</p>|', {'xs': [], 'repeat': lambda k, it: (iter(it), None)}),
    ('repeat-user-none', '<p tal:repeat="i xs">${i}</p>|', {'xs': [1], 'repeat': lambda k, it: (iter(it), None)}),
    (
        'repeat-user-none-two',
        '<p tal:repeat="i xs">${i}</p>|',
        {'xs': [1, 2], 'repeat': lambda k, it: (iter(it), None)},
    ),
    ('repeat-user-bad-pair', '<p tal:repeat="i xs">${i}</p>|', {'xs': [1], 'repeat': lambda k, it: (iter(it),)}),
    # Names and attribute access.
    ('builtin', '<p>${len(x)} ${str}</p>', {'x': 'abc'}),
    ('builtin-override', '<p>${len(x)}</p>', {'x': 'abc', 'len': lambda x: 99}),
    ('lookup-attr-dict', '<p>${x.a} ${x.b}</p>', {'x': {'a': 1, 'b': 2}}),
    ('lookup-attr-method', '<p>${x.keys is not None}</p>', {'x': {'keys': 1}}),
    ('lookup-attr-getitem', '<p>${x.present}</p>', {'x': GetItemOnly()}),
    ('lookup-attr-getitem-missing', '<p>${x.absent | "fallback"}</p>', {'x': GetItemOnly()}),
    ('attribute-chain', '<p>${v.title.upper()}</p>', {'v': VIDEOS[0]}),
    ('folded-constant', '<p>${"{:,}".format(x)} ${" ".join([])}|</p>', {'x': 1234567}),
    ('call-kwargs', '<p>${f(1, b=2)}</p>', {'f': lambda a, b: a + b}),
    ('call-many-args', '<p>${max(1, 5, 3, 2)}</p>', {}),
    ('subscript', '<p>${x[0]} ${x[-1]} ${d["k"]}</p>', {'x': [1, 2], 'd': {'k': '<'}}),
    ('slice', '<p>${x[1:]}</p>', {'x': [1, 2, 3]}),
    ('compare', '<p>${1 < x < 3} ${x == 2} ${x is None} ${x in [2]} ${x not in [2]}</p>', {'x': 2}),
    ('compare-chain-false', '<p>${3 < x < 5}</p>', {'x': 2}),
    ('ifexp', '<p>${"a" if x else "b"}</p>', {'x': 0}),
    ('boolop', '<p>${x and y} ${x or y} ${y and x}</p>', {'x': 0, 'y': 'q'}),
    ('arith', '<p>${x + 1} ${x * 2} ${x / 2} ${x // 2} ${x % 3} ${x ** 2} ${-x} ${~x}</p>', {'x': 7}),
    ('literals', '<p>${(1, 2)} ${[1, x]} ${ {"a": x} }</p>', {'x': '<'}),
    ('fstring-fallback', '<p>${f"{x}!"}</p>', {'x': '<'}),
    ('lambda-fallback', '<p>${(lambda a: a + x)(2)}</p>', {'x': 1}),
    ('comprehension-scope', '<p>${[q for q in [1, 2]]}${q}</p>', {}),
    ('econtext', '<p>${econtext["x"]}</p>', {'x': '<'}),
    # The listing item the benchmark uses.
    (
        'listing',
        (
            '<div class="container videos listing">\n'
            '    <span class="video" tal:repeat="video videos">\n'
            '        <a href="https://www.youtube.com/watch?v=${ video.id }" target="_blank">\n'
            '            <img src="https://img.youtube.com/vi/${ video.id }/maxresdefault.jpg"\n'
            '                 class="img img-responsive ${ \' \'.join([]) }"\n'
            '                 alt="${ video.title }"\n'
            '                 title="${ video.title }">\n'
            '        </a>\n'
            '        <a class="author">${ video.author }</a>\n'
            '        <div class="views">${ "{:,}".format(video.views) } views</div>\n'
            '    </span>\n'
            '</div>\n'
        ),
        {'videos': VIDEOS},
    ),
]


def render_both(source: str, variables: dict[str, Any]) -> tuple[Any, Any]:
    def run(engine: Any) -> Any:
        try:
            return engine.PageTemplate(source)(**variables)
        except Exception as e:  # noqa: BLE001 - comparing exception types across engines
            return e

    return run(chameleon), run(chameleon_rs)


@pytest.mark.parametrize(('source', 'variables'), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_same_output(source: str, variables: dict[str, Any]) -> None:
    want, got = render_both(source, variables)
    if isinstance(want, Exception):
        assert isinstance(got, Exception), f'reference raised {want!r}, we rendered {got!r}'
        # Chameleon wraps render-time errors in a RenderError subclass of the original type.
        names = {c.__name__ for c in type(want).__mro__}
        assert type(got).__name__ in names, f'reference raised {want!r}, we raised {got!r}'
        return
    assert not isinstance(got, Exception), f'we raised {got!r}, reference rendered {want!r}'
    assert got == want


RENDER_ERRORS: list[tuple[str, str, dict[str, Any], type[BaseException]]] = [
    ('undefined-name', '<p>${x}</p>', {}, NameError),
    ('undefined-in-repeat', '<p tal:repeat="i [i]">${i}</p>', {}, NameError),
    ('condition-before-repeat', '<div tal:condition="x" tal:repeat="x items">${x}</div>', {'items': [1]}, NameError),
    ('deleted-after-loop', '<p tal:repeat="i items">${i}</p>${i}', {'items': [1]}, NameError),
    ('lookup-attr-typeerror', '<p>${y.z}</p>', {'y': [1]}, TypeError),
    ('bad-bytes', '<p>${x}</p>', {'x': b'\xff'}, UnicodeDecodeError),
    ('unpack', '<p tal:repeat="(a, b) items">${a}</p>', {'items': [(1, 2, 3)]}, ValueError),
]


@pytest.mark.parametrize(
    ('source', 'variables', 'error'), [c[1:] for c in RENDER_ERRORS], ids=[c[0] for c in RENDER_ERRORS]
)
def test_same_render_error(source: str, variables: dict[str, Any], error: type[BaseException]) -> None:
    want, got = render_both(source, variables)
    assert isinstance(want, error), want
    assert isinstance(got, error), got


COMPILE_ERRORS: list[tuple[str, str, str]] = [
    ('empty-expression', '<p>${ }</p>', 'ExpressionError'),
    ('bad-content', '<p tal:content="1 +">a</p>', 'ExpressionError'),
    ('bad-attribute', '<p tal:foo="x">d</p>', 'CompilationError'),
    ('undefined-prefix', '<svg xlink:href="a"/>', 'KeyError'),
    ('unexpected-end-tag', '<p>a</b>', 'ParseError'),
    ('double-hyphen', '<!-- a -- b -->', 'ParseError'),
    ('nameless-end-tag', '<p>a</</p>', 'AttributeError'),
    ('bad-repeat', '<p tal:repeat="( a, b) x">d</p>', 'LanguageError'),
    ('unknown-type', '<p>${foo: 1}</p>', 'LookupError'),
]


@pytest.mark.parametrize(('source', 'name'), [c[1:] for c in COMPILE_ERRORS], ids=[c[0] for c in COMPILE_ERRORS])
def test_same_compile_error(source: str, name: str) -> None:
    with pytest.raises(Exception) as want:
        chameleon.PageTemplate(source)()
    assert any(c.__name__ == name for c in type(want.value).__mro__), want.value
    if name in ('KeyError', 'AttributeError'):
        # Chameleon raises these bare (an undefined prefix; a tag with no name), and so do we.
        with pytest.raises(type(want.value)) as got:
            chameleon_rs.PageTemplate(source)()
        assert str(got.value) == str(want.value)
        return
    if name == 'LookupError':
        with pytest.raises(LookupError):
            chameleon_rs.PageTemplate(source)()
        return
    with pytest.raises(getattr(chameleon_rs.exc, name)):
        chameleon_rs.PageTemplate(source)()


@pytest.mark.parametrize(
    'source',
    [
        '<p>é\nü ${ 1 + }</p>',
        '<p tal:content=" 1 + ">x</p>',
        '<p tal:content="x | 1 +">x</p>',
        '<p>${ a | 1 + }</p>',
        '<p tal:content="python: 1 +">x</p>',
        '<p tal:define="a 1; b 1 +">x</p>',
    ],
)
def test_error_positions_are_code_points(source: str) -> None:
    # The token is the stripped Python part, at the position Chameleon's slicing gives.
    with pytest.raises(chameleon.exc.ExpressionError) as want:
        chameleon.PageTemplate(source)
    with pytest.raises(chameleon_rs.exc.ExpressionError) as got:
        chameleon_rs.PageTemplate(source)
    assert (got.value.token, got.value.offset, got.value.location) == (
        str(want.value.token),
        want.value.offset,
        want.value.location,
    )


def test_unsupported_features_say_so() -> None:
    for source in (
        '<p metal:use-macro="x"/>',
        '<p i18n:translate="">y</p>',
        '<div metal:define-macro="m">y</div>',
    ):
        with pytest.raises(chameleon_rs.exc.UnsupportedError):
            chameleon_rs.PageTemplate(source)


def test_repeat_backup_happens_before_the_iterable() -> None:
    # The iterable sees the outer binding (backup first, None assigned after evaluation).
    source = '<p tal:repeat="i [i, i]">${i}</p>${i}'
    want, got = render_both(source, {'i': 'outer'})
    assert got == want == '<p>outer</p>\n<p>outer</p>outer'


def test_template_is_reusable_and_isolated() -> None:
    template = chameleon_rs.PageTemplate('<p tal:repeat="x items">${x}</p>')
    assert template(items=[1]) == '<p>1</p>'
    assert template(items=[2, 3]) == '<p>2</p>\n<p>3</p>'
    variables = {'items': [4]}
    template(**variables)
    assert variables == {'items': [4]}
