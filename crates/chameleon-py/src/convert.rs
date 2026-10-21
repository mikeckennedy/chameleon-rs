// Ported from Chameleon's src/chameleon/compiler.py (`emit_func_convert_and_escape` (the
// `__quote` helper), `emit_func_convert` (`__convert`), `emit_convert`, and the code
// `ExpressionTransform.visit_Interpolation`, `Compiler.visit_Content` and
// `Compiler.visit_Attribute` generate around them), modified: converts and escapes straight
// into the Rust output buffer.

//! Value conversion with Chameleon's exact type checks (D6).
//!
//! Chameleon converts an interpolated value in two stages. Each `${...}` goes through
//! `__quote` (convert, then escape if the result is a `str`). The interpolation's result is
//! then converted once more: a lone `${...}` gets `emit_convert` (the same conversion without
//! escaping) and is appended (text) or `%s`-formatted (attributes); in `a ${x} b` every part
//! is `%s`-formatted into one string. The second stage only matters when the first one did
//! not produce a `str` (an `__html__` or `translate` returning `5`, `b'x'`, a list, ...).
//!
//! Interpolation without escaping (CDATA, text mode) has the same stages with
//! `Escape::Structure`; `tal:content` appends the first stage's result directly.

use std::fmt::Write as _;

use pyo3::exceptions::PyTypeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyFloat, PyInt, PyString};

use chameleon_core::{Emitted, Escape, Output, Stage, escape_into};

use crate::expr::Cx;

/// Result of the first stage.
enum Quoted<'py> {
    /// `None`: nothing is written.
    Nothing,
    /// A `str` already escaped into the output.
    Written,
    /// Something that is not a `str`, left to the second stage.
    Other(Bound<'py, PyAny>),
}

/// Converts and writes `value` for `esc` at `stage` (see [`Stage`]).
pub(crate) fn emit<'py>(
    cx: &Cx<'_, 'py>,
    value: &Bound<'py, PyAny>,
    esc: Escape,
    stage: Stage,
    out: &mut Output,
) -> PyResult<Emitted> {
    let other = match quote(cx, value, esc, true, out)? {
        Quoted::Nothing => return Ok(Emitted::Nothing),
        Quoted::Written => return Ok(Emitted::Value),
        Quoted::Other(other) => other,
    };
    match stage {
        // `if NAME is not None: __append(NAME)`: the stream must hold strings.
        Stage::Content => append(&other, out),
        // Lone: `emit_convert`, then appended (text) or `"%s" % value` (attribute).
        Stage::Lone => match convert(cx, &other, esc.drops_default())? {
            None => Ok(Emitted::Nothing),
            Some(converted) if matches!(esc, Escape::Attr(_)) => {
                write_raw(&converted.str()?, out)?;
                Ok(Emitted::Value)
            }
            Some(converted) => append(&converted, out),
        },
        // A part: `"%s%s" % (..., NODE, ...)`.
        Stage::Part => {
            write_raw(&other.str()?, out)?;
            Ok(Emitted::Value)
        }
    }
}

/// `__quote(value, quote, entity, None, None)` with the result appended (a `tal:attributes`
/// dictionary value): the template's default marker is not special here.
pub(crate) fn emit_plain<'py>(
    cx: &Cx<'_, 'py>,
    value: &Bound<'py, PyAny>,
    esc: Escape,
    out: &mut Output,
) -> PyResult<Emitted> {
    match quote(cx, value, esc, false, out)? {
        Quoted::Nothing => Ok(Emitted::Nothing),
        Quoted::Written => Ok(Emitted::Value),
        Quoted::Other(other) => append(&other, out),
    }
}

