// The partial semantics follow chameleon_partials (`render_partial`: the partial's model is its
// keyword arguments plus `render_partial`, rendered with `encoding='utf-8'`) and Chameleon's
// src/chameleon/template.py (`BaseTemplate.render`'s error handler, `PageTemplate.render`'s
// defaults), modified: a partial renders as a native frame writing into the caller's output.

//! Built-in partials (D10): `render_partial(path, **kwargs)` written as a whole emitted
//! expression runs the partial's program as a native frame of the caller's render.
//!
//! A partial is a render of its own in `chameleon_partials` (a `page.render(...)` call), so a
//! native partial frame starts a *unit*: its template's builtins, its render defaults (those
//! of `encoding='utf-8'`), and a fresh `rcontext`, used by every frame it calls (macros, slot
//! bodies) until it returns. An error that ends the partial is formatted as its own render
//! formats it, then continues in the caller.
//!
//! Resolution (which template a path names) and preparation (compiling, the defaults) are
//! Python (`chameleon_rs.partials`), done at most once per call site and template per render;
//! later calls in the same render reuse them.

use std::cell::{Cell, OnceCell, RefCell};
use std::rc::Rc;

use pyo3::exceptions::PyRecursionError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyString};

use chameleon_core::{Call, Step};

use crate::expr::{Cx, Expr, eval};
use crate::program::{Program, new_repeat_dict};
use crate::scope::Scope;

static RENDERER_TYPE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TARGET: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static PREPARE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static RENDER_ERROR: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// `chameleon_rs.partials.PartialRenderer`, for an exact type check by address.
fn renderer_type(py: Python<'_>) -> PyResult<*mut pyo3::ffi::PyTypeObject> {
    let class = RENDERER_TYPE.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs.partials"))?.getattr("PartialRenderer")?.unbind())
    })?;
    Ok(class.as_ptr().cast())
}

fn partials_attr<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyAny>>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    Ok(cell
        .get_or_try_init(py, || {
            Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs.partials"))?.getattr(name)?.unbind())
        })?
        .bind(py)
        .clone())
}

/// A template prepared for native partial frames in this render: what `page.render(encoding=
/// 'utf-8', ...)` would use.
pub(crate) struct Target<'py> {
    /// Keeps the template (and so its compilation) alive for the render.
    _template: Bound<'py, PyAny>,
    pub(crate) program: Bound<'py, Program>,
    /// The template's builtins (`template`, `macros`, `render_partial`, ...).
    pub(crate) builtins: Bound<'py, PyDict>,
    /// `__translate`, `__decode`, `__on_error_handler`, `target_language` (in this order).
    pub(crate) defaults: Bound<'py, PyDict>,
    /// Its own `render_partial` builtin (resolving relative to it).
    own: Option<Bound<'py, PyAny>>,
}

/// A native partial's render (see the module docs).
pub(crate) struct Unit<'py> {
    /// Unique within the render (frames compare it to notice a change of unit).
    serial: u64,
    pub(crate) target: Rc<Target<'py>>,
    /// The partial's scope (its `econtext`), for its error message.
    scope: Bound<'py, Scope>,
    /// How many keys the scope started with (its model): an error message lists them first.
    initial: usize,
    /// What `chameleon_partials` adds to the model as `render_partial` when the scope does not
    /// hold it (a partial that never reads its scope as a whole is not given it: its builtin of
    /// the same name is the same object).
    added: Added<'py>,
    pub(crate) rcontext: OnceCell<Bound<'py, PyDict>>,
}

/// The `render_partial` a partial's model gets (see [`Unit::added`]).
enum Added<'py> {
    /// The model has its own.
    Nothing,
    /// The partial template's builtin.
    Own,
    /// The function called (`chameleon_partials`' own).
    Func(Bound<'py, PyAny>),
}

/// A call site's resolution in this render: the function called, the path, and the target
/// (`None`: the function is called as Python, not as a native frame).
struct SiteEntry<'py> {
    site: usize,
    func: Bound<'py, PyAny>,
    path: Bound<'py, PyAny>,
    target: Option<Rc<Target<'py>>>,
}

/// The render-wide partial state (part of [`crate::expr::RenderCtx`]).
#[derive(Default)]
pub(crate) struct Partials<'py> {
    /// The units of the partial frames on the stack, innermost last.
    units: RefCell<Vec<Unit<'py>>>,
    /// The innermost unit's serial (`0`: none), read on every frame activation.
    top: Cell<u64>,
    serials: Cell<u64>,
    /// The unit of the call just returned as [`Step::Call`], until its frame is pushed.
    pending: RefCell<Option<Unit<'py>>>,
    sites: RefCell<Vec<SiteEntry<'py>>>,
    /// Prepared templates, by object address.
    targets: RefCell<Vec<(usize, Rc<Target<'py>>)>>,
}

