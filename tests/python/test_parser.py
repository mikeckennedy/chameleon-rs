"""Differential tests: the Rust tokenizer and element parser against Chameleon's.

Chameleon's `iter_xml` / `iter_text` tokens and `ElementParser` items are normalized to plain
data (a `Token` becomes `(text, pos)`, a plain string `(text, None)`) and compared with what
`chameleon_rs._native._testing` returns for the same input, including errors (same type,
message and position). Inputs: every upstream fixture, the real-template corpus, hand-picked
edge cases and Hypothesis-generated markup.

The generated run uses a modest number of examples by default; set
CHAMELEON_RS_FUZZ_EXAMPLES (for example to 100000) for the full differential run.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import chameleon.exc
import pytest
from chameleon.namespaces import PY_NS, XML_NS, XMLNS_NS
from chameleon.parser import ElementParser
from chameleon.tokenize import Token, collector, iter_text, iter_xml
from chameleon.utils import read_encoded
from chameleon.zpt.program import MacroProgram
from chameleon_rs import exc
from chameleon_rs._native import _testing
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_corpus import FILES as CORPUS

FIXTURES = sorted((Path(__file__).parent.parent / 'upstream' / 'chameleon_tests' / 'inputs').iterdir())

ZPT_NAMESPACES: dict[str | None, str] = {**MacroProgram.DEFAULT_NAMESPACES}
ELEMENT_NAMESPACES: dict[str | None, str] = {'xmlns': XMLNS_NS, 'xml': XML_NS}
PARSER_TEST_NAMESPACES: dict[str | None, str] = {'xmlns': XMLNS_NS, 'xml': XML_NS, 'py': PY_NS}

TAG_KEYS = {'prefix', 'name', 'suffix', 'space', 'attrs', 'namespace', 'ns_attrs', 'ns_map'}
ATTR_KEYS = ('space', 'name', 'eq', 'quote', 'value')

Tok = tuple[str, int | None] | None


def tok(t: object) -> Tok:
    if t is None:
        return None
    assert isinstance(t, str), t
    return (str(t), t.pos) if isinstance(t, Token) else (t, None)


def tag(d: dict[str, Any]) -> dict[str, Any]:
    assert set(d) == TAG_KEYS, d.keys()
    attrs = []
    for a in d['attrs']:
        assert set(a) == set(ATTR_KEYS), a.keys()
        attrs.append({k: tok(a[k]) for k in ATTR_KEYS})
    return {
        'prefix': tok(d['prefix']),
        'name': tok(d['name']),
        'suffix': tok(d['suffix']),
        'space': tok(d['space']),
        'attrs': attrs,
        'namespace': tok(d['namespace']),
        'ns_attrs': [((tok(ns), tok(name)), tok(value)) for (ns, name), value in d['ns_attrs'].items()],
        'ns_map': {tok(k): tok(v) for k, v in d['ns_map'].items()},
    }


def item(kind: str, args: tuple[Any, ...]) -> tuple[Any, ...]:
    if kind in ('text', 'comment', 'cdata', 'default'):
        (token,) = args
        return (kind, (tok(token),))
    if kind == 'processing_instruction':
        (d,) = args
        assert set(d) == {'name', 'text'}, d.keys()
        return (kind, ({'name': tok(d['name']), 'text': tok(d['text'])},))
    if kind == 'start_tag':
        (node,) = args
        return (kind, (tag(node),))
    assert kind == 'element', kind
    start, end, children = args
    return (kind, (tag(start), None if end is None else tag(end), [item(*c) for c in children]))


def error(e: BaseException) -> tuple[Any, ...]:
    """What must match when parsing fails: type, message and the token's text and position."""
    if isinstance(e, exc.TemplateError):
        return ('ParseError', e.args[0], e.token, e.offset)
    if isinstance(e, chameleon.exc.TemplateError):
        msg, token = e.args
        return ('ParseError', msg, str(token), token.pos)
    return (type(e).__name__, str(e))


