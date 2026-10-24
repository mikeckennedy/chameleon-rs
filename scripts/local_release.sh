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
#   scripts/local_release.sh            PYTHONS="3.14 3.14t" scripts/local_release.sh
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
clone=${CLONE:-/tmp/chameleon-rs}
pythons=${PYTHONS:-3.12 3.13 3.14 3.14t 3.15 3.15t}

if [ -n "$(git -C "$repo" status --porcelain)" ]; then
    echo "local_release: uncommitted changes; commit them first (the build is of HEAD)" >&2
    exit 1
fi

rm -rf "$repo/dist"
mkdir "$repo/dist"

if [ -d "$clone/.git" ]; then
    git -C "$clone" fetch -q "$repo" HEAD
    git -C "$clone" checkout -q --detach FETCH_HEAD
    git -C "$clone" clean -qfdx -e /target
else
    git clone -q "$repo" "$clone"
fi
echo "local_release: building $(git -C "$clone" log --oneline -1) in $clone"
cd "$clone"

# macOS: maturin from the hash-pinned requirements, as the release workflow installs it.
tools=target/release-tools
uv venv -q --allow-existing --python 3.13 "$tools"
uv pip install -q --python "$tools" --require-hashes -r scripts/release-requirements.txt
python_args=()
for p in $pythons; do python_args+=(--python "$p"); done
"$tools/bin/python" scripts/pgo_wheel.py "${python_args[@]}" --maturin "$tools/bin/maturin" --out dist
"$tools/bin/maturin" sdist --out dist

# Linux: one container per architecture (x86_64 under emulation on Apple silicon), the clone
# mounted at /build. One at a time: the builds share target/pgo.
for arch in aarch64 x86_64; do
    if [ "$arch" = x86_64 ]; then platform=linux/amd64; else platform=linux/arm64; fi
    docker run --rm --platform "$platform" -v "$clone:/build" -w /build \
        -e PYTHONS="$pythons" "quay.io/pypa/manylinux_2_28_$arch" bash -c \
        "for p in \$PYTHONS; do scripts/linux_wheel.sh \"\$p\" $arch-unknown-linux-gnu manylinux_2_28 true; done"
done

cp dist/* "$repo/dist/"
echo "local_release: $(ls "$repo/dist" | wc -l | tr -d ' ') files in $repo/dist"
ls -1 "$repo/dist"
