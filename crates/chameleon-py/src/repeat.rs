// Ported from Chameleon's src/chameleon/tal.py (`RepeatDict`, and the `RepeatItem` descriptors
// `index`, `number`, `start`, `end`, `odd`, `even`, `parity`), modified: `RepeatDict` is a native
// class that also gives each `RepeatItem` its items (for `first` and `last`), and the descriptors are computed natively for the exact `RepeatItem` class of
// `chameleon_rs.tal`, with the same results and types.

//! The `repeat` variable without Python frames (Phase 10).
//!
//! `chameleon_rs.tal.RepeatDict` is [`RepeatDict`]: Chameleon's class keeps the dict's bound
//! `__getitem__` and `__setitem__` per instance, and each bound method holds a reference to the
//! slot descriptor of `dict`, which every thread's instances share. A render makes one per
//! looping partial, so on free-threaded Python those references contend; this class holds the
//! dict itself. `RepeatItem` stays a Python class; attribute access on an *exact* instance of it
//! is computed here, exactly as its Python code computes it. Anything else (a subclass, a name
//! the class defines, a missing key, an iterator without `__length_hint__`) returns `None`, and
//! the caller takes the ordinary `getattr` path, which then runs the Python code itself.

use pyo3::exceptions::{PyAttributeError, PyKeyError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyType};
use pyo3::{PyTraverseError, PyVisit};

/// Chameleon's `RepeatDict`: a proxy writing to and reading from the mapping it was made with.
///
/// ```python
/// repeat = RepeatDict({})
/// iterator, length = repeat('numbers', range(5))   # registers a RepeatItem
/// repeat['numbers'] is repeat.numbers
/// ```
#[pyclass(frozen, subclass, module = "chameleon_rs.tal")]
pub(crate) struct RepeatDict {
    d: Py<PyAny>,
}

impl RepeatDict {
    /// A fresh `RepeatDict({})`, as `PageTemplate.render` provides.
    pub(crate) fn empty(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
        Ok(Bound::new(py, Self { d: PyDict::new(py).into_any().unbind() })?.into_any())
    }

    /// `self(key, iterable)` for an exact or derived instance:
    ///
    /// ```python
    /// iterable = list(iterable) if iterable is not None else ()
    /// length = len(iterable)
    /// iterator = iter(iterable)
    /// self[key] = RepeatItem(iterator, length, iterable)   # Chameleon: RepeatItem(iterator, length)
    /// return iterator, length
    /// ```
    pub(crate) fn register<'py>(
        slf: &Bound<'py, Self>,
        key: &Bound<'py, PyAny>,
        iterable: &Bound<'py, PyAny>,
    ) -> PyResult<(Bound<'py, PyAny>, usize)> {
        let py = slf.py();
        let items = if iterable.is_none() {
            PyTuple::empty(py).into_any()
        } else {
            py.get_type::<PyList>().call1((iterable,))?
        };
        let length = items.len()?;
        let iterator = items.try_iter()?.into_any();
        let item = classes(py)?.item.bind(py).call1((&iterator, length, &items))?;
        if slf.is_exact_instance_of::<Self>() {
            slf.get().d.bind(py).set_item(key, item)?;
        } else {
            slf.as_any().set_item(key, item)?;
        }
        Ok((iterator, length))
    }
}

#[pymethods]
impl RepeatDict {
    #[new]
    fn new(d: Bound<'_, PyAny>) -> PyResult<Self> {
        // Chameleon binds `d.__setitem__` and `d.__getitem__` here, so a mapping without them
        // fails now, not on first use.
        if !d.is_exact_instance_of::<PyDict>() {
            d.getattr(intern!(d.py(), "__setitem__"))?;
            d.getattr(intern!(d.py(), "__getitem__"))?;
        }
        Ok(Self { d: d.unbind() })
    }

    fn __getitem__<'py>(&self, py: Python<'py>, key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.d.bind(py).get_item(key)
    }

    fn __setitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        self.d.bind(py).set_item(key, value)
    }

    /// `self[key]`, a missing key raising `AttributeError(key)`.
    fn __getattr__<'py>(slf: &Bound<'py, Self>, key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        match slf.as_any().get_item(key) {
            Err(e) if e.is_instance_of::<PyKeyError>(slf.py()) => {
                Err(PyAttributeError::new_err((key.clone().unbind(),)))
            }
            other => other,
        }
    }

    fn __call__<'py>(
        slf: &Bound<'py, Self>,
        key: &Bound<'py, PyAny>,
        iterable: &Bound<'py, PyAny>,
    ) -> PyResult<(Bound<'py, PyAny>, usize)> {
        Self::register(slf, key, iterable)
    }

    /// `copy.copy` shares the mapping, as copying Chameleon's bound methods does.
    fn __reduce__<'py>(slf: &Bound<'py, Self>) -> (Bound<'py, PyType>, (Py<PyAny>,)) {
        (slf.get_type(), (slf.get().d.clone_ref(slf.py()),))
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.d)
    }
}

