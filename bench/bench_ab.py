"""
Same-process A/B of the listing benchmark: a baseline build (any git ref) against the
installed build, measured interleaved in one process after a Chameleon render, as
`bench_skeleton.py` does. This is the per-phase regression check (docs/plan.md, "How speed is
measured"): run-to-run drift on a busy machine is larger than the effects being measured, so
the two builds are compared side by side rather than against numbers from another day.

Build the current tree first:  make build-release
Run:                           venv/bin/python bench/bench_ab.py <git-ref> [--rounds 8] [--tolerance 5]

The baseline is built with `maturin build --release` in a temporary git worktree and loaded
as the package `chameleon_rs_base` (its Python files are rewritten to import themselves under
that name). Its native code still imports `chameleon_rs.tal`, `chameleon_rs._rewrite` and
`chameleon_rs.exc` by name, which resolve to the current package: fine for the benchmark
templates (fast-path expressions only), not a general way to run old builds.

Prints both builds' per-item times for each round, Chameleon's time in the same process, and
the median difference; exits with status 1 if the median regression exceeds the tolerance.
"""

import argparse
import importlib
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import timeit
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'bench'))

N = 500
LISTING = (ROOT / 'bench' / 'templates' / 'home' / 'listing_loop.pt').read_text()


def build_baseline(ref: str, into: Path) -> None:
    """Builds `ref` and unpacks it as the package `chameleon_rs_base` under `into`."""
    worktree = into / 'worktree'
    subprocess.run(['git', '-C', str(ROOT), 'worktree', 'add', '-q', '--detach', str(worktree), ref], check=True)
    try:
        env = {**os.environ, 'PYO3_PYTHON': sys.executable}
        wheels = into / 'wheels'
        subprocess.run(
            [
                str(Path(sys.executable).parent / 'maturin'),
                'build',
                '--release',
                '-q',
                '-i',
                sys.executable,
                '-o',
                str(wheels),
            ],
            cwd=worktree,
            env=env,
            check=True,
        )
        wheel = next(wheels.glob('*.whl'))
        with zipfile.ZipFile(wheel) as z:
            z.extractall(into / 'unpacked')
    finally:
        subprocess.run(['git', '-C', str(ROOT), 'worktree', 'remove', '--force', str(worktree)], check=True)
    package = into / 'chameleon_rs_base'
    shutil.move(str(into / 'unpacked' / 'chameleon_rs'), package)
    for path in package.glob('*.py'):
        text = re.sub(r'\bchameleon_rs\b', 'chameleon_rs_base', path.read_text())
        path.write_text(text)


def per_item(fn) -> float:
    number, _ = timeit.Timer(fn).autorange()
    return min(timeit.repeat(fn, number=number, repeat=7)) / number / N * 1e9


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('ref', help='git ref of the baseline build, for example the previous phase commit')
    parser.add_argument('--rounds', type=int, default=8)
    parser.add_argument('--tolerance', type=float, default=5.0, help='allowed median regression, ns per item')
    args = parser.parse_args()

    import chameleon
    import chameleon_rs
    from bench_render import make_videos

    with tempfile.TemporaryDirectory() as tmp:
        build_baseline(args.ref, Path(tmp))
        sys.path.insert(0, tmp)
        base = importlib.import_module('chameleon_rs_base')

        videos = make_videos(N)
        ref = chameleon.PageTemplate(LISTING)
        new = chameleon_rs.PageTemplate(LISTING)
        old = base.PageTemplate(LISTING)
        assert new(videos=videos) == old(videos=videos) == ref(videos=videos), 'outputs differ'

        chameleon_ns = per_item(lambda: ref(videos=videos))
        rows = [
            (per_item(lambda: old(videos=videos)), per_item(lambda: new(videos=videos))) for _ in range(args.rounds)
        ]

    deltas = [b - a for a, b in rows]
    median = statistics.median(deltas)
    current = statistics.median(b for _, b in rows)
    print(f'Python {sys.version.split()[0]}, n={N}, listing ns per item (min of 7 repeats), interleaved')
    print(f'  Chameleon             {chameleon_ns:7.0f}')
    print(f'  baseline ({args.ref}) ' + ' '.join(f'{a:5.0f}' for a, _ in rows))
    print('  current              ' + ' '.join(f'{b:5.0f}' for _, b in rows))
    print(
        f'  median difference    {median:+.1f} ns (range {min(deltas):+.0f}..{max(deltas):+.0f}), '
        f'tolerance +{args.tolerance:g} ns'
    )
    print(f'  current vs Chameleon {chameleon_ns / current:.2f}x')
    return 1 if median > args.tolerance else 0


if __name__ == '__main__':
    sys.exit(main())
