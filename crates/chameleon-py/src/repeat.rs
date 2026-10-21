// Ported from Chameleon's src/chameleon/tal.py (`RepeatDict.__getattr__` and the `RepeatItem`
// descriptors `index`, `number`, `start`, `end`, `odd`, `even`, `parity`), modified: computed
// natively for the exact classes of `chameleon_rs.tal`, with the same results and types.

//! `repeat.name.index` and friends without Python frames (Phase 10).
//!
//! `chameleon_rs.tal.RepeatDict` and `RepeatItem` stay Python classes (their identity and
//! behaviour are Chameleon's); attribute access on an *exact* instance of either is computed
//! here, exactly as their Python code computes it. Anything else (a subclass, a name the class
//! defines, a missing key, an iterator without `__length_hint__`) returns `None`, and the
//! caller takes the ordinary `getattr` path, which then runs the Python code itself.

use pyo3::exceptions::{PyAttributeError, PyKeyError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyString;

struct Classes {
    dict: Py<PyAny>,
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
            dict: tal.getattr(intern!(py, "RepeatDict"))?.unbind(),
            item: tal.getattr(intern!(py, "RepeatItem"))?.unbind(),
            int: utils.getattr(intern!(py, "callableint"))?.unbind(),
            str: utils.getattr(intern!(py, "callablestr"))?.unbind(),
        })
    })
}

/// `getattr(obj, name)` for an exact `RepeatDict` or `RepeatItem`, when computed natively.
pub(crate) fn attr<'py>(obj: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = obj.py();
    let ty = obj.get_type();
    let c = classes(py)?;
    if ty.is(c.dict.bind(py)) {
        return dict_attr(obj, name);
    }
    if ty.is(c.item.bind(py)) {
        return item_attr(obj, name, c);
    }
    Ok(None)
}

/// `RepeatDict.__getattr__`: `self[key]`. Only reached in Python for names the class does not
/// define, which all start with `_`; a missing key goes the Python way (its `AttributeError`).
fn dict_attr<'py>(obj: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = obj.py();
    if name.to_str().map_or(true, |n| n.starts_with('_')) {
        return Ok(None);
    }
    match obj.get_item(name) {
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
    let ty = value.get_type();
    Ok(ty.is(c.int.bind(py)) || ty.is(c.str.bind(py)))
}
