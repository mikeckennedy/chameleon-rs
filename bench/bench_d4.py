"""
D4's revisit criterion, re-measured with the real expression layer (docs/architecture.md, D4):
revisit if a fallback-only build meets both the inline (0.6 us) and partial (0.8 us) targets,
or if the fast path saves less than 20 ns per expression on the benchmark mix.

"Fallback only" compiles every Python expression to the exact fallback (a Python function
built with Chameleon's rewriting), through the private `_fast_path=False` template option.
Native `render_partial` frames stay native in both builds (they are frames, not expression
evaluation); their keyword arguments are single names.

Build first:  make build-release
Run:          venv/bin/python bench/bench_d4.py [--runs 3]

Per expression: an item renders eight copies of one expression; the time of an item with no
expressions is subtracted and the rest divided by eight (spike 1's method, docs/spike-1.md).
"""

import argparse
import statistics
import sys
import timeit
from pathlib import Path

import chameleon_rs

sys.path.insert(0, str(Path(__file__).parent))
from bench_render import make_videos
from bench_skeleton import LISTING, PARTIAL_LISTING, TEMPLATES

N = 500

EXPRESSIONS = [
    'video.id',
    'video.title',
    'video.author',
    "' '.join([])",
    '"{:,}".format(video.views)',
    'video.views',
]


def per_item(fn) -> float:
    number, _ = timeit.Timer(fn).autorange()
    return min(timeit.repeat(fn, number=number, repeat=7)) / number / N * 1e9


def listing(fast: bool) -> chameleon_rs.PageTemplate:
    return chameleon_rs.PageTemplate(LISTING, _fast_path=fast)


def partial_listing(fast: bool) -> chameleon_rs.PageTemplate:
    # The partials themselves come from the configured folder's loader (fast path on): only the
    # listing's own expressions change between the builds.
    chameleon_rs.configure(template_folder=str(TEMPLATES))
    return chameleon_rs.PageTemplate(PARTIAL_LISTING, _fast_path=fast)


def per_expression(expression: str, fast: bool, videos: list) -> float:
    body = '${' + expression + '}'
    full = chameleon_rs.PageTemplate(f'<tal:r repeat="video videos">{body * 8}</tal:r>', _fast_path=fast)
    empty = chameleon_rs.PageTemplate('<tal:r repeat="video videos"></tal:r>', _fast_path=fast)
    return (per_item(lambda: full(videos=videos)) - per_item(lambda: empty(videos=videos))) / 8


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=3)
    args = parser.parse_args()
    videos = make_videos(N)

    templates = {fast: (listing(fast), partial_listing(fast)) for fast in (True, False)}
    for fast in (True, False):
        inline, partials = templates[fast]
        assert inline(videos=videos) == templates[True][0](videos=videos)
        assert partials(videos=videos) == templates[True][1](videos=videos)

    print(f'Python {sys.version.split()[0]}, n={N}, ns per item (min of 7 repeats)')
    for run in range(1, args.runs + 1):
        print(f'run {run}')
        for fast in (True, False):
            inline, partials = templates[fast]
            label = 'fast path + fallback' if fast else 'fallback only'
            inline_ns = per_item(lambda inline=inline: inline(videos=videos))
            partial_ns = per_item(lambda partials=partials: partials(videos=videos))
            print(f'  {label:<22} listing {inline_ns:7.1f}   partial listing {partial_ns:7.1f}')
        print('  per expression (fast / fallback / saved, ns):')
        saved = []
        for expression in EXPRESSIONS:
            fast_ns = per_expression(expression, True, videos)
            slow_ns = per_expression(expression, False, videos)
            saved.append(slow_ns - fast_ns)
            print(f'    {expression:<30}{fast_ns:7.1f} {slow_ns:7.1f} {slow_ns - fast_ns:7.1f}')
        print(f'    median saving {statistics.median(saved):.1f} ns (min {min(saved):.1f})')


if __name__ == '__main__':
    main()
