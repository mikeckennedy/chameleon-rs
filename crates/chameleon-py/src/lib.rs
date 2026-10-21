//! The `chameleon_rs._native` Python extension module.
//!
//! This crate is the only place that knows about Python: it implements the expression
//! compiler (D1, D4) and the runtime (D2) over Python objects and exposes the native
//! classes. Everything else lives in `chameleon-core`.

mod compiler;
mod convert;
mod custom;
mod errors;
mod expr;
mod lower;
mod program;
mod runtime;
mod scope;
mod strexpr;
mod testing;

use pyo3::prelude::*;

// Deliberately declared free-threading safe (PyO3's default, made explicit): every pyclass
// is frozen and `Sync`, and all render state is per call. See docs/architecture.md, D7, D9.
#[pymodule(gil_used = false)]
mod _native {
    use pyo3::prelude::*;

    #[pymodule_export]
    use super::custom::SubEval;
    #[pymodule_export]
    use super::expr::py_lookup_attr;
    #[pymodule_export]
    use super::program::{DefaultMarker, Program};
    #[pymodule_export]
    use super::scope::Scope;

    /// Version of the Rust core, for bug reports and for checking which build is loaded.
    #[pyfunction]
    fn core_version() -> &'static str {
        chameleon_core::VERSION
    }

    /// Tokens and element trees for the differential tests (see `testing.rs`).
    #[pymodule(gil_used = false)]
    mod _testing {
        #[pymodule_export]
        use crate::testing::{
            char_class, fast_path_coverage, free_variables, inventory, parse, parse_count, token_count,
            tokenizer_pattern, tokens, tree,
        };
    }

    #[pymodule_init]
    fn init(m: &Bound<'_, PyModule>) -> PyResult<()> {
        // CPython names a type in messages (`unsupported operand type(s) for +: 'Scope' and
        // 'int'`) by its `tp_name`, which for a native class includes the module. Setting
        // `__name__` rewrites it to what Chameleon's classes print (its scope is the Python
        // class `Scope`, its marker an `ImportableMarker`); `__module__` and `__qualname__`
        // are unchanged.
        m.getattr("Scope")?.setattr("__name__", "Scope")?;
        m.getattr("DefaultMarker")?.setattr("__name__", "ImportableMarker")?;
        m.add("DEFAULT_MARKER", super::program::marker(m.py())?)
    }
}
