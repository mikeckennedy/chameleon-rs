# Security

An email client's attack surface is every byte a stranger can send you. Read this before reviewing anything that parses external input, renders content, handles credentials, exposes data to agents, or adds dependencies.

Apply Rule #1 first, but security rules are the last ones to relax for consistency's sake. If the project does something unsafe consistently, flag it.

## Treat all external input as hostile

Mail servers relay whatever attackers send. MIME structure, headers, encodings, HTML, attachment names, and message text are all untrusted.

- **Parse with a hardened library** (`mail-parser` from Stalwart) and never with ad hoc string splitting on headers.
- **Bound everything**: maximum message size to parse fully, MIME nesting depth, number of parts, decoded attachment size (watch for compression bombs), header line counts. Past a limit, store the raw bytes and show a degraded view; don't crash or hang.
- **Never panic on malformed input.** A message that crashes the parser crashes sync for that account, every time sync retries. Fuzz parsers (`cargo fuzz`) and property-test them (see `testing.md`).
- **Attachment filenames are attacker-controlled.** Strip path separators and `..`, normalize Unicode, and never write to a path built from a filename without validating it stays inside the target directory. Watch for right-to-left override characters (`U+202E`) that disguise `exe` as `pdf`.
- **Header values can contain CR/LF.** Never copy a received header into an outgoing message (Reply-To, Subject) without stripping line breaks, or you enable header injection.

## Rendering HTML mail

```rust
/// Sanitizes untrusted HTML mail for display in a web view.
/// Scripts, event handlers, forms, and iframes are removed by ammonia's defaults.
/// Remote images are dropped unless the user allowed them, so opening a message can't confirm it was read.
#[must_use]
pub fn sanitize_email_html(html: &str, allow_remote_images: bool) -> String {
    ammonia::Builder::default()
        .url_schemes(HashSet::from(["http", "https", "mailto", "cid"]))
        .link_rel(Some("noopener noreferrer nofollow"))
        .attribute_filter(move |element, attribute, value| {
            let remote = value.starts_with("http:") || value.starts_with("https:") || value.starts_with("//");
            if element == "img" && attribute == "src" && remote && !allow_remote_images {
                None
            } else {
                Some(Cow::Borrowed(value))
            }
        })
        .clean(html)
        .to_string()
}
```

Verified: this removes `<script>`, `onclick`, `javascript:` links, `<form>`, `<iframe>`, and remote tracking pixels, and keeps `cid:` inline images.

- **Sanitize in Rust, then defend again in the web view.** Sanitizing is necessary but not sufficient. The shell's web view (WKWebView, Android WebView, WebView2) must also disable JavaScript and apply a Content Security Policy (`default-src 'none'; img-src cid: data:; style-src 'unsafe-inline'`, adding remote image sources only when the user allows them). On Apple platforms, a `WKContentRuleList` can block network loads outright.
- **Inline CSS is a loophole.** ammonia drops `style` attributes by default, which breaks most commercial mail layouts. If you allow them back, CSS `url(...)` can load remote resources, so the CSP becomes the real control. Never allow `style` without the CSP.
- **Remote content is off by default.** Loading remote images confirms the message was opened and leaks IP address and time. Allow per sender, never globally by default.
- **Never fetch on open.** Any network lookup driven by message content (website check, domain age, BIMI logo) runs when the message *arrives*, for every message, from a cache keyed by domain. A fetch on open is a read receipt, since an attacker can register a unique domain per target.

## Links and senders

- **Show the real destination.** Link text can say `paypal.com` while the `href` goes elsewhere; display or warn on mismatch.
- **Punycode and homoglyphs**: convert domains with the `idna` crate and flag mixed-script domains (Latin plus Cyrillic `а`) and lookalikes (`paypa1.com`).
- **Reduce to the registrable domain** with the Public Suffix List (the `psl` or `publicsuffix` crates) before any per-domain check; `bounce.mail.company.com` belongs to `company.com`.
- **Surface authentication results** from the receiving server's `Authentication-Results` header (SPF, DKIM, DMARC). Only trust that header when your own provider added it (the topmost instance from a known `authserv-id`); attackers can include fake ones lower down.

