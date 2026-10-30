//! Opt-in concurrency in `render_async` (D9): values and native partials awaited concurrently,
//! with a placeholder in the output until the render ends.
//!
//! - A value of a `@chameleon_rs.concurrent` helper (`chameleon_rs._async.ConcurrentCall`)
//!   that the render would suspend on in an emitted position starts as a task; the placeholder
//!   is filled with its result, converted and escaped for that position.
//! - With `concurrent` set, a native partial called with an awaitable keyword argument renders
//!   as a render of its own, in a task; the placeholder is filled with its output.
//!
//! Only where the output written is final (`chameleon_core::VmState::can_defer`: no
//! `tal:on-error` body, translation or attribute capture open), and only in the render's own
//! unit (not inside a native partial, so fan-out is one level deep), and at most [`limit`]
//! tasks per render (past it, a call is awaited in place). When the render ends, the tasks
//! (running meanwhile) are awaited until all are done or one fails; an error ends the render,
//! and the other tasks are cancelled.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyString, PyType};
use pyo3::{PyTraverseError, PyVisit};

use chameleon_core::{Escape, Output, Stage};

use crate::program::Program;
use crate::scope::Scope;

/// Placeholders are this character (plane 16 private use), a key of [`KEY_WIDTH`] hex digits
/// (the render's 128-bit nonce and the entry's id), and the character again.
pub(crate) const PREFIX: &[u8] = "\u{10FFFD}".as_bytes();
pub(crate) const KEY_WIDTH: usize = 40;

static CONCURRENT_CALL: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static AWAIT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static GET_RUNNING_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static NONCES: AtomicU64 = AtomicU64::new(0);

/// A per-render nonce for placeholder keys: 128 random bits from the operating system. Text in
/// the output cannot be made to match a placeholder without knowing it (a predictable nonce
/// would let a value equal to a placeholder be replaced by deferred content). Never `0` (the
/// render's "none drawn yet").
pub(crate) fn nonce() -> u128 {
    let mut bytes = [0u8; 16];
    let nonce = match getrandom::fill(&mut bytes) {
        Ok(()) => u128::from_le_bytes(bytes),
        // Should the system fail to give any: SipHash with its own random keys (`RandomState`
        // is seeded from the operating system once per thread), not predictable either.
        Err(_) => hashed_nonce(),
    };
    nonce.max(1)
}

fn hashed_nonce() -> u128 {
    use std::hash::{BuildHasher, Hasher};
    let n = NONCES.fetch_add(1, Ordering::Relaxed);
    let half = || {
        let mut h = std::collections::hash_map::RandomState::new().build_hasher();
        h.write_u64(n);
        h.finish()
    };
    (u128::from(half()) << 64) | u128::from(half())
}

pub(crate) fn placeholder(nonce: u128, id: u64) -> Vec<u8> {
    let mut p = Vec::with_capacity(PREFIX.len() * 2 + KEY_WIDTH);
    p.extend_from_slice(PREFIX);
    p.extend_from_slice(format!("{nonce:032x}{:08x}", id & 0xffff_ffff).as_bytes());
    p.extend_from_slice(PREFIX);
    p
}

/// Whether `value` is a `@concurrent` helper's call.
pub(crate) fn is_concurrent_call(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = value.py();
    let class = CONCURRENT_CALL.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import(intern!(py, "chameleon_rs._async"))?
                .getattr(intern!(py, "ConcurrentCall"))?
                .cast_into::<PyType>()?
                .unbind(),
        )
    })?;
    Ok(crate::repeat::is_type(value, class.bind(py)))
}

