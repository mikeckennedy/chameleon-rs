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


def test_many_threads_render_the_same_templates(tmp_path: Path, configured: Path) -> None:
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
    assert not sys._is_gil_enabled()  # ty: ignore[unresolved-attribute] - Python 3.13+


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


def test_many_threads_render_nested_partials(tmp_path: Path, configured: Path) -> None:
    # Built-in partials (Phase 9): resolution caches, per-render units and auto_reload checks
    # shared by threads; a failing partial's formatted error in some renders.
    (tmp_path / 'shared').mkdir()
    (tmp_path / 'shared' / 'card.pt').write_text(
        '<div tal:define="d n * 2">${render_partial("shared/_img.pt", n=d)}${1 // (n % 9)}</div>'
    )
    (tmp_path / 'shared' / '_img.pt').write_text('<img tal:repeat="i range(n % 3)" alt="${n}-${repeat.i.index}"/>')
    (tmp_path / 'page.pt').write_text(
        '<p tal:repeat="n ns"><b tal:on-error="string:E">${render_partial("shared/card.pt", n=n)}</b></p>'
    )
    loader = chameleon_rs.PageTemplateLoader(str(tmp_path), auto_reload=True)

    def expected(n: int) -> str:
        items = []
        for k in range(n, n + 3):
            if k % 9 == 0:
                items.append('<p><b>E</b></p>')
            else:
                d = k * 2
                imgs = '\n'.join(f'<img alt="{d}-{i}"/>' for i in range(d % 3))
                items.append(f'<p><b><div>{imgs}{1 // (k % 9)}</div></b></p>')
        return '\n'.join(items)

    def render(n: int) -> tuple[int, str]:
        return n, loader['page.pt'](ns=range(n, n + 3))

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(render, range(3000)))
    for n, out in results:
        assert out == expected(n), n


def test_many_threads_render_async_on_their_own_loops(tmp_path: Path, configured: Path) -> None:
    # Phase 11 (D9): threads each running an event loop with many concurrent `render_async`
    # calls of the same templates (macros, slots, translations, native partials suspending,
    # errors caught after a suspension, opt-in partial fan-out and @concurrent values), while
    # other threads render the same templates synchronously. On a free-threaded build this is
    # the stress suite (run it with `-W error::RuntimeWarning`).
    import asyncio

    (tmp_path / 'card.pt').write_text('<div tal:define="d data">${d}:${aw(n)}${1 // (n % 7)}</div>')
    layout = chameleon_rs.PageTemplate(LAYOUT + ITEMS)
    body = (
        PAGE.replace('Page ${n}', 'Page ${aw(n)}')
        .replace('${i}</b>', '${aw(i)}</b>')
        .replace(
            '</div></html>',
            '<p tal:on-error="string:E">${render_partial("card.pt", n=n, data=aw(n), aw=aw)}</p>'
            '${late(n)}</div></html>',
        )
    )
    page = chameleon_rs.PageTemplate(body)
    fanned = chameleon_rs.PageTemplate(body, concurrent=True)

    def translate(msgid: str, **kw: object) -> str:
        return f'<{msgid}|{kw.get("domain")}>'

    def sync_aw(v: object) -> object:
        return v

    expected = {n: page(layout=layout, n=n, translate=translate, aw=sync_aw, late=sync_aw) for n in range(8)}
    assert '<p>E</p>' in expected[0] and '[<b>2</b>]' in expected[3]

    async def aw(v: object) -> object:
        await asyncio.sleep(0)
        return v

    late = chameleon_rs.concurrent(aw)

    async def many(start: int) -> list[tuple[int, str]]:
        async def one(n: int) -> tuple[int, str]:
            template = fanned if n % 2 else page
            return n, await template.render_async(layout=layout, n=n % 8, translate=translate, aw=aw, late=late)

        return await asyncio.gather(*(one(n) for n in range(start, start + 50)))

    def in_thread(start: int) -> list[tuple[int, str]]:
        if start % 3 == 0:
            return [
                (n, page(layout=layout, n=n % 8, translate=translate, aw=sync_aw, late=sync_aw))
                for n in range(start, start + 50)
            ]
        return asyncio.run(many(start))

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = [r for chunk in pool.map(in_thread, range(0, 50 * 96, 50)) for r in chunk]
    assert len(results) == 50 * 96
    for n, out in results:
        assert out == expected[n % 8], n
