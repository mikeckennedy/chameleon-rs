//! Tokens and element trees as Python data: what `chameleon_rs.tokenize.iter_xml` and
//! `chameleon_rs.parser.ElementParser` (the compat modules, D7) build Chameleon's `Token`s
//! and items from, and what the differential tests in `_testing` compare.
//!
//! A token (Chameleon's `Token`, a `str` with a position) becomes `(text, pos)`, with `pos`
//! in code points; a plain string Chameleon creates without a position becomes
//! `(text, None)`. Items are `(kind, args)` tuples as `ElementParser` yields them; a tag is
//! a dict with Chameleon's keys, where `ns_attrs` is a list of
//! `((namespace, name), value)` pairs in order and `ns_map` maps prefixes (`None` for the
//! default namespace) to tokens, every one of them a `(text, pos)` token.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use chameleon_core::testing::{
    Attr, AttrKind, Item, Namespaces, NsValue, ParseOptions, Tag, parse_tokens, tokenize, tokenize_text,
};
use chameleon_core::{Source, Span};

use crate::errors::compile_error;

pub(crate) struct Conv<'py, 's> {
    pub(crate) py: Python<'py>,
    pub(crate) src: &'s str,
    pub(crate) source: Source<'s>,
}

impl<'py> Conv<'py, '_> {
    pub(crate) fn tok(&self, span: Span) -> PyResult<Bound<'py, PyTuple>> {
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
            let ns = self.ns_value(&NsValue { uri: a.ns.clone(), span: a.ns_span })?;
            let key = (ns, self.tok(a.name_span)?);
            let value = if a.kind == AttrKind::Valueless { self.plain("")? } else { self.tok(a.value)? };
            ns_attrs.append((key, value))?;
        }
        d.set_item("ns_attrs", ns_attrs)?;
        let ns_map = PyDict::new(self.py);
        for (prefix, span, value) in t.ns_map.iter_spans() {
            let key = match (prefix, span) {
                (None, _) => self.py.None().into_bound(self.py),
                (Some(_), Some(span)) => self.tok(span)?.into_any(),
                (Some(prefix), None) => self.plain(prefix)?.into_any(),
            };
            ns_map.set_item(key, self.ns_value(value)?)?;
        }
        d.set_item("ns_map", ns_map)?;
        Ok(d)
    }

    fn item(&self, item: &Item) -> PyResult<Bound<'py, PyTuple>> {
        // Recursive over nested elements (`chameleon_core::stack`): `RecursionError`, never a
        // native stack overflow.
        let _deep = chameleon_core::stack::enter().map_err(crate::errors::too_deep)?;
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

    pub(crate) fn items(&self, items: &[Item]) -> PyResult<Bound<'py, PyList>> {
        let list = PyList::empty(self.py);
        for item in items {
            list.append(self.item(item)?)?;
        }
        Ok(list)
    }
}

pub(crate) fn spans(body: &str, mode: &str) -> PyResult<Vec<Span>> {
    check_len(body)?;
    match mode {
        "xml" => Ok(tokenize(body)),
        "text" => Ok(tokenize_text(body)),
        _ => Err(PyValueError::new_err(format!("unknown tokenizer mode {mode:?}"))),
    }
}

/// `Span` offsets are `u32` (the core's sources are at most 4 GiB): checked before any span
/// of `body` is built, so an offset is never truncated.
fn check_len(body: &str) -> PyResult<()> {
    if u32::try_from(body.len()).is_err() {
        return Err(pyo3::exceptions::PyRuntimeError::new_err("template source is larger than 4 GiB"));
    }
    Ok(())
}

/// Byte spans of consecutive tokens of `body` with the given lengths in code points (the
/// first starts at 0). The lengths may cover less than the whole body.
pub(crate) fn spans_from_lengths(body: &str, lengths: &[usize]) -> PyResult<Vec<Span>> {
    check_len(body)?;
    let mut spans = Vec::with_capacity(lengths.len());
    let mut chars = body.char_indices();
    let mut at = 0;
    for &len in lengths {
        let start = at;
        for _ in 0..len {
            let Some((i, c)) = chars.next() else {
                return Err(PyValueError::new_err("token lengths exceed the source"));
            };
            at = i + c.len_utf8();
        }
        spans.push(Span::new(start, at));
    }
    Ok(spans)
}

/// `[(start, end), ...]`: the code-point range of each token of `body` (`iter_xml`, or
/// `iter_text` with `mode="text"`).
#[pyfunction]
#[pyo3(signature = (body, mode = "xml"))]
pub(crate) fn tokenize_ranges(py: Python<'_>, body: &str, mode: &str) -> PyResult<Vec<(usize, usize)>> {
    // Called from Python (`chameleon_rs.tokenize`): a panic is an `InternalError`.
    crate::errors::catch_panics(py, || {
        let source = Source::new(body);
        Ok(spans(body, mode)?
            .into_iter()
            .map(|s| (source.char_offset(s.start as usize), source.char_offset(s.end as usize)))
            .collect())
    })
}

/// `list(ElementParser(tokens, namespaces, restricted_namespace))` for tokens that are
/// consecutive slices of `body` with the given lengths (in code points), as `(kind, args)`
/// items with `(text, pos)` tokens (see the module docs). Errors are raised as compiling
/// raises them.
#[pyfunction]
#[pyo3(signature = (body, lengths, namespaces, restricted_namespace = true))]
#[allow(clippy::needless_pass_by_value)] // extracted from Python by value
pub(crate) fn parse_elements<'py>(
    py: Python<'py>,
    body: &str,
    lengths: Vec<usize>,
    namespaces: &Bound<'py, PyDict>,
    restricted_namespace: bool,
) -> PyResult<Bound<'py, PyList>> {
    // Called from Python (`chameleon_rs.parser.ElementParser`): a panic is an `InternalError`.
    crate::errors::catch_panics(py, || {
        let tokens = spans_from_lengths(body, &lengths)?;
        parse_spans(py, body, &tokens, namespaces, restricted_namespace)
    })
}

pub(crate) fn parse_spans<'py>(
    py: Python<'py>,
    body: &str,
    tokens: &[Span],
    namespaces: &Bound<'py, PyDict>,
    restricted_namespace: bool,
) -> PyResult<Bound<'py, PyList>> {
    let mut entries: Vec<(Option<String>, String)> = Vec::with_capacity(namespaces.len());
    // (A snapshot of the items: PyO3's dict iterator panics if the dict changes size.)
    for item in namespaces.items().iter() {
        let (k, v): (Option<String>, String) = item.extract()?;
        entries.push((k, v));
    }
    let options = ParseOptions {
        namespaces: Namespaces::new(entries.iter().map(|(k, v)| (k.as_deref(), v.as_str()))),
        restricted_namespace,
    };
    let items = parse_tokens(body, tokens, &options).map_err(|e| compile_error(py, e, body, ""))?;
    Conv { py, src: body, source: Source::new(body) }.items(&items)
}
