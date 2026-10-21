"""Concurrent renders of shared templates (frozen programs, per-call state; D7, D9)."""

import re
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor

import chameleon_rs
import pytest

LISTING = '<ul>\n  <li tal:repeat="v videos" class="${v[0]}">${v[1]} ${repeat.v.index}</li>\n</ul>'


def test_many_threads_render_the_same_templates() -> None:
    inner = chameleon_rs.PageTemplate('<b title="${x}">${x}</b>')
    page = chameleon_rs.PageTemplate(LISTING + '${render_partial("inner", x=n)}', partials={'inner': inner})

    def render(n: int) -> tuple[int, str]:
        videos = [(f'c{n}', f'<{i}>') for i in range(n % 7)]
        return n, page(videos=videos, n=n)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(render, range(800)))
    for n, out in results:
        items = '\n  '.join(f'<li class="c{n}">&lt;{i}&gt; {i}</li>' for i in range(n % 7))
        assert out == f'<ul>\n  {items}\n</ul><b title="{n}">{n}</b>'


def test_free_threaded_builds_keep_the_gil_disabled() -> None:
    # The extension declares `gil_used = false`; importing one that did not would re-enable
    # the GIL on a free-threaded interpreter (with a RuntimeWarning).
    if not sysconfig.get_config_var('Py_GIL_DISABLED'):
        pytest.skip('not a free-threaded interpreter')
    assert not sys._is_gil_enabled()  # type: ignore[attr-defined]


ERROR_PAGE = (
    '<?python def twice(v): return v * 2 ?>'
    '<ul><li tal:repeat="i items" tal:on-error="string:E${repeat.i.index}" class="${i}">'
    '${twice(i)}${100 // (i - 3)}</li></ul>'
    '<p tal:define="d {}" tal:on-error="error.type.__name__">${d.missing}</p>'
)


def test_many_threads_render_errors_and_code_blocks() -> None:
    page = chameleon_rs.PageTemplate(ERROR_PAGE)
    failing = chameleon_rs.PageTemplate('<p tal:define="x n">${x}${1 // (n % 5)}</p>')

    def render(n: int) -> tuple[str, str]:
        try:
            other = failing(n=n)
        except ZeroDivisionError as e:
            # The formatted render error (addresses differ between calls).
            other = re.sub(r'0x[0-9a-f]+', '0x?', str(e))
        return page(items=list(range(n % 6))), other

    expected = [render(n)[0] for n in range(6)]
    assert expected[4] == '<ul><li>E3</li></ul><p>AttributeError</p>'
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(render, range(4000)))
    for n, (out, other) in enumerate(results):
        assert out == expected[n % 6]
        if n % 5:
            assert other == f'<p>{n}{1 // (n % 5)}</p>'
        else:
            assert other.startswith('division by zero\n\n - Expression: "1 // (n % 5)"')
            assert f'x: {n}' in other