impl<'py> Partials<'py> {
    /// The innermost unit (its serial and target), if it is not the one with serial
    /// `current`: `None` when nothing changes. The active frame always belongs to the
    /// innermost unit (or to the render itself when there is none).
    #[inline]
    pub(crate) fn changed(&self, current: u64) -> Option<(u64, Option<Rc<Target<'py>>>)> {
        let top = self.top.get();
        if top == current {
            return None;
        }
        let target = self.units.borrow().last().map(|u| Rc::clone(&u.target));
        Some((top, target))
    }

    /// `rcontext` of the innermost unit, created on first use.
    pub(crate) fn rcontext(&self, py: Python<'py>) -> Bound<'py, PyDict> {
        let units = self.units.borrow();
        match units.last() {
            Some(unit) => unit.rcontext.get_or_init(|| PyDict::new(py)).clone(),
            None => PyDict::new(py),
        }
    }

    /// `rcontext` of the innermost unit, if it was created.
    pub(crate) fn rcontext_made(&self) -> Option<Bound<'py, PyDict>> {
        self.units.borrow().last().and_then(|u| u.rcontext.get().cloned())
    }

    /// The pending unit's frame was pushed.
    pub(crate) fn entered(&self) {
        if let Some(mut unit) = self.pending.borrow_mut().take() {
            let serial = self.serials.get() + 1;
            self.serials.set(serial);
            unit.serial = serial;
            self.units.borrow_mut().push(unit);
            self.top.set(serial);
        }
    }

