"""`tal:repeat` and the `repeat` variable against Chameleon: every `RepeatItem` attribute,
read as a value and called (`index()`, `letter()`), over iterables of every shape, nested
loops, unpacking, whitespace between items, and the names after the loop."""

from __future__ import annotations

from typing import Any

import chameleon
import chameleon.tal
import chameleon_rs
import pytest

ATTRIBUTES = [
    'index',
    'number',
    'even',
    'odd',
    'parity',
    'start',
    'end',
    'length',
    'letter',
    'Letter',
    'roman',
    'Roman',
]

ITERABLES = {
    'list': [10, 20, 30],
    'empty': [],
    'one': ['a'],
    'tuple': (1, 2),
    'string': 'xyz',
    'dict': {'a': 1, 'b': 2},
    'range': range(28),
    'set': {7},
    'none': None,
}


def render(engine: Any, source: str, **kw: Any) -> Any:
    try:
        return engine.PageTemplate(source)(**kw)
    except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
        return type(e).__name__, str(e).split('\n')[0]


@pytest.mark.parametrize('attribute', ATTRIBUTES)
@pytest.mark.parametrize('called', [False, True], ids=['value', 'called'])
@pytest.mark.parametrize('name', sorted(ITERABLES))
def test_repeat_item_attributes(attribute: str, called: bool, name: str) -> None:
    access = f'repeat.i.{attribute}' + ('()' if called else '')
    source = f'<ul>\n  <li tal:repeat="i items">${{i}}=${{{access}}}|${{repeat["i"].{attribute}}}</li>\n</ul>'
    items = ITERABLES[name]
    assert render(chameleon_rs, source, items=items) == render(chameleon, source, items=items)


GRID = [
    # Nested loops see the outer item.
    '<p tal:repeat="o [1, 2]"><b tal:repeat="i \'ab\'">${repeat.o.index}${repeat.i.letter}${o}${i}</b></p>',
    # Same name nested: the inner loop's item shadows, then the outer is restored.
    '<p tal:repeat="i [1, 2]"><b tal:repeat="i [3]">${i}${repeat.i.index}</b>${i}${repeat.i.index}</p>',
    # Unpacking.
    "<p tal:repeat=\"(k, v) items.items()\">${k}${v}${repeat['k', 'v'].index}</p>",
    '<p tal:repeat="(a, b) [(1, 2), (3,)]">${a}</p>',
    '<p tal:repeat="(a, b) [(1, 2, 3)]">${a}</p>',
    # After the loop: the names are restored, the repeat item stays.
    '<p tal:repeat="i [1, 2]">${i}</p>${repeat.i.index}|${repeat.i.length}|${i | "gone"}',
    '<p tal:define="i 9" tal:repeat="i [1]">${i}</p>${i}',
    # Whitespace between items, in several layouts.
    '<div>\n    <p tal:repeat="i [1, 2, 3]">${i}</p>\n</div>',
    '<div>\n\t<p tal:repeat="i [1, 2]">${i}</p></div>',
    '<div><p tal:repeat="i [1, 2]">${i}</p>\n</div>',
    '<div>\n  <tal:b repeat="i [1, 2]">${i}</tal:b>\n</div>',
    '<div>\n  <p tal:repeat="i [1, 2]" tal:replace="i" />\n</div>',
    '<div>\n  <p tal:repeat="i [1, 2]" tal:omit-tag="">${i}</p>\n</div>',
    'x <p tal:repeat="i [1, 2]">${i}</p>',
    # Conditions and loops on one element.
    '<p tal:condition="items" tal:repeat="i items">${i}</p>',
    '<p tal:repeat="i [0, 1, 2]" tal:condition="i">${i}</p>',
    '<p tal:define="xs [1, 2]" tal:repeat="i xs" tal:content="repeat.i.end">x</p>',
    # Iterables that misbehave.
    '<p tal:repeat="i 5">x</p>',
    '<p tal:repeat="i iter([1, 2])">${i}${repeat.i.length}</p>',
    '<p tal:repeat="i (x for x in [1, 2])">${i}${repeat.i.index}</p>',
    # A user-supplied `repeat`.
    '<p tal:repeat="i [1, 2]">${i}</p>',
    # The loop variable inside attributes and defines.
    '<p tal:repeat="i [1, 2]" class="c${i}" tal:attributes="id string:i${repeat.i.number}">${i}</p>',
    '<p tal:repeat="i [1, 2]" tal:define="j i * 2">${j}</p>${j | "-"}',
]


@pytest.mark.parametrize('source', GRID)
def test_repeat_grid(source: str) -> None:
    items = {'a': 1, 'b': 2}
    assert render(chameleon_rs, source, items=items) == render(chameleon, source, items=items)


