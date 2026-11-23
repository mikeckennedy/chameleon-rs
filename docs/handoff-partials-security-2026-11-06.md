# Handoff: porting chameleon-rs's partials security fixes to chameleon_partials and jinja_partials

Date: 2026-11-06. Source: the three chameleon-rs security reviews (releases 0.1.48 to 0.1.56, see `CHANGELOG.md` and `great-docs/_site/user-guide/security.md`). Each issue below was re-checked today against the actual code of the two target libraries on disk, with a probe script, so "present" and "not affected" are observed results, not guesses.

Targets checked: `chameleon_partials` 0.2.1 at `/Users/michaelkennedy/github/mk/chameleon_partials` (commit a3a95fd, the one chameleon-rs pins as its reference) and `jinja_partials` at `/Users/michaelkennedy/github/mk/jinja_partials` (commit 99253d9, Jinja2 3.1.6, Flask 3.1.3).

## Summary

| # | Issue (chameleon-rs release that fixed it) | chameleon_partials 0.2.1 | jinja_partials main | Priority |
|---|---|---|---|---|
| 1 | Template names from requests load files outside the folder (0.1.48) | **Bypass confirmed**: leading whitespace defeats the 0.2.1 check | Not affected: Jinja's loader refuses these | High (chameleon_partials) |
| 2 | Every spelling of a name compiles and caches its own template, without bound (0.1.48, 0.1.49) | **Present**: 8 registry entries for one file after 8 spellings | Not affected: Jinja's LRU holds 400 | Medium (chameleon_partials) |
| 3 | A directory name raises `IsADirectoryError` instead of "not found" (0.1.49) | **Present** | Not affected | Low (chameleon_partials) |
| 4 | Runaway partial recursion must end in `RecursionError`, never a crash or a wedged worker (0.1.49, 0.1.51) | Not re-checked; pure-Python recursion through Chameleon should raise `RecursionError` | **Present** in async mode: each nested render is a new OS thread with no depth limit, so a partial cycle spawns threads until the OS refuses | Medium (jinja_partials) |
| 5 | The `_template_name` hook chameleon-rs already calls on chameleon_partials when present | Missing (chameleon-rs falls back to its own check) | n/a | Do with #1 and #2 |

