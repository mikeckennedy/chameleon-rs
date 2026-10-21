# chameleon-rs

A Rust re-implementation of Python's Chameleon page template engine, shipped as the Python
package `chameleon-rs` (import `chameleon_rs`). Goal: drop-in compatible with Chameleon
(95%+ of its test suite, byte-identical output on real sites) and much faster.

- Decisions: `docs/architecture.md`. Build plan and current phase: `docs/plan.md`. Speculative
  ideas not yet designed (partials settings, Flask/FastAPI integrations, async extras, memory saver, post-release
  optimisation, docs, logo): `docs/vision.md`.
- Reference implementation: `temp/chameleon` (gitignored clone of malthe/chameleon at
  `90b9002`, installed editable in `venv`). When behavior is unclear, read it and run it.
- Chameleon's test suite is vendored unmodified in `tests/upstream/chameleon_tests`. Never
  edit those files; adapt the harness (`tests/upstream/conftest.py`) instead.

## Layout

- `crates/chameleon-core`: pure Rust, no Python. Tokenizer, parser, program builder,
  compiler, VM (generic over a `Runtime` trait), escaping, output buffer.
- `crates/chameleon-py`: the PyO3 extension `chameleon_rs._native`; the only crate that
  touches Python.
- `python/chameleon_rs`: public API classes, compat modules, exception formatting.
- `bench/`: Chameleon baseline benchmarks; re-run against both engines when rendering changes.

## Commands

- `make dev`: clone the reference Chameleon and chameleon_partials into `temp/` at the pinned
  commits, install them and the dev tools into `venv`, then build the extension.
- `make build`: `maturin develop` with the quick `dev-opt` profile (use `build-release` for
  benchmarks, `build-profiling` for py-spy / Instruments).
- `make lint`: rustfmt, clippy pedantic with `-D warnings`, cargo-deny, ruff.
- `make test`: cargo tests, our pytest suite, and the upstream suite on both engines.
- `make test-upstream-rust`: the upstream suite against chameleon_rs (the compatibility score).
- `make test-integration`: fastapi-chameleon, chameleon-flask, chameleon-robyn and
  chameleon_partials, unmodified, on chameleon_rs via `compat.install()` (scratch 3.14 venv).
- `make bench`: baseline benchmarks.

## Conventions

- Rust edition 2024, `unsafe_code = "forbid"`, no `unwrap()` outside tests, `thiserror` for
  error types. Visibility `pub(crate)` unless it is real API.
- Files that port Chameleon logic start with a comment naming the source file and noting it
  was modified (license requirement).
- Positions shown to Python users are code-point offsets, not byte offsets.
- Default behavior must match Chameleon exactly; new capabilities (async, concurrency) are
  opt-in.
- No runtime Python dependencies, ever: the package imports only the standard library and must
  not require stock Chameleon (compat vendors what it needs). Rust crates compile into the wheel;
  optional features go behind an extra, never a default dependency.
- Run `make lint test` before calling a change done, and report the real result.
