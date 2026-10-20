//! The `chameleon_rs._native` Python extension module.
//!
//! This crate is the only place that knows about Python: it implements the expression
//! runtime over Python objects and exposes the template classes. Everything else lives in
//! `chameleon-core`.

use pyo3::prelude::*;

// Deliberately declared free-threading safe (PyO3's default, made explicit): every pyclass
// must be `Sync`, and all render state is per call. See docs/architecture.md, D7 and D9.
#[pymodule(gil_used = false)]
mod _native {
    use pyo3::pyfunction;

    /// Version of the Rust core, for bug reports and for checking which build is loaded.
    #[pyfunction]
    fn core_version() -> &'static str {
        chameleon_core::VERSION
    }
}
