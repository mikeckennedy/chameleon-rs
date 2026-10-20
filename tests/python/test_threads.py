"""Concurrent renders of shared templates (frozen programs, per-call state; D7, D9)."""

from concurrent.futures import ThreadPoolExecutor

import chameleon_rs

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
