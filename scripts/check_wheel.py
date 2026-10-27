"""Install a built wheel (or sdist) into a fresh venv and test it (`make check-wheels`, release workflow).

For each artifact: a scan for private material (the corpus's folder names and
``$CHAMELEON_RS_CORPUS``, and outside GitHub Actions the home folder and checkout path, in every
file: metadata, SBOM, extension), then a new venv with the artifact and pytest only (so a
missing runtime dependency fails), a smoke render, Chameleon's upstream suite on chameleon_rs
(`tests/upstream`, `CHAMELEON_ENGINE=rust`) and `tests/python/test_typing.py` (with Flask,
FastAPI and Litestar installed for it from wheels; skipped, with a log line, where they have
none), run against the installed copy (the smoke test fails if the package is imported from the
source tree). An sdist is built into a wheel from scratch first, which proves it is complete.
Standard library only.

    python scripts/check_wheel.py dist/chameleon_rs-0.1.53-cp315-cp315-macosx_11_0_arm64.whl
    python scripts/check_wheel.py --python 3.13 dist/chameleon_rs-0.1.53.tar.gz
    python scripts/check_wheel.py --all dist         # every wheel in dist/ that this machine can run

``--python`` (a path, a command, or a version for ``uv python find``) is needed for an sdist;
for a wheel, the interpreter is taken from its tag (``cp314t`` -> 3.14t). ``--smoke-only``
skips the pytest suites, ``--suites-only`` the smoke test.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SMOKE = r"""
import asyncio, importlib, pkgutil, sys
import chameleon_rs
from chameleon_rs import PageTemplate

assert 'site-packages' in chameleon_rs.__file__, chameleon_rs.__file__
out = PageTemplate('<ul><li tal:repeat="x xs" class="${x}">${x}</li></ul>')(xs=['a', '<b>'])
assert out == '<ul><li class="a">a</li>\n<li class="&lt;b&gt;">&lt;b&gt;</li></ul>', out

async def value():
    return 'awaited'

out = asyncio.run(PageTemplate('<p>${v}</p>').render_async(v=value()))
assert out == '<p>awaited</p>', out
chameleon_rs.configure(markdown=True)
assert '<em>x</em>' in PageTemplate('<div>${markdown: "*x*"}</div>')()

# Standard library only: every module but the framework integrations imports with nothing
# else installed (this venv has the wheel and pytest), and so does the compat alias.
import chameleon_rs.compat
chameleon_rs.compat.install()
import chameleon, chameleon_partials
assert chameleon_partials is chameleon_rs.partials
FRAMEWORKS = {'flask', 'fastapi', 'starlette', 'robyn', 'litestar'}
for info in pkgutil.walk_packages(chameleon_rs.__path__, 'chameleon_rs.'):
    if info.name.split('.')[1] not in FRAMEWORKS:
        importlib.import_module(info.name)
gil = getattr(sys, '_is_gil_enabled', lambda: True)()
print('smoke ok:', chameleon_rs.core_version(), sys.version.split()[0], 'GIL' if gil else 'free-threaded')
"""


def run(cmd: list[str], **kw) -> None:
    print('+', ' '.join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True, **kw)


def resolve_python(spec: str) -> str:
    if Path(spec).is_file():
        return str(Path(spec).resolve())
    if shutil.which('uv'):
        found = subprocess.run(
            ['uv', 'python', 'find', spec], check=False, stdout=subprocess.PIPE, text=True, env=env()
        )
        if found.returncode == 0:
            return found.stdout.strip()
    found = shutil.which(spec) or shutil.which(f'python{spec}')
    if found:
        return found
    sys.exit(f'check_wheel: cannot find a Python for {spec!r}')


def wheel_python(wheel: Path) -> str:
    match = re.search(r'-cp(\d)(\d+)-cp\d+(t?)-', wheel.name)
    if not match:
        sys.exit(f'check_wheel: no CPython tag in {wheel.name}')
    return f'{match[1]}.{match[2]}{match[3]}'


def venv_python(venv: Path) -> Path:
    return venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def env() -> dict[str, str]:
    scrub = {'VIRTUAL_ENV', 'PYTHONPATH', 'CHAMELEON_EAGER', 'CHAMELEON_DEBUG', 'CHAMELEON_CACHE', 'CHAMELEON_RELOAD'}
    return {k: v for k, v in os.environ.items() if k not in scrub}


# Never in an artifact: the private template corpus (its folder names) and, outside CI, the
# builder's home folder or checkout path. CI paths (/home/runner/...) are not private.
PRIVATE_NAMES = (b'talk_python_to_me', b'talk-python-to-me', b'talk_python_training', b'talk-python-training')


def scan(artifact: Path) -> None:
    """Fail on private material in the wheel's metadata, SBOM and extension, or in the sdist."""
    ci = os.environ.get('GITHUB_ACTIONS') == 'true'
    forbidden = list(PRIVATE_NAMES)
    forbidden += [p.encode() for p in os.environ.get('CHAMELEON_RS_CORPUS', '').split(os.pathsep) if p]
    home = str(Path.home()).encode()
    local = [] if ci else [home, str(ROOT).encode()]
    if artifact.suffix == '.whl':
        with zipfile.ZipFile(artifact) as z:
            members = [(i.filename, z.read(i)) for i in z.infolist()]
    else:
        with tarfile.open(artifact) as t:
            members = [(m.name, f.read()) for m in t.getmembers() if m.isfile() and (f := t.extractfile(m))]
    problems = []
    for name, data in members:
        lowered = data.lower()
        problems += [f'{name}: {bad.decode()!r}' for bad in forbidden if bad.lower() in lowered]
        for path in local:
            if path not in data:
                continue
            # maturin's SBOM names the checkout (`path+file://...`): a local wheel's names this
            # machine's. Local wheels are for testing only; the release workflow builds the
            # uploaded ones.
            if '/sboms/' in name and home not in data:
                print(f'check_wheel: note: {name} names the local checkout (never upload local wheels)')
            else:
                problems.append(f'{name}: {path.decode()!r}')
    if problems:
        sys.exit(f'check_wheel: private material in {artifact.name}:\n  ' + '\n  '.join(problems))
    print(f'check_wheel: {artifact.name}: no private material', flush=True)


