// `lookup_attr` and the name rules are ported from Chameleon's src/chameleon/utils.py
// (`lookup_attr`) and compiler.py (`NameTransform`); the TALES expression types from
// tales.py (`TalesExpr`, `ExistsExpr`, `NotExpr`, `StructureExpr`, `ImportExpr`, `StringExpr`,
// `ProxyExpr`); modified: evaluated natively over a Rust expression tree instead of generated
// Python code.

//! The fast-path expression tree (D4) and its evaluation.

use std::cell::{Cell, OnceCell, RefCell};
use std::collections::BTreeSet;

use pyo3::exceptions::{
    PyAttributeError, PyKeyError, PyLookupError, PyNameError, PyTypeError, PyUnboundLocalError, PyValueError,
};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyDict, PyInt, PyList, PySet, PySlice, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use chameleon_core::{Call, Escape, FreeVars, Step};

use crate::custom::Custom;
use crate::program::Program;
use crate::scope::Scope;

#[derive(Clone, Copy, Debug)]
pub(crate) enum UnaryOp {
    Neg,
    Pos,
    Invert,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum BinaryOp {
    Add,
    Sub,
    Mul,
    MatMul,
    Div,
    FloorDiv,
    Mod,
    Pow,
    LShift,
    RShift,
    BitOr,
    BitXor,
    BitAnd,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum CmpOp {
    Rich(CompareOp),
    Is,
    IsNot,
    In,
    NotIn,
}

/// An expression: a Python expression lowered from CPython's `ast` (the census's node
/// types), a TALES expression type, or the exact fallback (a Python function built from the
/// rewritten `ast`).
#[derive(Debug)]
pub(crate) enum Expr {
    /// An ordinary name: local scope, then root, else `NameError`.
    Name(Py<PyString>),
    /// A builtin name: `econtext.get(name, builtin)`, so a render keyword overrides it.
    Builtin(Py<PyString>, Py<PyAny>),
    /// A template builtin (`template`, `macros`, `nothing`, `extra_builtins`): like
    /// [`Expr::Builtin`], with the value the render supplies (the template is never stored
    /// in the program, D4).
    TemplateBuiltin(Py<PyString>),
    /// A template builtin whose name starts with `__` (`__loader`): `NameTransform` leaves
    /// such names alone, so it is the render's value itself, never looked up in the scope.
    RenderBuiltin(Py<PyString>),
    Const(Py<PyAny>),
    /// An attribute of an original constant, resolved at compile time (one level, D4).
    Folded(Py<PyAny>),
    /// `econtext`: the current scope itself.
    Econtext,
    /// `rcontext`: the per-render dict.
    Rcontext,
    /// `translate`, `decode`, `on_error_handler`: Chameleon binds these from the scope
    /// (`econtext['__translate']`, ...) at the start of every render function.
    ScopeKey(Py<PyString>),
    Attr(Box<Expr>, Py<PyString>),
    Call {
        func: Box<Expr>,
        args: Vec<Expr>,
        kwargs: Vec<(Py<PyString>, Expr)>,
    },
    /// Python's `not x`, and the `not:` expression type (`target = not target`).
    Not(Box<Expr>),
    Unary(UnaryOp, Box<Expr>),
    Binary(BinaryOp, Box<Expr>, Box<Expr>),
    And(Vec<Expr>),
    Or(Vec<Expr>),
    Compare(Box<Expr>, Vec<(CmpOp, Expr)>),
    IfExp(Box<Expr>, Box<Expr>, Box<Expr>),
    Subscript(Box<Expr>, Box<Expr>),
    /// `lower:upper:step` in a subscript: `slice(lower, upper, step)`, as `BINARY_SLICE` /
    /// `BUILD_SLICE` build it (missing parts are `None`).
    Slice(Box<[Option<Expr>; 3]>),
    Tuple(Vec<Expr>),
    List(Vec<Expr>),
    Dict(Vec<(Expr, Expr)>),
    /// TALES `a | b`: the first alternative that does not raise one of Chameleon's
    /// `TalesExpr.exceptions`.
    Pipe(Vec<Expr>),
    /// `exists:`: `1`, or `0` if evaluating raised one of `ExistsExpr.exceptions`.
    Exists(Box<Expr>),
    /// `structure:`: the value wrapped in `Markup`.
    Structure(Box<Expr>),
    /// `import:`: `resolve_dotted(name)`, at every evaluation (it caches imports).
    Import(Py<PyString>),
    /// `string:` and each path of `load:` (Chameleon's `Interpolator`, braces optional).
    Interp(Box<StrInterp>),
    /// A top-level `render_partial(path, **kw)` in an emitted position: a native frame when
    /// no `render_partial` is bound in the scope, else the ordinary call (`call`).
    Partial {
        path: Box<Expr>,
        kwargs: Vec<(Py<PyString>, Expr)>,
        call: Box<Expr>,
    },
    /// `f(econtext, rcontext, default_marker, template_builtins)` built by
    /// `chameleon_rs._rewrite`.
    Fallback(Box<Fallback>),
    /// A user-registered expression type, through the AST bridge (`chameleon_rs._bridge`).
    Custom(Box<Custom>),
    /// Non-strict mode: raises `ExpressionError(*args)` (a syntax error deferred to render
    /// time, raised fresh each time, as Chameleon unpickles a new copy).
    Raise(Py<PyTuple>),
    /// Records a nested error token, then evaluates: the sub-expressions an expression type
    /// parses with `engine.parse` (`not:`, `structure:`, a type prefix after a pipe, each
    /// `${...}` of `string:`), which Chameleon gives a `TokenRef` of their own. Holds the
    /// enclosing site's `ExprId` plus one and the token (see [`RenderCtx::nested`]).
    Token(u32, u32, Box<Expr>),
    /// Code run in the render's namespace (see [`NsCode`]).
    Ns(Box<NsCode>),
}

/// A compiled expression site: the expression and its error token (index into the program's
/// token table plus one; `0` leaves the previous token in place). One table, so `eval` does a
/// single bounds check.
#[derive(Debug)]
pub(crate) struct Site {
    pub(crate) expr: Expr,
    pub(crate) token: u32,
    /// The expression contains [`Expr::Token`] nodes: evaluating it clears
    /// [`RenderCtx::nested`] first.
    pub(crate) nested: bool,
}

/// A `<?python ?>` block (`exec`), or a fallback expression that reads a name a code block
/// defined (`eval`): Chameleon inlines both into the render function, whose locals they
/// share. They run with the namespace of the frame running them (one per frame: each macro
/// or slot body is a function of its own in Chameleon) as globals.
#[derive(Debug)]
pub(crate) struct NsCode {
    pub(crate) code: Py<PyAny>,
    /// The `attrs` alias where the code appears.
    pub(crate) attrs: Option<Py<PyDict>>,
    /// Statements (`exec`, the value is `None`) rather than an expression (`eval`).
    pub(crate) exec: bool,
    /// The names the render function binds (see `chameleon_rs._rewrite.compile_code`): a
    /// `NameError` for one of them is an `UnboundLocalError` in Chameleon.
    pub(crate) bound: Py<PySet>,
}

/// The exact fallback: a Python function, and the names its rewritten code reads and writes.
#[derive(Debug)]
pub(crate) struct Fallback {
    pub(crate) func: Py<PyAny>,
    pub(crate) free: FreeVars,
}

/// How an expression type converts a sub-expression to text: the engine settings of the site
/// (`chameleon_core::Engine`), with the default as a Python string.
#[derive(Debug)]
pub(crate) struct TextConv {
    pub(crate) escape: Option<Escape>,
    pub(crate) marker: bool,
    pub(crate) default: Option<Py<PyString>>,
    pub(crate) literal_false: bool,
}

/// An interpolated string: `string:` and the paths of `load:`.
#[derive(Debug)]
pub(crate) struct StrInterp {
    pub(crate) parts: Vec<StrPart>,
    pub(crate) conv: TextConv,
}

#[derive(Debug)]
pub(crate) enum StrPart {
    Text(Py<PyString>),
    Expr(Expr),
}

impl Expr {
    pub(crate) fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        match self {
            Self::Name(n) | Self::ScopeKey(n) | Self::TemplateBuiltin(n) | Self::RenderBuiltin(n) | Self::Import(n) => {
                visit.call(n)
            }
            Self::Builtin(n, v) => {
                visit.call(n)?;
                visit.call(v)
            }
            Self::Const(v) | Self::Folded(v) => visit.call(v),
            Self::Raise(v) => visit.call(v),
            Self::Ns(n) => {
                visit.call(&n.code)?;
                visit.call(&n.bound)?;
                n.attrs.as_ref().map_or(Ok(()), |a| visit.call(a))
            }
            Self::Fallback(f) => visit.call(&f.func),
            Self::Custom(c) => c.traverse(visit),
            Self::Econtext | Self::Rcontext => Ok(()),
            Self::Attr(e, n) => {
                e.traverse(visit)?;
                visit.call(n)
            }
            Self::Call { func, args, kwargs } => {
                func.traverse(visit)?;
                args.iter().try_for_each(|a| a.traverse(visit))?;
                kwargs.iter().try_for_each(|(k, v)| {
                    visit.call(k)?;
                    v.traverse(visit)
                })
            }
            Self::Not(e) | Self::Unary(_, e) | Self::Exists(e) | Self::Structure(e) | Self::Token(_, _, e) => {
                e.traverse(visit)
            }
            Self::Binary(_, a, b) | Self::Subscript(a, b) => {
                a.traverse(visit)?;
                b.traverse(visit)
            }
            Self::Slice(parts) => parts.iter().flatten().try_for_each(|e| e.traverse(visit)),
            Self::And(v) | Self::Or(v) | Self::Tuple(v) | Self::List(v) | Self::Pipe(v) => {
                v.iter().try_for_each(|e| e.traverse(visit))
            }
            Self::Compare(left, ops) => {
                left.traverse(visit)?;
                ops.iter().try_for_each(|(_, e)| e.traverse(visit))
            }
            Self::IfExp(a, b, c) => {
                a.traverse(visit)?;
                b.traverse(visit)?;
                c.traverse(visit)
            }
            Self::Dict(items) => items.iter().try_for_each(|(k, v)| {
                k.traverse(visit)?;
                v.traverse(visit)
            }),
            Self::Interp(s) => s.traverse(visit),
            Self::Partial { path, kwargs, call } => {
                path.traverse(visit)?;
                kwargs.iter().try_for_each(|(k, v)| {
                    visit.call(k)?;
                    v.traverse(visit)
                })?;
                call.traverse(visit)
            }
        }
    }

    /// Whether any part runs as Python code (the fallback or a custom type).
    pub(crate) fn uses_fallback(&self) -> bool {
        match self {
            Self::Fallback(_) | Self::Custom(_) | Self::Ns(_) => true,
            Self::Token(_, _, e) => e.uses_fallback(),
            Self::Name(_)
            | Self::Builtin(..)
            | Self::TemplateBuiltin(_)
            | Self::RenderBuiltin(_)
            | Self::Const(_)
            | Self::Folded(_)
            | Self::Econtext
            | Self::Rcontext
            | Self::ScopeKey(_)
            | Self::Import(_)
            | Self::Raise(_) => false,
            Self::Attr(e, _) | Self::Not(e) | Self::Unary(_, e) | Self::Exists(e) | Self::Structure(e) => {
                e.uses_fallback()
            }
            Self::Call { func, args, kwargs } => {
                func.uses_fallback()
                    || args.iter().any(Self::uses_fallback)
                    || kwargs.iter().any(|(_, v)| v.uses_fallback())
            }
            Self::Binary(_, a, b) | Self::Subscript(a, b) => a.uses_fallback() || b.uses_fallback(),
            Self::Slice(parts) => parts.iter().flatten().any(Self::uses_fallback),
            Self::And(v) | Self::Or(v) | Self::Tuple(v) | Self::List(v) | Self::Pipe(v) => {
                v.iter().any(Self::uses_fallback)
            }
            Self::Compare(left, ops) => left.uses_fallback() || ops.iter().any(|(_, e)| e.uses_fallback()),
            Self::IfExp(a, b, c) => a.uses_fallback() || b.uses_fallback() || c.uses_fallback(),
            Self::Dict(items) => items.iter().any(|(k, v)| k.uses_fallback() || v.uses_fallback()),
            Self::Interp(s) => s.parts.iter().any(|p| matches!(p, StrPart::Expr(e) if e.uses_fallback())),
            Self::Partial { path, kwargs, call } => {
                path.uses_fallback() || kwargs.iter().any(|(_, v)| v.uses_fallback()) || call.uses_fallback()
            }
        }
    }

    /// The scope names this expression reads and writes (D9).
    pub(crate) fn free_vars(&self) -> FreeVars {
        let mut free = Free::default();
        self.collect(&mut free);
        free.finish()
    }

    fn collect(&self, f: &mut Free) {
        let py_name = |n: &Py<PyString>| Python::attach(|py| n.bind(py).to_string());
        match self {
            Self::Name(n) | Self::Builtin(n, _) | Self::TemplateBuiltin(n) | Self::ScopeKey(n) => {
                f.reads.insert(py_name(n));
            }
            Self::RenderBuiltin(_) | Self::Const(_) | Self::Folded(_) | Self::Import(_) | Self::Raise(_) => {}
            Self::Econtext | Self::Rcontext | Self::Custom(_) | Self::Ns(_) => f.dynamic = true,
            Self::Fallback(fb) => match &fb.free {
                FreeVars::Dynamic => f.dynamic = true,
                FreeVars::Known { reads, writes } => {
                    f.reads.extend(reads.iter().map(ToString::to_string));
                    f.writes.extend(writes.iter().map(ToString::to_string));
                }
            },
            Self::Attr(e, _)
            | Self::Not(e)
            | Self::Unary(_, e)
            | Self::Exists(e)
            | Self::Structure(e)
            | Self::Token(_, _, e) => e.collect(f),
            Self::Call { func, args, kwargs } => {
                func.collect(f);
                for a in args {
                    a.collect(f);
                }
                for (_, v) in kwargs {
                    v.collect(f);
                }
            }
            Self::Binary(_, a, b) | Self::Subscript(a, b) => {
                a.collect(f);
                b.collect(f);
            }
            Self::Slice(parts) => parts.iter().flatten().for_each(|e| e.collect(f)),
            Self::And(v) | Self::Or(v) | Self::Tuple(v) | Self::List(v) | Self::Pipe(v) => {
                for e in v {
                    e.collect(f);
                }
            }
            Self::Compare(left, ops) => {
                left.collect(f);
                for (_, e) in ops {
                    e.collect(f);
                }
            }
            Self::IfExp(a, b, c) => {
                a.collect(f);
                b.collect(f);
                c.collect(f);
            }
            Self::Dict(items) => items.iter().for_each(|(k, v)| {
                k.collect(f);
                v.collect(f);
            }),
            Self::Interp(s) => {
                let mut any = false;
                for part in &s.parts {
                    if let StrPart::Expr(e) = part {
                        e.collect(f);
                        any = true;
                    }
                }
                if any {
                    // Converting a sub-expression's value reads the template's helpers.
                    f.reads.extend(["__decode", "__translate", "target_language"].map(String::from));
                }
            }
            Self::Partial { path, kwargs, call } => {
                path.collect(f);
                for (_, v) in kwargs {
                    v.collect(f);
                }
                call.collect(f);
            }
        }
    }
}

impl StrInterp {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(d) = &self.conv.default {
            visit.call(d)?;
        }
        self.parts.iter().try_for_each(|p| match p {
            StrPart::Text(t) => visit.call(t),
            StrPart::Expr(e) => e.traverse(visit),
        })
    }
}

