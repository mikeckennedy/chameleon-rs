// Ported from Chameleon's src/chameleon/compiler.py (`Compiler.visit` text coalescing,
// `visit_Start`, `visit_End`, `visit_Attribute`, `visit_DictAttributes`, `visit_Define`,
// `visit_Repeat`, `visit_Condition`, `visit_Content`, `visit_Cache`, `visit_Cancel`,
// `visit_OnError`, `visit_CodeBlock`, `visit_MacroProgram`, `visit_Macro`'s slot resolution,
// `visit_UseInternalMacro`, `visit_UseExternalMacro`, `visit_DefineSlot`, `visit_Translate`,
// `visit_Name`, `visit_Domain`, `visit_TxContext`, `visit_Target`, `mangle`, and the
// `Interpolator`'s translation), modified: lowers the program tree to bytecode instead of
// generating Python source.

//! Lowering the program tree to bytecode: static text coalesced into one instruction per
//! run, attribute prefixes and suffixes precomputed.
//!
//! A program has several functions, as Chameleon's module does: the main render function
//! first (at instruction 0), then each macro, each followed by the fill-slot bodies (slot
//! closures) defined in it.

use std::collections::VecDeque;
use std::convert::Infallible;
use std::sync::Arc;

use crate::error::{CompileError, ErrorKind};
use crate::escape::Escape;
use crate::expr::{ExprCompiler, ExprId};
use crate::interpolate::{Implicit, Part};
use crate::nodes::{
    Attr, AttrValue, Content, ContentKind, Define, Element, Interp, Node, Translation, Tree, UseMacro, Value,
};
use crate::options::{Mode, Options};
use crate::parser::{Namespaces, ParseOptions, parse};
use crate::resolve::{Resolved, Table, resolve};
use crate::source::{Span, check_len};
use crate::tok::Tok;
use crate::tokenize::{tokenize, tokenize_text};
use crate::vm::{
    AttrInfo, AttrKind, AttrTranslateInfo, DefineInfo, DictInfo, FillInfo, I18nKey, InterpTranslateInfo, LoopInfo,
    MacroEntry, NameId, NameInfo, Op, Program, Stage, Tables, TextRef, TrValue, TranslateInfo, Visibility,
};
use crate::zpt::build;

/// Compiles a page template to a [`Program`], parsing expressions with `compiler`.
pub fn compile<C: ExprCompiler>(
    src: &str,
    compiler: &mut C,
    options: &Options,
) -> std::result::Result<Program, CompileError<C::Error>> {
    compile_tokens(src, None, compiler, options)
}

/// [`compile`] with the tokens given (a user-supplied `tokenizer`): byte spans into `src`, in
/// order and not overlapping. `None` tokenizes as the mode says.
pub fn compile_tokens<C: ExprCompiler>(
    src: &str,
    tokens: Option<Vec<Span>>,
    compiler: &mut C,
    options: &Options,
) -> std::result::Result<Program, CompileError<C::Error>> {
    let tree = build_tree_with(src, tokens, options).map_err(widen)?;
    let table = resolve(&tree, compiler, options)?;
    let mut program = lower(&tree, &table).map_err(widen)?;
    program.mark_scope_sharing(
        |id| matches!(compiler.free_vars(id), crate::expr::FreeVars::Known { writes, .. } if writes.is_empty()),
    );
    Ok(program)
}

/// Tokenizes, parses and builds the program tree of `src` (no expressions are compiled).
pub fn build_tree(src: &str, options: &Options) -> LResult<Tree> {
    build_tree_with(src, None, options)
}

