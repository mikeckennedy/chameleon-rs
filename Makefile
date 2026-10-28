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
# H. Turgut Uyar's Litestar engine for Chameleon (third party, no built-in replacement): its
# suite runs on the compat alias only (src/ layout, put on the path: installing it would pull in
# stock Chameleon). The same pinned clone is what `make dev` installs into venv (one pin).
LITESTAR_CHAMELEON_REPO := https://github.com/uyar/litestar-chameleon
LITESTAR_CHAMELEON_COMMIT := da0652d3b3b32f42af5ae806242779e8df21ba09
# Pyramid's Chameleon bindings (third party, no built-in replacement): like litestar-chameleon,
# on the compat alias only and put on the path, not installed (it depends on stock Chameleon).
# Its tests live in the package (pyramid_chameleon/tests), and it imports pkg_resources.
PYRAMID_CHAMELEON_REPO := https://github.com/Pylons/pyramid_chameleon
PYRAMID_CHAMELEON_COMMIT := e11b8947cd06a5913af2ec8d4aa5ebcd3c774994
# z3c.pt, the page template bridge of Zope and Plone (third party): it subclasses Chameleon's
# expression types, which the other suites don't. Like the two above it is put on the path,
# not installed; its zope.* dependencies are installed. Its suite runs with zope-testrunner,
# the alias installed at startup by tests/integration/child_alias.
Z3C_PT_REPO := https://github.com/zopefoundation/z3c.pt
Z3C_PT_COMMIT := a942d676a9d6af3a39febb7e3ac3934dc4551fb0
INTEGRATION_PYTHON ?= 3.14
INTEGRATION_VENV := $(CURDIR)/temp/venv-integration
INTEGRATION_SUITES := fastapi-chameleon chameleon-flask chameleon-robyn chameleon_partials litestar-chameleon

# Checks out a pinned commit in a clone; a checkout without .git (sources fetched as a
# tarball of that commit, as in a sandboxed worktree) is used as it is.
checkout = if [ -d $(1)/.git ]; then git -C $(1) checkout -q $(2); else echo "$(1): not a git clone, used as is"; fi

# Robyn ships wheels for CPython 3.10-3.14 only (no 3.15, no free-threaded build, no sdist), so
# it is not in the dev group: `make dev` installs it where a wheel exists, and the pinned
# version's sources are unpacked into temp/robyn-typing, where ty finds them (pyproject.toml,
# `extra-paths`). Where venv has no robyn, `test-robyn` runs the robyn tests in a 3.14 venv.
# One pin for all of them (dev, test-robyn, integration, the typing snapshot): bump it
# deliberately, and delete temp/robyn-typing to refresh the snapshot.
ROBYN_VERSION := 0.88.0
ROBYN_VENV := $(CURDIR)/temp/venv-robyn

.PHONY: dev reference build build-release build-profiling build-pgo wheels sdist release-local check-wheels test test-rust test-python \
	test-upstream test-upstream-rust test-robyn integration test-integration lint fmt bench docs docs-serve

dev: reference temp/litestar-chameleon
	uv pip install --python $(PY) --group dev --group docs -e temp/chameleon -e temp/chameleon_partials
	@# litestar-chameleon (test_litestar compares with it; `test-python` fails without it) depends
	@# on stock Chameleon, which venv has (editable, above). From the pinned clone, with
	@# --no-deps; not in the dev group: `maturin develop` installs that group into the
	@# integration venv, which must not have stock Chameleon.
	$(call checkout,temp/litestar-chameleon,$(LITESTAR_CHAMELEON_COMMIT))
	uv pip install -q --python $(PY) --no-deps ./temp/litestar-chameleon
	uv pip install -q --python $(PY) --only-binary robyn robyn==$(ROBYN_VERSION) \
		|| echo "robyn has no wheel for this Python: test-robyn runs its tests on $(INTEGRATION_PYTHON)"
	$(MAKE) temp/robyn-typing build

