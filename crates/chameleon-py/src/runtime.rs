// The loop name handling follows Chameleon's src/chameleon/compiler.py (`visit_Repeat`,
// `_enter_assignment`, `_leave_assignment`), and the error handling its `visit_OnError` and
// `visit_Macro` (the `rcontext['__error__']` record), modified: implemented as a `Runtime`
// for the core VM.

//! The Python [`Runtime`] (D2): values are `Py<PyAny>`, errors are `PyErr` untouched.

use std::sync::Arc;

use pyo3::exceptions::{PyException, PyKeyError, PyNameError, PyRecursionError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyDict, PyInt, PyIterator, PyList, PySet, PyString, PyTuple};

use chameleon_core::surrogates::Wtf8Buf;
use chameleon_core::{
    Active, Attrs, Countdown, Emitted, ErrorSite, Errors, Escape, Eval, ExprId, Fill, I18n, Loops, MacroStep, Macros,
    Msgid, NameId, NameValue, Names, Output, Quote, Stage, Step, Translate, Translation, Values, Visibility,
};

use crate::convert;
use crate::expr::{Cx, FrameCx, Helpers, RenderCtx, eval, eval_emitted};
use crate::macros::{Macro, SlotFill};
use crate::program::{Program, record_error};
use crate::repeat::RepeatDict;
use crate::scope::Scope;

static ERROR_INFO: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DEQUE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Rebuilt for every render (and every resume of an asynchronous one): it holds `Bound`
/// references, the VM state holds only `Py`. `ASYNC`: an asynchronous render (`render_async`,
/// D9), a separate instantiation of the VM, so the synchronous one has no async code at all.
pub(crate) struct PyRuntime<'a, 'py, const ASYNC: bool = false> {
    py: Python<'py>,
    render: &'a RenderCtx<'py>,
    /// The programs this runtime's frames ran, each held once, and the active frame's (an
    /// index): switching frames takes no new reference to a template's objects, which on
    /// free-threaded Python is an atomic operation on a counter every thread rendering it
    /// shares.
    codes: Vec<Bound<'py, Program>>,
    code: usize,
    scope: Bound<'py, Scope>,
    frame: FrameCx<'py>,
}

impl<'a, 'py, const ASYNC: bool> PyRuntime<'a, 'py, ASYNC> {
    pub(crate) fn new(render: &'a RenderCtx<'py>, code: Bound<'py, Program>, scope: Bound<'py, Scope>) -> Self {
        let py = code.py();
        let frame = FrameCx::new(None, 0, py.None().into_bound(py), render.marker.clone());
        Self { py, render, codes: vec![code], code: 0, scope, frame }
    }

    /// The active frame's program.
    #[inline]
    fn program(&self) -> &Program {
        self.codes[self.code].get()
    }

    /// Makes `code` the active frame's program.
    #[inline]
    fn set_code(&mut self, code: &Bound<'py, Program>) {
        if self.codes[self.code].is(code) {
            return;
        }
        if let Some(i) = self.codes.iter().position(|c| c.is(code)) {
            self.code = i;
        } else {
            self.code = self.codes.len();
            self.codes.push(code.clone());
        }
    }

    /// [`Eval::eval`] in an asynchronous render: a final value that is awaitable suspends the
    /// render (D9). Values computed inside the expression are not awaited (`coro().title`
    /// is the coroutine's attribute), nor are a native partial's arguments.
    #[inline(never)]
    fn eval_async(&self, e: &crate::expr::Expr, emitted: bool) -> PyResult<Step<Py<PyAny>>> {
        let step = if emitted { eval_emitted(e, &self.cx())? } else { Step::Ready(eval(e, &self.cx())?.unbind()) };
        match step {
            Step::Ready(v) if crate::awaitable::is_awaitable_with(v.bind(self.py), Some(&self.render.await_types))? => {
                Ok(Step::Suspend(v))
            }
            step => Ok(step),
        }
    }

    /// Records a task started for a placeholder and returns the placeholder.
    /// The render started as many tasks as it may (`crate::deferred::limit`).
    fn at_limit(&self) -> bool {
        self.render.deferred.borrow().len() >= crate::deferred::limit(self.py)
    }

    fn register(&self, task: Bound<'py, PyAny>, kind: crate::deferred::DeferKind) -> Vec<u8> {
        let mut deferred = self.render.deferred.borrow_mut();
        let id = deferred.len() as u64;
        deferred.push(crate::deferred::Deferred { id, task: task.unbind(), kind });
        crate::deferred::placeholder(self.render.nonce, id)
    }

    fn cx(&self) -> Cx<'_, 'py> {
        Cx { scope: &self.scope, render: self.render, frame: &self.frame }
    }

    /// The active program's private-use block for lone surrogates, if its source had any.
    fn surrogate_base(&self) -> Option<u32> {
        self.program().core.surrogate_base()
    }

