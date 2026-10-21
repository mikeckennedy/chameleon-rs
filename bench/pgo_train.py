"""
Training workload for profile-guided optimisation (`make build-pgo`): renders the benchmark
scenarios (listing, native partials, macros and slots, translation, the app page with its
layout and partial cards, a tiny template) and compiles every upstream fixture, a fixed number
of times, without timing anything, so the profile is the same on every run. chameleon_rs only.

Run by `make build-pgo` against the instrumented build; not a benchmark.
"""

import sys
from pathlib import Path

import chameleon_rs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'bench'))

from bench_macros import CARDS, TRANSLATED
from bench_page import CARD, TEMPLATES, make_model
from bench_render import make_videos
from bench_skeleton import LISTING, PARTIAL_LISTING, PARTIALS

ROUNDS = 40


def main() -> None:
    videos = make_videos(200)
    registry: dict[str, chameleon_rs.PageTemplate] = {}
    for name in PARTIALS:
        registry[name] = chameleon_rs.PageTemplate((TEMPLATES / name).read_text(), partials=registry)
    cards: dict[str, chameleon_rs.PageTemplate] = {}
    cards[CARD] = chameleon_rs.PageTemplate((TEMPLATES / CARD).read_text(), partials=cards)
    model = make_model(50)
    renders = [
        (chameleon_rs.PageTemplate(LISTING), {'videos': videos}),
        (chameleon_rs.PageTemplate(PARTIAL_LISTING, partials=registry), {'videos': videos}),
        (chameleon_rs.PageTemplate(CARDS), {'videos': videos}),
        (chameleon_rs.PageTemplate(TRANSLATED), {'videos': videos}),
        (chameleon_rs.PageTemplateFile(str(TEMPLATES / 'app' / 'episodes_inline.pt')), model),
        (chameleon_rs.PageTemplateFile(str(TEMPLATES / 'app' / 'episodes_partials.pt'), partials=cards), model),
        (chameleon_rs.PageTemplateFile(str(TEMPLATES / 'home' / 'listing_inline.pt')), {'videos': videos[:50]}),
    ]
    tiny = chameleon_rs.PageTemplate('<p class="${c}">${x}</p>')
    for _ in range(ROUNDS):
        for template, kw in renders:
            template(**kw)
        for i in range(200):
            tiny(x=i, c='a&b')
    fixtures = sorted((ROOT / 'tests' / 'upstream' / 'chameleon_tests' / 'inputs').glob('*.pt'))
    for _ in range(3):
        for path in fixtures:
            try:
                chameleon_rs.PageTemplateFile(str(path), strict=False).cook_check()
            except Exception:  # noqa: BLE001, S112 - some fixtures fail to compile on purpose
                continue


if __name__ == '__main__':
    main()
