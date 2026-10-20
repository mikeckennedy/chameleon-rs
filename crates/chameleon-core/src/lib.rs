//! Pure-Rust core of chameleon-rs.
//!
//! This crate knows nothing about Python. It owns everything that can be expressed without
//! a Python interpreter: tokenizing and parsing template source, building the program from
//! TAL attributes, lowering it to bytecode, escaping, the output buffer, and the VM.
//! Expressions are parsed by an injected [`ExprCompiler`] at compile time and evaluated by
//! an injected [`Runtime`] at render time; `chameleon-py` implements both over Python.
//!
//! See `docs/architecture.md` (D1-D3, D6) for the decisions behind this split.

mod compile;
mod entities;
mod entities_table;
mod error;
mod escape;
mod expr;
mod interpolate;
mod nodes;
mod output;
mod parser;
mod pychars;
mod source;
mod tal;
mod tokenize;
mod vm;
mod zpt;

pub use compile::compile;
pub use error::{CompileError, ErrorKind};
pub use escape::{Escape, Quote, escape_into, needs_escape};
pub use expr::{ExprCompileError, ExprCompiler, ExprContext, ExprId};
pub use output::{Mark, Output};
pub use pychars::is_space as is_python_space;
pub use source::{Span, char_offset, location};
pub use vm::{
    Call, Countdown, Done, Emitted, Loop, MAX_CAPACITY_HINT, MAX_FRAMES, NameId, Op, Program, Runtime, Step, TextRef,
    VmError, VmState, run,
};

/// The version of this crate, reported by the Python extension for diagnostics.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(test)]
mod tests;
