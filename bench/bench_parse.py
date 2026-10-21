"""
Tokenizer and element parser time, Chameleon (`iter_xml` + `ElementParser`) vs chameleon_rs
(the core's tokenizer and parser, no Python objects built), per template, over the upstream
fixtures and the real-template corpus (the directories in CHAMELEON_RS_CORPUS, separated
by ':', set in your own shell or an ignored `.envrc`; skipped when unset). Also the full
compile (`PageTemplateFile(path)`) of each corpus template chameleon_rs supports so far,
and the tree plus every expression of all of them, against Chameleon's full compile (cook).

Build first:  make build-release
Run:          venv/bin/python bench/bench_parse.py
"""

import os
import statistics
import sys
import timeit
from pathlib import Path

import chameleon
import chameleon_rs
from chameleon.parser import ElementParser
from chameleon.tokenize import iter_xml
from chameleon.utils import read_encoded
from chameleon.zpt.program import MacroProgram
from chameleon_rs import exc
from chameleon_rs._native import _testing

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / 'tests' / 'upstream' / 'chameleon_tests' / 'inputs'


def best(f, number: int) -> float:
    return min(timeit.repeat(f, number=number, repeat=5)) / number


def bodies(paths: list[Path]) -> list[str]:
    out = []
    for p in paths:
        try:
            body = read_encoded(p.read_bytes())
            list(ElementParser(iter_xml(body), MacroProgram.DEFAULT_NAMESPACES))
        except Exception:  # noqa: BLE001, S112 - only templates both engines parse are timed
            continue
        out.append(body)
    return out


def report(name: str, docs: list[str]) -> None:
    ns = MacroProgram.DEFAULT_NAMESPACES
    ref = [best(lambda b=b: list(ElementParser(iter_xml(b), ns)), 20) for b in docs]
    ours = [best(lambda b=b: _testing.parse_count(b), 200) for b in docs]
    tok_ref = [best(lambda b=b: list(iter_xml(b)), 20) for b in docs]
    tok_ours = [best(lambda b=b: _testing.token_count(b), 200) for b in docs]
    size = sum(map(len, docs))
    print(f'{name}: {len(docs)} templates, {size / 1024:.0f} KiB')
    for label, a, b in (('tokenize', tok_ref, tok_ours), ('tokenize + parse', ref, ours)):
        print(
            f'  {label:17s} Chameleon median {statistics.median(a) * 1e6:8.1f} us, total {sum(a) * 1e3:7.2f} ms | '
            f'chameleon_rs median {statistics.median(b) * 1e6:7.1f} us, total {sum(b) * 1e3:6.2f} ms '
            f'({sum(a) / sum(b):.1f}x)'
        )


def full_compile(paths: list[Path]) -> None:
    """Each corpus template compiled from scratch as a file template (a new `PageTemplateFile`
    each time, as `bench_compile.py` does; files have the `load:` expression type)."""
    ref, ours, resolved = [], [], []
    for path in paths:
        try:
            chameleon.PageTemplateFile(str(path)).cook_check()
        except Exception:  # noqa: BLE001, S112 - only templates Chameleon compiles are timed
            continue
        ref.append(best(lambda p=path: chameleon.PageTemplateFile(str(p)).cook_check(), 3))
        # Up to and including every expression (also for templates that stop at macros).
        body = read_encoded(path.read_bytes())
        resolved.append(best(lambda b=body: _testing.fast_path_coverage(b, load=True), 20))
        try:
            chameleon_rs.PageTemplateFile(path).cook_check()
        except exc.UnsupportedError:
            continue
        ours.append(best(lambda p=path: chameleon_rs.PageTemplateFile(p).cook_check(), 20))
    print(
        f'  Chameleon full compile (cook), {len(ref)} templates: median {statistics.median(ref) * 1e3:.2f} ms, '
        f'max {max(ref) * 1e3:.2f} ms'
    )
    print(
        f'  chameleon_rs tree + every expression, same templates: median '
        f'{statistics.median(resolved) * 1e3:.3f} ms, max {max(resolved) * 1e3:.3f} ms'
    )
    if ours:
        print(
            f'  chameleon_rs full compile, the {len(ours)} templates it supports: median '
            f'{statistics.median(ours) * 1e3:.3f} ms, max {max(ours) * 1e3:.3f} ms'
        )


def main() -> None:
    report('upstream fixtures', bodies(sorted(FIXTURES.iterdir())))
    dirs = os.environ.get('CHAMELEON_RS_CORPUS', '').split(':')
    corpus = sorted(f for d in dirs if d and Path(d).is_dir() for f in Path(d).rglob('*.pt'))
    if not corpus:
        print('corpus: not found (set CHAMELEON_RS_CORPUS)', file=sys.stderr)
        return
    docs = bodies(corpus)
    report('corpus', docs)
    full_compile(corpus)


if __name__ == '__main__':
    main()
