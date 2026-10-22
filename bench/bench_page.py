"""
App-shaped page benchmark (Phase 10): a page that fills a layout macro's slots (through
`load:`), with a navigation loop, a signed-in user block, helper calls (`cache_id`,
`format_date`), `structure:` and `__html__` values, and a list of 50 episode cards with
nested loops and attribute chains. Two variants: the cards inline, and each card through
`render_partial` (chameleon_partials for Chameleon; the built-in for chameleon_rs). Outputs are asserted identical first.

Build first:  make build-release
Run:          venv/bin/python bench/bench_page.py [--runs 3] [--episodes 50]
"""

import argparse
import datetime
import statistics
import sys
import timeit
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chameleon
import chameleon.zpt.template
import chameleon_partials
import chameleon_rs

TEMPLATES = Path(__file__).parent / 'templates'
CARD = 'app/_episode_card.pt'
REPORT = 'app/_report.pt'


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


class Row:
    """A report row as an application's model presents it: plain attributes, properties
    computed on each access, and a method."""

    def __init__(self, i: int) -> None:
        self.number = 1000 - i
        self.title = f'Episode {1000 - i}: notes & links' if i % 9 == 0 else f'Show {i} about Python and the web'
        self.published = datetime.date(2026, 1, 1) - datetime.timedelta(days=i)
        self.guests = [Guest(f'Guest {i}', f'guest-{i}')] + ([Guest(f'Co {i}', f'co-{i}')] if i % 4 == 0 else [])
        self.revenue = 1234.5 + i * 3.25
        self.listens = 5_000 + i * 17
        self.video_id = f'v{i:05d}' if i % 3 else None
        self.summary = Html(f'<p>Summary <em>{i}</em></p>')

    @property
    def url(self) -> str:
        return f'/episodes/show/{self.number}'

    @property
    def is_recent(self) -> bool:
        return self.published > datetime.date(2025, 9, 1)

    @property
    def badge_html(self) -> str:
        return '<span class="badge">new</span>' if self.is_recent else ''

    @property
    def guest_names(self) -> str:
        return ', '.join(g.name for g in self.guests)

    @property
    def has_video(self) -> bool:
        return self.video_id is not None

    @property
    def video_url(self) -> str:
        return f'https://www.youtube.com/watch?v={self.video_id}'

    def short_title(self) -> str:
        return self.title if len(self.title) < 30 else self.title[:27] + '...'


def money(value: float) -> str:
    return f'${value:,.2f}'


def pct(part: int, whole: int) -> str:
    return f'{100 * part / whole:.1f}%'


def make_report_model(rows: int) -> dict:
    model = make_model(0)
    items = [Row(i) for i in range(rows)]
    model.update(
        title='Report',
        rows=items,
        money=money,
        pct=pct,
        total_listens=sum(r.listens for r in items),
    )
    return model


def engines(model: dict, report: dict | None = None) -> list[tuple[str, Callable[[], Any], Callable[[], Any]]]:
    """(label, Chameleon render, chameleon_rs render) per variant."""
    chameleon_partials.register_extensions(str(TEMPLATES), auto_reload=False, cache_init=False)
    chameleon_rs.configure(template_folder=str(TEMPLATES))

    def bound(template, kw):
        return lambda: template(**kw)

    pairs = []
    for label, name, partials, kw in (
        ('inline cards', 'app/episodes_inline.pt', None, model),
        ('partial cards', 'app/episodes_partials.pt', True, model),
        ('report', 'app/report.pt', True, report or make_report_model(1000)),
    ):
        path = str(TEMPLATES / name)
        ref_kw = kw if partials is None else {**kw, 'render_partial': chameleon_partials.render_partial}
        ref = bound(chameleon.zpt.template.PageTemplateFile(path), ref_kw)
        ours = bound(chameleon_rs.PageTemplateFile(path), kw)
        pairs.append((label, ref, ours))
    return pairs


def scenario(name: str):
    """A chameleon_rs render for profiling (`page_inline`, `page_partials`, `page_report`)."""
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
