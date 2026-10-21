"""Chameleon's two-stage conversion for non-str `__html__()` and `translate` results.

Grid: value kind x source x position, each rendered by both engines (output or exception
type must match).
"""

from __future__ import annotations

from typing import Any

import chameleon
import chameleon_rs
import pytest


class StrSub(str):
    pass


VALUES: dict[str, Any] = {
    'int': 5,
    'bytes': b'x<',
    'list': [1, '<'],
    'none': None,
    'str': 'a<b',
    'strsub': StrSub('s<'),
}

POSITIONS = {
    'lone-text': '<p>${h}</p>',
    'mixed-text': '<p>a${h}b</p>',
    'lone-attr': '<a t="${h}">x</a>',
    'attr-parts': '<a t="a${h}b">x</a>',
    'content': '<p tal:content="h">x</p>',
    'structure': '<p tal:content="structure h">x</p>',
    'comment': '<!--${h}-->',
}


def variables(source: str, value: Any) -> dict[str, Any]:
    if source == 'html':

        class Html:
            def __html__(self) -> Any:
                return value

        return {'h': Html()}

    class Opaque:
        pass

    return {'h': Opaque(), 'translate': lambda msgid, **kw: value}


def outcome(engine: Any, body: str, kw: dict[str, Any]) -> tuple[str, Any]:
    try:
        return 'ok', engine.PageTemplate(body)(**kw)
    except Exception as e:  # noqa: BLE001 - comparing outcomes across engines
        return 'err', e


GRID = [(s, v, p) for s in ('html', 'translate') for v in VALUES for p in POSITIONS]


@pytest.mark.parametrize(('source', 'value', 'position'), GRID, ids=['-'.join(g) for g in GRID])
def test_conversion_grid(source: str, value: str, position: str) -> None:
    body = POSITIONS[position]
    want = outcome(chameleon, body, variables(source, VALUES[value]))
    got = outcome(chameleon_rs, body, variables(source, VALUES[value]))
    if want[0] == 'err':
        assert got[0] == 'err', got
        assert type(got[1]).__name__ in {c.__name__ for c in type(want[1]).__mro__}, (want, got)
    else:
        assert got == want


class Shown:
    def __str__(self) -> str:
        return '<b>shown</b>'


STRUCTURE_VALUES: dict[str, Any] = {
    'str': 'a<b>&',
    'strsub': StrSub('s<'),
    'int': 5,
    'none': None,
    'empty': '',
    'object': Shown(),
    'bytes': b'x<',
}

STRUCTURE_POSITIONS = [
    '<p>${structure: h}</p>',
    '<p>a${structure: h}b</p>',
    '<a t="${structure: h}">x</a>',
    '<a t="a${structure: h}b">x</a>',
    '<input checked="${structure: h}"/>',
    '<p tal:content="structure: h">x</p>',
    '<p tal:replace="structure: h">x</p>',
    '<p>${string:[${structure: h}]}</p>',
    '<p tal:define="m structure: h">${m}|${m.__html__()}|${type(m).__name__}</p>',
]


@pytest.mark.parametrize('value', sorted(STRUCTURE_VALUES))
@pytest.mark.parametrize('position', STRUCTURE_POSITIONS)
def test_structure_values(value: str, position: str) -> None:
    """`structure:` wraps the value in `Markup`, whose `__html__` chameleon_rs evaluates
    natively (Phase 10): same output as Chameleon wherever the value lands."""
    kw = {'h': STRUCTURE_VALUES[value]}
    want = outcome(chameleon, position, kw)
    got = outcome(chameleon_rs, position, kw)
    if want[0] == 'err':
        assert got[0] == 'err', got
    else:
        assert got == want
