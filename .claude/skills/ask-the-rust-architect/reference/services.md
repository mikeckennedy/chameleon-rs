# Services: the sync owner, coordination, MCP, and HTTP

The long-running processes: the process that owns sync, the MCP server agents talk to, and any network service (such as a push relay). Read this before reviewing sync-ownership, coordination, server, or MCP code.

Apply Rule #1 first: match the project's recorded process model.

## The sync owner

When several front ends can run at once (GUI, TUI, CLI, MCP server), exactly one process syncs with the mail server (see `architecture.md`). The default is **first process wins**, with no daemon:

- Every front end can host the sync engine (it's core code). The first one to take the owner lock runs it; the others are clients.
- Clients retry the lock every few seconds, so when the owner quits, another running process takes over.
- If no front end is running, nothing syncs. That's acceptable for a desktop mail client (people leave it open), and it avoids a second binary, login-item registration, and sandbox plumbing.
- **A background daemon** (launchd agent, systemd user unit, Windows service) is the upgrade when something must run with every window closed, such as send-later. It hosts the same engine and takes the same lock, so adding one later changes packaging, not architecture.

Ownership is an OS file lock, which the OS releases if the process dies (no stale PID files):

```rust
use std::fs::{File, TryLockError};
use std::path::Path;

/// Holding this value means this process owns sync and writes. The OS releases the lock if the process dies.
#[derive(Debug)]
pub struct OwnerLock {
    _file: File,
}

#[derive(Debug, thiserror::Error)]
pub enum OwnerError {
    #[error("another process already owns sync")]
    AlreadyOwned,
    #[error("could not open the lock file")]
    Io(#[from] std::io::Error),
}

impl OwnerLock {
    /// Tries to become the owner without waiting. Callers that lose become clients of the owner.
    pub fn try_acquire(path: &Path) -> Result<Self, OwnerError> {
        let file = File::options().create(true).truncate(false).write(true).open(path)?;
        match file.try_lock() {
            Ok(()) => Ok(Self { _file: file }),
            Err(TryLockError::WouldBlock) => Err(OwnerError::AlreadyOwned),
            Err(TryLockError::Error(err)) => Err(OwnerError::Io(err)),
        }
    }
}
```

`File::try_lock` is in std since Rust 1.89; no crate needed.

## Coordination: the database is the channel

Processes coordinate through the shared SQLite file, not sockets or local servers (details and verified code in `storage.md`):

- **Actions**: any process applies the change locally and inserts an `outbox` row in the same transaction; the sync owner replays the outbox against the server and records each outcome.
- **Notifications**: every process polls `PRAGMA data_version` every 100 to 250 ms and re-reads when another process has committed. The owner uses the same signal to notice new outbox rows.
- **Schema is the protocol.** GUI and TUI builds can be out of sync, so outbox `action` values and their payloads are versioned like an API: add new actions, never change the meaning of old ones, and have the owner mark unknown actions `failed` with a clear error rather than dropping them.

Reach for a local socket (a Unix domain socket or named pipe via the `interprocess` crate, never TCP) only for something the database can't carry, such as streaming large data or sub-millisecond latency. A mail client shouldn't need one.

## The MCP server

The MCP server exposes the core to AI agents (Claude Desktop, Claude Code, others). Use `rmcp`, the official Rust MCP SDK; its API has changed between major versions, so follow the examples for the version in `Cargo.lock` rather than older blog posts.

Design rules:

- **Tools come from the command registry** with `Surface::Mcp` (see `architecture.md`): `search`, `get_thread`, `get_contact`, `sender_trust`, `list_folders`, `draft_email`.
- **Draft by default, send by human.** Send, delete, and move create pending actions the user approves in the app UI. An agent saying "the user approved" doesn't count. Email is the top prompt-injection vector; see `security.md`.
- **Return trust signals with message content** (SPF/DKIM/DMARC results, domain age, first-contact status) so the agent can discount instructions in a suspicious message.
- **Results are bounded.** Paginate, cap body length, and strip quoted text; an agent's context window is not a mailbox export.
- **Transports**: stdio for local clients (Claude Desktop, Claude Code). Streamable HTTP **bound to `127.0.0.1` only**, with a per-install bearer token and `Origin` checking (protects against DNS-rebinding attacks from web pages).
- **stdio servers must never write logs to stdout**; stdout is the protocol. Logs go to stderr or a file.
- The MCP server follows the same ownership rule as every front end: it runs the sync engine only while it holds the owner lock (for example, when Claude Desktop is open and the app isn't), and otherwise queues work in the outbox.

## HTTP services (axum)

For a network service, for example a push relay that watches mailboxes and sends APNs notifications (iOS won't keep an IMAP IDLE connection alive in the background):

```rust
async fn serve(bind: &str) -> anyhow::Result<()> {
    let shutdown = CancellationToken::new();
    let app = Router::new().route("/healthz", get(|| async { "ok" }));
    let listener = tokio::net::TcpListener::bind(bind).await?;
    tracing::info!(%bind, "listening");

    let token = shutdown.clone();
    tokio::spawn(async move {
        let _ = tokio::signal::ctrl_c().await;
        token.cancel();
    });

    axum::serve(listener, app).with_graceful_shutdown(async move { shutdown.cancelled().await }).await?;
    Ok(())
}
```

- **Graceful shutdown**: stop accepting, finish in-flight requests, then cancel background tasks through the same `CancellationToken` (see `async.md`). In containers also handle `SIGTERM` (`tokio::signal::unix::signal(SignalKind::terminate())`), since that's what orchestrators send.
- **Configuration from environment variables** parsed once at startup into a typed struct; fail fast with a clear message on anything missing. Secrets come from the environment or a secret store, never from the repo.
- **Tower middleware** (`tower-http`) for timeouts, request size limits, tracing, and compression. Every handler has a timeout.
- **Health endpoint** (`/healthz`) that checks what the service needs to work.
- A service that holds users' mail credentials carries the highest security burden in the product. Store credentials encrypted, scope OAuth tokens minimally, and keep it out of the hot path of reading mail.

## Observability

- **`tracing`** everywhere, with `tracing-subscriber` configured once in each binary's `main`. Libraries emit events; they never install a subscriber.
- **Structured fields, not formatted strings**: `tracing::info!(account = %id, folder, count, "synced")`.
- **`#[tracing::instrument(skip(password, body))]`** on important async functions; always `skip` secrets and message content.
- **`RUST_LOG`** (via `EnvFilter`) controls verbosity. Desktop builds also write to a rotating log file in the app's log directory (`tracing-appender`) so users can attach logs to a bug report.

## Review checklist

- [ ] Exactly one process can own sync; ownership is an OS lock, not a PID file, and passes to another running process when the owner exits.
- [ ] Processes coordinate through the outbox and `data_version`, not ad hoc sockets; outbox actions are versioned like an API.
- [ ] MCP tools can draft but not send, delete, or move without human approval in the app.
- [ ] HTTP MCP and other local servers bind to `127.0.0.1` and check a token and `Origin`.
- [ ] stdio servers keep stdout for protocol only.
- [ ] Services shut down gracefully and every handler has a timeout.
- [ ] Logs are structured and never contain secrets or message bodies.
