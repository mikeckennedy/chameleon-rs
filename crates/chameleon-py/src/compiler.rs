// The TALES handling is ported from Chameleon's src/chameleon/tales.py (`TalesExpr.__call__`,
// `PythonExpr.translate`, `ExpressionParser.__call__`, `match_prefix`, `split_parts`,
// `re_continuation`), modified: rewritten in Rust, lowering CPython's `ast` to the fast-path
// tree instead of generating Python code.

//! The real [`ExprCompiler`] (D1, D4): CPython's own parser, a native fast path for the
//! census's node types, and an exact fallback for everything else.

use pyo3::exceptions::{PyLookupError, PySyntaxError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::types::{PyBool, PyBytes, PyComplex, PyDict, PyEllipsis, PyFloat, PyInt, PyList, PyNone, PyString};

use chameleon_core::{ExprCompileError, ExprCompiler, ExprContext, ExprId, Span, is_python_space};

use crate::errors::unsupported_error;
use crate::expr::{BinaryOp, CmpOp, Expr, UnaryOp, lookup_attr};

type CompileResult<T> = Result<T, ExprCompileError<PyErr>>;

/// TALES expression types Chameleon's `PageTemplate` knows, not yet supported here.
const OTHER_TYPES: [&str; 6] = ["string", "not", "exists", "import", "structure", "load"];

pub(crate) struct PyExprCompiler<'py> {
    py: Python<'py>,
    pub(crate) exprs: Vec<Expr>,
    /// Whether any expression mentions `repeat` (so native partial frames provide it).
    pub(crate) mentions_repeat: bool,
    ast_parse: Bound<'py, PyAny>,
    eval_mode: Bound<'py, PyDict>,
    builtins: Bound<'py, PyDict>,
    build_fallback: Bound<'py, PyAny>,
    lookup_attr: Bound<'py, PyAny>,
    marker: Bound<'py, PyAny>,
}

impl<'py> PyExprCompiler<'py> {
    pub(crate) fn new(py: Python<'py>, marker: Bound<'py, PyAny>) -> PyResult<Self> {
        let eval_mode = PyDict::new(py);
        // `mode` must be a keyword: ast.parse's second positional parameter is `filename`.
        eval_mode.set_item(intern!(py, "mode"), intern!(py, "eval"))?;
        Ok(Self {
            py,
            exprs: Vec::new(),
            mentions_repeat: false,
            ast_parse: py.import(intern!(py, "ast"))?.getattr(intern!(py, "parse"))?,
            eval_mode,
            builtins: py.import(intern!(py, "builtins"))?.dict(),
            build_fallback: py.import(intern!(py, "chameleon_rs._rewrite"))?.getattr(intern!(py, "build"))?,
            lookup_attr: wrap_pyfunction!(crate::expr::py_lookup_attr, py)?.into_any(),
            marker,
        })
    }

    /// `ExpressionParser.__call__`: an optional `type:` prefix selects the expression type.
    fn typed(&mut self, source: &str, cx: ExprContext, top: bool) -> CompileResult<Expr> {
        if let Some((prefix, rest)) = match_prefix(source) {
            return match prefix {
                "python" => self.tales(rest, cx, top),
                p if OTHER_TYPES.contains(&p) => {
                    Err(ExprCompileError::Other(unsupported_error(self.py, &format!("The '{p}:' expression type"))))
                }
                p => Err(ExprCompileError::Other(PyLookupError::new_err(format!("Unknown expression type: '{p}'.")))),
            };
        }
        self.tales(source, cx, top)
    }

    /// `TalesExpr.__call__`: alternatives separated by unescaped `|`; a part with a type
    /// prefix takes the rest of the expression.
    fn tales(&mut self, expression: &str, cx: ExprContext, top: bool) -> CompileResult<Expr> {
        let mut alternatives = Vec::new();
        let mut remaining = expression;
        while !remaining.is_empty() {
            if match_prefix(remaining).is_some() {
                alternatives.push(self.typed(remaining, cx, false)?);
                break;
            }
            let (part, rest) = split_pipe(remaining);
            let part = part.replace("\\|", "|");
            remaining = rest;
            let only = top && remaining.is_empty() && alternatives.is_empty();
            alternatives.push(self.python(&part, cx, only)?);
        }
        match alternatives.len() {
            0 => Err(ExprCompileError::Invalid { message: "No input:".to_owned() }),
            1 => Ok(alternatives.remove(0)),
            _ => Ok(Expr::Pipe(alternatives)),
        }
    }

