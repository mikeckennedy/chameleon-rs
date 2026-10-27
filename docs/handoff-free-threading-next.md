# Handoff: free-threaded performance, what's left after 0.1.52

Written 2026-10-27 at the end of the session that made pages with partials scale on free-threaded
Python (commits `06fe702`, `8f0079e`; see CHANGELOG 0.1.52). The background is Kitewing's handoff,
`~/github/mk/kitewing/spike/handoffs/chameleon-rs-free-threading.md`, and our reply beside it,
`chameleon-rs-free-threading-results.md`.

## Where things stand

On macOS, 3.15.2t, six threads ÷ one thread with release wheels (Kitewing's `spike/research/scaling_probe.py`):

| Probe | 0.1.51 | 0.1.52 | Kitewing's bar |
|---|---|---|---|
| `page` (layout and 52 partials) | 2.4× | 3.7× | 3.75× |
| `partials` | 2.4× | 3.8× | about 3.9× |
| `flat_shared` (one template shared by every thread) | 3.8× | 4.3× (before `8f0079e`) | none |

`8f0079e` stopped interning the constants of templates made from strings (see "Related" below).
`flat_shared` is a string template, so it may have given back some of its gain; re-measure it.
Partials and loader templates are files, so they keep the interning.

A `sample` of six threads shows no lock waits from chameleon-rs. What's left is atomic reference
counting on objects every thread shares. On free-threaded CPython, an object's count is cheap only
for the thread that created it; every other thread's incref and decref is an atomic operation on a
shared counter, and those contend.

How to measure: build a release wheel into a scratch 3.15t venv
(`venv/bin/maturin build --release -i <venv python> -o <dir>`), then from the Kitewing repo run
`<venv>/bin/python spike/research/scaling_probe.py partials threads 3` (also `page` and
`flat_shared`). Interleave the old and new wheels, three runs each: another project loads this
machine, so a single run can be off by ±0.3×. Find hot spots with Kitewing's
`spike/research/partials_spin.py` under `sample <pid> 6`, then give self time to callers. The
session's script for that is easy to rewrite: parse the call tree, take each node's count minus its
children's, and credit `_Py_IncRef`, `_Py_DecRefShared` and `_Py_MergeZeroLocalRefcount` to the
nearest `_native` frame.

## 1. The per-call reference to the shared `Program`

`partial_step` (`crates/chameleon-py/src/partials.rs`) builds
`Call { code: target.program.clone().into_any().unbind(), .. }` for every partial call: one incref
of the shared `Program`, then a decref when the VM pops the frame (`chameleon_core::vm::Frame`
holds `code: Val<R>`). The benchmark page does that 52 times per render, from every thread.

Ideas:

- The VM frame's `code` is a runtime value, `Py<PyAny>`. Since `5aadc84`, `PyRuntime` keeps a
  per-render list of programs (`codes`, `set_code`). If a frame's code could be an index into a
  per-render table, a partial call would take no reference at all. That means a change to the core
  `Runtime` trait, or a cheap code handle type: a generic `Code` associated type, with `Py<PyAny>`
  for the old behavior. It touches every `Call` site (macros, `include`, partials) and
  suspension/`SavedRender`.
- A cheaper first step: have `Target` (per render) hold the program as `Py<PyAny>` already, and
  clone that. It's still an incref of the same shared object, so on its own it won't help.

Expected gain: of the same order as frame switching (`activate`) was before `5aadc84`, which was
the largest refcount source then. Measure before and after with `partials`.

## 2. Per-thread prepared partials

Handoff item 3 in Kitewing's document. Every thread renders the same `Target` objects: the
partial's `Program`, its `builtins` dict, its `defaults` dict and the default marker. Those were
created by whichever thread compiled or prepared them, so every other thread pays the atomic
refcount path on each use, which happens on many expression evaluations (builtin lookups go through
`cx.builtins()`).

What it would take: `Partials::prepare` calls `chameleon_rs.partials._prepare(template)` once per
template per render. A per-thread cache (`threading.local` in `partials.py`, or a Rust
`thread_local!` keyed by template id and generation) could give each thread its own `builtins`
and `defaults` copies, and its own `Program`, which would mean compiling per thread or a cheap
`Program` copy that shares the `Arc<chameleon_core::Program>`.

Cautions:

- `auto_reload` has to invalidate per-thread copies.
- Memory scales with threads × templates.
- Errors and `econtext` contents must stay identical (`completed_econtext`).

It's worth doing only if item 1 doesn't reach the bars. Kitewing's Linux VM numbers decide that.

## 3. Single-thread speed: free-threaded vs GIL build

Kitewing measured chameleon-rs about 18% slower single-threaded on 3.15t than on 3.15 (page 9,600
against 11,700 renders a second; flat 16,300 against 20,900), where plain Python shows no gap. Not
re-measured since 0.1.52. One-thread rates rose a few percent on 3.15t.

Plan:

- Render the same flat template with one thread on each build (same commit, release wheels), and
  `sample` both.
- Suspects:
  - uncontended critical sections on dict operations (every scope `set_item`/`get_item` takes the
    dict's per-object lock on free-threaded builds);
  - atomic increfs on objects the main thread created when a worker thread renders: compare
    rendering on the main thread with rendering on a worker thread;
  - PyO3's free-threaded code paths.
- The scope dict is per render and owned by its thread, so look for anything shared that is
  touched per operation.
- Done when the gap is within about 5%, or the remaining cost is understood and written down.

## Related, found by the 0.1.52 soak

- **Unique templates on free-threaded builds keep about 120 bytes each, forever.** A template
  compiled from a unique string, say one generated per request, leaves its string constants
  immortal on 3.15t, because CPython's parser interns them on free-threaded builds. 0.1.51 does this
  too, and so does stock Chameleon. The GIL build doesn't grow. It doesn't matter for templates read
  from files. It could be documented (don't generate template source per request; Litestar's
  `render_string` cache bounds its own), or avoided where we parse, though the constants come from
  `ast.parse` itself. Possibly worth a CPython issue.
- `8f0079e` limited our own interning to file templates for the same reason: it had doubled that
  growth for string templates.
