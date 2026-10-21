// Ported from Chameleon's src/chameleon/compiler.py (the order in which `Compiler.visit_*`
// and `ExpressionTransform` compile expressions, the `default` / `attrs` aliases
// `visit_Define` and `visit_Alias` keep, `visit_Assignment` / `visit_Repeat` / `visit_Name`
// name checks, `visit_Cache` / `visit_Cancel`, and non-strict deferral in
// `ExpressionTransform.__call__`), modified: a pass over the typed tree that fills a table of
// compiled expressions instead of generating Python code.

//! Compiling a tree's expressions, in the order Chameleon compiles them.
//!
//! Chameleon builds the whole program first and compiles expressions afterwards, macros
//! before the main body, so a template with both a builder error and a bad expression
//! reports the builder error, and of two bad expressions the one compiled first. The order
//! inside each node follows the generated code: an `on-error` fallback before the element,
//! a content expression before its default content, dictionary attributes before the other
//! attributes, a `use-macro` element's slots before its macro expression.

use crate::error::{CompileError, ErrorKind};
use crate::escape::Escape;
use crate::expr::{DefaultAlias, Engine, ExprCompileError, ExprCompiler, ExprContext, ExprId, FreeVars};
use crate::interpolate::{Implicit, Part, interpolate_full};
use crate::nodes::{Attr, AttrValue, AttrsId, Interp, Node, Translation, Tree, Value, ValueId};
use crate::options::Options;
use crate::tok::Tok;

/// Names a template may not assign (`COMPILER_INTERNALS_OR_DISALLOWED`).
const DISALLOWED_NAMES: [&str; 2] = ["econtext", "rcontext"];

/// What an expression compiled to. A syntax error in non-strict mode compiles to an
/// expression that raises it ([`ExprCompiler::deferred`]); for an interpolation that is its
/// only part, so nothing of it is evaluated, as in Chameleon.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Resolved {
    Expr(ExprId),
    /// An interpolation's parts, and what implicit translation would translate.
    Parts(Vec<Part>, Option<Implicit>),
}

/// The compiled expressions of a tree and their analyses, indexed by [`ValueId`].
#[derive(Debug, Default)]
pub(crate) struct Table {
    pub(crate) values: Vec<Option<Resolved>>,
    /// The names each expression site reads and writes (D9): an interpolation's is the union
    /// of its parts'. Kept here, by site, for the analyses that need them (concurrent
    /// evaluation, last use), not in the tree.
    pub(crate) free: Vec<Option<FreeVars>>,
    /// The sites whose code makes their function a generator (see
    /// [`ExprCompiler::is_generator`]).
    pub(crate) generators: Vec<bool>,
}

impl Table {
    pub(crate) fn get(&self, id: ValueId) -> Option<&Resolved> {
        self.values.get(id.0 as usize).and_then(Option::as_ref)
    }

    pub(crate) fn free_vars(&self, id: ValueId) -> Option<&FreeVars> {
        self.free.get(id.0 as usize).and_then(Option::as_ref)
    }

    pub(crate) fn is_generator(&self, id: ValueId) -> bool {
        self.generators.get(id.0 as usize).copied().unwrap_or(false)
    }
}

/// The engine of `${...}` interpolation with escape `escape` (`char_escape` is empty for
/// CDATA and text mode).
fn interp_escape(escape: Escape) -> Option<Escape> {
    (escape != Escape::Structure).then_some(escape)
}

type Result<T, E> = std::result::Result<T, CompileError<E>>;

/// Compiles every expression of `tree`.
pub(crate) fn resolve<C: ExprCompiler>(tree: &Tree, compiler: &mut C, options: &Options) -> Result<Table, C::Error> {
    let mut r = Resolver {
        tree,
        compiler,
        strict: options.strict,
        surrogate_base: options.surrogate_base,
        default: DefaultAlias::Name,
        attrs: None,
        engine: Engine::default(),
        cached: vec![false; tree.values as usize],
        translations: Vec::new(),
        table: Table {
            values: vec![None; tree.values as usize],
            free: vec![None; tree.values as usize],
            generators: vec![false; tree.values as usize],
        },
    };
    for (_, node) in &tree.macros {
        r.node(node)?;
    }
    r.node(&tree.body)?;
    Ok(r.table)
}

