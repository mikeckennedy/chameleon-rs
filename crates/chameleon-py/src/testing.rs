//! `chameleon_rs._native._testing`: the core's tokens and element trees as Python data, in a
//! shape the differential tests compare directly with Chameleon's `iter_xml` and
//! `ElementParser` output (normalized the same way on the Python side).
//!
//! A token (Chameleon's `Token`, a `str` with a position) becomes `(text, pos)`, with `pos`
//! in code points; a plain string Chameleon creates without a position becomes
//! `(text, None)`. Items are `(kind, args)` tuples as `ElementParser` yields them; a tag is
//! a dict with Chameleon's keys, where `ns_attrs` is a list of
//! `((namespace, (name, pos)), value)` pairs in order and `ns_map` maps prefixes to tokens.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use chameleon_core::testing::{
    Attr, AttrKind, Class, Item, Namespaces, NsValue, ParseOptions, Tag, parse_tokens, pattern, tokenize, tokenize_text,
};
use chameleon_core::{Source, Span};

use crate::errors::compile_error;

struct Conv<'py, 's> {
    py: Python<'py>,
    src: &'s str,
    source: Source<'s>,
}

impl<'py> Conv<'py, '_> {
    fn tok(&self, span: Span) -> PyResult<Bound<'py, PyTuple>> {
        let pos = self.source.char_offset(span.start as usize);
        (span.text(self.src), pos).into_pyobject(self.py)
    }

    fn plain(&self, text: &str) -> PyResult<Bound<'py, PyTuple>> {
        (text, self.py.None()).into_pyobject(self.py)
    }

    fn opt(&self, span: Option<Span>) -> PyResult<Bound<'py, PyAny>> {
        match span {
            Some(s) => Ok(self.tok(s)?.into_any()),
            None => Ok(self.py.None().into_bound(self.py)),
        }
    }

    fn ns_value(&self, v: &NsValue) -> PyResult<Bound<'py, PyTuple>> {
        match v.span {
            Some(span) => (&*v.uri, self.source.char_offset(span.start as usize)).into_pyobject(self.py),
            None => self.plain(&v.uri),
        }
    }

    fn attr(&self, a: &Attr) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(self.py);
        d.set_item("space", self.tok(a.space)?)?;
        d.set_item("name", self.tok(a.name)?)?;
        let (eq, quote, value) = match a.kind {
            AttrKind::Quoted => (self.tok(a.eq)?, self.tok(a.quote)?, self.tok(a.value)?),
            AttrKind::Unquoted => (self.tok(a.eq)?, self.plain("")?, self.tok(a.value)?),
            AttrKind::Valueless => (self.plain("")?, self.plain("")?, self.plain("")?),
        };
        d.set_item("eq", eq)?;
        d.set_item("quote", quote)?;
        d.set_item("value", value)?;
        Ok(d)
    }

    fn tag(&self, t: &Tag) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(self.py);
        d.set_item("prefix", self.tok(t.prefix)?)?;
        d.set_item("name", self.tok(t.name)?)?;
        d.set_item("suffix", self.opt(t.suffix)?)?;
        d.set_item("space", self.opt(t.space)?)?;
        let attrs = PyList::empty(self.py);
        for a in &t.attrs {
            attrs.append(self.attr(a)?)?;
        }
        d.set_item("attrs", attrs)?;
        d.set_item("namespace", self.ns_value(&t.namespace)?)?;
        let ns_attrs = PyList::empty(self.py);
        for a in &t.ns_attrs {
            let key = (&*a.ns, self.tok(a.name_span)?);
            let value = if a.kind == AttrKind::Valueless { self.plain("")? } else { self.tok(a.value)? };
            ns_attrs.append((key, value))?;
        }
        d.set_item("ns_attrs", ns_attrs)?;
        let ns_map = PyDict::new(self.py);
        for (prefix, value) in t.ns_map.iter() {
            ns_map.set_item(prefix, self.ns_value(value)?)?;
        }
        d.set_item("ns_map", ns_map)?;
        Ok(d)
    }

    fn item(&self, item: &Item) -> PyResult<Bound<'py, PyTuple>> {
        let py = self.py;
        let one =
            |kind: &str, span: Span| -> PyResult<Bound<'py, PyTuple>> { (kind, (self.tok(span)?,)).into_pyobject(py) };
        match item {
            Item::Text(s) => one("text", *s),
            Item::Comment(s) => one("comment", *s),
            Item::Cdata(s) => one("cdata", *s),
            Item::Default(s) => one("default", *s),
            Item::ProcessingInstruction { name, text, .. } => {
                let d = PyDict::new(py);
                d.set_item("name", self.tok(*name)?)?;
                d.set_item("text", self.tok(*text)?)?;
                ("processing_instruction", (d,)).into_pyobject(py)
            }
            Item::StartTag(t) => ("start_tag", (self.tag(t)?,)).into_pyobject(py),
            Item::Element(e) => {
                let end = match &e.end {
                    Some(t) => self.tag(t)?.into_any(),
                    None => py.None().into_bound(py),
                };
                ("element", (self.tag(&e.start)?, end, self.items(&e.children)?)).into_pyobject(py)
            }
        }
    }

    fn items(&self, items: &[Item]) -> PyResult<Bound<'py, PyList>> {
        let list = PyList::empty(self.py);
        for item in items {
            list.append(self.item(item)?)?;
        }
        Ok(list)
    }
}

fn spans(body: &str, mode: &str) -> PyResult<Vec<Span>> {
    match mode {
        "xml" => Ok(tokenize(body)),
        "text" => Ok(tokenize_text(body)),
        _ => Err(PyValueError::new_err(format!("unknown tokenizer mode {mode:?}"))),
    }
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
/// the module docs). Errors are raised as compiling would raise them.
#[pyfunction]
#[pyo3(signature = (body, namespaces, restricted_namespace = true, mode = "xml"))]
pub(crate) fn parse<'py>(
    py: Python<'py>,
    body: &str,
    namespaces: &Bound<'py, PyDict>,
    restricted_namespace: bool,
    mode: &str,
) -> PyResult<Bound<'py, PyList>> {
    let mut entries: Vec<(Option<String>, String)> = Vec::with_capacity(namespaces.len());
    for (k, v) in namespaces.iter() {
        entries.push((k.extract()?, v.extract()?));
    }
    let options = ParseOptions {
        namespaces: Namespaces::new(entries.iter().map(|(k, v)| (k.as_deref(), v.as_str()))),
        restricted_namespace,
    };
    let tokens = spans(body, mode)?;
    let items = parse_tokens(body, &tokens, &options).map_err(|e| compile_error(py, e, body, "<string>"))?;
    Conv { py, src: body, source: Source::new(body) }.items(&items)
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
