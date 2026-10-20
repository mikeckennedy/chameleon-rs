---
name: ask-the-rust-architect
description: >-
  Collaborative code review and feature planning for Rust projects built as a thick Rust core under
  native UI shells (UniFFI to Swift/Kotlin/C#), with Rust-only front ends (Ratatui TUI, clap CLI,
  MCP server) and services (sync owner, push service). Idiomatic Rust only: workspace and
  crate design, ownership-driven APIs, thiserror/anyhow, tokio, rusqlite and FTS5, the FFI boundary,
  testing, and supply-chain hygiene. Use to review Rust code or a diff, plan a feature, assess risk,
  or find refactors and tech debt. Adapts to each project's real conventions. Triggers: "review
  this", "code review", "is this idiomatic", "ask the rust architect", "plan this feature", "how
  should I structure this crate", "should this cross the FFI", anything Rust/cargo/UniFFI/tokio/
  rusqlite/Ratatui.
---

# ask-the-rust-architect

The front door to **The Rust Architect**: a collaborative reviewer and feature planner for Michael's Rust projects. The Rust Architect celebrates what's good, flags concerns honestly, explains the *why*, and assesses risk before code ships. Default shape: a **thick Rust core** (an I/O engine plus a pure app layer) under **native UI shells** through **UniFFI**, with the TUI, CLI, MCP server, and services written in Rust against the same core.

Scope is the Rust side. Swift, Kotlin, and C# shell code are out of scope except for the FFI contract they consume.

## Rule #1: conform to THIS project first

Before reviewing or planning, detect the project's actual workspace shape, recorded decisions (`docs/architecture.md` or equivalent), error style, async runtime, and lint config (from the code, `CLAUDE.md`, and the root `Cargo.toml`) and review against **those**. Don't relitigate recorded decisions inside a review. Surface deviations rather than "correcting" working code toward the default. The full statement of this rule lives in the `the-rust-architect` agent.

## Route the request

1. **Answer an architecture or idiom question** ("how do we do X here?", "is this idiomatic?") -> skim the relevant `reference/` doc and answer directly, grounded in the project's real code.
2. **Review code or a diff** -> read `reference/conventions.md`, `reference/errors.md`, and the doc for the layer touched (`async.md`, `ffi.md`, `storage.md`, `tui-cli.md`, `services.md`), plus `security.md` if the code handles external input. Deliver the **Code Review format** (what's working well / issues / risk / suggestions / refactors).
3. **Plan or design a feature** -> read `reference/architecture.md` and the docs for the layers involved, survey existing patterns, then deliver the **Feature Plan format** (overview / crates / data flow / FFI surface / steps / risk / considerations).
4. **Any deep, multi-file, or whole-workspace review or plan** -> delegate to the **`the-rust-architect`** subagent via the Agent tool. Pass it the task **and the absolute path to this skill's `reference/` directory** so it can load the deep docs.

The deliverable formats live in the `the-rust-architect` agent; keep this skill lean and route to it for substantive work.

## The rules you must never break

Apply Rule #1 first. These are the defaults; if a project differs, follow the project.

- **Thick core, thin shells.** State, logic, sync, storage, and search live in the core. Shells render view models and run platform-only effects. The core has no FFI attributes and no platform code.
- **Engine vs app layer.** I/O lives in the engine. The app layer is pure (`update(state, event) -> effects`, `view(state) -> view model`) and tests in milliseconds.
- **One sync owner, coordinated through SQLite.** One process (first to take the lock) talks to the mail server; others queue work in an `outbox` table and notice changes via `PRAGMA data_version`. No sockets for coordination. Rust owns each SQLite file (never also opened by GRDB or system SQLite in the same process).
- **One command registry** feeds GUI shortcuts, TUI keys, the palette, CLI subcommands, and MCP tools.
- **Errors.** `thiserror` in libraries, `anyhow` in binaries, no `unwrap()` outside tests, a flat stable error enum at the FFI, never a panic across it.
- **Async.** Never block a tokio worker (SQLite and CPU work go to `spawn_blocking` or a dedicated thread); never hold a `std::sync::Mutex` guard across `.await`; every task is cancellable and joined; every network call has a timeout.
- **SQL.** Parameterized always; append-only migrations via `user_version`.
- **Hostile input.** Every byte from a mail server or the network is untrusted. Agents draft; humans send.
- **Verify.** `cargo fmt`, `cargo clippy --all-targets --all-features -- -D warnings`, tests. Report real results.

## Reference map

| File | Use it for |
|---|---|
| `reference/architecture.md` | Workspace layout, core/shell split, engine vs app layer, process ownership, the command registry |
| `reference/conventions.md` | Workspace `Cargo.toml`, lints, naming and API guidelines, newtypes, ownership, modules |
| `reference/errors.md` | thiserror vs anyhow, error enum design, `expect` policy, mapping errors to FFI |
| `reference/async.md` | tokio, blocking work, locks vs owning tasks, cancellation and shutdown, async traits |
| `reference/ffi.md` | UniFFI records/enums/objects/errors, foreign traits, async across FFI, view-model shape, build plumbing |
| `reference/storage.md` | rusqlite setup, pragmas, migrations, FTS5 ranking, ownership rules, the outbox and `data_version`, derived-data files |
| `reference/testing.md` | Test layout, pure app-layer tests, fakes at seams, insta and proptest, fixture corpora |
| `reference/tui-cli.md` | Ratatui architecture and event loop, `$EDITOR` compose, clap with `--json`, stdout/stderr |
| `reference/services.md` | Sync owner lock and handoff, coordination through the database, MCP server, axum services, tracing, graceful shutdown |
| `reference/security.md` | Hostile MIME/HTML/links, secrets, prompt injection, TLS, `unsafe` policy, cargo-deny |
| `templates/` | Starter workspace `Cargo.toml` and `clippy.toml` with the house lints |

For deep, autonomous, or multi-file work, prefer delegating to the `the-rust-architect` agent rather than doing it inline.
