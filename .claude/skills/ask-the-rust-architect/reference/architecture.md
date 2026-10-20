# Architecture: thick Rust core, thin shells

The architecture-level pattern for a Rust-core application with native UI shells and Rust front ends. Read this before planning any feature. Line-level style lives in `conventions.md`; the boundary details live in `ffi.md`, `async.md`, and `storage.md`.

Apply Rule #1 first: if the project's `docs/architecture.md` records a different decision, follow it.

## The shape

```
            SwiftUI/AppKit   Compose    WinUI        Ratatui TUI   clap CLI   MCP server
                 |              |          |              |            |           |
                 +----- UniFFI bindings ---+              +------ direct Rust calls +
                                |                                     |
                         <app>-ffi crate                              |
                                |                                     |
                                +---------------+---------------------+
                                                |
                                         <app>-core crate
                                     app layer (pure, no I/O)
                                     engine (sync, store, search)
                                                |
                                     SQLite files, network, disk
```

- **The core owns** state, business logic, sync, persistence, search, and the command registry.
- **Shells own** rendering and platform-only side effects: keychain, notifications, background task scheduling, file pickers, share sheets, the app container path.
- **Native shells reach the core through UniFFI** (the `<app>-ffi` crate). **Rust shells import the core directly**, with no FFI.

Why: an email client, a sync tool, or anything with real state and protocols is a thick-core case. Writing sync, storage, and search once in Rust pays for itself; writing them three times in Swift, Kotlin, and C# does not. The precedent is Element X (matrix-rust-sdk under SwiftUI and Compose with plain UniFFI).

## Workspace layout

```
Cargo.toml                 # [workspace], resolver 3, shared deps and lints
clippy.toml
deny.toml
crates/
  <app>-core/              # lib: engine + app layer. No FFI, no platform code.
  <app>-ffi/               # lib (lib + staticlib + cdylib): the UniFFI facade
  <app>/                   # bin: one binary with subcommands (TUI default, CLI, mcp)
  uniffi-bindgen/          # bin: runs uniffi::uniffi_bindgen_main() to generate bindings
xtask/                     # optional: build plumbing (XCFramework, Android, bindgen) as Rust
docs/
  architecture.md          # recorded decisions: decided vs open
```

- **One binary with subcommands** (`app`, `app search ...`, `app mcp`, and `app daemon` if one is ever added) when the Rust front ends ship together. It's one static artifact to distribute with `cargo-dist` and one place for config and logging setup. Split binaries only when they ship separately (a server-side push service, for example).
- **Split the core into more crates only when it earns it**: compile times, or a piece another project will reuse (a MIME/quote parser, for example). Start with modules; promote to a crate later.
- **Dependency direction is strict**: shells and FFI depend on core; core depends on nothing app-specific. A `use <app>_ffi` inside the core is a bug.

## Inside the core: engine and app layer

Borrowed from Crux's discipline, without the Crux dependency (Crux sends all I/O to the shell, which is wrong for a thick core with long-lived IMAP connections).

```
crates/<app>-core/src/
  lib.rs
  ids.rs              # newtypes: MessageId, AccountId, ImapUid
  error.rs            # CoreError (thiserror)
  commands.rs         # the command registry
  app/                # PURE: state, events, update, view models. No I/O, no async.
  engine/             # I/O: sync, transports, store, search, trust checks
    store.rs
    sync.rs
    transport/        # trait MailTransport + imap.rs, jmap.rs, gmail.rs, graph.rs
```

**The app layer** is a state machine: events in, effects and view models out.

```rust
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    InboxLoaded(Vec<InboxItem>),
    MoveDown,
    MoveUp,
    ArchiveSelected,
}

/// Work the app layer asks the engine (or shell) to perform.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Effect {
    Archive(MessageId),
    Render,
}

impl AppState {
    #[must_use]
    pub fn update(&mut self, event: Event) -> Vec<Effect> { /* pure */ }

    #[must_use]
    pub fn view(&self) -> InboxView { /* pure */ }
}
```

