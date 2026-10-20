# TUI and CLI front ends

The Rust-native front ends: a Ratatui terminal UI and a clap CLI for scripts and agents. They import the core directly (no FFI). Read this before reviewing terminal UI or command-line code.

Apply Rule #1 first: match the project's existing front-end structure.

## The TUI is just another shell

The TUI follows the same contract as SwiftUI or Compose: send events to the app layer, draw the view model, run platform effects. It has no business logic of its own. If a TUI change needs new logic, the logic goes in the core and every shell gets it.

That makes the TUI the cheapest way to dogfood the core: no Xcode, no FFI, no packaging, and it runs everywhere the core runs.

## Stack

- **Ratatui** (immediate mode: redraws the whole frame from state each time, which maps directly onto a view model) with **crossterm** (macOS, Linux, Windows).
- `crossterm` with the `event-stream` feature for async input. Depend on the same crossterm version Ratatui uses (check `Cargo.lock`) so there is one copy.
- Widgets worth knowing: `ratatui-textarea` (in-app editing), `tui-tree-widget` (folder trees), `tui-scrollview` (long bodies), `ratatui-image` (Kitty/Sixel/iTerm2 images with fallback), `nucleo` (Helix's fuzzy matcher for palettes and jumps), `html2text` (HTML to styled text).

## Terminal lifecycle

```rust
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let (_engine_tx, updates) = tokio::sync::mpsc::channel(64);
    let terminal = ratatui::init(); // raw mode, alternate screen, and a panic hook that restores the terminal
    let result = run::run(terminal, updates).await;
    ratatui::restore();
    result
}
```

- **`ratatui::init()` installs a panic hook** that restores the terminal. Without it, a panic leaves the user's shell in raw mode.
- **Always `restore()` on every exit path**, including errors (above, the result is captured first, then restored, then returned).
- For simple synchronous tools, `ratatui::run(|terminal| { ... })` wraps init and restore for you.

## The event loop

Terminal input and engine updates both become app events, and the screen redraws after each:

```rust
/// The TUI loop: terminal input and engine updates both become app events; redraw after each.
pub(crate) async fn run(
    mut terminal: DefaultTerminal,
    mut updates: mpsc::Receiver<EngineUpdate>,
) -> anyhow::Result<()> {
    let mut app = AppState::default();
    let mut input = EventStream::new();
    loop {
        let view = app.view();
        terminal.draw(|frame| crate::render(frame, &view))?;

        let event = tokio::select! {
            Some(update) = updates.recv() => match update {
                EngineUpdate::InboxChanged(items) => Event::InboxLoaded(items),
            },
            Some(term) = input.next() => match term? {
                TermEvent::Key(key) if key.kind == KeyEventKind::Press => match key_to_event(key) {
                    KeyAction::Quit => return Ok(()),
                    KeyAction::Compose => {
                        let _draft = compose_in_editor(&mut terminal)?;
                        continue;
                    }
                    KeyAction::App(event) => event,
                    KeyAction::Ignore => continue,
                },
                _ => continue,
            },
            else => return Ok(()),
        };
        let _effects = app.update(event); // hand effects to the engine here
    }
}
```

- **Filter `KeyEventKind::Press`.** On Windows crossterm reports both press and release; without the filter every key fires twice.
- **Render is a pure function of the view model** (`fn render(frame: &mut Frame, view: &InboxView)`), which is what makes `TestBackend` snapshot tests possible (see `testing.md`).
- **Keys come from the command registry**, not a hard-coded `match`, so the TUI, GUI, palette, and CLI agree (see `architecture.md`). The `match` in a `key_to_event` function is only acceptable as a stand-in.
- Don't do slow work in the loop. Effects go to the engine; results come back through `updates`.

## Compose in `$EDITOR`

Suspend the TUI, run the user's editor on a temp file, and resume:

```rust
/// Suspends the TUI, opens `$EDITOR` on a temp file, and resumes when the editor exits.
fn compose_in_editor(terminal: &mut DefaultTerminal) -> anyhow::Result<String> {
    let file = tempfile::Builder::new().suffix(".md").tempfile()?;
    let editor = std::env::var("VISUAL").or_else(|_| std::env::var("EDITOR")).unwrap_or_else(|_| "vi".into());

    ratatui::restore();
    let status = Command::new(&editor).arg(file.path()).status().with_context(|| format!("launching {editor}"));
    *terminal = ratatui::init();
    terminal.clear()?;

    anyhow::ensure!(status?.success(), "{editor} exited with an error");
    Ok(std::fs::read_to_string(file.path())?)
}
```

- Restore the terminal **before** the `?` on the editor's status, so a failed launch still brings the TUI back.
- `$VISUAL` takes precedence over `$EDITOR`. A `.md` suffix gives the editor Markdown highlighting; send Markdown as multipart HTML plus plain text (`comrak` or `pulldown-cmark`).
- `EDITOR` may contain arguments (`code --wait`). Split it with `shell-words` if you support that.

## Terminal-specific rendering

- **HTML mail is the hard part.** Most commercial mail is table-layout HTML; `html2text` makes it readable, not pretty. Always offer a one-key "open in browser" escape hatch.
- **Emit OSC 8 hyperlinks** so links are clickable in iTerm2, Kitty, WezTerm, and Windows Terminal, and show the real target URL (see `security.md`).
- Respect `NO_COLOR` and degrade on terminals without true color.

## The CLI

One binary, subcommands, the TUI as the default when no subcommand is given:

```rust
#[derive(Debug, Parser)]
#[command(name = "letterkey", version, about = "Keyboard-first email")]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Search all accounts.
    Search {
        query: String,
        #[arg(long, default_value_t = 20)]
        limit: u32,
        /// Emit JSON on stdout for scripts and agents.
        #[arg(long)]
        json: bool,
    },
    /// Run the push service.
    Serve {
        #[arg(long, default_value = "127.0.0.1:8080")]
        bind: String,
    },
}
```

```rust
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Logs go to stderr so stdout stays clean for --json output.
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .with_writer(std::io::stderr)
        .init();

    match Cli::parse().command {
        None => println!("(the TUI would start here)"),
        Some(Command::Search { query, limit, json }) => {
            let hits = vec![SearchHit { id: 1, subject: format!("{query} ({limit})") }];
            let mut out = std::io::stdout().lock();
            if json {
                serde_json::to_writer(&mut out, &hits)?;
                writeln!(out)?;
            } else {
                for hit in &hits {
                    writeln!(out, "{}\t{}", hit.id, hit.subject)?;
                }
            }
        }
        Some(Command::Serve { bind }) => serve(&bind).await?,
    }
    Ok(())
}
```

Rules for CLI output:

- **stdout is data, stderr is everything else** (logs, progress, prompts). `app search x --json | jq` must never see a log line.
- **`--json` on every command that returns data**, with a stable shape (serde structs, not ad hoc `json!` maps). The JSON shape is an API; agents and scripts depend on it.
- **Human output is line-oriented and greppable** (tab-separated when tabular).
- **Exit codes**: 0 success, 1 failure, 2 usage error (clap's default). Document any others.
- **Lock stdout once** (`stdout().lock()`) for loops that write many lines.
- **Destructive commands confirm or require `--yes`**; agents should get `draft` rather than `send` (see `security.md`).
- Generate shell completions and a man page from the clap definition (`clap_complete`, `clap_mangen`) instead of writing them by hand.

## Review checklist

- [ ] The TUI holds no business logic; it sends events and draws view models.
- [ ] Terminal is restored on every exit path, including panics and editor launches.
- [ ] Key presses are filtered to `KeyEventKind::Press`.
- [ ] Keybindings come from the command registry.
- [ ] CLI writes data to stdout only, logs to stderr, and offers `--json` with a stable shape.
- [ ] Render functions are pure and covered by `TestBackend` snapshots.