#[derive(Default)]
struct Free {
    reads: BTreeSet<String>,
    writes: BTreeSet<String>,
    dynamic: bool,
}

impl Free {
    fn finish(self) -> FreeVars {
        if self.dynamic {
            return FreeVars::Dynamic;
        }
        FreeVars::Known {
            reads: self.reads.into_iter().map(String::into_boxed_str).collect(),
            writes: self.writes.into_iter().map(String::into_boxed_str).collect(),
        }
    }
}

/// Render-wide state shared by every frame.
pub(crate) struct RenderCtx<'py> {
    pub(crate) marker: Bound<'py, PyAny>,
    /// Prototype partial registry: name -> native `Program`.
    pub(crate) partials: Option<Bound<'py, PyDict>>,
    /// The template's default `__translate`, `__decode`, ... for scopes that lack them
    /// (native partial frames).
    pub(crate) defaults: Option<Bound<'py, PyDict>>,
    /// The template builtins' values (`template`, `macros`, `nothing`, `__loader`, ...).
    pub(crate) builtins: Bound<'py, PyDict>,
    /// `rcontext`, created on first use.
    pub(crate) rcontext: OnceCell<Bound<'py, PyDict>>,
    /// The last nested error token set (see [`Expr::Token`]): the site it belongs to (its
    /// `ExprId` plus one) and the token. Read only on the error path, for the site the VM
    /// reports (`chameleon_core::ErrorSite`).
    pub(crate) nested: Cell<(u32, u32)>,
    /// Each frame's namespace for code blocks, by frame serial, created on first use (see
    /// [`NsCode`]).
    pub(crate) namespaces: RefCell<Vec<(u64, Bound<'py, PyDict>)>>,
    /// For a render called through `include` by another engine: the stream it writes to
    /// (that engine's slot fills append to it directly, through their closure).
    pub(crate) stream: OnceCell<Bound<'py, PyAny>>,
}

