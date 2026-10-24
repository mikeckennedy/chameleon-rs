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
    if matches!(stage, Stage::Bool | Stage::Format | Stage::Append) {
        return emit_special(cx, value, esc, stage, out);
    }
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
        // Handled above.
        Stage::Bool | Stage::Format | Stage::Append => {
            Err(pyo3::exceptions::PyRuntimeError::new_err("stage reached the general conversion"))
        }
    }
}

/// The stages that skip the usual two-step conversion (out of line: `emit` is hot).
#[cold]
#[inline(never)]
fn emit_special<'py>(
    cx: &Cx<'_, 'py>,
    value: &Bound<'py, PyAny>,
    esc: Escape,
    stage: Stage,
    out: &mut Output,
) -> PyResult<Emitted> {
    match stage {
        Stage::Bool => emit_bool(cx, value, esc, out),
        // `'%s' % value` unless `None` (a translated attribute).
        Stage::Format => {
            if value.is_none() {
                return Ok(Emitted::Nothing);
            }
            write_raw(&value.str()?, out)?;
            Ok(Emitted::Value)
        }
        // `__append(value)`: the final `''.join` takes only strings.
        Stage::Append => append(value, out),
        Stage::Content | Stage::Lone | Stage::Part => {
            Err(pyo3::exceptions::PyRuntimeError::new_err("a usual stage reached the special conversion"))
        }
    }
}

/// A boolean attribute's lone interpolation, whose value is true (`visit_Attribute` with
/// `literal_false=False`): `v = __quote(v)`, then `if not v: None; elif v is marker: None;
/// else: emit_convert(v)`, then `if v: v = name`, then `if v is not None: ' name="%s"' % v`.
/// So a true result is the name; a false one that is not `None` (an `__html__` returning
/// `0` or `''`) is written as text.
fn emit_bool<'py>(cx: &Cx<'_, 'py>, value: &Bound<'py, PyAny>, esc: Escape, out: &mut Output) -> PyResult<Emitted> {
    let before = out.mark();
    let other = match quote(cx, value, esc, true, out)? {
        Quoted::Nothing => return Ok(Emitted::Nothing),
        // A `str`, escaped: true unless empty.
        Quoted::Written => return Ok(if out.mark() == before { Emitted::Nothing } else { Emitted::True }),
        Quoted::Other(other) => other,
    };
    if !other.is_truthy()? {
        return Ok(Emitted::Nothing);
    }
    let Some(converted) = convert(cx, &other, esc.drops_default())? else { return Ok(Emitted::Nothing) };
    if converted.is_truthy()? {
        return Ok(Emitted::True);
    }
    let text = intern!(value.py(), "%s").rem(&converted)?;
    write_raw(text.cast::<PyString>()?, out)?;
    Ok(Emitted::Value)
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
    if value.is_none() || (marker_is_default && esc.drops_default() && value.is(cx.marker())) {
        return Ok(Quoted::Nothing);
    }
    if let Ok(s) = value.cast_exact::<PyString>() {
        write_str(s, esc, out)?;
        return Ok(Quoted::Written);
    }
    if is_markup(value)? {
        // `structure:`'s `Markup`: `__html__()` is `str(self)`, which every stage writes
        // unescaped (a lone value, a part, content, a boolean attribute's text).
        write_str(value.cast::<PyString>()?, Escape::Structure, out)?;
        return Ok(Quoted::Written);
    }
    if esc.drops_default() && is_markdown(value)? {
        // Converted Markdown in an attribute: escaped, never markup (see `is_markdown`).
        write_str(value.cast::<PyString>()?, esc, out)?;
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
    } else if let Some(html) = html_of(value)? {
        // The markup is returned as is, never escaped.
        let markup = html;
        return Ok(if markup.is_none() { Quoted::Nothing } else { Quoted::Other(markup) });
    } else {
        translate(cx, value, Target::Local)?
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

static MARKUP: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
static MARKDOWN: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();

/// Whether the value is a `markdown:` result (`chameleon_rs.utils.MarkdownMarkup`): markup in
/// content, but escaped in an attribute value. Its HTML is made from untrusted text and quotes
/// its own attributes (`<a href="...">`), so written raw into `alt="..."` it would close that
/// attribute and let the text add others (`onmouseover`).
fn is_markdown(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = value.py();
    let markdown = MARKDOWN.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs.utils"))?.getattr(intern!(py, "MarkdownMarkup"))?.unbind())
    })?;
    Ok(value.get_type().is(markdown.bind(py)))
}

