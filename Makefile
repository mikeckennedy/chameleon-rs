# Developer commands for chameleon-rs. Everything runs against ./venv (Python 3.15).
PY := venv/bin/python
# maturin develop finds the venv through VIRTUAL_ENV; CI never activates it.
MATURIN := VIRTUAL_ENV=$(CURDIR)/venv venv/bin/maturin
PYTEST := $(PY) -m pytest -q

# Reference implementations, cloned into temp/ (gitignored) at pinned commits so every
# comparison is against a known version. Bump deliberately and re-run the baselines.
CHAMELEON_REPO := https://github.com/malthe/chameleon
CHAMELEON_COMMIT := 90b900244abd9ce39c9a9ca70b38ed559ae2b969
PARTIALS_REPO := https://github.com/mikeckennedy/chameleon_partials
PARTIALS_COMMIT := f54ca1a103a3c034eaabc39b1bcb50210a8ad4d9

# Integration suites (level 4): projects written for Chameleon, run on chameleon-rs through
# the compat alias (tests/integration/chameleon_compat_plugin.py) in a scratch venv that has
# their dependencies but not stock Chameleon. Python 3.14: robyn has no 3.15 wheels yet.
FASTAPI_REPO := https://github.com/mikeckennedy/fastapi-chameleon
FASTAPI_COMMIT := 4d070f27a09f0e6fdb6b55c82d490d041dbf5112
FLASK_REPO := https://github.com/mikeckennedy/chameleon-flask
FLASK_COMMIT := 326644f1d55b51c3681104ab10c571c8ca8f8a3a
ROBYN_REPO := https://github.com/mikeckennedy/chameleon-robyn
ROBYN_COMMIT := ab4f6e83f50cf1b6d808645b34ff966a4483c048
INTEGRATION_PYTHON ?= 3.14
INTEGRATION_VENV := $(CURDIR)/temp/venv-integration
INTEGRATION_SUITES := fastapi-chameleon chameleon-flask chameleon-robyn chameleon_partials

# Checks out a pinned commit in a clone; a checkout without .git (sources fetched as a
# tarball of that commit, as in a sandboxed worktree) is used as it is.
checkout = if [ -d $(1)/.git ]; then git -C $(1) checkout -q $(2); else echo "$(1): not a git clone, used as is"; fi

# Robyn ships wheels for CPython 3.10-3.14 only (no 3.15, no free-threaded build, no sdist), so
# it is not in the dev group: `make dev` installs it where a wheel exists, and always unpacks the
# pinned version's sources into temp/robyn-typing, where ty finds them (pyproject.toml,
# `extra-paths`). Where venv has no robyn, `test-robyn` runs the robyn tests in a 3.14 venv.
ROBYN_TYPING_VERSION := 0.88.0
ROBYN_VENV := $(CURDIR)/temp/venv-robyn

.PHONY: dev reference build build-release build-profiling build-pgo test test-rust test-python \
	test-upstream test-upstream-rust test-robyn integration test-integration lint fmt bench docs docs-serve

dev: reference
	uv pip install --python $(PY) --group dev --group docs -e temp/chameleon -e temp/chameleon_partials
	uv pip install -q --python $(PY) --only-binary robyn robyn \
		|| echo "robyn has no wheel for this Python: test-robyn runs its tests on $(INTEGRATION_PYTHON)"
	@# With a GIL $(INTEGRATION_PYTHON), as venv may be free-threaded (`uv pip` does not download one).
	uv python find -q $(INTEGRATION_PYTHON) > /dev/null 2>&1 || uv python install -q $(INTEGRATION_PYTHON)
	uv pip install -q --python $(INTEGRATION_PYTHON) --target temp/robyn-typing --no-deps robyn==$(ROBYN_TYPING_VERSION)
	$(MAKE) build

reference: temp/chameleon temp/chameleon_partials
	$(call checkout,temp/chameleon,$(CHAMELEON_COMMIT))
	$(call checkout,temp/chameleon_partials,$(PARTIALS_COMMIT))

temp/chameleon:
	git clone -q $(CHAMELEON_REPO) $@

temp/chameleon_partials:
	git clone -q $(PARTIALS_REPO) $@

integration: reference temp/fastapi-chameleon temp/chameleon-flask temp/chameleon-robyn
	$(call checkout,temp/fastapi-chameleon,$(FASTAPI_COMMIT))
	$(call checkout,temp/chameleon-flask,$(FLASK_COMMIT))
	$(call checkout,temp/chameleon-robyn,$(ROBYN_COMMIT))
	test -x $(INTEGRATION_VENV)/bin/python || uv venv -q $(INTEGRATION_VENV) --python $(INTEGRATION_PYTHON)
	uv pip install -q --python $(INTEGRATION_VENV)/bin/python fastapi httpx 'flask[async]' robyn pytest maturin
	VIRTUAL_ENV=$(INTEGRATION_VENV) PYO3_PYTHON=$(INTEGRATION_VENV)/bin/python CARGO_TARGET_DIR=$(CURDIR)/target/integration \
		$(INTEGRATION_VENV)/bin/maturin develop -q --profile dev-opt

temp/fastapi-chameleon:
	git clone -q $(FASTAPI_REPO) $@

temp/chameleon-flask:
	git clone -q $(FLASK_REPO) $@

temp/chameleon-robyn:
	git clone -q $(ROBYN_REPO) $@

# Fast optimized build for the test loop; release (fat LTO) for benchmarks and wheels.
build:
	$(MATURIN) develop --profile dev-opt

build-release:
	$(MATURIN) develop --release

build-profiling:
	$(MATURIN) develop --profile profiling

