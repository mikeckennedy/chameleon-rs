// The TALES handling is ported from Chameleon's src/chameleon/tales.py (`TalesExpr.__call__`,
// `PythonExpr.translate`, `ProxyExpr.translate_proxy`, `NotExpr`, `ExistsExpr`,
// `StructureExpr`, `ImportExpr`, `StringExpr`, `ExpressionParser.__call__`, `match_prefix`,
// `split_parts`, `re_continuation`) and compiler.py (`ExpressionTransform.__call__`'s
// non-strict deferral), modified: rewritten in Rust, producing the native expression tree
// instead of generating Python code.

//! The real [`ExprCompiler`] (D1, D4): TALES prefixes and pipes, the expression types, and
//! CPython's own parser for `python:` with a native fast path (`lower.rs`) and an exact
//! fallback.

use std::collections::HashMap;
use std::sync::Arc;

use pyo3::exceptions::{PyLookupError, PySyntaxError, PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString, PyTuple};

use chameleon_core::{
    DefaultAlias, Escape, ExprCompileError, ExprCompiler, ExprContext, ExprId, FreeVars, Quote, Source, Tok, TokPos,
    is_python_space,
};

use crate::custom::{Custom, Sub, SubKind};
use crate::errors::unsupported_error;
use crate::expr::{Expr, Fallback, NsCode, TextConv};

/// An entry of a program's token table, resolved at compile time so the error path costs
/// a slice: Chameleon re-slices the template source at the token's position for its length
/// (`start..end`, bytes; empty past the end of the source), and reports the line and column of
/// that position (`Token.location`).
#[derive(Clone, Copy, Debug)]
pub(crate) struct TokenSpan {
    pub(crate) start: u32,
    pub(crate) end: u32,
    pub(crate) line: u32,
    pub(crate) column: u32,
}

impl TokenSpan {
    /// Resolves a token of `chars` code points at `pos` (code points past `pos.byte`, which
    /// may run past the end of the source, as Chameleon's split arithmetic can).
    fn new(source: &Source<'_>, pos: TokPos, chars: usize) -> Self {
        let text = source.text();
        let (start, beyond) = advance(text, pos.byte as usize, pos.chars as usize);
        let end = if beyond > 0 { start } else { advance(text, start, chars).0 };
        let (line, column) = source.location(start);
        let n = |v: usize| u32::try_from(v).unwrap_or(u32::MAX);
        Self { start: n(start), end: n(end), line: n(line), column: n(column + beyond) }
    }
}

/// The byte offset `n` code points after byte `from`, and how many code points were left
/// over at the end of `text`.
fn advance(text: &str, from: usize, n: usize) -> (usize, usize) {
    let Some(rest) = text.get(from..) else { return (text.len(), n) };
    let mut chars = rest.char_indices();
    match chars.nth(n) {
        Some((i, _)) => (from + i, 0),
        None => (text.len(), n - rest.chars().count()),
    }
}

pub(crate) type CompileResult<T> = Result<T, ExprCompileError<PyErr>>;

/// An entry of a template's `expression_types`, as `chameleon_rs.tales.native_types`
/// classifies it: one of Chameleon's own types, compiled natively, or a user's factory.
pub(crate) enum TypeSpec<'py> {
    Python,
    String {
        braces_required: bool,
    },
    Not,
    Exists,
    Import,
    Structure,
    /// `ProxyExpr(name, expression, ignore_prefix)` (`load:` is `ProxyExpr('__loader',
    /// ignore_prefix=False)`): each alternative is an interpolated string passed to `name`.
    Proxy {
        name: String,
        ignore_prefix: bool,
    },
    Custom(Bound<'py, PyAny>),
}

/// Compile-time configuration from the template class.
pub(crate) struct Config<'py> {
    pub(crate) types: HashMap<String, TypeSpec<'py>>,
    pub(crate) default_type: String,
    /// The template builtins' names (`template`, `macros`, `nothing`, `__loader`, ...).
    pub(crate) template_builtins: Vec<String>,
    /// `false` compiles every Python expression to the fallback (benchmarks, D4 revisit).
    pub(crate) fast_path: bool,
}

impl<'py> Config<'py> {
    /// Chameleon's `PageTemplate` defaults.
    pub(crate) fn page_template() -> Self {
        let types = [
            ("python", TypeSpec::Python),
            ("string", TypeSpec::String { braces_required: false }),
            ("not", TypeSpec::Not),
            ("exists", TypeSpec::Exists),
            ("import", TypeSpec::Import),
            ("structure", TypeSpec::Structure),
        ];
        Self {
            types: types.into_iter().map(|(k, v)| (k.to_owned(), v)).collect(),
            default_type: "python".to_owned(),
            template_builtins: ["macros", "nothing", "template"].map(String::from).to_vec(),
            fast_path: true,
        }
    }

