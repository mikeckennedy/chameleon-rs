//! `render_async` (D9): a render that suspends on awaitable values.
//!
//! [`RenderAwaitable`] is what `PageTemplate.render_async` awaits (through an `async def`, since
//! `asyncio.create_task` takes only coroutines). It drives the resumable VM: when an
//! expression's final value is awaitable, the VM suspends and the render delegates to the
//! value's `__await__()` iterator as `yield from` does (`send`, `throw`, `close`), then resumes
//! with its result, or raises its error at the suspended instruction (so `tal:on-error` sees
//! it; cancellation is not an `Exception` and passes through).
//!
//! Between the calls that drive it, everything the render holds is kept without the GIL
//! lifetime: the VM state (`Py` values), and the render-wide state ([`SavedRender`]: `rcontext`,
//! the partial units, code-block namespaces, ...). Each step binds it again, runs, and saves it.

use std::sync::Arc;

use pyo3::exceptions::{PyBaseException, PyGeneratorExit, PyRuntimeError, PyStopIteration, PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple, PyType};
use pyo3::{PyTraverseError, PyVisit};

use chameleon_core::{Done, Output, VmError, VmState, run, throw};

use crate::deferred::{DeferKind, Deferred};
use crate::errors::{catch_panics, vm_error};
use crate::expr::{Cx, FrameCx, RenderCtx, SavedRender};
use crate::program::{Program, output_str, record_error, render_error};
use crate::runtime::PyRuntime;
use crate::scope::Scope;

/// What an asynchronous render leaves for the event loop's own frames when it computes its
/// nesting budget (see `PyRuntime::frame_budget`).
const ASYNC_STACK_RESERVE: usize = 64;

/// `CO_ITERABLE_COROUTINE`: a generator made awaitable by `@types.coroutine`.
const CO_ITERABLE_COROUTINE: u32 = 0x100;

static GENERATOR_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// The nesting budget of an asynchronous render: the recursion limit less a fixed reserve
/// (its frames are not on the interpreter's stack, and that stack is the event loop's).
pub(crate) fn async_frame_budget(py: Python<'_>) -> usize {
    let limit = py
        .import(intern!(py, "sys"))
        .and_then(|sys| sys.getattr(intern!(py, "getrecursionlimit"))?.call0()?.extract::<usize>());
    limit.map_or(chameleon_core::DEFAULT_FRAME_BUDGET, |l| l.saturating_sub(ASYNC_STACK_RESERVE))
}

/// Whether `await value` accepts `value`: its type defines `__await__` (the type's slot, not
/// an instance attribute), or it is a generator flagged as an iterable coroutine
/// (`@types.coroutine`). The common value types answer without a lookup.
pub(crate) fn is_awaitable(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    is_awaitable_with(value, None)
}

/// Types checked in a render, with whether they define `__await__` (a render asks for every
/// value; a type lookup costs ~50 ns). Not kept across a suspension.
pub(crate) type AwaitTypes<'py> = std::cell::RefCell<Vec<(Bound<'py, PyType>, bool)>>;

/// How many types [`AwaitTypes`] remembers.
const AWAIT_TYPES: usize = 16;

/// [`is_awaitable`], remembering the answer per type in `cache`.
pub(crate) fn is_awaitable_with<'py>(value: &Bound<'py, PyAny>, cache: Option<&AwaitTypes<'py>>) -> PyResult<bool> {
    if value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyList>()
        || value.is_exact_instance_of::<PyTuple>()
        || value.is_exact_instance_of::<PyDict>()
        || value.is_exact_instance_of::<PyBytes>()
    {
        return Ok(false);
    }
    let py = value.py();
    let ty = value.get_type();
    let known = cache.and_then(|c| c.borrow().iter().find(|(t, _)| t.is(&ty)).map(|(_, a)| *a));
    let awaits = if let Some(a) = known {
        a
    } else {
        let a = defines_await(&ty)?;
        if let Some(cache) = cache {
            let mut cache = cache.borrow_mut();
            if cache.len() < AWAIT_TYPES {
                cache.push((ty.clone(), a));
            }
        }
        a
    };
    if awaits {
        return Ok(true);
    }
    let generator = GENERATOR_TYPE.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import(intern!(py, "types"))?.getattr(intern!(py, "GeneratorType"))?.cast_into::<PyType>()?.unbind(),
        )
    })?;
    if ty.is(generator.bind(py)) {
        let flags: u32 = value.getattr(intern!(py, "gi_code"))?.getattr(intern!(py, "co_flags"))?.extract()?;
        return Ok(flags & CO_ITERABLE_COROUTINE != 0);
    }
    Ok(false)
}