    /// A `str` for a piece of the active program's source text.
    fn source_str(&self, text: &str) -> PyResult<Bound<'py, PyString>> {
        convert::source_str(self.py, text, self.surrogate_base())
    }

    fn name(&self, name: NameId) -> PyResult<&Py<PyString>> {
        self.program().names.get(name.0 as usize).ok_or_else(|| PyValueError::new_err("unknown name id"))
    }

    /// The i18n settings as Python values.
    #[cold]
    fn set_i18n(&mut self, i18n: &I18n<Py<PyAny>>) -> PyResult<()> {
        let py = self.py;
        if self.frame.domain.is_some() || i18n.domain.is_some() {
            self.frame.domain = i18n.domain.as_deref().map(|d| self.source_str(d)).transpose()?;
        }
        if self.frame.context.is_some() || i18n.context.is_some() {
            self.frame.context = i18n.context.as_deref().map(|c| self.source_str(c)).transpose()?;
        }
        match &i18n.target_language {
            Some(target) => {
                let target = target.bind(py);
                if !self.frame.target_language.is(target) {
                    self.frame.target_language = target.clone();
                }
            }
            None => {
                if !self.frame.target_language.is_none() {
                    self.frame.target_language = py.None().into_bound(py);
                }
            }
        }
        Ok(())
    }

    /// A slot body's conversion helpers' i18n settings (`Translate::i18n_changed`), kept
    /// only when they differ from the frame's own (`i18n`), which they do when the body (or
    /// the slot body the fill was defined in) set its own.
    #[inline(never)]
    fn set_helpers(&mut self, i18n: &I18n<Py<PyAny>>, helpers: Option<&I18n<Py<PyAny>>>) -> PyResult<()> {
        let py = self.py;
        let target = |t: &Option<Py<PyAny>>| t.as_ref().filter(|t| !t.is_none(py)).map(Py::as_ptr);
        self.frame.helpers = match helpers {
            Some(h)
                if h.domain != i18n.domain
                    || h.context != i18n.context
                    || target(&h.target_language) != target(&i18n.target_language) =>
            {
                Some(Box::new(Helpers {
                    domain: h.domain.as_deref().map(|d| self.source_str(d)).transpose()?,
                    context: h.context.as_deref().map(|c| self.source_str(c)).transpose()?,
                    target_language: h
                        .target_language
                        .as_ref()
                        .map_or_else(|| py.None().into_bound(py), |t| t.bind(py).clone()),
                }))
            }
            _ => None,
        };
        Ok(())
    }

    /// The domain, context and `target_language` arguments of a foreign `include` call.
    fn i18n_args(&self) -> (Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, PyAny>) {
        let py = self.py;
        let opt = |s: &Option<Bound<'py, PyString>>| {
            s.as_ref().map_or_else(|| py.None().into_bound(py), |s| s.clone().into_any())
        };
        (opt(&self.frame.domain), opt(&self.frame.context), self.frame.target_language.clone())
    }

    /// The key of slot `name` in the scope (`__slot_<mangled name>`).
    fn slot_key(&self, name: &str) -> PyResult<Bound<'py, PyString>> {
        match self.program().slot_key(name) {
            Some(key) => Ok(key.bind(self.py).clone()),
            None => self.source_str(&format!("__slot_{name}")),
        }
    }

    /// Calls a foreign callable with a fresh stream (`f(stream, *args)`) and writes what it
    /// appended (`''.join(stream)`). Chameleon's slot fills append to their own template's
    /// stream through a closure instead: when this render was called by another engine, what
    /// was appended to the stream it writes to is moved here, in order.
    fn call_with_stream(&self, f: &Bound<'py, PyAny>, args: Vec<Bound<'py, PyAny>>, out: &mut Output) -> PyResult<()> {
        let py = self.py;
        let stream = PyList::empty(py);
        let mut call_args = vec![stream.clone().into_any()];
        call_args.extend(args);
        let outer = self.render.stream.get();
        let before = outer.map(PyAnyMethods::len).transpose()?;
        f.call1(PyTuple::new(py, call_args)?)?;
        let mut join = |items: &Bound<'py, PyAny>| -> PyResult<()> {
            let joined = intern!(py, "").call_method1(intern!(py, "join"), (items,))?;
            convert::write_str(joined.cast::<PyString>()?, Escape::Structure, out)
        };
        if let (Some(outer), Some(before)) = (outer, before)
            && outer.len()? > before
        {
            let appended = outer.get_item(pyo3::types::PySlice::new(py, isize::try_from(before)?, isize::MAX, 1))?;
            outer.del_item(pyo3::types::PySlice::new(py, isize::try_from(before)?, isize::MAX, 1))?;
            join(&appended)?;
        }
        join(stream.as_any())
    }
}

impl<const ASYNC: bool> Values for PyRuntime<'_, '_, ASYNC> {
    type Value = Py<PyAny>;
    type Error = PyErr;

    fn clone_value(&mut self, value: &Py<PyAny>) -> Py<PyAny> {
        value.clone_ref(self.py)
    }
}

