"""Every real template compiles, or stops with UnsupportedError: never a panic or a crash.

The templates are private and read in place (never copied here). Directories come from
CHAMELEON_RS_CORPUS (separated by ':'), defaulting to the two Talk Python checkouts; the test
skips when none exist.
"""

import os
from pathlib import Path

import chameleon_rs
import pytest
from chameleon_rs import exc

DEFAULT_CORPUS = (
    '/Volumes/owc-envoy-ultra/github/talk-python/talk-python-to-me/src/talk_python_to_me_com/talk_python_to_me_com/templates',
    '/Volumes/owc-envoy-ultra/github/talk-python/talk-python-training/src/talk-python-training/talk_python_training/templates',
)


def corpus_files() -> list[Path]:
    dirs = os.environ.get('CHAMELEON_RS_CORPUS', ':'.join(DEFAULT_CORPUS)).split(':')
    return sorted(f for d in dirs if d and Path(d).is_dir() for f in Path(d).rglob('*.pt'))


FILES = corpus_files()


@pytest.mark.skipif(not FILES, reason='no template corpus found (set CHAMELEON_RS_CORPUS)')
def test_corpus_compiles_or_is_unsupported() -> None:
    compiled = 0
    for path in FILES:
        try:
            chameleon_rs.PageTemplate(path.read_text(), filename=str(path))
        except exc.UnsupportedError:
            continue
        compiled += 1
    assert compiled > 0