Not applicable to either library, for the record: the upward sibling search hang (chameleon-rs searches the caller's folders; these libraries search one registered folder), inode-0 and hard-link confusion (only matters if you adopt file-identity caching, see #2), the native stack crashes, the Markdown bounds, and builtins replaced by render data (an engine behaviour; see "Documentation" at the end).

## chameleon_partials

### 1. Leading whitespace bypasses `_in_folder` (high)

`render_partial` checks the name with `_in_folder(template_file, template_path)` and then passes the unchanged name to Chameleon's `PageTemplateLoader`. Chameleon's `TemplateLoader.load` (`chameleon/loader.py`) does `spec = spec.strip()` before joining it to the search path. So a name that starts with whitespace passes the lexical check (`' ..'` is an ordinary directory name, so `folder/' ..'/secret.pt` is "inside") and the loader then strips it to `../secret.pt` and reads the file outside the folder.

Observed today, with `templates/partials/card.pt` registered and `secret.pt` one level above the folder:

```
'../secret.pt'               ValueError: Template not found: ../secret.pt.
' ../secret.pt'              RENDERED: SECRET OUTSIDE
'\t../secret.pt'             RENDERED: SECRET OUTSIDE
'\n../secret.pt'             RENDERED: SECRET OUTSIDE
'../secret.pt '              ValueError: Template not found: ../secret.pt .
'/etc/hosts'                 ValueError: Template not found: /etc/hosts.
'os:__init__.py'             ValueError: Template not found: os:__init__.py.
```

Trailing whitespace happens to be refused because `abspath` still normalizes the `..` away; only leading whitespace (space, tab, newline) is a hole. chameleon-rs is not affected: its `_find` strips first (`python/chameleon_rs/partials.py:238`), and its stricter `outside_folder` guard refuses surrounding whitespace outright (`python/chameleon_rs/_views.py:283`).

Fix: strip before checking, and load the same stripped name you checked, so the check and the loader see one string:

```python
def render_partial(template_file, **template_data):
    ...
    name = _template_name(template_file, template_path)   # see #5
    if name is None:
        raise ValueError(f'Template not found: {template_file}.')
    page = __templates[name]
```

where `_template_name` does `spec = template_file.strip()` first, then the existing `_in_folder` logic on `spec`. Keep the NUL and `:` refusals. Note `_in_folder` is lexical by design: a symlink placed inside the folder is still followed, as Jinja's `FileSystemLoader` does; keep that documented.

Tests to add to `tests/test_rendering.py` (extend the `test_names_outside_the_folder_are_not_found` parametrization): `' ../test_rendering.py'`, `'\t../test_rendering.py'`, `'\n../test_rendering.py'`, `'render/ ../../test_rendering.py'`. Also assert that `' render/bare.pt'` (leading space, a real file) still renders, since the loader strips it, so the fix must not refuse harmless whitespace unless you choose to.

### 2. Unbounded template cache from name spellings (medium)

Chameleon's loader caches by the raw name it was given (`registry[args]` in the `cache` decorator in `chameleon/loader.py`), so `shared/card.pt`, `./shared/card.pt`, `shared//card.pt`, `shared/./card.pt`, `shared/../shared/card.pt`, `' shared/card.pt'` and, on macOS and Windows, `SHARED/CARD.PT` each compile and keep their own `PageTemplate` for the life of the process. Observed today: 8 spellings of one file left 8 registry entries. With `render_partial(f'cards/{slug}.pt')` and a request-chosen `slug`, memory grows without bound (chameleon-rs measured 3,000 spellings at 360 to 406 MB).

Fix, the shape chameleon-rs uses (`python/chameleon_rs/partials.py:230-330`):

- Canonical spelling: `posixpath.normpath(spec)` after stripping (`_spelling`, line 230). That folds `./`, `//`, `/./` and an inside `..` into one key. Do this before the `_in_folder` check and pass the canonical name to the loader.
- One template per file: `os.stat` the joined path, require `stat.S_ISREG` (this also fixes #3), and remember the first spelling seen per file identity in a bounded dict (chameleon-rs: 1,024 entries), then call `__templates[first_spelling]`. See `_file_id` (268), `stat_identity` (278) and `remembered_spelling` (298). Identity is `(st_dev, st_ino)`, falling back to the case-folded resolved path when `st_ino` is 0 (FAT, exFAT, some network mounts report 0 for every file, the 0.1.56 bug), and including the resolved path when `st_nlink == 1` (some FUSE and overlay mounts repeat inodes).
- If you adopt identity caching with `auto_reload=True`, keep the 0.1.51 check too: before reusing a remembered spelling, confirm it still names the same identity (`current(first) == identity` in `remembered_spelling`), because a deployment that renames a new file over one of two hard-linked names would otherwise serve the wrong content.

A simpler, acceptable alternative if you do not want file identity: canonical spelling only, plus `os.path.normcase` on the key, and a documented note that distinct case spellings on case-insensitive filesystems still cache separately. That closes the `./`, `//` and `..` growth, which is the part a request can drive with unlimited variety.

Tests to port, from chameleon-rs `tests/python/test_partials.py`: `test_every_spelling_of_a_partial_is_one_template` (line 652: five spellings, assert `len(loader.registry) == 1`) and `test_case_spellings_of_a_partial_are_one_template` (line 727, with its `_case_insensitive` probe that skips on case-sensitive filesystems).

### 3. A directory name raises `IsADirectoryError` (low)

Observed today: `render_partial('partials')` and `render_partial('partials/')` raise `IsADirectoryError` from inside Chameleon, which `except ValueError` handlers written for "Template not found" miss. The regular-file check in #2 fixes this; otherwise add `os.path.isfile` and raise the same `ValueError('Template not found: ...')`. Test to port: `test_a_folder_name_is_not_a_template` (chameleon-rs line 705: `sub`, `sub/`, `.`, `./`).

### 5. Add the `_template_name` hook chameleon-rs already looks for

When chameleon-rs runs a template that was handed the original `chameleon_partials.render_partial` function in its model (through `chameleon_rs.compat.install()`), it renders the partial as a native frame instead of calling the function, and resolves the name through the original package's own check so the check is never bypassed. `_legacy_target` in `python/chameleon_rs/partials.py:460` does:

```python
name_of = module.get('_template_name')
if callable(name_of):
    name = name_of(template_file, folder)      # str to pass to __templates[...], or None
else:
    name = template_file if _in_folder(template_file.strip(), folder) else None
```

So define, in `chameleon_partials/__init__.py`, a module-level `_template_name(template_file: str, folder: str) -> str | None` that returns the canonical spelling to load (after strip, normpath, containment, regular-file and first-spelling logic from #1 to #3) or `None` when the name is refused or missing, and make `render_partial` use it. That gives one code path for both engines.

After releasing chameleon_partials: in chameleon-rs bump `PARTIALS_COMMIT` in the `Makefile` (line 12), run `make reference` then `make test-integration` (the chameleon_partials suite runs unmodified on chameleon_rs) and `venv/bin/pytest tests/python/test_partials.py -k "outside_the_registered_folder or compat_api"`, which exercises exactly this hook.

## jinja_partials

### Names from requests: not affected, but pin it with tests

Observed today through `register_environment`, Flask's `render_template`, and (same `env.get_template` path) the FastAPI, Starlette and Quart renderers: `../secret.html`, `' ../secret.html'`, `'\t../secret.html'`, `partials/ ../../secret.html`, `/etc/hosts`, an absolute path to a real file, `partials\..\..\secret.html`, `partials`, `partials/` and `''` all raise `TemplateNotFound`. Jinja's `split_template_path` refuses `..` segments and any `os.sep`/`os.altsep` inside a segment, drops empty segments (so a leading `/` is harmless), and `FileSystemLoader` requires a regular file. The name cache is Jinja's LRU (default 400 entries), so spellings cannot grow it.

Two things worth doing anyway:

- Add a parametrized regression test (names above, each `register_*` entry point) so a future loader or renderer change cannot regress silently. chameleon-rs's `tests/python/test_strict_names.py` has the list of shapes.
- `generate_render_partial(renderer)` hands the name to the caller's `renderer` untouched. Say in its docstring and in the user guide that a custom renderer is responsible for refusing names that leave its folder, and that the built-in ones get this from Jinja.

### 4. Nested async renders have no depth limit (medium)

`_render_async_blocking` runs a nested partial on a fresh `threading.Thread` (the deadlock fix in 78d264b). Each thread has its own event loop and its own Python recursion counter, so a partial that includes itself, or a cycle `a.html` to `b.html` to `a.html`, never reaches `RecursionError`. Observed today with `enable_async=True` and `partials/self.html` containing `{{ render_partial("partials/self.html", n=n+1) }}`: with `Thread.start` capped at 60 for safety, the render failed with the cap's error after 61 threads in 0.03 s; uncapped it would keep creating threads (each with a stack) until the OS refuses or memory runs out, while the pool workers for the outer renders stay blocked. The threads did unwind cleanly once the inner one failed (2 live threads afterwards), so this is a crash or stall, not a leak. The trigger is template-authored, not request data, which is why it is medium.

Fix: carry the nesting depth into the nested thread and raise `RecursionError` past a limit:

```python
MAX_NESTED_RENDERS = 100  # module constant; each level is a thread and an event loop

def _render_async_blocking(executor, template, data):
    depth = getattr(_render_thread_state, 'depth', 0)
    if not getattr(_render_thread_state, 'in_render', False):
        return executor.submit(_run_async_render, template, data, 1).result()
    if depth >= MAX_NESTED_RENDERS:
        raise RecursionError(f'partials nested more than {MAX_NESTED_RENDERS} deep')
    ... thread target calls _run_async_render(template, data, depth + 1)
```

with `_run_async_render` setting `_render_thread_state.depth` alongside `in_render`. Tests: the self-including partial under `enable_async=True` raises `RecursionError` and `threading.active_count()` returns to its baseline; the same template on a sync environment also raises `RecursionError` (pure-Python recursion, expected but not probed today); a legitimate nesting of, say, 20 levels still renders.

## Documentation to carry over (both libraries)

The chameleon-rs Security page (`great-docs/_site/user-guide/security.md`) has two paragraphs that apply verbatim to the partials libraries: never pass a request's parameters straight in as the model (`render_partial(**request.args)` lets data replace names the template relies on), and if a page or partial is chosen by the request, map the request value to a name you wrote (`PAGES = {'about': 'pages/about.pt'}`) rather than building a path from it. Neither library's guide says this today.

## Probe scripts

The probes that produced the observations above were run from this session's scratchpad and are not kept. Each is a dozen lines: register a `templates/` folder with `partials/card.*` inside and `secret.*` one level above, call the installed `render_partial` with each name, and print the result or the exception type. For the jinja_partials recursion probe, monkeypatch `threading.Thread.start` to raise after 60 starts before rendering the self-including partial.