impl<const ASYNC: bool> Eval for PyRuntime<'_, '_, ASYNC> {
    /// A frame's code is its `Program`, or for a macro of another template the `Macro`
    /// (which carries that template's builtins).
    #[inline]
    fn activate(&mut self, frame: Active<'_, Py<PyAny>>) -> PyResult<()> {
        let py = self.py;
        let code = frame.code.bind(py);
        // The frames of a native partial's render use its unit (builtins, defaults,
        // `rcontext`); there are none on the stack in most renders.
        if let Some((unit, target)) = self.render.partials.changed(self.frame.unit) {
            let marker = match &target {
                Some(t) => t.program.get().marker.bind(py),
                None => &self.render.marker,
            };
            if !self.frame.marker.is(marker) {
                self.frame.marker = marker.clone();
            }
            self.frame.unit = unit;
            self.frame.target = target;
        }
        if let Ok(program) = code.cast::<Program>() {
            self.set_code(program);
            if self.frame.builtins.is_some() {
                self.frame.builtins = None;
            }
        } else {
            let m = code.cast::<Macro>()?.get();
            self.set_code(m.program.bind(py));
            self.frame.builtins = Some(m.builtins.bind(py).clone());
        }
        self.scope = frame.scope.bind(py).cast::<Scope>()?.clone();
        self.frame.serial = frame.serial;
        // The code-block namespaces of frames that returned (see `RenderCtx::namespaces`).
        {
            let mut namespaces = self.render.namespaces.borrow_mut();
            while namespaces.last().is_some_and(|(serial, _)| *serial > frame.serial) {
                namespaces.pop();
            }
        }
        let i18n = frame.i18n;
        let plain = i18n.domain.is_none()
            && i18n.context.is_none()
            && self.frame.domain.is_none()
            && self.frame.context.is_none();
        let same_target = match &i18n.target_language {
            Some(t) => self.frame.target_language.is(t.bind(py)),
            None => self.frame.target_language.is_none(),
        };
        if !(plain && same_target) {
            self.set_i18n(i18n)?;
        }
        // Nested tokens name sites of the frame that set them.
        self.render.nested.set((0, 0));
        Ok(())
    }

    #[inline]
    fn eval(&mut self, expr: ExprId, emitted: bool) -> PyResult<Step<Py<PyAny>>> {
        let program = self.program();
        let site = program.exprs.get(expr.0 as usize).ok_or_else(|| PyValueError::new_err("unknown expression id"))?;
        // Chameleon's `__token = pos` is the VM's `ErrorSite`; only nested tokens are kept
        // here, cleared when an expression that sets them starts.
        if site.nested {
            self.render.nested.set((0, 0));
        }
        let e = &site.expr;
        if ASYNC {
            return self.eval_async(e, emitted);
        }
        if emitted { eval_emitted(e, &self.cx()) } else { Ok(Step::Ready(eval(e, &self.cx())?.unbind())) }
    }

    #[inline]
    fn emit(&mut self, value: Py<PyAny>, esc: Escape, stage: Stage, out: &mut Output) -> PyResult<Emitted> {
        // A `Bound` decrefs directly when dropped; a `Py` first checks a thread-local.
        let value = value.into_bound(self.py);
        convert::emit(&self.cx(), &value, esc, stage, out)
    }

    fn is_default(&mut self, value: &Py<PyAny>) -> bool {
        value.bind(self.py).is(self.cx().marker())
    }

    fn truthy(&mut self, value: &Py<PyAny>) -> PyResult<bool> {
        value.bind(self.py).is_truthy()
    }

    fn is_false(&mut self, value: &Py<PyAny>) -> bool {
        value.bind(self.py).is(PyBool::new(self.py, false))
    }

    fn partial_entered(&mut self) {
        self.render.partials.entered();
    }

    fn partial_returned(&mut self) {
        self.render.partials.left();
    }

    #[inline]
    fn wants_defer(&mut self) -> bool {
        ASYNC && self.render.defer_next.replace(false)
    }

    /// The pending partial as a render of its own, in a task (`crate::deferred`).
    fn defer_partial(&mut self) -> PyResult<Option<Vec<u8>>> {
        let py = self.py;
        if self.at_limit() {
            return Ok(None);
        }
        let Some(event_loop) = crate::deferred::running_loop(py) else { return Ok(None) };
        let Some(pending) = self.render.partials.take_pending() else { return Ok(None) };
        let render = crate::awaitable::RenderAwaitable::start(
            &pending.program,
            &pending.scope,
            Some(pending.defaults),
            Some(pending.builtins),
            pending.model,
            false,
            true,
        )?;
        let task = crate::deferred::start_task(&event_loop, Bound::new(py, render)?.as_any())?;
        Ok(Some(self.register(task, crate::deferred::DeferKind::Partial)))
    }

