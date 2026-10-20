# Storage: rusqlite, migrations, FTS5

The local data layer: one SQLite file per concern, owned by Rust, with FTS5 for search. Read this before reviewing any SQL, schema change, or code that opens a database.

Apply Rule #1 first: if the project uses another store, review against that.

## Setup

- **`rusqlite` with the `bundled` feature.** Every platform gets the same SQLite version with FTS5 and JSON compiled in, instead of whatever the OS ships.
- **Rust owns the database.** The path comes from the shell (`data_dir` argument); Rust never guesses the app container.
- **Pragmas on every connection**: WAL, `synchronous = NORMAL` (safe with WAL, much faster than `FULL`), `foreign_keys = ON` (off by default in SQLite), and a busy timeout.

```rust
fn configure(mut conn: Connection) -> Result<Self> {
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.pragma_update(None, "foreign_keys", "ON")?;
    conn.busy_timeout(std::time::Duration::from_secs(5))?;
    migrate(&mut conn)?;
    Ok(Self { conn })
}
```

## Ownership rules (these prevent corruption)

- **One SQLite library per file per process.** If Swift also opens the same file through GRDB, Core Data, or the system `libsqlite3` while Rust's bundled copy has it open, two SQLite copies share one process. POSIX advisory locks are per process, so closing a descriptor in one copy can silently drop the other's locks, and the database can corrupt. Rust owns the file; Swift calls through Rust. (Separate *processes* using different SQLite builds on one file are fine; the hazard is two copies inside one process.)
- **One writer connection per process.** Within a process, all writes go through a single connection on its own thread, in order. Readers use separate connections (WAL lets them read while the writer writes).
- **Several processes may write the same file.** SQLite serializes writers across processes with its file locks; WAL plus a busy timeout makes that safe and cheap, as long as every transaction is short. What must be single is the process that **syncs with the remote server**, not the process that writes (see `architecture.md`). The sync owner commits big batches in chunks so another process's quick write never waits long.
- **Never on a network filesystem.** SQLite's locking is unreliable over NFS/SMB, and WAL requires shared memory on one machine.
- **iOS App Group containers**: iOS kills an app suspended while holding a file lock in a shared container (crash code `0xdead10cc`). Keep transactions short and finished before backgrounding, or keep the database in the app's private container.

### The single writer

rusqlite is blocking, so the writer lives on a dedicated OS thread and async code awaits its results:

```rust
type Job = Box<dyn FnOnce(&mut Connection) + Send>;

/// The single writer for one database file. All writes run on its own OS thread, in order.
#[derive(Debug, Clone)]
pub struct Writer {
    jobs: mpsc::Sender<Job>,
}

impl Writer {
    pub fn open(path: &Path) -> Result<Self> {
        let mut conn = Connection::open(path)?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        let (jobs, mut rx) = mpsc::channel::<Job>(256);
        thread::Builder::new()
            .name("sqlite-writer".into())
            .spawn(move || {
                while let Some(job) = rx.blocking_recv() {
                    job(&mut conn);
                }
            })
            .map_err(CoreError::WriterSpawn)?;
        Ok(Self { jobs })
    }

    /// Runs `f` on the writer thread and awaits its result without blocking the runtime.
    pub async fn write<T, F>(&self, f: F) -> Result<T>
    where
        T: Send + 'static,
        F: FnOnce(&mut Connection) -> Result<T> + Send + 'static,
    {
        let (reply, response) = oneshot::channel();
        let job: Job = Box::new(move |conn| {
            let _ = reply.send(f(conn));
        });
        self.jobs.send(job).await.map_err(|_| CoreError::WriterStopped)?;
        response.await.map_err(|_| CoreError::WriterStopped)?
    }
}
```

Reads that might be slow (search, large lists) run on reader connections via `spawn_blocking`, never on a tokio worker (see `async.md`).

## Migrations

Append-only, tracked by `PRAGMA user_version`, each in its own transaction:

```rust
/// Ordered schema migrations. Append only: never edit an entry that has shipped.
const MIGRATIONS: &[&str] = &[
    // 1: messages + full-text index
    "CREATE TABLE message (
        id INTEGER PRIMARY KEY,
        subject TEXT NOT NULL,
        sender TEXT NOT NULL,
        received_at INTEGER NOT NULL
    );
    CREATE VIRTUAL TABLE mail_fts USING fts5(
        subject, sender, body, quoted, signature,
        tokenize = 'porter unicode61 remove_diacritics 2',
        prefix = '2 3'
    );",
    // 2: remember whether we have ever replied to a sender
    "ALTER TABLE message ADD COLUMN replied INTEGER NOT NULL DEFAULT 0;",
];

fn migrate(conn: &mut Connection) -> Result<()> {
    let current: i64 = conn.pragma_query_value(None, "user_version", |row| row.get(0))?;
    let applied = usize::try_from(current).unwrap_or(0);
    for (version, sql) in (1_i64..).zip(MIGRATIONS).skip(applied) {
        let tx = conn.transaction()?;
        tx.execute_batch(sql)?;
        tx.pragma_update(None, "user_version", version)?;
        tx.commit()?;
    }
    Ok(())
}
```