    /// Reads the `(prefix, spec)` pairs `chameleon_rs.tales.native_types` returns: `spec` is
    /// `('python',)`, `('string', braces_required)`, `('not',)`, `('exists',)`, `('import',)`,
    /// `('structure',)`, `('proxy', name, ignore_prefix)` or `('custom', factory)`.
    pub(crate) fn set_types(&mut self, specs: &Bound<'py, PyAny>) -> PyResult<()> {
        self.types.clear();
        for item in specs.try_iter()? {
            let item = item?;
            let (prefix, spec): (String, Bound<'py, PyTuple>) = item.extract()?;
            let kind: String = spec.get_item(0)?.extract()?;
            let spec = match kind.as_str() {
                "python" => TypeSpec::Python,
                "string" => TypeSpec::String { braces_required: spec.get_item(1)?.is_truthy()? },
                "not" => TypeSpec::Not,
                "exists" => TypeSpec::Exists,
                "import" => TypeSpec::Import,
                "structure" => TypeSpec::Structure,
                "proxy" => TypeSpec::Proxy {
                    name: spec.get_item(1)?.extract()?,
                    ignore_prefix: spec.get_item(2)?.is_truthy()?,
                },
                "custom" => TypeSpec::Custom(spec.get_item(1)?),
                other => return Err(PyValueError::new_err(format!("unknown expression type kind {other:?}"))),
            };
            self.types.insert(prefix, spec);
        }
        Ok(())
    }
}

/// Which `TalesExpr` subclass splits the alternatives.
#[derive(Clone, Copy)]
enum Alt<'a> {
    Python,
    Proxy(&'a str),
}

pub(crate) struct PyExprCompiler<'a, 'py> {
    pub(crate) py: Python<'py>,
    pub(crate) exprs: Vec<Expr>,
    /// Each expression's error token (index into `token_table` plus one; `0` for a source
    /// without a position, which leaves the previous token in place, as in Chameleon).
    pub(crate) tokens: Vec<u32>,
    pub(crate) token_table: Vec<TokenSpan>,
    /// Whether each expression contains nested tokens ([`Expr::Token`]).
    pub(crate) has_nested: Vec<bool>,
    /// Set while compiling an expression that gets nested tokens.
    nested: bool,
    /// Whether each expression makes its render function a generator (a `yield`).
    generators: Vec<bool>,
    /// Set while compiling an expression or code block with a `yield`.
    generator: bool,
    pub(crate) free: Vec<FreeVars>,
    /// Names `<?python ?>` blocks defined so far (`def` names, their parameters, imports):
    /// plain names in every later expression, which then runs in the render's namespace.
    local_names: Option<Bound<'py, PySet>>,
    /// The ones the render function itself binds.
    bound_names: Option<Bound<'py, PySet>>,
    /// Whether any expression mentions `repeat`, `econtext`, `rcontext` or a render default
    /// (so a native partial frame gets the whole scope a Python render would give it).
    pub(crate) mentions_scope: bool,
    pub(crate) config: Config<'py>,
    /// The `attrs` dict of each element, by `StaticAttrs::id`.
    pub(crate) attrs_dicts: HashMap<u32, Bound<'py, PyDict>>,
    ast_parse: Bound<'py, PyAny>,
    eval_mode: Bound<'py, PyDict>,
    pub(crate) builtins: Bound<'py, PyDict>,
    rewrite: Bound<'py, PyAny>,
    /// `chameleon_rs._rewrite.constant` and the template's constant cache: equal constants
    /// are one object template-wide, as in Chameleon's single generated module.
    constant: Bound<'py, PyAny>,
    fold: Bound<'py, PyAny>,
    pub(crate) constants: Bound<'py, PyDict>,
    bridge: Option<Bound<'py, PyAny>>,
    pub(crate) lookup_attr: Bound<'py, PyAny>,
    pub(crate) marker: Bound<'py, PyAny>,
    /// The template source and filename, for the position of a deferred error.
    source: &'a str,
    /// Its line index, for error tokens.
    index: Source<'a>,
    filename: &'a str,
    /// The private-use block the source's lone surrogates were mapped to
    /// (`chameleon_core::surrogates`): source text given to Python gets them back.
    pub(crate) surrogate_base: Option<u32>,
}

/// Names whose mention makes a native partial frame build the whole scope (see
/// `Program::full_scope`).
const SCOPE_NAMES: [&str; 7] =
    ["repeat", "econtext", "rcontext", "target_language", "__translate", "__decode", "__on_error_handler"];

impl<'a, 'py> PyExprCompiler<'a, 'py> {
    pub(crate) fn new(
        py: Python<'py>,
        marker: Bound<'py, PyAny>,
        config: Config<'py>,
        source: &'a str,
        filename: &'a str,
    ) -> PyResult<Self> {
        let eval_mode = PyDict::new(py);
        // `mode` must be a keyword: ast.parse's second positional parameter is `filename`.
        eval_mode.set_item(intern!(py, "mode"), intern!(py, "eval"))?;
        let rewrite = py.import(intern!(py, "chameleon_rs._rewrite"))?;
        Ok(Self {
            py,
            exprs: Vec::new(),
            tokens: Vec::new(),
            token_table: Vec::new(),
            has_nested: Vec::new(),
            nested: false,
            generators: Vec::new(),
            generator: false,
            free: Vec::new(),
            local_names: None,
            bound_names: None,
            mentions_scope: false,
            config,
            attrs_dicts: HashMap::new(),
            ast_parse: py.import(intern!(py, "ast"))?.getattr(intern!(py, "parse"))?,
            eval_mode,
            builtins: py.import(intern!(py, "builtins"))?.dict(),
            constant: rewrite.getattr(intern!(py, "constant"))?,
            fold: rewrite.getattr(intern!(py, "fold"))?,
            constants: PyDict::new(py),
            rewrite: rewrite.getattr(intern!(py, "compile_expression"))?,
            bridge: None,
            lookup_attr: wrap_pyfunction!(crate::expr::py_lookup_attr, py)?.into_any(),
            marker,
            source,
            index: Source::new(source),
            filename,
            surrogate_base: None,
        })
    }