    /// A `@concurrent` helper's call in the render's own unit, as a task
    /// (`crate::deferred`).
    fn defer_value(
        &mut self,
        value: Py<PyAny>,
        esc: Escape,
        stage: Stage,
        at: ErrorSite,
    ) -> PyResult<Result<Vec<u8>, Py<PyAny>>> {
        let py = self.py;
        if self.render.partials.top() != 0 || self.at_limit() || !crate::deferred::is_concurrent_call(value.bind(py))? {
            return Ok(Err(value));
        }
        let Some(event_loop) = crate::deferred::running_loop(py) else { return Ok(Err(value)) };
        let task = crate::deferred::start_task(&event_loop, value.bind(py))?;
        let program = self.program();
        let token = program.token_at(at, self.render.nested.get());
        let site = crate::deferred::ValueSite {
            esc,
            stage,
            scope: self.scope.clone().unbind(),
            builtins: self.frame.builtins.clone().map(Bound::unbind),
            marker: self.frame.marker.clone().unbind(),
            serial: self.frame.serial,
            domain: self.frame.domain.clone().map(Bound::unbind),
            context: self.frame.context.clone().map(Bound::unbind),
            target_language: self.frame.target_language.clone().unbind(),
            helpers: self.frame.helpers.as_ref().map(|h| h.unbind()),
            program: self.codes[self.code].clone().unbind(),
            token,
        };
        Ok(Ok(self.register(task, crate::deferred::DeferKind::Value(Box::new(site)))))
    }
}

impl<const ASYNC: bool> Names for PyRuntime<'_, '_, ASYNC> {
    fn lookup(&mut self, name: NameId) -> PyResult<Option<Py<PyAny>>> {
        let key = self.name(name)?.bind(self.py);
        Ok(Scope::lookup(&self.scope, key.as_any())?.map(Bound::unbind))
    }

    fn set_null(&mut self, name: NameId) -> PyResult<()> {
        let key = self.name(name)?.bind(self.py);
        self.scope.as_super().set_item(key, self.py.None())
    }

    /// `econtext[name] = value` (a tuple target for several names), then for a global
    /// definition `rcontext[name] = value` for each name (the whole value, as Chameleon does).
    fn define(&mut self, names: &[NameId], value: Py<PyAny>, visibility: Visibility) -> PyResult<()> {
        let py = self.py;
        if visibility == Visibility::Global {
            let rcontext = self.cx().rcontext();
            for name in names {
                rcontext.set_item(self.name(*name)?.bind(py), value.bind(py))?;
            }
        }
        self.assign(names, value, Visibility::Local)
    }

    /// `if backup is marker: del econtext[KEY] else: econtext[KEY] = backup`.
    fn restore(&mut self, name: NameId, backup: Option<Py<PyAny>>) -> PyResult<()> {
        let key = self.name(name)?.bind(self.py);
        match backup {
            Some(value) => self.scope.as_super().set_item(key, value),
            None => self.scope.as_super().del_item(key),
        }
    }

    fn text_value(&mut self, text: Option<&str>) -> Py<PyAny> {
        match text {
            Some(t) => self.source_str(t).unwrap_or_else(|_| PyString::new(self.py, t)).into_any().unbind(),
            None => self.py.None(),
        }
    }

    fn stream_value(&mut self, text: &Wtf8Buf) -> Py<PyAny> {
        match convert::wtf8_str(self.py, text) {
            Ok(s) => s.into_any().unbind(),
            Err(_) => PyString::new(self.py, &text.to_string()).into_any().unbind(),
        }
    }
}

impl<const ASYNC: bool> Loops for PyRuntime<'_, '_, ASYNC> {
    /// `__iterator, INDEX = getname('repeat')(key, iterable)`: whatever `repeat` is bound to,
    /// called with the name (or a tuple of names), its result unpacked like a Python
    /// assignment. The loop iterates `iter(__iterator)` (any iterable works, as in a `for`);
    /// the length stays a Python value unless it is an exact `int` that fits `i64`.
    fn repeat(&mut self, names: &[NameId], iterable: Py<PyAny>) -> PyResult<(Py<PyAny>, Countdown<Py<PyAny>>)> {
        let py = self.py;
        let repeat_name = intern!(py, "repeat");
        let repeat = Scope::lookup(&self.scope, repeat_name.as_any())?.ok_or_else(|| PyNameError::new_err("repeat"))?;
        let key = if let [one] = names {
            self.name(*one)?.bind(py).clone().into_any()
        } else {
            let keys = names.iter().map(|n| self.name(*n).map(|k| k.bind(py).clone())).collect::<PyResult<Vec<_>>>()?;
            PyTuple::new(py, keys)?.into_any()
        };
        if let Ok(repeat) = repeat.cast_exact::<RepeatDict>() {
            let (iterator, length) = RepeatDict::register(repeat, &key, iterable.bind(py))?;
            let index = match i64::try_from(length) {
                Ok(n) => Countdown::Int(n),
                Err(_) => Countdown::Value(length.into_pyobject(py)?.into_any().unbind()),
            };
            return Ok((iterator.unbind(), index));
        }
        let result = repeat.call1((key, iterable))?;
        let mut pair = unpack(&result, 2)?.into_iter();
        let (Some(iterable), Some(length)) = (pair.next(), pair.next()) else {
            return Err(PyValueError::new_err("not enough values to unpack (expected 2)"));
        };
        let iterator = iterable.try_iter()?.into_any().unbind();
        let index = match length.cast_exact::<PyInt>().ok().and_then(|n| n.extract::<i64>().ok()) {
            Some(n) => Countdown::Int(n),
            None => Countdown::Value(length.unbind()),
        };
        Ok((iterator, index))
    }

