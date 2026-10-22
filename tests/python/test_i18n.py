"""Translation (Phase 6b): `i18n:translate` with names, `i18n:domain`, `i18n:context`,
`i18n:target`, `i18n:attributes`, implicit translation, and conversion's own `translate` calls,
rendered by Chameleon and chameleon_rs and compared.

Two kinds of comparison: the output with a translation function that shows its arguments,
and the translation protocol itself: every call the translation function receives, with its
positional and keyword arguments in order (`translate(msgid, mapping=..., default=...,
domain=..., context=..., target_language=...)` differs by site, and so does where
`target_language` comes from: the render function's local or the scope).
"""

from __future__ import annotations

import re
from typing import Any

import chameleon
import chameleon_rs
import pytest

ADDRESS = re.compile(r'0x[0-9a-fA-F]+')


def canon(text: str) -> str:
    return ADDRESS.sub('0x?', text).replace('chameleon_rs.', 'chameleon.')


def shown(
    msgid: Any,
    domain: Any = None,
    mapping: Any = None,
    context: Any = None,
    target_language: Any = None,
    default: Any = None,
) -> str:
    # The mapping sorted: Chameleon builds it from a set of names.
    items = None if mapping is None else sorted(mapping.items())
    return f'[{msgid}|d={domain}|c={context}|t={target_language}|m={items}|def={default}]'


class Obj:
    def __str__(self) -> str:
        return 'O!'


class Html:
    def __html__(self) -> str:
        return '<b>h</b>'


VARIABLES = {'x': 'X', 'o': Obj(), 'h': Html()}


def outcome(engine: Any, source: str, options: dict[str, Any], **kw: Any) -> tuple[Any, ...]:
    try:
        return ('ok', canon(engine.PageTemplate(source, **options)(translate=shown, **VARIABLES, **kw)))
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        return ('raise', type(e).__name__, canon(str(e)))