    /// `ExpressionParser.__call__`: an optional `type:` prefix selects the expression type,
    /// else the default type. Sources are `Tok`s so syntax errors carry the position
    /// Chameleon's slicing gives. `top`: the whole emitted expression (where a native
    /// `render_partial` frame is possible).
    pub(crate) fn typed(&mut self, source: &Tok, cx: ExprContext<'_>, top: bool) -> CompileResult<Expr> {
        let (prefix, expression) = match match_prefix(&source.text) {
            Some((prefix, rest)) => (prefix.to_owned(), source.slice_from(source.text.len() - rest.len())),
            None => (self.config.default_type.clone(), source.clone()),
        };
        let spec = match self.config.types.get(&prefix) {
            Some(TypeSpec::Python) => Spec::Python,
            Some(TypeSpec::String { braces_required }) => Spec::String(*braces_required),
            Some(TypeSpec::Not) => Spec::Not,
            Some(TypeSpec::Exists) => Spec::Exists,
            Some(TypeSpec::Import) => Spec::Import,
            Some(TypeSpec::Structure) => Spec::Structure,
            Some(TypeSpec::Proxy { name, ignore_prefix }) => Spec::Proxy(name.clone(), *ignore_prefix),
            Some(TypeSpec::Custom(factory)) => Spec::Custom(factory.clone()),
            // `LookupError("Unknown expression type: %s." % str(KeyError(prefix)))`.
            None => {
                let message = format!("Unknown expression type: '{prefix}'.");
                return Err(ExprCompileError::Other(PyLookupError::new_err(message)));
            }
        };
        let inner = ExprContext { emitted: false, ..cx };
        Ok(match spec {
            Spec::Python => self.tales(&expression, cx, top, true, Alt::Python)?,
            Spec::Proxy(name, ignore_prefix) => self.tales(&expression, cx, false, ignore_prefix, Alt::Proxy(&name))?,
            Spec::String(braces_required) => Expr::Interp(Box::new(self.string(&expression, braces_required, cx)?)),
            // `engine.parse(self.expression)`: the rest may have a prefix of its own.
            // `engine.parse(self.expression)` (a `TokenRef` to the stripped rest); `exists:`
            // parses with `handle_errors=False`, so its token stays the outer one.
            Spec::Not => Expr::Not(Box::new(self.typed_ref(&expression, inner)?)),
            Spec::Exists => Expr::Exists(Box::new(self.typed(&expression, inner, false)?)),
            Spec::Structure => Expr::Structure(Box::new(self.typed_ref(&expression, inner)?)),
            // `self.expression.strip().replace('\n', ' ')`.
            Spec::Import => {
                let name = expression.text.trim_matches(is_python_space).replace('\n', " ");
                if self.has_mapped(&name) {
                    Expr::Import(self.source_str(&name).map_err(ExprCompileError::Other)?.unbind())
                } else {
                    Expr::Import(PyString::intern(self.py, &name).unbind())
                }
            }
            Spec::Custom(factory) => self.custom(&factory, &expression, inner)?,
        })
    }

    /// `engine.parse(source)` inside an expression type: [`typed`](Self::typed), with the
    /// stripped source as the error token while it evaluates.
    fn typed_ref(&mut self, source: &Tok, cx: ExprContext<'_>) -> CompileResult<Expr> {
        let expr = self.typed(source, cx, false)?;
        let token = self.token_id(&strip_tok(source));
        if token == 0 {
            return Ok(expr);
        }
        // The enclosing site is registered next (sub-expressions are not registered).
        let site = u32::try_from(self.exprs.len() + 1).unwrap_or(u32::MAX);
        self.nested = true;
        Ok(Expr::Token(site, token, Box::new(expr)))
    }