/// What expressions see of the active frame: its template's builtins (a macro's are those
/// of the template it belongs to), its serial, and its i18n settings (Chameleon's
/// `__i18n_domain`, `__i18n_context` and `target_language` locals).
pub(crate) struct FrameCx<'py> {
    /// A macro of another template: that template's builtins (`None`: the render's).
    pub(crate) builtins: Option<Bound<'py, PyDict>>,
    pub(crate) serial: u64,
    pub(crate) domain: Option<Bound<'py, PyString>>,
    pub(crate) context: Option<Bound<'py, PyString>>,
    pub(crate) target_language: Bound<'py, PyAny>,
}

impl<'py> FrameCx<'py> {
    /// A frame with no i18n domain or context.
    pub(crate) fn new(builtins: Option<Bound<'py, PyDict>>, serial: u64, target_language: Bound<'py, PyAny>) -> Self {
        Self { builtins, serial, domain: None, context: None, target_language }
    }
}

impl<'py> RenderCtx<'py> {
    pub(crate) fn new(
        marker: Bound<'py, PyAny>,
        partials: Option<Bound<'py, PyDict>>,
        defaults: Option<Bound<'py, PyDict>>,
        builtins: Bound<'py, PyDict>,
    ) -> Self {
        Self {
            marker,
            partials,
            defaults,
            builtins,
            rcontext: OnceCell::new(),
            nested: Cell::new((0, 0)),
            namespaces: RefCell::new(Vec::new()),
            stream: OnceCell::new(),
        }
    }