## Prompt injection and agents

Email is the top prompt-injection vector: anyone can put instructions in a message an agent will read.

- **Agents draft; humans send.** MCP tools and CLI paths used by agents can create drafts and pending actions. Send, delete, move, forwarding rules, and contact changes require approval in the app UI. An agent's claim that the user approved doesn't count.
- **Return trust signals with content** so the agent can weigh "failed DMARC on a three-day-old domain" against the instructions in the body.
- **Never let message content choose tool arguments for privileged actions** without that human approval step.
- **Bound what agents can read**: pagination, body length caps, no bulk export tool.

## Secrets

- **Credentials live in the OS keychain**, reached through the shell (the `PlatformHost` trait, see `ffi.md`) on GUI platforms, or the `keyring` crate in the TUI and CLI. Never in SQLite, config files, or environment variables on a desktop.
- **Wrap secrets in `secrecy::SecretString`** so they can't be printed by `Debug` or logged by accident, and are zeroized on drop.
- **Never log** passwords, OAuth tokens, message bodies, or full addresses at info level. `#[tracing::instrument(skip(...))]` every function that takes one.
- **OAuth** (Gmail, Microsoft 365) uses the system browser with PKCE and a loopback or custom-scheme redirect, never an embedded web view that could capture the password. Request minimal scopes.

## Transport security

- **TLS everywhere, verification always on.** Use `rustls` with platform or webpki roots. There is no "accept invalid certificates" setting in production code, even behind a flag.
- **Prefer implicit TLS** (IMAPS 993, SMTPS 465). If STARTTLS is needed, require it: if the server doesn't offer it, fail rather than continue in plaintext (STARTTLS stripping).
- **Timeouts on every connection** (see `async.md`).

## `unsafe`

- The workspace sets `unsafe_code = "forbid"`, and UniFFI's generated code compiles under it, so the FFI crate needs no exception.
- A crate that truly needs `unsafe` (a hand-written FFI shim, a performance hot spot) gets its own `[lints]` table, a `// SAFETY:` comment on every `unsafe` block explaining why the invariants hold, and a dedicated review. Run its tests under Miri where possible (`cargo +nightly miri test`).
- Prefer a well-audited crate's `unsafe` to your own.

## Supply chain

- **`cargo-deny`** in CI checks advisories (RustSec), licenses (an allow-list), banned crates and duplicate versions, and that every crate comes from crates.io. A starter `deny.toml` is in `templates/`.
- **Pin the toolchain** for reproducible builds (`rust-toolchain.toml` with a specific stable version) and the MSRV with `rust-version`.
- **Commit `Cargo.lock`** for applications, and build with `--locked` in CI and releases.
- **Review new dependencies** before adding them: maintenance, `unsafe` usage, transitive dependency count, build scripts and proc macros (they run code on the build machine). `cargo tree -i <crate>` shows why something is in the graph.
- **Apple App Store**: no downloaded code that changes behavior (guideline 2.5.2), no JIT on iOS, and watch for crates that link private Apple symbols.

## Review checklist

- [ ] External input is parsed by a hardened parser with size and depth limits, and malformed input can't panic.
- [ ] HTML is sanitized in Rust and the web view disables JavaScript and applies a CSP; remote content is off by default.
- [ ] No network fetch is triggered by opening a message.
- [ ] Agent-facing paths can't send, delete, or move without human approval in the app.
- [ ] Secrets come from the keychain, are wrapped in `SecretString`, and never reach logs or SQLite.
- [ ] TLS verification is never disabled; STARTTLS is required, not optional.
- [ ] No new `unsafe` without a `SAFETY` comment and an explicit exception.
- [ ] New dependencies pass `cargo deny check`.
