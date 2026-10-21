"""
Macro, slot and translation benchmark (Phase 6b): Chameleon and chameleon_rs render the same
pages, outputs asserted byte-identical first.

Scenarios:
  1. layout page: `home/listing_inline.pt`, a page that fills the `content` slot of the
     `shared/_layout.pt` macro (through `load:`) with a 50-video listing; per render.
  2. macro per item: a 500-video listing where each video is a macro call (`use-macro`) that
     fills a slot of the card macro; per item.
  3. translations: 500 items, each an `i18n:translate` element with two `i18n:name`s and a
     translated attribute, with the default translation function; per item.

Build first:  make build-release
Run:          venv/bin/python bench/bench_macros.py [--runs 3]
"""

import argparse
import statistics
import sys
import timeit
from pathlib import Path

import chameleon
import chameleon.zpt.template
import chameleon_rs

sys.path.insert(0, str(Path(__file__).parent))
from bench_render import make_videos

TEMPLATES = Path(__file__).parent / 'templates'

CARDS = """<tal:defs condition="False">
<div metal:define-macro="card" class="card">
    <a href="https://www.youtube.com/watch?v=${ video.id }" class="title">${ video.title }</a>
    <div class="body" metal:define-slot="body">no body</div>
    <div class="views">${ "{:,}".format(video.views) } views</div>
</div>
</tal:defs>
<div class="container videos listing">
    <span class="video" tal:repeat="video videos">
        <div metal:use-macro="macros['card']">
            <p metal:fill-slot="body" class="author">${ video.author }</p>
        </div>
    </span>
</div>
"""

TRANSLATED = """<div class="container videos listing">
    <p tal:repeat="video videos" i18n:translate="" title="${video.title}" i18n:attributes="title">
        Video <b i18n:name="title">${ video.title }</b> by <i i18n:name="author">${ video.author }</i>
    </p>
</div>
"""


def per_unit(fn, units: int, repeat: int = 7) -> tuple[float, float]:
    """(min, median) nanoseconds per unit over `repeat` timeit repeats."""
    number, _ = timeit.Timer(fn).autorange()
    times = [t / number / units * 1e9 for t in timeit.repeat(fn, number=number, repeat=repeat)]
    return min(times), statistics.median(times)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=3)
    args = parser.parse_args()

    few, many = make_videos(50), make_videos(500)
    page = str(TEMPLATES / 'home' / 'listing_inline.pt')
    pairs = [
        (
            'layout page (50 videos), per render',
            chameleon.zpt.template.PageTemplateFile(page),
            chameleon_rs.PageTemplateFile(page),
            {'videos': few},
            1,
        ),
        (
            'macro call per item (500), per item',
            chameleon.PageTemplate(CARDS),
            chameleon_rs.PageTemplate(CARDS),
            {'videos': many},
            len(many),
        ),
        (
            'translated element per item (500), per item',
            chameleon.PageTemplate(TRANSLATED),
            chameleon_rs.PageTemplate(TRANSLATED),
            {'videos': many},
            len(many),
        ),
    ]
    for label, ref, ours, kw, _ in pairs:
        assert ours(**kw) == ref(**kw), f'{label}: output differs'

    print(f'Python {sys.version.split()[0]}, ns (min / median of 7 repeats)')
    for run in range(1, args.runs + 1):
        print(f'run {run}')
        for label, ref, ours, kw, units in pairs:
            ref_min, ref_mid = per_unit(lambda ref=ref, kw=kw: ref(**kw), units)
            our_min, our_mid = per_unit(lambda ours=ours, kw=kw: ours(**kw), units)
            print(f'  {label:<46} Chameleon {ref_min:9.0f} {ref_mid:9.0f}')
            print(f'  {"":<46} chameleon_rs {our_min:6.0f} {our_mid:9.0f}   {ref_min / our_min:4.1f}x')


if __name__ == '__main__':
    main()
