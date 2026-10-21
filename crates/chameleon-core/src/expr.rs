//! The compile-time expression interface (D1).
//!
//! The core decides *where* expressions are (TAL attributes, `${...}` interpolation) and in
//! what lexical context, but never parses them: an [`ExprCompiler`] supplied by the caller
//! does. `chameleon-py` implements it with CPython's `ast`; core tests use a toy one.

use crate::escape::Escape;
use crate::tok::Tok;

/// Index of a compiled expression in the caller's expression table.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ExprId(pub u32);

/// What the name `default` means where an expression appears. Chameleon makes it a
/// compile-time alias (`Alias(["default"], ...)` in a `Define`), not a scope lookup.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum DefaultAlias {
    /// An ordinary name.
    #[default]
    Name,
    /// The default marker: inside `tal:content` / `tal:replace` (the expression and the
    /// default content), dynamic attribute values, and `tal:case` (the value and its body).
    Marker,
    /// The value `target_language` had at the enclosing `i18n:target` element (its
    /// expression and body).
    TargetLanguage,
}

/// An element's static attributes, the value of its `attrs` alias.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StaticAttrs<'a> {
    /// Which element: Chameleon makes one dictionary per element (a compile-time `Static`),
    /// shared by all its expressions and by every render, so an implementation must return
    /// the same object for the same id.
    pub id: u32,
    /// Name to value as written, in order.
    pub items: &'a [(String, String)],
}

/// The expression engine's settings where an expression appears (the arguments Chameleon's
/// `ExpressionTransform` passes to `ExpressionEngine`). They decide how an expression type
/// that converts its sub-expressions to text (`string:`, `load:`, a custom type's
/// `assign_text`) converts them: each sub-expression gets the conversion the site's own value
/// gets (`_convert_text`), so `${string:${x}}` in text escapes `x` twice, as Chameleon does.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Engine<'a> {
    /// `char_escape`: `Some` converts and escapes like `__quote` in that context (`${...}` in
    /// text, comments and attributes); `None` converts without escaping (`emit_convert`).
    pub escape: Option<Escape>,
    /// `default_marker` is the template's marker (attribute values): a sub-expression whose
    /// value is the marker converts to `default`. Elsewhere Chameleon passes `None`, so the
    /// marker is an ordinary object there.
    pub marker: bool,
    /// `default`: what the marker converts to, the attribute's static value (`None`: nothing).
    pub default: Option<&'a str>,
    /// `literal_false`: `false` for an interpolated boolean attribute, where each converted
    /// value that is false becomes `None`.
    pub literal_false: bool,
}

impl Default for Engine<'_> {
    fn default() -> Self {
        Self { escape: None, marker: false, default: None, literal_false: true }
    }
}

/// Where an expression appears, which changes how some names resolve.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ExprContext<'a> {
    pub default: DefaultAlias,
    /// `attrs` is a compile-time alias for the enclosing element's static attributes, or an
    /// ordinary name outside every element (`None`).
    pub attrs: Option<StaticAttrs<'a>>,
    /// The value is emitted directly (`${...}` or `tal:content`), so the expression may be a
    /// native frame call such as `render_partial(...)` that writes into the output itself.
    pub emitted: bool,
    /// One of the Interpolator's candidates, so a cheap structural rejection is fine (the
    /// message of the final failure is obtained by compiling again with this unset).
    pub candidate: bool,
    /// How sub-expressions convert to text here (see [`Engine`]).
    pub engine: Engine<'a>,
}

/// The template-scope names an expression reads and writes (D9): what concurrent evaluation
/// (an expression may start once the names it reads exist) and last-use analysis need. The
/// resolve pass records one per expression site, by `ValueId`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum FreeVars {
    /// Anything: the expression uses `econtext` or `rcontext` as a value, or a custom
    /// expression type, so no static set is known.
    #[default]
    Dynamic,
    /// Exactly these names (sorted, no duplicates). `reads` are scope lookups (`getname`, a
    /// builtin's `get`, the `__translate`-style keys); `writes` are names the expression
    /// assigns into the scope (comprehension targets, as Chameleon's rewriting does).
    Known { reads: Vec<Box<str>>, writes: Vec<Box<str>> },
}

impl FreeVars {
    /// No names at all (a constant).
    #[must_use]
    pub fn none() -> Self {
        Self::Known { reads: Vec::new(), writes: Vec::new() }
    }

    /// The names of both (an interpolation's parts, a pipe's alternatives).
    #[must_use]
    pub fn union(self, other: Self) -> Self {
        match (self, other) {
            (Self::Known { mut reads, mut writes }, Self::Known { reads: r, writes: w }) => {
                reads.extend(r);
                writes.extend(w);
                reads.sort();
                reads.dedup();
                writes.sort();
                writes.dedup();
                Self::Known { reads, writes }
            }
            _ => Self::Dynamic,
        }
    }
}

/// Why an expression did not compile.
#[derive(Debug, thiserror::Error)]
pub enum ExprCompileError<E> {
    /// Not a valid expression (Chameleon's `ExpressionError`). Drives the Interpolator's
    /// longest-candidate-first retry; where no retry is possible it becomes a compile error.
    /// `token` is the part the message is about (`None`: the whole source).
    #[error("invalid expression: {message}")]
    Invalid { message: String, token: Option<Tok> },
    /// Any other failure (unknown expression type, unsupported construct, interpreter error).
    /// Aborts compilation unchanged.
    #[error("expression compiler error")]
    Other(#[source] E),
}

pub trait ExprCompiler {
    type Error;

    /// Parses and registers one TALES expression. `source` is the expression text after
    /// entity decoding, with Chameleon's position (for error messages).
    fn compile(&mut self, source: &Tok, cx: ExprContext<'_>) -> Result<ExprId, ExprCompileError<Self::Error>>;

    /// Parses and registers a `<?python ... ?>` code block (statements, not an expression).
    fn compile_code(&mut self, source: &Tok) -> Result<ExprId, ExprCompileError<Self::Error>>;

    /// Registers an expression that raises Chameleon's `ExpressionError` with `message` about
    /// `token` when it is evaluated: non-strict mode (`strict=False`) defers a syntax error to
    /// the moment the expression is reached (`ExpressionTransform.__call__`).
    fn deferred(&mut self, message: &str, token: &Tok) -> Result<ExprId, ExprCompileError<Self::Error>>;

    /// The names expression `id` reads and writes (D9). Unknown means [`FreeVars::Dynamic`].
    fn free_vars(&self, id: ExprId) -> FreeVars {
        let _ = id;
        FreeVars::Dynamic
    }

    /// Whether expression (or code block) `id` makes the function it belongs to a generator
    /// (a `yield`): Chameleon's render function then runs nothing, so the function renders
    /// nothing.
    fn is_generator(&self, id: ExprId) -> bool {
        let _ = id;
        false
    }
}
