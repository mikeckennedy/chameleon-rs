"""Build profile-guided release wheels (`make wheels`, and the release workflow).

The same steps as `make build-pgo`, for a wheel and for any interpreter. Per interpreter: an
instrumented release wheel is built and installed into a training venv (never into the source
tree, so no instrumented extension is left in ``python/chameleon_rs``), ``bench/pgo_train.py``
runs on it, ``llvm-profdata`` merges the counts, and the release wheel is built with them. The
profile is made per interpreter because PyO3's code differs between Python versions and between
GIL and free-threaded builds (reference counting is inlined), and LLVM silently ignores a
profile of other code.

    python scripts/pgo_wheel.py --python 3.12 --python 3.14t --out dist
    python scripts/pgo_wheel.py --python /opt/python/cp314-cp314t/bin/python \\
        --target x86_64-unknown-linux-musl --fallback-plain -- --compatibility musllinux_1_2

``--python`` takes a path, a command on PATH, or a version that ``uv python find`` resolves.
Arguments after ``--`` go to the final ``maturin build``. ``--target`` is passed to both
builds: the instrumented build and the final one must agree on it, since cargo hashes an
explicit target into every symbol name, and a profile under other names matches nothing.

Two guards fail the build: rustc reporting a profile that no longer matches the code, and more
than ``MAX_MISSING`` functions of chameleon_core/chameleon_py without profile data
(``-pgo-warn-missing-function``; a correct build reports about 10, the inlining leftovers of the
training build, and one whose profile was made for another target about 700).

``--no-pgo`` builds a plain release wheel. ``--fallback-plain`` builds one when the profile
cannot be made (no llvm-tools or profiler runtime for the target, a training dependency without
a wheel), with a warning; the guards still fail. Each wheel's kind is reported, and appended to
``$GITHUB_STEP_SUMMARY`` in GitHub Actions.

Needs: rustup, maturin (next to this interpreter, on PATH, or ``--maturin``), and network
access for the training dependencies, installed from ``bench/pgo-requirements.txt`` with
``--require-hashes --only-binary :all:``. Standard library only.
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
REQUIREMENTS = ROOT / 'bench' / 'pgo-requirements.txt'
STALE = re.compile(r'profile data may be out of date|control flow change detected|hash mismatch')
MISSING = re.compile(r'no profile data available for function')
OURS = ('chameleon_core', 'chameleon_py')
MAX_MISSING = 100
# Dependencies' panic locations name their source files under ~/.cargo and ~/.rustup: keep the
# builder's home folder (and user name) out of the wheel. Not part of the crate hash, so it
# does not change symbol names.
REMAP = f'--remap-path-prefix={Path.home()}=~'
# Build settings a caller's environment must not leak into these builds or the training venv.
SCRUB = ('RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS', 'CARGO_TARGET_DIR', 'VIRTUAL_ENV', 'CHAMELEON_EAGER')


class ProfileUnavailable(Exception):
    """The profile could not be made on this platform (the fallback's trigger)."""


def run(cmd: list[str], env: dict[str, str] | None = None, capture: bool = False, cwd: Path = ROOT) -> str:
    print('+', ' '.join(cmd), flush=True)
    if not capture:
        subprocess.run(cmd, check=True, env=env, cwd=cwd)
        return ''
    proc = subprocess.run(
        cmd, check=False, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
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


def default_maturin() -> str:
    beside = Path(sys.executable).parent / ('maturin.exe' if os.name == 'nt' else 'maturin')
    return str(beside) if beside.exists() else shutil.which('maturin') or 'maturin'


def interpreter_tag(python: str, target: str | None) -> str:
    """cp315t-macosx-11.0-arm64: one working directory per interpreter, platform and target."""
    code = (
        'import sys, sysconfig; '
        "t = 't' if sysconfig.get_config_var('Py_GIL_DISABLED') else ''; "
        "print(f'cp{sys.version_info[0]}{sys.version_info[1]}{t}-' + sysconfig.get_platform())"
    )
    out = subprocess.run([python, '-c', code], check=True, stdout=subprocess.PIPE, text=True)
    tag = out.stdout.strip() + (f'-{target}' if target else '')
    return re.sub(r'[^A-Za-z0-9_.-]', '_', tag)


def venv_python(venv: Path) -> Path:
    return venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def pip_install(py: str, *args: str) -> None:
    if shutil.which('uv'):
        run(['uv', 'pip', 'install', '-q', '--python', py, *args], env=clean_env())
    else:
        run([py, '-m', 'pip', 'install', '-q', *args], env=clean_env())


def llvm_profdata() -> str:
    try:
        run(['rustup', 'component', 'add', 'llvm-tools'])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise ProfileUnavailable(f'llvm-tools: {e}') from e
    sysroot = subprocess.run(['rustc', '--print', 'sysroot'], check=True, stdout=subprocess.PIPE, text=True, cwd=ROOT)
    version = subprocess.run(['rustc', '-vV'], check=True, stdout=subprocess.PIPE, text=True, cwd=ROOT).stdout
    host = re.search(r'^host: (\S+)', version, re.MULTILINE)
    if host is None:
        sys.exit('pgo_wheel: rustc -vV printed no host')
    exe = 'llvm-profdata.exe' if os.name == 'nt' else 'llvm-profdata'
    path = Path(sysroot.stdout.strip()) / 'lib' / 'rustlib' / host.group(1) / 'bin' / exe
    if not path.exists():
        raise ProfileUnavailable(f'no {path}')
    return str(path)


def training_venv(python: str, work: Path) -> str:
    venv = work / 'venv'
    if not venv_python(venv).exists():
        if shutil.which('uv'):
            run(['uv', 'venv', '-q', '--python', python, str(venv)], env=clean_env())
        else:
            run([python, '-m', 'venv', str(venv)], env=clean_env())
    py = str(venv_python(venv))
    try:
        pip_install(py, '--require-hashes', '--only-binary', ':all:', '-r', str(REQUIREMENTS))
    except subprocess.CalledProcessError as e:
        raise ProfileUnavailable(f'training dependencies: {e}') from e
    return py


def target_args(target: str | None) -> list[str]:
    return ['--target', target] if target else []


def profile(python: str, maturin: str, target: str | None) -> Path:
    """Instrumented wheel, training run, merge. Returns the merged profile."""
    work = PGO_ROOT / interpreter_tag(python, target)
    data = work / 'data'
    shutil.rmtree(data, ignore_errors=True)
    shutil.rmtree(work / 'instrumented', ignore_errors=True)
    py = training_venv(python, work)
    profdata = llvm_profdata()
    env = clean_env(
        CARGO_ENCODED_RUSTFLAGS=rustflags(f'-Cprofile-generate={data}'), CARGO_TARGET_DIR=str(work / 'generate')
    )
    try:
        run(
            [maturin, 'build', '--release', '-i', python, '-o', str(work / 'instrumented'), *target_args(target)],
            env=env,
        )
    except subprocess.CalledProcessError as e:
        raise ProfileUnavailable(f'instrumented build (profiler runtime?): {e}') from e
    wheels = sorted((work / 'instrumented').glob('*.whl'))
    if len(wheels) != 1:
        sys.exit(f'pgo_wheel: expected one instrumented wheel, found {wheels}')
    pip_install(py, '--force-reinstall', '--no-deps', str(wheels[0]))
    # From the working directory, so `import chameleon_rs` cannot find the source tree.
    run([py, '-c', "import chameleon_rs; assert 'site-packages' in chameleon_rs.__file__"], cwd=work)
    run([py, str(ROOT / 'bench' / 'pgo_train.py')], env=clean_env(), cwd=work)
    raw = list(data.glob('*.profraw'))
    if not raw:
        sys.exit(f'pgo_wheel: the training run wrote no .profraw files to {data}')
    merged = work / 'merged.profdata'
    run([profdata, 'merge', '-o', str(merged), str(data)])
    return merged


def build(python: str, maturin: str, target: str | None, merged: Path | None, out: Path, extra: list[str]) -> None:
    work = PGO_ROOT / interpreter_tag(python, target)
    flags = [f'-Cprofile-use={merged}', '-Cllvm-args=-pgo-warn-missing-function'] if merged else []
    env = clean_env(
        CARGO_ENCODED_RUSTFLAGS=rustflags(*flags), CARGO_TARGET_DIR=str(work / ('use' if merged else 'plain'))
    )
    cmd = [maturin, 'build', '--release', '-i', python, '-o', str(out), *target_args(target), *extra]
    log = run(cmd, env=env, capture=True)
    if not merged:
        return
    # A profile that does not match the code (stale, or made for other symbol names) would
    # silently lose the optimisation.
    if STALE.search(log):
        sys.exit(f'pgo_wheel: the profile does not match the code for {python}')
    missing = [line for line in log.splitlines() if MISSING.search(line) and any(c in line for c in OURS)]
    print(f'pgo_wheel: {len(missing)} functions of our crates without profile data', flush=True)
    if len(missing) > MAX_MISSING:
        sys.exit(
            f'pgo_wheel: {len(missing)} functions of chameleon_core/chameleon_py have no profile data '
            f'(more than {MAX_MISSING}): the profile does not fit this build (another --target?)'
        )


def report(python: str, kind: str) -> None:
    line = f'{Path(python).name} ({interpreter_tag(python, None)}): {kind}'
    print(f'pgo_wheel: {line}', flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write(f'- {line}\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--python', action='append', required=True, help='interpreter (repeatable)')
    parser.add_argument('--out', type=Path, default=ROOT / 'dist', help='wheel output folder')
    parser.add_argument('--maturin', default=default_maturin())
    parser.add_argument('--target', help='Rust target triple, passed to both builds')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--no-pgo', action='store_true', help='a plain release wheel')
    mode.add_argument('--fallback-plain', action='store_true', help='a plain wheel when no profile can be made')
    parser.add_argument('maturin_args', nargs='*', help='extra `maturin build` arguments, after --')
    args = parser.parse_args()
    if args.target:
        run(['rustup', 'target', 'add', args.target])
    for spec in args.python:
        python = resolve_python(spec)
        merged = None
        kind = 'plain (--no-pgo)'
        if not args.no_pgo:
            try:
                merged = profile(python, args.maturin, args.target)
                kind = 'profile-guided'
            except ProfileUnavailable as e:
                if not args.fallback_plain:
                    sys.exit(f'pgo_wheel: no profile for {python}: {e}')
                print(f'::warning::pgo_wheel: no profile for {python}, building a plain wheel: {e}', flush=True)
                kind = f'plain (PGO unavailable: {e})'
        build(python, args.maturin, args.target, merged, args.out.resolve(), args.maturin_args)
        report(python, kind)


if __name__ == '__main__':
    main()