def reference_tokens(body: str, mode: str) -> list[Tok]:
    stream = iter_text(body) if mode == 'text' else iter_xml(body)
    return [tok(t) for t in stream]


def reference_parse(body: str, namespaces: dict[str | None, str], restricted: bool, mode: str) -> Any:
    stream = iter_text(body) if mode == 'text' else iter_xml(body)
    try:
        return [item(*i) for i in ElementParser(stream, namespaces, restricted)]
    except Exception as e:  # noqa: BLE001 - every exception type is part of the comparison
        return error(e)


def our_parse(body: str, namespaces: dict[str | None, str], restricted: bool, mode: str) -> Any:
    try:
        return _testing.parse(body, namespaces, restricted, mode)
    except Exception as e:  # noqa: BLE001 - every exception type is part of the comparison
        return error(e)


def check(
    body: str,
    namespaces: dict[str | None, str] = ZPT_NAMESPACES,
    restricted: bool = True,
    mode: str = 'xml',
) -> None:
    want = reference_tokens(body, mode)
    assert _testing.tokens(body, mode) == want
    assert our_parse(body, namespaces, restricted, mode) == reference_parse(body, namespaces, restricted, mode)


def read(path: Path) -> str | None:
    try:
        return read_encoded(path.read_bytes())
    except (UnicodeDecodeError, LookupError):
        return None


# -- the pattern and the character classes are Python's -------------------------------------


def test_xml_spe_pattern_is_verbatim() -> None:
    for name, pattern in collector.res.items():
        assert _testing.tokenizer_pattern(name) == pattern, name


# The tables are generated on Python 3.15 (scripts/gen_pychars.py, Unicode 17). An older
# interpreter's `re` has an older Unicode database, so it may not match code points assigned
# since; those, and only those, are a known deviation (docs/architecture.md, Phase 3).
TABLES_PYTHON = (3, 15)


@pytest.mark.parametrize(('name', 'pattern'), [('s', r'\s'), ('d', r'\d'), ('w', r'\w')])
def test_character_classes_match_python_re(name: str, pattern: str) -> None:
    rx = re.compile(pattern)
    want = {chr(c) for c in range(sys.maxunicode + 1) if not 0xD800 <= c <= 0xDFFF and rx.fullmatch(chr(c))}
    got = set(_testing.char_class(name))
    if sys.version_info[:2] >= TABLES_PYTHON:
        assert got == want
        return
    assert not want - got, 'the running Python matches code points the tables lack'
    extra = got - want
    assigned = sorted(f'U+{ord(c):04X}' for c in extra if unicodedata.category(c) != 'Cn')
    assert not assigned, f'tables differ on code points assigned in this Python: {assigned[:20]}'


# -- fixtures and the corpus ------------------------------------------------------------------


@pytest.mark.parametrize('path', FIXTURES, ids=[p.name for p in FIXTURES])
def test_upstream_fixture(path: Path) -> None:
    body = read(path)
    if body is None:
        pytest.skip('not decodable the way Chameleon reads files')
    check(body)
    check(body, PARSER_TEST_NAMESPACES, restricted=False)
    check(body, mode='text')


@pytest.mark.skipif(not CORPUS, reason='no template corpus found (set CHAMELEON_RS_CORPUS)')
def test_corpus() -> None:
    # One test for the whole private corpus, so no file names or contents end up in reports.
    failures = []
    for path in CORPUS:
        body = read(path)
        assert body is not None
        try:
            check(body)
        except AssertionError:
            # A short hash, not the name: the corpus is private. Reproduce locally by hashing
            # the paths the same way.
            failures.append(hashlib.sha256(str(path).encode()).hexdigest()[:10])
    assert not failures, f'{len(failures)} of {len(CORPUS)} corpus templates differ (path hashes: {failures[:10]})'


# -- hand-picked edge cases -------------------------------------------------------------------

