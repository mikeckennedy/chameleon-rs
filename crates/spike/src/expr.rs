//! Experiments 2, 3 and 4 (D4, D6): a fast-path expression tree lowered from CPython's own
//! `ast`, Chameleon's exact `lookup_attr` semantics, the fallback (a Python function per
//! expression), and exact-type conversion with escaping into a Rust buffer.

use pyo3::exceptions::{PyAttributeError, PyKeyError, PyNameError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::scope::Scope;

pub(crate) enum Expr {
    Name(Py<PyString>),
    Const(Py<PyAny>),
    Attr(Box<Expr>, Py<PyString>),
    Call(Box<Expr>, Vec<Expr>),
    List(Vec<Expr>),
    /// A Python function `f(getname, lookup_attr)`, Chameleon-style.
    Fallback(Py<PyAny>),
}

/// Lowers a Python expression to the fast-path tree using CPython's `ast` module.
/// Spike scope: Name, Constant, Attribute, Call (positional args), List.
pub(crate) fn compile(py: Python<'_>, source: &str) -> PyResult<Expr> {
    let ast = py.import(intern!(py, "ast"))?;
    // `mode` must be passed by keyword: ast.parse's second positional parameter is `filename`.
    let kwargs = PyDict::new(py);
    kwargs.set_item(intern!(py, "mode"), intern!(py, "eval"))?;
    let tree = ast.call_method(intern!(py, "parse"), (source.trim(),), Some(&kwargs))?;
    lower(&tree.getattr(intern!(py, "body"))?)
}

fn lower(node: &Bound<'_, PyAny>) -> PyResult<Expr> {
    let py = node.py();
    let kind = node.get_type().name()?;
    Ok(match kind.to_str()? {
        "Name" => Expr::Name(PyString::intern(py, node.getattr("id")?.cast::<PyString>()?.to_str()?).unbind()),
        "Constant" => Expr::Const(node.getattr("value")?.unbind()),
        "Attribute" => {
            let value = lower(&node.getattr("value")?)?;
            let attr = PyString::intern(py, node.getattr("attr")?.cast::<PyString>()?.to_str()?);
            // Optimization (c): an attribute of a constant (`'{:,}'.format`) is resolved once at
            // compile time. Constants are immutable builtins, so the result is the same every
            // render. If the lookup fails, keep it so the error is raised at render time.
            if let Expr::Const(constant) = &value
                && let Ok(resolved) = lookup_attr(constant.bind(py), &attr)
            {
                return Ok(Expr::Const(resolved.unbind()));
            }
            Expr::Attr(Box::new(value), attr.unbind())
        }
        "Call" => {
            if !node.getattr("keywords")?.cast::<PyList>()?.is_empty() {
                return Err(PyValueError::new_err("spike: keyword arguments not supported"));
            }
            let args = node.getattr("args")?.cast::<PyList>()?.iter().map(|a| lower(&a)).collect::<PyResult<_>>()?;
            Expr::Call(Box::new(lower(&node.getattr("func")?)?), args)
        }
        "List" => {
            Expr::List(node.getattr("elts")?.cast::<PyList>()?.iter().map(|e| lower(&e)).collect::<PyResult<_>>()?)
        }
        other => return Err(PyValueError::new_err(format!("spike: unsupported node {other}"))),
    })
}

/// Everything an expression needs at render time, rebuilt per render (D2: `PyRuntime<'py>`).
pub(crate) struct Env<'py> {
    pub(crate) scope: Bound<'py, Scope>,
    pub(crate) builtins: Bound<'py, PyDict>,
    /// Bound `scope.get_name` and the `lookup_attr` function, for fallback expressions.
    pub(crate) fallback_args: Bound<'py, PyTuple>,
}

pub(crate) fn eval<'py>(expr: &Expr, env: &Env<'py>) -> PyResult<Bound<'py, PyAny>> {
    let py = env.scope.py();
    match expr {
        Expr::Name(name) => {
            let name = name.bind(py);
            if let Some(value) = Scope::lookup(&env.scope, name)? {
                return Ok(value);
            }
            env.builtins.get_item(name)?.ok_or_else(|| PyNameError::new_err(name.clone().unbind()))
        }
        Expr::Const(value) => Ok(value.bind(py).clone()),
        Expr::Attr(obj, name) => lookup_attr(&eval(obj, env)?, name.bind(py)),
        Expr::Call(func, args) => {
            let func = eval(func, env)?;
            // Optimization (b): fixed arities go through vectorcall without a temporary tuple.
            match args.as_slice() {
                [] => func.call0(),
                [a] => func.call1((eval(a, env)?,)),
                [a, b] => func.call1((eval(a, env)?, eval(b, env)?)),
                _ => {
                    let args = args.iter().map(|a| eval(a, env)).collect::<PyResult<Vec<_>>>()?;
                    func.call1(PyTuple::new(py, args)?)
                }
            }
        }
        Expr::List(items) => {
            let items = items.iter().map(|e| eval(e, env)).collect::<PyResult<Vec<_>>>()?;
            Ok(PyList::new(py, items)?.into_any())
        }
        Expr::Fallback(func) => func.bind(py).call1(&env.fallback_args),
    }
}