/// `value.__html__()` if the value has the method (`None` if it has not). For an exact
/// `chameleon_rs.utils.Markup` (what `structure:` makes), whose `__html__` is `str(self)`,
/// that `str` is made natively, without the attribute lookup and the Python call.
fn is_markup(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = value.py();
    let markup = MARKUP.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs.utils"))?.getattr(intern!(py, "Markup"))?.unbind())
    })?;
    Ok(value.get_type().is(markup.bind(py)))
}

fn html_of<'py>(value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    if is_markup(value)? || is_markdown(value)? {
        return Ok(Some(value.str()?.into_any()));
    }
    if crate::repeat::is_plain_callable(value)? {
        return Ok(None);
    }
    // `getattr(target, '__html__', None)`: an attribute set to `None` counts as absent.
    match value.getattr_opt(intern!(value.py(), "__html__"))? {
        Some(html) if !html.is_none() => Ok(Some(html.call0()?)),
        _ => Ok(None),
    }
}

/// `emit_convert` (no escaping). `None` means the value converts to `None`.
fn convert<'py>(
    cx: &Cx<'_, 'py>,
    value: &Bound<'py, PyAny>,
    drops_default: bool,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if value.is_none() || (drops_default && value.is(cx.marker())) {
        return Ok(None);
    }
    let converted = if value.is_exact_instance_of::<PyBytes>() {
        decode(cx, value)?
    } else if value.is_exact_instance_of::<PyString>() {
        value.clone()
    } else if value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyFloat>() {
        value.str()?.into_any()
    } else if let Some(html) = html_of(value)? {
        html
    } else {
        translate(cx, value, Target::Scope)?
    };
    Ok(if converted.is_none() { None } else { Some(converted) })
}

fn decode<'py>(cx: &Cx<'_, 'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let decode = cx
        .scope_or_default(intern!(value.py(), "__decode"))?
        .ok_or_else(|| PyTypeError::new_err("no __decode function for bytes"))?;
    decode.call1((value,))
}

/// Which `target_language` a conversion passes: the render function's local (in the
/// `__quote` / `__convert` helpers) or `getname('target_language')` (in conversion code
/// generated inline with the expression, which Chameleon's name rewriting reaches).
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum Target {
    Local,
    Scope,
}

/// `translate(target, domain=..., context=..., target_language=...)`, then
/// `str(target) if target is converted else converted`.
fn translate<'py>(cx: &Cx<'_, 'py>, value: &Bound<'py, PyAny>, target: Target) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let converted = match cx.scope_or_default(intern!(py, "__translate"))? {
        Some(translate) if is_simple(cx, &translate, value)? => {
            if target == Target::Scope {
                // Evaluated for its error (a missing `target_language`), as the argument is.
                cx.scope_target_language()?;
            }
            simple_translate_value(value)?
        }
        Some(translate) => {
            let kwargs = PyDict::new(py);
            // The helpers of a slot body see the i18n settings of the function they belong to.
            let helpers = cx.frame.helpers.as_deref().filter(|_| target == Target::Local);
            let (domain, context) = helpers.map_or((&cx.frame.domain, &cx.frame.context), |h| (&h.domain, &h.context));
            kwargs.set_item(intern!(py, "domain"), domain)?;
            kwargs.set_item(intern!(py, "context"), context)?;
            let target_language = match target {
                Target::Local => helpers.map_or(&cx.frame.target_language, |h| &h.target_language).clone(),
                Target::Scope => cx.scope_target_language()?,
            };
            kwargs.set_item(intern!(py, "target_language"), target_language)?;
            translate.call((value,), Some(&kwargs))?
        }
        None => value.clone(),
    };
    if converted.is(value) { Ok(value.str()?.into_any()) } else { Ok(converted) }
}

static SIMPLE_TRANSLATE: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();

