// The semantics below follow Chameleon's src/chameleon/compiler.py (`Compiler.visit_Repeat`,
// `_enter_assignment`, `_leave_assignment`, `visit_Assignment`, `visit_Define`,
// `visit_Content`, `visit_Attribute`, `visit_DictAttributes`, `visit_Cache`, `visit_Cancel`,
// `visit_Condition`, `visit_OnError`, the error handler `visit_Macro` wraps every render
// function in, `visit_CodeBlock`, `emit_bool`, `visit_UseInternalMacro`,
// `visit_UseExternalMacro`, `visit_DefineSlot`, the slot resolution at the start of every
// macro function, `visit_Translate`, `visit_Name`, `visit_Domain`, `visit_TxContext`,
// `visit_Target`, and `ExpressionTransform.visit_Translate`), modified: implemented as
// bytecode run by a resumable VM.

//! The program, its bytecode, and the VM that runs it (D2, D3).
//!
//! The VM is generic over a [`Runtime`] that owns values and their semantics (Python objects
//! in `chameleon-py`). All per-render state lives in [`VmState`], which holds only runtime
//! values, so a render can stop at any instruction and resume later (D9): an instruction
//! either completes or leaves no trace, and a value delivered by [`VmState::resume_with`]
//! takes the place of the expression the instruction was evaluating.
//!
//! A program holds several functions, as Chameleon's generated module does: the main render
//! function, one per `metal:define-macro`, and one per `metal:fill-slot` body (a closure of
//! the function the slot was filled in). Each runs in its own [`Frame`].

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use crate::escape::Escape;
use crate::expr::ExprId;
use crate::output::{Mark, Output};
use crate::surrogates::Wtf8Buf;

/// A slice of a program's static text arena.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TextRef {
    pub start: u32,
    pub len: u32,
}

/// Index into a program's name table (variables assigned by the program).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct NameId(pub u32);

/// How a value reaches the output (Chameleon converts in different steps for each, D6).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stage {
    /// `tal:content` / `tal:replace`: converted once and appended; a non-`str` result is an
    /// error (Chameleon's final `''.join`).
    Content,
    /// A whole interpolation (`attr="${x}"`, a text node that is just `${x}`): converted, then
    /// converted again without escaping (`emit_convert`).
    Lone,
    /// One part of a mixed interpolation: converted, then `%s`-formatted.
    Part,
    /// A whole interpolation that is a boolean attribute's value (the value is already known
    /// to be true): converted like [`Stage::Lone`], then tested. The runtime returns
    /// [`Emitted::True`] for a true result (the VM writes the attribute's name instead),
    /// writes a false one that is not `None` as text ([`Emitted::Value`]), and returns
    /// [`Emitted::Nothing`] for `None`.
    Bool,
    /// A translated attribute value: `'%s' % value` unless it is `None`, nothing converted.
    Format,
    /// `__append(value)` as is (a translation's result): a `str`, or the error Chameleon's
    /// final `''.join` raises.
    Append,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct LoopInfo {
    pub(crate) names: Vec<NameId>,
    pub(crate) whitespace: TextRef,
    /// A global loop (`tal:repeat="global x ..."`) also assigns each item in `rcontext`, takes
    /// no backups, and ends with a `NameError` (see [`Loops::missing_backup`]).
    pub(crate) visibility: Visibility,
}

/// Where a `tal:define` (or `tal:repeat`) binds its names.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Visibility {
    /// The local scope, restored when the element ends.
    Local,
    /// The local scope and `rcontext`, never restored.
    Global,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct DefineInfo {
    pub(crate) names: Vec<NameId>,
    pub(crate) visibility: Visibility,
}

/// A dynamic attribute: `prefix VALUE suffix`, its static value and its filters.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct AttrInfo {
    pub(crate) name: TextRef,
    pub(crate) prefix: TextRef,
    pub(crate) suffix: TextRef,
    /// What the `default` marker converts to (`None` drops the attribute).
    pub(crate) default: Option<TextRef>,
    /// `(start, len)` in [`Tables::filters`].
    pub(crate) filters: (u32, u32),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct DictInfo {
    pub(crate) exclude: Vec<TextRef>,
    pub(crate) bool_names: Vec<TextRef>,
}

/// A `metal:fill-slot` registered before a macro call.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct FillInfo {
    /// The slot's name, mangled as Chameleon's `__slot_<name>` key mangles it.
    pub(crate) name: Arc<str>,
    /// Where the fill body's function starts.
    pub(crate) entry: u32,
    /// `metal:extend-macro`: added to the left of an existing slot stack.
    pub(crate) extend: bool,
}

/// An `i18n:translate` element.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct TranslateInfo {
    /// The explicit message id (`i18n:translate="msgid"`), else the content is the message id.
    pub(crate) msgid: Option<TextRef>,
    /// The `i18n:name`s inside, in order of appearance: the mapping's keys.
    pub(crate) names: Vec<TextRef>,
}

/// An `i18n:name`: its translation (index into [`Tables::translates`]) and its position in
/// that translation's names.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct NameInfo {
    pub(crate) translate: u32,
    pub(crate) index: u32,
    pub(crate) name: TextRef,
}

/// How [`Op::TrValue`] produces a value for a translation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum TrValue {
    /// A static attribute value, as written.
    Static(TextRef),
    /// A `tal:attributes` value: `__quote(value, quote, entity, default, marker)`.
    Subst { expr: ExprId, esc: Escape, default: Option<TextRef> },
    /// A boolean `tal:attributes` value (`emit_bool`): the marker is the static value, a true
    /// value the attribute's name, anything else `None`.
    Boolean { expr: ExprId, name: TextRef, default: Option<TextRef> },
    /// A lone `${...}` in a translated attribute: quoted, then `emit_convert`; for a boolean
    /// attribute (`boolean` is its name) a false value is `None` and a true result the name.
    Lone { expr: ExprId, esc: Escape, boolean: Option<TextRef> },
    /// One `${name}` of an implicitly translated interpolation: quoted (`assign_text`), the
    /// mapping's value.
    Part { expr: ExprId, esc: Escape },
}

/// A translated attribute (`i18n:attributes`, implicit attribute translation):
/// `translate(msgid or value, default=value, ...)`, written with `%s` unless `None`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct AttrTranslateInfo {
    pub(crate) attr: u32,
    pub(crate) msgid: Option<TextRef>,
}

/// An implicitly translated interpolation (`implicit_i18n_translate`, implicit attribute
/// translation): the message id with `${name}` for each expression, the mapping's keys (the
/// values are on the unwind stack), whether a mapping is passed at all (one constant node is
/// translated without one), and for an attribute its [`AttrInfo`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct InterpTranslateInfo {
    pub(crate) msgid: TextRef,
    pub(crate) keys: Vec<TextRef>,
    pub(crate) mapping: bool,
    pub(crate) attr: Option<u32>,
}

/// Side tables instructions refer to.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct Tables {
    pub(crate) loops: Vec<LoopInfo>,
    pub(crate) defines: Vec<DefineInfo>,
    pub(crate) attrs: Vec<AttrInfo>,
    pub(crate) dicts: Vec<DictInfo>,
    /// Dictionary indices (within the start tag) of each filtered attribute.
    pub(crate) filters: Vec<u32>,
    pub(crate) fills: Vec<FillInfo>,
    /// The (mangled) slot names each function resolves when it starts.
    pub(crate) slot_sets: Vec<Vec<Arc<str>>>,
    pub(crate) translates: Vec<TranslateInfo>,
    pub(crate) names: Vec<NameInfo>,
    pub(crate) tr_values: Vec<TrValue>,
    pub(crate) attr_translates: Vec<AttrTranslateInfo>,
    pub(crate) interp_translates: Vec<InterpTranslateInfo>,
    /// `i18n:domain` / `i18n:context` names.
    pub(crate) strings: Vec<Arc<str>>,
}

/// How [`Op::AttrValue`] converts its value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AttrKind {
    /// `tal:attributes="name expr"`: the default marker means the static value; `None`
    /// drops the attribute; anything else is converted and escaped.
    Subst { expr: ExprId, esc: Escape },
    /// A boolean attribute (`emit_bool`): the marker means the static value, a true value
    /// the attribute's name, a false one drops it.
    Boolean { expr: ExprId },
}

/// Which i18n setting [`Op::I18nSet`] changes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum I18nKey {
    /// `i18n:domain` (`__i18n_domain`).
    Domain,
    /// `i18n:context` (`__i18n_context`).
    Context,
}

/// One instruction. Jump targets are instruction indices within the same program.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Op {
    /// Static text, pre-joined at compile time.
    Text(TextRef),
    /// Evaluate, convert and write a value (`${...}`, attribute value parts).
    Emit {
        expr: ExprId,
        esc: Escape,
        stage: Stage,
    },
    /// A part of a boolean attribute's value: like [`Op::Emit`], but a false value writes
    /// nothing (`literal_false=False`). Separate so `Emit`, the hottest instruction, does
    /// not test a flag.
    EmitIfTrue {
        expr: ExprId,
        esc: Escape,
        stage: Stage,
    },
    /// `prefix VALUE suffix` for a single-expression attribute; nothing if the converted
    /// value is `None` (or the default marker, whose default is absent).
    Attr {
        expr: ExprId,
        esc: Escape,
        prefix: TextRef,
        suffix: TextRef,
    },
    /// `tal:content`: if the value is the default marker, run the default body that follows;
    /// otherwise write the value and continue at `end`.
    Content {
        expr: ExprId,
        esc: Escape,
        end: u32,
    },
    /// [`Op::Content`] with `i18n:translate=""`: the value is translated first.
    ContentTranslate {
        expr: ExprId,
        esc: Escape,
        end: u32,
    },
    /// `tal:condition`: continue at `target` if the value is false.
    JumpIfNot {
        expr: ExprId,
        target: u32,
    },
    /// `tal:repeat` setup; the body follows, `next` is the matching [`Op::LoopNext`].
    LoopStart {
        info: u32,
        expr: ExprId,
        next: u32,
    },
    /// End of a loop body: whitespace, then the next item or the loop exit.
    LoopNext {
        info: u32,
        body: u32,
    },
    /// One `tal:define` assignment: back the names up (local only), evaluate, bind.
    Define {
        info: u32,
        expr: ExprId,
    },
    /// A local assignment of static text (`macroname` around a macro call).
    DefineText {
        info: u32,
        text: TextRef,
    },
    /// Restore a local assignment's names (in order), dropping its unwind entry.
    Undefine {
        info: u32,
    },
    /// `tal:omit-tag` with an expression: evaluate once and remember whether it is true.
    OmitTest {
        expr: ExprId,
    },
    /// Continue at `target` if the innermost omit test was true (the tag is left out).
    JumpIfOmitted {
        target: u32,
    },
    PopOmit,
    /// Evaluate a `tal:attributes` dictionary before the start tag (`Cache`).
    CacheValue {
        expr: ExprId,
    },
    PopValues {
        n: u32,
    },
    /// A static attribute that a later dictionary may override: its text (the attribute
    /// table's `prefix`) is written unless one of the `cached` start-tag dictionaries in its
    /// filters has its name. (The payload is in the table to keep `Op` at 24 bytes.)
    AttrStatic {
        info: u32,
        cached: u32,
    },
    /// A `tal:attributes` value (see [`AttrKind`]).
    AttrValue {
        info: u32,
        kind: AttrKind,
        cached: u32,
    },
    /// A single-expression interpolated attribute that is boolean or filtered.
    AttrLone {
        expr: ExprId,
        esc: Escape,
        info: u32,
        boolean: bool,
        cached: u32,
    },
    /// Start of a mixed interpolated attribute that is boolean or filtered: the prefix, then
    /// the parts (written by [`Op::Emit`]), then [`Op::AttrEnd`] checks the result.
    AttrBegin {
        info: u32,
    },
    AttrEnd {
        info: u32,
        boolean: bool,
        cached: u32,
    },
    /// A dictionary's items as attributes; the dictionary is cached value `index`.
    AttrDict {
        info: u32,
        index: u32,
        cached: u32,
    },
    /// `tal:switch`: evaluate once for the cases inside.
    Switch {
        expr: ExprId,
    },
    PopSwitch,
    /// `tal:case`, first step: unless the switch is cancelled, evaluate the case and compare
    /// it with the switch value. A comparison that is `False` (identity) goes on to
    /// [`Op::CaseDefault`]; another true one cancels the switch and continues at `matched`;
    /// a false one continues at `no_match`.
    CaseTest {
        expr: ExprId,
        no_match: u32,
        matched: u32,
    },
    /// `tal:case`, second step: evaluate the case *again* and compare it with the default
    /// marker; true cancels the switch and continues, false continues at `no_match`.
    CaseDefault {
        expr: ExprId,
        no_match: u32,
    },
    /// `tal:on-error`: sets the frame's fallback mark (Chameleon's one `__fallback` local per
    /// render function, shared by nested handlers) and enters the protected body. An
    /// `Exception` raised before the matching [`Op::EndTry`] binds `name` (always `error`),
    /// calls the error handler, truncates the output to the fallback mark and continues at
    /// `handler`.
    Try {
        handler: u32,
        name: NameId,
    },
    /// End of a protected body: leave it and skip the handler.
    EndTry {
        end: u32,
    },
    /// Evaluate and discard (a `<?python ?>` code block).
    Exec {
        expr: ExprId,
    },
    /// The slot resolution at the start of a macro function: `__slot_x =
    /// econtext['__slot_x'].pop()` (`None` on any error) for each name of the slot set.
    PopSlots {
        info: u32,
    },
    /// `metal:define-slot`: if the function resolved a fill for slot `slot`, run it (a new
    /// frame with a copy of the scope) and continue at `end`; otherwise run the default
    /// content that follows.
    DefineSlot {
        slot: u32,
        end: u32,
    },
    /// Before a macro call: register a `metal:fill-slot` in the scope's slot stacks.
    FillSlot {
        info: u32,
    },
    /// `metal:use-macro` / `metal:extend-macro`: evaluate the macro, call its `include`
    /// (natively when it is one of ours) with a copy of the scope, then
    /// `econtext.update(rcontext)`.
    UseMacro {
        expr: ExprId,
    },
    /// Where a `metal:define-macro` element stood: render that macro of this program
    /// (`__token = None`, then a call with a copy of the scope, then
    /// `econtext.update(rcontext)`).
    CallMacro {
        entry: u32,
    },
    /// `i18n:domain` / `i18n:context`: back the setting up and set it (string `value`).
    I18nSet {
        key: I18nKey,
        value: u32,
    },
    /// `i18n:target`: back `target_language` up, evaluate, set it.
    Target {
        expr: ExprId,
    },
    /// Restore the innermost i18n setting changed by [`Op::I18nSet`] or [`Op::Target`].
    I18nRestore,
    /// `i18n:translate`: open a translation stream.
    TranslateBegin {
        info: u32,
    },
    /// Close the stream: normalize its whitespace and append `translate(...)`.
    TranslateEnd {
        info: u32,
    },
    /// `i18n:name`: open the name's stream.
    NameBegin {
        info: u32,
    },
    /// Close it: the text becomes the mapping value, `${name}` is written instead.
    NameEnd {
        info: u32,
    },
    /// Push a value for a translation (see [`TrValue`]).
    TrValue {
        info: u32,
    },
    /// In a slot body: a text node or attribute value of several parts starts (until
    /// [`Op::PartsEnd`]). Chameleon writes such a value whole, once every part is evaluated;
    /// the VM writes each part as it goes, so where a handler keeps the body's output
    /// ([`VmState::fill_catch`]) the parts written before the one that failed are taken back.
    PartsBegin,
    PartsEnd,
    /// Capture what the instructions until [`Op::CaptureEnd`] write.
    CaptureBegin,
    /// Push the captured text as a value; for a boolean attribute (`boolean`), a non-empty
    /// result becomes the attribute's name and an empty one `None`.
    CaptureEnd {
        boolean: Option<TextRef>,
    },
    /// Translate the pushed value of an attribute and write it (see [`AttrTranslateInfo`]).
    AttrTranslate {
        info: u32,
        cached: u32,
    },
    /// Translate an interpolation from its pushed parts and write it (see
    /// [`InterpTranslateInfo`]).
    InterpTranslate {
        info: u32,
        cached: u32,
    },
    /// The end of a function: return to the caller.
    End,
}

/// A named entry point of a program: a `metal:define-macro`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MacroEntry {
    /// The name as written.
    pub name: Box<str>,
    /// Chameleon's function name suffix (`render_<mangled>`): every character that is not a
    /// word character replaced by `_`.
    pub mangled: Box<str>,
    pub entry: u32,
}