/// Whether a class of the type's MRO defines `__await__` in its own namespace (what fills the
/// `am_await` slot `await` uses; `hasattr` on the type would also find a metaclass's).
fn defines_await(ty: &Bound<'_, PyType>) -> PyResult<bool> {
    let py = ty.py();
    for class in ty.getattr(intern!(py, "__mro__"))?.cast_into::<PyTuple>()?.iter() {
        if class.getattr(intern!(py, "__dict__"))?.contains(intern!(py, "__await__"))? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// The iterator `await value` delegates to: `type(value).__await__(value)`, or an iterable
/// coroutine itself.
fn await_iter<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let ty = value.get_type();
    let Ok(method) = ty.getattr(intern!(py, "__await__")) else { return Ok(value.clone()) };
    let iter = method.call1((value,))?;
    if !iter.hasattr(intern!(py, "__next__"))? {
        let name = iter.get_type().name()?;
        return Err(PyTypeError::new_err(format!("__await__() returned non-iterator of type '{name}'")));
    }
    Ok(iter)
}

/// A render in progress.
struct Running {
    st: VmState<Py<PyAny>>,
    render: Option<SavedRender>,
    program: Py<Program>,
    /// The render's `econtext`, for its error message.
    scope: Py<Scope>,
    /// The iterator of the value being awaited.
    inner: Option<Py<PyAny>>,
    started: bool,
    phase: Phase,
    /// A native partial's render started for a concurrent placeholder: the coroutines in its
    /// scope are the arguments its caller's expressions made for it, so a render that does not
    /// end with its result closes those never started (else each is warned about as never
    /// awaited when the page is cancelled or fails).
    owns_args: bool,
    /// The render returned its result.
    returned: bool,
}

/// What the awaited value is for.
enum Phase {
    /// The value the VM suspended on.
    Vm,
    /// The VM finished; the tasks started for placeholders (`crate::deferred`) are awaited
    /// until each is done or one fails (`asyncio.wait(..., return_when=FIRST_EXCEPTION)`, so a
    /// failure ends the render at once, not once the tasks before it in the document are
    /// done). `filled` holds the text of each entry done.
    Fill { entries: Vec<Deferred>, filled: Vec<Option<(Vec<u8>, bool)>> },
    /// The render failed (or was cancelled) with `error` while tasks it started were running:
    /// they were cancelled and are awaited (`asyncio.gather(..., return_exceptions=True)`), so
    /// their `finally` blocks have run when the error is raised, as a `TaskGroup` does. (The
    /// exception object, not a `PyErr`: `__traverse__` visits it, since its traceback's frames
    /// may refer back to this render.)
    Joining { error: Option<Py<PyBaseException>> },
}

/// What happened since the last step: the render starts, or the awaited value returned or
/// raised.
enum Event {
    Start,
    Awaited(Result<Py<PyAny>, PyErr>),
}

/// How a step is driven.
enum Input {
    Send(Py<PyAny>),
    Throw(PyErr),
}

/// An awaitable render (see the module docs). Not frozen: PyO3's borrow flag makes a second
/// call while one runs (from the render's own expressions, or another thread) the error a
/// running coroutine raises.
#[pyclass(module = "chameleon_rs._native")]
pub(crate) struct RenderAwaitable {
    running: Option<Box<Running>>,
}

impl RenderAwaitable {
    /// A render of `program` in `scope`, not started. `concurrent`: native partials called
    /// with an awaitable argument render concurrently (`crate::deferred`).
    pub(crate) fn start(
        program: &Bound<'_, Program>,
        scope: &Bound<'_, Scope>,
        defaults: Option<Bound<'_, PyDict>>,
        builtins: Option<Bound<'_, PyDict>>,
        concurrent: bool,
        owns_args: bool,
    ) -> PyResult<Self> {
        let py = program.py();
        let p = program.get();
        let builtins = builtins.unwrap_or_else(|| PyDict::new(py));
        let mut render = RenderCtx::new(p.marker.bind(py).clone(), defaults, builtins);
        render.awaits = true;
        render.concurrent = concurrent;
        render.nonce = crate::deferred::nonce();
        let target_language =
            Scope::lookup(scope, intern!(py, "target_language").as_any())?.filter(|t| !t.is_none()).map(Bound::unbind);
        let st = VmState::new(
            Arc::clone(&p.core),
            program.clone().into_any().unbind(),
            scope.clone().into_any().unbind(),
            target_language,
        );
        Ok(Self {
            running: Some(Box::new(Running {
                st,
                render: Some(render.save()),
                program: program.clone().unbind(),
                scope: scope.clone().unbind(),
                inner: None,
                started: false,
                phase: Phase::Vm,
                owns_args,
                returned: false,
            })),
        })
    }
}

/// What a step ended with: a value to yield to the event loop, or the render's end (its
/// result or error).
enum Outcome {
    Yield(Py<PyAny>),
    Return(Py<PyAny>),
    Raise(PyErr),
}

/// The error a render raises once the tasks it cancelled are done: its own, unless the join
/// itself was interrupted by something that is not an `Exception` (the render's task
/// cancelled, a `KeyboardInterrupt`): that passes through, with the render's error as its
/// context, so cancellation is never lost (`asyncio.timeout`, `TaskGroup` and a client
/// disconnect rely on the `CancelledError` coming back).
fn joined(py: Python<'_>, error: Option<Py<PyBaseException>>, event: Event) -> PyErr {
    let error = error.map(|e| PyErr::from_value(e.into_bound(py).into_any()));
    if let Event::Awaited(Err(interrupt)) = event
        && !interrupt.is_instance_of::<pyo3::exceptions::PyException>(py)
    {
        if let Some(error) = error
            && interrupt.value(py).getattr(intern!(py, "__context__")).is_ok_and(|c| c.is_none())
        {
            interrupt.set_context(py, Some(error));
        }
        return interrupt;
    }
    error.unwrap_or_else(|| PyRuntimeError::new_err("render error missing"))
}

fn is_stop_iteration(py: Python<'_>, e: &PyErr) -> bool {
    e.is_instance_of::<PyStopIteration>(py)
}

/// One delegated call on the awaited iterator: `Ok(y)` it yielded `y`, `Err(Ok(v))` it
/// returned `v`, `Err(Err(e))` it raised `e`.
fn delegate(py: Python<'_>, inner: &Bound<'_, PyAny>, input: Input) -> Result<Py<PyAny>, Result<Py<PyAny>, PyErr>> {
    let result = match input {
        Input::Send(v) if v.is_none(py) => inner.call_method0(intern!(py, "__next__")),
        Input::Send(v) => inner.call_method1(intern!(py, "send"), (v,)),
        Input::Throw(e) => {
            // PEP 380: `GeneratorExit` closes the delegate and is raised here; an iterator
            // without `throw` gets nothing.
            if e.is_instance_of::<PyGeneratorExit>(py) {
                if let Ok(close) = inner.getattr(intern!(py, "close")) {
                    close.call0().map_err(Err)?;
                }
                return Err(Err(e));
            }
            match inner.getattr(intern!(py, "throw")) {
                Ok(t) => t.call1((e.value(py),)),
                Err(missing) if missing.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => {
                    return Err(Err(e));
                }
                // As CPython: looking `throw` up failing otherwise is the error raised.
                Err(lookup) => return Err(Err(lookup)),
            }
        }
    };
    match result {
        Ok(y) => Ok(y.unbind()),
        Err(e) if is_stop_iteration(py, &e) => {
            let value = e.value(py).getattr(intern!(py, "value")).map_or_else(|_| py.None(), Bound::unbind);
            Err(Ok(value))
        }
        Err(e) => Err(Err(e)),
    }
}

impl Running {
    /// Drives the render until it yields to the event loop or ends.
    fn step(&mut self, py: Python<'_>, input: Input) -> Outcome {
        let mut event = if let Some(inner) = self.inner.take() {
            match delegate(py, inner.bind(py), input) {
                Ok(y) => {
                    self.inner = Some(inner);
                    return Outcome::Yield(y);
                }
                Err(result) => Event::Awaited(result),
            }
        } else {
            match input {
                Input::Send(v) if !self.started && !v.is_none(py) => {
                    return Outcome::Raise(PyTypeError::new_err(
                        "can't send non-None value to a just-started coroutine",
                    ));
                }
                // Thrown in before it started: the render never runs.
                Input::Throw(e) if !self.started => return Outcome::Raise(e),
                Input::Throw(e) => Event::Awaited(Err(e)),
                Input::Send(_) => Event::Start,
            }
        };
        loop {
            self.started = true;
            let next = match &mut self.phase {
                Phase::Vm => self.vm_event(py, event),
                Phase::Fill { .. } => self.fill_event(py, event),
                Phase::Joining { error } => Err(Outcome::Raise(joined(py, error.take(), event))),
            };
            let awaitable = match next {
                Ok(awaitable) => awaitable,
                Err(Outcome::Raise(error)) if self.has_deferred() => match self.begin_join(py, error) {
                    Ok(awaitable) => awaitable,
                    Err(outcome) => return outcome,
                },
                Err(outcome) => return outcome,
            };
            // `await awaitable`: delegate to its iterator (`__await__()` failing is an error
            // of the awaited value; a value already done returns at once).
            let iter = match await_iter(awaitable.bind(py)) {
                Ok(iter) => iter,
                Err(e) => {
                    event = Event::Awaited(Err(e));
                    continue;
                }
            };
            match delegate(py, &iter, Input::Send(py.None())) {
                Ok(y) => {
                    self.inner = Some(iter.unbind());
                    return Outcome::Yield(y);
                }
                Err(result) => event = Event::Awaited(result),
            }
        }
    }

    /// Runs the VM on an event: the next value to await, or the end.
    fn vm_event(&mut self, py: Python<'_>, event: Event) -> Result<Py<PyAny>, Outcome> {
        let result = match event {
            Event::Start => self.drive(py, None),
            Event::Awaited(Ok(value)) => match self.st.resume_with::<PyErr>(value) {
                Ok(()) => self.drive(py, None),
                Err(e) => Err(vm_error(py, e)),
            },
            Event::Awaited(Err(error)) => self.drive(py, Some(error)),
        };
        match result {
            Ok(Some(value)) => Ok(value),
            Ok(None) => self.begin_fill(py),
            Err(e) => Err(Outcome::Raise(e)),
        }
    }

    /// The VM finished: what to await for the placeholders' tasks, or the result.
    fn begin_fill(&mut self, py: Python<'_>) -> Result<Py<PyAny>, Outcome> {
        let entries = self.render.as_mut().map(|r| std::mem::take(&mut r.deferred)).unwrap_or_default();
        if entries.is_empty() {
            return Err(self.finish(py));
        }
        let filled = entries.iter().map(|_| None).collect();
        self.phase = Phase::Fill { entries, filled };
        self.fill_done(py)
    }

    /// What was awaited for the placeholders' tasks ended: what to await next, or the result.
    fn fill_event(&mut self, py: Python<'_>, event: Event) -> Result<Py<PyAny>, Outcome> {
        match event {
            Event::Start => Err(Outcome::Raise(PyRuntimeError::new_err("render restarted"))),
            // The wait itself interrupted (the render's task cancelled): raised as it is.
            Event::Awaited(Err(error)) if !error.is_instance_of::<pyo3::exceptions::PyException>(py) => {
                Err(Outcome::Raise(error))
            }
            // A task's own result or error: read from the task, with those of the others done.
            Event::Awaited(_) => self.fill_done(py),
        }
    }

    /// Takes the text of each task done, in document order (the first failed one's error ends
    /// the render), then returns what to await for the rest: the one task left, or
    /// `asyncio.wait(rest, return_when=FIRST_EXCEPTION)`. Once all are done, the result.
    fn fill_done(&mut self, py: Python<'_>) -> Result<Py<PyAny>, Outcome> {
        let count = match &self.phase {
            Phase::Fill { entries, .. } => entries.len(),
            _ => return Err(Outcome::Raise(PyRuntimeError::new_err("not filling placeholders"))),
        };
        let mut pending = Vec::new();
        for index in 0..count {
            let Phase::Fill { entries, filled } = &self.phase else { break };
            if filled[index].is_some() {
                continue;
            }
            let task = entries[index].task.bind(py).clone();
            let done = task.call_method0(intern!(py, "done")).and_then(|d| d.is_truthy());
            let text = match done {
                Ok(false) => {
                    pending.push(task);
                    continue;
                }
                Ok(true) => match task.call_method0(intern!(py, "result")) {
                    Ok(value) => self.convert(py, index, &value),
                    Err(error) => Err(error),
                },
                Err(error) => Err(error),
            };
            match text {
                Ok(text) => {
                    if let Phase::Fill { filled, .. } = &mut self.phase {
                        filled[index] = Some(text);
                    }
                }
                Err(error) => return Err(Outcome::Raise(self.fill_error(py, index, error))),
            }
        }
        match pending.len() {
            0 => {}
            1 => return Ok(pending.swap_remove(0).unbind()),
            _ => {
                return crate::deferred::wait_first_exception(py, pending).map(Bound::unbind).map_err(Outcome::Raise);
            }
        }
        let Phase::Fill { entries, filled } = &mut self.phase else {
            return Err(Outcome::Raise(PyRuntimeError::new_err("not filling placeholders")));
        };
        let mut filled: Vec<(u64, Vec<u8>, bool)> = entries
            .iter()
            .zip(std::mem::take(filled))
            .filter_map(|(entry, text)| text.map(|(bytes, surrogates)| (entry.id, bytes, surrogates)))
            .collect();
        let nonce = self.render.as_ref().map_or(0, SavedRender::nonce);
        if let Err(message) = crate::deferred::fill(&mut self.st.out, nonce, &mut filled) {
            return Err(Outcome::Raise(crate::errors::internal_error(py, message)));
        }
        self.phase = Phase::Vm;
        Err(self.finish(py))
    }

    /// A placeholder's text: a partial's output as it is, a value converted for its position
    /// in the frame context it was deferred from.
    fn convert(&mut self, py: Python<'_>, index: usize, value: &Bound<'_, PyAny>) -> PyResult<(Vec<u8>, bool)> {
        let Phase::Fill { entries, .. } = &self.phase else { return Err(PyRuntimeError::new_err("not filling")) };
        let Some(entry) = entries.get(index) else { return Err(PyRuntimeError::new_err("placeholder out of range")) };
        let site = match &entry.kind {
            DeferKind::Partial => return crate::deferred::text_bytes(value),
            DeferKind::Value(site) => site,
        };
        let saved = self.render.take().ok_or_else(|| PyRuntimeError::new_err("render state missing"))?;
        let render = RenderCtx::restore(saved, py);
        let frame = FrameCx {
            builtins: site.builtins.as_ref().map(|b| b.bind(py).clone()),
            unit: 0,
            target: None,
            marker: site.marker.bind(py).clone(),
            serial: site.serial,
            domain: site.domain.as_ref().map(|d| d.bind(py).clone()),
            context: site.context.as_ref().map(|c| c.bind(py).clone()),
            target_language: site.target_language.bind(py).clone(),
            helpers: site.helpers.as_ref().map(|h| Box::new(h.bind(py))),
        };
        let scope = site.scope.bind(py).clone();
        let mut out = Output::default();
        let result = crate::convert::emit(
            &Cx { scope: &scope, render: &render, frame: &frame },
            value,
            site.esc,
            site.stage,
            &mut out,
        );
        drop(frame);
        self.render = Some(render.save());
        result?;
        let surrogates = out.has_surrogates();
        Ok((out.as_bytes().to_vec(), surrogates))
    }

    /// The error a placeholder's task (or its conversion) raised, as the render raises it: a
    /// partial's is already its render's; a value's is recorded where the value was written.
    fn fill_error(&mut self, py: Python<'_>, index: usize, error: PyErr) -> PyErr {
        let Phase::Fill { entries, .. } = &self.phase else { return error };
        let Some(DeferKind::Value(site)) = entries.get(index).map(|e| &e.kind) else { return error };
        if !error.is_instance_of::<pyo3::exceptions::PyException>(py) {
            return error;
        }
        let Some(saved) = self.render.take() else { return error };
        let render = RenderCtx::restore(saved, py);
        let _ = record_error(py, render.rcontext(py), site.program.get(), site.token, &error);
        let error = render_error(py, error, self.scope.bind(py), &render);
        self.render = Some(render.save());
        error
    }

    /// The tasks started for placeholders and not yet awaited.
    fn take_deferred(&mut self) -> Vec<Deferred> {
        let mut entries = self.render.as_mut().map(|r| std::mem::take(&mut r.deferred)).unwrap_or_default();
        if let Phase::Fill { entries: fill, filled } = &mut self.phase {
            let rest = std::mem::take(fill);
            let done = std::mem::take(filled);
            entries.extend(rest.into_iter().zip(done).filter(|(_, text)| text.is_none()).map(|(entry, _)| entry));
        }
        entries
    }

    /// Cancels the tasks started for placeholders (the render was closed or dropped: nothing
    /// can be awaited any more).
    fn cancel_deferred(&mut self, py: Python<'_>) {
        crate::deferred::cancel_all(py, &self.take_deferred());
    }

    /// The render failed with `error`: cancels the tasks it started and returns what awaits
    /// them, after which `error` is raised (or, if they cannot be awaited, `error` at once).
    fn begin_join(&mut self, py: Python<'_>, error: PyErr) -> Result<Py<PyAny>, Outcome> {
        let entries = self.take_deferred();
        crate::deferred::cancel_all(py, &entries);
        match crate::deferred::join(py, &entries) {
            Ok(gather) => {
                self.phase = Phase::Joining { error: Some(error.into_value(py)) };
                Ok(gather.unbind())
            }
            Err(_) => Err(Outcome::Raise(error)),
        }
    }

    fn has_deferred(&self) -> bool {
        self.render.as_ref().is_some_and(|r| !r.deferred.is_empty())
            || matches!(&self.phase, Phase::Fill { entries, .. } if !entries.is_empty())
    }

    /// Runs the VM (or raises `error` at the suspended instruction): `Some(value)` suspended
    /// on `value`, `None` finished.
    fn drive(&mut self, py: Python<'_>, error: Option<PyErr>) -> PyResult<Option<Py<PyAny>>> {
        crate::errors::render_room()?;
        let saved = self.render.take().ok_or_else(|| PyRuntimeError::new_err("render state missing"))?;
        let render = RenderCtx::restore(saved, py);
        let program = self.program.bind(py).clone();
        let scope = self.scope.bind(py).clone();
        let result = catch_panics(py, || {
            let mut rt = PyRuntime::<true>::new(&render, program.clone(), scope.clone());
            let done = match error {
                Some(e) => throw(&mut self.st, &mut rt, e),
                None => run(&mut self.st, &mut rt),
            };
            match done {
                Ok(Done::Finished) => Ok(None),
                Ok(Done::Suspended(v)) => Ok(Some(v)),
                // Cancellation and other non-`Exception`s pass through unformatted.
                Err(VmError::Runtime(e)) if !e.is_instance_of::<pyo3::exceptions::PyException>(py) => Err(e),
                Err(VmError::Runtime(e)) => Err(render_error(py, e, &scope, &render)),
                Err(e) => Err(vm_error(py, e)),
            }
        });
        self.render = Some(render.save());
        result
    }

    fn finish(&mut self, py: Python<'_>) -> Outcome {
        self.program.get().core.record_size(self.st.out.len());
        match output_str(py, &self.st.out) {
            Ok(s) => {
                self.returned = true;
                Outcome::Return(s.into_any().unbind())
            }
            Err(e) => Outcome::Raise(e),
        }
    }
}

impl Drop for Running {
    /// A render dropped before it ended leaves no task running (not attempted while the
    /// interpreter shuts down).
    fn drop(&mut self) {
        let close_args = self.owns_args && !self.returned;
        if self.has_deferred() || close_args {
            Python::try_attach(|py| {
                self.cancel_deferred(py);
                if close_args {
                    close_unstarted(py, self.scope.bind(py));
                }
            });
        }
    }
}

static CORO_STATE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Closes each coroutine in `scope`'s own dict that never started (`inspect.getcoroutinestate`
/// is `CORO_CREATED`), as dropping one the render would have awaited (see `Running::owns_args`).
fn close_unstarted(py: Python<'_>, scope: &Bound<'_, Scope>) {
    let Ok(state) = CORO_STATE.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import(intern!(py, "inspect"))?.getattr(intern!(py, "getcoroutinestate"))?.unbind())
    }) else {
        return;
    };
    // A list of the values (iterating the dict itself panics in PyO3 if it changes size).
    for value in scope.as_super().values().iter() {
        if !value.hasattr(intern!(py, "cr_frame")).unwrap_or(false) {
            continue;
        }
        let created = state.bind(py).call1((&value,)).and_then(|s| s.eq(intern!(py, "CORO_CREATED"))).unwrap_or(false);
        if created {
            let _ = value.call_method0(intern!(py, "close"));
        }
    }
}