fn build_tree_with(src: &str, tokens: Option<Vec<Span>>, options: &Options) -> LResult<Tree> {
    if !check_len(src) {
        return Err(CompileError::TooLarge);
    }
    let tokens = match (tokens, options.mode) {
        (Some(tokens), _) => {
            let in_order = tokens.windows(2).all(|w| w[0].end <= w[1].start);
            let inside = tokens.last().is_none_or(|t| t.end as usize <= src.len());
            let boundaries = tokens.iter().all(|t| {
                t.start <= t.end && src.is_char_boundary(t.start as usize) && src.is_char_boundary(t.end as usize)
            });
            if !(in_order && inside && boundaries) {
                return Err(CompileError::new(
                    ErrorKind::Value,
                    "tokens must be ordered, non-overlapping slices of the source",
                    Tok::plain(""),
                ));
            }
            tokens
        }
        (None, Mode::Xml) => tokenize(src),
        (None, Mode::Text) => tokenize_text(src),
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
        CompileError::TooDeep(e) => CompileError::TooDeep(e),
        CompileError::Expr(never) => match never {},
    }
}

type LResult<T> = std::result::Result<T, CompileError<Infallible>>;

/// Lowers a tree whose expressions `table` holds.
pub(crate) fn lower(tree: &Tree, table: &Table) -> LResult<Program> {
    let mut l = Lower {
        tree,
        table,
        ops: Vec::new(),
        text: Vec::new(),
        pending: Vec::new(),
        names: Vec::new(),
        tables: Tables::default(),
        slots: Vec::new(),
        translations: Vec::new(),
        fills: VecDeque::new(),
        calls: Vec::new(),
        generator: false,
    };
    l.function(&tree.body)?;
    let mut macros = Vec::with_capacity(tree.macros.len());
    for (name, node) in &tree.macros {
        let entry = l.function(node)?;
        macros.push(MacroEntry { name: name.text.as_str().into(), mangled: mangle(&name.text).into(), entry });
    }
    // `render_<mangled>`: of macros whose names mangle alike, the last one defined wins.
    for (at, name) in std::mem::take(&mut l.calls) {
        let mangled = mangle(&name);
        let entry = macros
            .iter()
            .rev()
            .find(|m| *m.mangled == mangled)
            .map(|m| m.entry)
            .ok_or_else(|| CompileError::unsupported("internal: macro not lowered", Tok::plain(name.clone())))?;
        if let Some(Op::CallMacro { entry: e }) = l.ops.get_mut(at) {
            *e = entry;
        }
    }
    Ok(Program::new(l.ops, l.text, l.names, l.tables, macros))
}

/// Chameleon's `mangle`: every character that is not a word character becomes `_`.
fn mangle(name: &str) -> String {
    name.chars().map(|c| if crate::pychars::is_word(c) { c } else { '_' }).collect()
}

/// The slot names a function resolves when it starts (Chameleon's `_slots`, collected while
/// visiting the function, fill-slot bodies included): mangled, in order of appearance.
fn collect_slots<'t>(tree: &'t Tree, node: &'t Node, out: &mut Vec<Arc<str>>) {
    match node {
        Node::Text(_) | Node::Interpolation(_) | Node::Empty | Node::CodeBlock(_) | Node::UseInternalMacro(_) => {}
        Node::Sequence(items) => items.iter().for_each(|n| collect_slots(tree, n, out)),
        Node::Element(e) => collect_slots(tree, &e.content, out),
        Node::Content(c) => {
            if let Some(d) = &c.default {
                collect_slots(tree, d, out);
            }
        }
        Node::Define(d) => collect_slots(tree, &d.body, out),
        Node::Repeat(r) => collect_slots(tree, &r.body, out),
        Node::Condition { body, .. }
        | Node::Switch { body, .. }
        | Node::Case { body, .. }
        | Node::Translate { body, .. }
        | Node::Name { body, .. }
        | Node::Domain { body, .. }
        | Node::TxContext { body, .. }
        | Node::Target { body, .. } => collect_slots(tree, body, out),
        Node::OnError { fallback, body } => {
            collect_slots(tree, fallback, out);
            collect_slots(tree, body, out);
        }
        Node::DefineSlot { name, body } => {
            let mangled = mangle(&name.text);
            if !out.iter().any(|n| **n == *mangled) {
                out.push(mangled.into());
            }
            collect_slots(tree, body, out);
        }
        Node::UseMacro(m) => {
            if let Some(slots) = tree.slot_lists.get(m.slots.0 as usize) {
                for slot in slots {
                    collect_slots(tree, &slot.body, out);
                }
            }
        }
    }
}