# Profile-guided release build (Phase 10): an instrumented build runs the deterministic
# training workload (bench/pgo_train.py), llvm-profdata merges the counts, and the release
# build uses them. Separate target directories keep the plain release build's cache intact.
# RUSTFLAGS here replaces any rustflags from .cargo/config.toml (none are set today): add them
# to these lines if some are ever configured.
PGO_DIR := $(CURDIR)/target/pgo
LLVM_PROFDATA = $(shell rustc --print sysroot)/lib/rustlib/$(shell rustc -vV | sed -n 's/^host: //p')/bin/llvm-profdata

build-pgo:
	rustup component add llvm-tools
	rm -rf $(PGO_DIR)/data
	RUSTFLAGS="-Cprofile-generate=$(PGO_DIR)/data" CARGO_TARGET_DIR=$(PGO_DIR)/generate $(MATURIN) develop --release
	$(PY) bench/pgo_train.py
	$(LLVM_PROFDATA) merge -o $(PGO_DIR)/merged.profdata $(PGO_DIR)/data
	RUSTFLAGS="-Cprofile-use=$(PGO_DIR)/merged.profdata" CARGO_TARGET_DIR=$(PGO_DIR)/use \
		$(MATURIN) develop --release > $(PGO_DIR)/use.log 2>&1 || (cat $(PGO_DIR)/use.log; exit 1)
	cat $(PGO_DIR)/use.log
	@# A profile that no longer matches the code would silently lose the optimisation.
	! grep -E "profile data may be out of date|control flow change detected|hash mismatch" $(PGO_DIR)/use.log

test: test-rust test-python test-upstream test-upstream-rust test-robyn

test-rust:
	cargo test --workspace

test-python: build
	$(PYTEST)

test-upstream:
	CHAMELEON_ENGINE=python $(PYTEST) tests/upstream

test-upstream-rust: build
	CHAMELEON_ENGINE=rust $(PYTEST) tests/upstream

# chameleon_rs.robyn's tests where venv cannot install Robyn (Python 3.15, free-threaded): in a
# $(INTEGRATION_PYTHON) venv with the dev tools, stock Chameleon (the differential tests) and
# Robyn. Where venv has Robyn, test-python already ran them.
ROBYN_TESTS := tests/python/test_robyn.py tests/python/test_typing.py tests/python/test_api.py::test_no_runtime_dependencies

test-robyn: reference
	@if $(PY) -c 'import robyn' 2>/dev/null; then echo "test-robyn: robyn is in venv, test-python ran its tests"; else \
		set -e; \
		test -x $(ROBYN_VENV)/bin/python || uv venv -q $(ROBYN_VENV) --python $(INTEGRATION_PYTHON); \
		uv pip install -q --python $(ROBYN_VENV)/bin/python --group dev -e temp/chameleon -e temp/chameleon_partials robyn; \
		VIRTUAL_ENV=$(ROBYN_VENV) PYO3_PYTHON=$(ROBYN_VENV)/bin/python CARGO_TARGET_DIR=$(CURDIR)/target/robyn \
			$(ROBYN_VENV)/bin/maturin develop -q --profile dev-opt; \
		$(ROBYN_VENV)/bin/python -m pytest -q $(ROBYN_TESTS); \
	fi

# Each suite runs from its own checkout (its own rootdir and conftest), unmodified: first the
# original packages on the compat alias, then (BUILTIN_SUITES) the same suites against
# chameleon-rs's built-in chameleon_partials, chameleon_flask, fastapi_chameleon and
# chameleon_robyn (compat.install()'s mapping).
BUILTIN_SUITES := fastapi-chameleon chameleon-flask chameleon-robyn chameleon_partials

test-integration: integration
	set -e; for suite in $(INTEGRATION_SUITES); do \
		echo "== $$suite (original)"; \
		cd $(CURDIR)/temp/$$suite; \
		INTEGRATION_MODE=original \
		PYTHONPATH=$(CURDIR)/tests/integration:$(CURDIR)/temp/$$suite:$(CURDIR)/temp/chameleon_partials \
			$(INTEGRATION_VENV)/bin/python -m pytest -q -c pyproject.toml --rootdir . \
			-p chameleon_compat_plugin -p no:cacheprovider tests; \
	done
	set -e; for suite in $(BUILTIN_SUITES); do \
		echo "== $$suite (built-in)"; \
		cd $(CURDIR)/temp/$$suite; \
		INTEGRATION_MODE=builtin PYTHONPATH=$(CURDIR)/tests/integration:$(CURDIR)/temp/$$suite \
			$(INTEGRATION_VENV)/bin/python -m pytest -q -c pyproject.toml --rootdir . \
			-p chameleon_compat_plugin -p no:cacheprovider tests; \
	done

lint:
	cargo fmt --all -- --check
	cargo clippy --workspace --all-targets --all-features -- -D warnings
	cargo deny check -A license-not-encountered
	ruff check python tests/python tests/upstream/conftest.py tests/integration bench scripts
	ruff format --check python tests/python tests/upstream/conftest.py tests/integration scripts
	ty check

fmt:
	cargo fmt --all
	ruff format python tests/python tests/upstream/conftest.py tests/integration scripts

bench: build-release
	$(PY) bench/bench_render.py
	$(PY) bench/bench_split.py
	$(PY) bench/bench_compile.py
	$(PY) bench/bench_skeleton.py

# The docs site (docs/docs-site-plan.md): Great Docs imports chameleon_rs, so build first.
# Needs Quarto on PATH. The site is mirrored into site/ (committed), served at
# https://mkennedy.codes/docs/chameleon-rs/.
docs: build
	$(PY) scripts/build_docs.py

docs-serve:
	$(PY) scripts/serve_docs.py
