"""Macros and slots (Phase 6b), rendered by Chameleon and chameleon_rs and compared: the
output, or the exception's type and whole formatted message (addresses normalized).

Each case is a template and, optionally, other templates passed in as `others` (macros across
templates, templates used as macros). Covered: `metal:define-macro` rendered in place and
called, `use-macro` with a path or Python expression, `extend-macro` chains, `define-slot`
defaults, `fill-slot` (nested, repeated, re-exposed, in loops), `macroname`, the scope copy and
`econtext.update(rcontext)` after a call (globals), macros of other templates (their builtins),
macro names and lookup errors, errors through macro and slot frames, recursion, macros across
files (`load:`), and macros across engines (Chameleon's in ours, ours in Chameleon's).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import chameleon
import chameleon.zpt.template
import chameleon_rs
import pytest

ADDRESS = re.compile(r'0x[0-9a-fA-F]+')


def canon(text: str) -> str:
    return ADDRESS.sub('0x?', text).replace('chameleon_rs.', 'chameleon.')


def outcome(engine: Any, source: str, others: tuple[str, ...] = (), **kw: Any) -> tuple[Any, ...]:
    try:
        templates = [engine.PageTemplate(o) for o in others]
        return ('ok', canon(engine.PageTemplate(source)(others=templates, **kw)))
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        return ('raise', type(e).__name__, canon(str(e)))


def same(source: str, others: tuple[str, ...] = (), **kw: Any) -> tuple[Any, ...]:
    want = outcome(chameleon, source, others, **kw)
    got = outcome(chameleon_rs, source, others, **kw)
    assert got == want, source
    return got


def hidden(body: str) -> str:
    """Macro definitions that are not rendered in place."""
    return f'<tal:defs condition="False">{body}</tal:defs>'


M = '<div metal:define-macro="m">M[<b metal:define-slot="s">default</b>]${x | "nox"}</div>'

CASES: list[tuple[str, tuple[str, ...]]] = [
    # Defined and rendered in place, then called.
    (M + """<p metal:use-macro="macros['m']"><i metal:fill-slot="s">F</i></p>""", ()),
    (M + """<p tal:define="x 1" metal:use-macro="macros['m']"><i metal:fill-slot="s">F${x}</i></p>""", ()),
    (M + """<p metal:use-macro="macros['m']"><i metal:fill-slot="s" tal:define="global g 5">F</i></p>${g}""", ()),
    (M + """<p metal:use-macro="macros['m']"/>${macroname | 'none'}""", ()),
    (M + """<p metal:use-macro="template.macros['m']"/><p metal:use-macro="macros/m | nothing"/>""", ()),
    ("""<div metal:define-macro="m">${macroname}</div><p metal:use-macro="macros['m']"/>""", ()),
    ("""<div metal:define-macro="m">${macroname}</div><p metal:use-macro="python: macros['m']"/>""", ()),
    # Names: `-` and other non-word characters are mangled; lookups replace only `-`.
    (
        """<div metal:define-macro="a-b">x</div><p metal:use-macro="macros['a-b']"/><p metal:use-macro="macros['a_b']"/>""",
        (),
    ),
    ("""<div metal:define-macro="a.b">x</div><p metal:use-macro="macros['a.b']"/>""", ()),
    (
        """<a metal:define-macro="a-b">1</a><b metal:define-macro="a_b">2</b>${macros.names}<p metal:use-macro="macros['a-b']"/>""",
        (),
    ),
    ("""<p metal:use-macro="macros['nope']"/>""", ()),
    ("""<p metal:use-macro="42"/>""", ()),
    ("""<p metal:use-macro="None"/>""", ()),
    ("""<p metal:use-macro="nothing"/>""", ()),
    # Errors in macros and slots: each render function records its own token; slot bodies
    # record none; an internal call resets the caller's token.
    ('<div metal:define-macro="m">${1/0}</div>', ()),
    (hidden('<div metal:define-macro="m">${1/0}</div>') + """<p metal:use-macro="macros['m']"/>""", ()),
    (
        hidden('<div metal:define-macro="m">${y}<b metal:define-slot="s"/></div>')
        + """<p tal:define="y 1" metal:use-macro="macros['m']"><i metal:fill-slot="s">${z}</i></p>""",
        (),
    ),
    (
        hidden('<div metal:define-macro="m">X<b tal:on-error="string:E" metal:define-slot="s"/></div>')
        + """<p metal:use-macro="macros['m']"><i metal:fill-slot="s">${2/0}</i></p>""",
        (),
    ),
    (
        hidden('<div metal:define-macro="m">X${y}<b tal:on-error="string:E${y}" metal:define-slot="s"/></div>')
        + """<p tal:on-error="string:OUT" metal:use-macro="macros['m']"><i metal:fill-slot="s">${2/0}</i></p>""",
        (),
    ),
    (
        hidden('<div metal:define-macro="m"><b metal:define-slot="s"/></div>')
        + """<p metal:use-macro="macros['m']"><i metal:fill-slot="s" tal:on-error="string:E${error.lineno}">${z}</i></p>""",
        (),
    ),
    (
        hidden('<div metal:define-macro="m">${q}<b metal:define-slot="s"/></div>')
        + """<p tal:on-error="string:E${error.lineno},${error.offset}" metal:use-macro="macros['m']">"""
        + """<i metal:fill-slot="s">${z}</i></p>""",
        (),
    ),
    ("""<p metal:use-macro="others[0].macros['x']"/>""", ('<i metal:define-macro="x">other ${1/0}</i>',)),
    ("""<p metal:use-macro="others[0]"/>""", ('<i>top ${a.b}</i>',)),
    # Slots: defaults, extend-macro chains, re-exposed slots, repeated and nested fills.
    (
        hidden(
            '<div metal:define-macro="base">[<b metal:define-slot="a">A</b>|<b metal:define-slot="b">B</b>]</div>'
            """<div metal:define-macro="ext" metal:extend-macro="macros['base']">"""
            '<i metal:fill-slot="a">extA<u metal:define-slot="c">C</u></i></div>'
        )
        + """<p metal:use-macro="macros['ext']"/><p metal:use-macro="macros['ext']"><x metal:fill-slot="c">fillC</x></p>"""
        + """<p metal:use-macro="macros['ext']"><x metal:fill-slot="a">fillA</x></p>"""
        + """<p metal:use-macro="macros['ext']"><x metal:fill-slot="b">fillB</x></p>""",
        (),
    ),
    (
        hidden(
            '<div metal:define-macro="base">[<b metal:define-slot="a">A</b>]</div>'
            """<div metal:define-macro="e1" metal:extend-macro="macros['base']">"""
            '<i metal:fill-slot="a">e1<u metal:define-slot="a">inner</u></i></div>'
            """<div metal:define-macro="e2" metal:extend-macro="macros['e1']"><i metal:fill-slot="a">e2</i></div>"""
        )
        + """<p metal:use-macro="macros['e2']"/><p metal:use-macro="macros['e1']"/>"""
        + """<p metal:use-macro="macros['e2']"><y metal:fill-slot="a">page</y></p>""",
        (),
    ),
    (
        hidden('<div metal:define-macro="m"><b tal:repeat="i [1,2,3]" metal:define-slot="s">d${i}</b></div>')
        + """<p metal:use-macro="macros['m']"><x metal:fill-slot="s">f${i}</x></p>""",
        (),
    ),
    (
        hidden('<div metal:define-macro="m"><b metal:define-slot="s">d</b><b metal:define-slot="s">d2</b></div>')
        + """<p metal:use-macro="macros['m']"><x metal:fill-slot="s">f</x></p><p metal:use-macro="macros['m']"/>""",
        (),
    ),
    (
        hidden('<div metal:define-macro="m"><b metal:define-slot="s">d</b></div>')
        + """<p metal:use-macro="macros['m']"><x metal:fill-slot="s">f1</x><y metal:fill-slot="s">f2</y></p>""",
        (),
    ),
    (
        hidden('<div metal:define-macro="m"><b metal:define-slot="s">d</b></div>')
        + """<p metal:use-macro="macros['m']"><x metal:fill-slot="s">f1"""
        + """<q metal:use-macro="macros['m']"><z metal:fill-slot="s">inner</z></q></x></p>""",
        (),
    ),
    (
        hidden(
            '<div metal:define-macro="m">[<b metal:define-slot="s">d</b>]</div>'
            """<div metal:define-macro="outer"><p metal:use-macro="macros['m']">"""
            '<x metal:fill-slot="s">o<y metal:define-slot="s2">s2d</y></x></p></div>'
        )
        + """<p metal:use-macro="macros['outer']"><z metal:fill-slot="s2">S2</z></p><p metal:use-macro="macros['outer']"/>""",
        (),
    ),
    # Scope: a copy for the macro, the slot sees the macro's scope; globals come back.
    (
        hidden("""<div metal:define-macro="m">${x}<b tal:define="x 'mx'" metal:define-slot="s">d</b>${x}</div>""")
        + """<p tal:define="x 'px'" metal:use-macro="macros['m']"><y metal:fill-slot="s">${x}</y></p>${x}""",
        (),
    ),
    (
        hidden("""<div metal:define-macro="m" tal:define="global g 'G'">${x}</div>""")
        + """<p tal:define="x 1"><q metal:use-macro="macros['m']"/>${x}${g}</p>""",
        (),
    ),
    (
        hidden("""<div metal:define-macro="m"><i tal:define="global x 'G'"/></div>""")
        + """<p tal:define="x 1"><q metal:use-macro="macros['m']"/>${x}</p>${x}""",
        (),
    ),
    ("""<p metal:use-macro="others[0]"/>${g}""", ('<i tal:define="global g 7">top</i>',)),
    ("""<p tal:define="g 1"><b metal:use-macro="others[0]"/>${g}</p>${g}""", ('<i tal:define="global g 7">top</i>',)),
    # The slot stacks live in the scope (`__slot_<name>`), as in Chameleon.
    (
        hidden("""<div metal:define-macro="m">${sorted(k for k in econtext if k.startswith('__slot'))}</div>""")
        + """<p metal:use-macro="macros['m']"><x metal:fill-slot="s">f</x></p>""",
        (),
    ),
    # Code blocks: a namespace per frame.
    (
        hidden('<div metal:define-macro="m"><?python y = 5 ?>${y}</div>')
        + """<p metal:use-macro="macros['m']"/>""" * 2,
        (),
    ),
    # Templates used as macros, and macros of other templates (their own builtins).
    ("""<p metal:use-macro="others[0]"/>""", ('<i>other ${template is None}</i>',)),
    (
        """<p metal:use-macro="others[0].macros['x']"><b metal:fill-slot="s">FILL</b></p>""",
        ('<i metal:define-macro="x">other <b metal:define-slot="s"/>${len(macros.names)}</i>',),
    ),
    (
        """<p metal:use-macro="others[0]"><b metal:fill-slot="s">FILL</b></p>""",
        ('<i>top <b metal:define-slot="s"/></i>',),
    ),
    (
        """<p metal:use-macro="others[0]"><x metal:fill-slot="s">${n}</x></p>""",
        ('<i tal:define="n 5"><b metal:define-slot="s"/></i>',),
    ),
    (
        """<p tal:define="n 3" metal:use-macro="others[0]"><x metal:fill-slot="s">${n}</x></p>""",
        ('<i tal:define="n 5"><b metal:define-slot="s"/></i>',),
    ),
    (
        hidden('<div metal:define-macro="m">${template is not None} ${macros.names}</div>')
        + """<p metal:use-macro="others[0]"/>""",
        (
            """<i metal:define-macro="o">${template is not None} ${macros.names}</i><b metal:use-macro="macros['o']"/>""",
        ),
    ),
    ("""<p metal:use-macro="others[0].macros['nope']"/>""", ('<i>top</i>',)),
    # Recursion, stopped by an on-error.
    (
        hidden(
            """<div metal:define-macro="r">${n}<p tal:condition="n" tal:define="n n-1" metal:use-macro="macros['r']"/></div>"""
        )
        + """<x tal:define="n 5" metal:use-macro="macros['r']"/>""",
        (),
    ),
    (
        hidden("""<div metal:define-macro="r"><p metal:use-macro="macros['r']"/></div>""")
        + """<x tal:on-error="string:E" metal:use-macro="macros['r']"/>""",
        (),
    ),
    # Phase 10: macros and slot bodies that only read their scope run without a copy of it;
    # what they see must not change (caller's locals and root, the macro's locals in a slot
    # body, names defined between calls, after-call globals, errors, nested slots).
    (
        hidden('<div metal:define-macro="c">${v}:${x}<b metal:define-slot="s">d${v}</b></div>')
        + """<p tal:repeat="v [1, 2, 3]"><q tal:define="x v * 10" metal:use-macro="macros['c']">"""
        + """<i metal:fill-slot="s">F${v}${x}${repeat.v.index}</i></q></p>${v | 'none'}""",
        (),
    ),
    (
        hidden('<div metal:define-macro="c">[${y}]<b metal:define-slot="s"/></div>')
        + """<p tal:define="y 1" metal:use-macro="macros['c']"><i metal:fill-slot="s">${y}</i></p>"""
        + """<p tal:define="y 2" metal:use-macro="macros['c']"><i metal:fill-slot="s">${y}</i></p>${y | 'none'}""",
        (),
    ),
    (
        hidden('<div metal:define-macro="c" tal:define="m 7">${m}<b metal:define-slot="s"/></div>')
        + """<p metal:use-macro="macros['c']"><i metal:fill-slot="s">${m}${sorted(econtext)[:3]}</i></p>""",
        (),
    ),
    (
        hidden('<div metal:define-macro="c">${len([k for k in econtext if k.startswith("__slot")])}</div>')
        + """<p metal:use-macro="macros['c']"><i metal:fill-slot="s">x</i></p>""",
        (),
    ),
    (
        hidden('<div metal:define-macro="c">${nope}<b metal:define-slot="s"/></div>')
        + """<p tal:on-error="string:E${error.type.__name__}" metal:use-macro="macros['c']">"""
        + """<i metal:fill-slot="s">x</i></p>${error | 'no error'}""",
        (),
    ),
    (
        hidden('<div metal:define-macro="c"><b metal:define-slot="s"><u metal:define-slot="t">T</u></b></div>')
        + """<p metal:use-macro="macros['c']"><i metal:fill-slot="t">${q | 'noq'}</i></p>"""
        + """<p tal:define="global q 4" metal:use-macro="macros['c']"><i metal:fill-slot="s">S${q}</i></p>${q}""",
        (),
    ),
    (
        """<p tal:define="w 2" metal:use-macro="others[0]"><x metal:fill-slot="s">${w}${n | 0}</x></p>${w}""",
        ('<i>${w}<b metal:define-slot="s"/></i>',),
    ),
    (
        hidden('<div metal:define-macro="c">${[v for v in range(2)]}${v}</div>')
        + """<p tal:define="v 9" metal:use-macro="macros['c']"/>${v}""",
        (),
    ),
    # ... and once a scope has been handed to user code, a body that writes through the alias
    # must not see its own write (Chameleon's copy would not): no sharing after exposure.
    (
        """<p tal:define="x 'old'; ctx econtext" metal:use-macro="others[0]"/>${x}""",
        ("""<i>${ctx.__setitem__('x', 'new')}[${x}]</i>""",),
    ),
    (
        hidden("""<i metal:define-macro="m">${ctx.__setitem__('x', 'new')}[${x}]</i>""")
        + """<p tal:define="x 'old'; ctx econtext" metal:use-macro="macros['m']"/>${x}""",
        (),
    ),
    (
        hidden("""<i metal:define-macro="m">${ctx.__setitem__('x', 'new')}[${x}]</i>""")
        + """<?python econtext['ctx'] = econtext ?>"""
        + """<p tal:define="x 'old'" metal:use-macro="macros['m']"/>${x}""",
        (),
    ),
    (
        hidden("""<i metal:define-macro="m"><b metal:define-slot="s"/>[${x}]</i>""")
        + """<p tal:define="x 'old'; ctx econtext" metal:use-macro="macros['m']">"""
        + """<u metal:fill-slot="s">${ctx.__setitem__('x', 'new')}${x}</u></p>${x}""",
        (),
    ),
]


@pytest.mark.parametrize(('source', 'others'), CASES)
def test_macros_render_like_chameleon(source: str, others: tuple[str, ...]) -> None:
    same(source, others)


def test_macro_lookup() -> None:
    body = '<a metal:define-macro="a-b"/><b metal:define-macro="a_b"/><c metal:define-macro="x.y"/><d metal:define-macro="ü"/>'
    for engine in (chameleon, chameleon_rs):
        template = engine.PageTemplate(body)
        assert template.macros.names == ['a_b', 'x_y', 'ü']
        for name in ('a-b', 'a_b', 'x_y', 'ü'):
            assert template.macros[name].include
            assert template[name].include
        for name in ('x.y', 'nope'):
            with pytest.raises(KeyError) as info:
                template.macros[name]
            assert str(info.value) == f'"Macro does not exist: \'{name}\'."'


def test_macro_and_template_include_with_chameleons_signature() -> None:
    """`include(stream, econtext, rcontext, ...)` appends the rendering to the stream."""
    template = chameleon_rs.PageTemplate('<p metal:define-macro="m">${a}</p>|${a}')
    for include, want in ((template.macros['m'].include, '<p>1</p>'), (template.include, '<p>1</p>|1')):
        stream: list[str] = ['x']
        include(stream, {'a': 1, '__translate': None, '__decode': None, '__on_error_handler': None}, {})
        assert stream == ['x', want]
        # As Chameleon's render functions, it reads the render's settings first.
        with pytest.raises(KeyError, match='__decode'):
            include([], {'a': 1, '__translate': None}, {})


def test_included_macro_does_not_share_a_scope_its_caller_holds() -> None:
    """A macro called from outside (`include`) gets a scope that user code holds: the read-only
    macro it calls must still get a copy, as in Chameleon."""
    source = (
        """<i metal:define-macro="n">${ctx.__setitem__('x', 'new')}[${x}]</i>"""
        """<b metal:define-macro="m"><u metal:use-macro="macros['n']"/>${x}</b>"""
    )
    results = []
    for engine in (chameleon, chameleon_rs):
        template = engine.PageTemplate(source)
        scope = engine.utils.Scope(x='old', __translate=None, __decode=None, __on_error_handler=None)
        scope['ctx'] = scope
        scope['macros'] = template.macros
        stream: list[str] = []
        template.macros['m'].include(stream, scope, {})
        results.append(''.join(stream))
    assert results[0] == results[1]


@pytest.mark.parametrize(
    ('page_engine', 'macro_engine'),
    [(chameleon_rs, chameleon), (chameleon, chameleon_rs), (chameleon_rs, chameleon_rs)],
)
def test_macros_across_engines(page_engine: Any, macro_engine: Any) -> None:
    layout = '<div metal:define-macro="m" i18n:domain="L">[<b metal:define-slot="s">d</b>]${x}<i tal:define="global g 1"/></div>'
    page = """<p tal:define="x 'X'" metal:use-macro="lay.macros['m']"><u metal:fill-slot="s">F${x}</u></p>${g}"""
    want = chameleon.PageTemplate(page)(lay=chameleon.PageTemplate(layout))
    assert page_engine.PageTemplate(page)(lay=macro_engine.PageTemplate(layout)) == want
    want = chameleon.PageTemplate('<p metal:use-macro="t"/>')(t=chameleon.PageTemplate('<i>${1 + 1}</i>'))
    assert page_engine.PageTemplate('<p metal:use-macro="t"/>')(t=macro_engine.PageTemplate('<i>${1 + 1}</i>')) == want


def test_recursion_depth_matches_chameleon() -> None:
    """Chameleon's macros nest as Python calls: an on-error at every level catches at the depth
    the interpreter's recursion limit allows (three shapes: macros, templates used as macros,
    macros filling slots)."""
    recursive = hidden(
        """<div metal:define-macro="r"><p tal:on-error="string:E${n}" tal:define="n n+1">"""
        """<b metal:use-macro="macros['r']"/></p></div>"""
    )
    template_itself = """<x tal:define="n n+1"><p tal:on-error="string:E${n}"><b metal:use-macro="template"/></p></x>"""
    with_slots = hidden(
        """<div metal:define-macro="r"><p tal:on-error="string:E${n}" tal:define="n n+1">"""
        """<b metal:use-macro="macros['r']"><i metal:fill-slot="s">x</i></b></p><u metal:define-slot="s"/></div>"""
    )
    cases = [
        (recursive + """<x tal:define="n 0" metal:use-macro="macros['r']"/>""", {}),
        (template_itself, {'n': 0}),
        (with_slots + """<x tal:define="n 0" metal:use-macro="macros['r']"/>""", {}),
    ]
    for source, kw in cases:
        depths = [re.findall(r'E\d+', engine.PageTemplate(source)(**kw)) for engine in (chameleon, chameleon_rs)]
        assert depths[0] == depths[1]
        assert depths[1]
    limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(limit + 500)
        source, kw = cases[0]
        depths = [re.findall(r'E\d+', engine.PageTemplate(source)(**kw)) for engine in (chameleon, chameleon_rs)]
        assert depths[0] == depths[1]
    finally:
        sys.setrecursionlimit(limit)


def test_infinite_macro_recursion_is_a_recursion_error() -> None:
    # The upstream suite's case: not wrapped in RenderError.
    template = chameleon_rs.PageTemplate('<div metal:define-macro="main" metal:use-macro="template.macros.main" />')
    with pytest.raises(RecursionError) as info:
        template()
    assert not isinstance(info.value, chameleon_rs.exc.RenderError)
    assert str(info.value) == 'maximum recursion depth exceeded'


LAYOUT = """<html metal:define-macro="layout">
<head><title metal:define-slot="title">Default</title></head>
<body>
  <div metal:define-slot="content">no content</div>
  <p tal:condition="boom | False">${1/0}</p>
  ${main_var}