    /// `PythonExpr.translate`, then lowering. `only` is true for a whole emitted expression
    /// (where a native `render_partial` frame is possible).
    fn python(&mut self, expression: &str, cx: ExprContext, only: bool) -> CompileResult<Expr> {
        let string = continuation_to_spaces(expression.trim_matches(is_python_space));
        if cx.candidate && obviously_invalid(&string) {
            return Err(ExprCompileError::Invalid { message: "invalid syntax".to_owned() });
        }
        if string.contains("repeat") {
            self.mentions_repeat = true;
        }
        let tree = match self.ast_parse.call((string.as_str(),), Some(&self.eval_mode)) {
            Ok(tree) => tree,
            Err(e) if e.is_instance_of::<PySyntaxError>(self.py) => {
                let message = e.value(self.py).getattr(intern!(self.py, "msg")).and_then(|m| m.extract::<String>());
                return Err(ExprCompileError::Invalid { message: message.unwrap_or_else(|_| "invalid syntax".into()) });
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
            self.lower(&body, cx)
        })()
        .map_err(ExprCompileError::Other)?;
        if let Some(expr) = lowered {
            return Ok(expr);
        }
        let func = self
            .build_fallback
            .call1((tree, cx.default_is_marker, &self.lookup_attr))
            .map_err(ExprCompileError::Other)?;
        Ok(Expr::Fallback(func.unbind()))
    }

    /// `render_partial(path, name=value, ...)` as a native frame call.
    fn partial(&mut self, node: &Bound<'py, PyAny>, cx: ExprContext) -> PyResult<Option<Expr>> {
        let py = self.py;
        if kind(node)? != "Call" {
            return Ok(None);
        }
        let func = node.getattr(intern!(py, "func"))?;
        if kind(&func)? != "Name" || func.getattr(intern!(py, "id"))?.extract::<String>()? != "render_partial" {
            return Ok(None);
        }
        let args = node.getattr(intern!(py, "args"))?;
        let args = args.cast::<PyList>()?;
        let keywords = node.getattr(intern!(py, "keywords"))?;
        let keywords = keywords.cast::<PyList>()?;
        if args.len() != 1 || kind(&args.get_item(0)?)? == "Starred" {
            return Ok(None);
        }
        let Some(call) = self.lower(node, cx)? else { return Ok(None) };
        let Some(path) = self.lower(&args.get_item(0)?, cx)? else { return Ok(None) };
        let mut kwargs = Vec::with_capacity(keywords.len());
        for kw in keywords.iter() {
            let arg = kw.getattr(intern!(py, "arg"))?;
            if arg.is_none() {
                return Ok(None);
            }
            let Some(value) = self.lower(&kw.getattr(intern!(py, "value"))?, cx)? else { return Ok(None) };
            kwargs.push((intern_str(&arg)?, value));
        }
        Ok(Some(Expr::Partial { path: Box::new(path), kwargs, call: Box::new(call) }))
    }

