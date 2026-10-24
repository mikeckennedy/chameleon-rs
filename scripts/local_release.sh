#!/bin/bash
# Builds the release artifacts locally (`make release-local`) into dist/, which it empties
# first: profile-guided wheels for macOS (this machine's architecture) and manylinux_2_28
# x86_64 and aarch64 (in the PyPA images, scripts/linux_wheel.sh), per interpreter, and the
# sdist. Then `uv publish` uploads dist/.
#
# The build runs in a clone of HEAD at a neutral path (CLONE, /tmp/chameleon-rs), not in this
# checkout: maturin writes the checkout's path into every wheel's SBOM. The clone's target/ is
# kept between runs. Uncommitted changes are not built, so the tree must be clean.
#
# Each wheel is its own job (a pgo_wheel.py process, or a container per Linux wheel), JOBS at
# a time: a wheel's build ends in a single-threaded fat-LTO link and its training is single-
# threaded, so one at a time leaves most cores idle. Jobs share nothing but the clone: each
# builds in its own target/pgo/wheels/<interpreter tag>. Logs: target/release-logs/<job>.log.
#
#   scripts/local_release.sh      PYTHONS="3.14 3.14t" JOBS=4 OUT=/elsewhere scripts/local_release.sh
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
clone=${CLONE:-/tmp/chameleon-rs}
out=${OUT:-$repo/dist}
pythons=${PYTHONS:-3.12 3.13 3.14 3.14t 3.15 3.15t}
jobs=${JOBS:-8}

if [ -n "$(git -C "$repo" status --porcelain)" ]; then
    echo "local_release: uncommitted changes; commit them first (the build is of HEAD)" >&2
    exit 1
fi

rm -rf "$out"
mkdir -p "$out"

if [ -d "$clone/.git" ]; then
    git -C "$clone" fetch -q "$repo" HEAD
    git -C "$clone" checkout -q --detach FETCH_HEAD
    git -C "$clone" clean -qfdx -e /target
else
    git clone -q "$repo" "$clone"
fi
echo "local_release: building $(git -C "$clone" log --oneline -1) in $clone, $jobs jobs at a time"
cd "$clone"
logs=target/release-logs
rm -rf "$logs"
mkdir -p "$logs" dist

# maturin from the hash-pinned requirements, as the release workflow installs it.
tools=target/release-tools
uv venv -q --allow-existing --python 3.13 "$tools"
uv pip install -q --python "$tools" --require-hashes -r scripts/release-requirements.txt
"$tools/bin/maturin" sdist --out dist > "$logs/sdist.log" 2>&1

# One job: `macos <python>` or `<linux arch> <python>`, its output in its log.
build_one() {
    local kind=$1 python=$2 log="$logs/$1-$2.log"
    if [ "$kind" = macos ]; then
        "$tools/bin/python" scripts/pgo_wheel.py --python "$python" --maturin "$tools/bin/maturin" \
            --out dist > "$log" 2>&1
    else
        local platform=linux/arm64
        if [ "$kind" = x86_64 ]; then platform=linux/amd64; fi
        docker run --rm --platform "$platform" -v "$clone:/build" -w /build \
            "quay.io/pypa/manylinux_2_28_$kind" \
            scripts/linux_wheel.sh "$python" "$kind-unknown-linux-gnu" manylinux_2_28 true > "$log" 2>&1
    fi
    local status=$?
    if [ $status -eq 0 ]; then echo "local_release: done   $kind $python"; else echo "local_release: FAILED $kind $python ($log)"; fi
    return $status
}
export -f build_one
export logs tools clone

# x86_64 first: emulated, its builds are the longest.
for kind in x86_64 aarch64 macos; do
    for p in $pythons; do echo "$kind $p"; done
done | xargs -P "$jobs" -L 1 bash -c 'build_one "$@"' _ || {
    echo "local_release: a build failed; dist/ is left empty" >&2
    exit 1
}

cp dist/* "$out/"
echo "local_release: $(ls "$out" | wc -l | tr -d ' ') files in $out"
ls -1 "$out"
