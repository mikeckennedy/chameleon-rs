// The name rules are ported from Chameleon's src/chameleon/compiler.py (`NameTransform`) and
// the attribute rule from tales.py (`transform_attribute`, `lookup_attr`), modified: CPython's
// `ast` is lowered to the native fast-path tree instead of being rewritten and compiled.

//! Lowering CPython's `ast` to the fast-path tree (D4): the census's node types, in its order
//! (`Name`, `Attribute`, `Constant`, `Call`, `not`, `Subscript`, `Compare`, `IfExp`, `BoolOp`,
//! then arithmetic, literals and slices). Anything else returns `None` and the whole
//! expression takes the exact fallback.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::types::{PyBool, PyBytes, PyComplex, PyEllipsis, PyFloat, PyInt, PyList, PyNone, PyString};

use chameleon_core::{DefaultAlias, ExprContext};

use crate::compiler::PyExprCompiler;
use crate::expr::{BinaryOp, CmpOp, Expr, UnaryOp, lookup_attr};

impl<'py> PyExprCompiler<'_, 'py> {
    /// `NameTransform` for a name read in a Python expression (or `ProxyExpr`'s function):
    /// `None` when only the fallback can resolve it.
    pub(crate) fn name_expr(&mut self, name: &str, cx: ExprContext<'_>) -> PyResult<Option<Expr>> {
        let py = self.py;
        if name.starts_with("__") {
            // Left alone by `NameTransform`: a render-function parameter if it is a template
            // builtin (`__loader`), else whatever the fallback's globals hold.
            return Ok(self
                .config
                .template_builtins
                .iter()
                .any(|b| b == name)
                .then(|| Expr::RenderBuiltin(crate::convert::intern(py, name).unbind())));
        }
        Ok(Some(match name {
            "econtext" => Expr::Econtext,
            "rcontext" => Expr::Rcontext,
            "translate" | "decode" | "on_error_handler" => {
                Expr::ScopeKey(crate::convert::intern(py, &format!("__{name}")).unbind())
            }
            // Compile-time aliases (`Alias` in a `Define`) come before builtins.
            "default" if cx.default == DefaultAlias::Marker => Expr::Const(self.marker.clone().unbind()),
            // `Alias(["default"], "target_language")`: an ordinary lookup of that name.
            "default" if cx.default == DefaultAlias::TargetLanguage => {
                Expr::Name(crate::convert::intern(py, "target_language").unbind())
            }
            "attrs" if cx.attrs.is_some() => match self.attrs_dict(cx)? {
                Some(d) => Expr::Const(d.into_any().unbind()),
                None => return Ok(None),
            },
            // Template builtins are parameters of Chameleon's render function, so they shadow
            // Python's builtins of the same name.
            _ if self.config.template_builtins.iter().any(|b| b == name) => {
                Expr::TemplateBuiltin(crate::convert::intern(py, name).unbind())
            }
            _ => {
                let id = crate::convert::intern(py, name);
                match self.builtins.get_item(&id)? {
                    Some(builtin) => Expr::Builtin(id.unbind(), builtin.unbind()),
                    None => Expr::Name(id.unbind()),
                }
            }
        }))
    }

    /// `render_partial(path, name=value, ...)` as a native frame call.
    pub(crate) fn partial(&mut self, node: &Bound<'py, PyAny>, cx: ExprContext<'_>) -> PyResult<Option<Expr>> {
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
            // `translate=` and `encoding=` change the partial's render itself (its translation
            // function; chameleon_partials' own `encoding` collides): the Python call does that.
            if arg.is_none() || matches!(arg.extract::<String>()?.as_str(), "translate" | "encoding") {
                return Ok(None);
            }
            let Some(value) = self.lower(&kw.getattr(intern!(py, "value"))?, cx)? else { return Ok(None) };
            kwargs.push((intern_str(&arg)?, value));
        }
        Ok(Some(Expr::Partial { path: Box::new(path), kwargs, call: Box::new(call) }))
    }

    /// Lowers an `ast` node to the fast-path tree; `None` if any part is not on the whitelist.
    #[allow(clippy::too_many_lines)] // one arm per node type
    pub(crate) fn lower(&mut self, node: &Bound<'py, PyAny>, cx: ExprContext<'_>) -> PyResult<Option<Expr>> {
        // Recursive over the Python AST (`1+1+...`): a stack budget (`chameleon_core::stack`).
        let _deep = chameleon_core::stack::enter().map_err(crate::errors::too_deep)?;
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
        // An operand CPython's compiler may fold with: a literal constant (not an alias such
        // as `attrs` or the default marker, which are constants of ours, not of the code).
        let literal = |e: &Expr| matches!(e, Expr::Const(c) if is_literal(c.bind(py)));
        let kind = kind(node)?;
        Ok(Some(match kind.as_str() {
            "Name" => {
                if !is_load(&field!("ctx"))? {
                    return Ok(None);
                }
                let name: String = field!("id").extract()?;
                match self.name_expr(&name, cx)? {
                    Some(e) => e,
                    None => return Ok(None),
                }
            }
            "Constant" => Expr::Const(self.constant(&field!("value"))?.unbind()),
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
                let op = match self::kind(&field!("op"))?.as_str() {
                    "Not" => return Ok(Some(Expr::Not(operand))),
                    "USub" => UnaryOp::Neg,
                    "UAdd" => UnaryOp::Pos,
                    "Invert" => UnaryOp::Invert,
                    _ => return Ok(None),
                };
                // `-1.5` is one constant if CPython's compiler folds it (see `fold`).
                if literal(&operand)
                    && let Some(folded) = self.fold(node)?
                {
                    return Ok(Some(folded));
                }
                Expr::Unary(op, operand)
            }
            "Subscript" => {
                if !is_load(&field!("ctx"))? {
                    return Ok(None);
                }
                let (value, key) = (sub!(field!("value")), sub!(field!("slice")));
                if literal(&value)
                    && literal(&key)
                    && let Some(folded) = self.fold(node)?
                {
                    return Ok(Some(folded));
                }
                Expr::Subscript(value, key)
            }
            "Slice" => {
                let mut parts = [None, None, None];
                for (slot, name) in
                    parts.iter_mut().zip([intern!(py, "lower"), intern!(py, "upper"), intern!(py, "step")])
                {
                    let part = node.getattr(name)?;
                    if !part.is_none() {
                        *slot = Some(*sub!(part));
                    }
                }
                Expr::Slice(Box::new(parts))
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
            "BoolOp" => {
                let values = self.lower_all(&field!("values"), cx)?;
                let Some(values) = values else { return Ok(None) };
                match self::kind(&field!("op"))?.as_str() {
                    "And" => Expr::And(values),
                    "Or" => Expr::Or(values),
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
                let (left, right) = (sub!(field!("left")), sub!(field!("right")));
                // `'ab' * 2`, `2 ** 20`: one constant when CPython's compiler folds it.
                if literal(&left)
                    && literal(&right)
                    && let Some(folded) = self.fold(node)?
                {
                    return Ok(Some(folded));
                }
                Expr::Binary(op, left, right)
            }
            "Tuple" | "List" => {
                if !is_load(&field!("ctx"))? {
                    return Ok(None);
                }
                let Some(items) = self.lower_all(&field!("elts"), cx)? else { return Ok(None) };
                if kind == "List" {
                    return Ok(Some(Expr::List(items)));
                }
                // A tuple of constants is one constant (CPython's compiler folds it).
                if items.iter().all(literal)
                    && let Some(folded) = self.fold(node)?
                {
                    return Ok(Some(folded));
                }
                Expr::Tuple(items)
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

    fn lower_all(&mut self, list: &Bound<'py, PyAny>, cx: ExprContext<'_>) -> PyResult<Option<Vec<Expr>>> {
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

fn kind(node: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(node.get_type().name()?.to_str()?.to_owned())
}

fn is_load(ctx: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(kind(ctx)? == "Load")
}

fn intern_str(s: &Bound<'_, PyAny>) -> PyResult<Py<PyString>> {
    let s = s.cast::<PyString>()?;
    Ok(crate::convert::intern(s.py(), s.to_str()?).unbind())
}

/// A value the compiler treats as a constant: a literal, or a tuple of them (not an alias
/// such as `attrs` or the default marker, which are constants of ours, not of the code).
fn is_literal(v: &Bound<'_, PyAny>) -> bool {
    is_immutable_constant(v)
        || v.cast_exact::<pyo3::types::PyTuple>().is_ok_and(|t| t.iter().all(|item| is_literal(&item)))
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