- `update` mutates state and returns effects. It never touches the network, disk, clock, or randomness; those come in as event data.
- `view` produces plain data the shell draws. View models are cheap, cloneable, and FFI-friendly (see `ffi.md`).
- The engine executes effects (archive on the server, write to SQLite) and feeds results back as events.

**The engine** does the I/O. Its seams are traits (`MailTransport`, `Clock` if time matters for tests) with real and fake implementations. See `async.md` for writing them.

The payoff: the app layer tests in microseconds with no runtime, and the engine tests against fakes and a temp SQLite file.

## Data flow

```
shell input (key, click, CLI arg, MCP call)
  -> Event
  -> app::update(state, event) -> effects
  -> engine runs effects (network, SQLite)
  -> engine emits result events / change notifications
  -> app::view(state) -> view model
  -> shell renders
```

For native shells, the shell gets "something changed" through a foreign callback trait and pulls the view model it needs (see `ffi.md`). Pull beats push for large data: the shell asks for the rows it's displaying.

## Processes and ownership

When the GUI, the TUI, and the MCP server can all run at once, two processes syncing the same account will fight (duplicate fetches, racing flag updates, conflicting UIDVALIDITY handling).

- **One process owns sync.** Whichever front end first takes an OS file lock runs the sync engine; when it exits, another running front end takes over. A background daemon is an optional later upgrade that hosts the same engine (see `services.md`).
- **The database is the coordination channel.** Every front end reads and writes the shared SQLite file (WAL, short transactions). Server-side work goes into an `outbox` table that only the owner replays, and every process notices other processes' commits by polling `PRAGMA data_version`. No sockets or local servers. See `storage.md`.
- **Inside a process, one writer connection per SQLite file**: a single writer on an owning thread or task, plus reader connections. See `storage.md`.

## The command registry

Each user-facing command is defined once in the core:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Surface {
    Gui,
    Tui,
    Cli,
    Mcp,
}

#[derive(Debug)]
pub struct CommandSpec {
    /// Stable id, used as the CLI subcommand and MCP tool name.
    pub id: &'static str,
    pub title: &'static str,
    pub default_keys: &'static [&'static str],
    pub surfaces: &'static [Surface],
}

pub const COMMANDS: &[CommandSpec] = &[
    CommandSpec {
        id: "archive",
        title: "Archive",
        default_keys: &["e"],
        surfaces: &[Surface::Gui, Surface::Tui, Surface::Cli],
    },
    CommandSpec {
        id: "search",
        title: "Search",
        default_keys: &["/"],
        surfaces: &[Surface::Gui, Surface::Tui, Surface::Cli, Surface::Mcp],
    },
];

#[must_use]
pub fn command_for_key(key: &str, surface: Surface) -> Option<&'static CommandSpec> {
    COMMANDS.iter().find(|c| c.surfaces.contains(&surface) && c.default_keys.contains(&key))
}
```

Note that `archive` isn't exposed to MCP: destructive actions stay human-approved (see `security.md`).

GUI menu shortcuts, the TUI keymap, the command palette, CLI subcommands, and MCP tools are all generated or looked up from this list. Adding a command is one change in the core, and every surface picks it up. A command that exists on only one surface is a smell to flag in review.

## Build order and plumbing

- Build a Rust front end (the TUI) or the first native shell against the core early, so the core's API is shaped by a real consumer.
- **Keep the FFI platform-neutral from day one**, even while only Swift consumes it. Kotlin and C# bindings come later and should need no API changes.
- Budget for plumbing: cross-compile targets, XCFramework packaging, Android NDK, signing on each platform. Put the scripts in `xtask` or a `justfile` so they're versioned and repeatable.

## Smells to flag in review

- I/O, `async`, or `SystemTime::now()` inside the app layer.
- `uniffi` attributes or `#[cfg(target_os = ...)]` business logic in the core.
- A process that talks to a mail server without holding the owner lock, or performs a server-side action directly instead of queueing it in the outbox.
- A command implemented separately in two shells.
- View models that ship whole tables across FFI instead of the visible window.