    /// Whether `text` holds a lone surrogate (mapped to the private-use block).
    fn has_mapped(&self, text: &str) -> bool {
        self.surrogate_base.is_some_and(|base| chameleon_core::surrogates::to_wtf8(text, base).is_some())
    }

    /// A `str` for a piece of the source, its lone surrogates mapped back.
    fn source_str(&self, text: &str) -> PyResult<Bound<'py, PyString>> {
        crate::convert::source_str(self.py, text, self.surrogate_base)
    }

    /// Records an error token (`0` for a plain string without a position).
    fn token_id(&mut self, tok: &Tok) -> u32 {
        let Some(pos) = tok.pos else { return 0 };
        let span = TokenSpan::new(&self.index, pos, tok.text.chars().count());
        self.token_table.push(span);
        u32::try_from(self.token_table.len()).unwrap_or(0)
    }

    /// `TalesExpr.__call__`: alternatives separated by unescaped `|`; with `ignore_prefix`, a
    /// part with a type prefix takes the rest of the expression.
    fn tales(
        &mut self,
        expression: &Tok,
        cx: ExprContext<'_>,
        top: bool,
        ignore_prefix: bool,
        alt: Alt<'_>,
    ) -> CompileResult<Expr> {
        let mut alternatives = Vec::new();
        let mut remaining = expression.clone();
        while !remaining.is_empty() {
            if ignore_prefix && match_prefix(&remaining.text).is_some() {
                alternatives.push(self.typed_ref(&remaining, ExprContext { emitted: false, ..cx })?);
                remaining = remaining.slice_from(remaining.text.len());
                break;
            }
            let (part, rest) = split_pipe(&remaining.text);
            let (part_len, rest_at) = (part.len(), remaining.text.len() - rest.len());
            let part = remaining.slice(0, part_len);
            let part = part.with_text(part.text.replace("\\|", "|"));
            remaining = remaining.slice_from(rest_at);
            let only = top && remaining.is_empty() && alternatives.is_empty();
            alternatives.push(match alt {
                Alt::Python => self.python(&part, cx, only)?,
                Alt::Proxy(name) => self.proxy(&part, name, cx)?,
            });
        }
        match alternatives.len() {
            0 => Err(ExprCompileError::Invalid { message: "No input:".to_owned(), token: Some(remaining) }),
            1 => Ok(alternatives.remove(0)),
            _ => Ok(Expr::Pipe(alternatives)),
        }
    }

    /// `PythonExpr.translate`, then lowering. `only` is true for a whole emitted expression
    /// (where a native `render_partial` frame is possible).
    fn python(&mut self, expression: &Tok, cx: ExprContext<'_>, only: bool) -> CompileResult<Expr> {
        // `expression.strip()` is the token a syntax error reports.
        let stripped = strip_tok(expression);
        let string = continuation_to_spaces(&stripped.text);
        let invalid = |message: String| ExprCompileError::Invalid { message, token: Some(stripped.clone()) };
        if self.has_mapped(&string) {
            // A lone surrogate: Python's parser raises `UnicodeEncodeError` on the source, as
            // in Chameleon.
            let source = self.source_str(&string).map_err(ExprCompileError::Other)?;
            return match self.ast_parse.call((source,), Some(&self.eval_mode)) {
                Err(e) => Err(ExprCompileError::Other(e)),
                Ok(_) => Err(ExprCompileError::Other(PyValueError::new_err("a lone surrogate parsed"))),
            };
        }
        if cx.candidate && obviously_invalid(&string) {
            return Err(invalid("invalid syntax".to_owned()));
        }
        if SCOPE_NAMES.iter().any(|name| string.contains(name)) {
            self.mentions_scope = true;
        }
        let tree = match self.ast_parse.call((string.as_str(),), Some(&self.eval_mode)) {
            Ok(tree) => tree,
            Err(e) if e.is_instance_of::<PySyntaxError>(self.py) => {
                let message = e.value(self.py).getattr(intern!(self.py, "msg")).and_then(|m| m.extract::<String>());
                return Err(invalid(message.unwrap_or_else(|_| "invalid syntax".into())));
            }
            Err(e) => return Err(ExprCompileError::Other(e)),
        };
        let body = tree.getattr(intern!(self.py, "body")).map_err(ExprCompileError::Other)?;
        let lowered = (|| {
            if only
                && cx.emitted
                && let Some(partial) = self.partial(&body, cx)?
            {
                return Ok(Some(partial));
            }
            // Once a code block has defined names, expressions may read the render's locals:
            // the fallback's rewriter decides.
            if !self.config.fast_path || self.local_names.as_ref().is_some_and(|s| !s.is_empty()) {
                return Ok(None);
            }
            self.lower(&body, cx)
        })()
        .map_err(ExprCompileError::Other)?;
        if let Some(expr) = lowered {
            return Ok(expr);
        }
        self.fallback(&tree, cx).map_err(ExprCompileError::Other)
    }

