"""Generated templates using the features the VM runs (define, condition, repeat including
global loops, content, replace, attributes, omit-tag, switch/case, on-error, code blocks,
interpolation, failing expressions) rendered by both engines: the same output, or the same
exception type and formatted message (addresses and generated ids normalized).
CHAMELEON_RS_FUZZ_EXAMPLES raises the example count (default 300).
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from typing import Any

import chameleon
import chameleon_rs
import chameleon_rs.exc
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

EXAMPLES = int(os.environ.get('CHAMELEON_RS_FUZZ_EXAMPLES', '300'))


class Html:
    def __html__(self) -> str:
        return '<i>h</i>'


class Model:
    """An application object (Phase 10 fast paths): `__getattr__` for unknown names, a
    property, a method, a property returning markup, a mapping fallback."""

    def __getattr__(self, name: str) -> str:
        if name.startswith('_') or name == 'missing':
            raise AttributeError(name)
        return f'ga:{name}'

    @property
    def p(self) -> str:
        return 'prop<'

    def m(self, *args: Any) -> str:
        return f'm{args}'

    @property
    def html(self) -> Html:
        return Html()

    def __getitem__(self, key: str) -> Any:
        if key == 'missing':
            return lambda *a: 'item'
        raise KeyError(key)


class Message:
    """A message-like value with `default` and `mapping` (the default translation function
    interpolates it)."""

    default = 'Hi ${who} <b>'
    mapping = {'who': 'you&me'}  # noqa: RUF012 - a constant


VARIABLES: dict[str, Any] = {
    'o': Model(),
    'msg': Message(),
    'x': 1,
    'z': 0,
    'n': None,
    's': 'a<"b\'&',
    'e': '',
    'h': Html(),
    'xs': [1, 0, 'q'],
    'd': {'a': 'A', 'checked': 1, 'selected': 0, 'n': None},
    't': True,
    'f': False,
}

EXPR = st.sampled_from(
    [
        'x',
        'z',
        'n',
        's',
        'e',
        'h',
        'xs',
        'd',
        't',
        'f',
        'default',
        'x + 1',
        'not x',
        'undefined',
        'xs[0]',
        'attrs',
        '1/z',
        'not: 1/z',
        'string:a${1/z}',
        'v | 1/z',
        'y',
        'o.k',
        'o.p',
        'o.m(1)',
        'o.missing()',
        'o.html',
        'msg',
        's.upper()',
        "d.get('a')",
        "ctx.__setitem__('x', 'w')",
    ]
)
ON_ERROR = st.sampled_from(
    ['string:E', 'error.type.__name__', 'nothing', '1/z', 'structure s', 'v', 'string:${error.lineno}:${error.offset}']
)
NAME = st.sampled_from(['x', 'y', 'v', 'repeat_', 'a'])


@st.composite
def attributes(draw: Any) -> str:
    parts = []
    for _ in range(draw(st.integers(1, 3))):
        if draw(st.booleans()):
            parts.append(draw(EXPR))
        else:
            parts.append(f'{draw(st.sampled_from(["a", "b", "checked", "title", "selected"]))} {draw(EXPR)}')
    return '; '.join(parts)


TAL: dict[str, Any] = {
    'tal:define': st.builds(lambda g, n, e: f'{g}{n} {e}', st.sampled_from(['', 'global ']), NAME, EXPR),
    'tal:condition': EXPR,
    'tal:repeat': st.builds(
        lambda g, n, e: f'{g}{n} {e}',
        st.sampled_from(['', '', 'global ']),
        NAME,
        st.sampled_from(['xs', 's', 'd', 'n', '[1, 2]', 'x', '1/z']),
    ),
    'tal:on-error': ON_ERROR,
    'tal:content': st.builds(lambda k, e: k + e, st.sampled_from(['', 'structure ']), EXPR),
    'tal:replace': st.builds(lambda k, e: k + e, st.sampled_from(['', 'structure ']), EXPR),
    'tal:attributes': attributes(),
    'tal:omit-tag': st.sampled_from(['', 'x', 'z', 'n', 'undefined']),
    'tal:switch': EXPR,
    'tal:case': EXPR,
    'a': st.sampled_from(['1', '${x}', '${n}', 'p${s}q', '${default}']),
    'checked': st.sampled_from(['checked', '${x}', '${z}', '${e}${n}', '']),
}


@st.composite
def element(draw: Any, depth: int) -> str:
    tag = draw(st.sampled_from(['p', 'tal:b', 'input']))
    names = draw(st.lists(st.sampled_from(sorted(TAL)), max_size=3, unique=True))
    if 'tal:content' in names and 'tal:replace' in names:
        names.remove('tal:replace')
    if tag == 'tal:b':
        # Unprefixed attributes of a TAL element are TAL attributes (not whitelisted).
        names = [n for n in names if n.startswith('tal:') and n != 'tal:attributes']
    attrs = ''.join(f' {name}="{draw(TAL[name])}"' for name in names)
    if depth <= 0 or draw(st.booleans()):
        return f'<{tag}{attrs}/>' if draw(st.booleans()) else f'<{tag}{attrs}>t</{tag}>'
    children = draw(st.lists(node(depth - 1), max_size=3))
    return f'<{tag}{attrs}>{"".join(children)}</{tag}>'


@st.composite
def switch(draw: Any, depth: int) -> str:
    cases = ''.join(
        f'<b tal:case="{draw(EXPR)}"{draw(st.sampled_from(["", ' tal:define="y 1"']))}>'
        f'{draw(node(depth - 1)) if depth > 0 else "c"}</b>'
        for _ in range(draw(st.integers(1, 3)))
    )
    return f'<p tal:switch="{draw(EXPR)}">{cases}</p>'


@st.composite
def guarded(draw: Any, depth: int) -> str:
    """An element under `tal:on-error`, so errors inside it are caught rather than ending the
    render (most generated templates fail somewhere)."""
    children = ''.join(draw(st.lists(node(depth - 1), max_size=3))) if depth > 0 else draw(EXPR)
    tag = draw(st.sampled_from(['div', 'tal:x']))
    extra = draw(st.sampled_from(['', ' tal:omit-tag=""', ' class="c"', ' tal:define="v 3"']))
    return f'<{tag} tal:on-error="{draw(ON_ERROR)}"{extra}>{children}${{1/z}}</{tag}>|${{v | error | 0}}'


def node(depth: int) -> Any:
    text = st.sampled_from(
        [
            'a',
            '${x}',
            '\n  ',
            '${s}|${n}',
            '<!-- ${s} -->',
            '<![CDATA[${s}]]>',
            '${y | x}',
            '${v}',
            '<?python v = 2 ?>',
            '<?python def g(k): return k ?>',
            '<?python econtext["ctx"] = econtext ?>',
            '${g(1) | 0}',
        ]
    )
    return st.one_of(text, element(depth), switch(depth), guarded(depth))


ADDRESS = re.compile(r'0x[0-9a-fA-F]+')
BACKUP = re.compile(r'__backup_(\w+?)_\d+')


def canon(text: str) -> str:
    text = BACKUP.sub(r'__backup_\1_?', ADDRESS.sub('0x?', text))
    return text.replace('chameleon_rs.', 'chameleon.')


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=st.lists(node(2), min_size=1, max_size=3).map(''.join))
def test_generated_templates_render_like_chameleon(body: str) -> None:
    def run(engine: Any) -> Any:
        try:
            return engine.PageTemplate(body)(**VARIABLES)
        except Exception as e:  # noqa: BLE001 - comparing exception types across engines
            return e

    want, got = run(chameleon), run(chameleon_rs)
    assert not isinstance(got, chameleon_rs.exc.UnsupportedError), (body, got)
    if isinstance(want, Exception):
        assert isinstance(got, Exception), (body, want, got)
        names = {c.__name__ for c in type(want).__mro__}
        assert type(got).__name__ in names, (body, want, got)
        if not isinstance(want, chameleon.exc.TemplateError):
            # Render errors: the whole formatted message (compile errors' formatting is
            # Phase 7's).
            assert canon(str(got)) == canon(str(want)), body
        return
    assert not isinstance(got, Exception), (body, want, got)
    assert canon(got) == canon(want), body


# -- The same bodies as partials (Phase 9) -----------------------------------------------

PARTIALS_DIR = __import__('tempfile').mkdtemp(prefix='chameleon-rs-fuzz-')
_partial_count = __import__('itertools').count()


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=st.lists(node(2), min_size=1, max_size=3).map(''.join), in_loop=st.booleans())
def test_generated_partials_render_like_chameleon_partials(body: str, in_loop: bool) -> None:
    # The body rendered as a partial (native frame) by chameleon_rs, and through
    # chameleon_partials by Chameleon, with every variable passed as a keyword; once, or
    # twice in a loop around the call.
    import chameleon_partials
    import chameleon_rs._settings

    name = f'p{next(_partial_count)}.pt'
    with open(os.path.join(PARTIALS_DIR, name), 'w') as f:
        f.write(body)
    call = '${render_partial("%s", %s)}' % (name, ', '.join(f'{k}={k}' for k in VARIABLES))  # noqa: UP031
    page = f'<b tal:repeat="r [1, 2]">{call}</b>' if in_loop else f'<i>{call}</i>'
    chameleon_partials.register_extensions(PARTIALS_DIR, cache_init=False)
    chameleon_rs.configure(template_folder=PARTIALS_DIR)

    def run(engine: Any, render_partial: Any) -> Any:
        try:
            return engine.PageTemplate(page)(render_partial=render_partial, **VARIABLES)
        except Exception as e:  # noqa: BLE001 - comparing exception types across engines
            return e

    try:
        want = run(chameleon, chameleon_partials.render_partial)
        got = run(chameleon_rs, chameleon_rs.render_partial)
    finally:
        chameleon_rs._settings.clear()
    if isinstance(want, Exception):
        assert isinstance(got, Exception), (body, want, got)
        names = {c.__name__ for c in type(want).__mro__}
        assert type(got).__name__ in names, (body, want, got)
        if not isinstance(want, chameleon.exc.TemplateError):
            assert canon(str(got)) == canon(str(want)), body
        return
    assert not isinstance(got, Exception), (body, want, got)
    assert canon(got) == canon(want), body


# -- Macros, slots and i18n (Phase 6b) ---------------------------------------------------


def translate(
    msgid: Any,
    domain: Any = None,
    mapping: Any = None,
    context: Any = None,
    target_language: Any = None,
    default: Any = None,
) -> Any:
    """A translation function that shows every argument it got (the mapping sorted: Chameleon
    builds it from a set, whose order depends on string hashing)."""
    if msgid == 'none':
        return None
    # A name an error left unfinished maps to its stream, a list of the pieces appended so far
    # (chameleon_rs keeps them as one piece).
    items = (
        None if mapping is None else sorted((k, ''.join(v) if isinstance(v, list) else v) for k, v in mapping.items())
    )
    return f'[{msgid!r}|{domain}|{context}|{target_language}|{items}|{default!r}]'


def _shorter(msgid: Any, **kwargs: Any) -> Any:
    text = translate(msgid, **kwargs)
    if not isinstance(text, str):
        return text
    # (The engines' module names in reprs differ in length: cut the normalized text.)
    text = canon(text)
    return text[: len(text) // 3]


TRANSLATIONS: dict[str, Any] = {
    'show': translate,
    # Results shorter than the message (the output shrinks), empty, and multi-byte (a mark
    # left inside them would split a character).
    'empty': lambda msgid, **kwargs: '',
    'short': _shorter,
    'non-ascii': lambda msgid, **kwargs: 'é€😀' * (len(canon(str(msgid))) % 4),
}


class EqualTranslator:
    """Translators that all compare equal: each render gets a new one (the render defaults
    cached by Phase 10 must not be reused by equality)."""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EqualTranslator)

    def __hash__(self) -> int:
        return 0

    def __call__(self, msgid: Any, **kwargs: Any) -> Any:
        text = translate(msgid, **kwargs)
        return text if not isinstance(text, str) else f'{self.tag}{text}'


MACRO_EXPR = st.sampled_from(
    ["macros['m1']", "macros['m2']", "template.macros['m1']", "macros['nope']", 'nothing', 'x', "macros['m-1']"]
)
METAL_I18N: dict[str, Any] = {
    'metal:define-macro': st.sampled_from(['m1', 'm2', 'm-1']),
    'metal:define-slot': st.sampled_from(['s1', 's2']),
    'i18n:translate': st.sampled_from(['', 'msg']),
    'i18n:domain': st.sampled_from(['d1', 'd2']),
    'i18n:context': st.sampled_from(['c1']),
    'i18n:target': st.sampled_from(['string:fr', 'default', 'n', 'x']),
    'i18n:attributes': st.sampled_from(['a', 'title msg; a', 'checked', 'a none']),
    'title': st.sampled_from(['T', '${x}', 'p${s}q', '${n}']),
}


@st.composite
def i18n_element(draw: Any, depth: int, safe: bool) -> str:
    tag = draw(st.sampled_from(['p', 'tal:b', 'input']))
    pool = sorted(TAL) + sorted(METAL_I18N)
    if safe:
        pool.remove('tal:on-error')
    names = draw(st.lists(st.sampled_from(pool), max_size=4, unique=True))
    if 'tal:content' in names and 'tal:replace' in names:
        names.remove('tal:replace')
    if 'tal:content' in names and 'i18n:translate' in names:
        names.remove('i18n:translate')
    if tag == 'tal:b':
        names = [n for n in names if n.startswith(('tal:', 'metal:', 'i18n:')) and n != 'tal:attributes']
        names = [n for n in names if n != 'i18n:attributes']
    attrs = ''.join(f' {name}="{draw({**TAL, **METAL_I18N}[name])}"' for name in names)
    if depth <= 0 or draw(st.booleans()):
        return f'<{tag}{attrs}/>' if draw(st.booleans()) else f'<{tag}{attrs}>t ${{x}}</{tag}>'
    safe = safe or 'i18n:translate' in names
    children = draw(st.lists(i18n_node(depth - 1, safe), max_size=3))
    return f'<{tag}{attrs}>{"".join(children)}</{tag}>'


@st.composite
def use_macro(draw: Any, depth: int) -> str:
    """A macro call with fill-slots (some of which define slots again)."""
    attr = draw(st.sampled_from(['metal:use-macro', 'metal:use-macro', 'metal:extend-macro']))
    expr = draw(st.one_of(MACRO_EXPR, st.sampled_from(["macros['mt1']", "macros['mt2']"])))
    # No `i18n:domain` on the call itself: a fill the macro does not take is left over, and a
    # later call outside that domain takes it, where Chameleon's conversion helpers see the
    # settings at that call (a known difference: test_i18n.
    # test_conversion_in_a_left_over_slot_body_uses_its_functions_settings_at_the_call).
    extra = draw(st.sampled_from(['', ' tal:define="x 5"', ' tal:on-error="string:E"']))
    if attr == 'metal:extend-macro':
        extra += f' metal:define-macro="{draw(st.sampled_from(["m1", "m2"]))}"'
    fills = []
    for _ in range(draw(st.integers(0, 2))):
        slot = draw(st.sampled_from(['s1', 's2']))
        again = draw(st.sampled_from(['', ' metal:define-slot="s2"', ' i18n:translate=""', ' tal:define="x 7"']))
        inner = draw(i18n_node(depth - 1, 'translate' in again)) if depth > 0 else '${x}'
        # A slot defined inside the fill body, or a macro called from it (Phase 8).
        inner += draw(
            st.sampled_from(['', '<e metal:define-slot="s1">d</e>', f'<z metal:use-macro="{draw(MACRO_EXPR)}"/>'])
        )
        fills.append(f'<b metal:fill-slot="{slot}"{again}>f{inner}</b>')
    return f'<div {attr}="{expr}"{extra}>{"".join(fills)}</div>${{x}}${{macroname | 0}}'


@st.composite
def translation(draw: Any, depth: int) -> str:
    """`i18n:translate` with names inside."""
    msgid = draw(st.sampled_from(['', '', 'msg']))
    parts = []
    for i in range(draw(st.integers(0, 2))):
        inner = draw(i18n_node(depth - 1, True)) if depth > 0 else '${s}'
        cond = draw(st.sampled_from(['', ' tal:condition="z"', ' tal:define="x 3"']))
        parts.append(f' w <i i18n:name="n{i}"{cond}>{inner}</i>')
    return f'<p i18n:translate="{msgid}">a  {"".join(parts)} b</p>'


def i18n_node(depth: int, safe: bool = False) -> Any:
    """A node of the macro and i18n templates. `safe`: inside a translation, where nothing
    has a `tal:on-error` (Chameleon's one fallback register per function, set inside a
    translation stream and used by a handler outside it, deletes from the outer stream by the
    inner stream's item count, which chameleon_rs does not model: architecture.md, Phase 6b)."""
    text = st.sampled_from(
        [
            'a',
            '${x}',
            '\n  ',
            '${s} and ${x}',
            '${h}',
            ' Hello  world ',
            '${1/z}',
            '${v}',
            # Phase 10: scope aliases (a body writing through one must not see its write when
            # it would run on a copy), application objects, message values.
            '<?python econtext["ctx"] = econtext ?>',
            '<i tal:define="global ctx econtext"/>',
            "${ctx.__setitem__('x', 'w') | 0}[${x}]",
            '${o.k}${o.p}${o.m(2)}${o.missing() | 0}${o.html}${msg}',
        ]
    )
    if safe:
        return st.one_of(text, i18n_element(depth, True), translation(depth))
    # A macro whose slot is defined inside a translation (Phase 8: slot bodies and macro streams).
    translated_slot = st.sampled_from(
        [
            '<p metal:define-macro="mt2" i18n:translate="">F<s metal:define-slot="s1"/>G ${x}</p>',
            '<p metal:define-macro="mt1"><i i18n:translate="">F<b i18n:name="n"><s metal:define-slot="s2"/></b></i></p>',
        ]
    )
    return st.one_of(
        text,
        i18n_element(depth, False),
        use_macro(depth),
        translation(depth),
        guarded(depth),
        translated_slot,
    )


class _NestedFillUnderAHandler(HTMLParser):
    """Finds a fill under a macro call under a `tal:on-error` inside a fill."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, set[str]]] = []
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        if 'metal:fill-slot' in names:
            # Each element's role from the root: fill, handler, call, in that order.
            want = ['metal:fill-slot', 'tal:on-error', 'metal:use-macro']
            for _, above in self.stack:
                while want and (want[0] in above or (want[0] == 'metal:use-macro' and 'metal:extend-macro' in above)):
                    want.pop(0)
            self.found = self.found or not want
        self.stack.append((tag, names))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        while self.stack and self.stack.pop()[0] != tag:
            pass


def nested_fill_under_a_handler(body: str) -> bool:
    """A known difference (#1, `test_macros.test_handler_around_a_nested_call_in_a_slot_body`):
    a `tal:on-error` in a fill, around a macro call whose own fill fails. When both fills land
    in slots inside a translation, Chameleon's one fallback mark per function, taken in the
    outer stream, cuts the inner one and leaves the inner fill's start tag unclosed, where
    chameleon-rs takes the inner fill's output back. Which slot a fill lands in depends on the
    call that takes it, so any template with this structure is skipped."""
    parser = _NestedFillUnderAHandler()
    parser.feed(body)
    parser.close()
    return parser.found


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    body=st.lists(i18n_node(2), min_size=1, max_size=3).map(''.join),
    implicit=st.booleans(),
    language=st.sampled_from([None, 'de']),
    mode=st.sampled_from(['show', 'show', 'empty', 'short', 'non-ascii', 'objects']),
)
def test_generated_macro_and_i18n_templates_render_like_chameleon(
    body: str, implicit: bool, language: str | None, mode: str
) -> None:
    assume(not nested_fill_under_a_handler(body))
    options = {'implicit_i18n_translate': implicit, 'implicit_i18n_attributes': {'title', 'a'} if implicit else ()}

    def run(engine: Any) -> Any:
        try:
            template = engine.PageTemplate(body, **options)
            if mode == 'objects':
                # Two renders with new, equal translators, as a request per language would.
                return ''.join(
                    template(**VARIABLES, translate=EqualTranslator(tag), target_language=language)
                    for tag in ('A:', 'B:')
                )
            return template(**VARIABLES, translate=TRANSLATIONS[mode], target_language=language)
        except Exception as e:  # noqa: BLE001 - comparing exception types across engines
            return e

    want, got = run(chameleon), run(chameleon_rs)
    assert not isinstance(got, chameleon_rs.exc.UnsupportedError), (body, got)
    if isinstance(want, Exception):
        assert isinstance(got, Exception), (body, want, got)
        names = {c.__name__ for c in type(want).__mro__}
        assert type(got).__name__ in names, (body, want, got)
        # A recursion error's message, and where it stops, depend on the interpreter's stack
        # depth here (Hypothesis's frames vary between calls).
        if not isinstance(want, (chameleon.exc.TemplateError, RecursionError)) and 'recursion' not in str(want):
            assert canon(str(got)) == canon(str(want)), body
        return
    assert not isinstance(got, Exception), (body, want, got)
    assert canon(got) == canon(want), body