CASES = [
    '<p i18n:translate="">Hello <b i18n:name="who">${x}</b>!</p>',
    '<p i18n:translate="msg">Hello <b i18n:name="who">${x}</b>!</p>',
    '<p i18n:translate="">  </p>',
    '<p i18n:translate="msg">  </p>',
    '<p i18n:translate=""> a \n\t b <br/> c </p>',
    '<p i18n:translate=""><b i18n:name="a" tal:repeat="i [1,2,3]">${i}</b></p>',
    '<p i18n:translate=""><b i18n:name="a" tal:condition="False">x</b> and <i i18n:name="b">y</i></p>',
    '<p i18n:translate="">a <span i18n:name="n" i18n:translate="">b <i i18n:name="m">c</i></span></p>',
    '<p i18n:translate="">${x} and ${o}</p>',
    '<p i18n:translate="">a<?python y = 1 ?>${y}</p>',
    '<p i18n:translate="">${1/0}</p>',
    # tal:content / tal:replace with i18n:translate="".
    '<p i18n:translate="" tal:content="o"/>',
    '<p i18n:translate="" tal:content="None"/>',
    '<p i18n:translate="" tal:content="h"/>',
    '<p i18n:translate="" tal:content="structure h"/>',
    '<p i18n:translate="" tal:replace="x"/>',
    '<p i18n:translate="" tal:content="default">dflt</p>',
    '<p i18n:translate="" tal:content="1/0"/>',
    # The on-error fallback of a translated element is translated too.
    '<p tal:on-error="string:E" tal:condition="1/z" i18n:translate=""></p>',
    '<p tal:on-error="string:E" i18n:translate="">${1/z}</p>',
    # Domain, context and target: render-function locals, passed on to macros and slots, and
    # used by conversion (an object without `__html__` is translated).
    '<p i18n:domain="d1"><b i18n:translate="">x</b>${o}<i i18n:domain="d2" i18n:context="c2" i18n:translate="">y</i>${o}</p>${o}',
    '<p i18n:context="ctx">${o}<b i18n:translate="">x</b></p>',
    '<p i18n:target="string:fr" i18n:translate="">x ${o}</p>${o}',
    '<p i18n:target="default" i18n:translate="">x</p>',
    '<p tal:define="target_language string:zz" i18n:target="default" i18n:translate="">x</p>${o}<b tal:content="o"/>',
    '<p i18n:target="None" i18n:translate="">x</p>',
    '<div tal:on-error="string:E" i18n:domain="err"><p i18n:domain="inner">${1/0}</p></div>${o}',
    '<div i18n:domain="err" tal:on-error="string:E${o}"><p i18n:domain="inner">${1/0}</p></div>${o}',
    '<div tal:on-error="string:E"><p i18n:translate="">a${1/0}</p></div>',
    '<div i18n:translate="">a<p tal:on-error="string:E">b${1/0}</p>c</div>',
    # i18n:attributes on every kind of attribute value.
    '<img alt="a" i18n:attributes="alt"/>',
    '<img alt="a" title="t" i18n:attributes="alt msg_alt; title"/>',
    '<img alt="${x}" i18n:attributes="alt"/>',
    '<img alt="${x} y" i18n:attributes="alt"/>',
    '<img alt="${None}" i18n:attributes="alt"/>',
    '<img alt="${None}" i18n:attributes="alt msg"/>',
    '<img tal:attributes="alt None" i18n:attributes="alt"/>',
    '<img tal:attributes="alt None" i18n:attributes="alt msg"/>',
    '<img alt="a" tal:attributes="alt default" i18n:attributes="alt"/>',
    '<img alt="a" tal:attributes="alt o" i18n:attributes="alt"/>',
    '<img alt="a" tal:attributes="alt h" i18n:attributes="alt"/>',
    '<input checked="checked" tal:attributes="checked True" i18n:attributes="checked"/>',
    '<input checked="checked" tal:attributes="checked False" i18n:attributes="checked"/>',
    '<input checked="${True}" i18n:attributes="checked"/>',
    '<input checked="${False}" i18n:attributes="checked"/>',
    '<input checked="a${False}" i18n:attributes="checked"/>',
    '<input checked="a${True}" i18n:attributes="checked"/>',
    '<img alt="a" tal:attributes="dict(alt=2)" i18n:attributes="alt"/>',
    '<img tal:attributes="dict(alt=2); alt 3" i18n:attributes="alt"/>',
    '<img alt="&amp;" i18n:attributes="alt"/>',
    '<img alt="${x}" i18n:attributes="alt" i18n:domain="dd"/>',
    # A slot inside a translation in the macro writes to its defining function's stream.
    (
        """<div metal:define-macro="m"><p i18n:translate="">a <b metal:define-slot="s">default</b> c</p></div>"""
        """<div metal:use-macro="macros['m']">X<i metal:fill-slot="s">FILL</i>Y</div>"""
    ),
    (
        """<tal:d condition="False"><div metal:define-macro="m"><p i18n:translate="">a <b i18n:name="n" """
        """metal:define-slot="s">default</b> c</p></div></tal:d><div metal:use-macro="macros['m']">X"""
        """<i metal:fill-slot="s">FILL</i>Y</div>"""
    ),
    # Translation and macros: the caller's settings reach the macro and the slots.
    (
        """<tal:d condition="False"><i metal:define-macro="a"><b i18n:translate="">t</b><c metal:define-slot="s"/></i></tal:d>"""
        """<p i18n:domain="D" metal:use-macro="macros['a']"><x metal:fill-slot="s" i18n:translate="">f</x></p>"""
    ),
    (
        """<tal:d condition="False"><i metal:define-macro="a" i18n:domain="M"><b i18n:translate="">t</b>"""
        """<c metal:define-slot="s"/></i></tal:d><p i18n:domain="D" metal:use-macro="macros['a']">"""
        """<x metal:fill-slot="s" i18n:translate="">f</x></p>"""
    ),
    (
        """<tal:d condition="False"><i metal:define-macro="a" i18n:target="string:M"><b i18n:translate="">t</b>"""
        """<c metal:define-slot="s"/></i></tal:d><p i18n:target="string:T" metal:use-macro="macros['a']">"""
        """<x metal:fill-slot="s" i18n:translate="">f</x></p>"""
    ),
]

