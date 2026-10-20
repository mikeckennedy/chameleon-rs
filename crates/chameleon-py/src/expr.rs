// `lookup_attr` and the name rules are ported from Chameleon's src/chameleon/utils.py
// (`lookup_attr`) and compiler.py (`NameTransform`), modified: evaluated natively over a
// Rust expression tree instead of generated Python code.

//! The fast-path expression tree (D4) and its evaluation.

use std::cell::OnceCell;

use pyo3::exceptions::{PyAttributeError, PyKeyError, PyLookupError, PyNameError, PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::types::{PyBool, PyDict, PyList, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use chameleon_core::{Call, Step};

use crate::program::Program;
use crate::scope::Scope;

#[derive(Clone, Copy, Debug)]
pub(crate) enum UnaryOp {
    Neg,
    Pos,
    Invert,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum BinaryOp {
    Add,
    Sub,
    Mul,
    MatMul,
    Div,
    FloorDiv,
    Mod,
    Pow,
    LShift,
    RShift,
    BitOr,
    BitXor,
    BitAnd,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum CmpOp {
    Rich(CompareOp),
    Is,
    IsNot,
    In,
    NotIn,
}

/// A Python expression lowered from CPython's `ast` (the census's node types), or the exact
/// fallback: a Python function built from the rewritten `ast`.
#[derive(Debug)]
pub(crate) enum Expr {
    /// An ordinary name: local scope, then root, else `NameError`.
    Name(Py<PyString>),
    /// A builtin name: `econtext.get(name, builtin)`, so a render keyword overrides it.
    Builtin(Py<PyString>, Py<PyAny>),
    Const(Py<PyAny>),
    /// An attribute of an original constant, resolved at compile time (one level, D4).
    Folded(Py<PyAny>),
    /// `econtext`: the current scope itself.
    Econtext,
    /// `rcontext`: the per-render dict.
    Rcontext,
    /// `translate`, `decode`, `on_error_handler`: Chameleon binds these from the scope
    /// (`econtext['__translate']`, ...) at the start of every render function.
    ScopeKey(Py<PyString>),
    Attr(Box<Expr>, Py<PyString>),
    Call {
        func: Box<Expr>,
        args: Vec<Expr>,
        kwargs: Vec<(Py<PyString>, Expr)>,
    },
    Not(Box<Expr>),
    Unary(UnaryOp, Box<Expr>),
    Binary(BinaryOp, Box<Expr>, Box<Expr>),
    And(Vec<Expr>),
    Or(Vec<Expr>),
    Compare(Box<Expr>, Vec<(CmpOp, Expr)>),
    IfExp(Box<Expr>, Box<Expr>, Box<Expr>),
    Subscript(Box<Expr>, Box<Expr>),
    Tuple(Vec<Expr>),
    List(Vec<Expr>),
    Dict(Vec<(Expr, Expr)>),
    /// TALES `a | b`: the first alternative that does not raise one of Chameleon's
    /// `TalesExpr.exceptions`.
    Pipe(Vec<Expr>),
    /// A top-level `render_partial(path, **kw)` in an emitted position: a native frame when
    /// no `render_partial` is bound in the scope, else the ordinary call (`call`).
    Partial {
        path: Box<Expr>,
        kwargs: Vec<(Py<PyString>, Expr)>,
        call: Box<Expr>,
    },
    /// `f(econtext, rcontext, default_marker)` built by `chameleon_rs._rewrite`.
    Fallback(Py<PyAny>),
}

impl Expr {
    pub(crate) fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        match self {
            Self::Name(n) | Self::ScopeKey(n) => visit.call(n),
            Self::Builtin(n, v) => {
                visit.call(n)?;
                visit.call(v)
            }
            Self::Const(v) | Self::Folded(v) | Self::Fallback(v) => visit.call(v),
            Self::Econtext | Self::Rcontext => Ok(()),
            Self::Attr(e, n) => {
                e.traverse(visit)?;
                visit.call(n)
            }
            Self::Call { func, args, kwargs } => {
                func.traverse(visit)?;
                args.iter().try_for_each(|a| a.traverse(visit))?;
                kwargs.iter().try_for_each(|(k, v)| {
                    visit.call(k)?;
                    v.traverse(visit)
                })
            }
            Self::Not(e) | Self::Unary(_, e) => e.traverse(visit),
            Self::Binary(_, a, b) | Self::Subscript(a, b) => {
                a.traverse(visit)?;
                b.traverse(visit)
            }
            Self::And(v) | Self::Or(v) | Self::Tuple(v) | Self::List(v) | Self::Pipe(v) => {
                v.iter().try_for_each(|e| e.traverse(visit))
            }
            Self::Compare(left, ops) => {
                left.traverse(visit)?;
                ops.iter().try_for_each(|(_, e)| e.traverse(visit))
            }
            Self::IfExp(a, b, c) => {
                a.traverse(visit)?;
                b.traverse(visit)?;
                c.traverse(visit)
            }
            Self::Dict(items) => items.iter().try_for_each(|(k, v)| {
                k.traverse(visit)?;
                v.traverse(visit)
            }),
            Self::Partial { path, kwargs, call } => {
                path.traverse(visit)?;
                kwargs.iter().try_for_each(|(k, v)| {
                    visit.call(k)?;
                    v.traverse(visit)
                })?;
                call.traverse(visit)
            }
        }
    }
}

/// Render-wide state shared by every frame.
pub(crate) struct RenderCtx<'py> {
    pub(crate) marker: Bound<'py, PyAny>,
    /// Prototype partial registry: name -> native `Program`.
    pub(crate) partials: Option<Bound<'py, PyDict>>,
    /// The template's default `__translate`, `__decode`, ... for scopes that lack them
    /// (native partial frames).
    pub(crate) defaults: Option<Bound<'py, PyDict>>,
    /// `rcontext`, created on first use.
    pub(crate) rcontext: OnceCell<Bound<'py, PyDict>>,
}

impl<'py> RenderCtx<'py> {
    pub(crate) fn rcontext(&self, py: Python<'py>) -> &Bound<'py, PyDict> {
        self.rcontext.get_or_init(|| PyDict::new(py))
    }
}

/// Evaluation context: the active frame's scope plus render-wide state.
pub(crate) struct Cx<'a, 'py> {
    pub(crate) scope: &'a Bound<'py, Scope>,
    pub(crate) render: &'a RenderCtx<'py>,
}

impl<'py> Cx<'_, 'py> {
    /// A scope entry, falling back to the template defaults (for `__translate` and friends).
    pub(crate) fn scope_or_default(&self, key: &Bound<'py, PyString>) -> PyResult<Option<Bound<'py, PyAny>>> {
        if let Some(v) = Scope::lookup(self.scope, key.as_any())? {
            return Ok(Some(v));
        }
        match &self.render.defaults {
            Some(defaults) => defaults.get_item(key),
            None => Ok(None),
        }
    }
}

/// Evaluates an expression whose value is emitted: may return a native frame call.
pub(crate) fn eval_emitted(e: &Expr, cx: &Cx<'_, '_>) -> PyResult<Step<Py<PyAny>>> {
    if let Expr::Partial { path, kwargs, call } = e {
        let py = cx.scope.py();
        if Scope::lookup(cx.scope, intern!(py, "render_partial").as_any())?.is_some() {
            return Ok(Step::Ready(eval(call, cx)?.unbind()));
        }
        return partial_call(path, kwargs, cx).map(Step::Call);
    }
    Ok(Step::Ready(eval(e, cx)?.unbind()))
}

fn partial_call(path: &Expr, kwargs: &[(Py<PyString>, Expr)], cx: &Cx<'_, '_>) -> PyResult<Call<Py<PyAny>>> {
    let py = cx.scope.py();
    let path = eval(path, cx)?;
    let Some(registry) = &cx.render.partials else {
        // Without a registry the name is simply unbound, as in Chameleon.
        return Err(PyNameError::new_err("render_partial"));
    };
    let target =
        registry.get_item(&path)?.ok_or_else(|| PyValueError::new_err(format!("Partial not found: {path}")))?;
    let program = target.cast_into::<Program>().map_err(PyErr::from)?;
    let scope = Scope::empty(py)?;
    for (key, value) in kwargs {
        scope.as_super().set_item(key.bind(py), eval(value, cx)?)?;
    }
    let p = program.get();
    if p.needs_repeat {
        scope.as_super().set_item(intern!(py, "repeat"), crate::program::new_repeat_dict(py)?)?;
    }
    Ok(Call { program: p.core.clone(), code: program.into_any().unbind(), scope: scope.into_any().unbind() })
}

/// The exceptions a TALES pipe falls through on (`TalesExpr.exceptions`).
fn pipe_catches(py: Python<'_>, e: &PyErr) -> bool {
    e.is_instance_of::<PyAttributeError>(py)
        || e.is_instance_of::<PyNameError>(py)
        || e.is_instance_of::<PyLookupError>(py)
        || e.is_instance_of::<PyTypeError>(py)
        || e.is_instance_of::<PyValueError>(py)
}

#[allow(clippy::too_many_lines)] // one arm per node type
pub(crate) fn eval<'py>(e: &Expr, cx: &Cx<'_, 'py>) -> PyResult<Bound<'py, PyAny>> {
    let py = cx.scope.py();
    match e {
        Expr::Name(name) => {
            let name = name.bind(py);
            Scope::lookup(cx.scope, name.as_any())?.ok_or_else(|| PyNameError::new_err(name.clone().unbind()))
        }
        Expr::Builtin(name, builtin) => {
            Ok(Scope::lookup(cx.scope, name.bind(py).as_any())?.unwrap_or_else(|| builtin.bind(py).clone()))
        }
        Expr::Const(v) | Expr::Folded(v) => Ok(v.bind(py).clone()),
        Expr::Econtext => Ok(cx.scope.clone().into_any()),
        Expr::Rcontext => Ok(cx.render.rcontext(py).clone().into_any()),
        Expr::ScopeKey(key) => {
            let key = key.bind(py);
            cx.scope_or_default(key)?.ok_or_else(|| PyKeyError::new_err(key.clone().unbind()))
        }
        Expr::Attr(obj, name) => lookup_attr(&eval(obj, cx)?, name.bind(py)),
        Expr::Call { func, args, kwargs } => {
            let func = eval(func, cx)?;
            if kwargs.is_empty() {
                // Fixed arities go through vectorcall without building a list first.
                return match args.as_slice() {
                    [] => func.call0(),
                    [a] => func.call1((eval(a, cx)?,)),
                    [a, b] => {
                        let a = eval(a, cx)?;
                        func.call1((a, eval(b, cx)?))
                    }
                    _ => func.call1(PyTuple::new(py, eval_all(args, cx)?)?),
                };
            }
            let args = PyTuple::new(py, eval_all(args, cx)?)?;
            let kw = PyDict::new(py);
            for (k, v) in kwargs {
                kw.set_item(k.bind(py), eval(v, cx)?)?;
            }
            func.call(args, Some(&kw))
        }
        Expr::Not(v) => Ok(PyBool::new(py, !eval(v, cx)?.is_truthy()?).to_owned().into_any()),
        Expr::Unary(op, v) => {
            let v = eval(v, cx)?;
            match op {
                UnaryOp::Neg => v.neg(),
                UnaryOp::Pos => v.pos(),
                UnaryOp::Invert => v.bitnot(),
            }
        }
        Expr::Binary(op, a, b) => {
            let a = eval(a, cx)?;
            let b = eval(b, cx)?;
            match op {
                BinaryOp::Add => a.add(b),
                BinaryOp::Sub => a.sub(b),
                BinaryOp::Mul => a.mul(b),
                BinaryOp::MatMul => a.matmul(b),
                BinaryOp::Div => a.div(b),
                BinaryOp::FloorDiv => a.floor_div(b),
                BinaryOp::Mod => a.rem(b),
                BinaryOp::Pow => a.pow(b, py.None()),
                BinaryOp::LShift => a.lshift(b),
                BinaryOp::RShift => a.rshift(b),
                BinaryOp::BitOr => a.bitor(b),
                BinaryOp::BitXor => a.bitxor(b),
                BinaryOp::BitAnd => a.bitand(b),
            }
        }
        Expr::And(values) => {
            let mut last = None;
            for v in values {
                let value = eval(v, cx)?;
                if !value.is_truthy()? {
                    return Ok(value);
                }
                last = Some(value);
            }
            last.ok_or_else(|| PyValueError::new_err("empty BoolOp"))
        }
        Expr::Or(values) => {
            let mut last = None;
            for v in values {
                let value = eval(v, cx)?;
                if value.is_truthy()? {
                    return Ok(value);
                }
                last = Some(value);
            }
            last.ok_or_else(|| PyValueError::new_err("empty BoolOp"))
        }
        Expr::Compare(left, ops) => {
            let mut left = eval(left, cx)?;
            let mut result = None;
            for (i, (op, right)) in ops.iter().enumerate() {
                let right = eval(right, cx)?;
                let r = compare(py, &left, *op, &right)?;
                if i + 1 < ops.len() && !r.is_truthy()? {
                    return Ok(r);
                }
                result = Some(r);
                left = right;
            }
            result.ok_or_else(|| PyValueError::new_err("empty Compare"))
        }
        Expr::IfExp(test, body, orelse) => {
            if eval(test, cx)?.is_truthy()? {
                eval(body, cx)
            } else {
                eval(orelse, cx)
            }
        }
        Expr::Subscript(obj, key) => {
            let obj = eval(obj, cx)?;
            obj.get_item(eval(key, cx)?)
        }
        Expr::Tuple(items) => Ok(PyTuple::new(py, eval_all(items, cx)?)?.into_any()),
        Expr::List(items) => Ok(PyList::new(py, eval_all(items, cx)?)?.into_any()),
        Expr::Dict(items) => {
            let d = PyDict::new(py);
            for (k, v) in items {
                let k = eval(k, cx)?;
                d.set_item(k, eval(v, cx)?)?;
            }
            Ok(d.into_any())
        }
        Expr::Pipe(alternatives) => {
            let (last, rest) = alternatives.split_last().ok_or_else(|| PyValueError::new_err("empty pipe"))?;
            for alt in rest {
                match eval(alt, cx) {
                    Ok(v) => return Ok(v),
                    Err(err) if pipe_catches(py, &err) => {}
                    Err(err) => return Err(err),
                }
            }
            eval(last, cx)
        }
        Expr::Partial { call, .. } => eval(call, cx),
        Expr::Fallback(func) => {
            let rcontext = cx.render.rcontext(py);
            func.bind(py).call1((cx.scope, rcontext, &cx.render.marker))
        }
    }
}

fn eval_all<'py>(items: &[Expr], cx: &Cx<'_, 'py>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    items.iter().map(|e| eval(e, cx)).collect()
}

fn compare<'py>(
    py: Python<'py>,
    a: &Bound<'py, PyAny>,
    op: CmpOp,
    b: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let truth = |v: bool| Ok(PyBool::new(py, v).to_owned().into_any());
    match op {
        CmpOp::Rich(op) => a.rich_compare(b, op),
        CmpOp::Is => truth(a.is(b)),
        CmpOp::IsNot => truth(!a.is(b)),
        CmpOp::In => truth(b.contains(a)?),
        CmpOp::NotIn => truth(!b.contains(a)?),
    }
}

/// Chameleon's `lookup_attr` (utils.py:515), exactly: `getattr`; on `AttributeError` look up
/// `__getitem__` as an attribute and call it; a `KeyError` from that re-raises the original
/// `AttributeError`; any other error propagates.
pub(crate) fn lookup_attr<'py>(obj: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    let py = obj.py();
    match obj.getattr(name) {
        Ok(value) => Ok(value),
        Err(original) if original.is_instance_of::<PyAttributeError>(py) => {
            let get = match obj.getattr(intern!(py, "__getitem__")) {
                Ok(get) => get,
                Err(e) if e.is_instance_of::<PyAttributeError>(py) => return Err(original),
                Err(e) => return Err(e),
            };
            match get.call1((name,)) {
                Ok(value) => Ok(value),
                Err(e) if e.is_instance_of::<PyKeyError>(py) => Err(original),
                Err(e) => Err(e),
            }
        }
        Err(e) => Err(e),
    }
}

/// `lookup_attr` for Python callers (the fallback expressions).
#[pyfunction]
#[pyo3(name = "lookup_attr")]
pub(crate) fn py_lookup_attr<'py>(obj: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    lookup_attr(obj, name)
}
