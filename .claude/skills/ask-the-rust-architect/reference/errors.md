# Error handling

How errors are modeled, propagated, and shown, from the engine to the FFI boundary to a binary's `main`. Read this before reviewing any fallible code.

Apply Rule #1 first: match the project's error style if it has one.

## The split: thiserror in libraries, anyhow in binaries

| Where | Use | Why |
|---|---|---|
| Library crates (`<app>-core`) | `thiserror` enums | Callers need to **match** on what went wrong (re-auth vs retry vs show "not found") |
| Binaries (`main`, CLI handlers, daemon setup) | `anyhow::Result` + `.context(...)` | Callers only **report**; context strings explain the chain |
| FFI facade (`<app>-ffi`) | A flat `uniffi::Error` enum | Foreign code needs stable cases and a message, never Rust internals |

## Designing a library error enum

```rust
use crate::ids::MessageId;

/// Errors the engine can return. Callers match on the variant, so each one is a real case.
#[derive(Debug, thiserror::Error)]
pub enum CoreError {
    #[error("message {0} not found")]
    MessageNotFound(MessageId),

    #[error("account {account} needs to sign in again")]
    AuthExpired { account: String },

    #[error("network error talking to {host}")]
    Network {
        host: String,
        #[source]
        source: std::io::Error,
    },

    #[error("database error")]
    Database(#[from] rusqlite::Error),
}

pub type Result<T, E = CoreError> = std::result::Result<T, E>;
```

- **A variant per distinct caller decision.** `AuthExpired` triggers a sign-in flow, `Network` triggers a retry, `MessageNotFound` shows a message. If no caller would ever treat two variants differently, merge them.
- **Messages are lowercase, without trailing punctuation**, and don't repeat the source's message. `#[source]` and `#[from]` chain the cause; reporters walk the chain.
- **`#[from]` only when the conversion is unambiguous.** `rusqlite::Error` always means "database"; `std::io::Error` might mean network or disk, so wrap it explicitly with context fields.
- **Carry identifying data** (the id, the host, the account) as fields, not baked into a string.
- **One error enum per crate** is the default. Split per module only when the modules' failures are genuinely unrelated and the enum grows unwieldy.
- **Don't make the error type generic or boxed** (`Box<dyn Error>`) in library APIs. Callers can't match on it.

## anyhow at the edges

```rust
use anyhow::Context;

fn load_config(path: &std::path::Path) -> anyhow::Result<String> {
    std::fs::read_to_string(path).with_context(|| format!("reading config {}", path.display()))
}
```

- **Add context where the information lives**: the file path, the account, the operation. `with_context` takes a closure, so the string is only built on failure.
- `main` returns `anyhow::Result<()>`; anyhow prints the full cause chain.
- **Don't use `anyhow` in a library's public API.** It erases the type callers need to match on.
- To branch on a library error inside a binary, match on it before converting, or `downcast_ref::<CoreError>()` on the anyhow error.

## unwrap, expect, and panics

- **`unwrap()` is denied outside tests** (`clippy::unwrap_used = "deny"`; `clippy.toml` allows it in tests).
- **`expect("...")` is for true invariants**, and the message states the invariant: `.expect("regex literal is valid")`. If the "invariant" depends on input from outside the process, it is not an invariant; return an error.
- **Prefer infallible constructions** that remove the need: `let Some(x) = ... else { return ... };`, `unwrap_or_default()`, `unwrap_or_else(...)`.
- **Poisoned locks**: a `std::sync::Mutex` is poisoned when a holder panicked. For plain data, recover with `lock().unwrap_or_else(PoisonError::into_inner)` rather than propagating the panic.
- **Panics are bugs, never control flow.** No `panic!` for invalid user or server input.

## Errors across the FFI boundary

Foreign code gets a flat, stable enum with a message. Map from the core error at the facade:

```rust
/// Errors as foreign code sees them: flat, stable, and message-carrying.
#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum FfiError {
    #[error("not found: {detail}")]
    NotFound { detail: String },
    #[error("sign-in required for {account}")]
    AuthRequired { account: String },
    #[error("{detail}")]
    Internal { detail: String },
}

impl From<CoreError> for FfiError {
    fn from(err: CoreError) -> Self {
        match err {
            CoreError::MessageNotFound(id) => Self::NotFound { detail: id.to_string() },
            CoreError::AuthExpired { account } => Self::AuthRequired { account },
            other => Self::Internal { detail: other.to_string() },
        }
    }
}
```

- In Swift this becomes `enum FfiError: Swift.Error, LocalizedError` and exported functions become `throws`; Kotlin gets an exception hierarchy.
- **Only expose cases the UI acts on.** Everything else folds into `Internal` with a message (log the full chain on the Rust side first).
- **Never expose `rusqlite::Error`, `std::io::Error`, or anyhow** through the FFI. Swift shouldn't know the core uses SQLite.
- **Panics**: UniFFI catches a panic in an exported function and surfaces it as an unexpected error in the foreign language. That keeps the app alive, but a panic is still a bug to fix, and with `panic = "abort"` there is no catching at all. Keep the default unwind in FFI-loaded crates.

## Logging errors

- Log an error once, where it's handled, not at every `?` on the way up.
- Use `tracing` with fields: `tracing::warn!(error = %err, account = %id, "sync failed")`. Use `%err` (Display) for the message, or the `{err:#}`/`{err:?}` format for anyhow chains.
- Never log secrets or message bodies (see `security.md`).

## Review checklist

- [ ] Library functions return the crate's typed error, not `anyhow` or `Box<dyn Error>`.
- [ ] Each variant corresponds to a caller decision; no "Other(String)" catch-all doing real work.
- [ ] No `unwrap()` in non-test code; each `expect` names a real invariant.
- [ ] `?` across layers adds context or maps to the right variant.
- [ ] FFI errors are flat and stable; no Rust error types leak.
- [ ] Errors are logged once, with structured fields, without secrets.
