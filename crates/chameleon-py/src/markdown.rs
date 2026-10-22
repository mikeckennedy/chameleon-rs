//! The `markdown:` expression type (Phase 11b): [`MarkdownProfile`], one configured prefix,
//! and the conversion a render runs, through a process-wide LRU cache.
//!
//! A profile is built once, by `chameleon_rs.configure(markdown=...)`, and compiled into each
//! template that uses its prefix: a render never reads settings. The cache is keyed by the
//! profile and an `FxHash` of the text's UTF-8 (Python's own `SipHash` costs about 1 µs for 5 KB on
//! a string seen for the first time, as Markdown loaded per request is); a hit then compares
//! the text itself, so a hash collision can only cost a miss.

use std::borrow::Cow;
use std::collections::HashMap;
use std::hash::Hasher as _;
use std::panic::AssertUnwindSafe;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{LazyLock, Mutex, MutexGuard, PoisonError};

use chameleon_core::markdown::{Element, Extensions, Profile, RawHtml, Settings};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::{MutexExt, PyOnceLock};
use pyo3::types::PyString;

/// The default number of cached conversions (`configure(markdown_cache=...)`).
const DEFAULT_CACHE_SIZE: usize = 1024;

/// Texts longer than this are converted every time rather than cached.
const MAX_CACHED_TEXT: usize = 64 * 1024;

/// The cache's limit on its texts' and HTML's total size, whatever the number of entries.
const MAX_CACHE_BYTES: usize = 64 * 1024 * 1024;

/// Conversions of at least this many bytes release the GIL (or, on free-threaded builds,
/// detach), so other threads run meanwhile; shorter ones finish before that would pay off.
const DETACH_AT: usize = 4096;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

/// One `markdown:`-style prefix: its name and how it converts.
#[pyclass(frozen, module = "chameleon_rs._native")]
pub(crate) struct MarkdownProfile {
    name: String,
    /// Identifies the profile in cache keys (names repeat across configurations).
    id: u64,
    profile: Profile,
}

#[pymethods]
impl MarkdownProfile {
    /// Arguments as `chameleon_rs._settings` normalizes them: `raw_html` is `'escape'` or
    /// `'allow'`, `heading_ids` a prefix or `None`, `classes` `(tag, class)` pairs.
    #[new]
    #[pyo3(signature = (name, *, raw_html, images, inline, heading_ids, classes, table, strikethrough, tasklist, autolink, footnotes))]
    #[allow(clippy::too_many_arguments, clippy::fn_params_excessive_bools)]
    fn new(
        name: String,
        raw_html: &str,
        images: bool,
        inline: bool,
        heading_ids: Option<String>,
        classes: Vec<(String, String)>,
        table: bool,
        strikethrough: bool,
        tasklist: bool,
        autolink: bool,
        footnotes: bool,
    ) -> PyResult<Self> {
        let raw_html = match raw_html {
            "escape" => RawHtml::Escape,
            "allow" => RawHtml::Allow,
            other => {
                return Err(PyValueError::new_err(format!(
                    "markdown: raw_html for '{name}' must be 'escape' or 'allow', not '{other}'"
                )));
            }
        };
        let classes = classes
            .into_iter()
            .map(|(tag, class)| match Element::from_tag(&tag) {
                Some(element) => Ok((element, class)),
                None => Err(PyValueError::new_err(format!(
                    "markdown: classes for '{name}' names an unknown element '{tag}' (one of {})",
                    Element::tags()
                ))),
            })
            .collect::<PyResult<Vec<_>>>()?;
        let settings = Settings {
            raw_html,
            images,
            inline,
            heading_ids,
            classes,
            extensions: Extensions { table, strikethrough, tasklist, autolink, footnotes },
        };
        Ok(Self { name, id: NEXT_ID.fetch_add(1, Ordering::Relaxed), profile: Profile::new(settings) })
    }

    #[getter]
    fn name(&self) -> &str {
        &self.name
    }