/// The running event loop, if any (a render driven by hand has none: nothing is deferred).
pub(crate) fn running_loop(py: Python<'_>) -> Option<Bound<'_, PyAny>> {
    let get = GET_RUNNING_LOOP
        .get_or_try_init(py, || {
            Ok::<_, PyErr>(py.import(intern!(py, "asyncio"))?.getattr(intern!(py, "get_running_loop"))?.unbind())
        })
        .ok()?;
    get.bind(py).call0().ok()
}

static LIMIT: PyOnceLock<usize> = PyOnceLock::new();
static COROUTINE_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// How many tasks one render starts (`chameleon_rs.config.CONCURRENT_LIMIT`): past it, a call
/// is awaited where it stands, so the fan-out stays bounded however many calls the data makes.
/// Read once; an error reading it (the module failing to import, a value that is not a
/// count) is raised, and the next call tries again.
pub(crate) fn limit(py: Python<'_>) -> PyResult<usize> {
    LIMIT
        .get_or_try_init(py, || {
            py.import(intern!(py, "chameleon_rs.config"))?.getattr(intern!(py, "CONCURRENT_LIMIT"))?.extract::<usize>()
        })
        .copied()
}

/// `loop.create_task(...)` for `awaitable`: a `@concurrent` call's coroutine itself (so a task
/// cancelled before it starts closes the coroutine, rather than leaving it never awaited and
/// warned about), anything else through `_await`.
pub(crate) fn start_task<'py>(
    event_loop: &Bound<'py, PyAny>,
    awaitable: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = event_loop.py();
    if is_concurrent_call(awaitable)? {
        let inner = awaitable.getattr(intern!(py, "awaitable"))?;
        let coroutine = COROUTINE_TYPE.get_or_try_init(py, || {
            Ok::<_, PyErr>(
                py.import(intern!(py, "types"))?.getattr(intern!(py, "CoroutineType"))?.cast_into::<PyType>()?.unbind(),
            )
        })?;
        if crate::repeat::is_type(&inner, coroutine.bind(py)) {
            return event_loop.call_method1(intern!(py, "create_task"), (inner,));
        }
    }
    let wrap = AWAIT.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs._async"))?.getattr(intern!(py, "_await"))?.unbind())
    })?;
    let coroutine = wrap.bind(py).call1((awaitable,))?;
    event_loop.call_method1(intern!(py, "create_task"), (coroutine,))
}

/// Where a deferred value's text goes: what converting it needs (the frame's context when it
/// was deferred) and where its errors are reported.
pub(crate) struct ValueSite {
    pub(crate) esc: Escape,
    pub(crate) stage: Stage,
    pub(crate) scope: Py<Scope>,
    pub(crate) builtins: Option<Py<PyDict>>,
    pub(crate) marker: Py<PyAny>,
    pub(crate) serial: u64,
    pub(crate) domain: Option<Py<PyString>>,
    pub(crate) context: Option<Py<PyString>>,
    pub(crate) target_language: Py<PyAny>,
    pub(crate) helpers: Option<crate::expr::HelpersPy>,
    pub(crate) program: Py<Program>,
    pub(crate) token: u32,
}

pub(crate) enum DeferKind {
    /// A native partial's own render: its output is written as is.
    Partial,
    Value(Box<ValueSite>),
}

/// A task started by the render, and its placeholder's id.
pub(crate) struct Deferred {
    pub(crate) id: u64,
    pub(crate) task: Py<PyAny>,
    pub(crate) kind: DeferKind,
}

impl Deferred {
    pub(crate) fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.task)?;
        if let DeferKind::Value(site) = &self.kind {
            visit.call(&site.scope)?;
            if let Some(b) = &site.builtins {
                visit.call(b)?;
            }
            visit.call(&site.marker)?;
            if let Some(d) = &site.domain {
                visit.call(d)?;
            }
            if let Some(c) = &site.context {
                visit.call(c)?;
            }
            visit.call(&site.target_language)?;
            if let Some(h) = &site.helpers {
                h.traverse(visit)?;
            }
            visit.call(&site.program)?;
        }
        Ok(())
    }
}

/// Cancels the tasks (a render that failed or was cancelled leaves none running).
pub(crate) fn cancel_all(py: Python<'_>, entries: &[Deferred]) {
    for entry in entries {
        // A task already done, or whose loop is closed: nothing to do.
        let _ = entry.task.bind(py).call_method0(intern!(py, "cancel"));
    }
}

static GATHER: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// `asyncio.gather(*tasks, return_exceptions=True)`: what awaits cancelled tasks to their end.
pub(crate) fn join<'py>(py: Python<'py>, entries: &[Deferred]) -> PyResult<Bound<'py, PyAny>> {
    let gather = GATHER.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import(intern!(py, "asyncio"))?.getattr(intern!(py, "gather"))?.unbind())
    })?;
    let tasks = pyo3::types::PyTuple::new(py, entries.iter().map(|e| e.task.bind(py)))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item(intern!(py, "return_exceptions"), true)?;
    gather.bind(py).call(tasks, Some(&kwargs))
}

