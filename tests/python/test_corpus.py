"""Every real template builds the same program tree as Chameleon's, and compiles or stops with
UnsupportedError: never a panic, a crash or a wrong tree.

The templates are private and read in place (never copied here); failures report path hashes,
never names or content. Directories come from CHAMELEON_RS_CORPUS (separated by ':'), which
each developer sets in their own shell or an ignored `.envrc`; the tests skip when it is unset
or names no existing directory.
"""

from __future__ import annotations

import collections
import hashlib
import os
import re
from pathlib import Path

import chameleon
import chameleon_rs
import pytest
from chameleon_rs import exc
from test_builder import chameleon_inventory, fixture_options, our_inventory


def corpus_files() -> list[Path]:
    dirs = os.environ.get('CHAMELEON_RS_CORPUS', '').split(':')
    return sorted(f for d in dirs if d and Path(d).is_dir() for f in Path(d).rglob('*.pt'))


FILES = corpus_files()
needs_corpus = pytest.mark.skipif(not FILES, reason='no template corpus found (set CHAMELEON_RS_CORPUS)')


def tag(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:12]


def chameleon_accepts(path: Path) -> bool:
    try:
        chameleon.PageTemplate(path.read_bytes())
    except Exception:  # noqa: BLE001 - only templates Chameleon accepts are compared
        return False
    return True


@needs_corpus
def test_corpus_trees_match_chameleon() -> None:
    """Every template Chameleon compiles builds a tree with the same expressions and names,
    at the same positions."""
    differences = []
    built = 0
    for path in FILES:
        if not chameleon_accepts(path):
            continue
        body, options = fixture_options(path)
        if our_inventory(body, **options) != chameleon_inventory(body, **options):
            differences.append(tag(path))
        built += 1
    assert built > 0
    assert not differences, f'{len(differences)} trees differ: {differences}'


@needs_corpus
def test_corpus_compiles_or_is_unsupported(capsys: pytest.CaptureFixture[str]) -> None:
    compiled = 0
    reasons: collections.Counter[str] = collections.Counter()
    for path in FILES:
        try:
            # A file template, as the sites use them (it has the `load:` expression type).
            chameleon_rs.PageTemplateFile(path).cook_check()
        except exc.UnsupportedError as e:
            # The feature named in the message, never template content.
            reasons[re.sub(r' is not supported yet.*', '', e.msg, flags=re.DOTALL)] += 1
            continue
        compiled += 1
    assert compiled > 0
    with capsys.disabled():
        print(f'\ncorpus: {len(FILES)} templates, {compiled} compile fully, {len(FILES) - compiled} unsupported')
        for reason, n in reasons.most_common():
            print(f'  {n:4} {reason}')


@needs_corpus
def test_corpus_expressions_compile_and_fast_path_coverage(capsys: pytest.CaptureFixture[str]) -> None:
    """Every expression of every template compiles (the expression types the sites use are
    all supported), and most run entirely on the native fast path (D4)."""
    from chameleon_rs._native import _testing

    fast = fallback = 0
    for path in FILES:
        body, options = fixture_options(path)
        f, s = _testing.fast_path_coverage(body, load=True, **options)
        fast += f
        fallback += s
    assert fast > 0
    with capsys.disabled():
        print(f'\ncorpus expressions: {fast + fallback}, fast path {fast}, with fallback code {fallback}')