    /// The innermost partial frame ended.
    pub(crate) fn left(&self) -> Option<Unit<'py>> {
        let mut units = self.units.borrow_mut();
        let unit = units.pop();
        self.top.set(units.last().map_or(0, |u| u.serial));
        unit
    }

    /// The target for a call of `func(path, ...)` at `site`, resolving and preparing it on first
    /// use in this render.
    fn target(
        &self,
        site: usize,
        func: &Bound<'py, PyAny>,
        path: &Bound<'py, PyAny>,
        cx: &Cx<'_, 'py>,
    ) -> PyResult<Option<Rc<Target<'py>>>> {
        {
            let sites = self.sites.borrow();
            if let Some(entry) = sites.iter().find(|e| e.site == site && e.func.is(func)) {
                if entry.path.is(path) {
                    return Ok(entry.target.clone());
                }
                // A computed path: the same text is the same template.
                if entry.path.is_exact_instance_of::<PyString>()
                    && path.is_exact_instance_of::<PyString>()
                    && entry.path.eq(path)?
                {
                    return Ok(entry.target.clone());
                }
            }
        }
        let py = func.py();
        let caller = cx.builtins().get_item(intern!(py, "template"))?;
        let template = partials_attr(py, &TARGET, "_target")?.call1((func, caller, path))?;
        let target = if template.is_none() { None } else { self.prepare(&template)? };
        let mut sites = self.sites.borrow_mut();
        if let Some(entry) = sites.iter_mut().find(|e| e.site == site && e.func.is(func)) {
            entry.path = path.clone();
            entry.target.clone_from(&target);
        } else {
            sites.push(SiteEntry { site, func: func.clone(), path: path.clone(), target: target.clone() });
        }
        Ok(target)
    }

    /// `chameleon_rs.partials._prepare(template)`, once per template per render (so
    /// `auto_reload` checks a file at most once per render).
    fn prepare(&self, template: &Bound<'py, PyAny>) -> PyResult<Option<Rc<Target<'py>>>> {
        let key = template.as_ptr() as usize;
        if let Some((_, target)) = self.targets.borrow().iter().find(|(k, _)| *k == key) {
            return Ok(Some(Rc::clone(target)));
        }
        let py = template.py();
        let prepared = partials_attr(py, &PREPARE, "_prepare")?.call1((template,))?;
        if prepared.is_none() {
            return Ok(None);
        }
        let (program, builtins, defaults): (Bound<'py, Program>, Bound<'py, PyDict>, Bound<'py, PyDict>) =
            prepared.extract()?;
        let own = builtins.get_item(intern!(py, "render_partial"))?;
        let target = Rc::new(Target { _template: template.clone(), program, builtins, defaults, own });
        self.targets.borrow_mut().push((key, Rc::clone(&target)));
        Ok(Some(target))
    }
}

/// `render_partial(path, **kwargs)` in an emitted position (see [`Expr::Partial`]): a native
/// frame when `render_partial` is the built-in (unbound, a `PartialRenderer`) or
/// `chameleon_partials`' function and the template can render natively; otherwise the ordinary
/// call, `call`.
pub(crate) fn partial_step(
    site: &Expr,
    path: &Expr,
    kwargs: &[(Py<PyString>, Expr)],
    call: &Expr,
    cx: &Cx<'_, '_>,
) -> PyResult<Step<Py<PyAny>>> {
    let py = cx.scope.py();
    let name = intern!(py, "render_partial");
    let func = match Scope::lookup(cx.scope, name.as_any())? {
        Some(f) => f,
        None => match cx.builtins().get_item(name)? {
            Some(f) => f,
            // Unbound: the call raises `NameError`, as in Chameleon.
            None => return Ok(Step::Ready(eval(call, cx)?.unbind())),
        },
    };
    let builtin = func.get_type_ptr() == renderer_type(py)?;
    if !builtin && !func.is_instance_of::<pyo3::types::PyFunction>() {
        return Ok(Step::Ready(eval(call, cx)?.unbind()));
    }
    // Python's order: the function, the path, the keyword arguments, then the call.
    let path = eval(path, cx)?;
    let scope = Scope::empty(py)?;
    let locals = scope.as_super();
    for (key, value) in kwargs {
        locals.set_item(key.bind(py), eval(value, cx)?)?;
    }
    let site_key = std::ptr::from_ref(site) as usize;
    let Some(target) = cx.render.partials.target(site_key, &func, &path, cx)? else {
        return Ok(Step::Ready(func.call((path,), Some(locals))?.unbind()));
    };
    // chameleon_partials: `template_data['render_partial'] = render_partial` unless given (the
    // built-in passes the partial template's own, which resolves relative to it).
    let initial = kwargs.len();
    let p = target.program.get();
    let added = if kwargs.iter().any(|(key, _)| key.bind(py).is(name)) {
        Added::Nothing
    } else if builtin && let Some(own) = &target.own {
        if p.full_scope {
            locals.set_item(name, own)?;
        }
        Added::Own
    } else {
        locals.set_item(name, &func)?;
        Added::Func(func)
    };
    if p.full_scope {
        // `page.render(encoding='utf-8', **template_data)`: the defaults it lacks, then a
        // fresh `repeat`.
        for (key, value) in target.defaults.iter() {
            if !locals.contains(&key)? {
                locals.set_item(key, value)?;
            }
        }
        let repeat = intern!(py, "repeat");
        if !locals.contains(repeat)? {
            locals.set_item(repeat, new_repeat_dict(py)?)?;
        }
    }
    let call = Call {
        program: p.core.clone(),
        code: target.program.clone().into_any().unbind(),
        scope: scope.clone().into_any().unbind(),
    };
    *cx.render.partials.pending.borrow_mut() =
        Some(Unit { serial: 0, target, scope, initial, added, rcontext: OnceCell::new() });
    Ok(Step::Call(call))
}

/// The error a failed partial raises to its caller: `BaseTemplate.render`'s handler, with the
/// partial's errors, `econtext` and `rcontext` (see [`crate::program`]'s `render_error`).
pub(crate) fn partial_error<'py>(unit: &Unit<'py>, error: PyErr) -> PyErr {
    let py = unit.scope.py();
    if error.is_instance_of::<PyRecursionError>(py) {
        return error;
    }
    let Some(rcontext) = unit.rcontext.get() else { return error };
    let errors = match rcontext.get_item(intern!(py, "__error__")) {
        Ok(Some(errors)) if errors.is_truthy().unwrap_or(false) => errors,
        _ => return error,
    };
    let formatted = (|| -> PyResult<Bound<'py, PyAny>> {
        let econtext = error_econtext(unit)?;
        let render_error = RENDER_ERROR.get_or_try_init(py, || {
            Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs.exc"))?.getattr(intern!(py, "render_error"))?.unbind())
        })?;
        render_error.bind(py).call1((error.value(py), errors, econtext, rcontext, &unit.target.builtins))
    })();
    match formatted {
        Ok(exc) => PyErr::from_value(exc),
        Err(e) => e,
    }
}

/// The partial's `econtext` as `chameleon_partials`' render would have it: the model, then the
/// render defaults and `repeat` it lacks, then what the template defined.
fn error_econtext<'py>(unit: &Unit<'py>) -> PyResult<Bound<'py, PyDict>> {
    let py = unit.scope.py();
    let out = PyDict::new(py);
    let locals = unit.scope.as_super();
    let mut rest = Vec::new();
    for (i, (key, value)) in locals.iter().enumerate() {
        if i < unit.initial {
            out.set_item(key, value)?;
        } else {
            rest.push((key, value));
        }
    }
    let name = intern!(py, "render_partial");
    let added = match &unit.added {
        Added::Nothing => None,
        Added::Own => unit.target.own.as_ref(),
        Added::Func(func) => Some(func),
    };
    if let Some(added) = added
        && !out.contains(name)?
    {
        out.set_item(name, added)?;
    }
    for (key, value) in unit.target.defaults.iter() {
        if !locals.contains(&key)? {
            out.set_item(key, value)?;
        }
    }
    let repeat = intern!(py, "repeat");
    if !locals.contains(repeat)? {
        out.set_item(repeat, new_repeat_dict(py)?)?;
    }
    for (key, value) in rest {
        if !out.contains(&key)? {
            out.set_item(key, value)?;
        }
    }
    Ok(out)
}
