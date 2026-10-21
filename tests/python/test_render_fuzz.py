"""Generated templates using the features the VM runs (define, condition, repeat, content,
replace, attributes, omit-tag, switch/case, interpolation) rendered by both engines: the
same output, or the same exception type. CHAMELEON_RS_FUZZ_EXAMPLES raises the example
count (default 300).
"""

from __future__ import annotations

import os
from typing import Any

import chameleon
import chameleon_rs
import chameleon_rs.exc
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

EXAMPLES = int(os.environ.get('CHAMELEON_RS_FUZZ_EXAMPLES', '300'))


class Html:
    def __html__(self) -> str:
        return '<i>h</i>'


VARIABLES: dict[str, Any] = {
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
    ['x', 'z', 'n', 's', 'e', 'h', 'xs', 'd', 't', 'f', 'default', 'x + 1', 'not x', 'undefined', 'xs[0]', 'attrs']
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
    'tal:repeat': st.builds(lambda n, e: f'{n} {e}', NAME, st.sampled_from(['xs', 's', 'd', 'n', '[1, 2]', 'x'])),
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


def node(depth: int) -> Any:
    text = st.sampled_from(['a', '${x}', '\n  ', '${s}|${n}', '<!-- ${s} -->', '<![CDATA[${s}]]>', '${y | x}'])
    return st.one_of(text, element(depth), switch(depth))


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
        return
    assert not isinstance(got, Exception), (body, want, got)
    assert got == want, body
