"""Concurrent renders of shared templates (frozen programs, per-call state; D7, D9)."""

import re
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import chameleon_rs
import pytest

try:
    1 // 0  # noqa: B018 - the running Python's own message ("division by zero" on 3.14+)
except ZeroDivisionError as error:
    ZERO_DIVISION = str(error)

LISTING = '<ul>\n  <li tal:repeat="v videos" class="${v[0]}">${v[1]} ${repeat.v.index}</li>\n</ul>'


def test_many_threads_render_the_same_templates(tmp_path: Path) -> None:
    (tmp_path / 'inner.pt').write_text('<b title="${x}">${x}</b>')
    (tmp_path / 'page.pt').write_text(LISTING + '${render_partial("inner.pt", x=n)}')
    page = chameleon_rs.PageTemplateLoader(str(tmp_path))['page.pt']

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
            assert other.startswith(f'{ZERO_DIVISION}\n\n - Expression: "1 // (n % 5)"')
            assert f'x: {n}' in other


LAYOUT = (
    '<html metal:define-macro="page" i18n:domain="site"><title metal:define-slot="title">T</title>'
    '<body><div metal:define-slot="body">no body</div><p i18n:translate="">Hello <b i18n:name="who">${who}</b></p>'
    '</body></html>'
)
PAGE = (
    '<html metal:use-macro="layout.macros[\'page\']" tal:define="who n">'
    '<title metal:fill-slot="title">Page ${n}</title>'
    '<div metal:fill-slot="body"><i tal:repeat="i range(n % 4)" metal:use-macro="layout.macros[\'item\']">'
    '<b metal:fill-slot="label">${i}</b></i></div></html>'
)
ITEMS = '<tal:d condition="False"><u metal:define-macro="item">[<b metal:define-slot="label"/>]</u></tal:d>'


def test_many_threads_render_macros_slots_and_translations() -> None:
    import chameleon

    def translate(msgid: str, **kw: object) -> str:
        return f'<{msgid}|{kw.get("domain")}>'

    layout = chameleon_rs.PageTemplate(LAYOUT + ITEMS)
    page = chameleon_rs.PageTemplate(PAGE)
    reference = chameleon.PageTemplate(PAGE)
    reference_layout = chameleon.PageTemplate(LAYOUT + ITEMS)

    def render(n: int) -> tuple[int, str]:
        return n, page(layout=layout, n=n % 8, translate=translate)

    expected = {n: reference(layout=reference_layout, n=n, translate=translate) for n in range(8)}
    assert '<Hello ${who}|site>' in expected[3]
    assert '[<b>2</b>]' in expected[3]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(render, range(4000)))
    for n, out in results:
        assert out == expected[n % 8]