IMPLICIT_CASES = [
    '<p>Hello ${x} and ${o}</p>',
    '<p>Hello ${x} b </p><img alt="x ${x}"/>',
    '<p>${x}</p><p>a ${x.lower()}</p><p>An edge case: ${.</p><p>$${a}</p><p>a ${}</p>',
    '<p tal:define="target_language string:SCOPE">Hi ${x} b <img alt="x ${x}" title="Static"/></p>',
    '<p>  Welcome   to\n the site  </p><input checked="${x}" alt="${x}" />',
    '<p i18n:translate="">a ${x} b</p><p i18n:domain="d">${x} y</p>',
]


@pytest.mark.parametrize('language', [None, 'de'])
@pytest.mark.parametrize('source', CASES)
def test_translation_renders_like_chameleon(source: str, language: str | None) -> None:
    want = outcome(chameleon, source, {}, target_language=language, z=0)
    got = outcome(chameleon_rs, source, {}, target_language=language, z=0)
    assert got == want


@pytest.mark.parametrize('language', [None, 'de'])
@pytest.mark.parametrize('source', IMPLICIT_CASES)
def test_implicit_translation_renders_like_chameleon(source: str, language: str | None) -> None:
    options = {'implicit_i18n_translate': True, 'implicit_i18n_attributes': {'alt', 'title'}}
    want = outcome(chameleon, source, options, target_language=language)
    got = outcome(chameleon_rs, source, options, target_language=language)
    assert got == want


def calls(engine: Any, source: str, options: dict[str, Any], **kw: Any) -> list[tuple[Any, ...]]:
    """Every call the translation function gets: the positional arguments and the keyword
    arguments in the order they were passed (types and identity-free values)."""
    seen: list[tuple[Any, ...]] = []

    def translate(*args: Any, **kwargs: Any) -> Any:
        values = [(k, sorted(v.items()) if isinstance(v, dict) else v) for k, v in kwargs.items()]
        seen.append((tuple(str(a) if isinstance(a, Obj) else a for a in args), values))
        return shown(*args, **kwargs)

    try:
        engine.PageTemplate(source, **options)(translate=translate, **VARIABLES, **kw)
    except Exception as e:  # noqa: BLE001 - the calls before an error count too
        seen.append(('raised', type(e).__name__))
    return seen


@pytest.mark.parametrize('source', CASES + IMPLICIT_CASES)
def test_the_translate_protocol_matches_chameleon(source: str) -> None:
    options = {}
    if source in IMPLICIT_CASES:
        options = {'implicit_i18n_translate': True, 'implicit_i18n_attributes': {'alt', 'title'}}
    want = calls(chameleon, source, options, target_language='kw', z=0)
    got = calls(chameleon_rs, source, options, target_language='kw', z=0)
    assert got == want


def test_default_translation_interpolates_mappings() -> None:
    """The default `translate` (`simple_translate`): `${name}` and `$name` in the default are
    replaced from the mapping (the template's `$$who` is the text `$who`); unknown names stay."""
    source = '<p i18n:translate="">a ${x} <b i18n:name="who">${x}</b> $$who $who $other <i i18n:name="n2">2</i></p>'
    want = chameleon.PageTemplate(source)(x='X')
    assert chameleon_rs.PageTemplate(source)(x='X') == want == '<p>a X <b>X</b> <b>X</b> <b>X</b> $other <i>2</i></p>'


@pytest.mark.parametrize('result', [None, '', 'ééééé'])
def test_fallback_register_set_inside_a_closed_translation(result: str | None) -> None:
    """A deviation, pinned (architecture.md, Phase 6b amendments): an on-error inside an
    `i18n:translate` sets the function's one fallback register as a length of the translation
    stream; once the translation has closed, an outer handler uses that number on the outer
    stream (Chameleon: its items, here `<div>abc<div>E</div>`); chameleon_rs cuts at the
    translation's start, whatever the translation returned (never inside its text)."""
    source = (
        '<div tal:on-error="string:E">abc<p i18n:translate="">xy <i tal:on-error="string:e2">y</i> z</p>def${1/0}</div>'
    )
    kw: dict[str, Any] = {} if result is None else {'translate': lambda msgid, **_: result}
    assert chameleon.PageTemplate(source)(**kw) == '<div>abc<div>E</div>'
    assert chameleon_rs.PageTemplate(source)(**kw) == '<div>abc<p><div>E</div>'