struct Resolver<'t, C: ExprCompiler> {
    tree: &'t Tree,
    compiler: &'t mut C,
    strict: bool,
    surrogate_base: Option<u32>,
    default: DefaultAlias,
    attrs: Option<AttrsId>,
    /// The engine settings of the site being compiled (attributes set them).
    engine: Engine<'t>,
    /// Chameleon's `_expression_cache`: values a `Cache` node has evaluated (never cleared,
    /// shared by all macros).
    cached: Vec<bool>,
    /// `_translations`: the `i18n:name`s of each open `i18n:translate`.
    translations: Vec<Vec<String>>,
    table: Table,
}

impl<'t, C: ExprCompiler> Resolver<'t, C> {
    fn cx(&self, emitted: bool) -> ExprContext<'t> {
        let tree: &'t Tree = self.tree;
        ExprContext {
            default: self.default,
            attrs: self.attrs.and_then(|a| {
                tree.static_attrs.get(a.0 as usize).map(|items| crate::expr::StaticAttrs { id: a.0, items })
            }),
            emitted,
            candidate: false,
            engine: self.engine,
        }
    }

    /// Runs `f` with the engine settings of an attribute or interpolation site.
    fn with_engine<T>(&mut self, engine: Engine<'t>, f: impl FnOnce(&mut Self) -> T) -> T {
        let saved = std::mem::replace(&mut self.engine, engine);
        let result = f(self);
        self.engine = saved;
        result
    }

    /// Runs `f` with `default` aliased to `alias` (a `Define` with `Alias(["default"], ...)`).
    fn with_default<T>(&mut self, alias: DefaultAlias, f: impl FnOnce(&mut Self) -> T) -> T {
        let saved = std::mem::replace(&mut self.default, alias);
        let result = f(self);
        self.default = saved;
        result
    }

    fn store(&mut self, id: ValueId, value: Resolved) {
        let generator = match &value {
            Resolved::Expr(e) => self.compiler.is_generator(*e),
            Resolved::Parts(parts, _) => {
                parts.iter().any(|p| matches!(p, Part::Expr(e) if self.compiler.is_generator(*e)))
            }
        };
        if let Some(g) = self.table.generators.get_mut(id.0 as usize) {
            *g |= generator;
        }
        let free = match &value {
            Resolved::Expr(e) => self.compiler.free_vars(*e),
            Resolved::Parts(parts, _) => parts.iter().fold(FreeVars::none(), |acc, p| match p {
                Part::Text(_) => acc,
                Part::Expr(e) => acc.union(self.compiler.free_vars(*e)),
            }),
        };
        if let Some(slot) = self.table.values.get_mut(id.0 as usize)
            && slot.is_none()
        {
            *slot = Some(value);
            if let Some(f) = self.table.free.get_mut(id.0 as usize) {
                *f = Some(free);
            }
        }
    }

    /// `ExpressionTransform` in non-strict mode: the error is raised when the site is reached.
    fn defer(&mut self, message: &str, token: &Tok) -> Result<ExprId, C::Error> {
        match self.compiler.deferred(message, token) {
            Ok(id) => Ok(id),
            Err(ExprCompileError::Invalid { message, token }) => {
                Err(CompileError::new(ErrorKind::Expression, message, token.unwrap_or_else(|| Tok::plain(""))))
            }
            Err(ExprCompileError::Other(e)) => Err(CompileError::Expr(e)),
        }
    }

    /// `ExpressionTransform`: compiles a value, deferring a syntax error when not strict.
    fn value(&mut self, v: &Value, emitted: bool) -> Result<(), C::Error> {
        if self.table.get(v.id).is_some() {
            return Ok(());
        }
        let resolved = match self.compiler.compile(&v.source, self.cx(emitted)) {
            Ok(id) => Resolved::Expr(id),
            Err(ExprCompileError::Invalid { message, token }) => {
                let token = token.unwrap_or_else(|| v.source.clone());
                if self.strict {
                    return Err(CompileError::new(ErrorKind::Expression, message, token));
                }
                Resolved::Expr(self.defer(&message, &token)?)
            }
            Err(ExprCompileError::Other(e)) => return Err(CompileError::Expr(e)),
        };
        self.store(v.id, resolved);
        Ok(())
    }

    fn interp(&mut self, i: &Interp, emitted: bool) -> Result<(), C::Error> {
        if self.table.get(i.id).is_some() {
            return Ok(());
        }
        let cx = self.cx(emitted);
        let resolved = match interpolate_full(&i.source, cx, self.compiler, self.surrogate_base) {
            Ok((parts, implicit)) => Resolved::Parts(parts, implicit),
            Err(CompileError::Template { kind: ErrorKind::Expression, message, token }) if !self.strict => {
                let (text, pos) = token.resolve("");
                Resolved::Parts(vec![Part::Expr(self.defer(&message, &Tok { text, pos })?)], None)
            }
            Err(e) => return Err(e),
        };
        self.store(i.id, resolved);
        Ok(())
    }

    /// `visit_Cache` for one expression.
    fn cache(&mut self, v: &Value, emitted: bool) -> Result<(), C::Error> {
        if self.cached.get(v.id.0 as usize).copied().unwrap_or(false) {
            return Ok(());
        }
        self.value(v, emitted)?;
        if let Some(c) = self.cached.get_mut(v.id.0 as usize) {
            *c = true;
        }
        Ok(())
    }

    #[allow(clippy::too_many_lines)] // one arm per node kind
    fn node(&mut self, node: &'t Node) -> Result<(), C::Error> {
        match node {
            Node::Text(_) | Node::Empty | Node::UseInternalMacro(_) => {}
            Node::Interpolation(i) => {
                let engine = Engine { escape: interp_escape(i.escape), ..Engine::default() };
                self.with_engine(engine, |r| r.interp(i, i.translate == Translation::Plain))?;
            }
            Node::Sequence(items) => {
                for item in items {
                    self.node(item)?;
                }
            }
            Node::Element(e) => {
                if let Some(omit) = &e.omit {
                    self.cache(omit, false)?;
                }
                for dict in e.start.dicts() {
                    let engine = Engine { marker: true, ..Engine::default() };
                    self.with_engine(engine, |r| r.cache(dict, false))?;
                }
                let cached = e.start.dicts().count();
                for attr in &e.start.attrs {
                    match attr {
                        // An `on-error` fallback keeps only the static attributes, with the
                        // filters that refer to the element's dictionaries, which the fallback
                        // never caches: Chameleon's compiler then uses `None` as an AST node.
                        Attr::Static { filters, .. } if !filters.is_empty() && cached == 0 => {
                            let message = "'NoneType' object has no attribute '_fields'";
                            return Err(CompileError::new(ErrorKind::Attribute, message, Tok::plain("")));
                        }
                        Attr::Static { .. } | Attr::Dict { .. } => {}
                        Attr::Dynamic { value, filters, default, .. } => {
                            // A filtered value is checked before it is written, so it is not
                            // emitted directly (no native frame call).
                            let emitted = filters.is_empty();
                            let default = default.as_deref();
                            self.with_default(DefaultAlias::Marker, |r| r.attr_value(value, emitted, default))?;
                        }
                    }
                }
                self.node(&e.content)?;
            }
            Node::Content(c) => match &c.default {
                Some(default) => self.with_default(DefaultAlias::Marker, |r| {
                    r.cache(&c.expr, c.translate == Translation::Plain)?;
                    r.node(default)
                })?,
                None => self.value(&c.expr, c.translate == Translation::Plain)?,
            },
            Node::Define(d) => {
                let saved = self.attrs.replace(d.attrs);
                for assign in &d.assigns {
                    for name in &assign.names {
                        if DISALLOWED_NAMES.contains(&name.text.as_str()) {
                            return Err(CompileError::new(
                                ErrorKind::Translation,
                                "Name disallowed by compiler.",
                                name,
                            ));
                        }
                        if name.text.starts_with("__") {
                            let message = "Name disallowed by compiler (double underscore).";
                            return Err(CompileError::new(ErrorKind::Translation, message, name));
                        }
                    }
                    self.value(&assign.expr, false)?;
                }
                self.node(&d.body)?;
                self.attrs = saved;
            }
            Node::Condition { expr, body } => {
                self.value(expr, false)?;
                self.node(body)?;
            }
            Node::Repeat(rep) => {
                if let Some(name) = rep.names.iter().find(|n| DISALLOWED_NAMES.contains(&n.text.as_str())) {
                    return Err(CompileError::new(ErrorKind::Translation, "Name disallowed by compiler.", name));
                }
                self.value(&rep.expr, false)?;
                self.node(&rep.body)?;
            }
            Node::Switch { expr, body } => {
                self.cache(expr, false)?;
                self.node(body)?;
            }
            Node::Case { switch, expr, body } => self.with_default(DefaultAlias::Marker, |r| {
                // The condition compiles `value == marker`, `value == switch` and
                // `switch is not CANCEL`; an uncached switch is compiled where it is used.
                r.value(expr, false)?;
                if !r.cached.get(switch.id.0 as usize).copied().unwrap_or(false) {
                    r.value(switch, false)?;
                    // `visit_Cancel` asserts the switch has been cached.
                    return Err(CompileError::new(ErrorKind::Assertion, "", Tok::plain("")));
                }
                r.node(body)
            })?,
            Node::OnError { fallback, body } => {
                self.node(fallback)?;
                self.node(body)?;
            }
            Node::CodeBlock(code) => {
                let id = match self.compiler.compile_code(&code.source) {
                    Ok(id) => id,
                    Err(ExprCompileError::Invalid { message, token }) => {
                        let token = token.unwrap_or_else(|| code.source.clone());
                        return Err(CompileError::new(ErrorKind::Expression, message, token));
                    }
                    Err(ExprCompileError::Other(e)) => return Err(CompileError::Expr(e)),
                };
                self.store(code.id, Resolved::Expr(id));
            }
            Node::Translate { body, .. } => {
                self.translations.push(Vec::new());
                self.node(body)?;
                self.translations.pop();
            }
            Node::Name { name, body } => {
                let Some(names) = self.translations.last_mut() else {
                    return Err(CompileError::new(ErrorKind::Translation, "Not allowed outside of translation.", name));
                };
                if names.contains(&name.text) {
                    // Chameleon's message is not formatted.
                    return Err(CompileError::new(ErrorKind::Translation, "Duplicate translation name: %s.", name));
                }
                names.push(name.text.clone());
                self.node(body)?;
            }
            Node::Domain { body, .. } | Node::TxContext { body, .. } | Node::DefineSlot { body, .. } => {
                self.node(body)?;
            }
            Node::Target { expr, body } => self.with_default(DefaultAlias::TargetLanguage, |r| {
                r.value(expr, false)?;
                r.node(body)
            })?,
            Node::UseMacro(m) => {
                if let Some(slots) = self.tree.slot_lists.get(m.slots.0 as usize) {
                    for slot in slots {
                        self.node(&slot.body)?;
                    }
                }
                self.value(&m.expr, false)?;
            }
        }
        Ok(())
    }

    /// A dynamic attribute's value. `default` is the attribute's static value, which the
    /// marker converts to in `tal:attributes` (Chameleon passes no default to an
    /// interpolated attribute's engine).
    fn attr_value(&mut self, value: &'t AttrValue, emitted: bool, default: Option<&'t str>) -> Result<(), C::Error> {
        let engine = Engine { marker: true, default, ..Engine::default() };
        match value {
            AttrValue::Subst { expr, .. } => self.with_engine(engine, |r| r.value(expr, emitted)),
            AttrValue::Boolean { expr, .. } => self.with_engine(engine, |r| r.value(expr, false)),
            AttrValue::Interp { interp, boolean, .. } => {
                let engine = Engine {
                    escape: interp_escape(interp.escape),
                    marker: true,
                    default: None,
                    literal_false: !boolean,
                };
                let emitted = emitted && !boolean && interp.translate == Translation::Plain;
                self.with_engine(engine, |r| r.interp(interp, emitted))
            }
            AttrValue::Static(_) => Ok(()),
            AttrValue::Translate { value, .. } => self.attr_value(value, false, default),
        }
    }
}