/// An immutable compiled template. `Send + Sync`, shared through `Arc`.
#[derive(Debug)]
pub struct Program {
    pub(crate) ops: Vec<Op>,
    /// The static text arena, as written to the output: UTF-8, or WTF-8 (`surrogatepass`
    /// bytes) when the source had lone surrogates.
    pub(crate) text: Box<[u8]>,
    /// The arena as UTF-8 when it differs from `text` (lone surrogates still in their
    /// private-use block): what [`Program::text_str`] slices.
    text_utf8: Option<Box<str>>,
    /// The private-use block the source's lone surrogates were mapped to
    /// ([`crate::surrogates`]).
    surrogate_base: Option<u32>,
    pub(crate) names: Vec<Box<str>>,
    pub(crate) tables: Tables,
    pub(crate) macros: Vec<MacroEntry>,
    /// The entries (sorted) of the functions that may run with their caller's scope instead
    /// of a copy of it (see [`Program::mark_scope_sharing`]).
    shares_scope: Vec<u32>,
    /// The last render's output size, used as the next buffer's capacity (D6).
    capacity: AtomicUsize,
}

impl Program {
    pub(crate) fn new(
        ops: Vec<Op>,
        text: Vec<u8>,
        names: Vec<Box<str>>,
        tables: Tables,
        macros: Vec<MacroEntry>,
    ) -> Self {
        Self {
            ops,
            text: text.into_boxed_slice(),
            text_utf8: None,
            surrogate_base: None,
            names,
            tables,
            macros,
            shares_scope: Vec::new(),
            capacity: AtomicUsize::new(0),
        }
    }

    /// Records that the source's lone surrogates were mapped to the private-use block at
    /// `base` ([`crate::surrogates`]): the output arena gets their `surrogatepass` bytes,
    /// while [`Program::text_str`] keeps the mapped text.
    pub fn set_surrogate_base(&mut self, base: u32) {
        self.surrogate_base = Some(base);
        let mut bytes = self.text.to_vec();
        if crate::surrogates::block_to_surrogates(&mut bytes, base) {
            let utf8 = std::mem::replace(&mut self.text, bytes.into_boxed_slice());
            self.text_utf8 = String::from_utf8(utf8.into_vec()).ok().map(String::into_boxed_str);
        }
    }

    /// Finds the functions (macros, slot bodies) that cannot tell whether they run with a
    /// copy of their caller's scope (Chameleon's `econtext.copy()`) or with that scope itself:
    /// no instruction writes the scope (no `tal:define`, `tal:repeat`, `tal:on-error`, code
    /// block, macro call or slot fill) and every expression only reads names
    /// (`expr_reads_only`: its free variables are known and it assigns none; `econtext` used
    /// as a value or a custom expression type is not). Then the copy holds the same names,
    /// lookups give the same values, and nothing is written to either, so a call can skip
    /// the copy (Phase 10).
    pub fn mark_scope_sharing(&mut self, expr_reads_only: impl Fn(ExprId) -> bool) {
        let mut sharing = Vec::new();
        let mut start = 0usize;
        let mut pure = true;
        for (pc, op) in self.ops.iter().enumerate() {
            if matches!(op, Op::End) {
                if pure && let Ok(entry) = u32::try_from(start) {
                    sharing.push(entry);
                }
                start = pc + 1;
                pure = true;
            } else if pure {
                pure = self.op_reads_only(op, &expr_reads_only);
            }
        }
        self.shares_scope = sharing;
    }

    fn op_reads_only(&self, op: &Op, expr_reads_only: &impl Fn(ExprId) -> bool) -> bool {
        match op {
            Op::Text(_)
            | Op::JumpIfOmitted { .. }
            | Op::PopOmit
            | Op::PopValues { .. }
            | Op::AttrStatic { .. }
            | Op::AttrBegin { .. }
            | Op::AttrEnd { .. }
            | Op::AttrDict { .. }
            | Op::PopSwitch
            | Op::PopSlots { .. }
            | Op::DefineSlot { .. }
            | Op::I18nSet { .. }
            | Op::I18nRestore
            | Op::TranslateBegin { .. }
            | Op::TranslateEnd { .. }
            | Op::NameBegin { .. }
            | Op::NameEnd { .. }
            | Op::PartsBegin
            | Op::PartsEnd
            | Op::CaptureBegin
            | Op::CaptureEnd { .. }
            | Op::AttrTranslate { .. }
            | Op::InterpTranslate { .. }
            | Op::End => true,
            Op::Emit { expr, .. }
            | Op::EmitIfTrue { expr, .. }
            | Op::Attr { expr, .. }
            | Op::Content { expr, .. }
            | Op::ContentTranslate { expr, .. }
            | Op::JumpIfNot { expr, .. }
            | Op::OmitTest { expr }
            | Op::CacheValue { expr }
            | Op::AttrLone { expr, .. }
            | Op::Switch { expr }
            | Op::CaseTest { expr, .. }
            | Op::CaseDefault { expr, .. }
            | Op::Target { expr } => expr_reads_only(*expr),
            Op::AttrValue { kind: AttrKind::Subst { expr, .. } | AttrKind::Boolean { expr }, .. } => {
                expr_reads_only(*expr)
            }
            Op::TrValue { info } => match self.tables.tr_values.get(*info as usize) {
                Some(TrValue::Static(_)) => true,
                Some(
                    TrValue::Subst { expr, .. }
                    | TrValue::Boolean { expr, .. }
                    | TrValue::Lone { expr, .. }
                    | TrValue::Part { expr, .. },
                ) => expr_reads_only(*expr),
                None => false,
            },
            Op::LoopStart { .. }
            | Op::LoopNext { .. }
            | Op::Define { .. }
            | Op::DefineText { .. }
            | Op::Undefine { .. }
            | Op::Try { .. }
            | Op::EndTry { .. }
            | Op::Exec { .. }
            | Op::FillSlot { .. }
            | Op::UseMacro { .. }
            | Op::CallMacro { .. } => false,
        }
    }

    /// The (mangled) slot names the program fills or resolves, possibly repeated.
    pub fn slot_names(&self) -> impl Iterator<Item = &str> {
        let fills = self.tables.fills.iter().map(|f| &*f.name);
        fills.chain(self.tables.slot_sets.iter().flatten().map(|s| &**s))
    }

    /// Whether the function at `entry` may run with its caller's scope (see
    /// [`Program::mark_scope_sharing`]).
    #[must_use]
    pub fn shares_scope(&self, entry: u32) -> bool {
        self.shares_scope.binary_search(&entry).is_ok()
    }

    /// The private-use block the source's lone surrogates were mapped to, if any.
    #[must_use]
    pub fn surrogate_base(&self) -> Option<u32> {
        self.surrogate_base
    }

    /// Whether the static text holds lone surrogates (`surrogatepass` bytes).
    #[must_use]
    pub fn has_surrogate_text(&self) -> bool {
        self.text_utf8.is_some()
    }

    #[must_use]
    pub fn ops(&self) -> &[Op] {
        &self.ops
    }

    /// Names the program assigns, indexed by [`NameId`].
    #[must_use]
    pub fn names(&self) -> &[Box<str>] {
        &self.names
    }

    /// The macros (`metal:define-macro`), in Chameleon's dictionary order. The main render
    /// function starts at instruction 0.
    #[must_use]
    pub fn macros(&self) -> &[MacroEntry] {
        &self.macros
    }

    /// A slice of the static text arena; `None` for a reference outside it (a compiler bug).
    #[must_use]
    pub fn text(&self, r: TextRef) -> Option<&[u8]> {
        let start = r.start as usize;
        self.text.get(start..start + r.len as usize)
    }

    /// [`Program::text`] as a `str` (the arena holds UTF-8 pieces of the template).
    #[must_use]
    pub fn text_str(&self, r: TextRef) -> &str {
        if let Some(utf8) = &self.text_utf8 {
            let start = r.start as usize;
            return utf8.get(start..start + r.len as usize).unwrap_or_default();
        }
        self.text(r).and_then(|b| std::str::from_utf8(b).ok()).unwrap_or_default()
    }

    #[must_use]
    pub fn capacity_hint(&self) -> usize {
        self.capacity.load(Ordering::Relaxed)
    }

    /// Records a render's output size for the next buffer's capacity: averaged with the
    /// previous hint and capped, so one huge render does not inflate every later allocation.
    pub fn record_size(&self, len: usize) {
        let old = self.capacity.load(Ordering::Relaxed);
        let hint = if old == 0 { len } else { old / 2 + len / 2 };
        self.capacity.store(hint.min(MAX_CAPACITY_HINT), Ordering::Relaxed);
    }

    /// A readable listing of the bytecode, for snapshots and debugging.
    #[must_use]
    #[allow(clippy::too_many_lines)] // one arm per instruction
    pub fn dump(&self) -> String {
        use std::fmt::Write;
        let text = |r: TextRef| format!("{:?}", String::from_utf8_lossy(self.text(r).unwrap_or_default()));
        let names =
            |ids: &[NameId]| ids.iter().map(|n| self.names.get(n.0 as usize).map_or("?", |n| &**n)).collect::<Vec<_>>();
        let mut out = String::new();
        for m in &self.macros {
            let _ = writeln!(out, "macro {:?} at {}", m.name, m.entry);
        }
        for (i, op) in self.ops.iter().enumerate() {
            let line = match op {
                Op::Text(r) => format!("text {}", text(*r)),
                Op::Emit { expr, esc, stage } => format!("emit e{} {esc:?} {stage:?}", expr.0),
                Op::EmitIfTrue { expr, esc, stage } => {
                    format!("emit_if_true e{} {esc:?} {stage:?}", expr.0)
                }
                Op::Attr { expr, esc, prefix, suffix } => {
                    format!("attr e{} {esc:?} {} {}", expr.0, text(*prefix), text(*suffix))
                }
                Op::Content { expr, esc, end } => format!("content e{} {esc:?} end={end}", expr.0),
                Op::ContentTranslate { expr, esc, end } => {
                    format!("content_translate e{} {esc:?} end={end}", expr.0)
                }
                Op::JumpIfNot { expr, target } => format!("jump_if_not e{} {target}", expr.0),
                Op::LoopStart { info, expr, next } => match self.tables.loops.get(*info as usize) {
                    Some(li) => {
                        let global = if li.visibility == Visibility::Global { " global" } else { "" };
                        format!(
                            "loop{global} {:?} in e{} ws={} next={next}",
                            names(&li.names),
                            expr.0,
                            text(li.whitespace)
                        )
                    }
                    None => format!("loop ? in e{} next={next}", expr.0),
                },
                Op::LoopNext { body, .. } => format!("loop_next body={body}"),
                Op::Define { info, expr } => match self.tables.defines.get(*info as usize) {
                    Some(d) => format!("define {:?} {:?} = e{}", d.visibility, names(&d.names), expr.0),
                    None => format!("define ? = e{}", expr.0),
                },
                Op::DefineText { info, text: t } => match self.tables.defines.get(*info as usize) {
                    Some(d) => format!("define_text {:?} = {}", names(&d.names), text(*t)),
                    None => format!("define_text ? = {}", text(*t)),
                },
                Op::Undefine { info } => {
                    format!("undefine {:?}", self.tables.defines.get(*info as usize).map(|d| names(&d.names)))
                }
                Op::OmitTest { expr } => format!("omit_test e{}", expr.0),
                Op::JumpIfOmitted { target } => format!("jump_if_omitted {target}"),
                Op::PopOmit => "pop_omit".to_owned(),
                Op::CacheValue { expr } => format!("cache e{}", expr.0),
                Op::PopValues { n } => format!("pop_values {n}"),
                Op::AttrStatic { info, .. } => format!("attr_static {}", self.attr_text(*info)),
                Op::AttrValue { info, kind, .. } => format!("attr_value {kind:?} {}", self.attr_text(*info)),
                Op::AttrLone { expr, esc, info, boolean, .. } => {
                    format!("attr_lone e{} {esc:?} boolean={boolean} {}", expr.0, self.attr_text(*info))
                }
                Op::AttrBegin { info } => format!("attr_begin {}", self.attr_text(*info)),
                Op::AttrEnd { boolean, .. } => format!("attr_end boolean={boolean}"),
                Op::AttrDict { index, .. } => format!("attr_dict {index}"),
                Op::Switch { expr } => format!("switch e{}", expr.0),
                Op::PopSwitch => "pop_switch".to_owned(),
                Op::CaseTest { expr, no_match, matched } => {
                    format!("case_test e{} no_match={no_match} matched={matched}", expr.0)
                }
                Op::CaseDefault { expr, no_match } => format!("case_default e{} no_match={no_match}", expr.0),
                Op::Try { handler, .. } => format!("try handler={handler}"),
                Op::EndTry { end } => format!("end_try {end}"),
                Op::Exec { expr } => format!("exec e{}", expr.0),
                Op::PopSlots { info } => format!("pop_slots {:?}", self.tables.slot_sets.get(*info as usize)),
                Op::DefineSlot { slot, end } => format!("define_slot {slot} end={end}"),
                Op::FillSlot { info } => match self.tables.fills.get(*info as usize) {
                    Some(f) => {
                        format!("fill_slot {:?} at {}{}", f.name, f.entry, if f.extend { " extend" } else { "" })
                    }
                    None => "fill_slot ?".to_owned(),
                },
                Op::UseMacro { expr } => format!("use_macro e{}", expr.0),
                Op::CallMacro { entry } => format!("call_macro {entry}"),
                Op::I18nSet { key, value } => {
                    format!("i18n_set {key:?} {:?}", self.tables.strings.get(*value as usize))
                }
                Op::Target { expr } => format!("target e{}", expr.0),
                Op::I18nRestore => "i18n_restore".to_owned(),
                Op::TranslateBegin { info } => format!("translate_begin {info}"),
                Op::TranslateEnd { info } => match self.tables.translates.get(*info as usize) {
                    Some(t) => format!(
                        "translate_end {info} msgid={:?} names={:?}",
                        t.msgid.map(|m| self.text_str(m)),
                        t.names.iter().map(|n| self.text_str(*n)).collect::<Vec<_>>()
                    ),
                    None => format!("translate_end {info}"),
                },
                Op::NameBegin { .. } => "name_begin".to_owned(),
                Op::NameEnd { info } => match self.tables.names.get(*info as usize) {
                    Some(n) => format!("name_end {:?} of {}", self.text_str(n.name), n.translate),
                    None => "name_end ?".to_owned(),
                },
                Op::TrValue { info } => format!("tr_value {:?}", self.tables.tr_values.get(*info as usize)),
                Op::PartsBegin => "parts_begin".to_owned(),
                Op::PartsEnd => "parts_end".to_owned(),
                Op::CaptureBegin => "capture_begin".to_owned(),
                Op::CaptureEnd { boolean } => format!("capture_end boolean={:?}", boolean.map(|b| self.text_str(b))),
                Op::AttrTranslate { info, .. } => match self.tables.attr_translates.get(*info as usize) {
                    Some(t) => {
                        format!(
                            "attr_translate msgid={:?} {}",
                            t.msgid.map(|m| self.text_str(m)),
                            self.attr_text(t.attr)
                        )
                    }
                    None => "attr_translate ?".to_owned(),
                },
                Op::InterpTranslate { info, .. } => match self.tables.interp_translates.get(*info as usize) {
                    Some(t) => format!(
                        "interp_translate {} keys={:?} mapping={}{}",
                        text(t.msgid),
                        t.keys.iter().map(|k| self.text_str(*k)).collect::<Vec<_>>(),
                        t.mapping,
                        t.attr.map(|a| format!(" {}", self.attr_text(a))).unwrap_or_default()
                    ),
                    None => "interp_translate ?".to_owned(),
                },
                Op::End => "end".to_owned(),
            };
            let _ = writeln!(out, "{i:3} {line}");
        }
        out
    }

    fn attr_text(&self, info: u32) -> String {
        self.tables.attrs.get(info as usize).map_or_else(String::new, |a| {
            format!(
                "{:?}..{:?} default={:?}",
                self.text_str(a.prefix),
                self.text_str(a.suffix),
                a.default.map(|d| self.text_str(d))
            )
        })
    }
}

/// What evaluating an expression produced.
#[derive(Debug)]
pub enum Step<V> {
    Ready(V),
    /// The value must be awaited; the render suspends and resumes with the result (D9).
    Suspend(V),
    /// Run another program in a new frame writing into this output (a native partial, D3).
    /// Only returned where [`Eval::eval`] was asked for an emitted value.
    Call(Call<V>),
}

/// A program to run in a new frame: from [`Step::Call`], a render of its own (a native
/// partial: its main function, fresh i18n settings); from [`MacroStep::Frame`], a macro.
#[derive(Debug)]
pub struct Call<V> {
    pub program: Arc<Program>,
    /// Runtime data for the callee's expressions (in `chameleon-py`, its `Program` object, or
    /// a macro carrying its template's builtins).
    pub code: V,
    pub scope: V,
}