M_SLOT = '<p metal:define-macro="m"><s metal:define-slot="s"/></p>'
M_TRANSLATED = '<p metal:define-macro="t" i18n:domain="dm"><b i18n:translate="">F<s metal:define-slot="s"/>G</b></p>'
M_OTHER = '<p metal:define-macro="k" i18n:context="ck">K<em metal:define-slot="u">u</em>${o}</p>'
M_NOSLOT = '<p metal:define-macro="n">N</p>'

SLOT_HELPER_CASES = [
    # The body's own context: `tal:content` converts with the helpers (the function's, None).
    """<div metal:use-macro="macros['m']"><b metal:fill-slot="s"><p i18n:context="c1" tal:content="xs"></p></b></div>""",
    # Domain, context and target set on the body; interpolations, attributes, content.
    (
        """<div metal:use-macro="macros['m']"><b metal:fill-slot="s" i18n:domain="d1">${o} <i title="${o}" """
        """tal:attributes="alt o">a${o}b</i><i tal:replace="o"/></b></div>"""
    ),
    (
        """<div metal:use-macro="macros['m']"><b metal:fill-slot="s" i18n:target="string:t1">${o}<i title="${o}">"""
        """${o}</i></b></div>"""
    ),
    (
        """<div metal:use-macro="macros['m']"><b metal:fill-slot="s" i18n:context="c1" i18n:target="string:t1">"""
        """<i i18n:domain="d2">${o}</i>${o}</b></div>"""
    ),
    # The function's own settings around the macro call, and the body's.
    (
        """<div i18n:domain="d0" i18n:context="c0" metal:use-macro="macros['m']"><b metal:fill-slot="s" """
        """i18n:domain="d1">${o}<i i18n:translate="">x ${o}</i></b></div>"""
    ),
    (
        """<div i18n:domain="d0"><div i18n:target="string:t0" metal:use-macro="macros['m']"><b metal:fill-slot="s" """
        """i18n:domain="d1" i18n:target="string:t1">${o}</b></div></div>"""
    ),
    # The slot inside the macro's translation; the body translates too.
    (
        """<div metal:use-macro="macros['t']"><b metal:fill-slot="s" i18n:context="c1">${o}<i i18n:translate="">"""
        """y ${o}</i></b></div>"""
    ),
    # A macro called from the body gets the body's settings as its own.
    (
        """<div metal:use-macro="macros['m']"><b metal:fill-slot="s" i18n:domain="d1">${o}"""
        """<i metal:use-macro="macros['k']"/></b></div>"""
    ),
    # A fill defined in the body: still the function's helpers, its own settings from the body.
    (
        """<div metal:use-macro="macros['m']"><b metal:fill-slot="s" i18n:domain="d1">${o}"""
        """<i metal:use-macro="macros['k']"><em metal:fill-slot="u" i18n:context="c2">${o}</em></i></b></div>"""
    ),
    (
        """<div i18n:domain="d0" metal:use-macro="macros['m']"><b metal:fill-slot="s" i18n:domain="d1">"""
        """<i i18n:context="c1" metal:use-macro="macros['k']"><em metal:fill-slot="u">${o}</em></i></b></div>"""
    ),
    # Defined in a macro (the function is the macro's), and through extend-macro.
    (
        """<div metal:define-macro="w" i18n:domain="dw"><i metal:use-macro="macros['m']"><b metal:fill-slot="s" """
        """i18n:domain="d1">${o}</b></i></div>"""
    ),
    (
        """<div metal:extend-macro="macros['m']" metal:define-macro="e" i18n:domain="de"><b metal:fill-slot="s" """
        """i18n:context="c1">${o}</b></div><div i18n:domain="d0" metal:use-macro="macros['e']"/>"""
    ),
]


