# Plan 01: report Chameleon bugs upstream

Building chameleon-rs turned up bugs in Chameleon itself. This plan gathers the ones worth
reporting to [malthe/chameleon](https://github.com/malthe/chameleon) and tracks them through
to an issue (and, where the fix is small, a pull request).

Scope for now: the four bugs below. Quirks we reproduce for parity (the shared `attrs` dict,
the namespace pop-once-per-end-tag, slot-body text dropped under `on-error` + i18n, late
`TypeError` from a non-`str` translation) are not in scope; they are behaviour changes, not
crashes, and would need a separate conversation with upstream. Add new bugs here as they are
found.

Both reproduce on the pinned reference (`temp/chameleon` at `90b9002`, Python 3.15.1,
macOS), checked 2026-10-23.

## Bug 1: `tal:on-error` + `tal:attributes` + a static attribute crashes the compiler

**Reproduction**

```python
from chameleon import PageTemplate
PageTemplate('<p a="1" tal:on-error="string:E" tal:attributes="x"></p>')()
# AttributeError: 'NoneType' object has no attribute '_fields'
```

Also `tal:attributes="a; v 1"`. Without the static attribute, or without the handler, it
renders fine.

**What happens.** The `on-error` fallback element keeps only the static attributes, together
with the filters that refer to the element's `tal:attributes` dictionaries, but the fallback
never evaluates those dictionaries. The compiler then puts `None` in the AST where the
dictionary lookup should be. Nothing notices until the generated module is unparsed
(`ast._Unparser.visit_Call` → `iter_fields`), after every expression in the template
compiled, so the user gets a bare `AttributeError` with no template position. If the
template has any other compile error anywhere, that error is reported instead, which hides
the bug.

**Expected.** The template compiles, and the fallback renders `<p a="1">E</p>` on error
(or, failing that, a `TemplateError` that points at the element).

**Found by:** the async render fuzzer (`tests/python/test_async.py`). Pinned on our side by
`FALLBACK_FILTER_ERRORS` in `tests/python/test_errors.py`. chameleon-rs reproduces the
error (`Resolver::late_error`, `crates/chameleon-core/src/resolve.rs`), so if upstream fixes
it we follow.

## Bug 2: a render error from a non-UTF-8 template file can't be turned into a string

**Reproduction**

```python
from pathlib import Path
from chameleon import PageTemplateFile

path = Path('latin.pt')
path.write_bytes('<?xml version="1.0" encoding="latin-1"?>\n<p>caf\xe9 ${missing}</p>'.encode('latin-1'))
try:
    PageTemplateFile(str(path))()
except NameError as e:
    str(e)  # UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 ...
```

**What happens.** The error formatters in `src/chameleon/exc.py` (`TemplateError.__str__`
near line 200, `ExceptionFormatter.__call__` near line 334) re-read the template with a bare
`open(filename)`. That uses the locale encoding, not the template's declared encoding, and
only `OSError` is caught; the `UnicodeDecodeError` comes later, while iterating the file. So
`str(exc)` raises, and logging or a framework's error page loses the original message.

**Expected.** Read the file with the template's encoding (or `errors='replace'`), and/or
catch `UnicodeDecodeError` and leave the source lines out.

**Worth checking before filing:** on Windows the locale encoding is usually cp1252, so a
plain UTF-8 template may show mojibake in the source excerpt, or hit this same crash on the
few bytes cp1252 leaves undefined. If so, the bug is wider than latin-1 files and the
report should say so.

**Found by:** the security/hardening review. Our fix and regression test:
`test_render_error_of_a_file_that_is_not_utf8_still_formats` in
`tests/python/test_hardening.py`.

## Steps

1. **Check upstream `master`.** Fetch the latest malthe/chameleon into a scratch clone (not
   `temp/chameleon`, which stays at the pin) and re-run both reproductions. Search open and
   closed issues for `_fields`, `on-error`, `UnicodeDecodeError` and `exc.py`. Drop or link
   anything already reported or fixed. (`90b9002` is the merge of #444, a Unicode decode fix:
   read it to make sure it isn't this one.)
2. **Write one standalone script per bug** that runs on a clean `pip install chameleon`, with
   the Chameleon and Python versions printed, and save them under `plans/upstream-bugs/`.
3. **Draft the issue text** for each: reproduction, actual vs expected, root cause as
   above, a suggested fix. No mention of chameleon-rs is needed beyond "found while testing".
4. **Michael reviews the drafts and files them** (or approves Claude filing them with `gh`).
   Filing is public, so nothing goes upstream without that go-ahead.
5. **Optional PRs.** Bug 2 is a few lines in `exc.py` with an obvious test; offer a PR.
   Bug 1 touches the compiler's fallback handling; offer one only if the maintainer wants it.
6. **Follow up.** Record issue/PR links here. When a fix is released, update the reference
   pin, and for bug 1 change chameleon-rs to match the fixed behaviour (remove
   `late_error` for this case, update `FALLBACK_FILTER_ERRORS`).

## Bug 3: `_resolve_dotted` treats `..x` like `.x`

`_resolve_dotted('..path', 'os.sep')` tries to import `os.sep.path` instead of returning `os.path`: the loop tests `name[0]` (the string, always `'.'`) where zope.dottedname tested the list Chameleon renamed to `name_parts`. Latent: nothing in Chameleon passes a base module. Our fix: `a16d473`.

## Bug 4: `ExpressionEvaluator` raises `ValueError` with no builtins

`ExpressionEvaluator(engine, {})` fails to unpack `zip(*builtins.items())`. Our fix: `1a9ab5f`.

## Status

Upstream `master` was still the pin (`90b9002`) on 2026-10-28; all four reproduce there (Python 3.13 and 3.15). Filed 2026-10-28; the PRs come from `mikeckennedy/chameleon` and add a CHANGES entry each (they touch the same lines, so the later merges need a trivial rebase). CI waits for the maintainer to approve workflows for a first-time contributor.

| Bug | Reproduced at pin | Checked on master | Issue | PR |
|-----|-------------------|-------------------|-------|----|
| 1: on-error + attributes `_fields` crash | yes (2026-10-23) | yes (2026-10-28) | [#449](https://github.com/malthe/chameleon/issues/449) | offered, not sent |
| 2: `str(exc)` fails on non-UTF-8 file | yes (2026-10-23) | yes (2026-10-28) | [#450](https://github.com/malthe/chameleon/issues/450) | [#453](https://github.com/malthe/chameleon/pull/453) |
| 3: `_resolve_dotted` multiple dots | yes (2026-10-28) | yes (2026-10-28) | [#451](https://github.com/malthe/chameleon/issues/451) | [#454](https://github.com/malthe/chameleon/pull/454) |
| 4: `ExpressionEvaluator` empty builtins | yes (2026-10-28) | yes (2026-10-28) | [#452](https://github.com/malthe/chameleon/issues/452) | [#455](https://github.com/malthe/chameleon/pull/455) |
