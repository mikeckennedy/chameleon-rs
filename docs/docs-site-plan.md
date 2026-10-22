# Docs site plan (Great Docs)

The plan for chameleon-rs's user documentation, built with Great Docs the way
chameleon_partials, chameleon-flask, fastapi-chameleon and chameleon-robyn already are. It
covers the docs item in `docs/vision.md` §7 and the docs part of Phase 12 in `docs/plan.md`.

## 1. Pipeline: copy chameleon_partials

Every new piece below has a working counterpart in `../chameleon_partials`.

| Piece | chameleon_partials | chameleon-rs |
|---|---|---|
| Config | `great-docs.yml` at the repo root | Same file, adapted (§3) |
| Generator | great-docs 0.13.0, Quarto CLI on PATH (`/usr/local/bin/quarto`) | Same pins, as a dev dependency only, never a runtime one |
| Build dir | `great-docs/`, git-ignored | Same |
| Published site | `docs/`, committed; nginx serves it at a subpath | `site/`, committed (see §2) |
| Build script | `scripts/build_docs.py`: build, mirror `_site/`, then two post-process fixes for great-docs 0.13.0 bugs (dead `search.json` entries, missing canonical links) | Copy it unchanged except `DEST` |
| Preview | `scripts/serve_docs.py`, serving at the production subpath on :8099 | Copy it, with prefix `/docs/chameleon-rs` and port 8098 so both can run at once |
| URL | `https://mkennedy.codes/docs/chameleon-partials/` | `https://mkennedy.codes/docs/chameleon-rs/` |
| Analytics | Umami snippet in `include_in_header` | Same snippet and website id (same domain) |

Add `make docs` (build the extension, then run `build_docs.py`) and `make docs-serve`.

Add a CI job that builds the docs on every push, using `quarto-dev/quarto-actions/setup`, but
does not deploy them. Publishing stays manual: git push, then nginx serves the files, as on
the other sites. The job catches a broken build and broken code examples before release.

## 2. Decision needed: the `docs/` folder clash

In this repo, `docs/` holds the design documents: `architecture.md`, `plan.md`, `vision.md`,
`spike-1.md` and this file. Michael's other projects publish the site to `docs/`. The two
can't share the folder, because `build_docs.py` deletes and replaces its target on every
build.

Options:

- **A. Publish the site to `site/`. (Recommended.)** Nothing existing moves. The only cost is
  that this site's nginx `alias` points at `.../chameleon-rust/site/` rather than `.../docs/`,
  a one-line difference in the server config.
- **B. Move the design docs to `design/` and publish to `docs/` as the other repos do.** This
  touches 18 files that cite `docs/plan.md`, `docs/architecture.md` and so on (CLAUDE.md, the
  README, Rust and Python comments, the tests' README), plus the agent memory notes. A
  mechanical sed, but churn for no gain except matching the other repos.

## 3. `great-docs.yml` differences from chameleon_partials

- **`module: chameleon_rs`.** The distribution is called `chameleon-rs`, so the import name
  can't be inferred.
- **`dynamic: true`.** Great Docs imports the package, so the docs build must run after
  `make build` and on Python 3.12 or later. The private `_native` module and the internal
  modules (`_bridge`, `_rewrite`, `_settings`, `codegen`, `shadow`, and so on) stay out of
  the reference.
- **`parser: google`.** The docstrings today are prose without `Args:` or `Returns:`
  sections, so a docstring pass is part of the work (§6).
- **`repo`, `source`, `pypi`, `mcp: {enabled: false}`, `authors`, `jupyter: python3`.** As
  in chameleon_partials.
- **`skill`.** An agent skill with `extra_body`, `gotchas`, `best_practices` and a
  `decision_table`. Gotchas worth listing:
  - `compat.install()` must run before anything imports `chameleon`.
  - Partials search the calling template's folder by default; use
    `partial_search='registered'` for the original lookup.
  - `render_partial` is a builtin in every template.
  - The known differences from Chameleon (D11).
  - Async and offload are opt-in, never the default.

### API reference sections

Group the reference by task, listing only names in `__all__`:

1. **Templates:** `PageTemplate`, `PageTemplateFile`, `PageTextTemplate`,
   `PageTextTemplateFile`, `PageTemplateLoader`.
2. **Configuration:** `configure`, `core_version`, `__version__`.
3. **Partials:** `render_partial`, `extend_model`, `register_extensions`, `PartialRenderer`,
   `HTML`, `clear`, `PartialsException`.
4. **Flask and Quart:** `chameleon_rs.flask`. Add `chameleon_rs.fastapi` and
   `chameleon_rs.robyn` here when they exist.
5. **Async:** whatever Phase 11 adds (`render_async` and the helpers).
6. **Errors and web helpers:** `TemplateError`, and `ErrorPage`, `NotFound` and `model_of`
   from `chameleon_rs.web`.
7. **Compatibility:** `chameleon_rs.compat.install`.

## 4. User guide (`user_guide/*.qmd`, ordered)

1. **Getting started.** Install, render a first template, then use a template folder with
   `configure()`.
2. **Switching from Chameleon.** The one-line `compat.install()` route, and its
   `integrations=False` form.
3. **Template language.** chameleon-rs uses Chameleon's language unchanged (TAL, METAL, i18n,
   `${}`), so this page is short and links to Chameleon's own reference.
   - Option: adapt Chameleon's `reference.rst` (1,775 lines) into this site later. Chameleon's
     license, which chameleon-rs already carries, allows it with attribution.