@pytest.mark.parametrize('source', SLOT_HELPER_CASES)
def test_conversion_in_a_slot_body_uses_its_functions_i18n_settings(source: str) -> None:
    """Chameleon's conversion helpers (`__convert`, `__quote`: a value that is not a string, a
    number or `__html__` is translated) are closures of the render function, so in a slot body
    they see the defining render function's `__i18n_domain`, `__i18n_context` and
    `target_language`, not those set inside the body; the body's inline conversions and
    `translate` calls use the body's (architecture.md, Phase 12 amendments)."""
    body = f'<tal:d condition="False">{M_SLOT}{M_TRANSLATED}{M_OTHER}{M_NOSLOT}</tal:d>{source}'
    assert calls(chameleon_rs, body, {}, xs=[1]) == calls(chameleon, body, {}, xs=[1])
    assert outcome(chameleon_rs, body, {}, xs=[1]) == outcome(chameleon, body, {}, xs=[1])


M_CALLS_M = '<p metal:define-macro="w"><i metal:use-macro="macros[\'m\']"/></p>'


@pytest.mark.xfail(strict=True, reason='known difference: a left-over fill uses the settings where it was defined')
@pytest.mark.parametrize(
    'source',
    [
        # The body sets nothing of its own; a later call with another domain takes the fill.
        (
            """<div metal:use-macro="macros['n']"><b metal:fill-slot="s">${o}</b></div>"""
            """<div i18n:domain="d9" metal:use-macro="macros['m']"/>"""
        ),
        # The body sets its own context.
        (
            """<div metal:use-macro="macros['n']"><b metal:fill-slot="s" i18n:context="c1">${o}</b></div>"""
            """<div i18n:domain="d9" metal:use-macro="macros['m']"/>"""
        ),
        # Taken through another macro that calls the one with the slot.
        (
            """<div metal:use-macro="macros['n']"><b metal:fill-slot="s">${o}</b></div>"""
            """<div i18n:domain="d9" metal:use-macro="macros['w']"/>"""
        ),
        # `i18n:target` at the later call.
        (
            """<div metal:use-macro="macros['n']"><b metal:fill-slot="s">${o}</b></div>"""
            """<div i18n:target="string:t9" metal:use-macro="macros['m']"/>"""
        ),
    ],
)
def test_conversion_in_a_left_over_slot_body_uses_its_functions_settings_at_the_call(source: str) -> None:
    """A fill no macro took stays in the scope's slot stack, and a later macro call made while
    the function's settings differ can take it, directly or through another macro. Chameleon's
    helpers read the function's settings through closure cells, so they see the settings at
    that later call; chameleon-rs records them where the fill is defined, which is the same
    everywhere else: the function is suspended in the call that runs the body (architecture.md,
    Phase 12 amendments)."""
    body = f'<tal:d condition="False">{M_SLOT}{M_NOSLOT}{M_CALLS_M}</tal:d>{source}'
    assert calls(chameleon_rs, body, {}) == calls(chameleon, body, {})


def test_conversion_in_a_fill_defined_in_a_slot_body_a_foreign_macro_called() -> None:
    """A fill defined in a slot body that a stock Chameleon macro called belongs to the
    render function around that body: its helpers use `d0`, not the body's `d1`."""

    def tr(msgid: Any, **kw: Any) -> str:
        return f'[{msgid}|{kw.get("domain")}|{kw.get("context")}]'

    m = chameleon.PageTemplate('<p metal:define-macro="m"><s metal:define-slot="s"/></p>')
    page = (
        """<div i18n:domain="d0" metal:use-macro="m.macros['m']"><b metal:fill-slot="s">"""
        """<i i18n:domain="d1" metal:use-macro="k.macros['k']"><em metal:fill-slot="u">${o}</em></i></b></div>"""
    )
    results = []
    for engine in (chameleon, chameleon_rs):
        k = engine.PageTemplate(M_OTHER)
        results.append(engine.PageTemplate(page)(m=m, k=k, o=Obj(), translate=tr))
    assert results[1] == results[0]
    assert '[O!|d0|None]' in results[0]


