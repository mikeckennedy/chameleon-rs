"""
Phase 11 benchmarks: `render_async`, opt-in concurrency, and the render-offload experiment.

Scenarios:
  dashboard  The dashboard with slow helpers (bench_render.py's, made async: 3 x 20 ms I/O
             through `asyncio.sleep`, 2 file hashes through `asyncio.to_thread`, 20 partials):
             sync render with blocking helpers (Chameleon's way), `render_async` awaiting each
             helper in turn, and `render_async` with `@concurrent` helpers. Gate: near the
             25 ms ceiling.
  lag        Event-loop lag (a 1 ms ticker) while `render_async` renders a 1,000-row report
             whose rows each await a value. Gate: under 1 ms.
  serve      A synthetic ASGI-like server (the offload experiment): one asyncio loop, closed-
             loop clients issuing requests, each request 2 ms of awaited I/O then a render;
             heavy requests render the 1,000-row report (~1.5 ms), light ones a small page.
             Modes: `sync` (render() on the loop), `async` (render_async), `offload` (render()
             in a worker thread, `loop.run_in_executor`). Reports throughput, latency p50/p99
             per kind, and loop lag p99/max.

Build first:  make build-release   (and, for free-threaded numbers, a 3.14t venv: see docs/plan.md)
Run:          venv/bin/python bench/bench_async.py [dashboard|lag|serve ...] [--seconds 5]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import chameleon_rs

sys.path.insert(0, str(Path(__file__).parent))
from bench_page import make_model, make_report_model
from bench_render import make_videos

TEMPLATES = Path(__file__).parent / 'templates'
ASSETS = Path(__file__).parent / 'assets'


def ensure_assets() -> None:
    ASSETS.mkdir(exist_ok=True)
    for name in ('site.css', 'app.js'):
        f = ASSETS / name
        if not f.exists():
            f.write_bytes(os.urandom(512 * 1024))


def hash_file(filename: str) -> str:
    return hashlib.sha256((ASSETS / filename).read_bytes()).hexdigest()[:12]


def fetch_stat_blocking(name: str) -> str:
    time.sleep(0.020)
    return f'{len(name) * 1234:,}'


async def fetch_stat(name: str) -> str:
    await asyncio.sleep(0.020)
    return f'{len(name) * 1234:,}'


async def build_cache_id(filename: str) -> str:
    return await asyncio.to_thread(hash_file, filename)


def fmt(sec: float) -> str:
    return f'{sec * 1e6:8.1f} us' if sec < 1e-3 else f'{sec * 1e3:8.2f} ms'


# -- dashboard ------------------------------------------------------------------------


def bench_dashboard(seconds: float) -> None:
    print('\n== Dashboard with slow helpers (2 file hashes, 3 x 20 ms I/O, 20 partials) ==')
    ensure_assets()
    chameleon_rs.configure(template_folder=str(TEMPLATES))
    page = chameleon_rs.PageTemplateFile(str(TEMPLATES / 'home' / 'dashboard_slow.pt'))
    videos = make_videos(20)

    def timed(fn) -> float:
        fn()
        samples = []
        end = time.perf_counter() + seconds
        while time.perf_counter() < end or len(samples) < 5:
            t0 = time.perf_counter()
            fn()
            samples.append(time.perf_counter() - t0)
        return statistics.median(samples)

    sync_model = {'videos': videos, 'build_cache_id': hash_file, 'fetch_stat': fetch_stat_blocking}
    want = page.render(**sync_model)
    loop = asyncio.new_event_loop()

    def run_async(**model):
        return lambda: loop.run_until_complete(page.render_async(**model))

    async_model = {'videos': videos, 'build_cache_id': build_cache_id, 'fetch_stat': fetch_stat}
    concurrent_model = {
        'videos': videos,
        'build_cache_id': chameleon_rs.concurrent(build_cache_id),
        'fetch_stat': chameleon_rs.concurrent(fetch_stat),
    }
    for model in (async_model, concurrent_model):
        assert loop.run_until_complete(page.render_async(**model)) == want
    rows = [
        ('render(), blocking helpers (sequential)', timed(lambda: page.render(**sync_model))),
        ('render_async(), async helpers awaited in turn', timed(run_async(**async_model))),
        ('render_async(), @concurrent helpers', timed(run_async(**concurrent_model))),
    ]
    for label, med in rows:
        print(f'{label:<52}{fmt(med):>12}')
    start = time.perf_counter()
    for _ in range(20):
        hash_file('site.css')
    hash_time = (time.perf_counter() - start) / 20
    print(f'{"  ceiling: one 20 ms wait + one file hash":<52}{fmt(0.020 + hash_time):>12}')
    loop.close()


# -- event-loop lag during render_async -----------------------------------------------


async def ticker_lags(stop: asyncio.Event, lags: list[float]) -> None:
    while not stop.is_set():
        t0 = time.perf_counter()
        await asyncio.sleep(0.001)
        lags.append(time.perf_counter() - t0 - 0.001)


def bench_lag(seconds: float) -> None:
    print('\n== Event-loop lag while render_async renders (1 ms ticker) ==')
    chameleon_rs.configure(template_folder=str(TEMPLATES))
    page = chameleon_rs.PageTemplateFile(str(TEMPLATES / 'app' / 'report.pt'))
    model = make_report_model(1000)

    async def fetch(value):
        await asyncio.sleep(0)
        return value

    # Every row's summary is awaited: 1,000 suspensions per render.
    for row in model['rows']:
        row.async_summary = row.summary
    async_rows = chameleon_rs.PageTemplate(
        '<table><tr tal:repeat="row rows"><td>${row.number}</td><td>${fetch(row.summary)}</td>'
        '<td>${money(row.revenue)}</td></tr></table>'
    )

    async def scenario(label: str, work) -> None:
        lags: list[float] = []
        stop = asyncio.Event()
        tick = asyncio.create_task(ticker_lags(stop, lags))
        count = 0
        renders = []
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            t0 = time.perf_counter()
            await work()
            renders.append(time.perf_counter() - t0)
            count += 1
            await asyncio.sleep(0.002)
        stop.set()
        await tick
        lags.sort()
        print(
            f'{label:<48} render {fmt(statistics.median(renders))}  lag p99 {fmt(lags[int(len(lags) * 0.99)])}'
            f'  max {fmt(lags[-1])}  ({count} renders)'
        )

    async def main() -> None:
        await scenario('report, render() (blocks the loop)', lambda: _sync(page, model))
        await scenario('rows awaiting a value each, render_async()', lambda: async_rows.render_async(fetch=fetch, **model))

    asyncio.run(main())


async def _sync(page, model):
    page.render(**model)


# -- serve: the offload experiment ----------------------------------------------------


def bench_serve(
    seconds: float, clients: int, heavy_every: int, threads: int, modes: list[str], heavy_kind: str
) -> None:
    gil = getattr(sys, '_is_gil_enabled', lambda: True)()
    print(
        f'\n== Synthetic server: {clients} clients, 1 heavy request in {heavy_every}, 2 ms awaited I/O per request,'
        f' {threads} worker threads for offload (GIL {"enabled" if gil else "disabled"}) =='
    )
    chameleon_rs.configure(template_folder=str(TEMPLATES))
    heavy = chameleon_rs.PageTemplateFile(str(TEMPLATES / 'app' / 'report.pt'))
    light = chameleon_rs.PageTemplateFile(str(TEMPLATES / 'app' / 'episodes_inline.pt'))
    heavy_model = make_report_model(1000)
    if heavy_kind == 'transcript':
        # A transcript page: one long escaped text (the GIL is released while it is escaped).
        heavy = chameleon_rs.PageTemplate('<article><h1>${title}</h1><pre>${text}</pre></article>')
        line = 'Michael: so what about <templates> & "async" rendering? Guest: it depends.\n'
        heavy_model = {'title': 'Transcript', 'text': line * 20_000}
    print(f'heavy page: {heavy_kind}')
    light_model = make_model(5)
    for page, model in ((heavy, heavy_model), (light, light_model)):
        page.render(**model)
    pool = ThreadPoolExecutor(max_workers=threads)

    async def handle(mode: str, page, model) -> None:
        await asyncio.sleep(0.002)  # the request's awaited I/O (database, cache)
        if mode == 'sync':
            page.render(**model)
        elif mode == 'async':
            await page.render_async(**model)
        else:
            await asyncio.get_running_loop().run_in_executor(pool, lambda: page.render(**model))

    async def serve(mode: str) -> None:
        latencies: dict[str, list[float]] = {'heavy': [], 'light': []}
        lags: list[float] = []
        stop = asyncio.Event()
        tick = asyncio.create_task(ticker_lags(stop, lags))
        counter = 0
        end = time.perf_counter() + seconds

        async def client() -> None:
            nonlocal counter
            while time.perf_counter() < end:
                counter += 1
                kind = 'heavy' if counter % heavy_every == 0 else 'light'
                page, model = (heavy, heavy_model) if kind == 'heavy' else (light, light_model)
                t0 = time.perf_counter()
                await handle(mode, page, model)
                latencies[kind].append(time.perf_counter() - t0)

        started = time.perf_counter()
        await asyncio.gather(*(client() for _ in range(clients)))
        elapsed = time.perf_counter() - started
        stop.set()
        await tick
        total = sum(len(v) for v in latencies.values())

        def q(values: list[float], p: float) -> str:
            values = sorted(values)
            return fmt(values[min(len(values) - 1, int(len(values) * p))]) if values else '-'

        print(
            f'{mode:<8} {total / elapsed:8.0f} req/s   light p50 {q(latencies["light"], 0.5)} p99'
            f' {q(latencies["light"], 0.99)}   heavy p50 {q(latencies["heavy"], 0.5)} p99 {q(latencies["heavy"], 0.99)}'
            f'   loop lag p99 {q(lags, 0.99)} max {q(lags, 1.0)}'
        )

    async def main() -> None:
        for mode in modes:
            await serve(mode)

    asyncio.run(main())
    pool.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('scenarios', nargs='*', default=['dashboard', 'lag', 'serve'])
    parser.add_argument('--seconds', type=float, default=3.0)
    parser.add_argument('--clients', type=int, default=32)
    parser.add_argument('--heavy-every', type=int, default=5)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--heavy', default='report', choices=['report', 'transcript'])
    parser.add_argument('--modes', default='sync,async,offload')
    parser.add_argument('--rounds', type=int, default=1)
    args = parser.parse_args()
    print(f'Python {sys.version.split()[0]}, GIL enabled: {getattr(sys, "_is_gil_enabled", lambda: True)()}')
    for _ in range(args.rounds):
        if 'dashboard' in args.scenarios:
            bench_dashboard(args.seconds)
        if 'lag' in args.scenarios:
            bench_lag(args.seconds)
        if 'serve' in args.scenarios:
            bench_serve(
                args.seconds, args.clients, args.heavy_every, args.threads, args.modes.split(','), args.heavy
            )


if __name__ == '__main__':
    main()