    pub(crate) fn rcontext(&self, py: Python<'py>) -> &Bound<'py, PyDict> {
        self.rcontext.get_or_init(|| PyDict::new(py))
    }
}

/// Evaluation context: the active frame's scope and state, plus render-wide state.
pub(crate) struct Cx<'a, 'py> {
    pub(crate) scope: &'a Bound<'py, Scope>,
    pub(crate) render: &'a RenderCtx<'py>,
    pub(crate) frame: &'a FrameCx<'py>,
}

impl<'py> Cx<'_, 'py> {
    /// A scope entry, falling back to the template defaults (for `__translate` and friends).
    pub(crate) fn scope_or_default(&self, key: &Bound<'py, PyString>) -> PyResult<Option<Bound<'py, PyAny>>> {
        if let Some(v) = Scope::lookup(self.scope, key.as_any())? {
            return Ok(Some(v));
        }
        match &self.render.defaults {
            Some(defaults) => defaults.get_item(key),
            None => Ok(None),
        }
    }

    /// The active frame's template builtins.
    pub(crate) fn builtins(&self) -> &Bound<'py, PyDict> {
        self.frame.builtins.as_ref().unwrap_or(&self.render.builtins)
    }

    fn builtin(&self, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
        self.builtins().get_item(name)?.ok_or_else(|| PyNameError::new_err(name.clone().unbind()))
    }

