"""
First request after a reload (docs/vision.md, section 5): in a fresh interpreter, the time to
import the engine, then to load, compile and render the app page of `bench_page.py` once (its
layout macro through `load:`, its partial through the loader), then the steady-state render.
Each figure is the median of several fresh processes, for Chameleon and chameleon_rs.

Build first:  make build-release
Run:          venv/bin/python bench/bench_first.py [--processes 7]
"""

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

CHILD = r"""
import json, sys, time
t0 = time.perf_counter()
engine = sys.argv[1]
if engine == 'chameleon':
    import chameleon.zpt.template as m
else:
    import chameleon_rs as m
t1 = time.perf_counter()
sys.path.insert(0, sys.argv[2])
from bench_page import TEMPLATES, make_model
model = make_model(50)
t2 = time.perf_counter()
template = m.PageTemplateFile(str(TEMPLATES / 'app' / 'episodes_inline.pt'))
template.cook_check()
t3 = time.perf_counter()
template(**model)
t4 = time.perf_counter()
template(**model)
t5 = time.perf_counter()
print(json.dumps({'import': t1 - t0, 'compile': t3 - t2, 'first render': t4 - t3, 'second render': t5 - t4}))
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--processes', type=int, default=7)
    args = parser.parse_args()
    bench = str(Path(__file__).parent)
    print(f'Python {sys.version.split()[0]}, ms, median of {args.processes} fresh processes')
    for engine in ('chameleon', 'chameleon_rs'):
        runs = [
            json.loads(
                subprocess.run(
                    [sys.executable, '-c', CHILD, engine, bench], check=True, capture_output=True, text=True
                ).stdout
            )
            for _ in range(args.processes)
        ]
        cells = {k: statistics.median(r[k] for r in runs) * 1e3 for k in runs[0]}
        first = cells['compile'] + cells['first render']
        print(
            f'  {engine:<13}' + '  '.join(f'{k} {v:7.2f}' for k, v in cells.items()) + f'  first request {first:7.2f}'
        )


if __name__ == '__main__':
    main()
