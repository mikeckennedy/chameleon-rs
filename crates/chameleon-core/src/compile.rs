// Ported from Chameleon's src/chameleon/compiler.py (`Compiler.visit` text coalescing,
// `visit_Start`, `visit_End`, `visit_Attribute`, `visit_DictAttributes`, `visit_Define`,
// `visit_Repeat`, `visit_Condition`, `visit_Content`, `visit_Cache`, `visit_Cancel`,
// `visit_OnError`, `visit_CodeBlock`), modified:
// lowers the program tree to bytecode instead of generating Python source.

//! Lowering the program tree to bytecode: static text coalesced into one instruction per
//! run, attribute prefixes and suffixes precomputed.
//!
//! Node kinds the VM does not run yet (macros, slots, i18n) stop compilation with
//! `UnsupportedError`.

use std::convert::Infallible;

use crate::error::{CompileError, ErrorKind};
use crate::escape::Escape;
use crate::expr::{ExprCompiler, ExprId};
use crate::interpolate::Part;
use crate::nodes::{Attr, AttrValue, Content, ContentKind, Define, Element, Interp, Node, Translation, Tree, Value};
use crate::options::{Mode, Options};
use crate::parser::{Namespaces, ParseOptions, parse};
use crate::resolve::{Resolved, Table, resolve};
use crate::source::check_len;
use crate::tok::Tok;
use crate::tokenize::{tokenize, tokenize_text};
use crate::vm::{AttrInfo, AttrKind, DefineInfo, DictInfo, LoopInfo, NameId, Op, Program, Stage, TextRef, Visibility};
use crate::zpt::build;

/// Compiles a page template to a [`Program`], parsing expressions with `compiler`.
pub fn compile<C: ExprCompiler>(
    src: &str,
    compiler: &mut C,
    options: &Options,
) -> std::result::Result<Program, CompileError<C::Error>> {
    let tree = build_tree(src, options).map_err(widen)?;
    let table = resolve(&tree, compiler, options)?;
    lower(&tree, &table).map_err(widen)
}

/// Tokenizes, parses and builds the program tree of `src` (no expressions are compiled).
pub fn build_tree(src: &str, options: &Options) -> LResult<Tree> {
    if !check_len(src) {
        return Err(CompileError::TooLarge);
    }
    let tokens = match options.mode {
        Mode::Xml => tokenize(src),
        Mode::Text => tokenize_text(src),
    };
    let parse_options =
        ParseOptions { namespaces: Namespaces::zpt(), restricted_namespace: options.restricted_namespace };
    let items = parse(src, &tokens, &parse_options)?;
    build(src, &items, options)
}

/// Each expression site's source text and the names it reads and writes (D9), in tree order:
/// what the resolve table records, for tests and later analyses.
pub fn free_variables<C: ExprCompiler>(
    src: &str,
    compiler: &mut C,
    options: &Options,
) -> std::result::Result<Vec<(String, crate::expr::FreeVars)>, CompileError<C::Error>> {
    let tree = build_tree(src, options).map_err(widen)?;
    let table = resolve(&tree, compiler, options)?;
    Ok(crate::nodes::value_sites(&tree, src)
        .into_iter()
        .map(|(id, text)| (text, table.free_vars(id).cloned().unwrap_or_default()))
        .collect())
}

pub(crate) fn widen<E>(e: CompileError<Infallible>) -> CompileError<E> {
    match e {
        CompileError::Template { kind, message, token } => CompileError::Template { kind, message, token },
        CompileError::TooLarge => CompileError::TooLarge,
        CompileError::Expr(never) => match never {},
    }
}

type LResult<T> = std::result::Result<T, CompileError<Infallible>>;