    /// `getname('target_language')` (what Chameleon's rewritten expression code reads, as
    /// opposed to the render function's `target_language` local).
    pub(crate) fn scope_target_language(&self) -> PyResult<Bound<'py, PyAny>> {
        let py = self.scope.py();
        let key = intern!(py, "target_language");
        self.scope_or_default(key)?.ok_or_else(|| PyNameError::new_err((key.clone().unbind(),)))
    }
}

/// Evaluates an expression whose value is emitted: may return a native frame call.
pub(crate) fn eval_emitted(e: &Expr, cx: &Cx<'_, '_>) -> PyResult<Step<Py<PyAny>>> {
    if let Expr::Partial { path, kwargs, call } = e {
        let py = cx.scope.py();
        if Scope::lookup(cx.scope, intern!(py, "render_partial").as_any())?.is_some() {
            return Ok(Step::Ready(eval(call, cx)?.unbind()));
        }
        return partial_call(path, kwargs, cx).map(Step::Call);
    }
    Ok(Step::Ready(eval(e, cx)?.unbind()))
}

fn partial_call(path: &Expr, kwargs: &[(Py<PyString>, Expr)], cx: &Cx<'_, '_>) -> PyResult<Call<Py<PyAny>>> {
    let py = cx.scope.py();
    let path = eval(path, cx)?;
    let Some(registry) = &cx.render.partials else {
        // Without a registry the name is simply unbound, as in Chameleon.
        return Err(PyNameError::new_err("render_partial"));
    };
    let target =
        registry.get_item(&path)?.ok_or_else(|| PyValueError::new_err(format!("Partial not found: {path}")))?;
    let program = target.cast_into::<Program>().map_err(PyErr::from)?;
    let scope = Scope::empty(py)?;
    for (key, value) in kwargs {
        scope.as_super().set_item(key.bind(py), eval(value, cx)?)?;
    }
    let p = program.get();
    if p.needs_repeat {
        scope.as_super().set_item(intern!(py, "repeat"), crate::program::new_repeat_dict(py)?)?;
    }
    Ok(Call { program: p.core.clone(), code: program.into_any().unbind(), scope: scope.into_any().unbind() })
}

/// The exceptions a TALES pipe falls through on (`TalesExpr.exceptions`).
fn pipe_catches(py: Python<'_>, e: &PyErr) -> bool {
    e.is_instance_of::<PyAttributeError>(py)
        || e.is_instance_of::<PyNameError>(py)
        || e.is_instance_of::<PyLookupError>(py)
        || e.is_instance_of::<PyTypeError>(py)
        || e.is_instance_of::<PyValueError>(py)
}

/// The exceptions `exists:` turns into `0` (`ExistsExpr.exceptions`; not `ValueError`).
fn exists_catches(py: Python<'_>, e: &PyErr) -> bool {
    e.is_instance_of::<PyAttributeError>(py)
        || e.is_instance_of::<PyLookupError>(py)
        || e.is_instance_of::<PyTypeError>(py)
        || e.is_instance_of::<PyNameError>(py)
}

static MARKUP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static RESOLVE_DOTTED: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static EXPRESSION_ERROR: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn utils_attr<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyAny>>,
    module: &str,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    Ok(cell.get_or_try_init(py, || Ok::<_, PyErr>(py.import(module)?.getattr(name)?.unbind()))?.bind(py).clone())
}