/// A fill-slot body waiting to be lowered as its own function: the `FillSlot` table entry to
/// point at it, and the lexical context it was defined in (its function's slots, the open
/// translations).
struct PendingFill<'t> {
    body: &'t Node,
    info: usize,
    slots: Vec<Arc<str>>,
    translations: Vec<u32>,
}

struct Lower<'a> {
    tree: &'a Tree,
    table: &'a Table,
    ops: Vec<Op>,
    text: Vec<u8>,
    /// Static text not yet emitted, coalesced into one [`Op::Text`].
    pending: Vec<u8>,
    names: Vec<Box<str>>,
    tables: Tables,
    /// The slot names of the function being lowered (indices of [`Op::DefineSlot`]).
    slots: Vec<Arc<str>>,
    /// The open `i18n:translate` elements (indices into `tables.translates`).
    translations: Vec<u32>,
    fills: VecDeque<PendingFill<'a>>,
    /// `CallMacro` instructions and the macro each calls, patched once macros are lowered.
    calls: Vec<(usize, String)>,
    /// The function being lowered has an expression with a `yield` (see
    /// [`ExprCompiler::is_generator`]).
    generator: bool,
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

impl<'a> Lower<'a> {
    /// Lowers a root function (the main body or a macro), then the slot bodies defined in
    /// it; returns where it starts.
    fn function(&mut self, node: &'a Node) -> LResult<u32> {
        let entry = self.here()?;
        let mut slots = Vec::new();
        collect_slots(self.tree, node, &mut slots);
        if !slots.is_empty() {
            let info = index(self.tables.slot_sets.len())?;
            self.tables.slot_sets.push(slots.clone());
            self.op(Op::PopSlots { info })?;
        }
        self.slots = slots;
        self.translations.clear();
        self.generator = false;
        self.node(node)?;
        self.op(Op::End)?;
        self.generator_check(entry)?;
        while let Some(fill) = self.fills.pop_front() {
            let at = self.here()?;
            if let Some(f) = self.tables.fills.get_mut(fill.info) {
                f.entry = at;
            }
            self.slots = fill.slots;
            self.translations = fill.translations;
            self.generator = false;
            self.node(fill.body)?;
            self.op(Op::End)?;
            self.generator_check(at)?;
        }
        Ok(entry)
    }

    /// A function with a `yield` is a generator in Chameleon: calling it runs nothing. Its
    /// instructions are replaced by a return.
    fn generator_check(&mut self, entry: u32) -> LResult<()> {
        if self.generator {
            self.ops.truncate(entry as usize);
            self.calls.retain(|(at, _)| *at < entry as usize);
            self.op(Op::End)?;
        }
        Ok(())
    }

    fn expr(&mut self, v: &Value) -> LResult<ExprId> {
        self.generator |= self.table.is_generator(v.id);
        match self.table.get(v.id) {
            Some(Resolved::Expr(id)) => Ok(*id),
            _ => Err(CompileError::new(ErrorKind::Unsupported, "internal: expression not compiled", &v.source)),
        }
    }

