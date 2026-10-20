# Rust conventions

The line-level house style. Read this before any code review. Architecture lives in `architecture.md`; errors, async, FFI, and storage each have their own doc.

Apply Rule #1 first: match what the project already does.

## Workspace configuration

Starter files live in `templates/` (`Cargo.toml`, `clippy.toml`, `rustfmt.toml`), verified against Rust 1.99.

- **Edition 2024, resolver 3, MSRV pinned** in `[workspace.package]` (`rust-version`). Bump the MSRV deliberately, not by accident.
- **Shared versions in `[workspace.dependencies]`**; member crates write `tokio.workspace = true` and may add features (`tokio = { workspace = true, features = ["net"] }`).
- **Lints in `[workspace.lints]`**, and every crate opts in with `[lints] workspace = true`. A crate cannot combine `workspace = true` with its own overrides; one that needs different lints copies the table.
- **Release profile**: `lto = "thin"`, `codegen-units = 1`, `strip = "symbols"`. Keep `panic = "unwind"` in any crate loaded through FFI.

The house lint set:

```toml
[workspace.lints.rust]
unsafe_code = "forbid"
unreachable_pub = "warn"
missing_debug_implementations = "warn"

[workspace.lints.clippy]
pedantic = { level = "warn", priority = -1 }
unwrap_used = "deny"
dbg_macro = "deny"
todo = "warn"
module_name_repetitions = "allow"
missing_errors_doc = "allow"
```

- `pedantic` needs `priority = -1` so the individual entries override it.
- `clippy.toml` sets `allow-unwrap-in-tests` and `allow-expect-in-tests`, so tests stay readable.
- Allow a lint locally with `#[expect(clippy::lint_name, reason = "...")]` rather than `#[allow]`: `expect` warns when the exception is no longer needed.
- CI runs `cargo clippy --all-targets --all-features -- -D warnings`. Warnings are errors in CI, not at the desk.

## Formatting

`cargo fmt` with the template `rustfmt.toml` (`max_width = 120`, `use_small_heuristics = "Max"`). Don't hand-format. Options like `imports_granularity` and `group_imports` are nightly-only; don't add them to a stable project.

## Naming (Rust API Guidelines)

- Types and traits `UpperCamelCase`; functions, methods, modules, and variables `snake_case`; constants `SCREAMING_SNAKE_CASE`.
- Acronyms are words: `ImapUid`, `HttpClient`, `FfiError`, not `IMAPUID`.
- Conversions: `as_` is free and borrowed (`as_str`), `to_` is expensive or allocating (`to_string`), `into_` consumes self (`into_inner`).
- Getters drop `get_`: `fn subject(&self) -> &str`. Keep `get` for a lookup that can fail (`map.get(k)`), or when the plain name would be ambiguous (the newtype accessor `MessageId::get` in the example below).
- Iterators: `iter`, `iter_mut`, `into_iter`.
- Constructors: `new` for the primary one; `with_x`/`from_x` for variants; implement `From` for infallible conversions and `TryFrom` for fallible ones.
- Booleans read as predicates: `is_read`, `has_attachments`.

## Types: make invalid states unrepresentable

**Newtypes for identifiers.** A bare `i64` can be a local id, an IMAP UID, or a timestamp; the compiler can't tell. A newtype can.

```rust
/// Local primary key of a message row. Never confuse it with an IMAP UID.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct MessageId(i64);

impl MessageId {
    #[must_use]
    pub const fn new(raw: i64) -> Self {
        Self(raw)
    }

    #[must_use]
    pub const fn get(self) -> i64 {
        self.0
    }
}

/// An IMAP UID is only meaningful together with the folder's UIDVALIDITY.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ImapUid {
    pub uid_validity: u32,
    pub uid: u32,
}
```

- **Enums over flags.** `enum Signal { Red, Yellow, Green }`, not `is_bad: bool, is_warning: bool`. Clippy pedantic flags structs with more than three `bool`s for this reason.
- **Validate in constructors**, keep fields private, and expose accessors when an invariant exists (a non-empty address, a normalized domain).
- **`#[non_exhaustive]`** on public enums and structs that will grow, in crates other crates depend on. Not needed inside one binary.
- **Derive what's cheap and honest**: `Debug` always (`missing_debug_implementations` enforces it on public types), `Clone`, `PartialEq`/`Eq`, `Hash` when it's meaningful. `Copy` for small value types (ids, small enums). `Default` when there's a real default.

## Ownership and borrowing

- **Borrow in, own out.** Take `&str`, `&[T]`, `&Path`, `impl AsRef<Path>` when it helps callers; return owned `String`/`Vec<T>`.
- **Accept `String` only when you store it.** If the function keeps the value, take ownership and let the caller decide whether to clone.
- **Don't clone to silence the borrow checker.** Restructure first: split a struct so you borrow fields separately, collect ids and then mutate, or move the work into the owner. A clone that remains is fine but should be a choice, not a reflex.
- **Shared ownership**: `Arc<T>` for shared immutable data across threads; `Arc<str>`/`Arc<[T]>` for shared strings and slices. `Rc` only in single-threaded code, which in a tokio app is rare.
- **`Arc<Mutex<T>>` webs** spreading through the codebase usually want one owning task with a channel instead (see `async.md`).
- **`Cow<'_, str>`** when a function usually returns its input unchanged but sometimes must allocate (normalizing a header, stripping a signature).
- **Iterators over index loops**; `collect::<Result<Vec<_>, _>>()` to stop at the first error.

## Modules and visibility

- File-per-module: `engine.rs` plus `engine/`, not `engine/mod.rs`.
- `pub(crate)` by default; `pub` only for real API. `unreachable_pub` flags `pub` items nobody outside can reach.
- Re-export the crate's public surface from `lib.rs` so callers write `app_core::MessageId`, not a deep path.
- Avoid glob imports except `use super::*` in test modules and preludes you designed on purpose.
- Keep `main.rs` thin: parse args, set up logging, call into a library function that returns `anyhow::Result<()>`.

## Traits and generics

- **Traits at seams with more than one implementation** (real plus fake counts). A trait with one implementation and no fake is speculative; flag it.
- **Static dispatch first**: generics (`fn sync<T: MailTransport>(t: &T)`) or an enum over a closed set of variants. Use `dyn Trait` when the set is open, for heterogeneous collections, or to cut compile time.
- **Implement standard traits** instead of inventing methods: `Display` for user-facing text, `From`/`TryFrom`, `FromStr`, `IntoIterator`, `Default`.
- **`impl Trait` in argument position** for simple one-off generic parameters; named generics when the type appears more than once or callers need turbofish.

## Documentation and attributes

- `//!` crate and module docs say what the module is for and what it must not do ("pure: no I/O").
- `///` on every public item. Clippy pedantic wants backticks around code identifiers in docs (`` `UniFFI` ``).
- `#[must_use]` on pure functions and builders whose result is easy to drop by mistake (pedantic suggests where).
- Explain *why* in comments, not *what*.

## Dependencies

- Prefer well-maintained crates with small dependency trees. Before adding one, check maintenance, `unsafe` use, and license (cargo-deny enforces the last two, see `security.md`).
- Turn off default features you don't need (`default-features = false`).
- Features must be additive: enabling one must never break another.

## Build and verify

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo nextest run        # or: cargo test
cargo deny check
```

Run these after changes and report the real result. Check `CLAUDE.md` or a `justfile`/`xtask` for the project's actual commands before assuming.