/// Whether `value`'s type is exactly `ty`, without taking a reference to the type (PyO3's
/// `get_type()` does). Types of user objects are shared by every thread, so on free-threaded
/// Python that reference is an atomic operation that contends, on every attribute lookup.
pub(crate) fn is_type(value: &Bound<'_, PyAny>, ty: &Bound<'_, PyAny>) -> bool {
    std::ptr::eq(value.get_type_ptr().cast::<pyo3::ffi::PyObject>(), ty.as_ptr())
}

struct Classes {
    item: Py<PyAny>,
    int: Py<PyAny>,
    str: Py<PyAny>,
}

static CLASSES: PyOnceLock<Classes> = PyOnceLock::new();

fn classes(py: Python<'_>) -> PyResult<&Classes> {
    CLASSES.get_or_try_init(py, || {
        let tal = py.import(intern!(py, "chameleon_rs.tal"))?;
        let utils = py.import(intern!(py, "chameleon_rs.utils"))?;
        Ok::<_, PyErr>(Classes {
            item: tal.getattr(intern!(py, "RepeatItem"))?.unbind(),
            int: utils.getattr(intern!(py, "callableint"))?.unbind(),
            str: utils.getattr(intern!(py, "callablestr"))?.unbind(),
        })
    })
}

/// `getattr(obj, name)` for an exact `RepeatDict` or `RepeatItem`, when computed natively.
pub(crate) fn attr<'py>(obj: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Option<Bound<'py, PyAny>>> {
    if let Ok(repeat) = obj.cast_exact::<RepeatDict>() {
        return dict_attr(repeat, name);
    }
    let py = obj.py();
    let c = classes(py)?;
    if is_type(obj, c.item.bind(py)) {
        return item_attr(obj, name, c);
    }
    Ok(None)
}

/// `RepeatDict.__getattr__`: `self[key]`. Only reached in Python for names the class does not
/// define, which all start with `_`; a missing key goes the Python way (its `AttributeError`).
fn dict_attr<'py>(repeat: &Bound<'py, RepeatDict>, name: &Bound<'py, PyString>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = repeat.py();
    if name.to_str().map_or(true, |n| n.starts_with('_')) {
        return Ok(None);
    }
    match repeat.get().d.bind(py).get_item(name) {
        Ok(value) => Ok(Some(value)),
        Err(e) if e.is_instance_of::<PyKeyError>(py) => Ok(None),
        Err(e) => Err(e),
    }
}

/// The `RepeatItem` descriptors:
///
/// ```python
/// index = length - _iterator.__length_hint__() - 1     # callableint
/// number = index + 1; start = index == 0; end = index == length - 1   # callableint
/// odd = (index % 2 == 1 and 'odd') or ''; even = (index % 2 == 0 and 'even') or ''
/// parity = (index % 2 == 0 and 'even') or 'odd'        # callablestr
/// ```
fn item_attr<'py>(
    obj: &Bound<'py, PyAny>,
    name: &Bound<'py, PyString>,
    c: &Classes,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = obj.py();
    let Ok(which) = name.to_str() else { return Ok(None) };
    if !matches!(which, "index" | "number" | "start" | "end" | "odd" | "even" | "parity") {
        return Ok(None);
    }
    let iterator = obj.getattr(intern!(py, "_iterator"))?;
    // `__length_hint__` missing: the Python descriptor's `len(tuple(copy.copy(...)))` path.
    let Some(hint) = iterator.getattr_opt(intern!(py, "__length_hint__"))? else { return Ok(None) };
    let remaining = match hint.call0() {
        Ok(remaining) => remaining,
        Err(e) if e.is_instance_of::<PyAttributeError>(py) => return Ok(None),
        Err(e) => return Err(e),
    };
    let length = obj.getattr(intern!(py, "length"))?;
    let index = length.sub(remaining)?.sub(1)?;
    let int = |v: Bound<'py, PyAny>| c.int.bind(py).call1((v,));
    let text = |s: &Bound<'py, PyString>| c.str.bind(py).call1((s,));
    let half = |rest: i64| -> PyResult<bool> { index.rem(2)?.rich_compare(rest, CompareOp::Eq)?.is_truthy() };
    let empty = intern!(py, "");
    let value = match which {
        "index" => int(index.clone())?,
        "number" => int(index.add(1)?)?,
        "start" => int(index.rich_compare(0, CompareOp::Eq)?)?,
        "end" => int(index.rich_compare(length.sub(1)?, CompareOp::Eq)?)?,
        "odd" => text(if half(1)? { intern!(py, "odd") } else { empty })?,
        "even" => text(if half(0)? { intern!(py, "even") } else { empty })?,
        _ => text(if half(0)? { intern!(py, "even") } else { intern!(py, "odd") })?,
    };
    Ok(Some(value))
}

/// Whether `value` is an exact `callableint` or `callablestr` (what the `RepeatItem`
/// descriptors return): types with no `__html__`, `default` or `mapping` attribute, so their
/// conversion with the default translation function is `str(value)`.
pub(crate) fn is_plain_callable(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = value.py();
    let c = classes(py)?;
    Ok(is_type(value, c.int.bind(py)) || is_type(value, c.str.bind(py)))
}
