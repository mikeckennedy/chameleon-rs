# Testing

How the layers are tested so the suite stays fast and trustworthy. Read this when reviewing tests or planning how a feature will be verified.

Apply Rule #1 first: match the project's existing test layout and tools.

## The pyramid, by layer

| Layer | Test style | Speed | Needs |
|---|---|---|---|
| App layer (pure) | Plain `#[test]`: events in, assert effects and view model | Microseconds | Nothing |
| Parsers (MIME, quotes, signatures, domains) | Fixture corpus + `insta` snapshots + `proptest` | Milliseconds | Fixture files |
| Engine | `#[tokio::test]` against fakes at the seams + temp SQLite | Milliseconds | `tempfile` |
| Storage | Temp or in-memory SQLite; migrations from old fixture DBs | Milliseconds | `tempfile` |
| TUI | Ratatui `TestBackend` + `insta` snapshot of the whole screen | Milliseconds | Nothing |
| FFI facade | Rust tests calling the exported API (it's a `lib` too) | Milliseconds | Nothing |
| Real protocols | A few `#[ignore]` tests against a local mail server (Stalwart, Dovecot in Docker) | Seconds | Opt-in |

Most tests should live in the top four rows. If a feature can only be tested end to end, logic is probably in the wrong layer.

## Layout

- **Unit tests in the module** under `#[cfg(test)] mod tests { use super::*; ... }`. They can reach private items.
- **Integration tests in `crates/<crate>/tests/`** exercise the public API only. Each file is a separate test binary, so group related tests rather than making dozens of files.
- **Fixtures in `crates/<crate>/tests/fixtures/`**, loaded with `include_str!`/`include_bytes!` or a path from `env!("CARGO_MANIFEST_DIR")`.
- **Run with `cargo nextest run`** (faster, per-test isolation, better output), falling back to `cargo test`. Doc tests need `cargo test --doc`, since nextest doesn't run them.

## The pure app layer

Because `update` and `view` do no I/O, tests are plain function calls:

```rust
#[test]
fn archive_removes_row_and_emits_effect() {
    let mut app = AppState::default();
    let _ = app.update(Event::InboxLoaded(vec![item(1, "a"), item(2, "b")]));
    let _ = app.update(Event::MoveDown);
    let effects = app.update(Event::ArchiveSelected);
    assert_eq!(effects, vec![Effect::Archive(MessageId::new(2)), Effect::Render]);
    assert_eq!(app.view(), InboxView { rows: vec!["a".into()], selected: Some(0) });
}
```

Every keyboard command should have tests at this level. They're the cheapest tests you'll ever write, and they cover behavior users see.

## Fakes at the seams, not mocks

Implement the seam trait with a small deterministic fake. Avoid mocking frameworks; a fake is ordinary code that compiles against the trait and stays honest when the trait changes.

```rust
/// A fake transport for engine tests: no sockets, deterministic.
#[derive(Debug, Default)]
pub struct FakeTransport {
    pub messages: Vec<RawMessage>,
}

impl MailTransport for FakeTransport {
    fn fetch_since(&self, _folder: &str, after_uid: u32) -> impl Future<Output = Result<Vec<RawMessage>>> + Send {
        std::future::ready(Ok(self.messages.iter().filter(|m| m.uid > after_uid).cloned().collect()))
    }
}

#[tokio::test]
async fn sync_advances_high_water_mark() {
    let fake = FakeTransport {
        messages: vec![RawMessage { uid: 5, bytes: vec![] }, RawMessage { uid: 9, bytes: vec![] }],
    };
    assert_eq!(sync_folder(&fake, "INBOX", 5).await.unwrap(), 9);
}
```

- Fakes can live in the crate behind `#[cfg(test)]`, or behind a `test-support` feature when other crates' tests need them.
- **Time**: if behavior depends on time (snooze, retry backoff, "domain registered last week"), pass time in as data or use `tokio::time::pause()` in tests. Never `sleep` real seconds in a test.
- **Asserting `Send`**: a test that `tokio::spawn`s the future fails to compile if a non-`Send` value sneaks across an await. That's a cheap guard for public async APIs.

## Databases

```rust
#[test]
fn migrations_are_idempotent_on_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("mail.db");
    Store::open(&path).unwrap();
    let store = Store::open(&path).unwrap();
    let version: i64 = store.conn.pragma_query_value(None, "user_version", |r| r.get(0)).unwrap();
    assert_eq!(usize::try_from(version).unwrap(), MIGRATIONS.len());
}
```

- In-memory (`Connection::open_in_memory`) for query logic; a `tempfile` directory when WAL, file locking, `ATTACH`, or reopen behavior matters (in-memory databases don't use WAL).
- **Keep a fixture database from each shipped schema version** and test that each migrates to current.
- Search ranking gets its own tests: insert a few messages and assert the order (subject hit above body hit, AND semantics).

## Snapshots with insta

`insta` stores expected output in `snapshots/*.snap` files that you review like code.

```rust
#[test]
fn inbox_renders() {
    let mut terminal = Terminal::new(TestBackend::new(30, 5)).unwrap();
    let view = InboxView { rows: vec!["Budget review".into(), "Lunch?".into()], selected: Some(1) };
    terminal.draw(|frame| render(frame, &view)).unwrap();
    insta::assert_snapshot!(terminal.backend());
}
```

produces:

```
"┌Inbox───────────────────────┐"
"│Budget review               │"
"│Lunch?                      │"
"│                            │"
"└────────────────────────────┘"
```

- Use snapshots for whole TUI screens, parser output (`assert_debug_snapshot!` of a parsed message), HTML-to-text conversion, and CLI `--json` output.
- Review changes with `cargo insta review`; never bulk-accept without looking.
- Snapshots must be deterministic: no timestamps, random ids, or `HashMap` iteration order in the output.

## Property tests with proptest

For parsers and anything that handles arbitrary input, state a property and let proptest search for a counterexample:

```rust
proptest::proptest! {
    /// Whatever the user types, the escaped query is valid FTS5 syntax.
    #[test]
    fn any_input_is_a_valid_fts_query(input in "\\PC{0,40}") {
        let store = Store::open_in_memory().unwrap();
        let query = fts_query(&input);
        if !query.is_empty() {
            proptest::prop_assert!(store.search(&query, 5).is_ok(), "query {query:?} failed");
        }
    }
}
```

Good properties: "never panics", "round-trips" (parse then render then parse), "output is a subset of input", "idempotent" (normalizing twice equals normalizing once). Commit the `proptest-regressions/` files it writes; they're the failing cases it found.

## Real-world fixture corpora

For an email app the parsers face the hardest input: quoted-reply detection, signature splitting, table-layout HTML, broken MIME. Build a corpus of real messages (newsletters, receipts, Outlook and Gmail threads, mailing-list digests), **scrubbed of personal data**, and snapshot the parser's output for each. Every bug found in the wild becomes a new fixture.

## Review checklist

- [ ] New app-layer behavior has pure `update`/`view` tests.
- [ ] Engine tests use fakes at seams, not network or real time.
- [ ] Parsers have fixture snapshots and at least one property test.
- [ ] Schema changes come with a migration test.
- [ ] Snapshots are deterministic and were reviewed, not bulk-accepted.
- [ ] No test sleeps real time or depends on test order.
