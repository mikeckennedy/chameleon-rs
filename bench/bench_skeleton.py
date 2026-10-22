"""
Phase 2 walking-skeleton benchmark (docs/plan.md): the cut-down listing rendered by Chameleon
and by chameleon_rs, inline and through partials, per item.

Build first:  make build-release
Run:          venv/bin/python bench/bench_skeleton.py [--runs 3]

The partial figure is the built-in `render_partial` (Phase 9: path resolution from the
configured folder, native frames); the gate is 10x Chameleon + chameleon_partials, timed in
the same run.

Each figure is the per-item time of one full render of N videos; a run reports the min and
median of seven timeit repeats, and the script does `--runs` runs. Outputs are asserted
byte-identical between the engines before anything is timed.
"""

import argparse
import statistics
import sys
import timeit
from pathlib import Path

import chameleon
import chameleon_partials
import chameleon_rs

sys.path.insert(0, str(Path(__file__).parent))
from bench_render import make_videos

TEMPLATES = Path(__file__).parent / 'templates'
N = 500

# bench/templates/home/listing_inline.pt without the layout macro (spike 1's listing loop).
LISTING = (TEMPLATES / 'home' / 'listing_loop.pt').read_text()

# bench/templates/home/listing.pt without the layout macro: two partials per video.
PARTIAL_LISTING = """<div class="container videos listing">
    <span class="video" tal:repeat="v videos">
        ${ render_partial('shared/partials/video_square.pt', video=v) }
    </span>
</div>
"""
PARTIALS = ('shared/partials/video_square.pt', 'shared/partials/video_image.pt')


def per_item(fn, repeat: int = 7) -> tuple[float, float]:
    """(min, median) nanoseconds per item over `repeat` timeit repeats."""
    number, _ = timeit.Timer(fn).autorange()
    times = [t / number / N * 1e9 for t in timeit.repeat(fn, number=number, repeat=repeat)]
    return min(times), statistics.median(times)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=3)
    args = parser.parse_args()

    videos = make_videos(N)

    ref_listing = chameleon.PageTemplate(LISTING)
    rs_listing = chameleon_rs.PageTemplate(LISTING)
    assert rs_listing(videos=videos) == ref_listing(videos=videos), 'listing output differs'

    chameleon_partials.register_extensions(str(TEMPLATES), auto_reload=False, cache_init=False)
    ref_partials = chameleon.PageTemplate(PARTIAL_LISTING)
    chameleon_rs.configure(template_folder=str(TEMPLATES))
    rs_partials = chameleon_rs.PageTemplate(PARTIAL_LISTING)

    def ref_partial_render() -> str:
        return ref_partials(videos=videos, render_partial=chameleon_partials.render_partial)

    assert rs_partials(videos=videos) == ref_partial_render(), 'partial listing output differs'

    scenarios = [
        ('listing, Chameleon', lambda: ref_listing(videos=videos)),
        ('listing, chameleon_rs', lambda: rs_listing(videos=videos)),
        ('partial listing, Chameleon + chameleon_partials', ref_partial_render),
        ('partial listing, chameleon_rs built-in render_partial', lambda: rs_partials(videos=videos)),
    ]
    print(f'Python {sys.version.split()[0]}, n={N}, ns per item (min / median of 7 repeats)')
    for run in range(1, args.runs + 1):
        print(f'run {run}')
        for label, fn in scenarios:
            low, mid = per_item(fn)
            print(f'  {label:<62}{low:9.1f} {mid:9.1f}')


if __name__ == '__main__':
    main()