/// Whether calling `translate` on `value` is `simple_translate(value)`: it is that function,
/// or `PageTemplate`'s `encoding` wrapper around it (marked `__chameleon_rs_simple__`), which
/// only decodes a `bytes` message id first. The wrapper seen last is remembered for the render.
fn is_simple(cx: &Cx<'_, '_>, translate: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = value.py();
    if translate.is(simple_translate(py)?) {
        return Ok(true);
    }
    if value.is_instance_of::<pyo3::types::PyBytes>() {
        return Ok(false);
    }
    let address = translate.as_ptr() as usize;
    let (known, simple) = cx.render.simple_wrapper.get();
    if known == address {
        return Ok(simple);
    }
    let simple = translate.is_instance_of::<pyo3::types::PyFunction>()
        && translate
            .getattr_opt(intern!(py, "__chameleon_rs_simple__"))?
            .is_some_and(|v| v.is(&*pyo3::types::PyBool::new(py, true)));
    cx.render.simple_wrapper.set((address, simple));
    Ok(simple)
}

/// The default translation function, `chameleon_rs.i18n.simple_translate`.
fn simple_translate(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(SIMPLE_TRANSLATE
        .get_or_try_init(py, || {
            Ok::<_, PyErr>(
                py.import(intern!(py, "chameleon_rs.i18n"))?.getattr(intern!(py, "simple_translate"))?.unbind(),
            )
        })?
        .bind(py))
}

/// `simple_translate(value)` with no mapping or default, natively (the conversion of a value
/// that is not a string, a number or markup, with the default translation function):
///
/// ```python
/// default = getattr(msgid, 'default', msgid)
/// mapping = getattr(msgid, 'mapping', None)
/// if mapping: return _interp_regex.sub(replace, default)
/// return default
/// ```
///
/// A true `mapping` is interpolated by `chameleon_rs.i18n._interpolate` with the values
/// already read, so each attribute is read once, as in the Python function.
fn simple_translate_value<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    if crate::repeat::is_plain_callable(value)? {
        return Ok(value.clone());
    }
    let default = value.getattr_opt(intern!(py, "default"))?.unwrap_or_else(|| value.clone());
    if let Some(mapping) = value.getattr_opt(intern!(py, "mapping"))?
        && mapping.is_truthy()?
    {
        return interpolate(py)?.call1((default, mapping));
    }
    Ok(default)
}

static INTERPOLATE: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();