</body>
</html>
"""
PAGE = """<html metal:use-macro="load: layout.pt" tal:define="main_var 'MV'">
<title metal:fill-slot="title">Page</title>
<div metal:fill-slot="content">Hello ${name}!<span tal:replace="z"/></div>
</html>
"""
PAGE2 = """<html metal:use-macro="load('layout.pt').macros['layout']" tal:define="main_var 'MV'">
<div metal:fill-slot="content">${name}</div>
</html>
"""


@pytest.mark.parametrize(
    ('page', 'kw'),
    [
        ('page.pt', {'name': 'N', 'z': 'Z'}),
        ('page.pt', {'name': 'N'}),
        ('page.pt', {'name': 'N', 'z': 1, 'boom': True}),
        ('page2.pt', {'name': 'N'}),
        ('page2.pt', {}),
    ],
)
def test_macros_across_files(tmp_path: Path, page: str, kw: dict[str, Any]) -> None:
    """`load:` and the loader: errors in a layout name its file and show its source line."""
    (tmp_path / 'layout.pt').write_text(LAYOUT)
    (tmp_path / 'page.pt').write_text(PAGE)
    (tmp_path / 'page2.pt').write_text(PAGE2)
    results = []
    for cls in (chameleon.zpt.template.PageTemplateFile, chameleon_rs.PageTemplateFile):
        try:
            results.append(('ok', cls(str(tmp_path / page))(**kw)))
        except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
            results.append(('raise', type(e).__name__, canon(str(e))))
    assert results[0] == results[1]


@pytest.mark.parametrize(
    'source',
    [
        'a${(yield 1)}b',
        'a<p tal:content="(yield)"/>b',
        '<?python x = (yield) ?>a',
        'a${(x for x in [(yield)])}b',
        'a${len(list((lambda: (yield))()))}b',
        hidden('<div metal:define-macro="m">M${(yield)}</div>') + """x<p metal:use-macro="macros['m']"/>y""",
        hidden('<div metal:define-macro="m">M<b metal:define-slot="s"/>N</div>')
        + """x<p metal:use-macro="macros['m']"><i metal:fill-slot="s">F${(yield)}</i></p>y""",
    ],
)
def test_yield_makes_its_function_render_nothing(source: str) -> None:
    """Chameleon inlines expressions into render functions: a `yield` makes the function (the
    main one, a macro's, a slot's) a generator, whose call runs nothing."""
    same(source)


def test_macro_called_inside_a_slot_body_writes_to_the_define_slot_stream() -> None:
    """Chameleon's fill function appends its own text through its defining function's
    `__append`, but passes the define-slot site's `__stream` to a macro it calls: here the
    translation's stream, so `<a>K</a>` lands inside the translated text while the slot body's
    own text moves before it (architecture.md, Phase 8 amendments)."""
    source = (
        hidden(
            '<div metal:define-macro="m"><b i18n:translate="">F<s metal:define-slot="s"/>G</b></div>'
            '<a metal:define-macro="k">K</a>'
        )
        + """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <z metal:use-macro="macros['k']"/> U</y></x>"""
    )
    same(source, translate=lambda msgid, **kw: msgid)


SLOT_STREAM_MACROS = hidden(
    '<div metal:define-macro="m"><b i18n:translate="">F<s metal:define-slot="s"/>G</b></div>'
    '<div metal:define-macro="n"><b i18n:translate="">F<i i18n:name="x"><s metal:define-slot="s"/></i>G</b></div>'
    '<div metal:define-macro="plain">F<s metal:define-slot="s"/>G</div>'
    '<a metal:define-macro="k">K<em metal:define-slot="t">t</em></a>'
    '<a metal:define-macro="bad">B${1/0}</a>'
    '<div metal:define-macro="ext" metal:use-macro="macros[\'m\']"><y metal:fill-slot="s">P '
    '<e metal:define-slot="s2">d</e> Q</y></div>'
    '<div metal:define-macro="extk" metal:use-macro="macros[\'m\']"><y metal:fill-slot="s">P '
    '<z metal:use-macro="macros[\'k\']"/> <e metal:define-slot="s2">d</e> Q</y></div>'
)


@pytest.mark.parametrize(
    'use',
    [
        # Two macro calls, text around and between them.
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <z metal:use-macro="macros['k']"/> U """
            """<z metal:use-macro="macros['k']"/> V</y></x>"""
        ),
        # The macro's own slot filled inside the slot body.
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <z metal:use-macro="macros['k']">"""
            """<w metal:fill-slot="t">W${'w'}</w></z> U</y></x>"""
        ),
        # The define-slot inside an `i18n:name` of the translation.
        """<x metal:use-macro="macros['n']"><y metal:fill-slot="s">T <z metal:use-macro="macros['k']"/> U</y></x>""",
        # No translation around the define-slot: nothing moves.
        """<x metal:use-macro="macros['plain']"><y metal:fill-slot="s">T <z metal:use-macro="macros['k']"/> U</y></x>""",
        # Through `template.macros`.
        (
            """<x metal:use-macro="template.macros['m']"><y metal:fill-slot="s">T """
            """<z metal:use-macro="template.macros['k']"/> U</y></x>"""
        ),
        # The macro fails and a handler in the slot body catches it.
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s" tal:on-error="string:E">T """
            """<z metal:use-macro="macros['bad']"/> U</y></x>"""
        ),
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <q tal:on-error="string:E">"""
            """<z metal:use-macro="macros['bad']"/></q> U</y></x>"""
        ),
        # A macro of the slot body inside a macro of the slot body.
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <z metal:use-macro="macros['k']">"""
            """<w metal:fill-slot="t"><v metal:use-macro="macros['k']"/></w></z> U</y></x>"""
        ),
        # A slot defined inside a slot body, filled by the caller (a slot call, like a macro call).
        """<x metal:use-macro="macros['ext']"><w metal:fill-slot="s2">W</w></x>""",
        """<x metal:use-macro="macros['ext']"><w metal:fill-slot="s2">W <z metal:use-macro="macros['k']"/> V</w></x>""",
        """<x metal:use-macro="macros['extk']"><w metal:fill-slot="s2">W</w></x>""",
        """<x metal:use-macro="macros['ext']"/>""",
        # Repeated macro calls; a macro inside a fill of a macro the body called.
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T """
            """<z tal:repeat="i range(2)" metal:use-macro="macros['k']"/> U</y></x>"""
        ),
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <z metal:use-macro="macros['k']">"""
            """<w metal:fill-slot="t">a<v metal:use-macro="macros['k']"/>b</w></z> U</y></x>"""
        ),
        # A translation of the body's own around the macro call: the macro writes into it.
        (
            """<x metal:use-macro="macros['n']"><y metal:fill-slot="s">T <i18n:x i18n:translate="">in"""
            """<z metal:use-macro="macros['k']"/>w</i18n:x> U</y></x>"""
        ),
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <c tal:define="v structure: 1">"""
            """<z metal:use-macro="macros['k']"/></c> U</y></x>"""
        ),
        # A handler in the slot body: Chameleon cuts only the define-slot site's stream, so the
        # body's own output stays and what streams opened inside the handler's body held goes.
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <q tal:on-error="string:E">"""
            """<i i18n:translate="">a${1/0}</i></q> U</y></x>"""
        ),
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s"><i i18n:translate="">T """
            """<q tal:on-error="string:E">Q${1/0}</q></i> U</y></x>"""
        ),
        (
            """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <q tal:on-error="string:E">"""
            """<z metal:use-macro="macros['bad']"/>${1/0}</q> U</y></x>"""
        ),
    ],
)
def test_slot_body_macro_streams_like_chameleon(use: str) -> None:
    same(SLOT_STREAM_MACROS + use, translate=lambda msgid, **kw: f'<{msgid}|{kw.get("mapping")}>')