    /// `index -= 1; index > 0` with Python's arithmetic and comparison.
    fn countdown(&mut self, index: &Py<PyAny>) -> PyResult<(Py<PyAny>, bool)> {
        let next = index.bind(self.py).sub(1)?;
        let more = next.rich_compare(0, CompareOp::Gt)?.is_truthy()?;
        Ok((next.unbind(), more))
    }

    #[inline]
    fn next(&mut self, iterator: &Py<PyAny>) -> PyResult<Option<Py<PyAny>>> {
        let mut it = iterator.bind(self.py).cast::<PyIterator>()?.clone();
        it.next().transpose().map(|v| v.map(Bound::unbind))
    }

    /// `econtext[name] = item` (a tuple target for several names); a global loop's chained
    /// assignment then does the same in `rcontext`.
    #[inline]
    fn assign(&mut self, names: &[NameId], item: Py<PyAny>, visibility: Visibility) -> PyResult<()> {
        let py = self.py;
        if visibility == Visibility::Global {
            let item = item.bind(py);
            self.assign_into(names, item, self.scope.as_super().as_any())?;
            return self.assign_into(names, item, self.cx().rcontext().as_any());
        }
        if let [one] = names {
            return self.scope.as_super().set_item(self.name(*one)?.bind(py), item);
        }
        self.assign_into(names, item.bind(py), self.scope.as_super().as_any())
    }

    /// `name '__backup_x_ID' is not defined`, the backup variable a global loop's restore
    /// code refers to (Chameleon's ID is `id(names)`; ours is the interned name's address).
    fn missing_backup(&mut self, name: NameId) -> PyErr {
        match self.name(name) {
            Ok(key) => {
                let id = key.as_ptr() as usize;
                PyNameError::new_err(format!("name '__backup_{}_{id}' is not defined", key.bind(self.py)))
            }
            Err(e) => e,
        }
    }
}

impl<const ASYNC: bool> Errors for PyRuntime<'_, '_, ASYNC> {
    fn is_catchable(&mut self, error: &PyErr) -> bool {
        error.is_instance_of::<PyException>(self.py)
    }

    /// `econtext[name] = ErrorInfo(__exc, __tokens[__token][1:3])`, then `if handler is not
    /// None: handler(__exc)` with the render function's `on_error_handler` (the scope's
    /// `__on_error_handler`). Without a token, `__tokens[None]` is a `KeyError`.
    fn catch(&mut self, error: &PyErr, name: NameId, at: ErrorSite) -> PyResult<()> {
        let py = self.py;
        let program = self.program();
        let Some((_, line, column)) = program.token_info(program.token_at(at, self.render.nested.get())) else {
            return Err(PyKeyError::new_err((py.None(),)));
        };
        let class = ERROR_INFO.get_or_try_init(py, || {
            Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs.tal"))?.getattr(intern!(py, "ErrorInfo"))?.unbind())
        })?;
        let exc = error.value(py);
        let info = class.bind(py).call1((exc, (line, column)))?;
        self.scope.as_super().set_item(self.name(name)?.bind(py), info)?;
        let handler = self.cx().scope_or_default(intern!(py, "__on_error_handler"))?;
        if let Some(handler) = handler
            && !handler.is_none()
        {
            handler.call1((exc,))?;
        }
        Ok(())
    }

    fn record_error(&mut self, error: &PyErr, at: ErrorSite) {
        let rcontext = self.cx().rcontext();
        let program = self.program();
        let token = program.token_at(at, self.render.nested.get());
        // A failure to record (an odd `rcontext['__error__']`) must not replace the error.
        let _ = record_error(self.py, &rcontext, program, token, error);
    }

    /// In an asynchronous render, cancellation (any non-`Exception`) passes unformatted: the
    /// formatted error would be a `RenderError`, an `Exception` that `tal:on-error` catches.
    fn partial_failed(&mut self, error: PyErr) -> PyErr {
        match self.render.partials.left() {
            Some(_) if ASYNC && !error.is_instance_of::<PyException>(self.py) => error,
            Some(unit) => crate::partials::partial_error(&unit, error),
            None => error,
        }
    }

    fn recursion_error(&mut self) -> PyErr {
        PyRecursionError::new_err("maximum recursion depth exceeded")
    }