/// Evaluates an expression. The kinds the census ranks first (names, attributes, constants,
/// calls with up to two positional arguments, `not`, subscripts) are handled here; the rest
/// go through [`eval_more`], so this function's stack frame and prologue, which every
/// recursive call pays, stay small (912 bytes when every kind was inline).
pub(crate) fn eval<'py>(e: &Expr, cx: &Cx<'_, 'py>) -> PyResult<Bound<'py, PyAny>> {
    let py = cx.scope.py();
    match e {
        Expr::Name(name) => {
            let name = name.bind(py);
            Scope::lookup(cx.scope, name.as_any())?.ok_or_else(|| PyNameError::new_err(name.clone().unbind()))
        }
        Expr::Builtin(name, builtin) => {
            Ok(Scope::lookup(cx.scope, name.bind(py).as_any())?.unwrap_or_else(|| builtin.bind(py).clone()))
        }
        Expr::Const(v) | Expr::Folded(v) => Ok(v.bind(py).clone()),
        Expr::Attr(obj, name) => lookup_attr(&eval(obj, cx)?, name.bind(py)),
        // Fixed arities go through vectorcall without building a tuple first.
        Expr::Call { func, args, kwargs } if kwargs.is_empty() && args.len() <= 2 => {
            let func = eval(func, cx)?;
            match args.as_slice() {
                [] => func.call0(),
                [a] => func.call1((eval(a, cx)?,)),
                [a, b, ..] => {
                    let a = eval(a, cx)?;
                    func.call1((a, eval(b, cx)?))
                }
            }
        }
        Expr::Not(v) => Ok(PyBool::new(py, !eval(v, cx)?.is_truthy()?).to_owned().into_any()),
        Expr::Subscript(obj, key) => {
            let obj = eval(obj, cx)?;
            obj.get_item(eval(key, cx)?)
        }
        // `[]` is common (`' '.join(classes or [])`): no intermediate vector.
        Expr::List(items) if items.is_empty() => Ok(PyList::empty(py).into_any()),
        Expr::Econtext => Ok(cx.scope.clone().into_any()),
        Expr::Partial { call, .. } => eval(call, cx),
        Expr::Fallback(f) => {
            let rcontext = cx.render.rcontext(py);
            f.func.bind(py).call1((cx.scope, rcontext, &cx.render.marker, cx.builtins()))
        }
        _ => eval_more(e, cx),
    }
}

/// The other common kinds (comparisons, boolean and conditional expressions, arithmetic,
/// displays, pipes, general calls, names that are not plain scope lookups).
#[inline(never)]
#[allow(clippy::too_many_lines)] // one arm per node type
fn eval_more<'py>(e: &Expr, cx: &Cx<'_, 'py>) -> PyResult<Bound<'py, PyAny>> {
    let py = cx.scope.py();
    match e {
        Expr::TemplateBuiltin(name) => {
            let name = name.bind(py);
            match Scope::lookup(cx.scope, name.as_any())? {
                Some(v) => Ok(v),
                None => cx.builtin(name),
            }
        }
        Expr::RenderBuiltin(name) => cx.builtin(name.bind(py)),
        Expr::Rcontext => Ok(cx.render.rcontext(py).clone().into_any()),
        Expr::ScopeKey(key) => {
            let key = key.bind(py);
            cx.scope_or_default(key)?.ok_or_else(|| PyKeyError::new_err((key.clone().unbind(),)))
        }
        Expr::Call { func, args, kwargs } => {
            let func = eval(func, cx)?;
            let args = PyTuple::new(py, eval_all(args, cx)?)?;
            if kwargs.is_empty() {
                return func.call1(args);
            }
            let kw = PyDict::new(py);
            for (k, v) in kwargs {
                kw.set_item(k.bind(py), eval(v, cx)?)?;
            }
            func.call(args, Some(&kw))
        }
        Expr::Unary(op, v) => {
            let v = eval(v, cx)?;
            match op {
                UnaryOp::Neg => v.neg(),
                UnaryOp::Pos => v.pos(),
                UnaryOp::Invert => v.bitnot(),
            }
        }
        Expr::Binary(op, a, b) => {
            let a = eval(a, cx)?;
            let b = eval(b, cx)?;
            match op {
                BinaryOp::Add => a.add(b),
                BinaryOp::Sub => a.sub(b),
                BinaryOp::Mul => a.mul(b),
                BinaryOp::MatMul => a.matmul(b),
                BinaryOp::Div => a.div(b),
                BinaryOp::FloorDiv => a.floor_div(b),
                BinaryOp::Mod => a.rem(b),
                BinaryOp::Pow => a.pow(b, py.None()),
                BinaryOp::LShift => a.lshift(b),
                BinaryOp::RShift => a.rshift(b),
                BinaryOp::BitOr => a.bitor(b),
                BinaryOp::BitXor => a.bitxor(b),
                BinaryOp::BitAnd => a.bitand(b),
            }
        }
        Expr::And(values) => {
            let mut last = None;
            for v in values {
                let value = eval(v, cx)?;
                if !value.is_truthy()? {
                    return Ok(value);
                }
                last = Some(value);
            }
            last.ok_or_else(|| PyValueError::new_err("empty BoolOp"))
        }
        Expr::Or(values) => {
            let mut last = None;
            for v in values {
                let value = eval(v, cx)?;
                if value.is_truthy()? {
                    return Ok(value);
                }
                last = Some(value);
            }
            last.ok_or_else(|| PyValueError::new_err("empty BoolOp"))
        }
        Expr::Compare(left, ops) => {
            let mut left = eval(left, cx)?;
            let mut result = None;
            for (i, (op, right)) in ops.iter().enumerate() {
                let right = eval(right, cx)?;
                let r = compare(py, &left, *op, &right)?;
                if i + 1 < ops.len() && !r.is_truthy()? {
                    return Ok(r);
                }
                result = Some(r);
                left = right;
            }
            result.ok_or_else(|| PyValueError::new_err("empty Compare"))
        }
        Expr::IfExp(test, body, orelse) => {
            if eval(test, cx)?.is_truthy()? {
                eval(body, cx)
            } else {
                eval(orelse, cx)
            }
        }
        Expr::Tuple(items) => Ok(PyTuple::new(py, eval_all(items, cx)?)?.into_any()),
        Expr::Dict(items) => {
            let d = PyDict::new(py);
            for (k, v) in items {
                let k = eval(k, cx)?;
                d.set_item(k, eval(v, cx)?)?;
            }
            Ok(d.into_any())
        }
        Expr::Pipe(alternatives) => {
            let (last, rest) = alternatives.split_last().ok_or_else(|| PyValueError::new_err("empty pipe"))?;
            for alt in rest {
                match eval(alt, cx) {
                    Ok(v) => return Ok(v),
                    Err(err) if pipe_catches(py, &err) => {}
                    Err(err) => return Err(err),
                }
            }
            eval(last, cx)
        }
        Expr::List(items) => Ok(PyList::new(py, eval_all(items, cx)?)?.into_any()),
        Expr::Token(site, token, inner) => {
            cx.render.nested.set((*site, *token));
            eval(inner, cx)
        }
        Expr::Slice(_)
        | Expr::Exists(_)
        | Expr::Structure(_)
        | Expr::Import(_)
        | Expr::Interp(_)
        | Expr::Custom(_)
        | Expr::Raise(_)
        | Expr::Ns(_) => eval_rare(e, cx),
        _ => eval(e, cx),
    }
}