/// Lowers a tree whose expressions `table` holds.
pub(crate) fn lower(tree: &Tree, table: &Table) -> LResult<Program> {
    if let Some((name, _)) = tree.macros.first() {
        return Err(CompileError::unsupported("metal:define-macro", name));
    }
    let mut l = Lower {
        table,
        ops: Vec::new(),
        text: Vec::new(),
        pending: Vec::new(),
        names: Vec::new(),
        tables: crate::vm::Tables::default(),
    };
    l.node(&tree.body)?;
    l.flush()?;
    l.ops.push(Op::End);
    Ok(Program::new(l.ops, l.text, l.names, l.tables))
}

struct Lower<'a> {
    table: &'a Table,
    ops: Vec<Op>,
    text: Vec<u8>,
    /// Static text not yet emitted, coalesced into one [`Op::Text`].
    pending: Vec<u8>,
    names: Vec<Box<str>>,
    tables: crate::vm::Tables,
}

fn unsupported(what: &str) -> CompileError<Infallible> {
    CompileError::unsupported(what, Tok::plain(""))
}

fn too_large() -> CompileError<Infallible> {
    CompileError::new(ErrorKind::Unsupported, "program too large", Tok::plain(""))
}

fn index(n: usize) -> LResult<u32> {
    u32::try_from(n).map_err(|_| too_large())
}