    /// Lowers an `ast` node to the fast-path tree; `None` if any part is not on the whitelist.
    #[allow(clippy::too_many_lines)] // one arm per node type
    fn lower(&mut self, node: &Bound<'py, PyAny>, cx: ExprContext) -> PyResult<Option<Expr>> {
        let py = self.py;
        macro_rules! sub {
            ($node:expr) => {
                match self.lower(&$node, cx)? {
                    Some(e) => Box::new(e),
                    None => return Ok(None),
                }
            };
        }
        macro_rules! field {
            ($name:literal) => {
                node.getattr(intern!(py, $name))?
            };
        }
        let kind = kind(node)?;
        Ok(Some(match kind.as_str() {
            "Name" => {
                if !is_load(&field!("ctx"))? {
                    return Ok(None);
                }
                let id = field!("id");
                let name: String = id.extract()?;
                if name.starts_with("__") {
                    return Ok(None);
                }
                match name.as_str() {
                    "econtext" => Expr::Econtext,
                    "rcontext" => Expr::Rcontext,
                    "translate" | "decode" | "on_error_handler" => {
                        Expr::ScopeKey(PyString::intern(py, &format!("__{name}")).unbind())
                    }
                    "default" if cx.default_is_marker => Expr::Const(self.marker.clone().unbind()),
                    _ => match self.builtins.get_item(&id)? {
                        Some(builtin) => Expr::Builtin(intern_str(&id)?, builtin.unbind()),
                        None => Expr::Name(intern_str(&id)?),
                    },
                }
            }
            "Constant" => Expr::Const(field!("value").unbind()),
            "Attribute" => {
                if !is_load(&field!("ctx"))? {
                    return Ok(None);
                }
                let value = field!("value");
                let attr = intern_str(&field!("attr"))?;
                // An attribute of an original constant of an immutable builtin type resolves
                // to the same object every render (`'{:,}'.format`); a failed lookup stays a
                // runtime lookup so the error is raised at render time.
                if self::kind(&value)? == "Constant" {
                    let constant = value.getattr(intern!(py, "value"))?;
                    if is_immutable_constant(&constant)
                        && let Ok(resolved) = lookup_attr(&constant, attr.bind(py))
                    {
                        return Ok(Some(Expr::Folded(resolved.unbind())));
                    }
                }
                Expr::Attr(sub!(value), attr)
            }
            "Call" => {
                let func = sub!(field!("func"));
                let mut args = Vec::new();
                for arg in field!("args").cast::<PyList>()?.iter() {
                    if self::kind(&arg)? == "Starred" {
                        return Ok(None);
                    }
                    args.push(*sub!(arg));
                }
                let mut kwargs = Vec::new();
                for kw in field!("keywords").cast::<PyList>()?.iter() {
                    let arg = kw.getattr(intern!(py, "arg"))?;
                    if arg.is_none() {
                        return Ok(None);
                    }
                    kwargs.push((intern_str(&arg)?, *sub!(kw.getattr(intern!(py, "value"))?)));
                }
                Expr::Call { func, args, kwargs }
            }
            "UnaryOp" => {
                let operand = sub!(field!("operand"));
                match self::kind(&field!("op"))?.as_str() {
                    "Not" => Expr::Not(operand),
                    "USub" => Expr::Unary(UnaryOp::Neg, operand),
                    "UAdd" => Expr::Unary(UnaryOp::Pos, operand),
                    "Invert" => Expr::Unary(UnaryOp::Invert, operand),
                    _ => return Ok(None),
                }
            }
            "BinOp" => {
                let op = match self::kind(&field!("op"))?.as_str() {
                    "Add" => BinaryOp::Add,
                    "Sub" => BinaryOp::Sub,
                    "Mult" => BinaryOp::Mul,
                    "MatMult" => BinaryOp::MatMul,
                    "Div" => BinaryOp::Div,
                    "FloorDiv" => BinaryOp::FloorDiv,
                    "Mod" => BinaryOp::Mod,
                    "Pow" => BinaryOp::Pow,
                    "LShift" => BinaryOp::LShift,
                    "RShift" => BinaryOp::RShift,
                    "BitOr" => BinaryOp::BitOr,
                    "BitXor" => BinaryOp::BitXor,
                    "BitAnd" => BinaryOp::BitAnd,
                    _ => return Ok(None),
                };
                Expr::Binary(op, sub!(field!("left")), sub!(field!("right")))
            }
            "BoolOp" => {
                let values = self.lower_all(&field!("values"), cx)?;
                let Some(values) = values else { return Ok(None) };
                match self::kind(&field!("op"))?.as_str() {
                    "And" => Expr::And(values),
                    "Or" => Expr::Or(values),
                    _ => return Ok(None),
                }
            }
            "Compare" => {
                let left = sub!(field!("left"));
                let ops = field!("ops");
                let comparators = field!("comparators");
                let mut pairs = Vec::new();
                for (op, right) in ops.cast::<PyList>()?.iter().zip(comparators.cast::<PyList>()?.iter()) {
                    let op = match self::kind(&op)?.as_str() {
                        "Eq" => CmpOp::Rich(CompareOp::Eq),
                        "NotEq" => CmpOp::Rich(CompareOp::Ne),
                        "Lt" => CmpOp::Rich(CompareOp::Lt),
                        "LtE" => CmpOp::Rich(CompareOp::Le),
                        "Gt" => CmpOp::Rich(CompareOp::Gt),
                        "GtE" => CmpOp::Rich(CompareOp::Ge),
                        "Is" => CmpOp::Is,
                        "IsNot" => CmpOp::IsNot,
                        "In" => CmpOp::In,
                        "NotIn" => CmpOp::NotIn,
                        _ => return Ok(None),
                    };
                    pairs.push((op, *sub!(right)));
                }
                Expr::Compare(left, pairs)
            }
            "IfExp" => Expr::IfExp(sub!(field!("test")), sub!(field!("body")), sub!(field!("orelse"))),
            "Subscript" => {
                let slice = field!("slice");
                if !is_load(&field!("ctx"))? || self::kind(&slice)? == "Slice" {
                    return Ok(None);
                }
                Expr::Subscript(sub!(field!("value")), sub!(slice))
            }
            "Tuple" | "List" => {
                if !is_load(&field!("ctx"))? {
                    return Ok(None);
                }
                let Some(items) = self.lower_all(&field!("elts"), cx)? else { return Ok(None) };
                if kind == "Tuple" { Expr::Tuple(items) } else { Expr::List(items) }
            }
            "Dict" => {
                let keys = field!("keys");
                let values = field!("values");
                let mut items = Vec::new();
                for (k, v) in keys.cast::<PyList>()?.iter().zip(values.cast::<PyList>()?.iter()) {
                    if k.is_none() {
                        return Ok(None); // `**mapping`
                    }
                    items.push((*sub!(k), *sub!(v)));
                }
                Expr::Dict(items)
            }
            _ => return Ok(None),
        }))
    }