/// `__quote` (or `__convert` for `structure`): `None` stays `None`; in attributes the default
/// marker becomes the absent default; an exact `str` is escaped; an exact `int` or `float`
/// is `str()`ed unescaped (an `i64` via Rust's `Display`, which matches `str(int)`); exact
/// `bytes` are decoded with `__decode`; anything else uses `__html__()` as is if it has one,
/// else `translate`'s result (`str()` if it returned the value itself). A resulting `str` is
/// escaped.
/// `marker_is_default`: the call passes the template's default marker (it does everywhere
/// except for dictionary attributes).
fn quote<'py>(
    cx: &Cx<'_, 'py>,
    value: &Bound<'py, PyAny>,
    esc: Escape,
    marker_is_default: bool,
    out: &mut Output,
) -> PyResult<Quoted<'py>> {
    let py = value.py();
    if value.is_none() || (marker_is_default && esc.drops_default() && value.is(&cx.render.marker)) {
        return Ok(Quoted::Nothing);
    }
    if let Ok(s) = value.cast_exact::<PyString>() {
        write_str(s, esc, out)?;
        return Ok(Quoted::Written);
    }
    if value.is_exact_instance_of::<PyInt>() {
        if let Ok(n) = value.extract::<i64>() {
            let _ = write!(out, "{n}");
        } else {
            write_raw(&value.str()?, out)?;
        }
        return Ok(Quoted::Written);
    }
    if value.is_exact_instance_of::<PyFloat>() {
        write_raw(&value.str()?, out)?;
        return Ok(Quoted::Written);
    }
    let converted = if value.is_exact_instance_of::<PyBytes>() {
        decode(cx, value)?
    } else if let Some(html) = value.getattr_opt(intern!(py, "__html__"))? {
        // The markup is returned as is, never escaped.
        let markup = html.call0()?;
        return Ok(if markup.is_none() { Quoted::Nothing } else { Quoted::Other(markup) });
    } else {
        translate(cx, value)?
    };
    // `__re_needs_escape(target)` raises TypeError for a non-str, which skips escaping.
    match converted.cast::<PyString>() {
        Ok(s) => {
            write_str(s, esc, out)?;
            Ok(Quoted::Written)
        }
        Err(_) if converted.is_none() => Ok(Quoted::Nothing),
        Err(_) => Ok(Quoted::Other(converted)),
    }
}

/// `emit_convert` (no escaping). `None` means the value converts to `None`.
fn convert<'py>(
    cx: &Cx<'_, 'py>,
    value: &Bound<'py, PyAny>,
    drops_default: bool,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = value.py();
    if value.is_none() || (drops_default && value.is(&cx.render.marker)) {
        return Ok(None);
    }
    let converted = if value.is_exact_instance_of::<PyBytes>() {
        decode(cx, value)?
    } else if value.is_exact_instance_of::<PyString>() {
        value.clone()
    } else if value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyFloat>() {
        value.str()?.into_any()
    } else if let Some(html) = value.getattr_opt(intern!(py, "__html__"))? {
        html.call0()?
    } else {
        translate(cx, value)?
    };
    Ok(if converted.is_none() { None } else { Some(converted) })
}

fn decode<'py>(cx: &Cx<'_, 'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let decode = cx
        .scope_or_default(intern!(value.py(), "__decode"))?
        .ok_or_else(|| PyTypeError::new_err("no __decode function for bytes"))?;
    decode.call1((value,))
}

/// `translate(target, domain=..., context=..., target_language=...)`, then
/// `str(target) if target is converted else converted`.
fn translate<'py>(cx: &Cx<'_, 'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let converted = match cx.scope_or_default(intern!(py, "__translate"))? {
        Some(translate) => {
            let kwargs = PyDict::new(py);
            kwargs.set_item(intern!(py, "domain"), py.None())?;
            kwargs.set_item(intern!(py, "context"), py.None())?;
            let target_language = cx.scope_or_default(intern!(py, "target_language"))?;
            kwargs.set_item(intern!(py, "target_language"), target_language)?;
            translate.call((value,), Some(&kwargs))?
        }
        None => value.clone(),
    };
    if converted.is(value) { Ok(value.str()?.into_any()) } else { Ok(converted) }
}

/// `__append(value)`: Chameleon's final `''.join` accepts only strings.
fn append(value: &Bound<'_, PyAny>, out: &mut Output) -> PyResult<Emitted> {
    let s = value.cast::<PyString>().map_err(|_| {
        let name = value.get_type().name().map_or_else(|_| "?".into(), |n| n.to_string());
        PyTypeError::new_err(format!("sequence item: expected str instance, {name} found"))
    })?;
    write_raw(s, out)?;
    Ok(Emitted::Value)
}

fn write_raw(s: &Bound<'_, PyString>, out: &mut Output) -> PyResult<()> {
    write_str(s, Escape::Structure, out)
}

