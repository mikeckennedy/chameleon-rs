//! Micro-benchmarks timed inside Rust (no Python call overhead per operation). Each returns
//! nanoseconds per operation.

use std::hint::black_box;
use std::time::Instant;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};

use crate::expr::{self, Escape};
use crate::scope::Scope;

fn ns_per_op(n: u32, start: Instant) -> f64 {
    start.elapsed().as_secs_f64() * 1e9 / f64::from(n)
}

/// Experiment 1: two-level native scope lookup of `key` (hit in local or root).
#[pyfunction]
pub(crate) fn time_scope_lookup(scope: &Bound<'_, Scope>, key: &Bound<'_, PyString>, n: u32) -> PyResult<f64> {
    let start = Instant::now();
    for _ in 0..n {
        black_box(Scope::lookup(scope, key)?);
    }
    Ok(ns_per_op(n, start))
}

/// Experiment 1 (partials cost): building a fresh root `Scope` from a small dict in Rust,
/// what a native partial frame does per call.
#[pyfunction]
pub(crate) fn time_scope_new_root(vars: &Bound<'_, PyDict>, n: u32) -> PyResult<f64> {
    let py = vars.py();
    let start = Instant::now();
    for _ in 0..n {
        black_box(Scope::new_root(py, vars)?);
    }
    Ok(ns_per_op(n, start))
}

/// Experiment 1, plain-dict reference point.
#[pyfunction]
pub(crate) fn time_dict_lookup(dict: &Bound<'_, PyDict>, key: &Bound<'_, PyString>, n: u32) -> PyResult<f64> {
    let start = Instant::now();
    for _ in 0..n {
        black_box(dict.get_item(key)?);
    }
    Ok(ns_per_op(n, start))
}

/// Experiment 2: `obj.<name>` with exact `lookup_attr` semantics.
#[pyfunction]
pub(crate) fn time_lookup_attr(obj: &Bound<'_, PyAny>, name: &Bound<'_, PyString>, n: u32) -> PyResult<f64> {
    let start = Instant::now();
    for _ in 0..n {
        black_box(expr::lookup_attr(obj, name)?);
    }
    Ok(ns_per_op(n, start))
}

/// Experiment 3: calling a pre-built Python function (the fallback) with two arguments.
#[pyfunction]
pub(crate) fn time_call2(func: &Bound<'_, PyAny>, a: &Bound<'_, PyAny>, b: &Bound<'_, PyAny>, n: u32) -> PyResult<f64> {
    let start = Instant::now();
    for _ in 0..n {
        black_box(func.call1((a, b))?);
    }
    Ok(ns_per_op(n, start))
}

/// Experiment 4: escape `strings` into one buffer and build the final `str`; ns per string.
#[pyfunction]
pub(crate) fn time_escape(strings: &Bound<'_, PyList>, attr: bool, n: u32) -> PyResult<f64> {
    let escape = if attr { Escape::Attr } else { Escape::Text };
    let py = strings.py();
    let start = Instant::now();
    for _ in 0..n {
        let mut out = String::new();
        for s in strings.iter() {
            expr::emit(&s, escape, &mut out)?;
        }
        black_box(PyString::new(py, &out));
    }
    let count = u32::try_from(strings.len()).unwrap_or(u32::MAX);
    Ok(ns_per_op(n.saturating_mul(count), start))
}

/// Experiment 4: cost of the cached UTF-8 view (`PyUnicode_AsUTF8AndSize`) on fresh copies.
#[pyfunction]
pub(crate) fn utf8_view_len(s: &Bound<'_, PyString>) -> PyResult<usize> {
    Ok(s.to_str()?.len())
}

/// Experiment 4: the non-caching alternative (`PyUnicode_AsUTF8String`, a temporary bytes).
#[pyfunction]
pub(crate) fn utf8_encode_len(s: &Bound<'_, PyString>) -> PyResult<usize> {
    Ok(s.encode_utf8()?.as_bytes().len())
}

/// Experiment 7: keep values as `Bound` vs `Py` (unbind + bind back) across a store/load.
#[pyfunction]
pub(crate) fn time_bound_vs_py(values: &Bound<'_, PyList>, n: u32) -> (f64, f64) {
    let py = values.py();
    let items: Vec<Bound<'_, PyAny>> = values.iter().collect();
    let ops = n * u32::try_from(items.len()).unwrap_or(u32::MAX);

    let start = Instant::now();
    for _ in 0..n {
        let stored: Vec<Bound<'_, PyAny>> = items.iter().map(|v| black_box(v.clone())).collect();
        for v in &stored {
            black_box(v);
        }
    }
    let bound = ns_per_op(ops, start);

    let start = Instant::now();
    for _ in 0..n {
        let stored: Vec<Py<PyAny>> = items.iter().map(|v| black_box(v.clone().unbind())).collect();
        for v in &stored {
            black_box(v.bind(py));
        }
    }
    (bound, ns_per_op(ops, start))
}

/// Experiment 8: the cheapest possible native call from a Python wrapper.
#[pyfunction]
#[pyo3(signature = (**_kwargs))]
pub(crate) fn noop(_kwargs: Option<&Bound<'_, PyDict>>) {}
