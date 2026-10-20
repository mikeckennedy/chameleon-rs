# The FFI boundary (UniFFI)

How the Rust core is exposed to Swift, Kotlin, and C# shells. Read this before reviewing anything in the `<app>-ffi` crate or planning a feature that changes what shells can call.

Apply Rule #1 first: if the project uses swift-bridge, csbindgen, or Crux, review against that.

## Principles

- **One facade crate.** `<app>-ffi` is the only crate with `uniffi` in it. The core stays FFI-free so it can be tested, used by the TUI, and refactored without touching bindings.
- **Platform-neutral from day one.** Even while Swift is the only consumer, design every exported type and method so Kotlin and C# can consume it unchanged. No Apple-only concepts in the API.
- **Coarse-grained calls.** Each FFI call has overhead and every exported type is a contract with three languages. Export intent-level operations (`dispatch(action)`, `rows(offset, limit)`), not getters for every field.
- **The FFI surface is an API with a version.** Changing it ripples into every shell. Plan FFI changes explicitly (the feature plan format has a section for it).
- **The shell provides platform facts.** Paths (app container, caches), secrets from the keychain, network reachability, and "app is going to background" come from the shell as arguments or host-trait calls. Rust never guesses where it is.

## Crate setup

```toml
[package]
name = "app-ffi"
version = "0.1.0"
edition.workspace = true
rust-version.workspace = true

[lib]
crate-type = ["lib", "staticlib", "cdylib"]

[dependencies]
app-core.workspace = true
thiserror.workspace = true
tokio.workspace = true
uniffi = { workspace = true, features = ["tokio"] }

[lints]
workspace = true
```

- `staticlib` for Apple (link a static library into the XCFramework, the simplest App Store story); `cdylib` for Android, for C#, and for generating bindings in library mode; `lib` so Rust tests can use the crate.
- `uniffi::setup_scaffolding!();` once at the top of `lib.rs`.
- The workspace's `unsafe_code = "forbid"` stays on: UniFFI's generated scaffolding compiles under it (verified with uniffi 0.32).
- A tiny `uniffi-bindgen` binary crate generates bindings:

```rust
fn main() {
    uniffi::uniffi_bindgen_main()
}
```

with `uniffi = { workspace = true, features = ["cli"] }`, then:

```bash
cargo build -p app-ffi
cargo run -p uniffi-bindgen -- generate --library target/debug/libapp_ffi.dylib --language swift --out-dir bindings/swift
```

This produces `app_ffi.swift`, `app_ffiFFI.h`, and `app_ffiFFI.modulemap`. Generated files are build output: regenerate them, never hand-edit them.

## The vocabulary

| Rust | Derive / attribute | Swift sees | Use for |
|---|---|---|---|
| Plain data struct | `#[derive(uniffi::Record)]` | `struct` (value type, `Equatable, Hashable`) | View models, rows, settings |
| Enum (with or without data) | `#[derive(uniffi::Enum)]` | `enum` | User actions, states |
| Error enum | `#[derive(thiserror::Error, uniffi::Error)]` | `enum: Error, LocalizedError`, methods `throws` | Fallible calls (see `errors.md`) |
| Object with methods | `#[derive(uniffi::Object)]` + `#[uniffi::export] impl` | `class` | The engine handle, long-lived services |
| Trait foreign code implements | `#[uniffi::export(foreign)] pub trait` (or `rust, foreign` if Rust implements it too; `with_foreign` is the deprecated spelling) | `protocol` | Platform effects (notify, keychain) |

## A facade, end to end

```rust
//! `UniFFI` facade over the core. The only crate that knows about FFI.
use std::sync::{Arc, Mutex};

use app_core::app::{AppState, Event};

uniffi::setup_scaffolding!();

#[derive(Debug, Clone, Copy, uniffi::Enum)]
pub enum UserAction {
    MoveDown,
    MoveUp,
    Archive,
}

/// Implemented in Swift/Kotlin: platform side effects the core can't do itself.
#[uniffi::export(foreign)]
pub trait PlatformHost: Send + Sync {
    fn notify(&self, title: String, body: String);
    fn view_changed(&self);
}

#[derive(uniffi::Object)]
pub struct Letterbox {
    state: Mutex<AppState>,
    host: Arc<dyn PlatformHost>,
}

#[uniffi::export]
impl Letterbox {
    /// The shell passes the data directory; Rust never guesses platform paths.
    #[uniffi::constructor]
    pub fn new(data_dir: &str, host: Arc<dyn PlatformHost>) -> Result<Arc<Self>, FfiError> {
        if data_dir.is_empty() {
            return Err(FfiError::Internal { detail: "empty data dir".into() });
        }
        Ok(Arc::new(Self { state: Mutex::new(AppState::default()), host }))
    }

    pub fn dispatch(&self, action: UserAction) {
        let event = match action {
            UserAction::MoveDown => Event::MoveDown,
            UserAction::MoveUp => Event::MoveUp,
            UserAction::Archive => Event::ArchiveSelected,
        };
        let effects = self.lock_state().update(event);
        if !effects.is_empty() {
            self.host.view_changed();
        }
    }
}

#[uniffi::export(async_runtime = "tokio")]
impl Letterbox {
    /// Async across FFI: Swift sees `async throws`, Kotlin sees `suspend`.
    pub async fn sync_now(&self) -> Result<u32, FfiError> {
        tokio::time::sleep(std::time::Duration::from_millis(1)).await;
        Ok(0)
    }
}
```

