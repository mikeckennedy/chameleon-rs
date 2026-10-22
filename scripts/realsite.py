"""Real-site verification (plan.md, Phase 8): render every page of a web application with stock
Chameleon and with chameleon-rs, and compare the HTML byte for byte.

The application is described by an *adapter*, a Python file given by path (it lives outside
this repository, with the application). It defines:

    def setup() -> None                      # import and configure the app (may be async)
    def routes() -> list[str]                # every GET rule of the routing table
    def urls() -> list[tuple[str, str]]      # (rule, concrete URL) pairs to render
    async def fetch(url) -> (status, content_type, body_bytes)
    def teardown() -> None                   # optional (may be async)
    FROZEN_TIME = '2026-01-01 12:00:00'      # optional: the clock every request sees

Each engine runs in its own process (`crawl`), with everything that could make two runs differ
pinned rather than masked: `PYTHONHASHSEED=0`, the clock frozen (freezegun, which must be
installed in the application's environment), `random` seeded and `os.urandom` replaced by a
seeded generator before each request (so CSRF tokens, uuid4 and `secrets` repeat), and every
outbound network connection refused. `compare` then reports identical and differing pages,
writing a minimal diff of each difference.

    python scripts/realsite.py crawl --engine chameleon --adapter ADAPTER --out DIR_A
    python scripts/realsite.py crawl --engine rust --adapter ADAPTER --out DIR_B
    python scripts/realsite.py compare DIR_A DIR_B --report REPORT_DIR
    python scripts/realsite.py crawl --engine rust --adapter ADAPTER --out DIR --timing 15
    python scripts/realsite.py timings DIR_A DIR_B
    python scripts/realsite.py crawl --engine chameleon --shadow --adapter ADAPTER --out DIR

With `--shadow`, stock Chameleon serves and `chameleon_rs.shadow` repeats every render with
chameleon-rs, recording the differences it reports in the manifest.

With `--timing N`, each page's outermost template render is repeated N more times inside its
request (same arguments) and the median recorded; repeated renders must give the same output.
Outputs and reports contain the application's pages: keep them outside this repository.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import importlib.util
import inspect
import json
import os
import random
import socket
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# The real clock, kept in a container: freezegun swaps module attributes that are time functions.
_CLOCK = (time.perf_counter,)


def _perf_counter() -> float:
    return _CLOCK[0]()


def _maybe_await(value: Any, timeout: float | None = None) -> Any:
    if inspect.isawaitable(value):
        return asyncio.get_event_loop().run_until_complete(asyncio.wait_for(value, timeout))
    return value


# ---------------------------------------------------------------------------------------------
# Pinning


class _NetworkRefused(OSError):
    pass


def _refuse_network() -> None:
    """Refuse every outbound connection (AF_UNIX socket pairs, used by asyncio, still work)."""

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(self: socket.socket, address: Any) -> Any:
        if self.family == socket.AF_UNIX:
            return original_connect(self, address)
        raise _NetworkRefused(f'realsite: network access refused ({address!r})')

    def connect_ex(self: socket.socket, address: Any) -> Any:
        if self.family == socket.AF_UNIX:
            return original_connect_ex(self, address)
        raise _NetworkRefused(f'realsite: network access refused ({address!r})')

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        raise _NetworkRefused(f'realsite: name resolution refused ({host!r})')

    # Monkeypatched for the run, with looser signatures than the originals.
    socket.socket.connect = connect  # ty: ignore[invalid-assignment]
    socket.socket.connect_ex = connect_ex  # ty: ignore[invalid-assignment]
    socket.getaddrinfo = getaddrinfo  # ty: ignore[invalid-assignment]
    socket.create_connection = lambda address, *a, **k: (_ for _ in ()).throw(  # ty: ignore[invalid-assignment]
        _NetworkRefused(f'realsite: network access refused ({address!r})')
    )


def _reseed(url: str) -> None:
    """Make every source of randomness a function of the URL alone."""
    seed = int.from_bytes(hashlib.sha256(url.encode('utf-8')).digest()[:8], 'big')
    random.seed(seed)
    generator = random.Random(seed ^ 0x5EED)

    def urandom(n: int) -> bytes:
        return generator.randbytes(n)

    os.urandom = urandom  # ty: ignore[invalid-assignment] - monkeypatched for the run
    random._urandom = urandom  # ty: ignore[unresolved-attribute] - SystemRandom, hence `secrets`


# ---------------------------------------------------------------------------------------------
# Render instrumentation


class _RenderProbe:
    """Wraps `chameleon.PageTemplate.render` to find each request's outermost render: its time
    and, with repeats, the median of re-rendering it with the same arguments."""

    def __init__(self, repeats: int) -> None:
        self.repeats = repeats
        self.depth = 0
        self.samples: list[float] = []
        self.first: float | None = None
        self.unstable = False
        self.templates: list[str] = []

    def reset(self) -> None:
        self.samples = []
        self.first = None
        self.unstable = False
        self.templates = []

    def install(self) -> None:
        import chameleon

        cls = chameleon.PageTemplate
        original = cls.render
        probe = self

        def render(self: Any, *args: Any, **kwargs: Any) -> Any:
            if probe.depth:
                return original(self, *args, **kwargs)
            probe.depth += 1
            try:
                started = _perf_counter()
                result = original(self, *args, **kwargs)
                elapsed = _perf_counter() - started
                probe.templates.append(str(getattr(self, 'filename', '<string>')))
                if probe.first is None:
                    probe.first = elapsed
                for _ in range(probe.repeats):
                    started = _perf_counter()
                    again = original(self, *args, **kwargs)
                    probe.samples.append(_perf_counter() - started)
                    if again != result:
                        probe.unstable = True
                return result
            finally:
                probe.depth -= 1

        cls.render = render  # type: ignore[method-assign]


# ---------------------------------------------------------------------------------------------
# crawl


def _load_adapter(path: str) -> Any:
    sys.path.insert(0, str(Path(path).resolve().parent))  # the adapter's own helper modules
    spec = importlib.util.spec_from_file_location('realsite_adapter', path)
    if spec is None or spec.loader is None:
        raise SystemExit(f'cannot load adapter {path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules['realsite_adapter'] = module
    spec.loader.exec_module(module)
    return module


def _select_engine(engine: str) -> str:
    if engine == 'rust':
        import chameleon_rs.compat

        chameleon_rs.compat.install()
    import chameleon

    origin = str(Path(chameleon.__file__).resolve())
    is_rust = 'chameleon_rs' in origin
    if is_rust != (engine == 'rust'):
        raise SystemExit(f'engine {engine!r} requested but `import chameleon` gives {origin}')
    return origin


REPOSITORY = Path(__file__).resolve().parent.parent


def _outside_repository(path: str) -> Path:
    """Pages and reports hold an application's content: never inside this (public) repository."""
    resolved = Path(path).resolve()
    if resolved == REPOSITORY or REPOSITORY in resolved.parents:
        raise SystemExit(f'refusing to write {resolved}: it is inside the chameleon-rs repository')
    return resolved