EDGE_CASES = [
    '',
    'plain text',
    '<',
    '<>',
    '</',
    '</>',
    '< a>',
    '<a',
    '<a b="1"',
    '<a b=1 c d = "x" >',
    '<a\n  b="1"\n/>',
    '<p>a</p >',
    '<p>a</p\n',
    '<p>a<b>c</p>d',
    '<p>a</b>',
    'x<a>y</a></a>',
    '<a b="1" b="2"/>',
    '<a :b="1"/>',
    '<a xmlns:="u" :b="1"/>',
    '<a foo:bar="1"/>',
    '<a xmlns:foo="u"><b foo:bar="1"/></a><c foo:bar="2"/>',
    '<a xmlns:foo="u"><b></a><c foo:bar="1"/>',
    '<a xmlns="u" b="1"><c/></a>',
    '<a xmlns xmlns:x>t</a>',
    '<tal:block tal:content="x" metal:use-macro="y" i18n:translate=""/>',
    '<!-- a -- b -->',
    '<!-- ok --->',
    '<!---->',
    '<!--->',
    '<!-- ${x} -->',
    '<!DOCTYPE html>',
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "x.dtd">',
    '<!DOCTYPE a [ <!ENTITY b "c"> %d; <!-- e --> <?f g?> ]>',
    '<![CDATA[ x ]]>',
    '<![CDATA[ a ]] ]]>',
    '<![CDATA[',
    '<?xml version="1.0" encoding="utf-8"?><a/>',
    '<?xml?>',
    '<?xmlfoo bar?>',
    '<?python x = 1 ?>',
    '<?php echo 1; ?>',
    '<? ?>',
    '<?a',
    '<a b="<c>" d=\'>\'>e</a>',
    '<a b=c\x1cd>',
    '<a b=c　>',
    '<a　b>c</a　b>',
    '<a b="1"\x0b c="2">',
    '<a ٣b="1">',
    '<a ²b="1">',
    "<a b='1\"'>",
    '<a b=`1`>',
    '<a b= >',
    '<a b=>',
    '<a b="x"c="y">',
    '<a  b  =  "x"  >',
    '<a b="1"/ >',
    '<a/b>',
    '<a b\\n="1">',
    '<a b \\t= "1">',
    '<é á="1">x</é>',
    '<@click a="1"/>',
    '<a @click="x" :prop="y" v-on:z="w"/>',
    '<a b="${x}" c="$${y}">${z} $${w}</a>',
    'a & b &amp; &#123; &#x1F; &nbsp;',
    '\r\n<a>\r\n</a>\r\n',
]


@pytest.mark.parametrize('body', EDGE_CASES)
def test_edge_case(body: str) -> None:
    check(body)
    check(body, ELEMENT_NAMESPACES, restricted=False)
    check(body, mode='text')


