"""Build profile-guided release wheels (`make wheels`, and the release workflow).

The same steps as `make build-pgo`, for a wheel instead of an editable install, and for any
interpreter: per interpreter, an instrumented release build is installed into a training venv,
``bench/pgo_train.py`` runs on it, ``llvm-profdata`` merges the counts, and the release wheel is
built with them. The profile is made per interpreter because PyO3's code differs between Python
versions and between GIL and free-threaded builds (reference counting is inlined), and a profile
of other code is silently ignored.

    python scripts/pgo_wheel.py --python 3.12 --python 3.14t --out dist
    python scripts/pgo_wheel.py --python python3.13 --profile-only --profdata-out target/pgo/merged.profdata

``--python`` takes a path, a command on PATH, or a version that ``uv python find`` resolves.
``--profile-only`` stops after merging (the release workflow's Linux jobs: maturin-action then
builds the manylinux / musllinux wheel in the same container with ``-Cprofile-use``). The final
build fails if rustc reports that the profile does not match the code.

Needs: the Rust toolchain (rustup), maturin on PATH (or ``--maturin``), and network access for
the training venv's packages (pydantic, stock Chameleon and chameleon_partials, which the
benchmark modules import; from the pinned clones in temp/ when ``make reference`` made them,
else from PyPI). Standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PGO_ROOT = ROOT / 'target' / 'pgo' / 'wheels'
STALE = re.compile(r'profile data may be out of date|control flow change detected|hash mismatch')
# Build settings a caller's environment must not leak into the training builds or the
# training venv's own installs (the release workflow sets RUSTFLAGS for maturin-action).
# Dependencies' panic locations name their source files under ~/.cargo and ~/.rustup: keep the
# builder's home folder (and user name) out of the wheel. Not part of the crate hash, so the
# profile matches builds without it (the release workflow's Linux builds).
REMAP = f'--remap-path-prefix={Path.home()}=~'
SCRUB = ('RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS', 'CARGO_TARGET_DIR', 'VIRTUAL_ENV', 'CHAMELEON_EAGER')


def run(cmd: list[str], env: dict[str, str] | None = None, capture: bool = False) -> str:
    print('+', ' '.join(cmd), flush=True)
    if not capture:
        subprocess.run(cmd, check=True, env=env, cwd=ROOT)
        return ''
    proc = subprocess.run(
        cmd, check=False, env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    print(proc.stdout, flush=True)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout)
    return proc.stdout


def rustflags(*flags: str) -> str:
    # The encoded form: a path with spaces stays one argument.
    return '\x1f'.join([*flags, REMAP])


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env.update(extra)
    return env


def resolve_python(spec: str) -> str:
    if Path(spec).is_file():
        return str(Path(spec).resolve())
    found = shutil.which(spec)
    if found:
        return found
    if shutil.which('uv'):
        out = subprocess.run(
            ['uv', 'python', 'find', spec], check=True, stdout=subprocess.PIPE, text=True, env=clean_env()
        )
        return out.stdout.strip()
    sys.exit(f'pgo_wheel: cannot find a Python for {spec!r}')


def interpreter_tag(python: str) -> str:
    """cp315t-darwin-arm64: one training directory per interpreter and platform."""
    code = (
        'import sys, sysconfig; '
        "t = 't' if sysconfig.get_config_var('Py_GIL_DISABLED') else ''; "
        "print(f'cp{sys.version_info[0]}{sys.version_info[1]}{t}-' + sysconfig.get_platform())"
    )
    out = subprocess.run([python, '-c', code], check=True, stdout=subprocess.PIPE, text=True)
    return re.sub(r'[^A-Za-z0-9_.-]', '_', out.stdout.strip())


def venv_python(venv: Path) -> Path:
    return venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def llvm_profdata() -> str:
    run(['rustup', 'component', 'add', 'llvm-tools'])
    sysroot = subprocess.run(['rustc', '--print', 'sysroot'], check=True, stdout=subprocess.PIPE, text=True, cwd=ROOT)
    version = subprocess.run(['rustc', '-vV'], check=True, stdout=subprocess.PIPE, text=True, cwd=ROOT).stdout
    host = re.search(r'^host: (\S+)', version, re.MULTILINE)
    if host is None:
        sys.exit('pgo_wheel: rustc -vV printed no host')
    exe = 'llvm-profdata.exe' if os.name == 'nt' else 'llvm-profdata'
    return str(Path(sysroot.stdout.strip()) / 'lib' / 'rustlib' / host.group(1) / 'bin' / exe)


def training_venv(python: str, work: Path) -> Path:
    venv = work / 'venv'
    if not venv_python(venv).exists():
        if shutil.which('uv'):
            run(['uv', 'venv', '-q', '--python', python, str(venv)], env=clean_env())
        else:
            run([python, '-m', 'venv', str(venv)], env=clean_env())
    py = str(venv_python(venv))
    refs = [ROOT / 'temp' / 'chameleon', ROOT / 'temp' / 'chameleon_partials']
    packages = ['pydantic']
    if all(p.is_dir() for p in refs):
        packages += [arg for p in refs for arg in ('-e', str(p))]
    else:
        packages += ['chameleon', 'chameleon_partials']
    if shutil.which('uv'):
        run(['uv', 'pip', 'install', '-q', '--python', py, *packages], env=clean_env())
    else:
        run([py, '-m', 'pip', 'install', '-q', *packages], env=clean_env())
    return venv


def profile(python: str, maturin: str, profdata_out: Path | None) -> tuple[Path, Path]:
    """Instrumented build, training run, merge. Returns (merged profile, training venv)."""
    work = PGO_ROOT / interpreter_tag(python)
    data = work / 'data'
    shutil.rmtree(data, ignore_errors=True)
    venv = training_venv(python, work)
    profdata = llvm_profdata()
    env = clean_env(
        CARGO_ENCODED_RUSTFLAGS=rustflags(f'-Cprofile-generate={data}'),
        CARGO_TARGET_DIR=str(work / 'generate'),
        VIRTUAL_ENV=str(venv),
    )
    # A uv venv has no pip: maturin installs with uv there.
    run([maturin, 'develop', '--release', *(['--uv'] if shutil.which('uv') else [])], env=env)
    run([str(venv_python(venv)), 'bench/pgo_train.py'], env=clean_env())
    merged = profdata_out or work / 'merged.profdata'
    merged.parent.mkdir(parents=True, exist_ok=True)
    run([profdata, 'merge', '-o', str(merged), str(data)])
    return merged, venv


def build(python: str, maturin: str, merged: Path, out: Path, extra: list[str]) -> None:
    work = PGO_ROOT / interpreter_tag(python)
    env = clean_env(CARGO_ENCODED_RUSTFLAGS=rustflags(f'-Cprofile-use={merged}'), CARGO_TARGET_DIR=str(work / 'use'))
    log = run([maturin, 'build', '--release', '-i', python, '-o', str(out), *extra], env=env, capture=True)
    # A profile that no longer matches the code would silently lose the optimisation.
    if STALE.search(log):
        sys.exit(f'pgo_wheel: the profile does not match the code for {python}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--python', action='append', required=True, help='interpreter (repeatable)')
    parser.add_argument('--out', type=Path, default=ROOT / 'dist', help='wheel output folder')
    parser.add_argument('--maturin', default=shutil.which('maturin') or 'maturin')
    parser.add_argument('--profile-only', action='store_true', help='stop after merging the profile')
    parser.add_argument('--profdata-out', type=Path, help='where to write the merged profile')
    parser.add_argument('maturin_args', nargs='*', help='extra `maturin build` arguments, after --')
    args = parser.parse_args()
    if args.profdata_out and len(args.python) > 1:
        parser.error('--profdata-out takes one --python')
    for spec in args.python:
        python = resolve_python(spec)
        merged, _ = profile(python, args.maturin, args.profdata_out.resolve() if args.profdata_out else None)
        if not args.profile_only:
            build(python, args.maturin, merged, args.out.resolve(), args.maturin_args)


if __name__ == '__main__':
    main()