/// What a macro's `include` did ([`Macros::macro_call`]).
#[derive(Debug)]
pub enum MacroStep<V> {
    /// Run a function in a new frame (a macro of ours): where it starts (`0`: the main render
    /// function, for a template used as a macro), and how deep the call nests in interpreter
    /// frames (see [`Errors::frame_budget`]): `1` for a macro (Chameleon calls its function
    /// directly), `2` for a template used as a macro (its `include` method, then its
    /// function). The callee gets the caller's i18n settings.
    Frame { call: Call<V>, entry: u32, cost: u16 },
    /// The runtime called a foreign `include` and wrote its output.
    Done,
}

/// A `metal:fill-slot` body ready to be registered (Chameleon's `__fill_<name>` closure): its
/// function, and what the closure captures from the function that defines it (the i18n
/// settings as default arguments, the slots that function resolved).
#[derive(Debug)]
pub struct Fill<V> {
    pub program: Arc<Program>,
    pub code: V,
    pub entry: u32,
    pub i18n: I18n<V>,
    pub slots: Vec<Option<V>>,
    /// The output stream the defining function was writing to (see [`Frame::stream`]).
    pub stream: Option<u64>,
    /// For a fill defined in a slot body: the i18n settings that body's conversion helpers use
    /// (see [`Extra::helpers`]). `None` for a fill defined in a render function (main or
    /// macro): its helpers use `i18n`.
    pub helpers: Option<Box<I18n<V>>>,
}

/// A render function's i18n settings: Chameleon's `__i18n_domain`, `__i18n_context` and
/// `target_language` locals, passed on to every macro called.
#[derive(Debug)]
pub struct I18n<V> {
    pub domain: Option<Arc<str>>,
    pub context: Option<Arc<str>>,
    /// `None` is the runtime's null value (`None`), so a fresh render costs no value.
    pub target_language: Option<V>,
}

/// The frame being made current ([`Eval::activate`]).
#[derive(Debug, Clone, Copy)]
pub struct Active<'a, V> {
    pub code: &'a V,
    pub scope: &'a V,
    /// Unique within a render: what per-frame runtime state (a code block's namespace) is
    /// keyed by.
    pub serial: u64,
    pub i18n: &'a I18n<V>,
}

/// Whether [`Eval::emit`] wrote anything (`None` converts to nothing).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Emitted {
    Value,
    Nothing,
    /// [`Stage::Bool`] only: the converted value is true (whatever was written is dropped).
    True,
}

/// Where an error happened, for its error token (Chameleon's `__token`, assigned before each
/// expression): the expressions whose evaluation the current frame started last and the one
/// before it. The runtime resolves the token only on the error path (an expression whose
/// source has no position sets no token, so the previous one counts); the VM records the
/// pair with one store per evaluation.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ErrorSite {
    pub last: Option<ExprId>,
    pub previous: Option<ExprId>,
}

impl ErrorSite {
    /// From the packed form kept in [`VmState`]: `previous + 1` in the high half, `last + 1`
    /// in the low half (`0` for none).
    #[allow(clippy::cast_possible_truncation)] // the halves are u32 by construction
    fn unpack(recent: u64) -> Self {
        let id = |v: u32| v.checked_sub(1).map(ExprId);
        Self { last: id(recent as u32), previous: id((recent >> 32) as u32) }
    }
}

/// A loop's countdown (Chameleon's `__index`): the length `repeat()` returned, decremented
/// after each item, with whitespace written while it stays positive. Almost always a
/// machine integer; anything else is left to the runtime's arithmetic, so it fails (or not)
/// exactly when Chameleon's `index -= 1; if index > 0` would.
#[derive(Debug)]
pub enum Countdown<V> {
    Int(i64),
    Value(V),
}

/// What [`Loops::repeat`] returns: an iterator and the loop's countdown.
pub type Loop<V> = (V, Countdown<V>);

/// A translation call ([`Translate::translate`]), with exactly the arguments Chameleon's
/// generated code passes at that site.
#[derive(Debug)]
pub enum Translation<'a, V> {
    /// `i18n:translate`: `translate(msgid, mapping=mapping, default=default, domain=...,
    /// context=..., target_language=<local>)`.
    Block { msgid: Msgid<'a>, mapping: Option<Vec<(&'a str, NameValue)>>, default: &'a Wtf8Buf },
    /// `tal:content` with `i18n:translate=""`: `translate(value, default=None, domain=...,
    /// context=..., target_language=<local>)`.
    Content(V),
    /// A translated attribute: `translate(msgid or value, default=value, domain=...,
    /// context=..., target_language=getname('target_language'))`.
    Attr { msgid: Option<&'a str>, value: V },
    /// An implicitly translated interpolation: `translate(msgid, [mapping=mapping,]
    /// domain=..., context=..., target_language=getname('target_language'))`.
    Interp { msgid: &'a str, mapping: Option<Vec<(&'a str, V)>> },
}

/// A translated block's message id: the `i18n:translate` attribute's (source text), or its
/// normalized content (taken from the output, so it may hold lone surrogates).
#[derive(Debug, Clone, Copy)]
pub enum Msgid<'a> {
    Source(&'a str),
    Stream(&'a Wtf8Buf),
}

impl std::fmt::Display for Msgid<'_> {
    /// Lossy for a stream's lone surrogates.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Source(s) => f.write_str(s),
            Self::Stream(w) => w.fmt(f),
        }
    }
}

/// An `i18n:name`'s value in its translation's mapping: its text, or, when an error left
/// the name unfinished (and a `tal:on-error` inside the translation caught it), the stream
/// itself (Chameleon's list of the pieces appended so far, here the text so far as one piece).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NameValue {
    Text(Wtf8Buf),
    Pieces(Vec<Wtf8Buf>),
}

/// The values a runtime works with (D2). The VM is generic over the sub-traits below; a
/// runtime implements them all and gets [`Runtime`] through the blanket impl.
pub trait Values {
    type Value;
    type Error;
    /// Another reference to a value (a macro call passes the caller's `target_language` on).
    fn clone_value(&mut self, value: &Self::Value) -> Self::Value;
}

/// Frames, evaluation and output.
pub trait Eval: Values {
    /// Makes a frame current. Called whenever the active frame changes. Names are per
    /// program: a runtime resolves a [`NameId`] against the program of the active frame.
    fn activate(&mut self, frame: Active<'_, Self::Value>) -> Result<(), Self::Error>;
    /// `emitted` is true where the value is written straight to the output, the only place a
    /// [`Step::Call`] may be returned.
    fn eval(&mut self, expr: ExprId, emitted: bool) -> Result<Step<Self::Value>, Self::Error>;
    /// Converts and writes a value for `stage`.
    fn emit(&mut self, value: Self::Value, esc: Escape, stage: Stage, out: &mut Output)
    -> Result<Emitted, Self::Error>;
    fn is_default(&mut self, value: &Self::Value) -> bool;
    fn truthy(&mut self, value: &Self::Value) -> Result<bool, Self::Error>;
    /// Whether a value is the runtime's `False` object (identity, not truth).
    fn is_false(&mut self, value: &Self::Value) -> bool;
    /// The frame of a [`Step::Call`] was pushed: a native partial's render starts (its own
    /// `rcontext`, builtins and defaults, until [`Eval::partial_returned`] or
    /// [`Errors::partial_failed`]). Not called when the call failed to nest (too deep).
    fn partial_entered(&mut self) {}
    /// A native partial's frame returned.
    fn partial_returned(&mut self) {}
    /// Whether the native partial just returned as [`Step::Call`] should render on its own,
    /// concurrently, instead of in a frame (opt-in concurrency, D9). Asked for every partial
    /// call, so it must be cheap.
    fn wants_defer(&mut self) -> bool {
        false
    }
    /// Starts the partial [`Eval::wants_defer`] asked for as a render of its own and returns
    /// the placeholder to write in its place (the embedding replaces it with the partial's
    /// output when the render ends), or `None` to run it in a frame after all. Called only
    /// where the output written is final (see [`VmState::can_defer`]).
    fn defer_partial(&mut self) -> Result<Option<Vec<u8>>, Self::Error> {
        Ok(None)
    }
    /// The render would suspend on `value` in an emitted position where the output written is
    /// final: the embedding may start awaiting it concurrently instead and return the
    /// placeholder to write (it converts the result for `esc` and `stage` and fills it in when
    /// the render ends; `at` is where an error of the value is reported), or give the value
    /// back to suspend on (opt-in, D9).
    fn defer_value(
        &mut self,
        value: Self::Value,
        esc: Escape,
        stage: Stage,
        at: ErrorSite,
    ) -> Result<Result<Vec<u8>, Self::Value>, Self::Error> {
        let _ = (esc, stage, at);
        Ok(Err(value))
    }
}

/// Names: backups, binding, restoring.
pub trait Names: Values {
    /// The current value of a name (local, then global), as a loop's or define's backup.
    fn lookup(&mut self, name: NameId) -> Result<Option<Self::Value>, Self::Error>;
    /// Binds a name to the runtime's null value (`None`) in the local scope.
    fn set_null(&mut self, name: NameId) -> Result<(), Self::Error>;
    /// `tal:define`: binds the names in the local scope (unpacking for several names); a
    /// global definition also stores the whole value under each name in `rcontext`.
    fn define(&mut self, names: &[NameId], value: Self::Value, visibility: Visibility) -> Result<(), Self::Error>;
    /// Restores a name: deletes it if there was no backup.
    fn restore(&mut self, name: NameId, backup: Option<Self::Value>) -> Result<(), Self::Error>;
    /// A string value (`None` gives the runtime's null value).
    fn text_value(&mut self, text: Option<&str>) -> Self::Value;
    /// A string value taken from the output (a capture), which may hold lone surrogates.
    fn stream_value(&mut self, text: &Wtf8Buf) -> Self::Value {
        self.text_value(Some(&text.to_string()))
    }
}

/// `tal:repeat`.
pub trait Loops: Values {
    /// Registers a loop (Chameleon's `repeat(key, iterable)`) and returns an iterator over
    /// what it returned, and its length.
    fn repeat(&mut self, names: &[NameId], iterable: Self::Value) -> Result<Loop<Self::Value>, Self::Error>;
    /// `index - 1` and whether the result is `> 0`, for a [`Countdown::Value`].
    fn countdown(&mut self, index: &Self::Value) -> Result<(Self::Value, bool), Self::Error>;
    /// The next item. It must not suspend: [`Op::LoopNext`] performs its side effects only
    /// after this returns.
    fn next(&mut self, iterator: &Self::Value) -> Result<Option<Self::Value>, Self::Error>;
    /// Binds the loop names to an item (unpacking it for several names); a global loop also
    /// binds them in `rcontext` (unpacking the item again, as a chained assignment does).
    fn assign(&mut self, names: &[NameId], item: Self::Value, visibility: Visibility) -> Result<(), Self::Error>;
    /// The error a global loop raises when it ends: Chameleon restores names it never backed
    /// up, a `NameError` for its backup variable.
    fn missing_backup(&mut self, name: NameId) -> Self::Error;
}

/// Errors: `tal:on-error` and Chameleon's per-function error records. The defaults suit
/// runtimes without error tracking (every error is catchable, nothing is recorded).
pub trait Errors: Values {
    /// Whether `tal:on-error` catches the error (`except Exception`).
    fn is_catchable(&mut self, error: &Self::Error) -> bool {
        let _ = error;
        true
    }
    /// `tal:on-error` caught `error`: binds `name` to the error information (with the
    /// position of the error token `at` identifies) and calls the error handler. The VM then
    /// truncates the output and runs the fallback.
    fn catch(&mut self, error: &Self::Error, name: NameId, at: ErrorSite) -> Result<(), Self::Error> {
        let _ = (error, name, at);
        Ok(())
    }
    /// The current frame ends with `error` (Chameleon's render-function `except`: the token,
    /// its position, the filename and the error are appended to `rcontext['__error__']`).
    /// Not called for a slot's frame, whose function has no such handler.
    fn record_error(&mut self, error: &Self::Error, at: ErrorSite) {
        let _ = (error, at);
    }
    /// A native partial's frame ends with `error` (already recorded): its render's handler
    /// (Chameleon's `BaseTemplate.render` formats the error it raises) returns the error its
    /// caller sees.
    fn partial_failed(&mut self, error: Self::Error) -> Self::Error {
        error
    }
    /// The error for too many nested frames (`RecursionError`).
    fn recursion_error(&mut self) -> Self::Error;
    /// How deep calls may nest, in [`Call::cost`] units, before [`Errors::recursion_error`]:
    /// asked once per render, when nesting first gets deep. Chameleon's macros are Python
    /// functions, so its limit is the interpreter's recursion limit minus the frames already
    /// on the stack.
    fn frame_budget(&mut self) -> usize {
        DEFAULT_FRAME_BUDGET
    }
}

/// Attributes and `tal:case`.
pub trait Attrs: Values {
    /// `name in itertools.chain(container)`: whether iterating `container` yields an item
    /// equal to the string `name` (an attribute filter).
    fn yields_name(&mut self, container: &Self::Value, name: &str) -> Result<bool, Self::Error>;
    /// Writes a dictionary's items as ` name="value"` attributes (`visit_DictAttributes`).
    fn attr_dict(
        &mut self,
        dict: &Self::Value,
        exclude: &[&str],
        bool_names: &[&str],
        out: &mut Output,
    ) -> Result<(), Self::Error>;
    /// `value == switch` (or `value == default marker` when `switch` is `None`), as a value.
    fn case_compare(&mut self, value: &Self::Value, switch: Option<&Self::Value>) -> Result<Self::Value, Self::Error>;
}

/// Macros and slots (`metal:*`).
pub trait Macros: Values {
    /// `__macro.include(...)`: a macro of ours becomes a frame (with a copy of the active
    /// scope); anything else is called with a fresh stream, a copy of the scope, `rcontext`
    /// and the i18n settings, and its output written.
    fn macro_call(&mut self, value: Self::Value, out: &mut Output) -> Result<MacroStep<Self::Value>, Self::Error>;
    /// `econtext.copy()` of the active scope.
    fn copy_scope(&mut self) -> Result<Self::Value, Self::Error>;
    /// Whether a function marked by [`Program::mark_scope_sharing`] may run with its caller's
    /// scope: false once the embedding has handed a scope to user code (which could keep it
    /// and write to it later, where a copy would not see the write).
    fn may_share_scope(&mut self) -> bool {
        true
    }
    /// `econtext.update(rcontext)` in the active scope, after a macro call returned.
    fn after_macro(&mut self) -> Result<(), Self::Error>;
    /// Registers a fill for slot `name` in the active scope: `econtext['__slot_name'] =
    /// deque((fill,))`, or for `metal:extend-macro` `getname('__slot_name').appendleft(fill)`
    /// when that exists.
    fn fill_slot(&mut self, name: &str, fill: Fill<Self::Value>, extend: bool) -> Result<(), Self::Error>;
    /// `econtext['__slot_name'].pop()`, `None` on any error.
    fn pop_slot(&mut self, name: &str) -> Option<Self::Value>;
    /// Runs a resolved slot: a fill of ours is returned for a new frame; anything else is
    /// called with a fresh stream, a copy of the scope and `rcontext`, its output written,
    /// and `None` returned.
    fn slot_call(&mut self, slot: &Self::Value, out: &mut Output) -> Result<Option<Fill<Self::Value>>, Self::Error>;
}

/// Translation (`i18n:*`).
pub trait Translate: Values {
    /// The active frame's i18n settings changed (`i18n:domain`, `i18n:context`,
    /// `i18n:target`, or their restoration), or a slot body whose conversion helpers use other
    /// settings than its own became active, or the frame after one. `helpers`: those settings
    /// (see [`Extra::helpers`]); `None`, the helpers use `i18n`.
    fn i18n_changed(
        &mut self,
        i18n: &I18n<Self::Value>,
        helpers: Option<&I18n<Self::Value>>,
    ) -> Result<(), Self::Error>;
    /// Calls the translation function as Chameleon does at that site.
    fn translate(&mut self, call: Translation<'_, Self::Value>) -> Result<Self::Value, Self::Error>;
    /// `__quote(value, quote, entity, default, marker)`: converted and escaped for `esc`, as a
    /// value (a `str`, `None` or whatever `__html__` / `translate` returned).
    fn quote_value(
        &mut self,
        value: Self::Value,
        esc: Escape,
        default: Option<&str>,
    ) -> Result<Self::Value, Self::Error>;
    /// `emit_convert` (the attribute engine: the marker converts to `None`).
    fn convert_value(&mut self, value: Self::Value) -> Result<Self::Value, Self::Error>;
}

/// Everything the VM needs from its embedding (D2).
pub trait Runtime: Eval + Names + Loops + Errors + Attrs + Macros + Translate {}

impl<T: Eval + Names + Loops + Errors + Attrs + Macros + Translate> Runtime for T {}

/// A runtime's value and error types.
type Val<R> = <R as Values>::Value;
type Err<R> = <R as Values>::Error;

#[derive(Debug, thiserror::Error)]
pub enum VmError<E> {
    /// An error the runtime raised (or [`Errors::recursion_error`]), not caught by any
    /// `tal:on-error`.
    #[error("runtime error")]
    Runtime(#[source] E),
    #[error("internal error: {0}")]
    Internal(&'static str),
}

/// How the render stopped.
#[derive(Debug)]
pub enum Done<V> {
    Finished,
    Suspended(V),
}

/// Nested frames never allowed, whatever the runtime's budget (a guard against a recursion
/// limit raised so high that the frame stack would exhaust memory).
pub const MAX_FRAMES: usize = 100_000;

/// The nesting budget of a runtime that does not choose one ([`Errors::frame_budget`]).
pub const DEFAULT_FRAME_BUDGET: usize = 500;

/// The nesting of a native partial's render ([`Step::Call`]), in interpreter frames:
/// `render_partial`, the template's `render`, `BaseTemplate.render` and the render function.
const PARTIAL_COST: u16 = 4;

/// Nesting below which the budget is not asked for.
const SHALLOW: usize = 64;

/// Largest initial output buffer a program asks for (1 MiB).
pub const MAX_CAPACITY_HINT: usize = 1 << 20;

/// Where a program's main render function starts.
const fn entry_main() -> u32 {
    0
}

/// What kind of function a frame runs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    /// The main render function or a macro: Chameleon wraps each in a handler that records
    /// errors in `rcontext['__error__']`.
    Render,
    /// A native partial's render function: recorded like [`Kind::Render`], and the end of a
    /// render of its own (see [`Eval::partial_entered`]).
    Partial,
    /// A slot body: a closure without such a handler.
    Fill,
}

/// What a frame does for its caller when it returns.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum OnReturn {
    Nothing,
    /// Write the caller's text (an attribute's closing quote).
    Text(TextRef),
    /// `econtext.update(rcontext)` in the caller (after a macro call).
    Update,
}

#[derive(Debug)]
struct Frame<V> {
    program: Arc<Program>,
    pc: u32,
    code: V,
    scope: V,
    kind: Kind,
    on_return: OnReturn,
    /// Height of the unwind stack when the frame was entered; its own entries lie above.
    unwind_base: usize,
    /// The frame's recent expressions while another frame is active (see [`ErrorSite`]).
    recent: u64,
    /// Where `tal:on-error` truncates the output: the mark of the last [`Op::Try`] this
    /// frame entered (one register per frame, like Chameleon's `__fallback` local, so an
    /// outer handler truncates to an inner handler's mark once the inner one has started).
    fallback: Mark,
    serial: u64,
    /// The caller still has to run `econtext.update(rcontext)` (set when a macro returns).
    pending_update: bool,
    /// How deep the frame nests, in [`MacroStep::Frame`]'s `cost` units.
    depth: usize,
    /// What macros, slots and translation need, boxed so a plain frame (a native partial's)
    /// stays small: `None` is no i18n settings and no slots.
    extra: Option<Box<Extra<V>>>,
}

/// A frame's less common state.
#[derive(Debug)]
struct Extra<V> {
    i18n: I18n<V>,
    /// The fills the function resolved when it started (`__slot_*` locals), by slot index; a
    /// slot body sees those of the function it belongs to.
    slots: Vec<Option<V>>,
    /// For a slot body: the output stream its text goes to, its defining function's (a
    /// translation stream's id, or `None` for the render's output), wherever it is called
    /// from. (Other functions write to the stream open when they were called.)
    stream: Option<u64>,
    /// For a slot body: where its own output not yet moved to `stream` starts (it moves when
    /// the body calls a macro or a slot, and when it returns).
    start: Mark,
    /// For a slot body: its output was moved for the call in progress, so its own output
    /// starts again when that call returns.
    flushed: bool,
    /// For a slot body: the i18n settings its conversion helpers use, when they may differ
    /// from its own. Chameleon's `__convert` and `__quote` are closures of the render function
    /// (main or macro) the body belongs to, so a value they translate gets that function's
    /// `__i18n_domain`, `__i18n_context` and `target_language`, while the body's inline
    /// conversion and its `translate` calls use the body's own (which its `i18n:domain`,
    /// `i18n:context` and `i18n:target` change). The body starts with that function's settings
    /// as they were where the fill was defined (the function is suspended in the call that
    /// runs the body), so this is `None` until the body changes its own
    /// ([`VmState::keep_helpers`]), or set from the fill for a fill defined in a slot body.
    helpers: Option<Box<I18n<V>>>,
}

impl<V> Extra<V> {
    fn new(i18n: I18n<V>) -> Self {
        Self { i18n, slots: Vec::new(), stream: None, start: Mark::default(), flushed: false, helpers: None }
    }

