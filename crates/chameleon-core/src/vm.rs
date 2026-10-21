// The semantics below follow Chameleon's src/chameleon/compiler.py (`Compiler.visit_Repeat`,
// `_enter_assignment`, `_leave_assignment`, `visit_Assignment`, `visit_Define`,
// `visit_Content`, `visit_Attribute`, `visit_DictAttributes`, `visit_Cache`, `visit_Cancel`,
// `visit_Condition`, and `emit_bool`), modified: implemented as bytecode run by a resumable
// VM.

//! The program, its bytecode, and the VM that runs it (D2, D3).
//!
//! The VM is generic over a [`Runtime`] that owns values and their semantics (Python objects
//! in `chameleon-py`). All per-render state lives in [`VmState`], which holds only runtime
//! values, so a render can stop at any instruction and resume later (D9): an instruction
//! either completes or leaves no trace, and a value delivered by [`VmState::resume_with`]
//! takes the place of the expression the instruction was evaluating.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use crate::escape::Escape;
use crate::expr::ExprId;
use crate::output::{Mark, Output};

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
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct LoopInfo {
    pub(crate) names: Vec<NameId>,
    pub(crate) whitespace: TextRef,
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

/// Side tables instructions refer to.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct Tables {
    pub(crate) loops: Vec<LoopInfo>,
    pub(crate) defines: Vec<DefineInfo>,
    pub(crate) attrs: Vec<AttrInfo>,
    pub(crate) dicts: Vec<DictInfo>,
    /// Dictionary indices (within the start tag) of each filtered attribute.
    pub(crate) filters: Vec<u32>,
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
    End,
}

/// An immutable compiled template. `Send + Sync`, shared through `Arc`.
#[derive(Debug)]
pub struct Program {
    pub(crate) ops: Vec<Op>,
    pub(crate) text: Box<[u8]>,
    pub(crate) names: Vec<Box<str>>,
    pub(crate) tables: Tables,
    /// The last render's output size, used as the next buffer's capacity (D6).
    capacity: AtomicUsize,
}

