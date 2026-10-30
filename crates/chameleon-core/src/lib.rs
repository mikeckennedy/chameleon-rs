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
mod i18n;
mod interpolate;
#[cfg(feature = "markdown")]
pub mod markdown;
mod nodes;
mod options;
mod output;
mod parser;
mod pychars;
mod pychars_table;
mod pyre;
mod resolve;
mod source;
pub mod stack;
pub mod surrogates;
mod tal;
mod tok;
mod tokenize;
mod vm;
mod zpt;

pub use compile::{compile, compile_tokens};
pub use error::{CompileError, ErrToken, ErrorKind};
pub use escape::{Escape, Quote, escape_into, needs_escape};
pub use expr::{DefaultAlias, Engine, ExprCompileError, ExprCompiler, ExprContext, ExprId, FreeVars, StaticAttrs};
pub use options::{Mode, Options};
pub use output::{Mark, Output};
pub use pychars::is_space as is_python_space;
pub use source::{Source, Span, char_offset, location};
pub use tok::{Tok, TokPos};
pub use vm::{
    Active, AttrKind, Attrs, Call, Countdown, DEFAULT_FRAME_BUDGET, Done, Emitted, ErrorSite, Errors, Eval, Fill, I18n,
    I18nKey, Loop, Loops, MAX_CAPACITY_HINT, MAX_FRAMES, MacroEntry, MacroStep, Macros, Msgid, NameId, NameValue,
    Names, Op, Program, Runtime, Stage, Step, TextRef, Translate, Translation, Values, Visibility, VmError, VmState,
    run, throw,
};

/// Tokens, element trees and character classes, exposed for the differential tests in
/// `chameleon_rs._native._testing` (compared against Chameleon's `iter_xml` and
/// `ElementParser`). Not part of the rendering API.
pub mod testing {
    pub use crate::compile::{build_tree, free_variables};
    pub use crate::nodes::{Tree, dump as dump_tree, inventory};
    pub use crate::parser::{
        Attr, AttrKind, Element, Item, Namespaces, NsAttr, NsValue, ParseError, ParseErrorKind, ParseOptions, Tag,
        parse,
    };
    pub use crate::pychars::Class;
    pub use crate::source::check_len;
    pub use crate::tokenize::{pattern, tokenize, tokenize_text};

    /// [`parse`] with its error converted the way [`crate::compile`] converts it.
    pub fn parse_tokens<E>(
        src: &str,
        tokens: &[crate::Span],
        options: &ParseOptions,
    ) -> Result<Vec<Item>, crate::CompileError<E>> {
        if !crate::source::check_len(src) {
            return Err(crate::CompileError::TooLarge);
        }
        Ok(parse(src, tokens, options)?)
    }
}

/// The version of this crate, reported by the Python extension for diagnostics.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

// Compiled programs and parser options are shared across threads (frozen pyclasses under
// free-threaded Python), so they must stay `Send + Sync`.
const _: () = {
    const fn assert_send_sync<T: Send + Sync>() {}
    assert_send_sync::<Program>();
    assert_send_sync::<testing::ParseOptions>();
    assert_send_sync::<testing::Item>();
};

#[cfg(test)]
mod tests;