    /// Drops the values it holds (it goes back to the spare boxes). `stream` and `start` are
    /// left as they are: whoever takes the box sets them.
    fn clear(&mut self) {
        self.i18n = I18n { domain: None, context: None, target_language: None };
        self.slots.clear();
        self.helpers = None;
    }
}

/// Spare `extra` boxes a render keeps (see [`VmState::extra`]).
const SPARE_EXTRAS: usize = 16;

impl<V> Frame<V> {
    /// The frame's i18n settings (`none` when it has no `extra`).
    fn i18n<'a>(&'a self, none: &'a I18n<V>) -> &'a I18n<V> {
        self.extra.as_ref().map_or(none, |e| &e.i18n)
    }

    fn slots(&self) -> &[Option<V>] {
        self.extra.as_ref().map_or(&[], |e| &e.slots)
    }
}

/// Whether i18n settings are all unset.
fn is_plain<V>(i18n: &I18n<V>) -> bool {
    i18n.domain.is_none() && i18n.context.is_none() && i18n.target_language.is_none()
}

/// The single unwind stack (D3): everything a frame has entered and must leave.
#[derive(Debug)]
enum Unwind<V> {
    Loop {
        iterator: V,
        index: Countdown<V>,
        backups: Vec<Option<V>>,
    },
    /// A local `tal:define` assignment's backups.
    Define {
        backups: Vec<Option<V>>,
    },
    /// Whether a `tal:omit-tag` expression was true.
    Omit(bool),
    /// A cached value (a start tag's `tal:attributes` dictionary, a translation's value).
    Value(V),
    /// A `tal:switch` value; `None` once a case matched (Chameleon's `CANCEL` marker).
    Switch(Option<V>),
    /// A boolean or filtered attribute being written: where it started, where its value
    /// starts.
    Attr {
        start: Mark,
        value: Mark,
    },
    /// A `tal:on-error` body: where its handler starts and the name it binds.
    Try {
        handler: u32,
        name: NameId,
    },
    /// An i18n setting's previous value.
    I18n(Backup<V>),
    /// An `i18n:translate` stream: where it starts, its id, which translation of which
    /// program, and the value of each of its names so far.
    Translate {
        mark: Mark,
        id: u64,
        /// Boxed: the unwind stack's entries stay small (they are moved on every push).
        translation: Box<OpenTranslation>,
    },
    /// An `i18n:name` stream: where it starts, its id, which name of which program, and the
    /// slot body output that belongs before it ([`OpenTranslation::before`]).
    Name {
        mark: Mark,
        id: u64,
        program: Arc<Program>,
        info: u32,
        /// Boxed, allocated only when used: the unwind stack's entries stay small.
        before: Option<Box<Before>>,
    },
    /// Output captured as a value (a translated attribute's interpolation).
    Capture(Mark),
}

/// Slot body output kept aside before an `i18n:name` stream ([`OpenTranslation::before`]).
#[derive(Debug, Default)]
struct Before(Vec<u8>);

/// An open `i18n:translate`: which translation of which program, and its names' values.
#[derive(Debug)]
struct OpenTranslation {
    program: Arc<Program>,
    info: u32,
    names: Vec<NameValue>,
    /// Slot body output that belongs before the stream (in the stream it was opened in, at
    /// `mark`), written there when the stream closes: kept aside rather than inserted at
    /// `mark` each time, which would move the stream's text on every flush (quadratic in a
    /// loop of macro calls; see [`VmState::move_to_stream`]).
    before: Vec<u8>,
}

#[derive(Debug)]
enum Backup<V> {
    Domain(Option<Arc<str>>),
    Context(Option<Arc<str>>),
    Target(Option<V>),
}

/// Per-render state (D2): frames, the unwind stack and the output. Holds only runtime
/// values, so a suspended render can be stored and resumed.
#[derive(Debug)]
pub struct VmState<V> {
    frames: Vec<Frame<V>>,
    unwind: Vec<Unwind<V>>,
    resume: Option<V>,
    /// The active frame's last two evaluated expressions (see [`ErrorSite::unpack`]).
    recent: u64,
    suspended: bool,
    /// The last frame serial and stream id handed out.
    serials: u64,
    /// The runtime's nesting budget, once asked for.
    budget: Option<usize>,
    /// The runtime was last given a slot body's helpers' settings (see [`activate`]).
    helpers_on: bool,
    /// A frame of the render has had helpers' settings of its own (see [`Extra::helpers`]):
    /// until then, activating a frame does not look for them.
    helpers_any: bool,
    /// No i18n settings (what a frame without `extra` has).
    none: I18n<V>,
    /// The slot body frame (its serial) writing a value of several parts, and where the
    /// value starts in the output (see [`Op::PartsBegin`]).
    parts: Option<(u64, Mark)>,
    /// Boxes of popped frames' `extra`, for reuse (an allocation and a free per macro call
    /// cost more than the rest of the call on macOS's allocator).
    spare: Vec<Box<Extra<V>>>,
    pub out: Output,
}

impl<V> VmState<V> {
    /// A render of `program`'s main function with `scope`, no i18n domain or context, and
    /// `target_language` (the render's keyword, Chameleon's `render(target_language=...)`).
    #[must_use]
    pub fn new(program: Arc<Program>, code: V, scope: V, target_language: Option<V>) -> Self {
        let i18n = I18n { domain: None, context: None, target_language };
        Self::at(program, entry_main(), code, scope, i18n, Vec::new())
    }

    /// A render that starts at `entry` (a macro's `include`, or a slot body, called from
    /// outside) with the given i18n settings and, for a slot body, the slots its function
    /// resolved.
    #[must_use]
    pub fn at(program: Arc<Program>, entry: u32, code: V, scope: V, i18n: I18n<V>, slots: Vec<Option<V>>) -> Self {
        // Headroom over the last size: escaping reserves a little ahead, and a render that
        // just fills the buffer must not double it (and copy everything) at its last write.
        let hint = program.capacity_hint();
        let out = Output::with_capacity(if hint == 0 { 0 } else { hint + hint / 8 + 64 });
        let frame = Frame {
            program,
            pc: entry,
            code,
            scope,
            kind: Kind::Render,
            on_return: OnReturn::Nothing,
            unwind_base: 0,
            recent: 0,
            fallback: Mark::default(),
            serial: 1,
            pending_update: false,
            depth: 0,
            extra: (!is_plain(&i18n) || !slots.is_empty()).then(|| Box::new(Extra { slots, ..Extra::new(i18n) })),
        };
        Self {
            frames: vec![frame],
            unwind: Vec::new(),
            resume: None,
            recent: 0,
            suspended: false,
            serials: 1,
            budget: None,
            helpers_on: false,
            helpers_any: false,
            none: I18n { domain: None, context: None, target_language: None },
            parts: None,
            spare: Vec::new(),
            out,
        }
    }

    /// For a slot body called from outside ([`VmState::at`]): the i18n settings its conversion
    /// helpers use (see [`Extra::helpers`]).
    #[must_use]
    pub fn with_helpers(mut self, helpers: I18n<V>) -> Self {
        if let Some(frame) = self.frames.first_mut() {
            let extra = frame.extra.get_or_insert_with(|| {
                Box::new(Extra::new(I18n { domain: None, context: None, target_language: None }))
            });
            extra.helpers = Some(Box::new(helpers));
            self.helpers_any = true;
        }
        self
    }

    /// Supplies the awaited value of the expression the render suspended on. An error if
    /// the render is not suspended.
    pub fn resume_with<E>(&mut self, value: V) -> Result<(), VmError<E>> {
        if !self.suspended {
            return Err(VmError::Internal("resume_with on a render that is not suspended"));
        }
        self.suspended = false;
        self.resume = Some(value);
        Ok(())
    }

    /// Calls `f` on every runtime value the state holds (for a garbage collector's
    /// `__traverse__` once suspended renders live in Python objects).
    pub fn for_each_value(&self, mut f: impl FnMut(&V)) {
        for frame in &self.frames {
            f(&frame.code);
            f(&frame.scope);
            if let Some(extra) = &frame.extra {
                if let Some(t) = &extra.i18n.target_language {
                    f(t);
                }
                extra.slots.iter().flatten().for_each(&mut f);
            }
        }
        for entry in &self.unwind {
            match entry {
                Unwind::Loop { iterator, index, backups } => {
                    f(iterator);
                    if let Countdown::Value(v) = index {
                        f(v);
                    }
                    backups.iter().flatten().for_each(&mut f);
                }
                Unwind::Define { backups } => backups.iter().flatten().for_each(&mut f),
                Unwind::Value(v) | Unwind::Switch(Some(v)) | Unwind::I18n(Backup::Target(Some(v))) => f(v),
                Unwind::Omit(_)
                | Unwind::Switch(None)
                | Unwind::Attr { .. }
                | Unwind::Try { .. }
                | Unwind::I18n(_)
                | Unwind::Translate { .. }
                | Unwind::Name { .. }
                | Unwind::Capture(_) => {}
            }
        }
        if let Some(v) = &self.resume {
            f(v);
        }
    }

    /// Whether output written now is final: no `tal:on-error` body (which may truncate it),
    /// translation or `i18n:name` stream, capture or boolean attribute (which take it back
    /// as text) is open in any frame. A placeholder written then stays in the output, in
    /// place, until the render ends (slot bodies move text, placeholders with it).
    #[must_use]
    pub fn can_defer(&self) -> bool {
        !self.unwind.iter().any(|e| {
            matches!(
                e,
                Unwind::Try { .. }
                    | Unwind::Translate { .. }
                    | Unwind::Name { .. }
                    | Unwind::Capture(_)
                    | Unwind::Attr { .. }
            )
        })
    }

    /// Entries on the unwind stack (for tests and debugging).
    #[must_use]
    pub fn unwind_depth(&self) -> usize {
        self.unwind.len()
    }

    /// Frames on the stack (for tests and debugging).
    #[must_use]
    pub fn frame_depth(&self) -> usize {
        self.frames.len()
    }

    /// Pops the top frame, dropping whatever it left on the unwind stack.
    fn pop_frame(&mut self) {
        if let Some(mut frame) = self.frames.pop() {
            self.unwind.truncate(frame.unwind_base);
            if let Some(mut extra) = frame.extra.take()
                && self.spare.len() < SPARE_EXTRAS
            {
                extra.clear();
                self.spare.push(extra);
            }
        }
    }

    /// The active frame's `extra`, created (from a spare box when there is one) if it has
    /// none.
    fn active_extra(&mut self) -> Option<&mut Extra<V>> {
        let frame = self.frames.last_mut()?;
        if frame.extra.is_none() {
            let none = || I18n { domain: None, context: None, target_language: None };
            let extra = match self.spare.pop() {
                Some(mut extra) => {
                    *extra = Extra::new(none());
                    extra
                }
                None => Box::new(Extra::new(none())),
            };
            frame.extra = Some(extra);
        }
        frame.extra.as_deref_mut()
    }

    /// A translation stream's (or capture's) text: removed from the output after `mark`. The
    /// active frame's fallback mark, if it pointed into the stream, moves to its start: the
    /// stream's text is gone (Chameleon's register then indexes the inner stream's items in
    /// the outer stream, which is not modelled: see architecture.md, Phase 6b amendments).
    fn take_text<E>(&mut self, mark: Mark) -> Result<Wtf8Buf, VmError<E>> {
        if let Some(frame) = self.frames.last_mut()
            && frame.fallback > mark
        {
            frame.fallback = mark;
        }
        let bytes = self.out.take_since(mark);
        // Lone surrogates (written with `surrogatepass`) stay as they are, for the runtime.
        Wtf8Buf::from_bytes(bytes).ok_or(VmError::Internal("translation stream is not UTF-8"))
    }