/// The TALES types and rare node kinds, out of line, so `eval`'s stack frame and prologue
/// (paid by every recursive call on the hot path) stay what the census types need.
#[cold]
#[inline(never)]
fn eval_rare<'py>(e: &Expr, cx: &Cx<'_, 'py>) -> PyResult<Bound<'py, PyAny>> {
    let py = cx.scope.py();
    match e {
        Expr::Slice(parts) => {
            let mut values = [py.None().into_bound(py), py.None().into_bound(py), py.None().into_bound(py)];
            for (value, part) in values.iter_mut().zip(parts.iter()) {
                if let Some(e) = part {
                    *value = eval(e, cx)?;
                }
            }
            let [lower, upper, step] = values;
            py.get_type::<PySlice>().call1((lower, upper, step))
        }
        Expr::Exists(inner) => {
            let found = match eval(inner, cx) {
                Ok(_) => 1,
                Err(err) if exists_catches(py, &err) => 0,
                Err(err) => return Err(err),
            };
            Ok(PyInt::new(py, found).into_any())
        }
        Expr::Structure(inner) => {
            let value = eval(inner, cx)?;
            utils_attr(py, &MARKUP, "chameleon_rs.utils", "Markup")?.call1((value,))
        }
        Expr::Import(name) => utils_attr(py, &RESOLVE_DOTTED, "chameleon_rs.utils", "resolve_dotted")?.call1((name,)),
        Expr::Interp(s) => eval_interp(s, cx),
        Expr::Custom(c) => c.eval(cx),
        Expr::Raise(args) => {
            let exc = utils_attr(py, &EXPRESSION_ERROR, "chameleon_rs.exc", "ExpressionError")?.call1(args.bind(py))?;
            Err(PyErr::from_value(exc))
        }
        Expr::Ns(code) => eval_ns(code, cx),
        _ => eval(e, cx),
    }
}

static NAMESPACE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static EXEC: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static EVAL: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Runs code in the render's namespace for its program, after binding what Chameleon's render
/// function has in scope: `econtext`, `rcontext`, the default marker, the template builtins
/// and the `attrs` alias.
fn eval_ns<'py>(code: &NsCode, cx: &Cx<'_, 'py>) -> PyResult<Bound<'py, PyAny>> {
    let py = cx.scope.py();
    let key = cx.frame.serial;
    let existing = cx.render.namespaces.borrow().iter().find(|(k, _)| *k == key).map(|(_, ns)| ns.clone());
    let ns = if let Some(ns) = existing {
        ns
    } else {
        let defaults = PyDict::new(py);
        for key in [intern!(py, "__translate"), intern!(py, "__decode"), intern!(py, "__on_error_handler")] {
            if let Some(v) = cx.scope_or_default(key)? {
                defaults.set_item(key, v)?;
            }
        }
        let lookup = wrap_pyfunction!(py_lookup_attr, py)?;
        let ns = utils_attr(py, &NAMESPACE, "chameleon_rs._rewrite", "namespace")?.call1((lookup, defaults))?;
        let ns = ns.cast_into::<PyDict>()?;
        cx.render.namespaces.borrow_mut().push((key, ns.clone()));
        ns
    };
    ns.set_item(intern!(py, "econtext"), cx.scope)?;
    ns.set_item(intern!(py, "rcontext"), cx.render.rcontext(py))?;
    ns.set_item(intern!(py, "__rs_default"), &cx.render.marker)?;
    ns.set_item(intern!(py, "__rs_tpl"), cx.builtins())?;
    ns.set_item(intern!(py, "__rs_attrs"), code.attrs.as_ref().map(|a| a.bind(py)))?;
    let result = if code.exec {
        utils_attr(py, &EXEC, "builtins", "exec")?.call1((code.code.bind(py), &ns))
    } else {
        utils_attr(py, &EVAL, "builtins", "eval")?.call1((code.code.bind(py), &ns))
    };
    result.map_err(|e| unbound_local(py, e, &ns, code.bound.bind(py)))
}