    /// The exact fallback: Chameleon's rewriting applied to the tree, compiled to a function.
    fn fallback(&mut self, tree: &Bound<'py, PyAny>, cx: ExprContext<'_>) -> PyResult<Expr> {
        let yields = self.py.import(intern!(self.py, "chameleon_rs._rewrite"))?.getattr(intern!(self.py, "yields"))?;
        if yields.call1((tree,))?.is_truthy()? {
            self.generator = true;
        }
        let attrs = self.attrs_dict(cx)?;
        let kwargs = PyDict::new(self.py);
        kwargs
            .set_item(intern!(self.py, "template_builtins"), PyTuple::new(self.py, &self.config.template_builtins)?)?;
        kwargs.set_item(intern!(self.py, "constants"), &self.constants)?;
        kwargs.set_item(intern!(self.py, "local_names"), &self.local_names)?;
        let result =
            self.rewrite.call((tree, default_alias(self.py, cx.default), &attrs, &self.lookup_attr), Some(&kwargs))?;
        let (func, reads, writes, dynamic, in_namespace): (Bound<'py, PyAny>, Vec<String>, Vec<String>, bool, bool) =
            result.extract()?;
        if in_namespace {
            let attrs = attrs.map(Bound::unbind);
            let bound = self.bound_names()?.unbind();
            return Ok(Expr::Ns(Box::new(NsCode { code: func.unbind(), attrs, exec: false, bound })));
        }
        let free = if dynamic {
            FreeVars::Dynamic
        } else {
            FreeVars::Known {
                reads: reads.into_iter().map(String::into_boxed_str).collect(),
                writes: writes.into_iter().map(String::into_boxed_str).collect(),
            }
        };
        Ok(Expr::Fallback(Box::new(Fallback { func: func.unbind(), free })))
    }

    /// `ProxyExpr.translate_proxy`: the alternative as an interpolated string (braces
    /// optional), passed to the function bound to `name` (resolved like a Python name).
    fn proxy(&mut self, part: &Tok, name: &str, cx: ExprContext<'_>) -> CompileResult<Expr> {
        let path = Expr::Interp(Box::new(self.string(part, false, cx)?));
        let inner = ExprContext { emitted: false, candidate: false, ..cx };
        let func = match self.name_expr(name, inner).map_err(ExprCompileError::Other)? {
            Some(func) => func,
            None => self.python(&Tok::plain(name), inner, false)?,
        };
        Ok(Expr::Call { func: Box::new(func), args: vec![path], kwargs: Vec::new() })
    }

    /// `StringExpr` / the paths of `ProxyExpr`: Chameleon's `Interpolator` with `translate`
    /// and entity decoding off, each `${...}` or `$name` compiled as a full TALES expression
    /// and converted to text as the site's engine converts text.
    fn string(
        &mut self,
        expression: &Tok,
        braces_required: bool,
        cx: ExprContext<'_>,
    ) -> CompileResult<crate::expr::StrInterp> {
        let inner = ExprContext { emitted: false, ..cx };
        let py = self.py;
        let base = self.surrogate_base;
        let parts = crate::strexpr::interpolate(py, expression, braces_required, base, |s, candidate| {
            self.typed_ref(s, ExprContext { candidate, ..inner })
        })?;
        let engine = cx.engine;
        let conv = TextConv {
            escape: engine.escape,
            marker: engine.marker,
            default: engine.default.map(|d| PyString::new(self.py, d).unbind()),
            literal_false: engine.literal_false,
        };
        Ok(crate::expr::StrInterp { parts, conv })
    }

    /// A custom expression type through the AST bridge (`chameleon_rs._bridge.build`).
    fn custom(&mut self, factory: &Bound<'py, PyAny>, expression: &Tok, cx: ExprContext<'_>) -> CompileResult<Expr> {
        let py = self.py;
        let other = ExprCompileError::Other;
        let bridge = if let Some(b) = &self.bridge {
            b.clone()
        } else {
            let b = py.import(intern!(py, "chameleon_rs._bridge")).and_then(|m| m.getattr(intern!(py, "build")));
            let b = b.map_err(other)?;
            self.bridge = Some(b.clone());
            b
        };
        let attrs = self.attrs_dict(cx).map_err(other)?;
        let kwargs = PyDict::new(py);
        let set = |k: &Bound<'py, PyString>, v: Bound<'py, PyAny>| kwargs.set_item(k, v);
        set(intern!(py, "default_is_marker"), default_alias(py, cx.default)).map_err(other)?;
        set(intern!(py, "attrs"), attrs.map_or_else(|| py.None().into_bound(py), Bound::into_any)).map_err(other)?;
        set(intern!(py, "lookup_attr"), self.lookup_attr.clone()).map_err(other)?;
        let builtins = PyTuple::new(py, &self.config.template_builtins).map_err(other)?;
        set(intern!(py, "template_builtins"), builtins.into_any()).map_err(other)?;
        let text = self.source_str(&expression.text).map_err(other)?;
        let result = match bridge.call((factory, text), Some(&kwargs)) {
            Ok(r) => r,
            Err(e) => return Err(expression_error(py, e, expression)),
        };
        let (func, requests): (Bound<'py, PyAny>, Bound<'py, PyList>) = result.extract().map_err(other)?;
        let mut subs = Vec::with_capacity(requests.len());
        for request in requests.iter() {
            let (string, kind, char_escape, s): (String, String, Bound<'py, PyAny>, Bound<'py, PyAny>) =
                request.extract().map_err(other)?;
            let expr = self.typed(&Tok::plain(string), cx, false)?;
            let engine = cx.engine;
            let default = engine.default.map(|d| PyString::new(py, d).unbind());
            let kind = match kind.as_str() {
                "value" => SubKind::Value,
                "bool" => SubKind::Bool { s: s.unbind(), marker: engine.marker, default },
                _ => {
                    let escape = if char_escape.is_none() {
                        engine.escape
                    } else {
                        char_escape_of(&char_escape).map_err(other)?
                    };
                    SubKind::Text(TextConv {
                        escape,
                        marker: engine.marker,
                        default,
                        literal_false: engine.literal_false,
                    })
                }
            };
            subs.push(Sub { expr, kind });
        }
        Ok(Expr::Custom(Box::new(Custom { func: func.unbind(), subs: Arc::from(subs) })))
    }

    /// The `attrs` alias: the element's static attributes as a dict. One object per element,
    /// shared by its expressions and across renders, like Chameleon's compile-time `Static`
    /// (a template may mutate it: `attrs.setdefault(...)` persists into the next render). The
    /// expressions and fallback functions that use it keep it alive.
    pub(crate) fn attrs_dict(&mut self, cx: ExprContext<'_>) -> PyResult<Option<Bound<'py, PyDict>>> {
        let Some(attrs) = cx.attrs else { return Ok(None) };
        if let Some(d) = self.attrs_dicts.get(&attrs.id) {
            return Ok(Some(d.clone()));
        }
        let d = PyDict::new(self.py);
        for (k, v) in attrs.items {
            d.set_item(k, v)?;
        }
        self.attrs_dicts.insert(attrs.id, d.clone());
        Ok(Some(d))
    }

    /// The template's one object for a constant value.
    pub(crate) fn constant(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.constant.call1((&self.constants, value))
    }

    /// CPython's own constant folding of `node`, whose operands lowered to literals: the folded
    /// constant if the running interpreter's compiler folds it (see `_rewrite.fold`).
    pub(crate) fn fold(&self, node: &Bound<'py, PyAny>) -> PyResult<Option<Expr>> {
        let (folded, value): (bool, Bound<'py, PyAny>) = self.fold.call1((&self.constants, node))?.extract()?;
        Ok(folded.then(|| Expr::Const(value.unbind())))
    }

    fn register(&mut self, expr: Expr, token: u32) -> CompileResult<ExprId> {
        let id = u32::try_from(self.exprs.len())
            .map_err(|_| ExprCompileError::Other(unsupported_error(self.py, "More than 2**32 expressions")))?;
        self.free.push(expr.free_vars());
        self.exprs.push(expr);
        self.tokens.push(token);
        self.has_nested.push(std::mem::take(&mut self.nested));
        self.generators.push(std::mem::take(&mut self.generator));
        Ok(ExprId(id))
    }

    /// `visit_CodeBlock` (see `chameleon_rs._rewrite.compile_code`).
    fn code_block(&mut self, source: &Tok) -> PyResult<Expr> {
        let py = self.py;
        let local_names = if let Some(s) = &self.local_names {
            s.clone()
        } else {
            let s = PySet::empty(py)?;
            self.local_names = Some(s.clone());
            s
        };
        let bound_names = self.bound_names()?;
        let compile = py.import(intern!(py, "chameleon_rs._rewrite"))?.getattr(intern!(py, "compile_code"))?;
        let builtins = PyTuple::new(py, &self.config.template_builtins)?;
        let text = self.source_str(&source.text)?;
        let code = compile.call1((text, local_names, &bound_names, builtins, &self.constants))?;
        if code.is_none() {
            // A `yield`: the function this block belongs to never runs.
            self.generator = true;
            return Ok(Expr::Const(py.None()));
        }
        let bound = bound_names.unbind();
        Ok(Expr::Ns(Box::new(NsCode { code: code.unbind(), attrs: None, exec: true, bound })))
    }

    fn bound_names(&mut self) -> PyResult<Bound<'py, PySet>> {
        if let Some(s) = &self.bound_names {
            return Ok(s.clone());
        }
        let s = PySet::empty(self.py)?;
        self.bound_names = Some(s.clone());
        Ok(s)
    }
}

/// `typed`'s choice, owned so the compiler can be borrowed mutably while building.
enum Spec<'py> {
    Python,
    String(bool),
    Not,
    Exists,
    Import,
    Structure,
    Proxy(String, bool),
    Custom(Bound<'py, PyAny>),
}

/// A custom type's `ExpressionError` drives the Interpolator's retry like any other;
/// anything else aborts compilation.
fn expression_error(py: Python<'_>, e: PyErr, expression: &Tok) -> ExprCompileError<PyErr> {
    let is_expression_error = py
        .import(intern!(py, "chameleon_rs.exc"))
        .and_then(|m| m.getattr(intern!(py, "ExpressionError")))
        .is_ok_and(|cls| e.value(py).is_instance(&cls).unwrap_or(false));
    if !is_expression_error {
        return ExprCompileError::Other(e);
    }
    let value = e.value(py);
    let args = value.getattr(intern!(py, "args")).ok();
    let arg = |i: usize| args.as_ref().and_then(|a| a.get_item(i).ok()).and_then(|v| v.extract::<String>().ok());
    let message = arg(0).unwrap_or_default();
    let token = arg(1);
    let token = token.map_or_else(|| expression.clone(), Tok::plain);
    ExprCompileError::Invalid { message, token: Some(token) }
}

/// `char_escape` passed to `engine.parse`: how `_convert_text` picks the quote.
fn char_escape_of(chars: &Bound<'_, PyAny>) -> PyResult<Option<Escape>> {
    let chars: Vec<String> = chars.try_iter()?.map(|c| c?.extract()).collect::<PyResult<_>>()?;
    if chars.is_empty() {
        return Ok(None);
    }
    let other: Vec<&str> = chars.iter().map(String::as_str).filter(|c| !["&", "<", ">"].contains(c)).collect();
    if other.is_empty() {
        return Ok(Some(Escape::Text));
    }
    for (quote, escape) in [("\"", Quote::Double), ("'", Quote::Single), ("", Quote::Bare)] {
        if chars.iter().any(|c| c == quote) {
            return Ok(Some(Escape::Attr(escape)));
        }
    }
    Err(PyTypeError::new_err(format!("Unsupported escape set: {chars:?}.")))
}

impl ExprCompiler for PyExprCompiler<'_, '_> {
    type Error = PyErr;

    /// The expression's error token is its stripped source (`TokenRef(string.strip())`).
    fn compile(&mut self, source: &Tok, cx: ExprContext<'_>) -> Result<ExprId, ExprCompileError<PyErr>> {
        // (A rejected Interpolator candidate may have set it.)
        self.generator = false;
        let expr = self.typed(source, cx, true)?;
        let token = self.token_id(&strip_tok(source));
        self.register(expr, token)
    }

    /// A code block's error token is its whole source, unstripped (`TokenRef(node.source)`).
    fn compile_code(&mut self, source: &Tok) -> Result<ExprId, ExprCompileError<PyErr>> {
        self.generator = false;
        let expr = self.code_block(source).map_err(ExprCompileError::Other)?;
        let token = self.token_id(source);
        self.register(expr, token)
    }

    /// `ExpressionError(message, token)` raised at render time, with the token's code-point
    /// offset and location computed now.
    fn deferred(&mut self, message: &str, token: &Tok) -> Result<ExprId, ExprCompileError<PyErr>> {
        let py = self.py;
        // Chameleon's `ExpressionError(msg, token)`, with the token's source position.
        let token_obj = match token.pos {
            Some(p) => {
                let offset = p.offset(&Source::new(self.source));
                crate::errors::make_token(py, &token.text, offset, self.source, self.filename, self.surrogate_base)
            }
            None => self.source_str(&token.text).map(Bound::into_any),
        }
        .map_err(ExprCompileError::Other)?;
        let args = (message, token_obj).into_pyobject(py).map_err(ExprCompileError::Other)?;
        // `TokenRef(exc.token)` precedes the raise.
        let token_id = self.token_id(token);
        self.register(Expr::Raise(args.unbind()), token_id)
    }

    fn free_vars(&self, id: ExprId) -> FreeVars {
        self.free.get(id.0 as usize).cloned().unwrap_or_default()
    }

    fn is_generator(&self, id: ExprId) -> bool {
        self.generators.get(id.0 as usize).copied().unwrap_or(false)
    }
}

/// `Token.strip()` with Python's whitespace (the position moves past the leading part).
fn strip_tok(t: &Tok) -> Tok {
    let lead = t.text.len() - t.text.trim_start_matches(is_python_space).len();
    let chars = t.text[..lead].chars().count();
    Tok { text: t.text.trim_matches(is_python_space).to_owned(), pos: t.pos.map(|p| p.plus(chars)) }
}

/// `match_prefix`: `^\s*([a-z][a-z0-9\-_]*):`. Returns the prefix and the text after `:`.
pub(crate) fn match_prefix(s: &str) -> Option<(&str, &str)> {
    let t = s.trim_start_matches(is_python_space);
    let first = t.chars().next()?;
    if !first.is_ascii_lowercase() {
        return None;
    }
    let len = t.find(|c: char| !(c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '_'))?;
    let rest = t.get(len..)?.strip_prefix(':')?;
    Some((t.get(..len)?, rest))
}

/// Splits at the first `|` not preceded by a backslash (`(?<!\\)\|`).
fn split_pipe(s: &str) -> (&str, &str) {
    let bytes = s.as_bytes();
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'|' && (i == 0 || bytes[i - 1] != b'\\') {
            return (s.get(..i).unwrap_or_default(), s.get(i + 1..).unwrap_or_default());
        }
    }
    (s, "")
}

/// `re_continuation.sub('\n', s)` with `\\\s*$` (MULTILINE), then newlines to spaces.
fn continuation_to_spaces(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find('\\') {
        out.push_str(rest.get(..i).unwrap_or_default());
        let after = rest.get(i + 1..).unwrap_or_default();
        let ws = after.find(|c: char| !is_python_space(c)).unwrap_or(after.len());
        let run = after.get(..ws).unwrap_or_default();
        // Greedy `\s*`, backtracking to the last position where `$` matches: the end of the
        // string if the run reaches it, else just before the run's last newline.
        let matched = if ws == after.len() { Some(ws) } else { run.rfind('\n') };
        if let Some(m) = matched {
            out.push('\n');
            rest = after.get(m..).unwrap_or_default();
        } else {
            out.push('\\');
            rest = after;
        }
    }
    out.push_str(rest);
    out.replace('\n', " ")
}

/// A cheap structural check for Interpolator candidates: unbalanced brackets or an
/// unterminated string are always syntax errors. Anything it cannot judge exactly (string
/// prefixes, backslashes, comments) is left to `ast.parse`.
fn obviously_invalid(s: &str) -> bool {
    if s.contains('\\') || s.contains('#') {
        return false;
    }
    let chars: Vec<char> = s.chars().collect();
    let mut stack = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        match c {
            '\'' | '"' => {
                if i > 0 && chars[i - 1].is_alphanumeric() {
                    return false; // a string prefix (f"", rb'') or worse: let the parser decide
                }
                let triple = chars.get(i + 1) == Some(&c) && chars.get(i + 2) == Some(&c);
                let width = if triple { 3 } else { 1 };
                let mut j = i + width;
                loop {
                    if j + width > chars.len() {
                        return true; // unterminated string
                    }
                    if chars[j..j + width].iter().all(|&x| x == c) {
                        break;
                    }
                    j += 1;
                }
                i = j + width;
                continue;
            }
            '(' | '[' | '{' => stack.push(c),
            ')' | ']' | '}' => {
                let open = match c {
                    ')' => '(',
                    ']' => '[',
                    _ => '{',
                };
                if stack.pop() != Some(open) {
                    return true;
                }
            }
            _ => {}
        }
        i += 1;
    }
    !stack.is_empty()
}

