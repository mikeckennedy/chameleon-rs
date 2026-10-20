"""The native partial frame prototype (Phase 2 gate; Phase 9 builds the real feature)."""

import chameleon_partials
import pytest
from chameleon_rs import PageTemplate

IMAGE = '<img src="${video}" class="${\' \'.join(classes or [])}">'
SQUARE = '<div>${render_partial("image", video=video, classes=["a", "b"])}</div>'


def test_partial_renders_into_the_callers_output() -> None:
    image = PageTemplate(IMAGE)
    square = PageTemplate(SQUARE, partials={'image': image})
    page = PageTemplate(
        '<p tal:repeat="v videos">${render_partial("square", video=v)}</p>',
        partials={'image': image, 'square': square},
    )
    out = page(videos=['x', 'y<'])
    assert out == ('<p><div><img src="x" class="a b"></div></p>\n<p><div><img src="y&lt;" class="a b"></div></p>')


def test_partial_scope_is_fresh() -> None:
    inner = PageTemplate('${secret | "hidden"}|${shown}')
    outer = PageTemplate('${render_partial("inner", shown=1)}', partials={'inner': inner})
    assert outer(secret='leak') == 'hidden|1'


def test_partial_in_attribute_and_content() -> None:
    inner = PageTemplate('a&amp;b')
    outer = PageTemplate(
        '<a title="${render_partial(\'i\')}" tal:content="render_partial(\'i\')">d</a>', partials={'i': inner}
    )
    assert outer() == '<a title="a&amp;b">a&amp;b</a>'


def test_partial_with_loop_gets_its_own_repeat() -> None:
    inner = PageTemplate('<i tal:repeat="x xs">${repeat.x.index}</i>')
    outer = PageTemplate(
        '<b tal:repeat="x xs">${repeat.x.index}${render_partial("inner", xs=[7, 8])}${repeat.x.index}</b>',
        partials={'inner': inner},
    )
    assert outer(xs=[1]) == '<b>0<i>0</i>\n<i>1</i>0</b>'


def test_explicit_render_partial_wins() -> None:
    # A render_partial passed in (chameleon_partials style) is called like any function.
    outer = PageTemplate('${render_partial("x", a=1)}', partials={})
    assert outer(render_partial=lambda name, **kw: f'<{name} {kw}>') == '&lt;x {&#39;a&#39;: 1}&gt;'.replace(
        '&#39;', "'"
    )
    chameleon_partials  # noqa: B018 - imported to show the old API stays compatible


def test_missing_partial() -> None:
    with pytest.raises(ValueError, match='Partial not found'):
        PageTemplate('${render_partial("nope")}', partials={})()
    with pytest.raises(NameError):
        PageTemplate('${render_partial("nope")}')()


def test_infinite_partial_recursion_is_a_recursion_error() -> None:
    partials: dict[str, PageTemplate] = {}
    partials['loop'] = PageTemplate('${render_partial("loop")}', partials=partials)
    with pytest.raises(RecursionError):
        partials['loop']()
