//! `chameleon_rs._native._testing`: the core's tokens and element trees as Python data, in a
//! shape the differential tests compare directly with Chameleon's `iter_xml` and
//! `ElementParser` output (normalized the same way on the Python side; see `elements.rs`).

use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use chameleon_core::Source;
use chameleon_core::testing::{Class, ParseOptions, parse_tokens, pattern, tokenize};

use crate::elements::{Conv, parse_spans, spans};
use crate::errors::compile_error;

static SLOT_ERROR_SITES_LIKE_CHAMELEON: AtomicBool = AtomicBool::new(false);

/// Records errors through slot bodies as Chameleon does (see
/// `Errors::slot_error_sites_like_chameleon`), for every render in the process; returns the
/// previous setting. For the differential tests, which compare error messages with Chameleon's.
#[pyfunction]
pub(crate) fn set_slot_error_sites_like_chameleon(on: bool) -> bool {
    SLOT_ERROR_SITES_LIKE_CHAMELEON.swap(on, Ordering::Relaxed)
}

pub(crate) fn slot_error_sites_like_chameleon() -> bool {
    SLOT_ERROR_SITES_LIKE_CHAMELEON.load(Ordering::Relaxed)
}

/// `[(token, pos), ...]` like `[(str(t), t.pos) for t in iter_xml(body)]`. `mode` is `xml` or
/// `text` (`iter_text`).
#[pyfunction]
#[pyo3(signature = (body, mode = "xml"))]
pub(crate) fn tokens<'py>(py: Python<'py>, body: &str, mode: &str) -> PyResult<Bound<'py, PyList>> {
    let conv = Conv { py, src: body, source: Source::new(body) };
    let list = PyList::empty(py);
    for span in spans(body, mode)? {
        list.append(conv.tok(span)?)?;
    }
    Ok(list)
}

/// `list(ElementParser(iter_xml(body), namespaces, restricted_namespace))`, normalized (see
/// `elements.rs`). Errors are raised as compiling would raise them.
#[pyfunction]
#[pyo3(signature = (body, namespaces, restricted_namespace = true, mode = "xml"))]
pub(crate) fn parse<'py>(
    py: Python<'py>,
    body: &str,
    namespaces: &Bound<'py, PyDict>,
    restricted_namespace: bool,
    mode: &str,
) -> PyResult<Bound<'py, PyList>> {
    parse_spans(py, body, &spans(body, mode)?, namespaces, restricted_namespace)
}