@pytest.mark.xfail(strict=True, reason='known difference: Chameleon appends a text node and its expressions at once')
def test_handler_in_a_slot_body_drops_the_failing_text_node() -> None:
    """Chameleon appends static text and the expressions in it as one string, so when the
    expression fails the text before it was never appended; chameleon-rs writes the text
    first. Only visible in a slot body whose output belongs to another stream than the
    define-slot site's, where the handler does not cut the body's own output (architecture.md,
    Phase 8 amendments)."""
    same(
        SLOT_STREAM_MACROS
        + """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <q tal:on-error="string:E">Q${1/0}</q>"""
        + """ U</y></x>""",
        translate=lambda msgid, **kw: f'<{msgid}|{kw.get("mapping")}>',
    )


@pytest.mark.xfail(strict=True, reason='known difference, found by the Phase 10 fuzz run, present since Phase 8')
def test_extended_slot_inside_a_name_with_a_recursive_translated_fill() -> None:
    """An `extend-macro` whose first fill of a slot (defined inside an `i18n:name`) is a
    translated body calling the macro itself, and whose second fill is plain: Chameleon writes
    the plain fill's output inside the outer `<b>` before the translation's, chameleon-rs after
    it (`<b><b>fa</b>[...]` against `<b>fa</b><b>[...]`). Not caused by Phase 10: a7bd81a
    renders it the same way."""
    same(
        """<p metal:define-macro="mt1"><i i18n:translate="">F<b i18n:name="n"><s metal:define-slot="s2"/></b></i>"""
        """</p><div metal:extend-macro="macros['mt1']" metal:define-macro="m2"><b metal:fill-slot="s2" """
        """i18n:translate="">fa<z metal:use-macro="macros['m2']"/></b><b metal:fill-slot="s2">fa</b></div>"""
        """${x}${macroname | 0}""",
        x=1,
        translate=lambda msgid, **kw: repr([msgid, kw.get('mapping')]),
    )
