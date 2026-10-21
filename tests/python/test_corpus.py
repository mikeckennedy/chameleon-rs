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
from typing import Any, Self

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
            reasons[re.sub(r' is not supported yet.*', '', e.args[0], flags=re.DOTALL)] += 1
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


class Anything(str):
    """A stand-in for any application value: a string that has every attribute, can be
    called, indexed and iterated (two items, a few levels deep), compares equal to its kind
    and formats as anything. The same object goes to both engines."""

    depth: int

    def __new__(cls, depth: int = 0) -> Self:
        value = super().__new__(cls, f'any{depth}')
        value.depth = depth
        return value

    def __getattribute__(self, name: str) -> Any:
        if name == 'depth' or name.startswith('__'):
            return object.__getattribute__(self, name)
        return Anything(object.__getattribute__(self, 'depth') + 1)

    def __call__(self, *args: Any, **kwargs: Any) -> Anything:
        return Anything(self.depth + 1)

    def __iter__(self) -> Any:
        return iter(() if self.depth > 3 else (Anything(self.depth + 1), Anything(self.depth + 1)))

    def __len__(self) -> int:
        return 2

    def __getitem__(self, key: Any) -> Anything:  # type: ignore[override]
        return Anything(self.depth + 1)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Anything)

    def __hash__(self) -> int:
        return 1

    def __lt__(self, other: object) -> bool:
        return False

    __gt__ = __le__ = __ge__ = __lt__

    def __int__(self) -> int:
        return 1

    def __index__(self) -> int:
        return 1

    def __float__(self) -> float:
        return 1.0

    def __add__(self, other: object) -> Anything:  # type: ignore[override]
        return Anything(self.depth + 1)

    __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = __truediv__ = __mod__ = __add__  # type: ignore[assignment]

    def __format__(self, spec: str) -> str:
        return 'formatted'

    def __contains__(self, item: object) -> bool:  # type: ignore[override]
        return False


class Nothing(Anything):
    """The stand-in's empty twin: false, empty when iterated, of length 0 (the `else`
    branches and empty loops the always-true stand-in never reaches)."""

    def __new__(cls, depth: int = 0) -> Self:
        value = str.__new__(cls, '')
        value.depth = depth
        return value

    def __getattribute__(self, name: str) -> Any:
        if name == 'depth' or name.startswith('__'):
            return object.__getattribute__(self, name)
        return Nothing(object.__getattribute__(self, 'depth') + 1)

    def __call__(self, *args: Any, **kwargs: Any) -> Nothing:
        return Nothing(self.depth + 1)

    def __getitem__(self, key: Any) -> Nothing:  # type: ignore[override]
        return Nothing(self.depth + 1)

    def __iter__(self) -> Any:
        return iter(())

    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return False


def render_corpus(stand_in: Any) -> tuple[int, int, list[str]]:
    """Each template rendered by both engines with every name it needs bound to `stand_in`
    (found by rendering until no `NameError` is left): (identical renders, same errors,
    hashes of templates that differ)."""
    from test_errors import canon

    rendered = failed = 0
    differences = []
    for path in FILES:
        templates = [chameleon.PageTemplateFile(str(path)), chameleon_rs.PageTemplateFile(str(path))]
        variables: dict[str, Any] = {}
        while True:
            outcomes: list[tuple[Any, ...]] = []
            for template in templates:
                try:
                    outcomes.append(('ok', template(**variables)))
                except Exception as e:  # noqa: BLE001 - comparing exceptions across engines
                    outcomes.append(('raise', type(e).__name__, canon(str(e)), e))
            want, got = outcomes[0][:3], outcomes[1][:3]
            name = outcomes[0][3].args[0] if want[:2] == ('raise', 'NameError') and outcomes[0][3].args else None
            if want == got and isinstance(name, str) and name not in variables:
                variables[name] = stand_in
                continue
            break
        if want != got:
            differences.append(tag(path))
        elif want[0] == 'ok':
            rendered += 1
        else:
            failed += 1
    return rendered, failed, differences


@needs_corpus
def test_corpus_renders_like_chameleon(capsys: pytest.CaptureFixture[str]) -> None:
    """Every template rendered by both engines, as a file template (layouts through `load:`
    macros), twice: names bound to a stand-in that is true and iterates two items, then to
    one that is false and empty. The same output, or the same error and message. A plumbing
    smoke test of macros, slots and everything else on real templates, not the sites' data."""
    for label, stand_in in (('true', Anything()), ('false', Nothing())):
        rendered, failed, differences = render_corpus(stand_in)
        assert not differences, f'{label} stand-in: {len(differences)} templates differ: {differences}'
        with capsys.disabled():
            print(f'\ncorpus renders ({label} stand-in): {rendered} identical, {failed} raise the same error')