def crawl(args: argparse.Namespace) -> int:
    if os.environ.get('PYTHONHASHSEED') != '0':
        os.environ['PYTHONHASHSEED'] = '0'
        os.execv(sys.executable, [sys.executable, *sys.argv])

    out = _outside_repository(args.out)
    (out / 'pages').mkdir(parents=True, exist_ok=True)
    origin = _select_engine(args.engine)
    differences: list[Any] = []
    if args.shadow:
        if args.engine != 'chameleon':
            raise SystemExit('--shadow runs on stock Chameleon (--engine chameleon)')
        import chameleon_rs.shadow

        chameleon_rs.shadow.install(report=differences.append)
    _refuse_network()
    asyncio.set_event_loop(asyncio.new_event_loop())

    adapter = _load_adapter(args.adapter)
    frozen = getattr(adapter, 'FROZEN_TIME', '2026-01-01 12:00:00')

    import freezegun

    freezer = freezegun.freeze_time(frozen, real_asyncio=True)
    freezer.start()
    try:
        _reseed('setup')
        _maybe_await(adapter.setup())
        probe = _RenderProbe(args.timing)
        probe.install()
        rules = list(adapter.routes())
        pairs = list(adapter.urls())
        if args.limit:
            pairs = pairs[: args.limit]
        manifest: list[dict[str, Any]] = []
        for index, (rule, url) in enumerate(pairs):
            probe.reset()
            _reseed(url)
            started = _perf_counter()
            try:
                status, content_type, body = _maybe_await(adapter.fetch(url), args.request_timeout)
                error = None
            except Exception as exc:  # noqa: BLE001 - recorded and compared like a page
                status, content_type, body = -1, 'error', f'{type(exc).__name__}: {exc}'.encode()
                error = type(exc).__name__
            request_time = _perf_counter() - started
            name = f'{index:05d}.html'
            (out / 'pages' / name).write_bytes(body)
            manifest.append(
                {
                    'rule': rule,
                    'url': url,
                    'status': status,
                    'content_type': content_type,
                    'sha256': hashlib.sha256(body).hexdigest(),
                    'file': name,
                    'error': error,
                    'rendered': probe.first is not None,
                    'templates': probe.templates,
                    'render_first_s': probe.first,
                    'render_median_s': statistics.median(probe.samples) if probe.samples else None,
                    'render_unstable': probe.unstable,
                    'request_s': request_time,
                }
            )
            if args.verbose:
                print(f'{index:5d} {status} {url}', flush=True)
        if hasattr(adapter, 'teardown'):
            _maybe_await(adapter.teardown())
    finally:
        freezer.stop()

    meta = {'engine': args.engine, 'chameleon': origin, 'routes': rules, 'pages': manifest}
    if args.shadow:
        meta['shadow_differences'] = [{'filename': d.filename, 'diff': d.diff} for d in differences]
        print(f'shadow mode: {len(differences)} renders differed')
    (out / 'manifest.json').write_text(json.dumps(meta, indent=1), encoding='utf-8')
    rendered = sum(1 for page in manifest if page['rendered'])
    print(f'{args.engine}: {len(manifest)} URLs fetched, {rendered} rendered a template, {len(rules)} rules')
    return 0


