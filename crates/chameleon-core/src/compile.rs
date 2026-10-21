// Ported from Chameleon's src/chameleon/compiler.py (`Compiler.visit` text coalescing,
// `visit_Start`, `visit_End`, `visit_Attribute`), modified: lowers the program tree to
// bytecode instead of generating Python source.

//! Lowering the program tree to bytecode: static text coalesced into one instruction per
//! run, attribute prefixes and suffixes precomputed.

use crate::error::CompileError;
use crate::escape::Escape;
use crate::expr::ExprCompiler;
use crate::nodes::{AttrNode, Node, Part};
use crate::parser::{ParseOptions, parse};
use crate::source::{Span, check_len};
use crate::tokenize::tokenize;
use crate::vm::{LoopInfo, NameId, Op, Program, TextRef};
use crate::zpt::Builder;

/// Compiles a page template to a [`Program`], parsing expressions with `compiler`.
pub fn compile<C: ExprCompiler>(src: &str, compiler: &mut C) -> Result<Program, CompileError<C::Error>> {
    if !check_len(src) {
        return Err(CompileError::TooLarge);
    }
    let tokens = tokenize(src);
    let items = parse(src, &tokens, &ParseOptions::default())?;
    let tree = Builder::new(src, compiler).build(&items)?;
    lower(&tree).ok_or_else(|| CompileError::new(crate::ErrorKind::Unsupported, "program too large", Span::default()))
}

/// Lowers a program tree. `None` if it does not fit in `u32` indices.
pub(crate) fn lower(tree: &Node) -> Option<Program> {
    let mut l = Lower::default();
    l.node(tree)?;
    l.flush()?;
    l.ops.push(Op::End);
    Some(Program::new(l.ops, l.text, l.names, l.loops))
}

#[derive(Default)]
struct Lower {
    ops: Vec<Op>,
    text: Vec<u8>,
    /// Static text not yet emitted, coalesced into one [`Op::Text`].
    pending: Vec<u8>,
    names: Vec<Box<str>>,
    loops: Vec<LoopInfo>,
}

impl Lower {
    fn node(&mut self, node: &Node) -> Option<()> {
        match node {
            Node::Text(s) => self.pending.extend_from_slice(s.as_bytes()),
            Node::Interpolation { parts, esc } => {
                // A text node that is just `${x}` is a lone interpolation.
                if let [Part::Expr(expr)] = parts.as_slice() {
                    self.op(Op::Emit { expr: *expr, esc: *esc, lone: true })?;
                } else {
                    self.parts(parts, *esc)?;
                }
            }
            Node::Sequence(nodes) => {
                for n in nodes {
                    self.node(n)?;
                }
            }
            Node::Element { start, body, end } => {
                self.pending.extend_from_slice(start.open.as_bytes());
                for attr in &start.attrs {
                    match attr {
                        AttrNode::Static(s) => self.pending.extend_from_slice(s.as_bytes()),
                        AttrNode::Single { prefix, expr, quote, suffix } => {
                            let prefix = self.intern(prefix.as_bytes())?;
                            let suffix = self.intern(suffix.as_bytes())?;
                            self.op(Op::Attr { expr: *expr, esc: Escape::Attr(*quote), prefix, suffix })?;
                        }
                        AttrNode::Parts { prefix, parts, quote, suffix } => {
                            self.pending.extend_from_slice(prefix.as_bytes());
                            self.parts(parts, Escape::Attr(*quote))?;
                            self.pending.extend_from_slice(suffix.as_bytes());
                        }
                    }
                }
                self.pending.extend_from_slice(start.close.as_bytes());
                self.node(body)?;
                if let Some(end) = end {
                    self.pending.extend_from_slice(end.as_bytes());
                }
            }
            Node::Content { expr, esc, default } => {
                let at = self.op(Op::Content { expr: *expr, esc: *esc, end: 0 })?;
                self.node(default)?;
                let end = self.here()?;
                if let Some(Op::Content { end: e, .. }) = self.ops.get_mut(at) {
                    *e = end;
                }
            }
            Node::Condition { expr, body } => {
                let at = self.op(Op::JumpIfNot { expr: *expr, target: 0 })?;
                self.node(body)?;
                let target = self.here()?;
                if let Some(Op::JumpIfNot { target: t, .. }) = self.ops.get_mut(at) {
                    *t = target;
                }
            }
            Node::Repeat { names, expr, whitespace, body } => {
                let names = names.iter().map(|n| self.name(n)).collect::<Option<Vec<_>>>()?;
                let whitespace = self.intern(whitespace.as_bytes())?;
                let info = u32::try_from(self.loops.len()).ok()?;
                self.loops.push(LoopInfo { names, whitespace });
                let start = self.op(Op::LoopStart { info, expr: *expr, next: 0 })?;
                self.node(body)?;
                let body = u32::try_from(start + 1).ok()?;
                let next = self.op(Op::LoopNext { info, body })?;
                if let Some(Op::LoopStart { next: n, .. }) = self.ops.get_mut(start) {
                    *n = u32::try_from(next).ok()?;
                }
            }
        }
        Some(())
    }

    fn parts(&mut self, parts: &[Part], esc: Escape) -> Option<()> {
        for part in parts {
            match part {
                Part::Text(s) => self.pending.extend_from_slice(s.as_bytes()),
                Part::Expr(expr) => {
                    self.op(Op::Emit { expr: *expr, esc, lone: false })?;
                }
            }
        }
        Some(())
    }

    /// Flushes pending text, then appends `op`; returns its index.
    fn op(&mut self, op: Op) -> Option<usize> {
        self.flush()?;
        self.ops.push(op);
        Some(self.ops.len() - 1)
    }

    /// The index the next instruction will get (after flushing pending text).
    fn here(&mut self) -> Option<u32> {
        self.flush()?;
        u32::try_from(self.ops.len()).ok()
    }

    fn flush(&mut self) -> Option<()> {
        if !self.pending.is_empty() {
            let pending = std::mem::take(&mut self.pending);
            let r = self.intern(&pending)?;
            self.ops.push(Op::Text(r));
        }
        Some(())
    }

    fn intern(&mut self, bytes: &[u8]) -> Option<TextRef> {
        let start = u32::try_from(self.text.len()).ok()?;
        let len = u32::try_from(bytes.len()).ok()?;
        self.text.extend_from_slice(bytes);
        Some(TextRef { start, len })
    }

    fn name(&mut self, name: &str) -> Option<NameId> {
        let i = if let Some(i) = self.names.iter().position(|n| &**n == name) {
            i
        } else {
            self.names.push(name.into());
            self.names.len() - 1
        };
        Some(NameId(u32::try_from(i).ok()?))
    }
}