    /// A box for a frame's `extra`, reused when one is spare.
    fn extra(
        &mut self,
        i18n: I18n<V>,
        slots: Vec<Option<V>>,
        stream: Option<u64>,
        start: Mark,
        helpers: Option<Box<I18n<V>>>,
    ) -> Box<Extra<V>> {
        if helpers.is_some() {
            self.helpers_any = true;
        }
        match self.spare.pop() {
            Some(mut extra) => {
                // Field by field: an empty `slots` keeps the spare box's allocation.
                extra.i18n = i18n;
                if slots.is_empty() {
                    extra.slots.clear();
                } else {
                    extra.slots = slots;
                }
                extra.stream = stream;
                extra.start = start;
                extra.flushed = false;
                extra.helpers = helpers;
                extra
            }
            None => Box::new(Extra { i18n, slots, stream, start, flushed: false, helpers }),
        }
    }

    /// The active frame, a slot body (the caller checks), is about to change its own i18n
    /// settings: its conversion helpers keep the ones it has (see [`Extra::helpers`]).
    #[cold]
    fn keep_helpers<R: Runtime<Value = V>>(&mut self, rt: &mut R) {
        let Some(frame) = self.frames.last_mut() else { return };
        if let Some(extra) = &mut frame.extra
            && extra.helpers.is_none()
        {
            let own = &extra.i18n;
            extra.helpers = Some(Box::new(I18n {
                domain: own.domain.clone(),
                context: own.context.clone(),
                target_language: own.target_language.as_ref().map(|t| rt.clone_value(t)),
            }));
            self.helpers_any = true;
        }
    }

    fn frame_base(&self) -> usize {
        self.frames.last().map_or(0, |f| f.unwind_base)
    }

    fn next_serial(&mut self) -> u64 {
        self.serials += 1;
        self.serials
    }

    /// The stream the active function writes to (Chameleon's `__append` where a slot is
    /// filled): the innermost translation stream it opened, else a slot body's own stream,
    /// else the stream open when the function was called.
    fn current_stream(&self) -> Option<u64> {
        let innermost = |entries: &[Unwind<V>]| {
            entries.iter().rev().find_map(|e| match e {
                Unwind::Translate { id, .. } | Unwind::Name { id, .. } => Some(*id),
                _ => None,
            })
        };
        let frame = self.frames.last()?;
        let (below, own) = self.unwind.split_at(frame.unwind_base.min(self.unwind.len()));
        let stream = frame.extra.as_ref().and_then(|e| e.stream);
        innermost(own).or(if frame.kind == Kind::Fill { stream } else { innermost(below) })
    }

    /// Where a slot body's output bound for `stream` goes: before the first translation stream
    /// opened since (its index in the unwind stack and its mark); `None` if there is none (it
    /// is already in place).
    fn move_target(&self, stream: Option<u64>) -> Option<(usize, Mark)> {
        let base = match stream {
            None => 0,
            Some(id) => {
                let at = self.unwind.iter().rposition(
                    |e| matches!(e, Unwind::Translate { id: i, .. } | Unwind::Name { id: i, .. } if *i == id),
                )?;
                at + 1
            }
        };
        self.unwind.get(base..)?.iter().enumerate().find_map(|(i, e)| match e {
            Unwind::Translate { mark, .. } | Unwind::Name { mark, .. } => Some((base + i, *mark)),
            _ => None,
        })
    }

    /// Calls `f` on every mark into the output the render holds.
    fn for_each_mark(&mut self, mut f: impl FnMut(&mut Mark)) {
        for entry in &mut self.unwind {
            match entry {
                Unwind::Translate { mark, .. } | Unwind::Name { mark, .. } | Unwind::Capture(mark) => f(mark),
                Unwind::Attr { start, value } => {
                    f(start);
                    f(value);
                }
                _ => {}
            }
        }
        for frame in &mut self.frames {
            f(&mut frame.fallback);
            if let Some(extra) = &mut frame.extra {
                f(&mut extra.start);
            }
        }
    }

    /// The unwind entries from `at` up are about to be dropped without closing (an error):
    /// the slot body output kept aside before their streams goes into the output at their
    /// marks, where the handler's truncation then sees it as Chameleon's stream holds it.
    #[cold]
    fn place_before_from(&mut self, at: usize) {
        for i in (at..self.unwind.len()).rev() {
            let (mark, bytes) = match self.unwind.get_mut(i) {
                Some(Unwind::Translate { mark, translation, .. }) => (*mark, std::mem::take(&mut translation.before)),
                Some(Unwind::Name { mark, before, .. }) => (*mark, before.take().map(|b| b.0).unwrap_or_default()),
                _ => continue,
            };
            if bytes.is_empty() {
                continue;
            }
            self.out.insert_at(mark, &bytes);
            let n = bytes.len();
            self.for_each_mark(|m| {
                if *m >= mark {
                    *m = m.shifted(n);
                }
            });
        }
    }

    /// A handler in a slot body caught an error (its own entries above it already dropped).
    /// When the body's output belongs to another stream than the define-slot site's,
    /// Chameleon's `del __stream[__fallback:]` cuts only the latter: what streams opened in the
    /// handler's body held is dropped (`abandoned`), the body's own output is kept (moved to its
    /// stream), and then the define-slot site's stream is cut at the fallback. Returns whether
    /// it did that; otherwise the caller cuts as for any function.
    /// `parts`: the value of several parts the body was writing when the error was raised (in
    /// this frame), whose parts written so far Chameleon would not have written.
    fn fill_catch(&mut self, abandoned: Option<Mark>, parts: Option<(u64, Mark)>) -> bool {
        let Some(frame) = self.frames.last() else { return false };
        if frame.kind != Kind::Fill {
            return false;
        }
        let parts = parts.filter(|&(serial, _)| serial == frame.serial).map(|(_, mark)| mark);
        let Some(stream) = frame.extra.as_ref().map(|e| e.stream) else { return false };
        let own = self.unwind.get(frame.unwind_base..).unwrap_or_default();
        if own.iter().any(|e| matches!(e, Unwind::Translate { .. } | Unwind::Name { .. } | Unwind::Capture(_))) {
            return false;
        }
        if self.move_target(stream).is_none() {
            return false;
        }
        if let Some(mark) = abandoned {
            self.out.truncate(mark);
        }
        if let Some(mark) = parts {
            self.out.truncate(mark);
        }
        self.flush_fill();
        let Some(frame) = self.frames.last_mut() else { return false };
        let cut = frame.fallback;
        self.out.truncate(cut);
        if let Some(extra) = frame.extra.as_mut() {
            extra.flushed = false;
            extra.start = extra.start.min(cut);
        }
        true
    }

    /// A slot body is about to call a macro. Chameleon passes the macro the define-slot site's
    /// `__stream` (the innermost stream open there), while the body itself writes with its
    /// defining function's `__append`: the body's output so far moves to its stream now, and
    /// the macro's output stays where it is written. When the body has a stream of its own
    /// open (a translation or capture inside it), the macro writes to that one, which stays
    /// the body's: only what the body wrote before it opened that stream moves (Chameleon
    /// appended it to the defining function's stream at once, so whatever else writes there
    /// during the call, such as another fill of the same function, comes after it).
    fn flush_fill(&mut self) {
        let Some(frame) = self.frames.last() else { return };
        if frame.kind != Kind::Fill {
            return;
        }
        let own = self.unwind.get(frame.unwind_base..).unwrap_or_default();
        let open = own.iter().find_map(|e| match e {
            Unwind::Translate { mark, .. } | Unwind::Name { mark, .. } | Unwind::Capture(mark) => Some(*mark),
            _ => None,
        });
        let Some((stream, start)) = frame.extra.as_ref().map(|e| (e.stream, e.start)) else { return };
        if let Some(open) = open {
            // The body's own output now starts at that stream (`move_to_stream` moves `start`).
            self.move_to_stream(stream, start, open);
            return;
        }
        let end = self.out.mark();
        self.move_to_stream(stream, start, end);
        let at = self.out.mark();
        if let Some(extra) = self.frames.last_mut().and_then(|f| f.extra.as_mut()) {
            extra.start = at;
            extra.flushed = true;
        }
    }

    /// The call a slot body made after [`VmState::flush_fill`] returned: the body's own output
    /// starts again here (what the call wrote is not the body's). Nothing if no flush happened
    /// (the call writes into a stream of the body's own, or the active frame is no slot body).
    fn restart_fill(&mut self) {
        let at = self.out.mark();
        if let Some(frame) = self.frames.last_mut()
            && frame.kind == Kind::Fill
            && let Some(extra) = &mut frame.extra
            && std::mem::take(&mut extra.flushed)
        {
            extra.start = at;
        }
    }