#[pymethods]
impl RenderAwaitable {
    /// Itself, as a coroutine's: awaited by a second awaiter while the first is suspended in it
    /// is the error CPython raises (the two would otherwise drive one render by turns).
    fn __await__(slf: &Bound<'_, Self>) -> PyResult<Py<Self>> {
        if let Ok(this) = slf.try_borrow()
            && this.running.as_ref().is_some_and(|r| r.inner.is_some())
        {
            return Err(PyRuntimeError::new_err("coroutine is being awaited already"));
        }
        Ok(slf.clone().unbind())
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        step(slf, Input::Send(py.None()))
    }

    fn send(slf: &Bound<'_, Self>, value: Py<PyAny>) -> PyResult<Py<PyAny>> {
        step(slf, Input::Send(value))
    }

    /// `throw(exc)`, or the deprecated `throw(type, value=None, traceback=None)`.
    #[pyo3(signature = (typ, val = None, tb = None))]
    fn throw(
        slf: &Bound<'_, Self>,
        typ: &Bound<'_, PyAny>,
        val: Option<&Bound<'_, PyAny>>,
        tb: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let error = if typ.is_instance_of::<pyo3::exceptions::PyBaseException>() {
            if val.is_some_and(|v| !v.is_none()) {
                return Err(PyTypeError::new_err("instance exception may not have a separate value"));
            }
            PyErr::from_value(typ.clone())
        } else if let Ok(class) = typ.cast::<PyType>()
            && class.is_subclass_of::<pyo3::exceptions::PyBaseException>()?
        {
            let exc = match val {
                Some(v) if v.is_instance(class)? => v.clone(),
                Some(v) if v.is_none() => class.call0()?,
                Some(v) if v.is_instance_of::<PyTuple>() => class.call1(v.cast::<PyTuple>()?)?,
                Some(v) => class.call1((v,))?,
                None => class.call0()?,
            };
            PyErr::from_value(exc)
        } else {
            return Err(PyTypeError::new_err("exceptions must be classes or instances deriving from BaseException"));
        };
        if let Some(tb) = tb.filter(|t| !t.is_none()) {
            error.value(py).setattr(intern!(py, "__traceback__"), tb)?;
        }
        step(slf, Input::Throw(error))
    }