def test_conversion_in_a_slot_body_ignores_its_i18n_context() -> None:
    """The case the extended fuzzer found (a strict xfail from Phase 8 to Phase 12)."""
    body = (
        '<div metal:use-macro="macros[\'m\']"><b metal:fill-slot="s"><p i18n:context="c1" tal:content="xs"></p></b></div>'
        '<p metal:define-macro="m"><s metal:define-slot="s"/></p>'
    )

    def tr(msgid: Any, **kw: Any) -> str:
        return f'[{msgid}|{kw.get("context")}]'

    results = [engine.PageTemplate(body)(xs=[1], translate=tr) for engine in (chameleon, chameleon_rs)]
    assert results[0] == results[1]


class Translator:
    """Equal to every other translator: per-render defaults must not be reused by equality."""

    def __init__(self, language: str) -> None:
        self.language = language

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Translator)

    def __hash__(self) -> int:
        return 0

    def __call__(self, msgid: Any, **kw: Any) -> str:
        return f'{self.language}:{kw.get("default") or msgid}'


@pytest.mark.parametrize('encoding', [None, 'utf-8'])
def test_equal_translators_are_not_confused(encoding: str | None) -> None:
    """Phase 10 caches the render defaults; distinct translators must each be used, whether
    passed per render (with or without `encoding`) or set on the template."""
    body = '<p i18n:translate="">Hi</p>'
    results = []
    for engine in (chameleon, chameleon_rs):
        template = engine.PageTemplate(body)
        out = [template.render(encoding=encoding, translate=Translator(lang)) for lang in ('de', 'fr')]
        for lang in ('it', 'es'):
            template.translate = Translator(lang)
            out.append(template.render(encoding=encoding))
        results.append(out)
    assert (
        results[0]
        == results[1]
        == [
            '<p>de:Hi</p>',
            '<p>fr:Hi</p>',
            '<p>it:Hi</p>',
            '<p>es:Hi</p>',
        ]
    )


class Message:
    """A message-like value: its `default` and `mapping` are read once per conversion."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    @property
    def default(self) -> str:
        self.reads.append('default')
        return 'Hello ${name}'

    @property
    def mapping(self) -> dict[str, str]:
        self.reads.append('mapping')
        return {'name': 'World'}


def test_message_attributes_are_read_once() -> None:
    results = []
    for engine in (chameleon, chameleon_rs):
        message = Message()
        results.append((engine.PageTemplate('<p>${m}|${string:${m}}</p>')(m=message), message.reads))
    assert results[0] == results[1]


@pytest.mark.xfail(strict=True, reason='known difference: write boundaries in a name a handler left unfinished')
@pytest.mark.parametrize(
    'source',
    [
        # A macro failing partway: its failing text node (`x`) was never appended.
        (
            """<tal:d condition="False"><p metal:define-macro="bad2">x${1/0}</p></tal:d>"""
            """<p i18n:translate="">fa<q i18n:name="k" tal:on-error="string:E">r<z metal:use-macro="macros['bad2']"/>"""
            """</q></p>"""
        ),
        # Chameleon's name stream is a list of its appends.
        """<p i18n:translate="">fa<q i18n:name="k" tal:on-error="string:E">r<z>${'s'}</z>${1/0}</q></p>""",
    ],
)
def test_handler_on_a_name_maps_it_to_its_appends(source: str) -> None:
    """A `tal:on-error` on an `i18n:name` element: Chameleon maps the name to its stream,
    the list of what was appended to it (`['<q>r', '<z>', 's', '</z>']`), where chameleon-rs
    has one piece; and in a macro that failed partway, Chameleon never appended the failing
    text node (`['<q>r', '<p>']`) where chameleon-rs keeps its text (`<q>r<p>x`). Pre-existing, the
    same write-boundary family as `test_handler_in_a_slot_body_drops_the_failing_text_node`
    (architecture.md, Phase 12 amendments)."""

    def tr(msgid: Any, **kw: Any) -> str:
        return repr(kw.get('mapping'))

    assert chameleon_rs.PageTemplate(source)(translate=tr) == chameleon.PageTemplate(source)(translate=tr)
