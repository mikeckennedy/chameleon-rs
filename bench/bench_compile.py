"""
Baseline compile ("cook") time for Python Chameleon: parse + compile + exec of the generated
module, measured per template.

Covers the upstream fixtures plus the benchmark templates. Templates are loaded as files
(like real apps, and so `load:` is available). Chameleon's MemoryLoader never caches, so each
new PageTemplateFile compiles from scratch.

Run: venv/bin/python bench/bench_compile.py
"""

import statistics
import time
from pathlib import Path

from chameleon import PageTemplateFile
from chameleon.exc import TemplateError

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / 'tests' / 'upstream' / 'chameleon_tests' / 'inputs'
BENCH_TEMPLATES = ROOT / 'bench' / 'templates'


def compile_time(path: Path, repeat: int = 20) -> float:
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        PageTemplateFile(str(path), strict=False).cook_check()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def main() -> None:
    results: list[tuple[str, int, float]] = []
    skipped = 0
    paths = sorted(FIXTURES.glob('*.pt')) + sorted(BENCH_TEMPLATES.rglob('*.pt'))
    for path in paths:
        body = path.read_bytes()
        try:
            results.append((str(path.relative_to(ROOT)), len(body), compile_time(path)))
        except TemplateError:
            skipped += 1  # fixtures that intentionally fail to compile

    times = [t for _, _, t in results]
    total_bytes = sum(size for _, size, _ in results)
    print(f'{len(results)} templates compiled ({skipped} skipped: intentional compile errors)')
    print(f'median {statistics.median(times) * 1e3:.2f} ms, mean {statistics.mean(times) * 1e3:.2f} ms, '
          f'max {max(times) * 1e3:.2f} ms per template')
    print(f'throughput {total_bytes / sum(times) / 1024:.0f} KiB/s of template source')
    print('\nslowest 5:')
    for name, size, t in sorted(results, key=lambda r: r[2], reverse=True)[:5]:
        print(f'  {t * 1e3:7.2f} ms  {size:6d} B  {name}')
    print('\nbenchmark templates:')
    for name, size, t in results:
        if name.startswith('bench/'):
            print(f'  {t * 1e3:7.2f} ms  {size:6d} B  {name}')


if __name__ == '__main__':
    main()