    fn parts(&mut self, i: &Interp) -> LResult<&'a [Part]> {
        self.generator |= self.table.is_generator(i.id);
        let table: &'a Table = self.table;
        match table.get(i.id) {
            Some(Resolved::Parts(parts, _)) => Ok(parts),
            _ => Err(CompileError::new(ErrorKind::Unsupported, "internal: interpolation not compiled", &i.source)),
        }
    }

    /// What an implicitly translated interpolation translates, if it is translated at all.
    fn implicit(&self, i: &Interp) -> Option<&'a Implicit> {
        let table: &'a Table = self.table;
        if i.translate != Translation::Translated {
            return None;
        }
        match table.get(i.id) {
            Some(Resolved::Parts(_, implicit)) => implicit.as_ref(),
            _ => None,
        }
    }

    /// An implicit translation: the parts' values, then the translation (`attr`: the
    /// attribute it is the value of).
    fn interp_translate(&mut self, i: &Interp, implicit: &Implicit, attr: Option<u32>, cached: u32) -> LResult<()> {
        for part in self.parts(i)? {
            if let Part::Expr(expr) = part {
                let info = index(self.tables.tr_values.len())?;
                self.tables.tr_values.push(TrValue::Part { expr: *expr, esc: i.escape });
                self.op(Op::TrValue { info })?;
            }
        }
        let msgid = self.intern(implicit.msgid.as_bytes())?;
        let keys = implicit.keys.iter().map(|k| self.intern(k.as_bytes())).collect::<LResult<_>>()?;
        let info = index(self.tables.interp_translates.len())?;
        self.tables.interp_translates.push(InterpTranslateInfo { msgid, keys, mapping: implicit.mapping, attr });
        self.op(Op::InterpTranslate { info, cached })?;
        Ok(())
    }

    #[allow(clippy::too_many_lines)] // one arm per node kind
    fn node(&mut self, node: &'a Node) -> LResult<()> {
        match node {
            Node::Text(s) => self.pending.extend_from_slice(s.as_bytes()),
            Node::Empty => {}
            Node::Interpolation(i) => {
                if let Some(implicit) = self.implicit(i) {
                    return self.interp_translate(i, implicit, None, 0);
                }
                let parts = self.parts(i)?;
                // A text node that is just `${x}` is a lone interpolation.
                if let [Part::Expr(expr)] = parts {
                    self.op(Op::Emit { expr: *expr, esc: i.escape, stage: Stage::Lone })?;
                } else {
                    self.emit_parts(parts, i.escape, false)?;
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
            Node::Translate { msgid, body } => {
                let explicit = if msgid.text.is_empty() { None } else { Some(self.intern(msgid.text.as_bytes())?) };
                let info = index(self.tables.translates.len())?;
                self.tables.translates.push(TranslateInfo { msgid: explicit, names: Vec::new() });
                self.op(Op::TranslateBegin { info })?;
                self.translations.push(info);
                self.node(body)?;
                self.translations.pop();
                self.op(Op::TranslateEnd { info })?;
            }
            Node::Name { name, body } => {
                let Some(&translate) = self.translations.last() else {
                    return Err(CompileError::unsupported("internal: i18n:name outside a translation", name));
                };
                let name_ref = self.intern(name.text.as_bytes())?;
                let existing = self.tables.translates.get(translate as usize).and_then(|t| {
                    t.names.iter().position(|n| {
                        self.text.get(n.start as usize..(n.start + n.len) as usize) == Some(name.text.as_bytes())
                    })
                });
                let at = if let Some(i) = existing {
                    i
                } else {
                    let t = self
                        .tables
                        .translates
                        .get_mut(translate as usize)
                        .ok_or_else(|| unsupported("internal: translation missing"))?;
                    t.names.push(name_ref);
                    t.names.len() - 1
                };
                let info = index(self.tables.names.len())?;
                self.tables.names.push(NameInfo { translate, index: index(at)?, name: name_ref });
                self.op(Op::NameBegin { info })?;
                self.node(body)?;
                self.op(Op::NameEnd { info })?;
            }
            Node::Domain { name, body } => self.i18n_set(I18nKey::Domain, &name.text, body)?,
            Node::TxContext { name, body } => self.i18n_set(I18nKey::Context, &name.text, body)?,
            Node::Target { expr, body } => {
                let expr = self.expr(expr)?;
                self.op(Op::Target { expr })?;
                self.node(body)?;
                self.op(Op::I18nRestore)?;
            }
            Node::DefineSlot { name, body } => {
                let mangled = mangle(&name.text);
                let slot = self
                    .slots
                    .iter()
                    .position(|n| **n == *mangled)
                    .ok_or_else(|| CompileError::unsupported("internal: slot not collected", name))?;
                let at = self.op(Op::DefineSlot { slot: index(slot)?, end: 0 })?;
                self.node(body)?;
                let end = self.here()?;
                if let Some(Op::DefineSlot { end: e, .. }) = self.ops.get_mut(at) {
                    *e = end;
                }
            }
            Node::UseMacro(m) => self.use_macro(m)?,
            Node::UseInternalMacro(name) => {
                let at = self.op(Op::CallMacro { entry: 0 })?;
                self.calls.push((at, name.text.clone()));
            }
        }
        Ok(())
    }

    fn i18n_set(&mut self, key: I18nKey, name: &str, body: &'a Node) -> LResult<()> {
        let value = index(self.tables.strings.len())?;
        self.tables.strings.push(name.into());
        self.op(Op::I18nSet { key, value })?;
        self.node(body)?;
        self.op(Op::I18nRestore)?;
        Ok(())
    }

    /// `visit_UseExternalMacro` inside its `macroname` definition: each fill-slot registered
    /// (its body lowered later as a function of its own), then the call.
    fn use_macro(&mut self, m: &'a UseMacro) -> LResult<()> {
        let name = self.name("macroname")?;
        let define = index(self.tables.defines.len())?;
        self.tables.defines.push(DefineInfo { names: vec![name], visibility: Visibility::Local });
        let text = self.intern(m.macroname.as_bytes())?;
        self.op(Op::DefineText { info: define, text })?;
        let tree: &'a Tree = self.tree;
        if let Some(slots) = tree.slot_lists.get(m.slots.0 as usize) {
            for slot in slots {
                let info = self.tables.fills.len();
                self.tables.fills.push(FillInfo { name: mangle(&slot.name.text).into(), entry: 0, extend: m.extend });
                self.op(Op::FillSlot { info: index(info)? })?;
                self.fills.push_back(PendingFill {
                    body: &slot.body,
                    info,
                    slots: self.slots.clone(),
                    translations: self.translations.clone(),
                });
            }
        }
        let expr = self.expr(&m.expr)?;
        self.op(Op::UseMacro { expr })?;
        self.op(Op::Undefine { info: define })?;
        Ok(())
    }

    fn content(&mut self, c: &'a Content) -> LResult<()> {
        let translate = c.translate == Translation::Translated;
        let expr = self.expr(&c.expr)?;
        let esc = if c.kind == ContentKind::Structure { Escape::Structure } else { Escape::Content };
        let Some(default) = &c.default else {
            // An `on-error` fallback: no default, so the marker is an ordinary object.
            if translate {
                // (With `i18n:translate=""` on the element, the fallback is translated; the
                // marker then skips nothing, there being no default content.)
                let at = self.op(Op::ContentTranslate { expr, esc, end: 0 })?;
                let end = index(at + 1)?;
                if let Some(Op::ContentTranslate { end: e, .. }) = self.ops.get_mut(at) {
                    *e = end;
                }
            } else {
                self.op(Op::Emit { expr, esc, stage: Stage::Content })?;
            }
            return Ok(());
        };
        let at = self.op(if translate {
            Op::ContentTranslate { expr, esc, end: 0 }
        } else {
            Op::Content { expr, esc, end: 0 }
        })?;
        self.node(default)?;
        let end = self.here()?;
        if let Some(Op::Content { end: e, .. } | Op::ContentTranslate { end: e, .. }) = self.ops.get_mut(at) {
            *e = end;
        }
        Ok(())
    }

    /// `visit_Define`: local names are backed up before each assignment and restored, in
    /// reverse order, after the body; global names are not restored.
    fn define(&mut self, d: &'a Define) -> LResult<()> {
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

    fn element(&mut self, e: &'a Element) -> LResult<()> {
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
        value: &'a AttrValue,
        default: Option<&str>,
        filters: &[u32],
        cached: u32,
    ) -> LResult<()> {
        let kind = match value {
            AttrValue::Subst { expr, quote } => AttrKind::Subst { expr: self.expr(expr)?, esc: Escape::Attr(*quote) },
            AttrValue::Boolean { expr, .. } => AttrKind::Boolean { expr: self.expr(expr)? },
            AttrValue::Interp { interp, boolean, .. } => {
                if let Some(implicit) = self.implicit(interp) {
                    let info = self.attr_info(name, prefix, suffix, default, filters)?;
                    return self.interp_translate(interp, implicit, Some(info), cached);
                }
                let parts = self.parts(interp)?;
                if !*boolean && filters.is_empty() {
                    // The common case: `name="${x}"` or `name="a ${x} b"`.
                    if let [Part::Expr(expr)] = parts {
                        let prefix = self.intern(prefix.as_bytes())?;
                        let suffix = self.intern(suffix.as_bytes())?;
                        self.op(Op::Attr { expr: *expr, esc: interp.escape, prefix, suffix })?;
                    } else {
                        self.pending.extend_from_slice(prefix.as_bytes());
                        self.emit_parts(parts, interp.escape, false)?;
                        self.pending.extend_from_slice(suffix.as_bytes());
                    }
                    return Ok(());
                }
                // Boolean or filtered: the value is written, then checked.
                let info = self.attr_info(name, prefix, suffix, default, filters)?;
                if let [Part::Expr(expr)] = parts {
                    self.op(Op::AttrLone { expr: *expr, esc: interp.escape, info, boolean: *boolean, cached })?;
                } else {
                    self.op(Op::AttrBegin { info })?;
                    self.emit_parts(parts, interp.escape, *boolean)?;
                    self.op(Op::AttrEnd { info, boolean: *boolean, cached })?;
                }
                return Ok(());
            }
            AttrValue::Translate { msgid, value } => {
                let attr = self.attr_info(name, prefix, suffix, default, filters)?;
                self.tr_value(value, name, default)?;
                let msgid = msgid.as_ref().map(|m| self.intern(m.as_bytes())).transpose()?;
                let info = index(self.tables.attr_translates.len())?;
                self.tables.attr_translates.push(AttrTranslateInfo { attr, msgid });
                self.op(Op::AttrTranslate { info, cached })?;
                return Ok(());
            }
            AttrValue::Static(_) => return Err(unsupported("internal: a static value outside a translation")),
        };
        let info = self.attr_info(name, prefix, suffix, default, filters)?;
        self.op(Op::AttrValue { info, kind, cached })?;
        Ok(())
    }

    /// The value a translated attribute translates (`ExpressionTransform.visit_Translate`'s
    /// inner node), pushed on the unwind stack.
    fn tr_value(&mut self, value: &'a AttrValue, name: &str, default: Option<&str>) -> LResult<()> {
        let default = default.map(|d| self.intern(d.as_bytes())).transpose()?;
        let v = match value {
            AttrValue::Static(text) => TrValue::Static(self.intern(text.as_bytes())?),
            AttrValue::Subst { expr, quote } => {
                TrValue::Subst { expr: self.expr(expr)?, esc: Escape::Attr(*quote), default }
            }
            AttrValue::Boolean { expr, .. } => {
                TrValue::Boolean { expr: self.expr(expr)?, name: self.intern(name.as_bytes())?, default }
            }
            AttrValue::Interp { interp, boolean, .. } => {
                let parts = self.parts(interp)?;
                let boolean = if *boolean { Some(self.intern(name.as_bytes())?) } else { None };
                if let [Part::Expr(expr)] = parts {
                    TrValue::Lone { expr: *expr, esc: interp.escape, boolean }
                } else {
                    self.op(Op::CaptureBegin)?;
                    self.emit_parts(parts, interp.escape, boolean.is_some())?;
                    self.op(Op::CaptureEnd { boolean })?;
                    return Ok(());
                }
            }
            AttrValue::Translate { .. } => return Err(unsupported("internal: nested attribute translation")),
        };
        let info = index(self.tables.tr_values.len())?;
        self.tables.tr_values.push(v);
        self.op(Op::TrValue { info })?;
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