- **Never edit a migration that has shipped.** Fix forward with a new one. Editing an old entry means existing users and new users end up with different schemas.
- rusqlite 0.40 doesn't map `usize` to SQL; use `i64` for `user_version` and counts.
- SQLite's `ALTER TABLE` is limited. Bigger changes use the create-copy-drop-rename dance inside one migration.
- Test migrations by opening a database twice (idempotent) and by migrating a fixture database from each shipped version.

## Queries

- **Parameterize everything** (`?1`, `params![...]`). Never `format!` a value into SQL.
- **`prepare_cached`** for statements that run often.
- **`.optional()`** (from `OptionalExtension`) turns "no row" into `None`, then map it to a typed error:

```rust
pub fn message(&self, id: MessageId) -> Result<MessageRow> {
    self.conn
        .prepare_cached("SELECT id, subject, sender FROM message WHERE id = ?1")?
        .query_row([id.get()], |row| {
            Ok(MessageRow { id: MessageId::new(row.get(0)?), subject: row.get(1)?, sender: row.get(2)? })
        })
        .optional()?
        .ok_or(CoreError::MessageNotFound(id))
}
```

- **Transactions for multi-statement writes**, and keep them short: no network calls inside a transaction.
- Map rows into structs at the boundary; don't pass `rusqlite::Row` around.

## Full-text search with FTS5

- **Use FTS5 as the index and own the ranking.** Don't build a custom inverted index.
- **Index parsed fields as separate columns** (subject, sender, new body, quoted text, signature) so `bm25()` can weight them. The weights are positional, matching the column order:

```rust
/// FTS5 retrieval: AND-matched candidates, ranked by per-column bm25 weights.
pub fn search(&self, query: &str, limit: u32) -> Result<Vec<MessageId>> {
    let mut stmt = self.conn.prepare_cached(
        "SELECT rowid FROM mail_fts
         WHERE mail_fts MATCH ?1
         ORDER BY bm25(mail_fts, 10.0, 5.0, 1.0, 0.2, 0.05)
         LIMIT ?2",
    )?;
    let ids = stmt
        .query_map(params![query, limit], |row| row.get(0).map(MessageId::new))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(ids)
}
```

- `bm25()` returns lower-is-better (negative) scores, so `ORDER BY` ascending is correct.
- Multi-word queries are AND by default. A subject hit outranks a body hit, and a signature hit barely counts.
- **Split retrieval from ranking**: FTS5 returns the top few hundred AND-matched candidates; Rust reranks them with app signals (sender importance, recency, replied-to, thread activity).

### Never pass raw user input to MATCH

The right side of `MATCH` is FTS5 query syntax. Raw input misbehaves in three ways (all verified):

- `follow-up` is a **syntax error** (`-` is an operator).
- `from: bob` **errors** because `from` isn't a column; `subject: x` **silently becomes a column filter** because `subject` is one.
- `Q3 OR nothing` **silently becomes an OR query**.

Quote every term:

```rust
/// Turns user input into an FTS5 query: every term quoted (so `-`, `:`, `AND`, and `"` are literal),
/// joined by spaces (implicit AND), with prefix matching on the last term for search-as-you-type.
#[must_use]
pub fn fts_query(input: &str) -> String {
    let terms: Vec<String> = input.split_whitespace().map(|t| format!("\"{}\"", t.replace('"', "\"\""))).collect();
    match terms.split_last() {
        Some((last, rest)) if !rest.is_empty() => format!("{} {last}*", rest.join(" ")),
        Some((last, _)) => format!("{last}*"),
        None => String::new(),
    }
}
```

If the app supports operators (`from:alice`, `has:attachment`), parse them yourself into SQL predicates plus an FTS5 query; don't hand them to FTS5.

## Coordinating processes through SQLite

When several front ends run at once (the GUI, the TUI, the CLI, an MCP server), the shared database is the coordination channel. No sockets, no local servers. Two pieces do the work, both verified across real processes:

**1. An outbox table.** Any process applies a user action locally and queues the server-side work in the same transaction. The UI updates immediately, the action survives crashes, and offline actions replay when the network returns, so the outbox doubles as the local-first offline queue.

```rust
pub const SCHEMA: &str = "
    CREATE TABLE IF NOT EXISTS message (
        id INTEGER PRIMARY KEY,
        subject TEXT NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS outbox (
        id INTEGER PRIMARY KEY,
        action TEXT NOT NULL,          -- 'archive', 'send', ...
        message_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at INTEGER NOT NULL DEFAULT (unixepoch())
    );
    CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (id) WHERE status = 'pending';
";

/// Any process: change local state and queue the server-side action in one transaction.
/// The UI updates immediately; the sync owner replays the action against the server later.
pub fn archive(conn: &mut Connection, id: MessageId) -> Result<i64> {
    let tx = conn.transaction()?;
    tx.execute("UPDATE message SET archived = 1 WHERE id = ?1", [id.get()])?;
    tx.execute("INSERT INTO outbox (action, message_id) VALUES ('archive', ?1)", [id.get()])?;
    let outbox_id = tx.last_insert_rowid();
    tx.commit()?;
    Ok(outbox_id)
}

/// Sync owner only: the oldest pending action, if any.
pub fn next_pending(conn: &Connection) -> Result<Option<OutboxItem>> {
    Ok(conn
        .prepare_cached(
            "SELECT id, action, message_id, attempts FROM outbox WHERE status = 'pending' ORDER BY id LIMIT 1",
        )?
        .query_row([], |row| {
            Ok(OutboxItem { id: row.get(0)?, action: row.get(1)?, message_id: row.get(2)?, attempts: row.get(3)? })
        })
        .optional()?)
}

/// Sync owner only: record the outcome. Failures retry until `max_attempts`, then stay failed for the UI to show.
pub fn complete(conn: &Connection, item: &OutboxItem, outcome: Result<(), String>, max_attempts: i64) -> Result<()> {
    match outcome {
        Ok(()) => conn.execute("UPDATE outbox SET status = 'done', error = NULL WHERE id = ?1", [item.id])?,
        Err(message) => {
            let status = if item.attempts + 1 >= max_attempts { "failed" } else { "pending" };
            conn.execute(
                "UPDATE outbox SET status = ?2, attempts = attempts + 1, error = ?3 WHERE id = ?1",
                params![item.id, status, message],
            )?
        }
    };
    Ok(())
}
```

- Only the sync owner reads the outbox and talks to the server, so there is no claim/lock dance between consumers.
- A process that needs the result (did the send go out?) watches its outbox row's `status`.
- Server actions must be idempotent or checked before replay: archiving twice is harmless; sending twice is not, so `send` generates and stores the `Message-ID` header before the first attempt, and a retry first checks the Sent folder for that id.
- Prune `done` rows periodically, keeping recent ones for an activity view.

**2. `PRAGMA data_version` for change detection.** Its value changes whenever *another* connection, in any process, commits to the file. Reading it costs microseconds. Every process polls it every 100 to 250 ms and re-reads its view when it changes:

```rust
/// Detects commits made by *other* connections (other processes included) without sockets or file watchers.
/// `PRAGMA data_version` changes when another connection commits; it doesn't change for this connection's own writes.
#[derive(Debug)]
pub struct ChangeWatcher {
    conn: Connection,
    last: i64,
}

impl ChangeWatcher {
    pub fn new(conn: Connection) -> Result<Self> {
        let last = data_version(&conn)?;
        Ok(Self { conn, last })
    }

    /// Cheap enough to call every 100 to 250 ms. Returns true once per batch of outside commits.
    pub fn changed(&mut self) -> Result<bool> {
        let now = data_version(&self.conn)?;
        let changed = now != self.last;
        self.last = now;
        Ok(changed)
    }
}

fn data_version(conn: &Connection) -> Result<i64> {
    Ok(conn.pragma_query_value(None, "data_version", |row| row.get(0))?)
}
```

- The watcher needs its **own connection**: `data_version` ignores that connection's own commits by design, so a process notices its own writes directly and other processes' writes through the watcher.
- It says *something* changed, not what. Keep a cheap way to find out (an `updated_at` or a monotonically increasing `change_seq` column) so a view re-reads only what moved.
- The sync owner uses the same watcher to wake up when another process adds to the outbox.
- Poll from the watcher's own thread (or `spawn_blocking`), not a tokio worker, like any rusqlite call.

## Derived data in its own file

Vectors, caches, and anything else that can be recomputed go in a separate database file, `ATTACH`ed to the main connection:

```rust
conn.execute("ATTACH DATABASE ?1 AS vec", [vectors_path.to_string_lossy()])?;
conn.execute_batch(
    "CREATE TABLE IF NOT EXISTS vec.embedding (message_id INTEGER PRIMARY KEY, model TEXT NOT NULL, content_hash BLOB NOT NULL, vector BLOB NOT NULL)",
)?;
```

- Change the embedding model: delete the file and re-embed. Nothing irreplaceable lives there.
- Store the model name and a content hash per row to detect stale vectors.
- In WAL mode a transaction that touches both files is atomic per file, not across both. That's acceptable only because the derived file is rebuildable.
- `sqlite-vec` must be **statically linked** against the same bundled SQLite (iOS won't load extensions at runtime). Its flat (brute-force) search is stable; its ANN indexes were alpha as of 2026. Re-check its status before depending on an index.

## Review checklist

- [ ] No SQL built with `format!` from values; FTS queries go through an escaping helper.
- [ ] No rusqlite call on a tokio worker thread.
- [ ] Each process writes through its single writer connection, with short transactions.
- [ ] Only the sync owner talks to the server; other processes queue work in the outbox.
- [ ] Cross-process changes are noticed through `data_version` polling, not sockets or file watchers.
- [ ] New schema is a new migration; no shipped migration edited.
- [ ] Multi-statement writes are in a short transaction with no network I/O inside.
- [ ] Nothing irreplaceable is stored in a derived-data file.
- [ ] No other SQLite library opens the same file in the same process.
