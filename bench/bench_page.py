"""
App-shaped page benchmark (Phase 10): a page that fills a layout macro's slots (through
`load:`), with a navigation loop, a signed-in user block, helper calls (`cache_id`,
`format_date`), `structure:` and `__html__` values, and a list of 50 episode cards with
nested loops and attribute chains. Two variants: the cards inline, and each card through
`render_partial` (chameleon_partials for Chameleon; the native prototype registry for
chameleon_rs until Phase 9 builds partials in). Outputs are asserted identical first.

Build first:  make build-release
Run:          venv/bin/python bench/bench_page.py [--runs 3] [--episodes 50]
"""

import argparse
import datetime
import statistics
import sys
import timeit
from dataclasses import dataclass, field
from pathlib import Path

import chameleon
import chameleon.zpt.template
import chameleon_partials
import chameleon_rs

TEMPLATES = Path(__file__).parent / 'templates'
CARD = 'app/_episode_card.pt'


class Html(str):
    """A `Markup`-like value: rendered as is, never escaped."""

    __slots__ = ()

    def __html__(self) -> str:
        return self


@dataclass
class Guest:
    name: str
    slug: str


@dataclass
class Episode:
    number: int
    title: str
    slug: str
    published: datetime.date
    duration_text: str
    listens: int
    featured: bool
    summary: Html
    guests: list[Guest] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class Link:
    url: str
    title: str


@dataclass
class Site:
    name: str
    footer_html: str
    footer_links: list[Link]


@dataclass
class User:
    name: str
    avatar: str
    is_admin: bool


CACHE_IDS = {
    '/static/css/site.css': '3f2a91bc',
    '/static/css/theme.css': '77d01e2a',
    '/static/js/site.js': 'a0c4e911',
}


def cache_id(path: str) -> str:
    return CACHE_IDS.get(path, '0')


MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def format_date(d: datetime.date) -> str:
    # Not `strftime`, which costs more than the rest of a card (time.strftime's locale work).
    return f'{MONTHS[d.month - 1]} {d.day:02d}, {d.year}'


def make_model(episodes: int) -> dict:
    eps = [
        Episode(
            number=500 - i,
            title=f'Episode {500 - i}: Python & "fast" <templates>'
            if i % 7 == 0
            else f'Building things with Python {i}',
            slug=f'building-things-{i}',
            published=datetime.date(2026, 1, 1) + datetime.timedelta(days=7 * i),
            duration_text=f'{40 + i % 30} min',
            listens=10_000 + i * 1_337,
            featured=i % 10 == 0,
            summary=Html(f'<p>We talk about <em>topic {i}</em> and more.</p>'),
            guests=[Guest(f'Guest {i}', f'guest-{i}')] + ([Guest(f'Co-host {i}', f'co-{i}')] if i % 3 == 0 else []),
            tags=['python', 'web'] if i % 2 else ['python'],
        )
        for i in range(episodes)
    ]
    return {
        'title': 'All episodes',
        'intro_html': 'Every episode, <strong>newest first</strong>.',
        'episodes': eps,
        'site': Site(
            'Podcast', '<a href="/privacy">Privacy</a>', [Link('/about', 'About'), Link('/contact', 'Contact')]
        ),
        'nav': [Link('/', 'Home'), Link('/episodes', 'Episodes'), Link('/guests', 'Guests'), Link('/about', 'About')],
        'active': '/episodes',
        'user': User('Sam Example', '/static/img/sam.png', False),
        'year': 2026,
        'cache_id': cache_id,
        'format_date': format_date,
    }


def engines(model: dict) -> list[tuple[str, object, object]]:
    """(label, Chameleon render, chameleon_rs render) per variant."""
    chameleon_partials.register_extensions(str(TEMPLATES), auto_reload=False, cache_init=False)
    registry: dict[str, chameleon_rs.PageTemplate] = {}
    registry[CARD] = chameleon_rs.PageTemplate((TEMPLATES / CARD).read_text(), partials=registry)
    pairs = []
    for label, name, partials in (
        ('inline cards', 'app/episodes_inline.pt', None),
        ('partial cards', 'app/episodes_partials.pt', registry),
    ):
        path = str(TEMPLATES / name)
        ref = chameleon.zpt.template.PageTemplateFile(path)
        ours = chameleon_rs.PageTemplateFile(path, partials=partials)
        if partials is None:
            pairs.append((label, lambda ref=ref: ref(**model), lambda ours=ours: ours(**model)))
        else:
            rp = chameleon_partials.render_partial
            pairs.append(
                (label, lambda ref=ref, rp=rp: ref(render_partial=rp, **model), lambda ours=ours: ours(**model))
            )
    return pairs


def scenario(name: str):
    """A chameleon_rs render for profiling (`page_inline`, `page_partials`)."""
    model = make_model(50)
    pairs = {label.split()[0]: ours for label, _, ours in engines(model)}
    return pairs[name.removeprefix('page_')]


def per_render(fn, repeat: int = 7) -> tuple[float, float]:
    number, _ = timeit.Timer(fn).autorange()
    times = [t / number * 1e6 for t in timeit.repeat(fn, number=number, repeat=repeat)]
    return min(times), statistics.median(times)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--episodes', type=int, default=50)
    args = parser.parse_args()

    model = make_model(args.episodes)
    pairs = engines(model)
    for label, ref, ours in pairs:
        assert ours() == ref(), f'{label}: output differs'

    print(f'Python {sys.version.split()[0]}, {args.episodes} episodes, us per render (min / median of 7 repeats)')
    for run in range(1, args.runs + 1):
        print(f'run {run}')
        for label, ref, ours in pairs:
            ref_min, ref_mid = per_render(ref)
            our_min, our_mid = per_render(ours)
            print(
                f'  {label:<14} Chameleon {ref_min:8.1f} {ref_mid:8.1f}   chameleon_rs {our_min:7.1f} {our_mid:7.1f}'
                f'   {ref_min / our_min:4.1f}x'
            )


if __name__ == '__main__':
    main()