    /// A slot body's output (from `start` to `end`: the end of the output, or the start of a
    /// stream the body has open) moves to its stream: Chameleon's fill function writes with
    /// its defining function's `__append`, so if translation streams were opened since (around
    /// the `metal:define-slot`), it belongs before the first of them. Marks in the moved output
    /// end up at `end`: they belong to the define-slot site's stream (`tal:on-error` in the
    /// body truncates `__stream`, not what the body appended).
    ///
    /// The output is taken out and kept aside in that stream's entry (`before`), written at
    /// its mark when the stream closes ([`VmState::place_before_from`] on an error): moving it
    /// into place now would move everything written since, on every flush.
    fn move_to_stream(&mut self, stream: Option<u64>, start: Mark, end: Mark) {
        let Some((target, first)) = self.move_target(stream) else { return };
        // `first` after `start`: the first such stream is the body's own, so its output is in
        // place (only reachable with `end` at that stream, from `flush_fill`).
        if end <= start || first > start {
            return;
        }
        let bytes = self.out.cut(start, end);
        match self.unwind.get_mut(target) {
            Some(Unwind::Translate { translation, .. }) => translation.before.extend_from_slice(&bytes),
            Some(Unwind::Name { before, .. }) => before.get_or_insert_default().0.extend_from_slice(&bytes),
            _ => {}
        }
        let n = end.position() - start.position();
        // A partial move leaves what follows `end` in place (a stale mark past the output's
        // end goes to the new end in a full move).
        let partial = end < self.out.mark().shifted(n);
        self.for_each_mark(|m| {
            if partial && *m >= end {
                *m = m.unshifted(n);
            } else if *m >= start {
                *m = start;
            }
        });
    }
}

enum Action<V> {
    /// A native partial's render: the call, where the caller continues, and what it writes
    /// when the callee returns.
    Partial(Call<V>, u32, OnReturn),
    /// A macro or slot body.
    Push(Pushed<V>),
    Pop,
    Suspend(V, u32),
}

/// What a slot body's frame gets from its fill: its own i18n settings, its stream and its
/// helpers' settings ([`Fill::helpers`]).
type FillFrame<V> = (I18n<V>, Option<u64>, Option<Box<I18n<V>>>);

/// A frame to push: the call, where the caller continues, and the frame's details.
struct Pushed<V> {
    call: Call<V>,
    /// Where the function starts.
    entry: u32,
    /// See [`MacroStep::Frame`].
    cost: u16,
    return_pc: u32,
    on_return: OnReturn,
    kind: Kind,
    slots: Vec<Option<V>>,
    /// For a slot body: its own i18n settings, stream, and helpers' settings.
    fill: Option<FillFrame<V>>,
    /// The caller's `__token` is reset before the call (an internal macro call).
    reset_token: bool,
}

impl<V> Pushed<V> {
    /// A macro call (the caller's i18n settings, no slots).
    fn call(call: Call<V>, entry: u32, cost: u16, return_pc: u32, on_return: OnReturn) -> Self {
        Self {
            call,
            entry,
            cost,
            return_pc,
            on_return,
            kind: Kind::Render,
            slots: Vec::new(),
            fill: None,
            reset_token: false,
        }
    }
}

/// A slot body's helpers' settings, when they may differ from its own (see [`Extra::helpers`]).
fn helpers_of<V>(frame: &Frame<V>) -> Option<&I18n<V>> {
    frame.extra.as_ref().and_then(|e| e.helpers.as_deref())
}

/// Makes the active (last) frame the runtime's.
fn activate<R: Runtime>(rt: &mut R, st: &mut VmState<Val<R>>) -> Result<(), VmError<Err<R>>> {
    let Some(frame) = st.frames.last() else { return Ok(()) };
    let i18n = frame.i18n(&st.none);
    rt.activate(Active { code: &frame.code, scope: &frame.scope, serial: frame.serial, i18n })
        .map_err(VmError::Runtime)?;
    if st.helpers_any {
        activate_helpers(rt, st).map_err(VmError::Runtime)?;
    }
    Ok(())
}

/// After [`activate`], in a render where a slot body's helpers have settings of their own:
/// tells the runtime when the active frame is such a body, or the frame after one.
#[cold]
#[inline(never)]
fn activate_helpers<R: Runtime>(rt: &mut R, st: &mut VmState<Val<R>>) -> Result<(), Err<R>> {
    let Some(frame) = st.frames.last() else { return Ok(()) };
    let helpers = helpers_of(frame);
    if helpers.is_some() || st.helpers_on {
        st.helpers_on = helpers.is_some();
        rt.i18n_changed(frame.i18n(&st.none), helpers)?;
    }
    Ok(())
}

/// The active frame's i18n settings changed.
fn i18n_changed<R: Runtime>(st: &mut VmState<Val<R>>, rt: &mut R) -> Result<(), Err<R>> {
    let Some(frame) = st.frames.last() else { return Ok(()) };
    let helpers = helpers_of(frame);
    st.helpers_on = helpers.is_some();
    rt.i18n_changed(frame.i18n(&st.none), helpers)
}

/// Runs (or resumes) a render until it finishes or suspends. A suspended render continues
/// only after [`VmState::resume_with`].
pub fn run<R: Runtime>(st: &mut VmState<Val<R>>, rt: &mut R) -> Result<Done<Val<R>>, VmError<Err<R>>> {
    if st.suspended {
        return Err(VmError::Internal("run on a suspended render without resume_with"));
    }
    // Each program the render runs, held once: cloning a frame's `Arc` at every frame switch
    // made each partial call update a count every thread rendering that template shares.
    let mut programs: Vec<Arc<Program>> = Vec::new();
    loop {
        let Some(frame) = st.frames.last_mut() else { return Ok(Done::Finished) };
        let i = if let Some(i) = programs.iter().position(|p| Arc::ptr_eq(p, &frame.program)) {
            i
        } else {
            programs.push(Arc::clone(&frame.program));
            programs.len() - 1
        };
        let program = &programs[i];
        if program.has_surrogate_text() {
            st.out.set_has_surrogates();
        }
        let mut pc = frame.pc as usize;
        let update = std::mem::take(&mut frame.pending_update);
        st.recent = frame.recent;
        activate(rt, st)?;
        if update && let Err(error) = rt.after_macro() {
            unwind_error(st, rt, error)?;
            continue;
        }
        let action = match execute(program, &mut pc, st, rt) {
            Ok(action) => action,
            Err(VmError::Runtime(error)) => {
                unwind_error(st, rt, error)?;
                continue;
            }
            Err(e) => return Err(e),
        };
        match action {
            Action::Partial(call, return_pc, on_return) => {
                if rt.wants_defer() && st.can_defer() && defer_partial(st, rt, return_pc, on_return)? {
                    continue;
                }
                push_partial(st, rt, call, return_pc, on_return)?;
            }
            Action::Push(pushed) => push_frame(st, rt, pushed)?,
            Action::Pop => {
                let Some(done) = st.frames.last() else { return Err(VmError::Internal("pop without a frame")) };
                let on_return = done.on_return;
                let partial = done.kind == Kind::Partial;
                let fill =
                    if done.kind == Kind::Fill { done.extra.as_ref().map(|e| (e.stream, e.start)) } else { None };
                st.pop_frame();
                st.restart_fill();
                if partial {
                    rt.partial_returned();
                }
                match on_return {
                    OnReturn::Nothing => {}
                    OnReturn::Text(text) => {
                        if let Some(caller) = st.frames.last() {
                            st.out.push(caller.program.text(text).ok_or(VmError::Internal("text out of range"))?);
                        }
                    }
                    OnReturn::Update => {
                        if let Some(caller) = st.frames.last_mut() {
                            caller.pending_update = true;
                        }
                    }
                }
                if let Some((stream, start)) = fill {
                    let end = st.out.mark();
                    st.move_to_stream(stream, start, end);
                }
            }
            Action::Suspend(value, at) => {
                if let Some(frame) = st.frames.last_mut() {
                    frame.pc = at;
                    frame.recent = st.recent;
                }
                st.suspended = true;
                return Ok(Done::Suspended(value));
            }
        }
    }
}

/// Room for a frame at nesting `depth` ([`Errors::frame_budget`]); the recursion error if not.
#[inline]
fn check_depth<R: Runtime>(st: &mut VmState<Val<R>>, rt: &mut R, depth: usize) -> Result<bool, VmError<Err<R>>> {
    if depth <= SHALLOW && st.frames.len() < MAX_FRAMES {
        return Ok(true);
    }
    deep(st, rt, depth)
}

#[cold]
#[inline(never)]
fn deep<R: Runtime>(st: &mut VmState<Val<R>>, rt: &mut R, depth: usize) -> Result<bool, VmError<Err<R>>> {
    let too_deep = st.frames.len() >= MAX_FRAMES
        || (depth > SHALLOW && depth > *st.budget.get_or_insert_with(|| rt.frame_budget()));
    if too_deep {
        let error = rt.recursion_error();
        unwind_error(st, rt, error)?;
    }
    Ok(!too_deep)
}

/// An emitted value the render would suspend on: written as a placeholder if the output is
/// final and the runtime defers it (opt-in concurrency; `None`), else given back to suspend on.
/// Out of line, so the instruction loop stays as it was.
#[cold]
#[inline(never)]
fn defer_emitted<R: Runtime>(
    st: &mut VmState<Val<R>>,
    rt: &mut R,
    value: Val<R>,
    esc: Escape,
    stage: Stage,
) -> Result<Option<Val<R>>, VmError<Err<R>>> {
    if !st.can_defer() {
        return Ok(Some(value));
    }
    match rt.defer_value(value, esc, stage, ErrorSite::unpack(st.recent)).map_err(VmError::Runtime)? {
        Ok(placeholder) => {
            st.out.push(&placeholder);
            Ok(None)
        }
        Err(value) => Ok(Some(value)),
    }
}

/// A native partial started as a render of its own (opt-in concurrency): its placeholder
/// is written where its output goes, and the caller continues. False if the runtime runs it
/// in a frame after all.
#[cold]
#[inline(never)]
fn defer_partial<R: Runtime>(
    st: &mut VmState<Val<R>>,
    rt: &mut R,
    return_pc: u32,
    on_return: OnReturn,
) -> Result<bool, VmError<Err<R>>> {
    let placeholder = match rt.defer_partial() {
        Ok(Some(p)) => p,
        Ok(None) => return Ok(false),
        Err(error) => {
            unwind_error(st, rt, error)?;
            return Ok(true);
        }
    };
    let recent = st.recent;
    let Some(caller) = st.frames.last_mut() else { return Err(VmError::Internal("call without a caller")) };
    caller.pc = return_pc;
    caller.recent = recent;
    st.out.push(&placeholder);
    if let OnReturn::Text(text) = on_return {
        let program = Arc::clone(&caller.program);
        st.out.push(program.text(text).ok_or(VmError::Internal("text out of range"))?);
    }
    Ok(true)
}

/// Pushes a native partial's frame: its main function, no i18n settings, no slots.
fn push_partial<R: Runtime>(
    st: &mut VmState<Val<R>>,
    rt: &mut R,
    call: Call<Val<R>>,
    return_pc: u32,
    on_return: OnReturn,
) -> Result<(), VmError<Err<R>>> {
    let recent = st.recent;
    let Some(caller) = st.frames.last_mut() else { return Err(VmError::Internal("call without a caller")) };
    caller.pc = return_pc;
    caller.recent = recent;
    let depth = caller.depth + usize::from(PARTIAL_COST);
    if !check_depth(st, rt, depth)? {
        return Ok(());
    }
    let serial = st.next_serial();
    st.frames.push(Frame {
        program: call.program,
        pc: 0,
        code: call.code,
        scope: call.scope,
        kind: Kind::Partial,
        on_return,
        unwind_base: st.unwind.len(),
        recent: 0,
        fallback: Mark::default(),
        serial,
        pending_update: false,
        depth,
        extra: None,
    });
    rt.partial_entered();
    Ok(())
}

/// The helpers' settings of a fill defined in slot body `frame`: the body's (see
/// [`Extra::helpers`]).
#[cold]
fn fill_helpers<R: Runtime>(frame: &Frame<Val<R>>, rt: &mut R) -> Option<Box<I18n<Val<R>>>> {
    let extra = frame.extra.as_ref()?;
    let helpers = extra.helpers.as_deref().unwrap_or(&extra.i18n);
    Some(Box::new(I18n {
        domain: helpers.domain.clone(),
        context: helpers.context.clone(),
        target_language: helpers.target_language.as_ref().map(|t| rt.clone_value(t)),
    }))
}

/// Pushes a frame for a macro or a slot body.
fn push_frame<R: Runtime>(st: &mut VmState<Val<R>>, rt: &mut R, pushed: Pushed<Val<R>>) -> Result<(), VmError<Err<R>>> {
    let Pushed { call, entry, cost, return_pc, on_return, kind, slots, fill, reset_token } = pushed;
    if reset_token {
        st.recent = 0;
    }
    let recent = st.recent;
    let Some(caller) = st.frames.last_mut() else { return Err(VmError::Internal("call without a caller")) };
    caller.pc = return_pc;
    caller.recent = recent;
    let depth = caller.depth + usize::from(cost);
    if !check_depth(st, rt, depth)? {
        return Ok(());
    }
    let Some(caller) = st.frames.last() else { return Err(VmError::Internal("call without a caller")) };
    let (i18n, stream, helpers) = if let Some((i18n, stream, helpers)) = fill {
        (i18n, stream, helpers)
    } else {
        let i18n = caller.i18n(&st.none);
        let i18n = I18n {
            domain: i18n.domain.clone(),
            context: i18n.context.clone(),
            target_language: i18n.target_language.as_ref().map(|t| rt.clone_value(t)),
        };
        (i18n, None, None)
    };
    let extra = if kind == Kind::Fill || !is_plain(&i18n) || !slots.is_empty() {
        let start = st.out.mark();
        Some(st.extra(i18n, slots, stream, start, helpers))
    } else {
        None
    };
    let serial = st.next_serial();
    st.frames.push(Frame {
        program: call.program,
        pc: entry,
        code: call.code,
        scope: call.scope,
        kind,
        on_return,
        unwind_base: st.unwind.len(),
        recent: 0,
        fallback: Mark::default(),
        serial,
        pending_update: false,
        depth,
        extra,
    });
    Ok(())
}

/// Resumes a suspended render by raising `error` at the instruction it suspended on (Phase
/// 11: the awaited value failed, or the task was cancelled through `throw()`). The error
/// unwinds exactly as if that instruction had raised it: an enclosing `tal:on-error` that
/// catches it renders its fallback and the render continues; otherwise it is returned.
pub fn throw<R: Runtime>(st: &mut VmState<Val<R>>, rt: &mut R, error: Err<R>) -> Result<Done<Val<R>>, VmError<Err<R>>> {
    if !st.suspended {
        return Err(VmError::Internal("throw on a render that is not suspended"));
    }
    st.suspended = false;
    st.resume = None;
    if let Some(frame) = st.frames.last() {
        st.recent = frame.recent;
        activate(rt, st)?;
    }
    unwind_error(st, rt, error)?;
    run(st, rt)
}

/// An error raised in the active frame (D3 item 2): the innermost `tal:on-error` of the frame
/// that catches it gets it, or the frame ends (recording the error, unless it is a slot
/// body) and its caller is tried.
///
/// Catching drops every unwind entry above the handler's own, without restoring anything:
/// Chameleon's generated code has no `try/finally`, so a failed body's `tal:define` and loop
/// names (and i18n settings) keep their values, while cached values, omit tests, switches,
/// attribute marks and translation streams simply go. The output is truncated to the frame's
/// fallback mark.
#[cold]
#[inline(never)]
fn unwind_error<R: Runtime>(st: &mut VmState<Val<R>>, rt: &mut R, mut error: Err<R>) -> Result<(), VmError<Err<R>>> {
    loop {
        let Some(frame) = st.frames.last_mut() else { return Err(VmError::Runtime(error)) };
        let base = frame.unwind_base;
        let handler = st
            .unwind
            .get(base..)
            .and_then(|entries| entries.iter().rposition(|e| matches!(e, Unwind::Try { .. })).map(|i| base + i));
        if let Some(at) = handler
            && rt.is_catchable(&error)
        {
            let Some(&Unwind::Try { handler, name }) = st.unwind.get(at) else {
                return Err(VmError::Internal("handler vanished"));
            };
            // Settings changed inside keep their values (no `finally`): the runtime must see
            // the frame's settings as they are now.
            let changed = st.unwind[at..].iter().any(|e| matches!(e, Unwind::I18n(_)));
            st.place_before_from(at);
            unfinished_names(&mut st.unwind, at, &st.out);
            let abandoned = st.unwind[at..].iter().find_map(|e| match e {
                Unwind::Translate { mark, .. } | Unwind::Name { mark, .. } => Some(*mark),
                _ => None,
            });
            st.unwind.truncate(at);
            // Whatever value of several parts was being written ends here.
            let parts = st.parts.take();
            if let Some(frame) = st.frames.last_mut() {
                frame.pc = handler;
            }
            if changed {
                i18n_changed(st, rt).map_err(VmError::Runtime)?;
            }
            match rt.catch(&error, name, ErrorSite::unpack(st.recent)) {
                Ok(()) if st.fill_catch(abandoned, parts) => {
                    if let Some(frame) = st.frames.last_mut() {
                        frame.recent = st.recent;
                    }
                    return Ok(());
                }
                Ok(()) => {
                    let Some(frame) = st.frames.last_mut() else { return Err(VmError::Internal("no frame")) };
                    // `del __stream[__fallback:]` on the stream of the handler's site: the
                    // translation streams opened inside the body are abandoned, whatever the
                    // register says (it may have been set inside one of them, as a length of
                    // that stream).
                    let cut = abandoned.map_or(frame.fallback, |m| m.min(frame.fallback));
                    st.out.truncate(cut);
                    // A slot body's own output may start past the cut (a call's output was cut).
                    if let Some(extra) = frame.extra.as_mut()
                        && extra.start > cut
                    {
                        extra.start = cut;
                    }
                    frame.recent = st.recent;
                    return Ok(());
                }
                // Raised by the handler itself (outside the protected body): keep unwinding.
                Err(e) => {
                    error = e;
                    continue;
                }
            }
        }
        let kind = frame.kind;
        if kind != Kind::Fill {
            rt.record_error(&error, ErrorSite::unpack(st.recent));
        }
        // The frame's streams are abandoned (a frame that returns has closed its own).
        if let Some(frame) = st.frames.last() {
            st.place_before_from(frame.unwind_base);
        }
        st.pop_frame();
        if kind == Kind::Partial {
            error = rt.partial_failed(error);
        }
        // A call from a slot body failed: as when it returns (see `flush_fill`).
        st.restart_fill();
        let Some(caller) = st.frames.last_mut() else { return Err(VmError::Runtime(error)) };
        caller.pending_update = false;
        st.recent = caller.recent;
        activate(rt, st)?;
    }
}

fn text<E>(program: &Program, r: TextRef) -> Result<&[u8], VmError<E>> {
    program.text(r).ok_or(VmError::Internal("text out of range"))
}

/// Evaluates an expression for an instruction, or returns the action that leaves it (a
/// suspension restarts the instruction; frame calls are only valid where `$call` says).
macro_rules! eval_or_leave {
    ($st:expr, $rt:expr, $pc:expr, $expr:expr, $emitted:expr, $call:expr) => {
        match value_of($st, $rt, $expr, $emitted)? {
            Step::Ready(v) => v,
            Step::Suspend(v) => return Ok(Action::Suspend(v, *$pc as u32)),
            Step::Call(call) => return $call(call),
        }
    };
}

#[allow(clippy::cast_possible_truncation)] // instruction indices fit in u32 (checked at build)
#[allow(clippy::too_many_lines)] // one arm per instruction
#[inline(always)]
#[allow(clippy::inline_always)] // one call site (`run`); measured: see docs/plan.md, Phase 4
fn execute<R: Runtime>(
    program: &Arc<Program>,
    pc: &mut usize,
    st: &mut VmState<Val<R>>,
    rt: &mut R,
) -> Result<Action<Val<R>>, VmError<Err<R>>> {
    let rt_err = VmError::Runtime;
    let no_call = |_| Err(VmError::Internal("frame call where a value is needed"));
    loop {
        let Some(op) = program.ops.get(*pc) else { return Err(VmError::Internal("pc out of range")) };
        match *op {
            Op::Text(r) => st.out.push(text(program, r)?),
            Op::Emit { expr, esc, stage } => {
                let value = match value_of(st, rt, expr, true)? {
                    Step::Ready(v) => v,
                    Step::Suspend(v) => match defer_emitted(st, rt, v, esc, stage)? {
                        None => {
                            *pc += 1;
                            continue;
                        }
                        Some(v) => return Ok(Action::Suspend(v, *pc as u32)),
                    },
                    Step::Call(call) => return Ok(Action::Partial(call, *pc as u32 + 1, OnReturn::Nothing)),
                };
                rt.emit(value, esc, stage, &mut st.out).map_err(rt_err)?;
            }
            Op::EmitIfTrue { expr, esc, stage } => {
                let value = eval_or_leave!(st, rt, pc, expr, false, no_call);
                if rt.truthy(&value).map_err(rt_err)? {
                    rt.emit(value, esc, stage, &mut st.out).map_err(rt_err)?;
                }
            }
            Op::Attr { expr, esc, prefix, suffix } => {
                let value = match value_of(st, rt, expr, true)? {
                    Step::Ready(v) => v,
                    Step::Suspend(v) => return Ok(Action::Suspend(v, *pc as u32)),
                    Step::Call(call) => {
                        st.out.push(text(program, prefix)?);
                        return Ok(Action::Partial(call, *pc as u32 + 1, OnReturn::Text(suffix)));
                    }
                };
                let mark = st.out.mark();
                st.out.push(text(program, prefix)?);
                match rt.emit(value, esc, Stage::Lone, &mut st.out).map_err(rt_err)? {
                    Emitted::Nothing => st.out.truncate(mark),
                    Emitted::Value | Emitted::True => st.out.push(text(program, suffix)?),
                }
            }
            Op::Content { expr, esc, end } => {
                let value =
                    eval_or_leave!(st, rt, pc, expr, true, |call| Ok(Action::Partial(call, end, OnReturn::Nothing)));
                if !rt.is_default(&value) {
                    rt.emit(value, esc, Stage::Content, &mut st.out).map_err(rt_err)?;
                    *pc = end as usize;
                    continue;
                }
            }
            Op::JumpIfNot { expr, target } => {
                let value = eval_or_leave!(st, rt, pc, expr, false, no_call);
                if !rt.truthy(&value).map_err(rt_err)? {
                    *pc = target as usize;
                    continue;
                }
            }
            Op::LoopStart { info, expr, next } => {
                let li = program.tables.loops.get(info as usize).ok_or(VmError::Internal("bad loop"))?;
                // Chameleon backs the names up before evaluating the iterable (a global loop
                // takes no backups), and binds them to None after registering the loop with
                // `repeat`.
                let mut backups = Vec::new();
                if li.visibility == Visibility::Local {
                    backups.reserve(li.names.len());
                    for &name in &li.names {
                        backups.push(rt.lookup(name).map_err(rt_err)?);
                    }
                }
                let iterable = eval_or_leave!(st, rt, pc, expr, false, no_call);
                let (iterator, index) = rt.repeat(&li.names, iterable).map_err(rt_err)?;
                for &name in &li.names {
                    rt.set_null(name).map_err(rt_err)?;
                }
                let item = rt.next(&iterator).map_err(rt_err)?;
                st.unwind.push(Unwind::Loop { iterator, index, backups });
                *pc = bind_or_exit(st, rt, li, item, *pc + 1, next as usize + 1)?;
                continue;
            }
            Op::LoopNext { info, body } => {
                let li = program.tables.loops.get(info as usize).ok_or(VmError::Internal("bad loop"))?;
                let Some(Unwind::Loop { iterator, index, .. }) = st.unwind.last() else {
                    return Err(VmError::Internal("loop_next without its loop"));
                };
                // Chameleon runs `index -= 1; if index > 0: __append(whitespace)` and then
                // fetches the next item. The countdown is computed first (it is what can raise
                // first), but nothing is changed until the fetch has succeeded.
                let countdown = match index {
                    Countdown::Int(n) => Countdown::Int(*n - 1),
                    Countdown::Value(v) => Countdown::Value(rt.countdown(v).map_err(rt_err)?),
                };
                let item = rt.next(iterator).map_err(rt_err)?;
                let more = match countdown {
                    Countdown::Int(n) => {
                        set_index(st, Countdown::Int(n));
                        n > 0
                    }
                    Countdown::Value((v, more)) => {
                        set_index(st, Countdown::Value(v));
                        more
                    }
                };
                if more {
                    st.out.push(text(program, li.whitespace)?);
                }
                *pc = bind_or_exit(st, rt, li, item, body as usize, *pc + 1)?;
                continue;
            }
            Op::Define { info, expr } => {
                let d = program.tables.defines.get(info as usize).ok_or(VmError::Internal("bad define"))?;
                // `_enter_assignment` (local only) happens before the expression is evaluated.
                let mut backups = Vec::new();
                if d.visibility == Visibility::Local {
                    backups.reserve(d.names.len());
                    for &name in &d.names {
                        backups.push(rt.lookup(name).map_err(rt_err)?);
                    }
                }
                let value = eval_or_leave!(st, rt, pc, expr, false, no_call);
                rt.define(&d.names, value, d.visibility).map_err(rt_err)?;
                if d.visibility == Visibility::Local {
                    st.unwind.push(Unwind::Define { backups });
                }
            }
            Op::Undefine { info } => {
                let d = program.tables.defines.get(info as usize).ok_or(VmError::Internal("bad define"))?;
                let Some(Unwind::Define { backups }) = st.unwind.pop() else {
                    return Err(VmError::Internal("undefine without its define"));
                };
                for (&name, backup) in d.names.iter().zip(backups) {
                    rt.restore(name, backup).map_err(rt_err)?;
                }
            }
            Op::OmitTest { expr } => {
                let value = eval_or_leave!(st, rt, pc, expr, false, no_call);
                let omit = rt.truthy(&value).map_err(rt_err)?;
                st.unwind.push(Unwind::Omit(omit));
            }
            Op::JumpIfOmitted { target } => {
                let Some(Unwind::Omit(omit)) = st.unwind.last() else {
                    return Err(VmError::Internal("omit check without its test"));
                };
                if *omit {
                    *pc = target as usize;
                    continue;
                }
            }
            Op::PopOmit => {
                let Some(Unwind::Omit(_)) = st.unwind.pop() else {
                    return Err(VmError::Internal("pop_omit without its test"));
                };
            }
            Op::CacheValue { expr } => {
                let value = eval_or_leave!(st, rt, pc, expr, false, no_call);
                st.unwind.push(Unwind::Value(value));
            }
            Op::PopValues { n } => {
                for _ in 0..n {
                    let Some(Unwind::Value(_)) = st.unwind.pop() else {
                        return Err(VmError::Internal("pop_values without its values"));
                    };
                }
            }
            Op::AttrStatic { info, cached } => {
                let a = program.tables.attrs.get(info as usize).ok_or(VmError::Internal("bad attribute"))?;
                if !filtered(program, st, rt, program.text_str(a.name), a.filters, cached)? {
                    st.out.push(text(program, a.prefix)?);
                }
            }
            Op::AttrValue { info, kind, cached } => {
                let a = program.tables.attrs.get(info as usize).ok_or(VmError::Internal("bad attribute"))?;
                let (expr, emitted) = match kind {
                    AttrKind::Subst { expr, .. } => (expr, a.filters.1 == 0),
                    AttrKind::Boolean { expr } => (expr, false),
                };
                let value = match value_of(st, rt, expr, emitted)? {
                    Step::Ready(v) => v,
                    Step::Suspend(v) => return Ok(Action::Suspend(v, *pc as u32)),
                    Step::Call(call) => {
                        st.out.push(text(program, a.prefix)?);
                        return Ok(Action::Partial(call, *pc as u32 + 1, OnReturn::Text(a.suffix)));
                    }
                };
                let mark = st.out.mark();
                st.out.push(text(program, a.prefix)?);
                let written = if rt.is_default(&value) {
                    // `__quote` / `emit_bool`: the marker means the static value, as written.
                    match a.default {
                        Some(d) => {
                            st.out.push(text(program, d)?);
                            true
                        }
                        None => false,
                    }
                } else {
                    match kind {
                        AttrKind::Subst { esc, .. } => {
                            rt.emit(value, esc, Stage::Lone, &mut st.out).map_err(rt_err)? == Emitted::Value
                        }
                        AttrKind::Boolean { .. } => {
                            let on = rt.truthy(&value).map_err(rt_err)?;
                            if on {
                                st.out.push(text(program, a.name)?);
                            }
                            on
                        }
                    }
                };
                if !written || filtered(program, st, rt, program.text_str(a.name), a.filters, cached)? {
                    st.out.truncate(mark);
                } else {
                    st.out.push(text(program, a.suffix)?);
                }
            }
            Op::AttrLone { expr, esc, info, boolean, cached } => {
                let a = program.tables.attrs.get(info as usize).ok_or(VmError::Internal("bad attribute"))?;
                let value = eval_or_leave!(st, rt, pc, expr, false, no_call);
                if boolean && !rt.truthy(&value).map_err(rt_err)? {
                    // A false part is `None`: the attribute is dropped.
                } else {
                    let start = st.out.mark();
                    st.out.push(text(program, a.prefix)?);
                    let value_at = st.out.mark();
                    let stage = if boolean { Stage::Bool } else { Stage::Lone };
                    match rt.emit(value, esc, stage, &mut st.out).map_err(rt_err)? {
                        Emitted::Nothing => st.out.truncate(start),
                        Emitted::Value => finish_attr(program, st, rt, a, start, value_at, Finish::Keep, cached)?,
                        Emitted::True => finish_attr(program, st, rt, a, start, value_at, Finish::Name, cached)?,
                    }
                }
            }
            Op::AttrBegin { info } => {
                let a = program.tables.attrs.get(info as usize).ok_or(VmError::Internal("bad attribute"))?;
                let start = st.out.mark();
                st.out.push(text(program, a.prefix)?);
                let value = st.out.mark();
                st.unwind.push(Unwind::Attr { start, value });
            }
            Op::AttrEnd { info, boolean, cached } => {
                let a = program.tables.attrs.get(info as usize).ok_or(VmError::Internal("bad attribute"))?;
                let Some(Unwind::Attr { start, value }) = st.unwind.pop() else {
                    return Err(VmError::Internal("attr_end without its attribute"));
                };
                let finish = if boolean { Finish::Mixed } else { Finish::Keep };
                finish_attr(program, st, rt, a, start, value, finish, cached)?;
            }
            Op::AttrDict { info, index, cached } => {
                let d = program.tables.dicts.get(info as usize).ok_or(VmError::Internal("bad dictionary"))?;
                let exclude: Vec<&str> = d.exclude.iter().map(|&r| program.text_str(r)).collect();
                let bool_names: Vec<&str> = d.bool_names.iter().map(|&r| program.text_str(r)).collect();
                let dict = cached_value(&st.unwind, cached, index)?;
                rt.attr_dict(dict, &exclude, &bool_names, &mut st.out).map_err(rt_err)?;
            }
            Op::Switch { expr } => {
                let value = eval_or_leave!(st, rt, pc, expr, false, no_call);
                st.unwind.push(Unwind::Switch(Some(value)));
            }
            Op::PopSwitch => {
                let Some(Unwind::Switch(_)) = st.unwind.pop() else {
                    return Err(VmError::Internal("pop_switch without its switch"));
                };
            }
            Op::CaseTest { expr, no_match, matched } => {
                let base = st.frame_base();
                let at = st.unwind[base..]
                    .iter()
                    .rposition(|e| matches!(e, Unwind::Switch(_)))
                    .map(|i| base + i)
                    .ok_or(VmError::Internal("case without its switch"))?;
                if matches!(st.unwind.get(at), Some(Unwind::Switch(None))) {
                    *pc = no_match as usize;
                    continue;
                }
                let value = eval_or_leave!(st, rt, pc, expr, false, no_call);
                let Some(Unwind::Switch(Some(switch))) = st.unwind.get(at) else {
                    return Err(VmError::Internal("case without its switch"));
                };
                let equal = rt.case_compare(&value, Some(switch)).map_err(rt_err)?;
                if rt.is_false(&equal) {
                    // `__condition is False`: try `value == default`.
                } else if rt.truthy(&equal).map_err(rt_err)? {
                    st.unwind[at] = Unwind::Switch(None);
                    *pc = matched as usize;
                    continue;
                } else {
                    *pc = no_match as usize;
                    continue;
                }
            }
            Op::CaseDefault { expr, no_match } => {
                let value = eval_or_leave!(st, rt, pc, expr, false, no_call);
                let equal = rt.case_compare(&value, None).map_err(rt_err)?;
                if rt.truthy(&equal).map_err(rt_err)? {
                    let base = st.frame_base();
                    if let Some(i) = st.unwind[base..].iter().rposition(|e| matches!(e, Unwind::Switch(_))) {
                        st.unwind[base + i] = Unwind::Switch(None);
                    }
                } else {
                    *pc = no_match as usize;
                    continue;
                }
            }
            Op::Try { handler, name } => {
                let mark = st.out.mark();
                if let Some(frame) = st.frames.last_mut() {
                    frame.fallback = mark;
                }
                st.unwind.push(Unwind::Try { handler, name });
            }
            Op::EndTry { end } => {
                let Some(Unwind::Try { .. }) = st.unwind.pop() else {
                    return Err(VmError::Internal("end_try without its try"));
                };
                *pc = end as usize;
                continue;
            }
            Op::Exec { expr } => {
                let _ = eval_or_leave!(st, rt, pc, expr, false, no_call);
            }
            Op::DefineText { .. }
            | Op::ContentTranslate { .. }
            | Op::PopSlots { .. }
            | Op::DefineSlot { .. }
            | Op::FillSlot { .. }
            | Op::UseMacro { .. }
            | Op::CallMacro { .. }
            | Op::I18nSet { .. }
            | Op::Target { .. }
            | Op::I18nRestore
            | Op::TranslateBegin { .. }
            | Op::TranslateEnd { .. }
            | Op::NameBegin { .. }
            | Op::NameEnd { .. }
            | Op::TrValue { .. }
            | Op::PartsBegin
            | Op::PartsEnd
            | Op::CaptureBegin
            | Op::CaptureEnd { .. }
            | Op::AttrTranslate { .. }
            | Op::InterpTranslate { .. } => match execute_rare(program, op, *pc, st, rt)? {
                Flow::Next => {}
                Flow::Jump(to) => {
                    *pc = to;
                    continue;
                }
                Flow::Leave(action) => return Ok(action),
            },
            Op::End => {
                if st.unwind.len() != st.frame_base() {
                    return Err(VmError::Internal("frame ended with entries on the unwind stack"));
                }
                return Ok(Action::Pop);
            }
        }
        *pc += 1;
    }
}

/// Stores an `i18n:name`'s value in its translation: the innermost open one of this program
/// with that index (a slot body's names belong to its defining function's).
fn set_name<V>(unwind: &mut [Unwind<V>], program: &Arc<Program>, n: &NameInfo, value: NameValue) {
    let entry = unwind.iter_mut().rev().find_map(|e| match e {
        Unwind::Translate { translation: t, .. } if Arc::ptr_eq(&t.program, program) && t.info == n.translate => {
            Some(&mut t.names)
        }
        _ => None,
    });
    if let Some(slot) = entry.and_then(|names| names.get_mut(n.index as usize)) {
        *slot = value;
    }
}

/// An error caught above unfinished `i18n:name` streams (the entries from `from` up): each
/// name's mapping value becomes its stream, a list of what was appended to it (its text up
/// to the next stream opened inside it).
fn unfinished_names<V>(unwind: &mut [Unwind<V>], from: usize, out: &Output) {
    let Some(dropped) = unwind.get(from..) else { return };
    let marks: Vec<Mark> = dropped
        .iter()
        .filter_map(|e| match e {
            Unwind::Translate { mark, .. } | Unwind::Name { mark, .. } => Some(*mark),
            _ => None,
        })
        .collect();
    let mut names = Vec::new();
    for (i, e) in dropped.iter().filter(|e| matches!(e, Unwind::Translate { .. } | Unwind::Name { .. })).enumerate() {
        if let Unwind::Name { mark, program, info, .. } = e {
            let end = marks.get(i + 1).copied().unwrap_or_else(|| out.mark());
            let text = out.as_bytes().get(mark.position()..end.position()).unwrap_or_default();
            let pieces = Wtf8Buf::from_bytes(text.to_vec()).filter(|t| !t.is_empty()).into_iter().collect();
            names.push((Arc::clone(program), *info, pieces));
        }
    }
    let (kept, _) = unwind.split_at_mut(from);
    for (program, info, pieces) in names {
        if let Some(n) = program.tables.names.get(info as usize) {
            set_name(kept, &program, n, NameValue::Pieces(pieces));
        }
    }
}

/// What [`execute_rare`] did.
enum Flow<V> {
    /// Go on with the next instruction.
    Next,
    /// Go on at this instruction. (`pc` is passed by value: taking its address would keep
    /// it in memory in [`execute`]'s loop, measured +10 ns per listing item.)
    Jump(usize),
    Leave(Action<V>),
}

/// [`eval_or_leave`] for [`execute_rare`] (`pc` by value).
macro_rules! eval_or_leave_rare {
    ($st:expr, $rt:expr, $pc:expr, $expr:expr, $emitted:expr, $call:expr) => {
        match value_of($st, $rt, $expr, $emitted)? {
            Step::Ready(v) => v,
            Step::Suspend(v) => return Ok(Flow::Leave(Action::Suspend(v, $pc as u32))),
            Step::Call(call) => return $call(call),
        }
    };
}

/// The instructions of macros, slots and translation, out of [`execute`]'s loop: with them
/// inline, the loop's code got worse for every instruction (measured: +6 ns per listing item
/// on the toy runtime).
#[cold]
#[inline(never)]
#[allow(clippy::cast_possible_truncation)] // instruction indices fit in u32 (checked at build)
#[allow(clippy::too_many_lines)] // one arm per instruction
fn execute_rare<R: Runtime>(
    program: &Arc<Program>,
    op: &Op,
    pc: usize,
    st: &mut VmState<Val<R>>,
    rt: &mut R,
) -> Result<Flow<Val<R>>, VmError<Err<R>>> {
    let rt_err = VmError::Runtime;
    let no_call = |_| Err(VmError::Internal("frame call where a value is needed"));
    match *op {
        Op::DefineText { info, text: t } => {
            let d = program.tables.defines.get(info as usize).ok_or(VmError::Internal("bad define"))?;
            let mut backups = Vec::with_capacity(d.names.len());
            for &name in &d.names {
                backups.push(rt.lookup(name).map_err(rt_err)?);
            }
            let value = rt.text_value(Some(program.text_str(t)));
            rt.define(&d.names, value, Visibility::Local).map_err(rt_err)?;
            st.unwind.push(Unwind::Define { backups });
        }
        Op::ContentTranslate { expr, esc, end } => {
            let value = eval_or_leave_rare!(st, rt, pc, expr, false, no_call);
            if !rt.is_default(&value) {
                let value = rt.translate(Translation::Content(value)).map_err(rt_err)?;
                rt.emit(value, esc, Stage::Content, &mut st.out).map_err(rt_err)?;
                return Ok(Flow::Jump(end as usize));
            }
        }
        Op::PopSlots { info } => {
            let names = program.tables.slot_sets.get(info as usize).ok_or(VmError::Internal("bad slot set"))?;
            let slots = names.iter().map(|n| rt.pop_slot(n)).collect();
            if let Some(extra) = st.active_extra() {
                extra.slots = slots;
            }
        }
        Op::DefineSlot { slot, end } => {
            let filled = st.frames.last().is_some_and(|f| matches!(f.slots().get(slot as usize), Some(Some(_))));
            if filled {
                // A slot called from a slot body: like a macro call (see `flush_fill`).
                st.flush_fill();
            }
            let Some(frame) = st.frames.last() else { return Err(VmError::Internal("no frame")) };
            if let Some(Some(fill)) = frame.slots().get(slot as usize) {
                let fill = rt.slot_call(fill, &mut st.out).map_err(rt_err)?;
                let Some(fill) = fill else {
                    st.restart_fill();
                    return Ok(Flow::Jump(end as usize));
                };
                let scope = if fill.program.shares_scope(fill.entry)
                    && rt.may_share_scope()
                    && let Some(frame) = st.frames.last()
                {
                    rt.clone_value(&frame.scope)
                } else {
                    rt.copy_scope().map_err(rt_err)?
                };
                let call = Call { program: fill.program, code: fill.code, scope };
                return Ok(Flow::Leave(Action::Push(Pushed {
                    call,
                    entry: fill.entry,
                    cost: 1,
                    return_pc: end,
                    on_return: OnReturn::Nothing,
                    kind: Kind::Fill,
                    slots: fill.slots,
                    fill: Some((fill.i18n, fill.stream, fill.helpers)),
                    reset_token: false,
                })));
            }
        }
        Op::FillSlot { info } => {
            let f = program.tables.fills.get(info as usize).ok_or(VmError::Internal("bad fill"))?;
            let stream = st.current_stream();
            let Some(frame) = st.frames.last() else { return Err(VmError::Internal("no frame")) };
            let i18n = frame.i18n(&st.none);
            let fill = Fill {
                program: Arc::clone(program),
                code: rt.clone_value(&frame.code),
                entry: f.entry,
                i18n: I18n {
                    domain: i18n.domain.clone(),
                    context: i18n.context.clone(),
                    target_language: i18n.target_language.as_ref().map(|t| rt.clone_value(t)),
                },
                slots: frame.slots().iter().map(|s| s.as_ref().map(|v| rt.clone_value(v))).collect(),
                stream,
                // A fill defined in a slot body belongs to the same render function as the body
                // (also one a foreign macro called: `VmState::with_helpers`).
                helpers: if frame.kind == Kind::Fill || helpers_of(frame).is_some() {
                    fill_helpers(frame, rt)
                } else {
                    None
                },
            };
            rt.fill_slot(&f.name, fill, f.extend).map_err(rt_err)?;
        }
        Op::UseMacro { expr } => {
            let value = eval_or_leave_rare!(st, rt, pc, expr, false, no_call);
            st.flush_fill();
            match rt.macro_call(value, &mut st.out).map_err(rt_err)? {
                MacroStep::Frame { call, entry, cost } => {
                    let pushed = Pushed::call(call, entry, cost, pc as u32 + 1, OnReturn::Update);
                    return Ok(Flow::Leave(Action::Push(pushed)));
                }
                MacroStep::Done => {
                    st.restart_fill();
                    rt.after_macro().map_err(rt_err)?;
                }
            }
        }
        Op::CallMacro { entry } => {
            let Some(frame) = st.frames.last() else { return Err(VmError::Internal("no frame")) };
            let code = rt.clone_value(&frame.code);
            let scope = if program.shares_scope(entry) && rt.may_share_scope() {
                rt.clone_value(&frame.scope)
            } else {
                rt.copy_scope().map_err(rt_err)?
            };
            let call = Call { program: Arc::clone(program), code, scope };
            let mut pushed = Pushed::call(call, entry, 1, pc as u32 + 1, OnReturn::Update);
            pushed.reset_token = true;
            st.flush_fill();
            return Ok(Flow::Leave(Action::Push(pushed)));
        }
        Op::I18nSet { key, value } => {
            let name = program.tables.strings.get(value as usize).ok_or(VmError::Internal("bad i18n name"))?;
            if st.frames.last().is_some_and(|f| f.kind == Kind::Fill) {
                st.keep_helpers(rt);
            }
            let Some(extra) = st.active_extra() else { return Err(VmError::Internal("no frame")) };
            let i18n = &mut extra.i18n;
            let backup = match key {
                I18nKey::Domain => Backup::Domain(i18n.domain.replace(Arc::clone(name))),
                I18nKey::Context => Backup::Context(i18n.context.replace(Arc::clone(name))),
            };
            i18n_changed(st, rt).map_err(rt_err)?;
            st.unwind.push(Unwind::I18n(backup));
        }
        Op::Target { expr } => {
            let value = eval_or_leave_rare!(st, rt, pc, expr, false, no_call);
            if st.frames.last().is_some_and(|f| f.kind == Kind::Fill) {
                st.keep_helpers(rt);
            }
            let Some(extra) = st.active_extra() else { return Err(VmError::Internal("no frame")) };
            let i18n = &mut extra.i18n;
            let backup = i18n.target_language.replace(value);
            i18n_changed(st, rt).map_err(rt_err)?;
            st.unwind.push(Unwind::I18n(Backup::Target(backup)));
        }
        Op::I18nRestore => {
            let Some(Unwind::I18n(backup)) = st.unwind.pop() else {
                return Err(VmError::Internal("i18n_restore without its setting"));
            };
            let Some(extra) = st.active_extra() else { return Err(VmError::Internal("no frame")) };
            let i18n = &mut extra.i18n;
            match backup {
                Backup::Domain(d) => i18n.domain = d,
                Backup::Context(c) => i18n.context = c,
                Backup::Target(t) => i18n.target_language = t,
            }
            i18n_changed(st, rt).map_err(rt_err)?;
        }
        Op::TranslateBegin { info } => {
            let t = program.tables.translates.get(info as usize).ok_or(VmError::Internal("bad translation"))?;
            let id = st.next_serial();
            st.unwind.push(Unwind::Translate {
                mark: st.out.mark(),
                id,
                translation: Box::new(OpenTranslation {
                    program: Arc::clone(program),
                    info,
                    names: vec![NameValue::Text(Wtf8Buf::default()); t.names.len()],
                    before: Vec::new(),
                }),
            });
        }
        Op::TranslateEnd { info } => {
            let t = program.tables.translates.get(info as usize).ok_or(VmError::Internal("bad translation"))?;
            let Some(Unwind::Translate { mark, mut translation, .. }) = st.unwind.pop() else {
                return Err(VmError::Internal("translate_end without its translation"));
            };
            let content = st.take_text(mark)?;
            // Slot body output that belongs before the translation's result.
            st.out.push(&std::mem::take(&mut translation.before));
            let default = content.normalize_space();
            let msgid = match t.msgid {
                Some(m) => Msgid::Source(program.text_str(m)),
                // `if msgid:` around the call.
                None if default.is_empty() => return Ok(Flow::Next),
                None => Msgid::Stream(&default),
            };
            let mapping = (!t.names.is_empty())
                .then(|| t.names.iter().map(|n| program.text_str(*n)).zip(translation.names).collect::<Vec<_>>());
            let value = rt.translate(Translation::Block { msgid, mapping, default: &default }).map_err(rt_err)?;
            rt.emit(value, Escape::Structure, Stage::Append, &mut st.out).map_err(rt_err)?;
        }
        Op::NameBegin { info } => {
            let id = st.next_serial();
            st.unwind.push(Unwind::Name { mark: st.out.mark(), id, program: Arc::clone(program), info, before: None });
        }
        Op::NameEnd { info } => {
            let n = program.tables.names.get(info as usize).ok_or(VmError::Internal("bad name"))?;
            let Some(Unwind::Name { mark, before, .. }) = st.unwind.pop() else {
                return Err(VmError::Internal("name_end without its name"));
            };
            let content = st.take_text(mark)?;
            if let Some(before) = before {
                st.out.push(&before.0);
            }
            set_name(&mut st.unwind, program, n, NameValue::Text(content));
            st.out.push(b"${");
            st.out.push(text(program, n.name)?);
            st.out.push(b"}");
        }
        Op::TrValue { info } => {
            let v = program.tables.tr_values.get(info as usize).ok_or(VmError::Internal("bad value"))?;
            let value = match *v {
                TrValue::Static(t) => rt.text_value(Some(program.text_str(t))),
                TrValue::Subst { expr, esc, default } => {
                    let value = eval_or_leave_rare!(st, rt, pc, expr, false, no_call);
                    let default = default.map(|d| program.text_str(d));
                    rt.quote_value(value, esc, default).map_err(rt_err)?
                }
                TrValue::Boolean { expr, name, default } => {
                    let value = eval_or_leave_rare!(st, rt, pc, expr, false, no_call);
                    if rt.is_default(&value) {
                        rt.text_value(default.map(|d| program.text_str(d)))
                    } else if rt.truthy(&value).map_err(rt_err)? {
                        rt.text_value(Some(program.text_str(name)))
                    } else {
                        rt.text_value(None)
                    }
                }
                TrValue::Lone { expr, esc, boolean } => {
                    let value = eval_or_leave_rare!(st, rt, pc, expr, false, no_call);
                    // `literal_false=False` (a boolean attribute): a false value, and a
                    // false result, are `None`; a true result becomes the name (`Replace`).
                    let boolean_false = |rt: &mut R, v: &Val<R>| -> Result<bool, VmError<Err<R>>> {
                        Ok(boolean.is_some() && !rt.truthy(v).map_err(VmError::Runtime)?)
                    };
                    if boolean_false(rt, &value)? {
                        rt.text_value(None)
                    } else {
                        let quoted = rt.quote_value(value, esc, None).map_err(rt_err)?;
                        if boolean_false(rt, &quoted)? {
                            rt.text_value(None)
                        } else {
                            let converted = rt.convert_value(quoted).map_err(rt_err)?;
                            match boolean {
                                Some(name) if rt.truthy(&converted).map_err(rt_err)? => {
                                    rt.text_value(Some(program.text_str(name)))
                                }
                                _ => converted,
                            }
                        }
                    }
                }
                TrValue::Part { expr, esc } => {
                    let value = eval_or_leave_rare!(st, rt, pc, expr, false, no_call);
                    rt.quote_value(value, esc, None).map_err(rt_err)?
                }
            };
            st.unwind.push(Unwind::Value(value));
        }
        Op::PartsBegin => {
            let serial = st.frames.last().map_or(0, |f| f.serial);
            st.parts = Some((serial, st.out.mark()));
        }
        Op::PartsEnd => st.parts = None,
        Op::CaptureBegin => st.unwind.push(Unwind::Capture(st.out.mark())),
        Op::CaptureEnd { boolean } => {
            let Some(Unwind::Capture(mark)) = st.unwind.pop() else {
                return Err(VmError::Internal("capture_end without its capture"));
            };
            let content = st.take_text(mark)?;
            let value = match boolean {
                // `Replace`: a true (non-empty) result is the attribute's name.
                Some(name) if !content.is_empty() => rt.text_value(Some(program.text_str(name))),
                Some(_) => rt.text_value(None),
                None => rt.stream_value(&content),
            };
            st.unwind.push(Unwind::Value(value));
        }
        Op::AttrTranslate { info, cached } => {
            let t = program.tables.attr_translates.get(info as usize).ok_or(VmError::Internal("bad attribute"))?;
            let a = program.tables.attrs.get(t.attr as usize).ok_or(VmError::Internal("bad attribute"))?;
            let Some(Unwind::Value(value)) = st.unwind.pop() else {
                return Err(VmError::Internal("attr_translate without its value"));
            };
            let msgid = t.msgid.map(|m| program.text_str(m));
            let value = rt.translate(Translation::Attr { msgid, value }).map_err(rt_err)?;
            write_attr(program, st, rt, a, value, Stage::Format, cached)?;
        }
        Op::InterpTranslate { info, cached } => {
            let t = program.tables.interp_translates.get(info as usize).ok_or(VmError::Internal("bad translation"))?;
            let at = st.unwind.len().checked_sub(t.keys.len()).ok_or(VmError::Internal("missing values"))?;
            let mut values = Vec::with_capacity(t.keys.len());
            for entry in st.unwind.drain(at..) {
                let Unwind::Value(v) = entry else { return Err(VmError::Internal("missing value")) };
                values.push(v);
            }
            let mapping =
                t.mapping.then(|| t.keys.iter().map(|k| program.text_str(*k)).zip(values).collect::<Vec<_>>());
            let msgid = program.text_str(t.msgid);
            let value = rt.translate(Translation::Interp { msgid, mapping }).map_err(rt_err)?;
            match t.attr {
                Some(attr) => {
                    let a = program.tables.attrs.get(attr as usize).ok_or(VmError::Internal("bad attribute"))?;
                    let value = rt.convert_value(value).map_err(rt_err)?;
                    write_attr(program, st, rt, a, value, Stage::Format, cached)?;
                }
                None => {
                    rt.emit(value, Escape::Structure, Stage::Content, &mut st.out).map_err(rt_err)?;
                }
            }
        }
        _ => return Err(VmError::Internal("not a rare instruction")),
    }
    Ok(Flow::Next)
}

/// A translated attribute's value: `if value is not None [and name not in chain(...)]:
/// __append(prefix + '%s' % value + suffix)`.
fn write_attr<R: Runtime>(
    program: &Program,
    st: &mut VmState<Val<R>>,
    rt: &mut R,
    a: &AttrInfo,
    value: Val<R>,
    stage: Stage,
    cached: u32,
) -> Result<(), VmError<Err<R>>> {
    let mark = st.out.mark();
    st.out.push(text(program, a.prefix)?);
    if rt.emit(value, Escape::Structure, stage, &mut st.out).map_err(VmError::Runtime)? == Emitted::Nothing
        || filtered(program, st, rt, program.text_str(a.name), a.filters, cached)?
    {
        st.out.truncate(mark);
    } else {
        st.out.push(text(program, a.suffix)?);
    }
    Ok(())
}

/// How [`finish_attr`] treats a written attribute value.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Finish {
    /// Keep the value as written.
    Keep,
    /// The value is true: write the attribute's name instead (a boolean attribute).
    Name,
    /// A boolean attribute's mixed interpolation (always a `str`): empty drops the attribute,
    /// anything else becomes the name.
    Mixed,
}

/// A boolean or filtered attribute whose value has been written after `value`: dropped if a
/// filter has the name, otherwise finished as `finish` says.
#[allow(clippy::too_many_arguments)]
fn finish_attr<R: Runtime>(
    program: &Program,
    st: &mut VmState<Val<R>>,
    rt: &mut R,
    a: &AttrInfo,
    start: Mark,
    value: Mark,
    finish: Finish,
    cached: u32,
) -> Result<(), VmError<Err<R>>> {
    // `if __attr is not None and name not in chain(...)`: the filter is only consulted for a
    // value that is not `None`. The boolean engine (`literal_false=False`) turns a false
    // *result* into `None`, which drops the attribute.
    if finish == Finish::Mixed && st.out.mark() == value {
        st.out.truncate(start);
        return Ok(());
    }
    if filtered(program, st, rt, program.text_str(a.name), a.filters, cached)? {
        st.out.truncate(start);
        return Ok(());
    }
    if finish != Finish::Keep {
        // `Replace`: a true value becomes the attribute's name.
        st.out.truncate(value);
        st.out.push(text(program, a.name)?);
    }
    st.out.push(text(program, a.suffix)?);
    Ok(())
}

/// The `index`th of the `cached` start-tag dictionaries (the top of the unwind stack).
fn cached_value<V, E>(unwind: &[Unwind<V>], cached: u32, index: u32) -> Result<&V, VmError<E>> {
    let at = unwind.len().checked_sub(cached as usize).ok_or(VmError::Internal("missing cached values"))?;
    match unwind.get(at + index as usize) {
        Some(Unwind::Value(v)) => Ok(v),
        _ => Err(VmError::Internal("missing cached value")),
    }
}

/// `name in chain(*dicts)` for an attribute's filters.
fn filtered<R: Runtime>(
    program: &Program,
    st: &VmState<Val<R>>,
    rt: &mut R,
    name: &str,
    filters: (u32, u32),
    cached: u32,
) -> Result<bool, VmError<Err<R>>> {
    let (start, len) = (filters.0 as usize, filters.1 as usize);
    let list = program.tables.filters.get(start..start + len).ok_or(VmError::Internal("bad filters"))?;
    for &k in list {
        if rt.yields_name(cached_value(&st.unwind, cached, k)?, name).map_err(VmError::Runtime)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn set_index<V>(st: &mut VmState<V>, new: Countdown<V>) {
    if let Some(Unwind::Loop { index, .. }) = st.unwind.last_mut() {
        *index = new;
    }
}

/// A resumed value, or a fresh evaluation. Inlined into every instruction that evaluates:
/// an out-of-line call per expression measurably slows the listing benchmark.
#[inline(always)]
#[allow(clippy::inline_always)] // measured: see docs/plan.md, Phase 4
fn value_of<R: Runtime>(
    st: &mut VmState<Val<R>>,
    rt: &mut R,
    expr: ExprId,
    emitted: bool,
) -> Result<Step<Val<R>>, VmError<Err<R>>> {
    if let Some(v) = st.resume.take() {
        return Ok(Step::Ready(v));
    }
    // The error token's bookkeeping: one store (see [`ErrorSite`]).
    st.recent = (st.recent << 32) | u64::from(expr.0.wrapping_add(1));
    rt.eval(expr, emitted).map_err(VmError::Runtime)
}

/// Binds a fetched item and returns `body`, or (no item) ends the loop, restoring the names
/// in order, and returns `exit`. A global loop has nothing to restore: Chameleon's restore
/// code refers to backups it never made, a `NameError` for the first name.
fn bind_or_exit<R: Runtime>(
    st: &mut VmState<Val<R>>,
    rt: &mut R,
    li: &LoopInfo,
    item: Option<Val<R>>,
    body: usize,
    exit: usize,
) -> Result<usize, VmError<Err<R>>> {
    if let Some(item) = item {
        rt.assign(&li.names, item, li.visibility).map_err(VmError::Runtime)?;
        return Ok(body);
    }
    let Some(Unwind::Loop { backups, .. }) = st.unwind.pop() else {
        return Err(VmError::Internal("loop exit without its loop"));
    };
    if li.visibility == Visibility::Global {
        let first = li.names.first().copied().ok_or(VmError::Internal("loop without names"))?;
        return Err(VmError::Runtime(rt.missing_backup(first)));
    }
    for (&name, backup) in li.names.iter().zip(backups) {
        rt.restore(name, backup).map_err(VmError::Runtime)?;
    }
    Ok(exit)
}

#[cfg(test)]
mod size_tests {
    /// Unwind entries are moved on every push: kept at 48 bytes (a slot body's kept-aside
    /// output is boxed for that).
    #[test]
    fn unwind_entries_stay_small() {
        assert_eq!(std::mem::size_of::<super::Unwind<u64>>(), 48);
    }
}