4. **Partials.** `render_partial`, the caller-folder search and `partial_search`, and
   `extend_model`.
5. **Flask and Quart.** `@template`, `render`/`response`, error pages, `configure`.
6. **FastAPI and Robyn.** Once those integrations exist.
7. **Async rendering.** `render_async`, what can suspend, fan-out, the event-loop rationale
   (Granian single-threaded + asyncio), and the offload switch if Phase 11 keeps it.
8. **Free-threaded Python.** 3.14t support and what has been stress-tested.
9. **Configuration reference.** Every `configure()` option and its default.
10. **Compatibility.** What is identical, the D11 differences, and how it is measured
    (upstream suite 233/233, byte-identical crawl).
11. **Performance.** The benchmark numbers, the method behind each, and how to reproduce
    them with `bench/`. Real-app figures are aggregate only (see §7).
12. **Migrating from chameleon_partials, chameleon-flask and fastapi-chameleon.** A table
    mapping each old call to its new one, and the pitfalls.

**Recipes (`recipes/`):** HTMX fragments with `render_partial`, testing templates with
pytest, rendering email bodies, and sharing a layout macro.

Also generated: a changelog page from a new `CHANGELOG.md` (there isn't one yet; Phase 12
needs it anyway) and the license page.

## 5. Keeping the examples honest

Write the examples as executable Quarto cells (`jupyter: python3`), so every build runs them
against the built extension, and the CI docs job fails when an example breaks. Where a page
needs a server (Flask, Quart, FastAPI), show code in plain blocks, and add a pytest that
pulls those blocks out and runs them against a test client.

## 6. Work order

1. **Scaffolding.** Make the §2 decision, then add `great-docs.yml`, the scripts, the make
   targets, the `.gitignore` entries, the pyproject `Documentation` URL and a stub landing
   page. Build and preview at the subpath.
2. **Docstring pass.** Google-style `Args`, `Returns`, `Raises` and examples on every
   exported name. Do this after Phase 11 merges, because Phase 11 edits the same Python
   files.
3. **User guide pages 1–5 and 9–10.** These describe stable features and can be written now,
   in parallel with Phase 11, since they live in new files.
4. **Pages 7–8.** After Phase 11 merges.
5. **Page 11 and the README.** Use the final release numbers.
6. **Page 12 and the recipes.**
7. **The skill block and `skill_extra_body.md`.**
8. **CI docs job, first publish, and the server's nginx alias.** The alias is Michael's
   step.
9. **Logo and favicon.** When the logo exists (vision §8).

Steps 1 and 3 can start now, in a worktree, without touching Phase 11's files.

## 7. Privacy rule

The docs are public. Nothing from the Talk Python apps goes into them: no template content,
names, routes, paths or URL counts that would identify them. Real-app performance appears
only as aggregate ratios ("a production Quart site: renders 2.2x faster, +30% requests per
second"), the same rule as `docs/plan.md`.

## 8. Decisions (Michael, 2026-10-21)

- **§2:** publish to `site/`. The design docs stay in `docs/`.
- **URL slug:** `https://mkennedy.codes/docs/chameleon-rs/`.
- **Template language:** adapt Chameleon's reference into the site now (`language/`).
- **Start time:** now, in the `docs-site` worktree, without touching Phase 11's files.

## 9. Status (2026-10-21)

Done:

- Step 1 (scaffolding): `great-docs.yml`, `scripts/build_docs.py` and `serve_docs.py`, `make
  docs` / `make docs-serve`, the `docs` dependency group, the `.gitignore` entry, the
  pyproject `Documentation` URL, and the CI `docs` job (builds only).
- Step 3, plus pages 11 and 12: the landing page (`index.qmd`, so it doesn't depend on the
  README), Getting started, Switching from Chameleon (with shadow mode), Template language,
  Partials, Configuration, Flask and Quart, Compatibility, Performance, Migrating.
- The language reference: Chameleon's `reference.rst` adapted into seven pages in
  `language/`. Every example was checked on both engines, which found 11 errors in the
  original reference (fixed in the pages).
- Recipes: HTMX fragments, testing templates, email bodies, a shared layout.
- Step 7: the skill block and `skill_extra_body.md`.

Changes from the plan:

- `dynamic: false`. Runtime introspection reports a cyclic alias for `render_partial` (a
  `PartialRenderer` instance whose class is named `function`) and falls back to static
  analysis anyway.
- The Flask examples run as executable cells through Flask's test client, so no separate
  pytest is needed to keep them honest (§5).
- Quarto 1.9.38 (the version the partials site was built with) is installed from the release
  tarball into `~/.local/share/quarto-1.9.38`, linked from `~/.local/bin/quarto`. The
  Homebrew cask needs sudo.

Left:

- Step 2 (docstring pass) and pages 7–8 (async, free-threading): after Phase 11 merges. The
  Flask page's "renders are synchronous for now" note changes then too.
- Logo and favicon (Michael has graphics).
- A `CHANGELOG.md` and GitHub releases, for the changelog page (Phase 12).
- The nginx alias for `site/` (Michael's step) and the first publish.
