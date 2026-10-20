# Async and concurrency

tokio patterns for the engine, the sync owner, and services. Read this before reviewing anything with `async`, threads, locks, or channels.

Apply Rule #1 first: if the project uses another runtime or is sync-only, review against that.

## The runtime

- **tokio, multi-thread, owned by the engine.** Binaries use `#[tokio::main]`. A library loaded through FFI has no `main`, so the engine builds and owns its runtime (or the FFI facade uses UniFFI's `async_runtime = "tokio"`, see `ffi.md`).
- **One runtime per process.** Never call `Runtime::block_on` from inside async code (it panics), and never create a runtime per request.
- **Futures you `tokio::spawn` must be `Send + 'static`.** Move owned data in (`async move`, clone `Arc`s before the spawn), not borrows.

## Never block a worker thread

A tokio worker that blocks stalls every task scheduled on it. In an email client that means the UI stops getting updates while a big FTS query runs.

Blocking work includes rusqlite calls, embedding inference, HTML-to-text conversion, MIME parsing of large messages, `std::fs`, `std::thread::sleep`, and anything CPU-bound that takes more than about 100 microseconds.

```rust
/// Blocking work (SQLite, embeddings, HTML parsing) never runs on an async worker thread.
pub async fn count_words_off_runtime(text: String) -> usize {
    tokio::task::spawn_blocking(move || text.split_whitespace().count()).await.unwrap_or(0)
}
```

- **`spawn_blocking`** for occasional blocking calls.
- **A dedicated thread with a channel** for a resource that is always blocking and must be serialized, like the SQLite writer connection (see `storage.md`).
- **`tokio::time::sleep`**, never `std::thread::sleep`, in async code.

## Shared state: prefer an owning task

The default for mutable shared state is **one task that owns it**, plus a cloneable handle that sends it messages. No locks, no poisoning, no lock held across an await, and the state's lifetime is obvious.

```rust
use std::collections::HashMap;

use tokio::sync::{mpsc, oneshot};

/// Messages the owning task understands. Replies travel back on a oneshot channel.
#[derive(Debug)]
enum Request {
    Get { domain: String, reply: oneshot::Sender<Option<bool>> },
    Put { domain: String, reachable: bool },
}

/// A cheap, cloneable handle. The cache itself lives inside one task, so there is no lock.
#[derive(Debug, Clone)]
pub struct DomainCache {
    tx: mpsc::Sender<Request>,
}

impl DomainCache {
    #[must_use]
    pub fn spawn() -> Self {
        let (tx, mut rx) = mpsc::channel(64);
        tokio::spawn(async move {
            let mut cache: HashMap<String, bool> = HashMap::new();
            while let Some(request) = rx.recv().await {
                match request {
                    Request::Get { domain, reply } => {
                        let _ = reply.send(cache.get(&domain).copied());
                    }
                    Request::Put { domain, reachable } => {
                        cache.insert(domain, reachable);
                    }
                }
            }
            // All handles dropped: the task ends on its own.
        });
        Self { tx }
    }

    pub async fn get(&self, domain: &str) -> Option<bool> {
        let (reply, response) = oneshot::channel();
        self.tx.send(Request::Get { domain: domain.to_owned(), reply }).await.ok()?;
        response.await.ok().flatten()
    }

    pub async fn put(&self, domain: &str, reachable: bool) {
        let _ = self.tx.send(Request::Put { domain: domain.to_owned(), reachable }).await;
    }
}
```

When a lock is simpler (a small counter, a config snapshot), use one correctly:

- **`std::sync::Mutex`** (or `parking_lot`) for short critical sections with **no `.await` inside**. It's faster than the tokio mutex.
- **Never hold a `std::sync::MutexGuard` across `.await`.** Inside a spawned task the compiler rejects it ("future cannot be sent between threads safely"). In code that isn't spawned (a `block_on`, some FFI paths) it compiles and can deadlock. Scope the guard in a block that ends before the await:

```rust
pub async fn bump_then_wait(&self) {
    {
        let mut n = self.inner.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        *n += 1;
    }
    tokio::task::yield_now().await;
}
```

- **`tokio::sync::Mutex`** only when a lock genuinely must be held across an await (serializing access to one IMAP connection, for example). It's slower and easy to over-use.
- **`RwLock`** for read-mostly data; **`tokio::sync::watch`** to broadcast "latest value" (current sync status, settings); **`broadcast`** for events many listeners need.

## Channels

- **Bounded by default** (`mpsc::channel(n)`). An unbounded channel turns a slow consumer into a memory leak. Pick the bound deliberately and note what happens when it's full.
- `oneshot` for request/response; `watch` for state; `broadcast` for fan-out events; `mpsc` for work queues and actor mailboxes.
- A closed channel is a normal shutdown signal, not an error to panic on.

## Cancellation and shutdown

Nothing is fire-and-forget. Every long-lived task:

1. listens on a `tokio_util::sync::CancellationToken`,
2. lives in a `JoinSet` or `TaskTracker` that someone joins at shutdown,
3. finishes or rolls back its current unit of work before exiting.

```rust
/// A long-running sync loop that shuts down cleanly when the token is cancelled.
pub async fn run_sync_loop(mut commands: mpsc::Receiver<SyncCommand>, shutdown: CancellationToken) {
    let mut interval = tokio::time::interval(Duration::from_secs(60));
    loop {
        tokio::select! {
            () = shutdown.cancelled() => break,
            Some(command) = commands.recv() => {
                tracing::info!(?command, "sync requested");
            }
            _ = interval.tick() => {
                tracing::debug!("periodic sync");
            }
        }
    }
}
```

```rust
/// Spawns child tasks into a `JoinSet` so none outlive the engine.
pub async fn sync_all(accounts: Vec<String>, shutdown: CancellationToken) -> usize {
    let mut tasks = JoinSet::new();
    for account in accounts {
        let shutdown = shutdown.clone();
        tasks.spawn(async move {
            tokio::select! {
                () = shutdown.cancelled() => None,
                () = tokio::time::sleep(Duration::from_millis(1)) => Some(account),
            }
        });
    }
    let mut done = 0;
    while let Some(result) = tasks.join_next().await {
        if matches!(result, Ok(Some(_))) {
            done += 1;
        }
    }
    done
}
```

- **Cancellation safety in `select!`**: when one branch wins, the others are dropped mid-flight. `mpsc::Receiver::recv`, `CancellationToken::cancelled`, and `sleep` are cancel-safe. Reading a partial frame from a socket, or a multi-step "fetch then write to SQLite" future, is not. Keep non-cancel-safe work out of `select!` branches, or make it run to completion in its own task.
- A dropped `JoinSet` aborts its tasks, which is a good default for child work.
- **iOS and Android background limits**: the OS suspends the process with little warning. Commit SQLite transactions promptly and don't hold file locks while idle (see `storage.md`).

## Timeouts and retries

- **Every network call has a timeout** (`tokio::time::timeout`), including connect, login, and IDLE re-arm. A hung IMAP server must not hang a sync task forever.
- **Retries use exponential backoff with jitter and a cap.** Don't retry auth failures; surface them (`CoreError::AuthExpired`).

## Async traits

Native `async fn` in traits works (Rust 1.75+), but the returned future's `Send`-ness isn't part of the signature, so generic callers can't spawn it. Spell out the bound in the trait:

```rust
use std::future::Future;

/// The seam between the sync engine and a mail protocol (IMAP, JMAP, Gmail API, Graph).
/// Spelling out `+ Send` lets the engine spawn these futures on a multi-thread runtime.
pub trait MailTransport: Send + Sync {
    fn fetch_since(&self, folder: &str, after_uid: u32) -> impl Future<Output = Result<Vec<RawMessage>>> + Send;
}

/// Generic over the transport: static dispatch, no boxing.
pub async fn sync_folder<T: MailTransport>(transport: &T, folder: &str, after_uid: u32) -> Result<u32> {
    let batch = transport.fetch_since(folder, after_uid).await?;
    Ok(batch.iter().map(|m| m.uid).max().unwrap_or(after_uid))
}
```

- Implementations can still write `async fn fetch_since(...)`. A fake with nothing to await returns `std::future::ready(...)` to keep clippy's `unused_async` quiet.
- Traits with `impl Future` returns are **not dyn-compatible**. For `Box<dyn MailTransport>`, use an enum over the closed set of providers (preferred: IMAP, JMAP, Gmail, Graph is a closed set), or the `async-trait` crate.

## Review checklist

- [ ] No blocking calls (rusqlite, `std::fs`, heavy CPU, `thread::sleep`) on a runtime worker.
- [ ] No `std::sync::MutexGuard` alive across `.await`.
- [ ] Every spawned task is cancellable and joined; no orphaned `tokio::spawn`.
- [ ] Channels are bounded, and full or closed channels are handled.
- [ ] Every network operation has a timeout; retries back off and give up.
- [ ] `select!` branches are cancel-safe.
- [ ] Shared mutable state has one clear owner.