    /// Ends the render: closes the value being awaited, drops the render's state.
    fn close(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let mut this = slf.try_borrow_mut().map_err(|_| PyValueError::new_err("coroutine already executing"))?;
        let Some(mut running) = this.running.take() else { return Ok(()) };
        drop(this);
        running.cancel_deferred(py);
        if let Some(inner) = &running.inner
            && let Ok(close) = inner.bind(py).getattr(intern!(py, "close"))
        {
            close.call0()?;
        }
        Ok(())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        let Some(running) = &self.running else { return Ok(()) };
        let mut result = Ok(());
        running.st.for_each_value(|v| {
            if result.is_ok() {
                result = visit.call(v);
            }
        });
        result?;
        if let Some(render) = &running.render {
            render.traverse(&visit)?;
        }
        visit.call(&running.program)?;
        visit.call(&running.scope)?;
        if let Some(inner) = &running.inner {
            visit.call(inner)?;
        }
        match &running.phase {
            Phase::Fill { entries, .. } => {
                for entry in entries {
                    entry.traverse(&visit)?;
                }
            }
            Phase::Joining { error: Some(error) } => visit.call(error)?,
            Phase::Vm | Phase::Joining { error: None } => {}
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.running = None;
    }
}

/// One step of the render, as a coroutine's `send` / `throw` answers: the value to yield, or
/// `StopIteration(result)`, or the render's error.
fn step(slf: &Bound<'_, RenderAwaitable>, input: Input) -> PyResult<Py<PyAny>> {
    let py = slf.py();
    let mut this = slf.try_borrow_mut().map_err(|_| PyValueError::new_err("coroutine already executing"))?;
    let Some(running) = this.running.as_mut() else {
        return match input {
            Input::Throw(e) => Err(e),
            Input::Send(_) => Err(PyRuntimeError::new_err("cannot reuse already awaited coroutine")),
        };
    };
    // As a coroutine: the error, and the render can still be started.
    if !running.started
        && let Input::Send(v) = &input
        && !v.is_none(py)
    {
        return Err(PyTypeError::new_err("can't send non-None value to a just-started coroutine"));
    }
    // (The fill and join steps run outside `drive`'s own `catch_panics`.)
    let outcome = catch_panics(py, || Ok(running.step(py, input))).unwrap_or_else(Outcome::Raise);
    match outcome {
        Outcome::Yield(y) => Ok(y),
        Outcome::Return(value) => {
            this.running = None;
            Err(PyStopIteration::new_err((value,)))
        }
        Outcome::Raise(e) => {
            this.running = None;
            Err(stop_iteration_raised(py, e))
        }
    }
}

/// PEP 479, as a coroutine: a `StopIteration` raised by the render (an expression calling
/// `next()` on an exhausted iterator) must not end it as if it returned, so it becomes
/// `RuntimeError`, caused by it. (Raised out of `__next__`, the awaiting coroutine would take
/// it for the render's result, its args.)
fn stop_iteration_raised(py: Python<'_>, e: PyErr) -> PyErr {
    if !is_stop_iteration(py, &e) {
        return e;
    }
    let error = PyRuntimeError::new_err("coroutine raised StopIteration");
    error.set_cause(py, Some(e));
    error
}