    /// `text` converted to HTML, as `Markup` (through the cache, as in a template).
    fn convert<'py>(slf: &Bound<'py, Self>, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        to_markup(slf.get(), text, None)
    }

    fn __repr__(&self) -> String {
        format!("<MarkdownProfile {:?} {:?}>", self.name, self.profile.settings())
    }
}

static MARKUP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn markup<'py>(py: Python<'py>, html: &str) -> PyResult<Bound<'py, PyAny>> {
    let class = MARKUP
        .get_or_try_init(py, || Ok::<_, PyErr>(py.import("chameleon_rs.utils")?.getattr("MarkdownMarkup")?.unbind()))?;
    class.bind(py).call1((html,))
}

/// A `markdown:` value as markup (`MarkdownMarkup`, which attribute values escape): `None` and `''` render nothing, the `default` marker passes
/// through (the element keeps its own content), other non-strings go through `str()`, and
/// strings convert (cached). Lone surrogates, which UTF-8 can't hold, become U+FFFD.
pub(crate) fn to_markup<'py>(
    profile: &MarkdownProfile,
    value: &Bound<'py, PyAny>,
    marker: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    if value.is_none() {
        return markup(py, "");
    }
    if marker.is_some_and(|m| value.is(m)) {
        return Ok(value.clone());
    }
    let text = match value.cast::<PyString>() {
        Ok(s) => s.clone(),
        Err(_) => value.str()?,
    };
    let s = match text.to_str() {
        Ok(s) => Cow::Borrowed(s),
        Err(_) => Cow::Owned(replace_surrogates(&text)?),
    };
    let s: &str = &s;
    if s.is_empty() {
        return markup(py, "");
    }
    let cacheable = s.len() <= MAX_CACHED_TEXT;
    let key = Key { profile: profile.id, hash: if cacheable { text_hash(s) } else { 0 } };
    if cacheable && let Some(hit) = lock(py).get(py, key, s) {
        return Ok(hit.into_bound(py));
    }
    let html = if s.len() >= DETACH_AT { py.detach(|| convert(profile, s)) } else { convert(profile, s) };
    let html = html.map_err(|()| {
        PyRuntimeError::new_err(format!(
            "markdown: converting with the {:?} profile failed (a converter bug)",
            profile.name
        ))
    })?;
    let result = markup(py, &html)?;
    if cacheable {
        let evicted = lock(py).insert(key, s, html.len(), result.clone().unbind());
        // Released after the lock: freeing an object can run Python code.
        drop(evicted);
    }
    Ok(result)
}

/// The conversion, with a panic in the converter caught: it becomes a `RuntimeError` (an
/// `Exception`, which frameworks handle; not a `ValueError`, which a `|` fallback would hide).
fn convert(profile: &MarkdownProfile, text: &str) -> Result<String, ()> {
    std::panic::catch_unwind(AssertUnwindSafe(|| profile.profile.to_html(text))).map_err(|_| ())
}

/// The text with each lone surrogate replaced by U+FFFD (one per surrogate: through UTF-16,
/// where a lone surrogate is one unit; UTF-8 lossy conversion would give one per byte).
fn replace_surrogates(text: &Bound<'_, PyString>) -> PyResult<String> {
    let bytes = text.call_method1("encode", ("utf-16-le", "surrogatepass"))?;
    let bytes: &[u8] = bytes.extract()?;
    let (pairs, _) = bytes.as_chunks::<2>();
    let units: Vec<u16> = pairs.iter().map(|pair| u16::from_le_bytes(*pair)).collect();
    Ok(String::from_utf16_lossy(&units))
}

fn text_hash(text: &str) -> u64 {
    let mut hasher = rustc_hash::FxHasher::default();
    hasher.write(text.as_bytes());
    hasher.finish()
}