/// A `NameError` for a name the render function binds (but has not bound yet) is, in
/// Chameleon's generated function, an `UnboundLocalError`.
fn unbound_local(py: Python<'_>, e: PyErr, ns: &Bound<'_, PyDict>, bound: &Bound<'_, PySet>) -> PyErr {
    if !e.is_instance_of::<PyNameError>(py) || e.is_instance_of::<PyUnboundLocalError>(py) {
        return e;
    }
    let Ok(name) = e.value(py).getattr(intern!(py, "name")) else { return e };
    if !bound.contains(&name).unwrap_or(false) || ns.contains(&name).unwrap_or(true) {
        return e;
    }
    PyUnboundLocalError::new_err(format!(
        "cannot access local variable '{name}' where it is not associated with a value"
    ))
}

/// Chameleon's `Interpolator` result: one node is the value itself (a sub-expression's may be
/// `None` or not a `str`); otherwise `'%s' * n % (...)`, with `None` as `''`.
fn eval_interp<'py>(s: &StrInterp, cx: &Cx<'_, 'py>) -> PyResult<Bound<'py, PyAny>> {
    let py = cx.scope.py();
    if let [only] = s.parts.as_slice() {
        return match only {
            StrPart::Text(t) => Ok(t.bind(py).clone().into_any()),
            StrPart::Expr(e) => crate::convert::convert_text(cx, &eval(e, cx)?, &s.conv),
        };
    }
    // Joined by Python, so strings with lone surrogates work as they do in Chameleon.
    let pieces = PyList::empty(py);
    for part in &s.parts {
        match part {
            StrPart::Text(t) => pieces.append(t.bind(py))?,
            StrPart::Expr(e) => {
                let value = crate::convert::convert_text(cx, &eval(e, cx)?, &s.conv)?;
                if !value.is_none() {
                    pieces.append(value.str()?)?;
                }
            }
        }
    }
    intern!(py, "").call_method1(intern!(py, "join"), (pieces,))
}

pub(crate) fn eval_all<'py>(items: &[Expr], cx: &Cx<'_, 'py>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    items.iter().map(|e| eval(e, cx)).collect()
}

fn compare<'py>(
    py: Python<'py>,
    a: &Bound<'py, PyAny>,
    op: CmpOp,
    b: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let truth = |v: bool| Ok(PyBool::new(py, v).to_owned().into_any());
    match op {
        CmpOp::Rich(op) => a.rich_compare(b, op),
        CmpOp::Is => truth(a.is(b)),
        CmpOp::IsNot => truth(!a.is(b)),
        CmpOp::In => truth(b.contains(a)?),
        CmpOp::NotIn => truth(!b.contains(a)?),
    }
}

/// Chameleon's `lookup_attr` (utils.py:515), exactly: `getattr`; on `AttributeError` look up
/// `__getitem__` as an attribute and call it; a `KeyError` from that re-raises the original
/// `AttributeError`; any other error propagates.
pub(crate) fn lookup_attr<'py>(obj: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    let py = obj.py();
    match obj.getattr(name) {
        Ok(value) => Ok(value),
        Err(original) if original.is_instance_of::<PyAttributeError>(py) => {
            let get = match obj.getattr(intern!(py, "__getitem__")) {
                Ok(get) => get,
                Err(e) if e.is_instance_of::<PyAttributeError>(py) => return Err(original),
                Err(e) => return Err(e),
            };
            match get.call1((name,)) {
                Ok(value) => Ok(value),
                Err(e) if e.is_instance_of::<PyKeyError>(py) => Err(original),
                Err(e) => Err(e),
            }
        }
        Err(e) => Err(e),
    }
}

/// `lookup_attr` for Python callers (the fallback expressions).
#[pyfunction]
#[pyo3(name = "lookup_attr")]
pub(crate) fn py_lookup_attr<'py>(obj: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    lookup_attr(obj, name)
}

#[cfg(test)]
mod size_tests {
    #[test]
    fn expr_stays_small() {
        // The hot loop walks these trees; keep the node at Phase 4's size.
        assert_eq!(std::mem::size_of::<super::Expr>(), 56);
    }
}