    /// Chameleon's macros nest as Python calls, so they fail where the interpreter's
    /// recursion limit is reached: the limit, less the frames on the stack now (the innermost
    /// is the template's `render`), less Chameleon's own frames before the first macro
    /// (`BaseTemplate.render`, the render function) and the helpers a macro calls into
    /// (measured: an `on-error` around infinite macro recursion catches at the same depth).
    ///
    /// An asynchronous render is measured the same way while it runs: its caller awaits it, so
    /// the stack is the event loop's, the coroutines awaiting it, then `render_async` and
    /// `_render_async`, one frame more than `render` called in its place. So it stops where
    /// that `render` would.
    fn frame_budget(&mut self) -> usize {
        let py = self.py;
        let reserve = if ASYNC { 3 } else { 4 };
        let measure = || -> PyResult<usize> {
            let sys = py.import(intern!(py, "sys"))?;
            let limit: usize = sys.getattr(intern!(py, "getrecursionlimit"))?.call0()?.extract()?;
            let mut depth = 0usize;
            let mut frame = sys.getattr(intern!(py, "_getframe"))?.call0().ok();
            while let Some(f) = frame.filter(|f| !f.is_none()) {
                depth += 1;
                frame = f.getattr(intern!(py, "f_back")).ok();
            }
            Ok(limit.saturating_sub(depth + reserve))
        };
        measure().unwrap_or(chameleon_core::DEFAULT_FRAME_BUDGET)
    }
}

impl<const ASYNC: bool> Attrs for PyRuntime<'_, '_, ASYNC> {
    /// `name in __chain(dict)`: iterate and compare with `==`, as `in` on an iterator does.
    fn yields_name(&mut self, container: &Py<PyAny>, name: &str) -> PyResult<bool> {
        let py = self.py;
        let name = self.source_str(name)?;
        for item in container.bind(py).try_iter()? {
            if item?.eq(&name)? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// `visit_DictAttributes`: `for name, value in TARGET.items()`, skipping names `in
    /// EXCLUDE` (a set of the names written after it) and `None`, a name `in BOOL_NAMES` written
    /// as itself when true and skipped when false, each value through
    /// `__quote(value, '"', '&quot;', None, None)`. The sets are real sets, so membership is
    /// hashed and an unhashable name raises `TypeError`, as in Chameleon.
    fn attr_dict(&mut self, dict: &Py<PyAny>, exclude: &[&str], bool_names: &[&str], out: &mut Output) -> PyResult<()> {
        let py = self.py;
        let strs = |names: &[&str]| names.iter().map(|n| self.source_str(n)).collect::<PyResult<Vec<_>>>();
        let bool_set = (!bool_names.is_empty()).then(|| PySet::new(py, strs(bool_names)?)).transpose()?;
        let exclude_set = PySet::new(py, strs(exclude)?)?;
        let items = dict.bind(py).call_method0(intern!(py, "items"))?;
        for item in items.try_iter()? {
            let item = item?;
            let mut pair = unpack(&item, 2)?.into_iter();
            let (Some(name), Some(mut value)) = (pair.next(), pair.next()) else { continue };
            if let Some(bools) = &bool_set
                && bools.contains(&name)?
            {
                if !value.is_truthy()? {
                    continue;
                }
                value = name.clone();
            }
            if exclude_set.contains(&name)? || value.is_none() {
                continue;
            }
            // `' ' + name + '=' + '"' + __quote(...) + '"'`: the name must be a `str`.
            let name = name.cast::<PyString>().map_err(|_| {
                pyo3::exceptions::PyTypeError::new_err(format!(
                    "can only concatenate str (not \"{}\") to str",
                    name.get_type().name().map_or_else(|_| "?".into(), |n| n.to_string())
                ))
            })?;
            let mark = out.mark();
            out.push(b" ");
            convert::write_str(name, Escape::Structure, out)?;
            out.push(b"=\"");
            // `default_marker` is `None` in this `__quote` call, so the template's marker is
            // an ordinary object here (converted with `str()`).
            match convert::emit_plain(&self.cx(), &value, Escape::Attr(Quote::Double), out) {
                Ok(_) => out.push(b"\""),
                Err(e) => {
                    out.truncate(mark);
                    return Err(e);
                }
            }
        }
        Ok(())
    }

    fn case_compare(&mut self, value: &Py<PyAny>, switch: Option<&Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let py = self.py;
        let other = match switch {
            Some(s) => s.bind(py).clone(),
            None => self.cx().marker().clone(),
        };
        Ok(value.bind(py).rich_compare(other, CompareOp::Eq)?.unbind())
    }
}

impl<const ASYNC: bool> PyRuntime<'_, '_, ASYNC> {
    /// `target[a], target[b] = item` (or `target[a] = item`).
    fn assign_into(&self, names: &[NameId], item: &Bound<'_, PyAny>, target: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = self.py;
        if let [one] = names {
            return target.set_item(self.name(*one)?.bind(py), item);
        }
        let values = unpack(item, names.len())?;
        for (name, value) in names.iter().zip(values) {
            target.set_item(self.name(*name)?.bind(py), value)?;
        }
        Ok(())
    }
}

/// Whether this interpreter's "too many values" message has "got N" (CPython 3.14+).
static UNPACK_SIZED: PyOnceLock<bool> = PyOnceLock::new();

/// Python's sequence unpacking for `a, b = item`, with CPython's messages.
fn unpack<'py>(item: &Bound<'py, PyAny>, n: usize) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py = item.py();
    // An exact tuple of the right size unpacks to its items.
    if let Ok(tuple) = item.cast_exact::<PyTuple>()
        && tuple.len() == n
    {
        return Ok(tuple.iter().collect());
    }
    // CPython 3.14 added "got N" to the too-many message for exact tuples and lists.
    let sized = *UNPACK_SIZED.get_or_init(py, || py.version_info() >= (3, 14))
        && (item.is_exact_instance_of::<PyTuple>() || item.is_exact_instance_of::<PyList>());
    let mut values = Vec::with_capacity(n);
    let iterator = item.try_iter().map_err(|e| {
        // CPython's message for a type that is not iterable at all (no `__iter__`, no
        // `__getitem__`); an error raised by `__iter__` itself passes through.
        let ty = item.get_type();
        let iterable = ty.hasattr(intern!(item.py(), "__iter__")).unwrap_or(true)
            || ty.hasattr(intern!(item.py(), "__getitem__")).unwrap_or(true);
        if !iterable && e.is_instance_of::<pyo3::exceptions::PyTypeError>(item.py()) {
            let name = item.get_type().name().map_or_else(|_| "?".into(), |n| n.to_string());
            pyo3::exceptions::PyTypeError::new_err(format!("cannot unpack non-iterable {name} object"))
        } else {
            e
        }
    })?;
    for value in iterator {
        if values.len() == n {
            let message = if sized {
                format!("too many values to unpack (expected {n}, got {})", item.len()?)
            } else {
                format!("too many values to unpack (expected {n})")
            };
            return Err(PyValueError::new_err(message));
        }
        values.push(value?);
    }
    if values.len() < n {
        return Err(PyValueError::new_err(format!("not enough values to unpack (expected {n}, got {})", values.len())));
    }
    Ok(values)
}

