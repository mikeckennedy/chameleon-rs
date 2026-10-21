// The loop name handling follows Chameleon's src/chameleon/compiler.py (`visit_Repeat`,
// `_enter_assignment`, `_leave_assignment`), and the error handling its `visit_OnError` and
// `visit_Macro` (the `rcontext['__error__']` record), modified: implemented as a `Runtime`
// for the core VM.

//! The Python [`Runtime`] (D2): values are `Py<PyAny>`, errors are `PyErr` untouched.

use pyo3::exceptions::{PyException, PyKeyError, PyNameError, PyRecursionError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyInt, PyIterator, PyList, PySet, PyString, PyTuple};

use chameleon_core::{
    Countdown, Emitted, ErrorSite, Escape, ExprId, NameId, Output, Quote, Runtime, Stage, Step, Visibility,
};

use crate::convert;
use crate::expr::{Cx, RenderCtx, eval, eval_emitted};
use crate::program::{Program, record_error};
use crate::scope::Scope;

static ERROR_INFO: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Rebuilt for every render (and, once async lands, every resume): it holds `Bound`
/// references, the VM state holds only `Py`.
pub(crate) struct PyRuntime<'a, 'py> {
    py: Python<'py>,
    render: &'a RenderCtx<'py>,
    code: Bound<'py, Program>,
    scope: Bound<'py, Scope>,
}

impl<'a, 'py> PyRuntime<'a, 'py> {
    pub(crate) fn new(render: &'a RenderCtx<'py>, code: Bound<'py, Program>, scope: Bound<'py, Scope>) -> Self {
        Self { py: code.py(), render, code, scope }
    }

    fn cx(&self) -> Cx<'_, 'py> {
        Cx { scope: &self.scope, render: self.render }
    }

    fn name(&self, name: NameId) -> PyResult<&Py<PyString>> {
        self.code.get().names.get(name.0 as usize).ok_or_else(|| PyValueError::new_err("unknown name id"))
    }
}

impl Runtime for PyRuntime<'_, '_> {
    type Value = Py<PyAny>;
    type Error = PyErr;

    fn activate(&mut self, code: &Py<PyAny>, scope: &Py<PyAny>) -> PyResult<()> {
        self.code = code.bind(self.py).cast::<Program>()?.clone();
        self.scope = scope.bind(self.py).cast::<Scope>()?.clone();
        // Nested tokens name sites of the frame that set them.
        self.render.nested.set((0, 0));
        Ok(())
    }

    #[inline]
    fn eval(&mut self, expr: ExprId, emitted: bool) -> PyResult<Step<Py<PyAny>>> {
        let program = self.code.get();
        let site = program.exprs.get(expr.0 as usize).ok_or_else(|| PyValueError::new_err("unknown expression id"))?;
        // Chameleon's `__token = pos` is the VM's `ErrorSite`; only nested tokens are kept
        // here, cleared when an expression that sets them starts.
        if site.nested {
            self.render.nested.set((0, 0));
        }
        let e = &site.expr;
        if emitted { eval_emitted(e, &self.cx()) } else { Ok(Step::Ready(eval(e, &self.cx())?.unbind())) }
    }

    #[inline]
    fn emit(&mut self, value: Py<PyAny>, esc: Escape, stage: Stage, out: &mut Output) -> PyResult<Emitted> {
        // A `Bound` decrefs directly when dropped; a `Py` first checks a thread-local.
        let value = value.into_bound(self.py);
        convert::emit(&self.cx(), &value, esc, stage, out)
    }

    fn is_default(&mut self, value: &Py<PyAny>) -> bool {
        value.bind(self.py).is(&self.render.marker)
    }

    fn truthy(&mut self, value: &Py<PyAny>) -> PyResult<bool> {
        value.bind(self.py).is_truthy()
    }

    fn lookup(&mut self, name: NameId) -> PyResult<Option<Py<PyAny>>> {
        let key = self.name(name)?.bind(self.py);
        Ok(Scope::lookup(&self.scope, key.as_any())?.map(Bound::unbind))
    }

    fn set_null(&mut self, name: NameId) -> PyResult<()> {
        let key = self.name(name)?.bind(self.py);
        self.scope.as_super().set_item(key, self.py.None())
    }

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
            return self.assign_into(names, item, self.render.rcontext(py).as_any());
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

    fn is_catchable(&mut self, error: &PyErr) -> bool {
        error.is_instance_of::<PyException>(self.py)
    }

    /// `econtext[name] = ErrorInfo(__exc, __tokens[__token][1:3])`, then `if handler is not
    /// None: handler(__exc)` with the render function's `on_error_handler` (the scope's
    /// `__on_error_handler`). Without a token, `__tokens[None]` is a `KeyError`.
    fn catch(&mut self, error: &PyErr, name: NameId, at: ErrorSite) -> PyResult<()> {
        let py = self.py;
        let program = self.code.get();
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
        let rcontext = self.render.rcontext(self.py);
        let program = self.code.get();
        let token = program.token_at(at, self.render.nested.get());
        // A failure to record (an odd `rcontext['__error__']`) must not replace the error.
        let _ = record_error(self.py, rcontext, program, token, error);
    }

    fn recursion_error(&mut self) -> PyErr {
        PyRecursionError::new_err("maximum recursion depth exceeded while rendering a template")
    }

    /// `econtext[name] = value` (a tuple target for several names), then for a global
    /// definition `rcontext[name] = value` for each name (the whole value, as Chameleon does).
    fn define(&mut self, names: &[NameId], value: Py<PyAny>, visibility: Visibility) -> PyResult<()> {
        let py = self.py;
        if visibility == Visibility::Global {
            let rcontext = self.render.rcontext(py);
            for name in names {
                rcontext.set_item(self.name(*name)?.bind(py), value.bind(py))?;
            }
        }
        self.assign(names, value, Visibility::Local)
    }

    /// `name in __chain(dict)`: iterate and compare with `==`, as `in` on an iterator does.
    fn yields_name(&mut self, container: &Py<PyAny>, name: &str) -> PyResult<bool> {
        let py = self.py;
        let name = PyString::new(py, name);
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
        let bool_set = (!bool_names.is_empty()).then(|| PySet::new(py, bool_names)).transpose()?;
        let exclude_set = PySet::new(py, exclude)?;
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
            None => self.render.marker.clone(),
        };
        Ok(value.bind(py).rich_compare(other, CompareOp::Eq)?.unbind())
    }

    fn is_false(&mut self, value: &Py<PyAny>) -> bool {
        value.bind(self.py).is(PyBool::new(self.py, false))
    }

    /// `if backup is marker: del econtext[KEY] else: econtext[KEY] = backup`.
    fn restore(&mut self, name: NameId, backup: Option<Py<PyAny>>) -> PyResult<()> {
        let key = self.name(name)?.bind(self.py);
        match backup {
            Some(value) => self.scope.as_super().set_item(key, value),
            None => self.scope.as_super().del_item(key),
        }
    }
}

impl PyRuntime<'_, '_> {
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

/// Python's sequence unpacking for `a, b = item`, with CPython's messages.
fn unpack<'py>(item: &Bound<'py, PyAny>, n: usize) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let sized = item.is_exact_instance_of::<PyTuple>() || item.is_exact_instance_of::<PyList>();
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