# Robyn's .py/.pyi files from one of the pinned release's wheels (standard library only, any
# Python: no 3.14 interpreter needed).
temp/robyn-typing:
	$(PY) scripts/fetch_robyn_typing.py $(ROBYN_VERSION) $@

reference: temp/chameleon temp/chameleon_partials
	$(call checkout,temp/chameleon,$(CHAMELEON_COMMIT))
	$(call checkout,temp/chameleon_partials,$(PARTIALS_COMMIT))

temp/chameleon:
	git clone -q $(CHAMELEON_REPO) $@

temp/chameleon_partials:
	git clone -q $(PARTIALS_REPO) $@

integration: reference temp/fastapi-chameleon temp/chameleon-flask temp/chameleon-robyn temp/litestar-chameleon temp/pyramid_chameleon temp/z3c.pt
	$(call checkout,temp/fastapi-chameleon,$(FASTAPI_COMMIT))
	$(call checkout,temp/chameleon-flask,$(FLASK_COMMIT))
	$(call checkout,temp/chameleon-robyn,$(ROBYN_COMMIT))
	$(call checkout,temp/litestar-chameleon,$(LITESTAR_CHAMELEON_COMMIT))
	$(call checkout,temp/pyramid_chameleon,$(PYRAMID_CHAMELEON_COMMIT))
	$(call checkout,temp/z3c.pt,$(Z3C_PT_COMMIT))
	test -x $(INTEGRATION_VENV)/bin/python || uv venv -q $(INTEGRATION_VENV) --python $(INTEGRATION_PYTHON)
	uv pip install -q --python $(INTEGRATION_VENV)/bin/python fastapi httpx 'flask[async]' robyn==$(ROBYN_VERSION) 'litestar>=2.25,<3' pyramid 'setuptools<82' pytest maturin \
		zope.interface zope.component 'zope.i18n>=3.5' zope.traversing zope.contentprovider \
		zope.pagetemplate zope.testing zope.testrunner
	VIRTUAL_ENV=$(INTEGRATION_VENV) PYO3_PYTHON=$(INTEGRATION_VENV)/bin/python CARGO_TARGET_DIR=$(CURDIR)/target/integration \
		$(INTEGRATION_VENV)/bin/maturin develop -q --profile dev-opt

temp/fastapi-chameleon:
	git clone -q $(FASTAPI_REPO) $@

temp/chameleon-flask:
	git clone -q $(FLASK_REPO) $@

temp/chameleon-robyn:
	git clone -q $(ROBYN_REPO) $@

temp/litestar-chameleon:
	git clone -q $(LITESTAR_CHAMELEON_REPO) $@

temp/pyramid_chameleon:
	git clone -q $(PYRAMID_CHAMELEON_REPO) $@

temp/z3c.pt:
	git clone -q $(Z3C_PT_REPO) $@

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

# Release artifacts (Phase 12b) into dist/: a profile-guided wheel per interpreter
# (scripts/pgo_wheel.py, the steps of build-pgo with a training venv per interpreter, found by
# uv), and the sdist. The release workflow (.github/workflows/release.yml) runs the same script.
WHEEL_PYTHONS ?= 3.12 3.13 3.14 3.14t 3.15 3.15t

wheels: reference
	$(PY) scripts/pgo_wheel.py $(foreach p,$(WHEEL_PYTHONS),--python $(p)) --maturin venv/bin/maturin --out dist

sdist:
	$(MATURIN) sdist --out dist

# A local release (no CI): dist/ emptied, then macOS and manylinux wheels and the sdist built
# from a clone of HEAD at /tmp/chameleon-rs (scripts/local_release.sh), ready for `uv publish`.
release-local:
	scripts/local_release.sh

# Each wheel in dist/ this machine can run, in a fresh venv with nothing else installed: a
# smoke render, the upstream suite on chameleon_rs and test_typing (scripts/check_wheel.py).
check-wheels:
	$(PY) scripts/check_wheel.py --all dist

test: test-rust test-python test-upstream test-upstream-rust test-robyn

test-rust:
	cargo test --workspace