# Markup tokens over 1 MB of each kind. fancy-regex's fixed backtracking stack overflows
# around here, which is why production matching does not use it; these must still match.
MB = 1_100_000
LARGE_CASES = {
    'comment': '<!--' + 'x' * MB + '-->',
    'comment-multiline': '<!--\n' + ('y' * 79 + '\n') * 14_000 + '-->',
    'cdata': '<![CDATA[' + 'a]b' * (MB // 3) + ']]>',
    'processing-instruction': '<?php ' + 'x' * MB + ' ?>',
    'doctype': '<!DOCTYPE html [' + '<!ENTITY a "b">' * (MB // 15) + ']>',
    'quoted-attribute': '<p tal:condition="False" title="' + 'A' * MB + '">hidden</p>',
    'single-quoted-attribute': "<p title='" + 'A' * MB + "'>x</p>",
    'unquoted-attribute': '<p title=' + 'A' * MB + '>x</p>',
    'many-attributes': '<p' + ''.join(f' a{i}="x"' for i in range(250_000)) + '>x</p>',
    'many-valueless-attributes': '<p' + ' a' * 550_000 + '/>',
    'long-name': '<' + 'n' * MB + '>x</' + 'n' * MB + '>',
    'long-text': 'text ' * (MB // 5),
}


@pytest.mark.parametrize('name', list(LARGE_CASES))
def test_large_tokens(name: str) -> None:
    check(LARGE_CASES[name])


def test_large_attribute_renders_like_chameleon() -> None:
    import chameleon
    import chameleon_rs

    body = LARGE_CASES['quoted-attribute']
    assert chameleon_rs.PageTemplate(body)() == chameleon.PageTemplate(body)() == ''


def test_error_positions() -> None:
    with pytest.raises(exc.ParseError) as err:
        _testing.parse('é\n<!-- a -- b -->', ZPT_NAMESPACES)
    assert (err.value.args[0], err.value.token, err.value.offset, err.value.location) == (
        "The string '--' is not allowed in a comment.",
        '--',
        9,
        (2, 7),
    )
    with pytest.raises(KeyError, match='Undefined namespace prefix: foo.'):
        _testing.parse('<a foo:bar="1"/>', ZPT_NAMESPACES)
    with pytest.raises(AttributeError):
        _testing.parse('</', ZPT_NAMESPACES)


# -- generated markup -------------------------------------------------------------------------

FUZZ_EXAMPLES = int(os.environ.get('CHAMELEON_RS_FUZZ_EXAMPLES', '300'))

Choice = tuple[list[str], list[str]]

# (common, tricky) choices. A "clean" element draws only common ones, so most generated
# documents nest properly and parse deeply; a "dirty" one mixes in the tricky ones.
TAG_NAMES: Choice = (
    ['a', 'div', 'p', 'tal:block', 'metal:x', 'foo:bar', 'span', 'tal:content'],
    ['é', 'a\u3000b', '@click', '1a', '\u0663', '?xml', 'a.b-c_d', 'x:', ':x', 'xmlns', 'xml:lang', 'undeclared:x'],
)
ATTR_NAMES: Choice = (
    ['a', 'class', 'id', 'tal:content', 'tal:repeat', 'xmlns:foo', 'foo:bar', 'xmlns', 'xml:lang', 'i18n:translate'],
    ['é', 'a\u3000b', '@click', ':prop', '1a', '\u0663b', '\u00b2', 'x:', 'undeclared:x', 'b\\n', '"q"', 'a=b'],
)
VALUES = ['', 'x', 'u', '${x}', '$${x}', '<b>', '>', 'a b', '&amp;', '\n', 'a\u3000b', '${', '/', 'urn:x']
VALUE: Choice = (VALUES, ['?>', '-->', ']]>', '"', "'", '`', '\x00'])
SPACES = [' ', '  ', '\n', '\t', '\r', '\r\n', '\x0b', '\x0c', '\x1c', '\x1f', '\x85', '\xa0', '\u2028', '\u3000']
SPACES += ['\u200b', '\u180e']
SPACE: Choice = ([' ', ' ', '\n', '  ', '\t'], SPACES)
EQ: Choice = (['=', ' = ', '\n=\n'], ['\u3000=', '=\u3000', '\x0b='])
TAG_END: Choice = (['>'], [' >', '\n>', '\u3000>', '/ >', '', '\x0b>'])
EMPTY_END: Choice = (['/>', ' />', '\n/>'], ['/', '/\u3000>'])
END_SPACE: Choice = (['', '', ' ', '\n'], ['\u3000', '\x0b', '\x0c'])
END_CLOSE: Choice = (['>'], [''])
MARKUP = ['<', '</', '>', '/>', '/', '=', '"', "'", '`', '<!--', '-->', '--', '-', '<!---->', '<![CDATA[', ']]>']
MARKUP += [']', ']]', '[', '<?', '?>', '?', '<?xml', '<?python', '<?php', '<!DOCTYPE', '<!ENTITY', '<!', '%a;']
MARKUP += ['&amp;', '&#123;', '&#x1F;', '&', ';', '${', '}', '$', '$$', '\\n', '\\', '\u00b2', '\u0301', '\x00']


def pick(draw: st.DrawFn, clean: bool, choice: Choice) -> str:
    common, tricky = choice
    if clean:
        return draw(st.sampled_from(common))
    return draw(st.sampled_from(common) | st.sampled_from(tricky))


@st.composite
def attribute(draw: st.DrawFn, clean: bool) -> str:
    space = pick(draw, clean, SPACE)
    name = pick(draw, clean, ATTR_NAMES)
    value = pick(draw, clean, VALUE) if clean or draw(st.booleans()) else draw(st.text(max_size=4))
    form = draw(st.integers(0, 5))
    if form == 0:
        return f'{space}{name}'
    if form in (1, 2):
        return f'{space}{name}="{value}"'
    if form == 3:
        return f"{space}{name}='{value}'"
    if form == 4:
        return f'{space}{name}={value}' if not clean else f'{space}{name}=x'
    return f'{space}{name}{pick(draw, clean, EQ)}"{value}"'


@st.composite
def element(draw: st.DrawFn, children: st.SearchStrategy[str]) -> str:
    clean = draw(st.integers(0, 6)) > 0
    name = pick(draw, clean, TAG_NAMES)
    attrs = ''.join(draw(st.lists(attribute(clean), max_size=4)))
    form = draw(st.integers(0, 9))
    if form == 0:
        return f'<{name}{attrs}{pick(draw, clean, EMPTY_END)}'
    start = f'<{name}{attrs}{pick(draw, clean, TAG_END)}'
    body = ''.join(draw(st.lists(children, max_size=4)))
    if form == 1:
        return start + body  # never closed
    if not clean and draw(st.booleans()):
        name = pick(draw, clean, TAG_NAMES)  # possibly a mismatched end tag
    return f'{start}{body}</{name}{pick(draw, clean, END_SPACE)}{pick(draw, clean, END_CLOSE)}'


LEAF = st.one_of(
    st.text(max_size=6),
    st.sampled_from(VALUES),
    st.sampled_from(MARKUP),
    st.sampled_from(SPACES),
    st.text(alphabet='<>/!?-[]="\' a:x', max_size=8),
    st.builds(lambda t: f'<!--{t}-->', st.text(alphabet='-> a${}', max_size=6)),
    st.builds(lambda t: f'<![CDATA[{t}]]>', st.text(alphabet=']>[ a${}', max_size=6)),
    st.builds(
        lambda n, t: f'<?{n}{t}?>', st.sampled_from(['xml', 'python', 'php', 'x', '']), st.text(' ?>a=1', max_size=6)
    ),
    st.sampled_from(['<!DOCTYPE html>', '<!DOCTYPE a [ <!ENTITY b "c"> %d; ]>', '<?xml version="1.0"?>']),
)
FRAGMENT = st.recursive(LEAF, lambda children: element(children), max_leaves=30)
MARKUP_TEXT = st.lists(FRAGMENT, max_size=8).map(''.join)
FOO = {**ZPT_NAMESPACES, 'foo': 'urn:foo'}
CONFIGS = st.sampled_from(
    [
        (FOO, True, 'xml'),
        (FOO, True, 'xml'),
        (ZPT_NAMESPACES, True, 'xml'),
        (ELEMENT_NAMESPACES, False, 'xml'),
        ({**FOO, None: 'urn:default'}, False, 'xml'),
        (FOO, True, 'text'),
    ]
)


@settings(
    max_examples=FUZZ_EXAMPLES,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(body=MARKUP_TEXT, config=CONFIGS)
def test_generated_markup(body: str, config: tuple[dict[str | None, str], bool, str]) -> None:
    namespaces, restricted, mode = config
    check(body, namespaces, restricted, mode)