impl Program {
    pub(crate) fn new(ops: Vec<Op>, text: Vec<u8>, names: Vec<Box<str>>, tables: Tables) -> Self {
        Self { ops, text: text.into_boxed_slice(), names, tables, capacity: AtomicUsize::new(0) }
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

    /// A slice of the static text arena; `None` for a reference outside it (a compiler bug).
    #[must_use]
    pub fn text(&self, r: TextRef) -> Option<&[u8]> {
        let start = r.start as usize;
        self.text.get(start..start + r.len as usize)
    }

    /// [`Program::text`] as a `str` (the arena holds UTF-8 pieces of the template).
    #[must_use]
    pub fn text_str(&self, r: TextRef) -> &str {
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
    pub fn dump(&self) -> String {
        use std::fmt::Write;
        let text = |r: TextRef| format!("{:?}", String::from_utf8_lossy(self.text(r).unwrap_or_default()));
        let names =
            |ids: &[NameId]| ids.iter().map(|n| self.names.get(n.0 as usize).map_or("?", |n| &**n)).collect::<Vec<_>>();
        let mut out = String::new();
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
                Op::JumpIfNot { expr, target } => format!("jump_if_not e{} {target}", expr.0),
                Op::LoopStart { info, expr, next } => match self.tables.loops.get(*info as usize) {
                    Some(li) => {
                        format!("loop {:?} in e{} ws={} next={next}", names(&li.names), expr.0, text(li.whitespace))
                    }
                    None => format!("loop ? in e{} next={next}", expr.0),
                },
                Op::LoopNext { body, .. } => format!("loop_next body={body}"),
                Op::Define { info, expr } => match self.tables.defines.get(*info as usize) {
                    Some(d) => format!("define {:?} {:?} = e{}", d.visibility, names(&d.names), expr.0),
                    None => format!("define ? = e{}", expr.0),
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
    /// Only returned where [`Runtime::eval`] was asked for an emitted value.
    Call(Call<V>),
}

#[derive(Debug)]
pub struct Call<V> {
    pub program: Arc<Program>,
    /// Runtime data for the callee's expressions (in `chameleon-py`, its `Program` object).
    pub code: V,
    pub scope: V,
}

/// Whether [`Runtime::emit`] wrote anything (`None` converts to nothing).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Emitted {
    Value,
    Nothing,
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

/// What [`Runtime::repeat`] returns: an iterator and the loop's countdown.
pub type Loop<V> = (V, Countdown<V>);

/// Value semantics, supplied by the embedding (D2). Names are per program: a runtime
/// resolves a [`NameId`] against the program it was last [activated](Runtime::activate) for.
///
/// The methods come in groups (a split into sub-traits is planned with Phase 6b, which adds
/// macros and i18n): frames and evaluation; output; names (backups, binding, restoring);
/// loops; attributes and `tal:case`.
pub trait Runtime {
    type Value;
    type Error;

    // -- Frames and evaluation ------------------------------------------------------------

    /// Makes `code` and `scope` (a frame's) current. Called whenever the active frame changes.
    fn activate(&mut self, code: &Self::Value, scope: &Self::Value) -> Result<(), Self::Error>;
    /// `emitted` is true where the value is written straight to the output, the only place a
    /// [`Step::Call`] may be returned.
    fn eval(&mut self, expr: ExprId, emitted: bool) -> Result<Step<Self::Value>, Self::Error>;
    // -- Output ---------------------------------------------------------------------------

    /// Converts and writes a value for `stage`.
    fn emit(&mut self, value: Self::Value, esc: Escape, stage: Stage, out: &mut Output)
    -> Result<Emitted, Self::Error>;
    fn is_default(&mut self, value: &Self::Value) -> bool;
    fn truthy(&mut self, value: &Self::Value) -> Result<bool, Self::Error>;
    // -- Names ----------------------------------------------------------------------------

    /// The current value of a name (local, then global), as a loop's or define's backup.
    fn lookup(&mut self, name: NameId) -> Result<Option<Self::Value>, Self::Error>;
    /// Binds a name to the runtime's null value (`None`) in the local scope.
    fn set_null(&mut self, name: NameId) -> Result<(), Self::Error>;
    /// `tal:define`: binds the names in the local scope (unpacking for several names); a
    /// global definition also stores the whole value under each name in `rcontext`.
    fn define(&mut self, names: &[NameId], value: Self::Value, visibility: Visibility) -> Result<(), Self::Error>;
    /// Restores a name: deletes it if there was no backup.
    fn restore(&mut self, name: NameId, backup: Option<Self::Value>) -> Result<(), Self::Error>;
    // -- Loops ----------------------------------------------------------------------------

    /// Registers a loop (Chameleon's `repeat(key, iterable)`) and returns an iterator over
    /// what it returned, and its length.
    fn repeat(&mut self, names: &[NameId], iterable: Self::Value) -> Result<Loop<Self::Value>, Self::Error>;
    /// `index - 1` and whether the result is `> 0`, for a [`Countdown::Value`].
    fn countdown(&mut self, index: &Self::Value) -> Result<(Self::Value, bool), Self::Error>;
    /// The next item. It must not suspend: [`Op::LoopNext`] performs its side effects only
    /// after this returns.
    fn next(&mut self, iterator: &Self::Value) -> Result<Option<Self::Value>, Self::Error>;
    /// Binds the loop names to an item (unpacking it for several names).
    fn assign(&mut self, names: &[NameId], item: Self::Value) -> Result<(), Self::Error>;
    // -- Attributes and `tal:case` --------------------------------------------------------

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
    /// Whether a value is the runtime's `False` object (identity, not truth).
    fn is_false(&mut self, value: &Self::Value) -> bool;
}

#[derive(Debug, thiserror::Error)]
pub enum VmError<E> {
    #[error("runtime error")]
    Runtime(#[source] E),
    #[error("maximum recursion depth exceeded")]
    Recursion,
    #[error("internal error: {0}")]
    Internal(&'static str),
}

/// How the render stopped.
#[derive(Debug)]
pub enum Done<V> {
    Finished,
    Suspended(V),
}

/// Nested frames allowed before the render raises `RecursionError` (Chameleon hits Python's
/// recursion limit at a few hundred nested macro calls).
pub const MAX_FRAMES: usize = 500;

/// Largest initial output buffer a program asks for (1 MiB).
pub const MAX_CAPACITY_HINT: usize = 1 << 20;

#[derive(Debug)]
struct Frame<V> {
    program: Arc<Program>,
    pc: u32,
    code: V,
    scope: V,
    /// Text of the caller to write when this frame returns (an attribute's closing quote).
    on_return: Option<TextRef>,
    /// Height of the unwind stack when the frame was entered; its own entries lie above.
    unwind_base: usize,
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
    /// A cached value (a start tag's `tal:attributes` dictionary).
    Value(V),
    /// A `tal:switch` value; `None` once a case matched (Chameleon's `CANCEL` marker).
    Switch(Option<V>),
    /// A boolean or filtered attribute being written: where it started, where its value
    /// starts.
    Attr {
        start: Mark,
        value: Mark,
    },
}

/// Per-render state (D2): frames, the unwind stack and the output. Holds only runtime
/// values, so a suspended render can be stored and resumed.
#[derive(Debug)]
pub struct VmState<V> {
    frames: Vec<Frame<V>>,
    unwind: Vec<Unwind<V>>,
    resume: Option<V>,
    suspended: bool,
    pub out: Output,
}

impl<V> VmState<V> {
    #[must_use]
    pub fn new(program: Arc<Program>, code: V, scope: V) -> Self {
        let out = Output::with_capacity(program.capacity_hint());
        Self {
            frames: vec![Frame { program, pc: 0, code, scope, on_return: None, unwind_base: 0 }],
            unwind: Vec::new(),
            resume: None,
            suspended: false,
            out,
        }
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
                Unwind::Value(v) | Unwind::Switch(Some(v)) => f(v),
                Unwind::Omit(_) | Unwind::Switch(None) | Unwind::Attr { .. } => {}
            }
        }
        if let Some(v) = &self.resume {
            f(v);
        }
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
    fn pop_frame(&mut self) -> Option<Frame<V>> {
        let frame = self.frames.pop()?;
        self.unwind.truncate(frame.unwind_base);
        Some(frame)
    }

    fn frame_base(&self) -> usize {
        self.frames.last().map_or(0, |f| f.unwind_base)
    }
}

enum Action<V> {
    Push(Call<V>, u32, Option<TextRef>),
    Pop,
    Suspend(V, u32),
}

/// Runs (or resumes) a render until it finishes or suspends. A suspended render continues
/// only after [`VmState::resume_with`].
pub fn run<R: Runtime>(st: &mut VmState<R::Value>, rt: &mut R) -> Result<Done<R::Value>, VmError<R::Error>> {
    if st.suspended {
        return Err(VmError::Internal("run on a suspended render without resume_with"));
    }
    loop {
        let Some(frame) = st.frames.last() else { return Ok(Done::Finished) };
        let program = Arc::clone(&frame.program);
        let mut pc = frame.pc as usize;
        rt.activate(&frame.code, &frame.scope).map_err(VmError::Runtime)?;
        let action = execute(&program, &mut pc, st, rt)?;
        match action {
            Action::Push(call, return_pc, on_return) => {
                if st.frames.len() >= MAX_FRAMES {
                    return Err(VmError::Recursion);
                }
                set_pc(st, return_pc);
                let unwind_base = st.unwind.len();
                st.frames.push(Frame {
                    program: call.program,
                    pc: 0,
                    code: call.code,
                    scope: call.scope,
                    on_return,
                    unwind_base,
                });
            }
            Action::Pop => {
                let done = st.pop_frame();
                if let (Some(Frame { on_return: Some(text), .. }), Some(caller)) = (done, st.frames.last()) {
                    st.out.push(caller.program.text(text).ok_or(VmError::Internal("text out of range"))?);
                }
            }
            Action::Suspend(value, at) => {
                set_pc(st, at);
                st.suspended = true;
                return Ok(Done::Suspended(value));
            }
        }
    }
}

fn set_pc<V>(st: &mut VmState<V>, pc: u32) {
    if let Some(frame) = st.frames.last_mut() {
        frame.pc = pc;
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
    program: &Program,
    pc: &mut usize,
    st: &mut VmState<R::Value>,
    rt: &mut R,
) -> Result<Action<R::Value>, VmError<R::Error>> {
    let rt_err = VmError::Runtime;
    let no_call = |_| Err(VmError::Internal("frame call where a value is needed"));
    loop {
        let Some(op) = program.ops.get(*pc) else { return Err(VmError::Internal("pc out of range")) };
        match *op {
            Op::Text(r) => st.out.push(text(program, r)?),
            Op::Emit { expr, esc, stage } => {
                let value = match value_of(st, rt, expr, true)? {
                    Step::Ready(v) => v,
                    Step::Suspend(v) => return Ok(Action::Suspend(v, *pc as u32)),
                    Step::Call(call) => return Ok(Action::Push(call, *pc as u32 + 1, None)),
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
                        return Ok(Action::Push(call, *pc as u32 + 1, Some(suffix)));
                    }
                };
                let mark = st.out.mark();
                st.out.push(text(program, prefix)?);
                match rt.emit(value, esc, Stage::Lone, &mut st.out).map_err(rt_err)? {
                    Emitted::Value => st.out.push(text(program, suffix)?),
                    Emitted::Nothing => st.out.truncate(mark),
                }
            }
            Op::Content { expr, esc, end } => {
                let value = eval_or_leave!(st, rt, pc, expr, true, |call| Ok(Action::Push(call, end, None)));
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
                // Chameleon backs the names up before evaluating the iterable, and binds them
                // to None after registering the loop with `repeat`.
                let mut backups = Vec::with_capacity(li.names.len());
                for &name in &li.names {
                    backups.push(rt.lookup(name).map_err(rt_err)?);
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
                        return Ok(Action::Push(call, *pc as u32 + 1, Some(a.suffix)));
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
                    if rt.emit(value, esc, Stage::Lone, &mut st.out).map_err(rt_err)? == Emitted::Nothing {
                        st.out.truncate(start);
                    } else {
                        finish_attr(program, st, rt, a, start, value_at, boolean, cached)?;
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
                finish_attr(program, st, rt, a, start, value, boolean, cached)?;
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

/// A boolean or filtered attribute whose value has been written after `value`: dropped if a
/// filter has the name; for a boolean attribute an empty value drops it and any other value
/// becomes the name.
#[allow(clippy::too_many_arguments)]
fn finish_attr<R: Runtime>(
    program: &Program,
    st: &mut VmState<R::Value>,
    rt: &mut R,
    a: &AttrInfo,
    start: Mark,
    value: Mark,
    boolean: bool,
    cached: u32,
) -> Result<(), VmError<R::Error>> {
    // `if __attr is not None and name not in chain(...)`: the filter is only consulted for a
    // value that is not `None`. The boolean engine (`literal_false=False`) turns a false
    // *result* into `None`, which drops the attribute.
    if boolean && st.out.mark() == value {
        st.out.truncate(start);
        return Ok(());
    }
    if filtered(program, st, rt, program.text_str(a.name), a.filters, cached)? {
        st.out.truncate(start);
        return Ok(());
    }
    if boolean {
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
    st: &VmState<R::Value>,
    rt: &mut R,
    name: &str,
    filters: (u32, u32),
    cached: u32,
) -> Result<bool, VmError<R::Error>> {
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
    st: &mut VmState<R::Value>,
    rt: &mut R,
    expr: ExprId,
    emitted: bool,
) -> Result<Step<R::Value>, VmError<R::Error>> {
    match st.resume.take() {
        Some(v) => Ok(Step::Ready(v)),
        None => rt.eval(expr, emitted).map_err(VmError::Runtime),
    }
}

/// Binds a fetched item and returns `body`, or (no item) ends the loop, restoring the names
/// in order, and returns `exit`.
fn bind_or_exit<R: Runtime>(
    st: &mut VmState<R::Value>,
    rt: &mut R,
    li: &LoopInfo,
    item: Option<R::Value>,
    body: usize,
    exit: usize,
) -> Result<usize, VmError<R::Error>> {
    if let Some(item) = item {
        rt.assign(&li.names, item).map_err(VmError::Runtime)?;
        return Ok(body);
    }
    let Some(Unwind::Loop { backups, .. }) = st.unwind.pop() else {
        return Err(VmError::Internal("loop exit without its loop"));
    };
    for (&name, backup) in li.names.iter().zip(backups) {
        rt.restore(name, backup).map_err(VmError::Runtime)?;
    }
    Ok(exit)
}