/// The cache, recovered if a panic poisoned the lock: no update can stop halfway (the map's
/// own operations don't panic), so its state is always whole.
fn lock(py: Python<'_>) -> MutexGuard<'static, Cache> {
    CACHE.lock_py_attached(py).unwrap_or_else(PoisonError::into_inner)
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct Key {
    profile: u64,
    hash: u64,
}

struct Entry {
    text: Box<str>,
    html: Py<PyAny>,
    /// The text's and the HTML's length, counted against [`MAX_CACHE_BYTES`].
    bytes: usize,
    stamp: u64,
}

/// A bounded LRU map from (profile, text) to the converted `Markup`, limited both in entries
/// and in bytes. Two texts with the same hash share a slot (the later one replaces the earlier).
struct Cache {
    capacity: usize,
    bytes: usize,
    clock: u64,
    entries: HashMap<Key, Entry>,
}

static CACHE: LazyLock<Mutex<Cache>> =
    LazyLock::new(|| Mutex::new(Cache { capacity: DEFAULT_CACHE_SIZE, bytes: 0, clock: 0, entries: HashMap::new() }));

impl Cache {
    fn get(&mut self, py: Python<'_>, key: Key, text: &str) -> Option<Py<PyAny>> {
        self.clock += 1;
        let entry = self.entries.get_mut(&key)?;
        if &*entry.text != text {
            return None;
        }
        entry.stamp = self.clock;
        // Only increments the reference count: no Python code runs under the lock.
        Some(entry.html.clone_ref(py))
    }

    /// Adds a conversion; returns what it evicted or replaced, for the caller to drop after
    /// releasing the lock.
    #[must_use]
    fn insert(&mut self, key: Key, text: &str, html_len: usize, html: Py<PyAny>) -> Vec<Entry> {
        let bytes = text.len() + html_len;
        if self.capacity == 0 {
            return vec![Entry { text: Box::from(""), html, bytes: 0, stamp: 0 }];
        }
        self.clock += 1;
        let mut evicted = Vec::new();
        if let Some(old) = self.entries.insert(key, Entry { text: text.into(), html, bytes, stamp: self.clock }) {
            self.bytes -= old.bytes;
            evicted.push(old);
        }
        self.bytes += bytes;
        if self.entries.len() > self.capacity || self.bytes > MAX_CACHE_BYTES {
            // Least recently used first, down to seven eighths of both limits, so evictions
            // come in batches (amortized logarithmic time per insert).
            let keep = self.capacity - self.capacity / 8;
            let keep_bytes = MAX_CACHE_BYTES - MAX_CACHE_BYTES / 8;
            let mut by_age: Vec<(u64, Key)> = self.entries.iter().map(|(k, e)| (e.stamp, *k)).collect();
            by_age.sort_unstable_by_key(|(stamp, _)| *stamp);
            for (_, k) in by_age {
                if self.entries.len() <= keep && self.bytes <= keep_bytes {
                    break;
                }
                if let Some(e) = self.entries.remove(&k) {
                    self.bytes -= e.bytes;
                    evicted.push(e);
                }
            }
        }
        evicted
    }
}

/// Sets the cache's size (`0` turns it off) and empties it (`configure(markdown=...)` and
/// `configure(markdown_cache=...)` call it; profiles of an earlier configuration are gone).
#[pyfunction]
pub(crate) fn set_markdown_cache(py: Python<'_>, size: usize) {
    let old = {
        let mut cache = lock(py);
        cache.capacity = size;
        cache.bytes = 0;
        std::mem::take(&mut cache.entries)
    };
    // Released after the lock: freeing an object can run Python code.
    drop(old);
}

/// `(size, entries, bytes)`, for tests.
#[pyfunction]
pub(crate) fn markdown_cache_info(py: Python<'_>) -> (usize, usize, usize) {
    let cache = lock(py);
    (cache.capacity, cache.entries.len(), cache.bytes)
}

/// The cache's limits, for `chameleon_rs._settings`: (default entries, largest cached text,
/// total bytes).
#[pyfunction]
pub(crate) fn markdown_cache_limits() -> (usize, usize, usize) {
    (DEFAULT_CACHE_SIZE, MAX_CACHED_TEXT, MAX_CACHE_BYTES)
}