def check(artifact: Path, python: str, smoke: bool, suites: bool) -> None:
    print(f'== {artifact.name} on {python}', flush=True)
    scan(artifact)
    with tempfile.TemporaryDirectory(prefix='chameleon-rs-check-') as tmp:
        venv = Path(tmp) / 'venv'
        run([python, '-m', 'venv', str(venv)], env=env())
        py = str(venv_python(venv))
        # pip builds an sdist into a wheel in an isolated environment (maturin, the Rust
        # toolchain), from the sdist's files only.
        run([py, '-m', 'pip', 'install', '-q', '--no-cache-dir', str(artifact), 'pytest'], env=env())
        if smoke:
            # Out of the source tree, so `import chameleon_rs` cannot pick up python/.
            run([py, '-c', SMOKE], env=env(), cwd=tmp)
        if not suites:
            return
        common = [py, '-m', 'pytest', '-q', '-p', 'no:cacheprovider']
        run([*common, 'tests/upstream'], env={**env(), 'CHAMELEON_ENGINE': 'rust'}, cwd=ROOT)
        # test_typing imports the framework integrations, hence their frameworks (after the
        # smoke test: the package itself must not need them). Their compiled dependencies as
        # wheels only (pydantic-core needs Rust to build, msgspec a C toolchain): on an
        # interpreter they have no wheel for yet, the test is skipped rather than failing the
        # release. (PyYAML has no 3.15 wheel; it builds without a compiler.) Robyn's tests skip
        # without it.
        binary = ['--only-binary', 'pydantic-core', '--only-binary', 'msgspec']
        frameworks = [py, '-m', 'pip', 'install', '-q', *binary, 'flask', 'fastapi', 'litestar']
        print('+', ' '.join(frameworks), flush=True)
        if subprocess.run(frameworks, check=False, env=env()).returncode:
            print('check_wheel: SKIPPED test_typing: no wheel of a framework dependency here', flush=True)
            return
        run([*common, 'tests/python/test_typing.py'], env=env(), cwd=ROOT)


def runnable(wheel: Path) -> bool:
    """A wheel built for this machine's platform (the venv check would fail on another)."""
    plat = sysconfig.get_platform().replace('-', '_').replace('.', '_')
    if sys.platform == 'darwin':
        return plat.split('_')[-1] in wheel.name and 'macosx' in wheel.name
    if sys.platform == 'win32':
        return plat in wheel.name
    libc = 'manylinux' if platform.libc_ver()[0] == 'glibc' else 'musllinux'
    return libc in wheel.name and os.uname().machine in wheel.name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('artifacts', nargs='+', type=Path)
    parser.add_argument('--python', help='interpreter (required for an sdist)')
    parser.add_argument('--all', action='store_true', help='the arguments are folders: check their wheels')
    stage = parser.add_mutually_exclusive_group()
    stage.add_argument('--smoke-only', action='store_true', help='skip the pytest suites')
    stage.add_argument('--suites-only', action='store_true', help='skip the smoke test')
    args = parser.parse_args()
    artifacts = (
        sorted(w for d in args.artifacts for w in d.glob('*.whl') if runnable(w)) if args.all else args.artifacts
    )
    if not artifacts:
        sys.exit('check_wheel: nothing to check')
    for artifact in artifacts:
        if artifact.suffix == '.whl':
            python = resolve_python(args.python or wheel_python(artifact))
        elif args.python:
            python = resolve_python(args.python)
        else:
            sys.exit('check_wheel: an sdist needs --python')
        check(artifact.resolve(), python, smoke=not args.suites_only, suites=not args.smoke_only)
    print(f'check_wheel: {len(artifacts)} artifact(s) passed')


if __name__ == '__main__':
    main()
