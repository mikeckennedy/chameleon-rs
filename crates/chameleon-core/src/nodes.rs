// Ported from Chameleon's src/chameleon/nodes.py, modified: a typed Rust tree holding only
// the node kinds the Phase 2 skeleton builds (Phase 4 completes it).

//! The program tree built from the element tree: what to output, before lowering to bytecode.

use crate::escape::{Escape, Quote};
use crate::expr::ExprId;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Node {
    /// Static output.
    Text(String),
    /// `${...}` interpolation in text or a comment; each expression part is escaped with `esc`
    /// and a `None` value outputs nothing.
    Interpolation {
        parts: Vec<Part>,
        esc: Escape,
    },
    Sequence(Vec<Node>),
    Element {
        start: StartTag,
        body: Box<Node>,
        end: Option<String>,
    },
    /// `tal:content`: the expression's value, or `default` (the original body) when the
    /// expression evaluates to the default marker.
    Content {
        expr: ExprId,
        esc: Escape,
        default: Box<Node>,
    },
    Condition {
        expr: ExprId,
        body: Box<Node>,
    },
    /// `tal:repeat` (local). `whitespace` goes between items, not after the last.
    Repeat {
        names: Vec<String>,
        expr: ExprId,
        whitespace: String,
        body: Box<Node>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Part {
    Text(String),
    Expr(ExprId),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct StartTag {
    /// `<` and the name.
    pub open: String,
    pub attrs: Vec<AttrNode>,
    /// `>`, `/>` or whatever followed the last attribute.
    pub close: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum AttrNode {
    /// The attribute exactly as written, leading whitespace included.
    Static(String),
    /// `name="${expr}"`: dropped when the converted value is `None` (or the default marker).
    Single { prefix: String, expr: ExprId, quote: Quote, suffix: String },
    /// `name="a ${x} b"`: always present; a `None` part outputs nothing.
    Parts { prefix: String, parts: Vec<Part>, quote: Quote, suffix: String },
}