Swift gets `init(dataDir: String, host: PlatformHost) throws`, `func dispatch(action: UserAction)`, `func syncNow() async throws -> UInt32`, and `protocol PlatformHost: AnyObject, Sendable`.

Notes:

- **Constructors return `Arc<Self>`**; objects are shared, so methods take `&self` and use interior mutability.
- **`&str` parameters work** and avoid clippy's `needless_pass_by_value`. Small enums passed by value should derive `Copy` for the same reason.
- **Foreign traits are `Send + Sync`** and Rust may call them from any thread. The Swift implementation must hop to the main actor before touching UI state. Say so in the trait's docs.
- In a real app the `Mutex<AppState>` would sit behind the engine's owning task (see `async.md`); this example keeps it minimal.

## Threading across the boundary

- **Synchronous exports run on the caller's thread**, which for a UI shell is usually the main thread. A sync export must return in well under a frame (a few milliseconds): read from memory, enqueue a command, return. Anything slower (SQLite queries, network, search) is an `async` export or a command whose result arrives through the host trait.
- **Async exports** use `#[uniffi::export(async_runtime = "tokio")]` with uniffi's `tokio` feature, so tokio-dependent futures (timers, tokio I/O) work when Swift or Kotlin awaits them.
- **Callbacks into foreign code are cheap but not free.** Coalesce: notify "inbox changed" once per batch, not once per message.

## View-model shape: pull a window, keep ids stable

Large collections never cross the FFI wholesale. The shell asks for the window it's displaying:

```rust
/// One visible row. `id` is stable across updates so the shell can diff and keep selection.
#[derive(Debug, Clone, uniffi::Record)]
pub struct MessageRow {
    pub id: i64,
    pub sender: String,
    pub subject: String,
    pub snippet: String,
    pub unread: bool,
}

/// The shell asks for the window it is displaying; the core never ships the whole mailbox.
#[uniffi::export]
impl Letterbox {
    pub fn row_count(&self) -> u32 { /* ... */ }
    pub fn rows(&self, offset: u32, limit: u32) -> Vec<MessageRow> { /* ... */ }
}
```

- This fits AppKit's `NSTableView` data source (`numberOfRows` plus row fetch) and SwiftUI's `List`/`Table` with stable `id`s equally well. Records are `Hashable` in Swift, so diffable data sources work directly.
- **Stable ids** let the shell preserve selection and scroll position and animate inserts and removals.
- **Pre-format in Rust** what every shell would otherwise format identically (relative dates, sender display names, snippets), but leave locale-sensitive presentation that the platform does better (number and date formatting styles) to the shell when it matters.
- **Push "what changed", pull the data.** The host trait says `view_changed()` (or a finer-grained `rows_changed(ids)`); the shell pulls what it needs.

## Panics

UniFFI catches a panic in an exported function and surfaces it to Swift or Kotlin as an unexpected error, which keeps the app alive. That only works with `panic = "unwind"` (the default); never set `panic = "abort"` in a profile used for the FFI library. Hand-rolled `extern "C"` code outside UniFFI needs `std::panic::catch_unwind`. Either way, a panic is a bug to fix (see `errors.md`).

## Packaging

- **Apple**: build `staticlib` for each target (`aarch64-apple-darwin` and `x86_64-apple-darwin` combined with `lipo` for a universal macOS slice; `aarch64-apple-ios`, `aarch64-apple-ios-sim` for iOS), wrap them with the generated header and modulemap in an XCFramework, and ship that inside a Swift Package. `cargo-swift` automates this. Declare required-reason APIs in `PrivacyInfo.xcprivacy` (Rust's `std::fs::metadata` calls `stat`, which Apple's scanner flags as the file-timestamp category).
- **Android**: `cargo-ndk` builds the `cdylib` for each ABI into `jniLibs`; generate Kotlin bindings with `--language kotlin`.
- **Windows/C#**: `uniffi-bindgen-cs` (third-party generator), or csbindgen for a hand-shaped C ABI.
- Script all of this in `xtask` or a `justfile`. Plumbing that lives in someone's shell history breaks the first time someone else builds.

## Review checklist

- [ ] `uniffi` appears only in the FFI crate.
- [ ] No exported type leaks core internals (`rusqlite` types, `anyhow`, paths the shell didn't provide).
- [ ] Sync exports are fast; slow work is async or command-plus-callback.
- [ ] Collections are windowed with stable ids, not shipped whole.
- [ ] Foreign-trait docs say which thread calls them.
- [ ] Errors map to the flat FFI enum; no `unwrap` or panic path.
- [ ] The API would make sense in Kotlin and C# unchanged.
