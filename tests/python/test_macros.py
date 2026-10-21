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


@pytest.mark.xfail(strict=True, reason='Phase 8: a macro called in a slot body writes to the slot stream')
def test_macro_called_inside_a_slot_body_writes_to_the_define_slot_stream() -> None:
    """Chameleon's fill function appends its own text through its defining function's
    `__append`, but passes the define-slot site's `__stream` to a macro it calls: here the
    translation's stream, so `<a>K</a>` lands inside the translated text. chameleon_rs moves
    the slot body's whole output (architecture.md, Phase 6b amendments)."""
    source = (
        hidden(
            '<div metal:define-macro="m"><b i18n:translate="">F<s metal:define-slot="s"/>G</b></div>'
            '<a metal:define-macro="k">K</a>'
        )
        + """<x metal:use-macro="macros['m']"><y metal:fill-slot="s">T <z metal:use-macro="macros['k']"/> U</y></x>"""
    )
    same(source, translate=lambda msgid, **kw: msgid)