/// `chameleon_rs.i18n._interpolate(default, mapping)`: `simple_translate`'s interpolation.
fn interpolate(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(INTERPOLATE
        .get_or_try_init(py, || {
            Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs.i18n"))?.getattr(intern!(py, "_interpolate"))?.unbind())
        })?
        .bind(py))
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

/// Strings at least this long (bytes of UTF-8) are escaped or copied with the thread detached
/// from the interpreter (the GIL released on a GIL build). Below it, detaching and attaching
/// again costs more than the copy.
const DETACH_AT: usize = 64 * 1024;

/// A long stretch of pure-Rust work (Phase 11's offload experiment): escaped with the thread
/// detached, so other threads (the event loop, other renders) run meanwhile. The `str` is
/// immutable and the render holds a reference, so its UTF-8 buffer stays valid. Out of line:
/// the usual path stays as it was.
#[cold]
#[inline(never)]
fn escape_detached(py: Python<'_>, text: &str, esc: Escape, out: &mut Output) {
    py.detach(|| escape_into(text.as_bytes(), esc, out));
}

/// Writes a `str` through its UTF-8 view; a string with lone surrogates (which has none) is
/// encoded with `surrogatepass` and the output flagged (D6).
pub(crate) fn write_str(s: &Bound<'_, PyString>, esc: Escape, out: &mut Output) -> PyResult<()> {
    if let Ok(text) = s.to_str() {
        if text.len() >= DETACH_AT {
            escape_detached(s.py(), text, esc, out);
        } else {
            escape_into(text.as_bytes(), esc, out);
        }
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
    if conv.marker && value.is(cx.marker()) {
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
        } else if let Some(html) = html_of(value)? {
            Ok(html)
        } else {
            translate(cx, value, Target::Scope)
        };
    };
    // `__quote(target, quote, quote_entity, default, default_marker)`.
    let target = if esc.drops_default() && is_markdown(value)? {
        // Converted Markdown in an attribute: escaped (see `is_markdown`).
        value.clone()
    } else if value.is_exact_instance_of::<PyBytes>() {
        decode(cx, value)?
    } else if value.is_exact_instance_of::<PyString>() {
        value.clone()
    } else if value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyFloat>() {
        return Ok(value.str()?.into_any());
    } else if let Some(html) = html_of(value)? {
        return Ok(html);
    } else {
        translate(cx, value, Target::Local)?
    };
    // `__re_needs_escape(target)` raises TypeError for a non-str, which skips escaping.
    match target.cast::<PyString>() {
        Ok(s) => escape_py(s, esc),
        Err(_) => Ok(target),
    }
}

static SYS_INTERN: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();

/// `sys.intern(text)`: a name read from a template, interned as Python code's identifiers
/// are, so scope lookups compare by identity. Not PyO3's `PyString::intern`: that makes the
/// string immortal (`PyUnicode_InternFromString`), and every distinct name a template ever
/// used would stay for the life of the process (templates built per request with generated
/// names grew without bound). `sys.intern` makes it mortal where CPython can (3.13 and later
/// with the GIL; 3.12 and free-threaded builds intern only immortally), so it goes with the
/// last template using it.
pub(crate) fn intern<'py>(py: Python<'py>, text: &str) -> Bound<'py, PyString> {
    let fresh = PyString::new(py, text);
    let Ok(sys_intern) = SYS_INTERN.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import(intern!(py, "sys"))?.getattr(intern!(py, "intern"))?.unbind())
    }) else {
        return fresh;
    };
    match sys_intern.bind(py).call1((&fresh,)).map(Bound::cast_into::<PyString>) {
        Ok(Ok(interned)) => interned,
        _ => fresh,
    }
}

/// A `str` for a piece of template source: with lone surrogates mapped to the private-use
/// block at `base` (`chameleon_core::surrogates`), mapped back.
pub(crate) fn source_str<'py>(py: Python<'py>, text: &str, base: Option<u32>) -> PyResult<Bound<'py, PyString>> {
    if let Some(base) = base
        && let Some(bytes) = chameleon_core::surrogates::to_wtf8(text, base)
    {
        let bytes = PyBytes::new(py, &bytes);
        return PyString::from_encoded_object(&bytes, Some(c"utf-8"), Some(c"surrogatepass"));
    }
    Ok(PyString::new(py, text))
}

/// A `str` for text taken from the output (WTF-8: lone surrogates as `surrogatepass` bytes).
pub(crate) fn wtf8_str<'py>(
    py: Python<'py>,
    text: &chameleon_core::surrogates::Wtf8Buf,
) -> PyResult<Bound<'py, PyString>> {
    if let Some(s) = text.as_str() {
        return Ok(PyString::new(py, s));
    }
    let bytes = PyBytes::new(py, text.as_bytes());
    PyString::from_encoded_object(&bytes, Some(c"utf-8"), Some(c"surrogatepass"))
}

/// `__quote(value, quote, entity, default, marker)` as a value: the attribute engine (`esc`
/// is an attribute's) converts the marker to `default` (source text of a program whose lone
/// surrogates were mapped to the block at `base`).
pub(crate) fn quote_value<'py>(
    cx: &Cx<'_, 'py>,
    value: &Bound<'py, PyAny>,
    esc: Escape,
    default: Option<&str>,
    base: Option<u32>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let conv = crate::expr::TextConv {
        escape: Some(esc),
        marker: esc.drops_default(),
        default: default.map(|d| source_str(py, d, base)).transpose()?.map(Bound::unbind),
        literal_false: true,
    };
    convert_text(cx, value, &conv)
}

/// `emit_convert` with the attribute engine's settings (the marker converts to `None`).
pub(crate) fn convert_value<'py>(cx: &Cx<'_, 'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let conv = crate::expr::TextConv { escape: None, marker: true, default: None, literal_false: true };
    convert_text(cx, value, &conv)
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
