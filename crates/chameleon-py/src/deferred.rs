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
//! unit (not inside a native partial, so fan-out is one level deep). When the render ends, the
//! tasks are awaited in document order (they have been running meanwhile); an error ends the
//! render, and the other tasks are cancelled.

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
/// (the render's nonce and the entry's id), and the character again.
pub(crate) const PREFIX: &[u8] = "\u{10FFFD}".as_bytes();
pub(crate) const KEY_WIDTH: usize = 24;

static CONCURRENT_CALL: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static AWAIT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static GET_RUNNING_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static NONCES: AtomicU64 = AtomicU64::new(0x9e37_79b9_7f4a_7c15);

/// A per-render nonce for placeholder keys (distinct for every render in the process).
pub(crate) fn nonce() -> u64 {
    // A Weyl sequence mixed by a multiply: distinct values, none easy to guess from output.
    let n = NONCES.fetch_add(0x9e37_79b9_7f4a_7c15, Ordering::Relaxed);
    (n ^ (n >> 31)).wrapping_mul(0xbf58_476d_1ce4_e5b9)
}

pub(crate) fn placeholder(nonce: u64, id: u64) -> Vec<u8> {
    let mut p = Vec::with_capacity(PREFIX.len() * 2 + KEY_WIDTH);
    p.extend_from_slice(PREFIX);
    p.extend_from_slice(format!("{nonce:016x}{:08x}", id & 0xffff_ffff).as_bytes());
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
    Ok(value.get_type().is(class.bind(py)))
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

/// `loop.create_task(_await(awaitable))`.
pub(crate) fn start_task<'py>(
    event_loop: &Bound<'py, PyAny>,
    awaitable: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = event_loop.py();
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

/// Fills each placeholder of this render (`nonce`) with its text (`filled`, by id).
pub(crate) fn fill(out: &mut Output, nonce: u64, filled: &mut Vec<(u64, Vec<u8>, bool)>) {
    let mine = format!("{nonce:016x}");
    out.fill_placeholders(PREFIX, KEY_WIDTH, |key| {
        let key = std::str::from_utf8(key).ok()?;
        if key.get(..16)? != mine {
            return None;
        }
        let id = u64::from_str_radix(key.get(16..)?, 16).ok()?;
        let at = filled.iter().position(|(i, _, _)| *i == id)?;
        let (_, text, surrogates) = filled.swap_remove(at);
        Some((text, surrogates))
    });
}