/// Chameleon's `lookup_attr` (utils.py:515), exactly: `getattr`; on `AttributeError` look up
/// `__getitem__` as an attribute and call it; a `KeyError` re-raises the original error.
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

/// Python-callable `lookup_attr`, for fallback expressions.
#[pyfunction]
#[pyo3(name = "lookup_attr")]
pub(crate) fn py_lookup_attr<'py>(obj: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    lookup_attr(obj, name)
}

#[derive(Clone, Copy)]
pub(crate) enum Escape {
    /// Element text: `&`, `<`, `>`, plus NUL -> `&#0;` (Chameleon's text quote is `'\0'`).
    Text,
    /// Attribute value in double quotes: `&`, `<`, `>`, `"`.
    Attr,
}

/// Chameleon's `__quote` conversion with exact type checks (D6), appended to `out`.
pub(crate) fn emit(value: &Bound<'_, PyAny>, escape: Escape, out: &mut String) -> PyResult<()> {
    if value.is_none() {
        return Ok(());
    }
    if let Ok(s) = value.cast_exact::<PyString>() {
        escape_into(s.to_str()?, escape, out);
    } else if value.is_exact_instance_of::<PyInt>() {
        // Optimization (a): Rust's i64 Display matches Python's str(int) exactly.
        match value.extract::<i64>() {
            Ok(n) => {
                use std::fmt::Write;
                let _ = write!(out, "{n}");
            }
            Err(_) => out.push_str(value.str()?.to_str()?),
        }
    } else if value.is_exact_instance_of::<PyFloat>() {
        out.push_str(value.str()?.to_str()?);
    } else if let Ok(b) = value.cast_exact::<PyBytes>() {
        escape_into(&String::from_utf8_lossy(b.as_bytes()), escape, out);
    } else if let Some(html) = value.getattr_opt(intern!(value.py(), "__html__"))? {
        out.push_str(html.call0()?.str()?.to_str()?);
    } else {
        escape_into(value.str()?.to_str()?, escape, out);
    }
    Ok(())
}

/// Chameleon's `__quote` gate (compiler.py:241): escaping happens only if the string
/// contains one of these. NUL is not in the gate.
const GATE: [bool; 256] = {
    let mut gate = [false; 256];
    gate[b'&' as usize] = true;
    gate[b'<' as usize] = true;
    gate[b'>' as usize] = true;
    gate[b'"' as usize] = true;
    gate[b'\'' as usize] = true;
    gate
};

/// Per-byte replacement classes once the gate has matched: 0 = copy, else an index into
/// `ENTITIES`. `&`, `<`, `>` always; plus the context's quote character (NUL for text, `"`
/// for double-quoted attributes; the real compiler supplies any quote via `char2entity`).
const fn replace_table(quote: u8, quote_class: u8) -> [u8; 256] {
    let mut table = [0u8; 256];
    table[b'&' as usize] = 1;
    table[b'<' as usize] = 2;
    table[b'>' as usize] = 3;
    table[quote as usize] = quote_class;
    table
}

const TEXT_TABLE: [u8; 256] = replace_table(b'\0', 5);
const ATTR_TABLE: [u8; 256] = replace_table(b'"', 4);
const ENTITIES: [&str; 6] = ["", "&amp;", "&lt;", "&gt;", "&quot;", "&#0;"];

/// Optimization (d): a gate scan, then one table-driven pass; unchanged runs are copied in
/// one `push_str`. Matches Chameleon: `'a\0b'` stays raw, `'a&\0b'` becomes `a&amp;&#0;b`.
pub(crate) fn escape_into(s: &str, escape: Escape, out: &mut String) {
    let bytes = s.as_bytes();
    if !bytes.iter().any(|&b| GATE[b as usize]) {
        out.push_str(s);
        return;
    }
    let table = match escape {
        Escape::Text => &TEXT_TABLE,
        Escape::Attr => &ATTR_TABLE,
    };
    let mut last = 0;
    for (i, &b) in bytes.iter().enumerate() {
        let class = table[b as usize];
        if class != 0 {
            // Every escaped byte is ASCII, so `last..i` lies on char boundaries.
            out.push_str(&s[last..i]);
            out.push_str(ENTITIES[class as usize]);
            last = i + 1;
        }
    }
    out.push_str(&s[last..]);
}