# The litestar-chameleon comparisons skip where it is not installed (a scratch venv), but
# not here: a venv rebuilt without `make dev` would otherwise skip them silently.
test-python: build
	CHAMELEON_RS_REQUIRE_LITESTAR_CHAMELEON=1 $(PYTEST)

test-upstream:
	CHAMELEON_ENGINE=python $(PYTEST) tests/upstream

test-upstream-rust: build
	CHAMELEON_ENGINE=rust $(PYTEST) tests/upstream

# chameleon_rs.robyn's tests where venv cannot install Robyn (Python 3.15, free-threaded): in a
# $(INTEGRATION_PYTHON) venv with the dev tools, stock Chameleon (the differential tests) and
# Robyn. Where venv has Robyn, test-python already ran them. The extension is built into that
# venv (target/robyn) only when crates/, Cargo.* or pyproject.toml changed since the last build
# there (the Python sources are an editable install): the first run costs a full build.
ROBYN_TESTS := tests/python/test_robyn.py tests/python/test_typing.py tests/python/test_api.py::test_no_runtime_dependencies

test-robyn: reference
	@if $(PY) -c 'import robyn' 2>/dev/null; then echo "test-robyn: robyn is in venv, test-python ran its tests"; else \
		set -e; \
		test -x $(ROBYN_VENV)/bin/python || uv venv -q $(ROBYN_VENV) --python $(INTEGRATION_PYTHON); \
		uv pip install -q --python $(ROBYN_VENV)/bin/python --group dev -e temp/chameleon -e temp/chameleon_partials \
			robyn==$(ROBYN_VERSION); \
		stamp=$(ROBYN_VENV)/.chameleon-rs-built; \
		if [ ! -f $$stamp ] || [ -n "$$(find crates Cargo.toml Cargo.lock pyproject.toml -newer $$stamp -print -quit)" ]; then \
			VIRTUAL_ENV=$(ROBYN_VENV) PYO3_PYTHON=$(ROBYN_VENV)/bin/python CARGO_TARGET_DIR=$(CURDIR)/target/robyn \
				$(ROBYN_VENV)/bin/maturin develop -q --profile dev-opt; \
			touch $$stamp; \
		else echo "test-robyn: extension in $(ROBYN_VENV) is up to date"; fi; \
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
		PYTHONPATH=$(CURDIR)/tests/integration:$(CURDIR)/temp/$$suite:$(CURDIR)/temp/$$suite/src:$(CURDIR)/temp/chameleon_partials \
			$(INTEGRATION_VENV)/bin/python -m pytest -q -c pyproject.toml --rootdir . \
			-p chameleon_compat_plugin -p no:cacheprovider tests; \
	done
	@echo "== pyramid_chameleon (original)"
	cd $(CURDIR)/temp/pyramid_chameleon && INTEGRATION_MODE=original \
		PYTHONPATH=$(CURDIR)/tests/integration:$(CURDIR)/temp/pyramid_chameleon \
		$(INTEGRATION_VENV)/bin/python -m pytest -q -c setup.cfg --rootdir . \
		-p chameleon_compat_plugin -p no:cacheprovider pyramid_chameleon/tests
	@echo "== z3c.pt (original)"
	cd $(CURDIR)/temp/z3c.pt && INTEGRATION_MODE=original \
		PYTHONPATH=$(CURDIR)/tests/integration/child_alias:$(CURDIR)/temp/z3c.pt/src \
		$(INTEGRATION_VENV)/bin/zope-testrunner --test-path=src
	set -e; for suite in $(BUILTIN_SUITES); do \
		echo "== $$suite (built-in)"; \
		cd $(CURDIR)/temp/$$suite; \
		INTEGRATION_MODE=builtin PYTHONPATH=$(CURDIR)/tests/integration:$(CURDIR)/temp/$$suite \
			$(INTEGRATION_VENV)/bin/python -m pytest -q -c pyproject.toml --rootdir . \
			-p chameleon_compat_plugin -p no:cacheprovider tests; \
	done

lint: temp/robyn-typing
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