class Repeat:
    """A user's `repeat`: called with the key and the iterable."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, key: Any, iterable: Any) -> tuple[Any, Any]:
        self.calls.append(key)
        items = list(iterable)
        return items, float(len(items))


def test_user_supplied_repeat() -> None:
    source = '<p tal:repeat="(a, b) [(1, 2), (3, 4)]">${a}${b}</p>\n<i tal:repeat="x \'ab\'">${x}</i>'
    results = []
    for engine in (chameleon, chameleon_rs):
        repeat = Repeat()
        results.append((engine.PageTemplate(source)(repeat=repeat), repeat.calls))
    assert results[0] == results[1]


ITEM_ATTRIBUTES = '|'.join(f'${{r.{a}}}${{type(r.{a}).__name__}}' for a in ATTRIBUTES)


@pytest.mark.parametrize(
    'iterator',
    [lambda: iter([1, 2, 3]), lambda: iter((1, 2)), lambda: (i for i in range(3)), lambda: iter([])],
    ids=['list', 'tuple', 'generator', 'empty'],
)
@pytest.mark.parametrize('length', [3, 2, 10**20], ids=['3', '2', 'big'])
def test_repeat_items_made_by_hand(iterator: Any, length: int) -> None:
    """`RepeatItem` and `RepeatDict` attributes chameleon_rs computes natively (Phase 10), on
    each engine's own classes: iterators with and without `__length_hint__`, huge lengths, a
    missing key, the dict's own names, and a subclass."""
    source = f'<p>{ITEM_ATTRIBUTES}|${{d.r.index}}|${{d.nope | "no"}}|${{d.__class__.__name__}}</p>'
    results = []
    for engine in (chameleon, chameleon_rs):
        item = engine.tal.RepeatItem(iterator(), length)

        class Sub(engine.tal.RepeatDict):  # type: ignore[misc, name-defined]
            pass

        for d in (engine.tal.RepeatDict({'r': item}), Sub({'r': item})):
            results.append(render(engine, source, r=item, d=d))
    assert results[:2] == results[2:]


def test_letters_beyond_z_and_roman_numerals() -> None:
    source = '<i tal:repeat="x items">${repeat.x.letter}${repeat.x.Letter}${repeat.x.roman}</i>'
    items = list(range(60))
    assert render(chameleon_rs, source, items=items) == render(chameleon, source, items=items)


# --- `first` and `last` (malthe/chameleon#144): Zope's, which Chameleon never had -----------


class Obj:
    def __init__(self, kind: str) -> None:
        self.kind = kind


GROUPED = [Obj('a'), Obj('a'), Obj('b'), Obj('c'), Obj('c')]


@pytest.mark.parametrize(
    ('items', 'expected'),
    [
        # Attributes of objects, keys of mappings.
        (GROUPED, 'a:1:0|a:0:1|b:1:1|c:1:0|c:0:1|'),
        ([{'kind': k.kind} for k in GROUPED], 'a:1:0|a:0:1|b:1:1|c:1:0|c:0:1|'),
        ([Obj('x')], 'x:1:1|'),
        ([], ''),
    ],
    ids=['objects', 'mappings', 'one', 'empty'],
)
def test_repeat_first_and_last(items: list[Any], expected: str) -> None:
    source = (
        '<tal:r repeat="o items">${o.kind if hasattr(o, "kind") else o["kind"]}'
        ":${int(repeat.o.first('kind'))}:${int(repeat['o'].last('kind'))}|</tal:r>"
    )
    assert chameleon_rs.PageTemplate(source)(items=items) == expected
    with pytest.raises(AttributeError, match='first'):
        chameleon.PageTemplate(source)(items=items or [Obj('x')])


def test_repeat_first_and_last_group_a_sorted_sequence() -> None:
    # Zope's documented use: a heading before each group, a rule after it.
    source = (
        '<div tal:repeat="o items"><h2 tal:condition="repeat.o.first(\'kind\')">${o.kind}</h2>'
        '<hr tal:condition="repeat.o.last(\'kind\')"/></div>'
    )
    assert chameleon_rs.PageTemplate(source)(items=GROUPED) == (
        '<div><h2>a</h2></div>\n<div><hr/></div>\n<div><h2>b</h2><hr/></div>\n<div><h2>c</h2></div>\n<div><hr/></div>'
    )


def test_repeat_first_and_last_edge_cases() -> None:
    # Without a name, whole items compare; a part missing from either item counts as different.
    source = '<i tal:repeat="x items">${int(repeat.x.first())}${int(repeat.x.last())}${int(repeat.x.first(\'n\'))}</i>'
    assert chameleon_rs.PageTemplate(source)(items=[1, 1, 2]) == '<i>101</i>\n<i>011</i>\n<i>111</i>'
    # After a nested loop of the same name, `repeat.i` is the inner loop's, as for `index`.
    source = (
        '<p tal:repeat="i [1, 1]"><b tal:repeat="i [2, 2]">${int(repeat.i.first())}</b>'
        '${int(repeat.i.first())}${repeat.i.index}</p>'
    )
    assert chameleon_rs.PageTemplate(source)() == '<p><b>1</b>\n<b>0</b>01</p>\n<p><b>1</b>\n<b>0</b>01</p>'
    # A `RepeatItem` made by hand without its items (Chameleon's signature) has everything else.
    item = chameleon_rs.tal.RepeatItem(iter([1, 2]), 2)
    next(item._iterator)
    assert item.start
    with pytest.raises(TypeError, match='need the repeated items'):
        item.first()