impl Lower<'_> {
    fn expr(&self, v: &Value) -> LResult<ExprId> {
        match self.table.get(v.id) {
            Some(Resolved::Expr(id)) => Ok(*id),
            _ => Err(CompileError::new(ErrorKind::Unsupported, "internal: expression not compiled", &v.source)),
        }
    }

    fn parts(&self, i: &Interp) -> LResult<&[Part]> {
        match self.table.get(i.id) {
            Some(Resolved::Parts(parts)) => Ok(parts),
            _ => Err(CompileError::new(ErrorKind::Unsupported, "internal: interpolation not compiled", &i.source)),
        }
    }

    #[allow(clippy::too_many_lines)] // one arm per node kind
    fn node(&mut self, node: &Node) -> LResult<()> {
        match node {
            Node::Text(s) => self.pending.extend_from_slice(s.as_bytes()),
            Node::Empty => {}
            Node::Interpolation(i) => {
                if i.translate == Translation::Translated {
                    return Err(CompileError::unsupported("Implicit translation (implicit_i18n_translate)", &i.source));
                }
                let parts = self.parts(i)?.to_vec();
                // A text node that is just `${x}` is a lone interpolation.
                if let [Part::Expr(expr)] = parts.as_slice() {
                    self.op(Op::Emit { expr: *expr, esc: i.escape, stage: Stage::Lone })?;
                } else {
                    self.emit_parts(&parts, i.escape, false)?;
                }
            }
            Node::Sequence(nodes) => {
                for n in nodes {
                    self.node(n)?;
                }
            }
            Node::Element(e) => self.element(e)?,
            Node::Content(c) => self.content(c)?,
            Node::Define(d) => self.define(d)?,
            Node::Condition { expr, body } => {
                let expr = self.expr(expr)?;
                let at = self.op(Op::JumpIfNot { expr, target: 0 })?;
                self.node(body)?;
                let target = self.here()?;
                if let Some(Op::JumpIfNot { target: t, .. }) = self.ops.get_mut(at) {
                    *t = target;
                }
            }
            Node::Repeat(r) => {
                let expr = self.expr(&r.expr)?;
                let names = r.names.iter().map(|n| self.name(&n.text)).collect::<LResult<Vec<_>>>()?;
                let whitespace = self.intern(r.whitespace.as_bytes())?;
                let info = index(self.tables.loops.len())?;
                self.tables.loops.push(LoopInfo { names, whitespace, visibility: r.visibility });
                let start = self.op(Op::LoopStart { info, expr, next: 0 })?;
                self.node(&r.body)?;
                let body = index(start + 1)?;
                let next = self.op(Op::LoopNext { info, body })?;
                if let Some(Op::LoopStart { next: n, .. }) = self.ops.get_mut(start) {
                    *n = index(next)?;
                }
            }
            Node::Switch { expr, body } => {
                let expr = self.expr(expr)?;
                self.op(Op::Switch { expr })?;
                self.node(body)?;
                self.op(Op::PopSwitch)?;
            }
            Node::Case { expr, body, .. } => {
                let expr = self.expr(expr)?;
                let test = self.op(Op::CaseTest { expr, no_match: 0, matched: 0 })?;
                let default = self.op(Op::CaseDefault { expr, no_match: 0 })?;
                let matched = index(default + 1)?;
                self.node(body)?;
                let no_match = self.here()?;
                if let Some(Op::CaseTest { no_match: n, matched: m, .. }) = self.ops.get_mut(test) {
                    *n = no_match;
                    *m = matched;
                }
                if let Some(Op::CaseDefault { no_match: n, .. }) = self.ops.get_mut(default) {
                    *n = no_match;
                }
            }
            Node::OnError { fallback, body } => {
                // `try: body except Exception: bind error; handler; truncate; fallback`.
                let name = self.name("error")?;
                let at = self.op(Op::Try { handler: 0, name })?;
                self.node(body)?;
                let leave = self.op(Op::EndTry { end: 0 })?;
                let handler = self.here()?;
                self.node(fallback)?;
                let end = self.here()?;
                if let Some(Op::Try { handler: h, .. }) = self.ops.get_mut(at) {
                    *h = handler;
                }
                if let Some(Op::EndTry { end: e }) = self.ops.get_mut(leave) {
                    *e = end;
                }
            }
            Node::CodeBlock(code) => {
                let expr = self.expr(code)?;
                self.op(Op::Exec { expr })?;
            }
            Node::Translate { msgid, .. } => return Err(CompileError::unsupported("i18n:translate", msgid)),
            Node::Name { name, .. } => return Err(CompileError::unsupported("i18n:name", name)),
            Node::Domain { name, .. } => return Err(CompileError::unsupported("i18n:domain", name)),
            Node::TxContext { name, .. } => return Err(CompileError::unsupported("i18n:context", name)),
            Node::Target { expr, .. } => return Err(CompileError::unsupported("i18n:target", &expr.source)),
            Node::DefineSlot { name, .. } => return Err(CompileError::unsupported("metal:define-slot", name)),
            Node::UseMacro(m) => {
                let what = if m.extend { "metal:extend-macro" } else { "metal:use-macro" };
                return Err(CompileError::unsupported(what, &m.expr.source));
            }
            Node::UseInternalMacro(name) => return Err(CompileError::unsupported("metal:define-macro", name)),
        }
        Ok(())
    }

    fn content(&mut self, c: &Content) -> LResult<()> {
        if c.translate == Translation::Translated {
            return Err(CompileError::unsupported("i18n:translate on tal:content / tal:replace", &c.expr.source));
        }
        let expr = self.expr(&c.expr)?;
        let esc = if c.kind == ContentKind::Structure { Escape::Structure } else { Escape::Content };
        let Some(default) = &c.default else {
            // An `on-error` fallback: no default, so the marker is an ordinary object.
            self.op(Op::Emit { expr, esc, stage: Stage::Content })?;
            return Ok(());
        };
        let at = self.op(Op::Content { expr, esc, end: 0 })?;
        self.node(default)?;
        let end = self.here()?;
        if let Some(Op::Content { end: e, .. }) = self.ops.get_mut(at) {
            *e = end;
        }
        Ok(())
    }

    /// `visit_Define`: local names are backed up before each assignment and restored, in
    /// reverse order, after the body; global names are not restored.
    fn define(&mut self, d: &Define) -> LResult<()> {
        let mut locals = Vec::new();
        for assign in &d.assigns {
            let expr = self.expr(&assign.expr)?;
            let names = assign.names.iter().map(|n| self.name(&n.text)).collect::<LResult<Vec<_>>>()?;
            let info = index(self.tables.defines.len())?;
            self.tables.defines.push(DefineInfo { names, visibility: assign.visibility });
            self.op(Op::Define { info, expr })?;
            if assign.visibility == Visibility::Local {
                locals.push(info);
            }
        }
        self.node(&d.body)?;
        for info in locals.into_iter().rev() {
            self.op(Op::Undefine { info })?;
        }
        Ok(())
    }

    fn element(&mut self, e: &Element) -> LResult<()> {
        let omit = match &e.omit {
            Some(v) => {
                let expr = self.expr(v)?;
                self.op(Op::OmitTest { expr })?;
                Some(self.op(Op::JumpIfOmitted { target: 0 })?)
            }
            None => None,
        };
        self.pending.extend_from_slice(e.start.open.as_bytes());
        let dicts: Vec<&Value> = e.start.dicts().collect();
        for dict in &dicts {
            let expr = self.expr(dict)?;
            self.op(Op::CacheValue { expr })?;
        }
        let cached = index(dicts.len())?;
        let mut dict_index = 0;
        for attr in &e.start.attrs {
            match attr {
                Attr::Static { text, filters, name } => {
                    if filters.is_empty() {
                        self.pending.extend_from_slice(text.as_bytes());
                    } else {
                        let info = self.attr_info(name, text, "", None, filters)?;
                        self.op(Op::AttrStatic { info, cached })?;
                    }
                }
                Attr::Dynamic { name, prefix, suffix, value, default, filters } => {
                    self.dynamic_attr(name, prefix, suffix, value, default.as_deref(), filters, cached)?;
                }
                Attr::Dict { exclude, bool_names, .. } => {
                    let exclude = exclude.iter().map(|n| self.intern(n.as_bytes())).collect::<LResult<_>>()?;
                    let bool_names = bool_names.iter().map(|n| self.intern(n.as_bytes())).collect::<LResult<_>>()?;
                    let info = index(self.tables.dicts.len())?;
                    self.tables.dicts.push(DictInfo { exclude, bool_names });
                    self.op(Op::AttrDict { info, index: dict_index, cached })?;
                    dict_index += 1;
                }
            }
        }
        if cached > 0 {
            self.op(Op::PopValues { n: cached })?;
        }
        self.pending.extend_from_slice(e.start.close.as_bytes());
        if let Some(at) = omit {
            // Only an omit test splits the text: the jump needs a target.
            let after_start = self.here()?;
            if let Some(Op::JumpIfOmitted { target }) = self.ops.get_mut(at) {
                *target = after_start;
            }
        }
        self.node(&e.content)?;
        if let Some(end) = &e.end {
            let skip = if omit.is_some() { Some(self.op(Op::JumpIfOmitted { target: 0 })?) } else { None };
            self.pending.extend_from_slice(end.as_bytes());
            if let Some(at) = skip {
                let after_end = self.here()?;
                if let Some(Op::JumpIfOmitted { target }) = self.ops.get_mut(at) {
                    *target = after_end;
                }
            }
        }
        if omit.is_some() {
            self.op(Op::PopOmit)?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn dynamic_attr(
        &mut self,
        name: &str,
        prefix: &str,
        suffix: &str,
        value: &AttrValue,
        default: Option<&str>,
        filters: &[u32],
        cached: u32,
    ) -> LResult<()> {
        let kind = match value {
            AttrValue::Subst { expr, quote } => AttrKind::Subst { expr: self.expr(expr)?, esc: Escape::Attr(*quote) },
            AttrValue::Boolean { expr, .. } => AttrKind::Boolean { expr: self.expr(expr)? },
            AttrValue::Interp { interp, boolean, .. } => {
                if interp.translate == Translation::Translated {
                    return Err(CompileError::unsupported("Implicit attribute translation", &interp.source));
                }
                let parts = self.parts(interp)?.to_vec();
                if !*boolean && filters.is_empty() {
                    // The common case: `name="${x}"` or `name="a ${x} b"`.
                    if let [Part::Expr(expr)] = parts.as_slice() {
                        let prefix = self.intern(prefix.as_bytes())?;
                        let suffix = self.intern(suffix.as_bytes())?;
                        self.op(Op::Attr { expr: *expr, esc: interp.escape, prefix, suffix })?;
                    } else {
                        self.pending.extend_from_slice(prefix.as_bytes());
                        self.emit_parts(&parts, interp.escape, false)?;
                        self.pending.extend_from_slice(suffix.as_bytes());
                    }
                    return Ok(());
                }
                // Boolean or filtered: the value is written, then checked.
                let info = self.attr_info(name, prefix, suffix, default, filters)?;
                if let [Part::Expr(expr)] = parts.as_slice() {
                    self.op(Op::AttrLone { expr: *expr, esc: interp.escape, info, boolean: *boolean, cached })?;
                } else {
                    self.op(Op::AttrBegin { info })?;
                    self.emit_parts(&parts, interp.escape, *boolean)?;
                    self.op(Op::AttrEnd { info, boolean: *boolean, cached })?;
                }
                return Ok(());
            }
            AttrValue::Static(_) | AttrValue::Translate { .. } => {
                return Err(unsupported("i18n:attributes (attribute translation)"));
            }
        };
        let info = self.attr_info(name, prefix, suffix, default, filters)?;
        self.op(Op::AttrValue { info, kind, cached })?;
        Ok(())
    }

    fn attr_info(
        &mut self,
        name: &str,
        prefix: &str,
        suffix: &str,
        default: Option<&str>,
        filters: &[u32],
    ) -> LResult<u32> {
        let info = AttrInfo {
            name: self.intern(name.as_bytes())?,
            prefix: self.intern(prefix.as_bytes())?,
            suffix: self.intern(suffix.as_bytes())?,
            default: default.map(|d| self.intern(d.as_bytes())).transpose()?,
            filters: self.filters(filters)?,
        };
        let i = index(self.tables.attrs.len())?;
        self.tables.attrs.push(info);
        Ok(i)
    }

    fn filters(&mut self, filters: &[u32]) -> LResult<(u32, u32)> {
        let start = index(self.tables.filters.len())?;
        self.tables.filters.extend_from_slice(filters);
        Ok((start, index(filters.len())?))
    }

    /// The parts of an interpolation; for a boolean attribute (`if_true`) a false part writes
    /// nothing.
    fn emit_parts(&mut self, parts: &[Part], esc: Escape, if_true: bool) -> LResult<()> {
        for part in parts {
            match part {
                Part::Text(s) => self.pending.extend_from_slice(s.as_bytes()),
                Part::Expr(expr) => {
                    let stage = Stage::Part;
                    self.op(if if_true {
                        Op::EmitIfTrue { expr: *expr, esc, stage }
                    } else {
                        Op::Emit { expr: *expr, esc, stage }
                    })?;
                }
            }
        }
        Ok(())
    }

    /// Flushes pending text, then appends `op`; returns its index.
    fn op(&mut self, op: Op) -> LResult<usize> {
        self.flush()?;
        self.ops.push(op);
        Ok(self.ops.len() - 1)
    }

    /// The index the next instruction will get (after flushing pending text).
    fn here(&mut self) -> LResult<u32> {
        self.flush()?;
        index(self.ops.len())
    }

    fn flush(&mut self) -> LResult<()> {
        if !self.pending.is_empty() {
            let pending = std::mem::take(&mut self.pending);
            let r = self.intern(&pending)?;
            self.ops.push(Op::Text(r));
        }
        Ok(())
    }

    fn intern(&mut self, bytes: &[u8]) -> LResult<TextRef> {
        let start = index(self.text.len())?;
        let len = index(bytes.len())?;
        self.text.extend_from_slice(bytes);
        Ok(TextRef { start, len })
    }

    fn name(&mut self, name: &str) -> LResult<NameId> {
        let i = if let Some(i) = self.names.iter().position(|n| &**n == name) {
            i
        } else {
            self.names.push(name.into());
            self.names.len() - 1
        };
        Ok(NameId(index(i)?))
    }
}
