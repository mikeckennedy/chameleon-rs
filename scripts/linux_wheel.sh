#!/bin/bash
# Builds one Linux wheel inside a PyPA manylinux / musllinux image, from the repository root
# (the release workflow runs it with `docker run` on a runner of the image's architecture):
# rustup with the toolchain pinned in rust-toolchain.toml, maturin from the hash-pinned
# scripts/release-requirements.txt, then scripts/pgo_wheel.py for the image's interpreter.
#
#   scripts/linux_wheel.sh <python: 3.14t> <rust target> <compatibility: manylinux_2_28> <pgo: true|false>
set -euo pipefail

version=$1
target=$2
compatibility=$3
pgo=$4

# The image's own interpreter, by its ABI directory (3.14t -> /opt/python/cp314-cp314t).
abi=$(echo "$version" | sed -E 's/^3\.([0-9]+)(t?)$/cp3\1-cp3\1\2/')
python="/opt/python/$abi/bin/python"
test -x "$python" || { echo "linux_wheel: no $python in this image" >&2; exit 1; }

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
export PATH="$HOME/.cargo/bin:$PATH"
rustup toolchain install   # the one rust-toolchain.toml names

tools=/tmp/release-tools
/opt/python/cp313-cp313/bin/python -m venv "$tools"
"$tools/bin/python" -m pip install -q --require-hashes -r scripts/release-requirements.txt

if [ "$pgo" = true ]; then mode=--fallback-plain; else mode=--no-pgo; fi
"$tools/bin/python" scripts/pgo_wheel.py --python "$python" --target "$target" \
    --maturin "$tools/bin/maturin" "$mode" --out dist -- --compatibility "$compatibility"