# ---------------------------------------------------------------------------------------------
# compare


def _lines(body: bytes) -> list[str]:
    text = body.decode('utf-8', 'surrogateescape')
    return text.replace('>', '>\n').splitlines(keepends=True)


def compare(args: argparse.Namespace) -> int:
    report = _outside_repository(args.report) if args.report else None
    first = json.loads((Path(args.a) / 'manifest.json').read_text(encoding='utf-8'))
    second = json.loads((Path(args.b) / 'manifest.json').read_text(encoding='utf-8'))
    if report:
        report.mkdir(parents=True, exist_ok=True)
    pages_b = {page['url']: page for page in second['pages']}
    identical = differing = missing = 0
    rendered_identical = 0
    rules_seen: set[str] = set()
    summary: list[str] = []
    for page in first['pages']:
        other = pages_b.get(page['url'])
        if other is None:
            missing += 1
            continue
        rules_seen.add(page['rule'])
        body_a = (Path(args.a) / 'pages' / page['file']).read_bytes()
        body_b = (Path(args.b) / 'pages' / other['file']).read_bytes()
        if page['status'] == other['status'] and body_a == body_b:
            identical += 1
            rendered_identical += page['rendered'] and other['rendered']
            continue
        differing += 1
        line = f'DIFF {page["status"]}/{other["status"]} {page["url"]}'
        summary.append(line)
        if report:
            diff = difflib.unified_diff(_lines(body_a), _lines(body_b), first['engine'], second['engine'], n=2)
            (report / f'{page["file"]}.diff').write_text(f'{line}\n' + ''.join(diff), encoding='utf-8')
    uncovered = sorted(set(first['routes']) - rules_seen)
    print(
        f'{len(first["pages"])} URLs, {len(rules_seen)} of {len(first["routes"])} rules covered; '
        f'{identical} identical ({rendered_identical} rendered by a template), {differing} differ, '
        f'{missing} missing'
    )
    for line in summary:
        print(line)
    if args.verbose and uncovered:
        print('rules not covered:')
        for rule in uncovered:
            print('  ', rule)
    return 1 if differing or missing else 0


def timings(args: argparse.Namespace) -> int:
    runs = [json.loads((Path(d) / 'manifest.json').read_text(encoding='utf-8')) for d in (args.a, args.b)]
    by_url = [{p['url']: p for p in run['pages'] if p['render_median_s'] is not None} for run in runs]
    common = sorted(set(by_url[0]) & set(by_url[1]))
    if not common:
        print('no page has timings in both runs (crawl with --timing N)')
        return 1
    a = [by_url[0][u]['render_median_s'] for u in common]
    b = [by_url[1][u]['render_median_s'] for u in common]
    ratios = [x / y for x, y in zip(a, b, strict=True) if y > 0]
    names = [run['engine'] for run in runs]
    print(f'{len(common)} pages with a template render in both runs')
    print(
        f'total of per-page medians: {names[0]} {sum(a) * 1e3:.1f} ms, {names[1]} {sum(b) * 1e3:.1f} ms '
        f'({sum(a) / sum(b):.2f}x)'
    )
    print(
        f'median page: {names[0]} {statistics.median(a) * 1e6:.0f} us, {names[1]} {statistics.median(b) * 1e6:.0f} us'
    )
    quantiles = statistics.quantiles(ratios, n=10)
    print(
        f'per-page speedup {names[0]}/{names[1]}: median {statistics.median(ratios):.2f}x, '
        f'p10 {quantiles[0]:.2f}x, p90 {quantiles[-1]:.2f}x, min {min(ratios):.2f}x, max {max(ratios):.2f}x'
    )
    unstable = [u for u in common if by_url[0][u]['render_unstable'] or by_url[1][u]['render_unstable']]
    print(f'pages whose repeated renders differed: {len(unstable)}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('crawl')
    p.add_argument('--engine', choices=['chameleon', 'rust'], required=True)
    p.add_argument('--adapter', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--timing', type=int, default=0, help='repeat each outermost render N times')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--shadow', action='store_true', help='stock Chameleon with chameleon_rs.shadow installed')
    p.add_argument('--request-timeout', type=float, default=120.0, help='seconds before a request counts as failed')
    p.add_argument('-v', '--verbose', action='store_true')
    p.set_defaults(func=crawl)
    p = sub.add_parser('compare')
    p.add_argument('a')
    p.add_argument('b')
    p.add_argument('--report')
    p.add_argument('-v', '--verbose', action='store_true')
    p.set_defaults(func=compare)
    p = sub.add_parser('timings')
    p.add_argument('a')
    p.add_argument('b')
    p.set_defaults(func=timings)
    args = parser.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
