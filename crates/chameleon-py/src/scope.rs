// Ported from Chameleon's src/chameleon/utils.py (`Scope`), modified: a native dict subclass
// whose root dict is a Rust field (D5).

//! The template scope: a two-level mapping, the local dict first, then a shared root.

use pyo3::exceptions::{PyKeyError, PyNameError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

static SCOPE_ITER: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

#[pyclass(extends = PyDict, frozen, module = "chameleon_rs._native")]
pub(crate) struct Scope {
    /// The root dict holding globals. `None` for a root scope.
    root: Option<Py<PyDict>>,
}

impl Scope {
    /// Two-level lookup with direct dict access (no Python method dispatch).
    #[inline]
    pub(crate) fn lookup<'py>(slf: &Bound<'py, Self>, key: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        if let Some(value) = slf.as_super().get_item(key)? {
            return Ok(Some(value));
        }
        match &slf.get().root {
            Some(root) => root.bind(slf.py()).get_item(key),
            None => Ok(None),
        }
    }

    /// A new root scope holding a copy of `vars` (what `Scope(**kw)` does).
    pub(crate) fn new_root<'py>(py: Python<'py>, vars: &Bound<'py, PyDict>) -> PyResult<Bound<'py, Self>> {
        let scope = Bound::new(py, Self { root: None })?;
        scope.as_super().update(vars.as_mapping())?;
        Ok(scope)
    }

    /// An empty root scope.
    pub(crate) fn empty(py: Python<'_>) -> PyResult<Bound<'_, Self>> {
        Bound::new(py, Self { root: None })
    }

    fn root_or_self<'py>(slf: &Bound<'py, Self>) -> Bound<'py, PyDict> {
        match &slf.get().root {
            Some(root) => root.bind(slf.py()).clone(),
            None => slf.as_super().clone(),
        }
    }
}

#[pymethods]
impl Scope {
    #[new]
    #[pyo3(signature = (*_args, **_kwargs))]
    fn __new__(_args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>) -> Self {
        Self { root: None }
    }

    #[pyo3(signature = (*args, **kwargs))]
    fn __init__(slf: &Bound<'_, Self>, args: &Bound<'_, PyTuple>, kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        // dict.__init__(self, *args, **kwargs); super(dict, self) would skip dict itself.
        let mut init_args = vec![slf.clone().into_any()];
        init_args.extend(args.iter());
        slf.py().get_type::<PyDict>().getattr("__init__")?.call(PyTuple::new(slf.py(), init_args)?, kwargs)?;
        Ok(())
    }

    fn __getitem__<'py>(slf: &Bound<'py, Self>, key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        Self::lookup(slf, key)?.ok_or_else(|| PyKeyError::new_err((key.clone().unbind(),)))
    }

    fn __contains__(slf: &Bound<'_, Self>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        Ok(Self::lookup(slf, key)?.is_some())
    }

    /// Local keys, then root keys not shadowed locally, lazily (`chameleon_rs.utils.scope_iter`,
    /// Chameleon's generator): changing the scope while iterating it raises `RuntimeError`.
    fn __iter__<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        let py = slf.py();
        let iter = SCOPE_ITER
            .get_or_try_init(py, || Ok::<_, PyErr>(py.import("chameleon_rs.utils")?.getattr("scope_iter")?.unbind()))?;
        let root = slf.get().root.as_ref().map(|r| r.bind(py).clone());
        iter.bind(py).call1((slf, root))
    }

    #[pyo3(signature = (key, default = None))]
    fn get<'py>(
        slf: &Bound<'py, Self>,
        key: &Bound<'py, PyAny>,
        default: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        Ok(match Self::lookup(slf, key)? {
            Some(value) => value,
            None => default.unwrap_or_else(|| slf.py().None().into_bound(slf.py())),
        })
    }

    fn get_name<'py>(slf: &Bound<'py, Self>, key: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
        Self::lookup(slf, key.as_any())?.ok_or_else(|| PyNameError::new_err((key.clone().unbind(),)))
    }

    #[getter]
    fn vars<'py>(slf: &Bound<'py, Self>) -> Bound<'py, Self> {
        slf.clone()
    }

    /// A new scope with this scope's local items whose root is this scope's root (or this
    /// scope, if it is the root).
    pub(crate) fn copy<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, Self>> {
        let root = Self::root_or_self(slf).unbind();
        let copy = Bound::new(slf.py(), Self { root: Some(root) })?;
        // `Scope(self)`: `dict.__init__` on a dict subclass with its own `__iter__` takes the
        // slow path, `self[key]` for each local key in order, which (local first) is the
        // local value: the same as copying the local items directly.
        // (A snapshot of the local items, `PyDict_Items`: PyO3's dict iterator panics if the
        // dict changes size, which a key's `__hash__` or another thread can do; `dict.update`
        // would take the subclass's `__iter__`, and copy the root's keys too.)
        let target = copy.as_super();
        for item in slf.as_super().items().iter() {
            let (key, value): (Bound<'py, PyAny>, Bound<'py, PyAny>) = item.extract()?;
            target.set_item(key, value)?;
        }
        Ok(copy)
    }

    fn set_local(slf: &Bound<'_, Self>, name: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        slf.as_super().set_item(name, value)
    }

    fn set_global(slf: &Bound<'_, Self>, name: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        Self::root_or_self(slf).set_item(name, value)
    }

    #[pyo3(name = "setLocal")]
    fn set_local_camel(slf: &Bound<'_, Self>, name: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        Self::set_local(slf, name, value)
    }

    #[pyo3(name = "setGlobal")]
    fn set_global_camel(slf: &Bound<'_, Self>, name: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        Self::set_global(slf, name, value)
    }

    /// Visits the root dict. The dict items themselves are traversed (and cleared, which
    /// breaks cycles) by the inherited dict slots; a frozen class has no `__clear__`.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(root) = &self.root {
            visit.call(root)?;
        }
        Ok(())
    }
}