static WAIT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// `chameleon_rs._async._wait_failed(tasks)`: what wakes the render when every task is done,
/// or one failed or was cancelled (`asyncio.wait`'s `FIRST_EXCEPTION` waits on past a
/// cancelled one).
pub(crate) fn wait_first_failure<'py>(py: Python<'py>, tasks: Vec<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    let wait = WAIT.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs._async"))?.getattr(intern!(py, "_wait_failed"))?.unbind())
    })?;
    wait.bind(py).call1((pyo3::types::PyList::new(py, tasks)?,))
}

/// A partial render's output (a `str`) as UTF-8 bytes, and whether they hold lone surrogates.
pub(crate) fn text_bytes(value: &Bound<'_, PyAny>) -> PyResult<(Vec<u8>, bool)> {
    let s = value.cast::<PyString>()?;
    if let Ok(text) = s.to_str() {
        return Ok((text.as_bytes().to_vec(), false));
    }
    let py = value.py();
    let bytes = s.call_method1(intern!(py, "encode"), (intern!(py, "utf-8"), intern!(py, "surrogatepass")))?;
    Ok((bytes.extract::<Vec<u8>>()?, true))
}

/// Fills each placeholder of this render (`nonce`) with its text (`filled`, by id). Each
/// placeholder must occur exactly once: a second occurrence, or one left unfilled, is an
/// internal error rather than text put in the wrong place.
pub(crate) fn fill(out: &mut Output, nonce: u128, filled: &mut Vec<(u64, Vec<u8>, bool)>) -> Result<(), &'static str> {
    let mine = format!("{nonce:032x}");
    // By id: one lookup per placeholder (a search of the list made a page of many concurrent
    // calls, a `@concurrent` helper in a long `repeat`, quadratic).
    let mut texts: HashMap<u64, (Vec<u8>, bool)> =
        filled.drain(..).map(|(id, text, surrogates)| (id, (text, surrogates))).collect();
    let mut duplicate = false;
    out.fill_placeholders(PREFIX, KEY_WIDTH, |key| {
        let key = std::str::from_utf8(key).ok()?;
        if key.get(..32)? != mine {
            return None;
        }
        let id = u64::from_str_radix(key.get(32..)?, 16).ok()?;
        let Some(text) = texts.remove(&id) else {
            duplicate = true;
            return None;
        };
        Some(text)
    });
    if duplicate {
        return Err("a placeholder occurs more than once in the output");
    }
    if !texts.is_empty() {
        return Err("a placeholder is missing from the output");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn output(parts: &[&[u8]]) -> Output {
        let mut out = Output::default();
        for p in parts {
            out.push(p);
        }
        out
    }

    #[test]
    fn placeholders_fill_once_each() {
        let nonce = 0x1234_u128;
        let mut out = output(&[b"a", &placeholder(nonce, 0), b"b", &placeholder(nonce, 1), b"c", &placeholder(7, 0)]);
        let mut filled = vec![(1, b"ONE".to_vec(), false), (0, b"ZERO".to_vec(), false)];
        assert_eq!(fill(&mut out, nonce, &mut filled), Ok(()));
        let mut want = b"aZERObONEc".to_vec();
        want.extend_from_slice(&placeholder(7, 0));
        assert_eq!(out.as_bytes(), want);
    }

    #[test]
    fn a_duplicated_or_missing_placeholder_is_an_error() {
        let nonce = 99_u128;
        let mut out = output(&[&placeholder(nonce, 0), &placeholder(nonce, 0)]);
        let mut filled = vec![(0, b"X".to_vec(), false)];
        assert!(fill(&mut out, nonce, &mut filled).is_err());
        let mut out = output(&[b"nothing"]);
        let mut filled = vec![(0, b"X".to_vec(), false)];
        assert!(fill(&mut out, nonce, &mut filled).is_err());
    }

    #[test]
    fn many_placeholders_fill_in_linear_time() {
        // Once quadratic: each placeholder searched the list of filled texts.
        let nonce = 7_u128;
        let count = 100_000_u64;
        let mut out = Output::default();
        for id in 0..count {
            out.push(&placeholder(nonce, id));
        }
        let mut filled: Vec<_> = (0..count).rev().map(|id| (id, b"x".to_vec(), false)).collect();
        let start = std::time::Instant::now();
        assert_eq!(fill(&mut out, nonce, &mut filled), Ok(()));
        assert!(start.elapsed() < std::time::Duration::from_secs(10), "{:?}", start.elapsed());
        assert_eq!(out.as_bytes().len(), 100_000);
    }

    #[test]
    fn nonces_differ() {
        assert_ne!(nonce(), nonce());
    }
}