impl<const ASYNC: bool> Macros for PyRuntime<'_, '_, ASYNC> {
    /// `__m = __macro.include; __m(__stream, econtext.copy(), rcontext, __i18n_domain,
    /// __i18n_context, target_language)`: a `Macro` of ours runs as a frame; anything else is
    /// called with a fresh stream.
    fn macro_call(&mut self, value: Py<PyAny>, out: &mut Output) -> PyResult<MacroStep<Py<PyAny>>> {
        let py = self.py;
        let value = value.into_bound(py);
        // A `Macro`'s `include` is the macro itself (the class cannot be subclassed).
        let include = match value.cast::<Macro>() {
            Ok(m) => m.clone().into_any(),
            Err(_) => value.getattr(intern!(py, "include"))?,
        };
        if let Ok(m) = include.cast::<Macro>() {
            let get = m.get();
            let program = Arc::clone(&get.program.bind(py).get().core);
            let scope = if program.shares_scope(get.entry) && !self.render.scope_exposed.get() {
                self.scope.clone()
            } else {
                Scope::copy(&self.scope)?
            };
            return Ok(MacroStep::Frame {
                call: chameleon_core::Call {
                    program,
                    code: m.clone().into_any().unbind(),
                    scope: scope.into_any().unbind(),
                },
                entry: get.entry,
                // A template used as a macro goes through its `include` method first.
                cost: if get.entry == 0 { 2 } else { 1 },
            });
        }
        let (domain, context, target_language) = self.i18n_args();
        let scope = Scope::copy(&self.scope)?.into_any();
        let rcontext = self.cx().rcontext().clone().into_any();
        self.call_with_stream(&include, vec![scope, rcontext, domain, context, target_language], out)?;
        Ok(MacroStep::Done)
    }

    fn may_share_scope(&mut self) -> bool {
        !self.render.scope_exposed.get()
    }

    fn copy_scope(&mut self) -> PyResult<Py<PyAny>> {
        Ok(Scope::copy(&self.scope)?.into_any().unbind())
    }

    /// `econtext.update(rcontext)`: the local dict gets every `rcontext` entry (the globals
    /// defined so far, and `__error__` once an error was recorded).
    fn after_macro(&mut self) -> PyResult<()> {
        // Nothing to copy while `rcontext` is empty (or not even made yet).
        let Some(rcontext) = self.cx().rcontext_made() else { return Ok(()) };
        if rcontext.is_empty() {
            return Ok(());
        }
        self.scope.as_super().update(rcontext.as_mapping())
    }

    fn fill_slot(&mut self, name: &str, fill: Fill<Py<PyAny>>, extend: bool) -> PyResult<()> {
        let py = self.py;
        let key = self.slot_key(name)?;
        let fill = Py::new(py, SlotFill::new(fill))?;
        if extend && let Some(stack) = Scope::lookup(&self.scope, key.as_any())? {
            // `_slots = getname(KEY)` succeeded: `_slots.appendleft(fill)`.
            stack.call_method1(intern!(py, "appendleft"), (fill,))?;
            return Ok(());
        }
        let deque = DEQUE.get_or_try_init(py, || {
            Ok::<_, PyErr>(py.import(intern!(py, "collections"))?.getattr(intern!(py, "deque"))?.unbind())
        })?;
        let stack = deque.bind(py).call1(((fill,),))?;
        self.scope.as_super().set_item(key, stack)
    }