/// The `default` alias as `chameleon_rs._rewrite.aliases_for` takes it: `True` for the
/// marker, `'target_language'` inside `i18n:target`, else `False`.
fn default_alias(py: Python<'_>, default: DefaultAlias) -> Bound<'_, PyAny> {
    match default {
        DefaultAlias::Marker => pyo3::types::PyBool::new(py, true).to_owned().into_any(),
        DefaultAlias::TargetLanguage => intern!(py, "target_language").clone().into_any(),
        DefaultAlias::Name => pyo3::types::PyBool::new(py, false).to_owned().into_any(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prefixes() {
        assert_eq!(match_prefix(" python: 1"), Some(("python", " 1")));
        assert_eq!(match_prefix("a-b_1:x"), Some(("a-b_1", "x")));
        assert_eq!(match_prefix("{'a': 1}"), None);
        assert_eq!(match_prefix("A:1"), None);
    }

    #[test]
    fn pipes() {
        assert_eq!(split_pipe("a | b | c"), ("a ", " b | c"));
        assert_eq!(split_pipe(r#""a\|b""#), (r#""a\|b""#, ""));
    }

    #[test]
    fn continuations() {
        assert_eq!(continuation_to_spaces("a \\\n b"), "a    b");
        assert_eq!(continuation_to_spaces("a \\ b"), "a \\ b");
        assert_eq!(continuation_to_spaces("a\nb"), "a b");
    }

    #[test]
    fn structural_prefilter() {
        for bad in ["x} b ${y", "a(", "'abc", "(]"] {
            assert!(obviously_invalid(bad), "{bad}");
        }
        for unsure in ["x", "{'}': 1}", "f'{x}'", "'''a'''", "a # }", "'a\\'b'"] {
            assert!(!obviously_invalid(unsure), "{unsure}");
        }
    }
}
