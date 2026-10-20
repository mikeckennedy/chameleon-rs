//! Experiment 1 (D5): a native two-level `Scope`, a dict subclass with Chameleon's lookup
//! rules (local dict first, then the shared root dict), compared with Chameleon's Python
//! `Scope.get_name`.

use pyo3::exceptions::PyNameError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};

#[pyclass(extends = PyDict, frozen, module = "chameleon_spike")]
pub(crate) struct Scope {
    /// The root dict holding globals. `None` for the root scope itself.
    root: Option<Py<PyDict>>,
}

impl Scope {
    /// Two-level lookup with direct dict access (no Python method dispatch).
    pub(crate) fn lookup<'py>(
        slf: &Bound<'py, Self>,
        key: &Bound<'py, PyString>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        if let Some(value) = slf.as_super().get_item(key)? {
            return Ok(Some(value));
        }
        match &slf.get().root {
            Some(root) => root.bind(slf.py()).get_item(key),
            None => Ok(None),
        }
    }

    pub(crate) fn new_root<'py>(py: Python<'py>, vars: &Bound<'py, PyDict>) -> PyResult<Bound<'py, Self>> {
        let scope = Bound::new(py, Self { root: None })?;
        scope.as_super().update(vars.as_mapping())?;
        Ok(scope)
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
        // dict.__init__(self, *args, **kwargs). (super(dict, self) would skip dict itself.)
        let mut init_args = vec![slf.clone().into_any()];
        init_args.extend(args.iter());
        slf.py().get_type::<PyDict>().getattr("__init__")?.call(PyTuple::new(slf.py(), init_args)?, kwargs)?;
        Ok(())
    }

    fn get_name<'py>(slf: &Bound<'py, Self>, key: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
        Self::lookup(slf, key)?.ok_or_else(|| PyNameError::new_err(key.clone().unbind()))
    }

    /// Chameleon's `Scope.copy()`: a new scope with this scope's local items whose root is
    /// this scope's root (or this scope, if it is the root).
    fn copy<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, Self>> {
        let py = slf.py();
        let root = match &slf.get().root {
            Some(root) => root.clone_ref(py),
            None => slf.as_super().clone().unbind(),
        };
        let copy = Bound::new(py, Self { root: Some(root) })?;
        copy.as_super().update(slf.as_super().as_mapping())?;
        Ok(copy)
    }
}
