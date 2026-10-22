"""Unpack Robyn's Python sources and stubs into a folder, for ty (`make temp/robyn-typing`).

Robyn ships wheels for CPython 3.10-3.14 only, so the dev venv (3.15) cannot install it; ty
reads the pinned release's ``.py`` / ``.pyi`` files from this folder instead (pyproject.toml,
``[tool.ty.environment] extra-paths``). Any of the release's wheels will do: the Python files
are the same in each, and the native module is left out. Standard library only, any Python.

    python scripts/fetch_robyn_typing.py 0.88.0 temp/robyn-typing
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


def main(version: str, dest: Path) -> int:
    url = f'https://pypi.org/pypi/robyn/{version}/json'
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            release = json.load(r)
        wheels = sorted((f for f in release['urls'] if f['packagetype'] == 'bdist_wheel'), key=lambda f: f['filename'])
        if not wheels:
            print(f'robyn {version} has no wheels on PyPI', file=sys.stderr)
            return 1
        wheel = wheels[0]
        with urllib.request.urlopen(wheel['url'], timeout=120) as r:
            data = r.read()
    except (urllib.error.URLError, OSError) as e:
        print(
            f'Cannot download robyn {version} from PyPI for type checking ({e}). ty needs '
            f'{dest}: run `make {dest}` when online.',
            file=sys.stderr,
        )
        return 1
    if hashlib.sha256(data).hexdigest() != wheel['digests']['sha256']:
        print(f'{wheel["filename"]}: sha256 mismatch', file=sys.stderr)
        return 1
    tmp = dest.with_name(dest.name + '.tmp')
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if name.startswith('robyn/') and name.endswith(('.py', '.pyi', 'py.typed')):
                archive.extract(name, tmp)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    print(f'{dest}: robyn {version} sources from {wheel["filename"]}')
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], Path(sys.argv[2])))
