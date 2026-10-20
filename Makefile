# Developer commands for chameleon-rs. Everything runs against ./venv (Python 3.15).
PY := venv/bin/python
PYTEST := $(PY) -m pytest -q

# Reference implementations, cloned into temp/ (gitignored) at pinned commits so every
# comparison is against a known version. Bump deliberately and re-run the baselines.
CHAMELEON_REPO := https://github.com/malthe/chameleon
CHAMELEON_COMMIT := 90b900244abd9ce39c9a9ca70b38ed559ae2b969
PARTIALS_REPO := https://github.com/mikeckennedy/chameleon_partials
PARTIALS_COMMIT := f54ca1a103a3c034eaabc39b1bcb50210a8ad4d9

.PHONY: dev reference build build-release build-profiling test test-rust test-python \
	test-upstream test-upstream-rust lint fmt bench

dev: reference
	uv pip install --python $(PY) --group dev -e temp/chameleon -e temp/chameleon_partials
	$(MAKE) build

reference: temp/chameleon temp/chameleon_partials
	git -C temp/chameleon checkout -q $(CHAMELEON_COMMIT)
	git -C temp/chameleon_partials checkout -q $(PARTIALS_COMMIT)

temp/chameleon:
	git clone -q $(CHAMELEON_REPO) $@

temp/chameleon_partials:
	git clone -q $(PARTIALS_REPO) $@

# Fast optimized build for the test loop; release (fat LTO) for benchmarks and wheels.
build:
	venv/bin/maturin develop --profile dev-opt

build-release:
	venv/bin/maturin develop --release

build-profiling:
	venv/bin/maturin develop --profile profiling

test: test-rust test-python test-upstream

test-rust:
	cargo test --workspace

test-python: build
	$(PYTEST)

test-upstream:
	CHAMELEON_ENGINE=python $(PYTEST) tests/upstream

test-upstream-rust: build
	CHAMELEON_ENGINE=rust $(PYTEST) tests/upstream

lint:
	cargo fmt --all -- --check
	cargo clippy --workspace --all-targets --all-features -- -D warnings
	cargo deny check -A license-not-encountered
	ruff check python tests/python tests/upstream/conftest.py bench
	ruff format --check python tests/python tests/upstream/conftest.py

fmt:
	cargo fmt --all
	ruff format python tests/python tests/upstream/conftest.py

bench: build-release
	$(PY) bench/bench_render.py
	$(PY) bench/bench_split.py
	$(PY) bench/bench_compile.py