    /// `try: __slot_x = econtext['__slot_x'].pop() except: __slot_x = None`.
    fn pop_slot(&mut self, name: &str) -> Option<Py<PyAny>> {
        let py = self.py;
        let key = self.slot_key(name).ok()?;
        let stack = Scope::lookup(&self.scope, key.as_any()).ok().flatten()?;
        stack.call_method0(intern!(py, "pop")).ok().map(Bound::unbind)
    }

    /// `__slot_x(__stream, econtext.copy(), rcontext)`.
    fn slot_call(&mut self, slot: &Py<PyAny>, out: &mut Output) -> PyResult<Option<Fill<Py<PyAny>>>> {
        let py = self.py;
        let slot = slot.bind(py);
        if let Ok(fill) = slot.cast::<SlotFill>() {
            return Ok(Some(fill.get().fill(py)));
        }
        let scope = Scope::copy(&self.scope)?.into_any();
        let rcontext = self.cx().rcontext().clone().into_any();
        self.call_with_stream(slot, vec![scope, rcontext], out)?;
        Ok(None)
    }
}

impl<const ASYNC: bool> Translate for PyRuntime<'_, '_, ASYNC> {
    fn i18n_changed(&mut self, i18n: &I18n<Py<PyAny>>, helpers: Option<&I18n<Py<PyAny>>>) -> PyResult<()> {
        self.set_i18n(i18n)?;
        if helpers.is_some() || self.frame.helpers.is_some() {
            self.set_helpers(i18n, helpers)?;
        }
        Ok(())
    }

    /// The render function's `translate` (`econtext['__translate']`), called with exactly the
    /// arguments, in the order, Chameleon's generated code uses at each site.
    fn translate(&mut self, call: Translation<'_, Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let py = self.py;
        let cx = self.cx();
        let key = intern!(py, "__translate");
        let translate = cx.scope_or_default(key)?.ok_or_else(|| PyKeyError::new_err((key.clone().unbind(),)))?;
        let kwargs = PyDict::new(py);
        let none = || py.None().into_bound(py);
        let i18n = |kwargs: &Bound<'_, PyDict>, target: Bound<'_, PyAny>| -> PyResult<()> {
            kwargs.set_item(intern!(py, "domain"), &cx.frame.domain)?;
            kwargs.set_item(intern!(py, "context"), &cx.frame.context)?;
            kwargs.set_item(intern!(py, "target_language"), target)
        };
        let msgid = match call {
            Translation::Block { msgid, mapping, default } => {
                let mapping = match mapping {
                    Some(items) => {
                        let d = PyDict::new(py);
                        for (k, v) in items {
                            let k = self.source_str(k)?;
                            match v {
                                NameValue::Text(t) => d.set_item(k, convert::wtf8_str(py, &t)?)?,
                                NameValue::Pieces(p) => {
                                    let p = p.iter().map(|t| convert::wtf8_str(py, t)).collect::<PyResult<Vec<_>>>()?;
                                    d.set_item(k, PyList::new(py, p)?)?;
                                }
                            }
                        }
                        d.into_any()
                    }
                    None => none(),
                };
                kwargs.set_item(intern!(py, "mapping"), mapping)?;
                kwargs.set_item(intern!(py, "default"), convert::wtf8_str(py, default)?)?;
                i18n(&kwargs, self.frame.target_language.clone())?;
                match msgid {
                    Msgid::Source(m) => self.source_str(m)?.into_any(),
                    Msgid::Stream(m) => convert::wtf8_str(py, m)?.into_any(),
                }
            }
            Translation::Content(value) => {
                kwargs.set_item(intern!(py, "default"), none())?;
                i18n(&kwargs, self.frame.target_language.clone())?;
                value.into_bound(py)
            }
            Translation::Attr { msgid, value } => {
                let value = value.into_bound(py);
                kwargs.set_item(intern!(py, "default"), &value)?;
                i18n(&kwargs, cx.scope_target_language()?)?;
                match msgid {
                    Some(m) => self.source_str(m)?.into_any(),
                    None => value,
                }
            }
            Translation::Interp { msgid, mapping } => {
                if let Some(items) = mapping {
                    let d = PyDict::new(py);
                    for (k, v) in items {
                        d.set_item(self.source_str(k)?, v)?;
                    }
                    kwargs.set_item(intern!(py, "mapping"), d)?;
                }
                i18n(&kwargs, cx.scope_target_language()?)?;
                self.source_str(msgid)?.into_any()
            }
        };
        Ok(translate.call((msgid,), Some(&kwargs))?.unbind())
    }

    fn quote_value(&mut self, value: Py<PyAny>, esc: Escape, default: Option<&str>) -> PyResult<Py<PyAny>> {
        let value = value.into_bound(self.py);
        Ok(convert::quote_value(&self.cx(), &value, esc, default, self.surrogate_base())?.unbind())
    }

    fn convert_value(&mut self, value: Py<PyAny>) -> PyResult<Py<PyAny>> {
        let value = value.into_bound(self.py);
        Ok(convert::convert_value(&self.cx(), &value)?.unbind())
    }
}