    fn lower_all(&mut self, list: &Bound<'py, PyAny>, cx: ExprContext) -> PyResult<Option<Vec<Expr>>> {
        let mut out = Vec::new();
        for item in list.cast::<PyList>()?.iter() {
            if kind(&item)? == "Starred" {
                return Ok(None);
            }
            match self.lower(&item, cx)? {
                Some(e) => out.push(e),
                None => return Ok(None),
            }
        }
        Ok(Some(out))
    }
}

impl ExprCompiler for PyExprCompiler<'_> {
    type Error = PyErr;

    fn compile(&mut self, source: &str, _span: Span, cx: ExprContext) -> Result<ExprId, ExprCompileError<PyErr>> {
        let expr = self.typed(source, cx, true)?;
        let id = u32::try_from(self.exprs.len())
            .map_err(|_| ExprCompileError::Other(unsupported_error(self.py, "More than 2**32 expressions")))?;
        self.exprs.push(expr);
        Ok(ExprId(id))
    }
}

fn kind(node: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(node.get_type().name()?.to_str()?.to_owned())
}

fn is_load(ctx: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(kind(ctx)? == "Load")
}

fn intern_str(s: &Bound<'_, PyAny>) -> PyResult<Py<PyString>> {
    let s = s.cast::<PyString>()?;
    Ok(PyString::intern(s.py(), s.to_str()?).unbind())
}

/// Constants `ast.parse` produces, all immutable builtins.
fn is_immutable_constant(v: &Bound<'_, PyAny>) -> bool {
    v.is_exact_instance_of::<PyString>()
        || v.is_exact_instance_of::<PyBytes>()
        || v.is_exact_instance_of::<PyInt>()
        || v.is_exact_instance_of::<PyFloat>()
        || v.is_exact_instance_of::<PyComplex>()
        || v.is_exact_instance_of::<PyBool>()
        || v.is_exact_instance_of::<PyNone>()
        || v.is_exact_instance_of::<PyEllipsis>()
}

/// `match_prefix`: `^\s*([a-z][a-z0-9\-_]*):`. Returns the prefix and the text after `:`.
fn match_prefix(s: &str) -> Option<(&str, &str)> {
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