fn build_tree(
    py: Python<'_>,
    body: &str,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<chameleon_core::testing::Tree> {
    let options = crate::program::options(options)?;
    chameleon_core::testing::build_tree(body, &options).map_err(|e| {
        let e = match e {
            chameleon_core::CompileError::Template { kind, message, token } => {
                chameleon_core::CompileError::Template { kind, message, token }
            }
            chameleon_core::CompileError::TooLarge => chameleon_core::CompileError::TooLarge,
            chameleon_core::CompileError::TooDeep(e) => chameleon_core::CompileError::TooDeep(e),
            chameleon_core::CompileError::Expr(never) => match never {},
        };
        compile_error(py, e, body, "<string>")
    })
}

/// The program tree of `body` as the builder makes it (see `chameleon_core::testing::dump_tree`),
/// for snapshots and differential tests; the keywords are `Program`'s options. Builder errors
/// are raised as compiling raises them (expressions are not compiled).
#[pyfunction]
#[pyo3(signature = (body, **options))]
pub(crate) fn tree(py: Python<'_>, body: &str, options: Option<&Bound<'_, PyDict>>) -> PyResult<String> {
    let tree = build_tree(py, body, options)?;
    Ok(chameleon_core::testing::dump_tree(&tree, body))
}

/// `[(kind, text, pos), ...]`: every expression and assigned name of the tree (see
/// `chameleon_core::testing::inventory`).
#[pyfunction]
#[pyo3(signature = (body, **options))]
pub(crate) fn inventory(
    py: Python<'_>,
    body: &str,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<(&'static str, String, Option<usize>)>> {
    let tree = build_tree(py, body, options)?;
    Ok(chameleon_core::testing::inventory(&tree, body))
}

/// `collector.res[name]` as built by the core (Python syntax, before translation).
#[pyfunction]
pub(crate) fn tokenizer_pattern(name: &str) -> Option<String> {
    pattern(name)
}

/// Every character the core's version of Python's `\s`, `\d` or `\w` matches.
#[pyfunction]
pub(crate) fn char_class(name: &str) -> PyResult<String> {
    let class = match name {
        "s" => Class::Space,
        "d" => Class::Digit,
        "w" => Class::Word,
        _ => return Err(PyValueError::new_err(format!("unknown class {name:?}"))),
    };
    Ok(class.chars().collect())
}

/// Tokenizes and parses `body` with the ZPT namespaces and returns the number of top-level
/// items, without building Python objects: for timing the tokenizer and parser alone.
#[pyfunction]
pub(crate) fn parse_count(py: Python<'_>, body: &str) -> PyResult<usize> {
    let tokens = tokenize(body);
    let items =
        parse_tokens(body, &tokens, &ParseOptions::default()).map_err(|e| compile_error(py, e, body, "<string>"))?;
    Ok(items.len())
}

/// The number of tokens in `body`, for timing the tokenizer alone.
#[pyfunction]
pub(crate) fn token_count(body: &str) -> usize {
    tokenize(body).len()
}

/// `[(text, reads, writes), ...]`: each expression site of `body` in tree order with the
/// names it reads and writes (D9), as the resolve table records them; `reads` and `writes`
/// are `None` when the expression is dynamic (it uses `econtext`, a custom type, ...). The
/// keywords are `Program`'s options (`PageTemplate`'s expression types).
#[pyfunction]
#[pyo3(signature = (body, **options))]
#[allow(clippy::type_complexity)] // the Python shape
pub(crate) fn free_variables(
    py: Python<'_>,
    body: &str,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<(String, Option<Vec<String>>, Option<Vec<String>>)>> {
    let options = crate::program::options(options)?;
    let marker = crate::program::marker(py)?;
    let mut compiler =
        crate::compiler::PyExprCompiler::new(py, marker, crate::compiler::Config::page_template(), body, "<string>")?;
    let sites = chameleon_core::testing::free_variables(body, &mut compiler, &options)
        .map_err(|e| compile_error(py, e, body, "<string>"))?;
    let names = |v: Vec<Box<str>>| v.into_iter().map(String::from).collect();
    Ok(sites
        .into_iter()
        .map(|(text, free)| match free {
            chameleon_core::FreeVars::Dynamic => (text, None, None),
            chameleon_core::FreeVars::Known { reads, writes } => (text, Some(names(reads)), Some(names(writes))),
        })
        .collect())
}

/// `(fast, fallback)`: how many of the compiled expressions of `body` run entirely on the
/// native fast path, and how many contain a fallback function or a custom type (D4's
/// coverage). `load` adds file templates' `load:` type. Compiles only (no lowering), so
/// templates with features the VM does not run yet are counted too.
#[pyfunction]
#[pyo3(signature = (body, load = false, **options))]
pub(crate) fn fast_path_coverage(
    py: Python<'_>,
    body: &str,
    load: bool,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<(usize, usize)> {
    let options = crate::program::options(options)?;
    let marker = crate::program::marker(py)?;
    let mut config = crate::compiler::Config::page_template();
    if load {
        config.types.insert(
            "load".to_owned(),
            crate::compiler::TypeSpec::Proxy { name: "__loader".to_owned(), ignore_prefix: false },
        );
        config.template_builtins.push("__loader".to_owned());
    }
    let mut compiler = crate::compiler::PyExprCompiler::new(py, marker, config, body, "<string>")?;
    chameleon_core::testing::free_variables(body, &mut compiler, &options)
        .map_err(|e| compile_error(py, e, body, "<string>"))?;
    let fallback = compiler.exprs.iter().filter(|e| e.uses_fallback()).count();
    Ok((compiler.exprs.len() - fallback, fallback))
}
