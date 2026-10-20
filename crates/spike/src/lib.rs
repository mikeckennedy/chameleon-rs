//! Phase 1 spike: cost-model experiments for `docs/spike-1.md`. Throwaway code, never
//! shipped (not in the workspace's default members). Driven by `bench/spike1.py`.

mod expr;
mod micro;
mod program;
mod scope;
mod vm;

use pyo3::prelude::*;

#[pymodule(gil_used = false)]
mod chameleon_spike {
    #[pymodule_export]
    use super::expr::py_lookup_attr;
    #[pymodule_export]
    use super::micro::{
        noop, time_bound_vs_py, time_call2, time_dict_lookup, time_escape, time_lookup_attr, time_scope_lookup,
        time_scope_new_root, utf8_encode_len, utf8_view_len,
    };
    #[pymodule_export]
    use super::program::{Program, RenderAwaitable};
    #[pymodule_export]
    use super::scope::Scope;
}
