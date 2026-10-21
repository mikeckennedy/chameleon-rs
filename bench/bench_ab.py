"""
Same-process A/B of the listing benchmark: a baseline build (any git ref) against the
installed build, measured interleaved in one process after a Chameleon render, as
`bench_skeleton.py` does. This is the per-phase regression check (docs/plan.md, "How speed is
measured"): run-to-run drift on a busy machine is larger than the effects being measured, so
the two builds are compared side by side rather than against numbers from another day.

Build the current tree first:  make build-release
Run:                           venv/bin/python bench/bench_ab.py <git-ref> [--rounds 8] [--tolerance 5]
                               [--scenario listing|macro|page] [--keep DIR]

The baseline is built with `maturin build --release` from the ref's files (`git archive`), loaded
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
    # The ref's files exported with `git archive` (no worktree is registered in the repository).
    source = into / 'source'
    source.mkdir()
    archive = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', ref], check=True, capture_output=True)
    subprocess.run(['tar', '-x', '-C', str(source)], input=archive.stdout, check=True)
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
        cwd=source,
        env=env,
        check=True,
    )
    wheel = next(wheels.glob('*.whl'))
    with zipfile.ZipFile(wheel) as z:
        z.extractall(into / 'unpacked')
    package = into / 'chameleon_rs_base'
    shutil.move(str(into / 'unpacked' / 'chameleon_rs'), package)
    for path in package.glob('*.py'):
        text = re.sub(r'\bchameleon_rs\b', 'chameleon_rs_base', path.read_text())
        path.write_text(text)


def per_item(fn, units: int = N) -> float:
    number, _ = timeit.Timer(fn).autorange()
    return min(timeit.repeat(fn, number=number, repeat=7)) / number / units * 1e9


def renders(scenario: str, chameleon, current, base) -> tuple[list, int]:
    """(Chameleon, baseline, current) render functions for `scenario`, and units per render."""
    from bench_render import make_videos

    def bound(template, kw):
        return lambda: template(**kw)

    if scenario in ('listing', 'macro'):
        from bench_macros import CARDS

        kw = {'videos': make_videos(N)}
        source = LISTING if scenario == 'listing' else CARDS
        return [bound(e.PageTemplate(source), kw) for e in (chameleon, base, current)], N
    if scenario == 'page':
        import chameleon.zpt.template
        from bench_page import TEMPLATES, make_model

        path = str(TEMPLATES / 'app' / 'episodes_inline.pt')
        classes = (chameleon.zpt.template.PageTemplateFile, base.PageTemplateFile, current.PageTemplateFile)
        return [bound(cls(path), make_model(50)) for cls in classes], 1
    if scenario == 'report':
        import chameleon.zpt.template
        import chameleon_partials
        from bench_page import REPORT, TEMPLATES, make_report_model

        chameleon_partials.register_extensions(str(TEMPLATES), auto_reload=False, cache_init=False)
        model = make_report_model(1000)
        path = str(TEMPLATES / 'app' / 'report.pt')
        ours = []
        for engine in (base, current):
            if partial_registry(engine):
                registry: dict = {}
                registry[REPORT] = engine.PageTemplate((TEMPLATES / REPORT).read_text(), partials=registry)
                ours.append(bound(engine.PageTemplateFile(path, partials=registry), model))
            else:
                engine.configure(template_folder=str(TEMPLATES))
                ours.append(bound(engine.PageTemplateFile(path), model))
        ref = chameleon.zpt.template.PageTemplateFile(path)
        return [bound(ref, {**model, 'render_partial': chameleon_partials.render_partial}), *ours], 1
    if scenario == 'partials':
        import chameleon_partials
        from bench_render import make_videos
        from bench_skeleton import PARTIAL_LISTING, PARTIALS, TEMPLATES

        chameleon_partials.register_extensions(str(TEMPLATES), auto_reload=False, cache_init=False)
        videos = make_videos(N)
        ours = []
        for engine in (base, current):
            if partial_registry(engine):
                registry = {}
                for name in PARTIALS:
                    registry[name] = engine.PageTemplate((TEMPLATES / name).read_text(), partials=registry)
                ours.append(bound(engine.PageTemplate(PARTIAL_LISTING, partials=registry), {'videos': videos}))
            else:
                engine.configure(template_folder=str(TEMPLATES))
                ours.append(bound(engine.PageTemplate(PARTIAL_LISTING), {'videos': videos}))
        ref = chameleon.PageTemplate(PARTIAL_LISTING)
        return [bound(ref, {'videos': videos, 'render_partial': chameleon_partials.render_partial}), *ours], N
    raise SystemExit(f'unknown scenario {scenario!r}')


def partial_registry(engine) -> bool:
    """Whether a build has the Phase 2 prototype (`PageTemplate(partials={...})`) instead of
    the built-in partials (`configure`, Phase 9)."""
    return not hasattr(engine, 'configure')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('ref', help='git ref of the baseline build, for example the previous phase commit')
    parser.add_argument('--rounds', type=int, default=8)
    parser.add_argument('--tolerance', type=float, default=5.0, help='allowed median regression, ns per item')
    parser.add_argument(
        '--scenario',
        default='listing',
        choices=('listing', 'macro', 'page', 'report', 'partials'),
        help='listing (the gate, ns per item), macro (a macro call filling a slot, ns per item) or '
        'page (bench_page.py inline cards, ns per render), report (its 1,000-row report page) or '
        'partials (the partial listing, ns per item)',
    )
    parser.add_argument('--keep', help='build the baseline into this directory once and reuse it')
    args = parser.parse_args()

    import chameleon
    import chameleon_rs

    with tempfile.TemporaryDirectory() as tmp:
        where = Path(args.keep).resolve() if args.keep else Path(tmp)
        if not (where / 'chameleon_rs_base').exists():
            where.mkdir(parents=True, exist_ok=True)
            build_baseline(args.ref, where)
        sys.path.insert(0, str(where))
        base = importlib.import_module('chameleon_rs_base')

        (ref, old, new), units = renders(args.scenario, chameleon, chameleon_rs, base)
        assert new() == old() == ref(), 'outputs differ'

        chameleon_ns = per_item(ref, units)
        rows = [(per_item(old, units), per_item(new, units)) for _ in range(args.rounds)]

    deltas = [b - a for a, b in rows]
    median = statistics.median(deltas)
    current = statistics.median(b for _, b in rows)
    unit = 'ns per render' if units == 1 else f'n={N}, ns per item'
    print(f'Python {sys.version.split()[0]}, {args.scenario}, {unit} (min of 7 repeats), interleaved')
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