/// Writes a `str` through its UTF-8 view; a string with lone surrogates (which has none) is
/// encoded with `surrogatepass` and the output flagged (D6).
pub(crate) fn write_str(s: &Bound<'_, PyString>, esc: Escape, out: &mut Output) -> PyResult<()> {
    if let Ok(text) = s.to_str() {
        escape_into(text.as_bytes(), esc, out);
        return Ok(());
    }
    let py = s.py();
    let encoded = s.call_method1(intern!(py, "encode"), (intern!(py, "utf-8"), intern!(py, "surrogatepass")))?;
    let bytes = encoded.cast::<PyBytes>()?;
    escape_into(bytes.as_bytes(), esc, out);
    out.set_has_surrogates();
    Ok(())
}

/// A sub-expression's value converted to text the way its site's engine does (`assign_text`,
/// used by `string:`, `load:` and custom types): `__quote` with the site's escaping, or
/// `emit_convert` without escaping; the marker becomes the engine's default when the engine
/// has the marker; with `literal_false` unset, a false value is `None` first.
pub(crate) fn convert_text<'py>(
    cx: &Cx<'_, 'py>,
    value: &Bound<'py, PyAny>,
    conv: &crate::expr::TextConv,
) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    if (!conv.literal_false && !value.is_truthy()?) || value.is_none() {
        return Ok(py.None().into_bound(py));
    }
    if conv.marker && value.is(&cx.render.marker) {
        return Ok(match &conv.default {
            Some(d) => d.bind(py).clone().into_any(),
            None => py.None().into_bound(py),
        });
    }
    let Some(esc) = conv.escape else {
        // `emit_convert`.
        return if value.is_exact_instance_of::<PyBytes>() {
            decode(cx, value)
        } else if value.is_exact_instance_of::<PyString>() {
            Ok(value.clone())
        } else if value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyFloat>() {
            Ok(value.str()?.into_any())
        } else if let Some(html) = value.getattr_opt(intern!(py, "__html__"))? {
            html.call0()
        } else {
            translate(cx, value)
        };
    };
    // `__quote(target, quote, quote_entity, default, default_marker)`.
    let target = if value.is_exact_instance_of::<PyBytes>() {
        decode(cx, value)?
    } else if value.is_exact_instance_of::<PyString>() {
        value.clone()
    } else if value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyFloat>() {
        return Ok(value.str()?.into_any());
    } else if let Some(html) = value.getattr_opt(intern!(py, "__html__"))? {
        return html.call0();
    } else {
        translate(cx, value)?
    };
    // `__re_needs_escape(target)` raises TypeError for a non-str, which skips escaping.
    match target.cast::<PyString>() {
        Ok(s) => escape_py(s, esc),
        Err(_) => Ok(target),
    }
}

/// Chameleon's escaping of a Python `str`, returning a `str` (the same object when the gate
/// does not match, as `__quote` returns it).
fn escape_py<'py>(s: &Bound<'py, PyString>, esc: Escape) -> PyResult<Bound<'py, PyAny>> {
    let py = s.py();
    if let Ok(text) = s.to_str() {
        if !chameleon_core::needs_escape(text.as_bytes()) || !esc.escapes() {
            return Ok(s.clone().into_any());
        }
        let mut out = Output::with_capacity(text.len() + 16);
        escape_into(text.as_bytes(), esc, &mut out);
        return Ok(PyString::from_bytes(py, out.as_bytes())?.into_any());
    }
    // Lone surrogates: Chameleon's `str.replace` calls.
    let gate = ["&", "<", ">", "\"", "'"];
    let mut any = false;
    for g in gate {
        any |= s.contains(g)?;
    }
    if !any || !esc.escapes() {
        return Ok(s.clone().into_any());
    }
    let quote = match esc {
        Escape::Text => Some(("\0", "&#0;")),
        Escape::Attr(chameleon_core::Quote::Double) => Some(("\"", "&quot;")),
        Escape::Attr(chameleon_core::Quote::Single) => Some(("'", "&#39;")),
        Escape::Attr(chameleon_core::Quote::Bare) => Some(("", "&#0;")),
        Escape::Content | Escape::Structure => None,
    };
    let replace = intern!(py, "replace");
    let mut target = s.clone().into_any();
    for (from, to) in [("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")].into_iter().chain(quote) {
        target = target.call_method1(replace, (from, to))?;
    }
    Ok(target)
}
