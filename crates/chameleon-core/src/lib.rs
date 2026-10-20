//! Pure-Rust core of chameleon-rs.
//!
//! This crate knows nothing about Python. It owns everything that can be expressed without
//! a Python interpreter: tokenizing and parsing template source, building the program from
//! TAL/METAL/i18n attributes, escaping, and the output buffer. Expression *evaluation* is
//! abstracted behind a runtime trait implemented by the `chameleon-py` crate.
//!
//! See `docs/architecture.md` for the decisions behind this split.

/// The version of this crate, reported by the Python extension for diagnostics.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
