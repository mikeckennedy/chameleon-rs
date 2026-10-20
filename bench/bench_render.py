"""
Baseline benchmarks for Python Chameleon, to compare the Rust engine against later.

Scenarios:
  1. listing page using render_partial (the chameleon_partials demo app), 50 and 500 videos
  2. the same page with the partials inlined, to isolate per-partial overhead
  3. auto_reload on vs. off
  4. a dashboard with slow helpers (file hash + simulated 20ms I/O calls)
  5. event-loop blocking: sync render() inside an asyncio app

Run: venv/bin/python bench/bench_render.py
"""

import asyncio
import hashlib
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import chameleon_partials
from chameleon import PageTemplateLoader
from pydantic import BaseModel

TEMPLATES = Path(__file__).parent / 'templates'
ASSETS = Path(__file__).parent / 'assets'


class Video(BaseModel):
    id: str
    title: str
    url: str
    author: str
    views: int
    category: str | None = None


def make_videos(n: int) -> list[Video]:
    return [
        Video(
            id=f'vid{i:05d}xyz',
            title=f'Video number {i} about Python & "templates" <fast>',
            url=f'https://www.youtube.com/watch?v=vid{i:05d}xyz',
            author=f'Author {i % 17}',
            views=1_000_000 - i * 37,
            category='Python',
        )
        for i in range(n)
    ]


# ---------- slow helpers for the dashboard scenario ----------

def ensure_assets() -> None:
    ASSETS.mkdir(exist_ok=True)
    for name in ('site.css', 'app.js'):
        f = ASSETS / name
        if not f.exists():
            f.write_bytes(os.urandom(512 * 1024))


def build_cache_id(filename: str) -> str:
    # Deliberately uncached: read the file and hash it (hashlib releases the GIL on big inputs).
    return hashlib.sha256((ASSETS / filename).read_bytes()).hexdigest()[:12]


def fetch_stat(name: str) -> str:
    # Stand-in for an HTTP call or SQLite query: 20ms of GIL-releasing wait.
    time.sleep(0.020)
    return f'{len(name) * 1234:,}'


# ---------- timing helpers ----------

def time_it(fn, *, min_time: float = 1.0, min_runs: int = 5) -> tuple[float, float]:
    """Returns (median seconds, stdev seconds) per call."""
    fn()  # warm up (compile, caches)
    samples: list[float] = []
    start = time.perf_counter()
    while len(samples) < min_runs or time.perf_counter() - start < min_time:
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples), (statistics.stdev(samples) if len(samples) > 1 else 0.0)


def fmt(sec: float) -> str:
    if sec < 1e-3:
        return f'{sec * 1e6:8.1f} µs'
    return f'{sec * 1e3:8.2f} ms'


def setup(auto_reload: bool) -> PageTemplateLoader:
    chameleon_partials.register_extensions(str(TEMPLATES), auto_reload=auto_reload, cache_init=False)
    return PageTemplateLoader(str(TEMPLATES), auto_reload=auto_reload)


def render(loader: PageTemplateLoader, name: str, **model) -> str:
    return loader[name].render(render_partial=chameleon_partials.render_partial, **model)


# ---------- scenarios ----------

def bench_listing() -> None:
    print('\n== Listing page (layout macro + loop) ==')
    print(f'{"scenario":<44}{"median":>12}{"per item":>12}')
    for auto_reload in (False, True):
        loader = setup(auto_reload)
        for n in (50, 500):
            videos = make_videos(n)
            for tpl, label in (('home/listing.pt', 'partials'), ('home/listing_inline.pt', 'inline')):
                med, _ = time_it(lambda: render(loader, tpl, videos=videos))
                name = f'{label:<9} n={n:<4} auto_reload={auto_reload}'
                print(f'{name:<44}{fmt(med):>12}{fmt(med / n):>12}')


def bench_output_check() -> None:
    loader = setup(False)
    videos = make_videos(3)
    a = render(loader, 'home/listing.pt', videos=videos)
    b = render(loader, 'home/listing_inline.pt', videos=videos)
    norm = lambda s: ''.join(s.split())  # noqa: E731
    print(f'\n(sanity) partial vs inline output identical ignoring whitespace: {norm(a) == norm(b)}')


def bench_dashboard() -> None:
    print('\n== Dashboard with slow helpers (2 file hashes, 3 x 20ms I/O, 20 partials) ==')
    ensure_assets()
    loader = setup(False)
    videos = make_videos(20)
    model = dict(videos=videos, build_cache_id=build_cache_id, fetch_stat=fetch_stat)
    med, _ = time_it(lambda: render(loader, 'home/dashboard_slow.pt', **model), min_time=2.0)
    print(f'{"full render, sequential (Chameleon today)":<52}{fmt(med):>12}')

    calls = [(build_cache_id, 'site.css'), (build_cache_id, 'app.js'),
             (fetch_stat, 'subscribers'), (fetch_stat, 'downloads'), (fetch_stat, 'revenue')]
    med_seq, _ = time_it(lambda: [f(a) for f, a in calls], min_time=2.0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        med_par, _ = time_it(lambda: list(pool.map(lambda c: c[0](c[1]), calls)), min_time=2.0)
    print(f'{"  just the 5 slow helpers, sequential":<52}{fmt(med_seq):>12}')
    print(f'{"  just the 5 slow helpers, warm pool (GIL Python)":<52}{fmt(med_par):>12}')
    print(f'{"  => ceiling for a concurrent render":<52}{fmt(med - med_seq + med_par):>12}')


async def measure_loop_lag(work, duration: float = 2.0) -> tuple[float, float, int]:
    """Run `work` (a coroutine fn) repeatedly while a 1ms ticker measures event-loop lag."""
    lags: list[float] = []
    stop = False

    async def ticker():
        while not stop:
            t0 = time.perf_counter()
            await asyncio.sleep(0.001)
            lags.append(time.perf_counter() - t0 - 0.001)

    t = asyncio.create_task(ticker())
    count = 0
    end = time.perf_counter() + duration
    while time.perf_counter() < end:
        await work()
        count += 1
    stop = True
    await t
    lags.sort()
    return lags[int(len(lags) * 0.99)], lags[-1], count


def bench_event_loop() -> None:
    print('\n== Event-loop blocking (asyncio, single thread, like Granian ST) ==')
    ensure_assets()
    loader = setup(False)
    v500 = make_videos(500)
    v20 = make_videos(20)
    slow_model = dict(videos=v20, build_cache_id=build_cache_id, fetch_stat=fetch_stat)

    async def listing_sync():
        render(loader, 'home/listing.pt', videos=v500)
        await asyncio.sleep(0.005)  # idle gap so the ticker is fully serviced between renders

    async def dashboard_sync():
        render(loader, 'home/dashboard_slow.pt', **slow_model)
        await asyncio.sleep(0.005)  # idle gap so the ticker is fully serviced between renders

    async def dashboard_to_thread():
        await asyncio.to_thread(render, loader, 'home/dashboard_slow.pt', **slow_model)

    for label, work in (('listing n=500, sync render()', listing_sync),
                        ('dashboard, sync render()', dashboard_sync),
                        ('dashboard, asyncio.to_thread(render)', dashboard_to_thread)):
        p99, worst, count = asyncio.run(measure_loop_lag(work))
        print(f'{label:<40} loop lag p99 {fmt(p99)}  worst {fmt(worst)}  ({count} renders)')


if __name__ == '__main__':
    print(f'Python {sys.version.split()[0]}, GIL enabled: {sys._is_gil_enabled()}')
    bench_output_check()
    bench_listing()
    bench_dashboard()
    bench_event_loop()
