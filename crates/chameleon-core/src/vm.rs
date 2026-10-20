// The loop semantics below follow Chameleon's src/chameleon/compiler.py
// (`Compiler.visit_Repeat`, `_enter_assignment`, `_leave_assignment`, `visit_Content`,
// `visit_Attribute`), modified: implemented as bytecode run by a resumable VM.

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
use crate::output::Output;

/// A slice of a program's static text arena.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TextRef {
    pub start: u32,
    pub len: u32,
}

/// Index into a program's name table (variables assigned by the program).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct NameId(pub u32);

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct LoopInfo {
    pub(crate) names: Vec<NameId>,
    pub(crate) whitespace: TextRef,
}

/// One instruction. Jump targets are instruction indices within the same program.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Op {
    /// Static text, pre-joined at compile time.
    Text(TextRef),
    /// Evaluate, convert and write a value (`${...}`, attribute value parts). `lone` is set
    /// when the expression is the whole interpolation (a text node that is just `${x}`),
    /// which selects Chameleon's second conversion stage for non-`str` results.
    Emit {
        expr: ExprId,
        esc: Escape,
        lone: bool,
    },
    /// `prefix VALUE suffix` for a single-expression attribute; nothing if the converted
    /// value is `None`.
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
    End,
}

/// An immutable compiled template. `Send + Sync`, shared through `Arc`.
#[derive(Debug)]
pub struct Program {
    pub(crate) ops: Vec<Op>,
    pub(crate) text: Box<[u8]>,
    pub(crate) names: Vec<Box<str>>,
    pub(crate) loops: Vec<LoopInfo>,
    /// The last render's output size, used as the next buffer's capacity (D6).
    capacity: AtomicUsize,
}

impl Program {
    pub(crate) fn new(ops: Vec<Op>, text: Vec<u8>, names: Vec<Box<str>>, loops: Vec<LoopInfo>) -> Self {
        Self { ops, text: text.into_boxed_slice(), names, loops, capacity: AtomicUsize::new(0) }
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
        let mut out = String::new();
        for (i, op) in self.ops.iter().enumerate() {
            let line = match op {
                Op::Text(r) => format!("text {}", text(*r)),
                Op::Emit { expr, esc, lone } => {
                    format!("emit e{} {esc:?}{}", expr.0, if *lone { " lone" } else { "" })
                }
                Op::Attr { expr, esc, prefix, suffix } => {
                    format!("attr e{} {esc:?} {} {}", expr.0, text(*prefix), text(*suffix))
                }
                Op::Content { expr, esc, end } => format!("content e{} {esc:?} end={end}", expr.0),
                Op::JumpIfNot { expr, target } => format!("jump_if_not e{} {target}", expr.0),
                Op::LoopStart { info, expr, next } => {
                    let li = &self.loops[*info as usize];
                    let names: Vec<_> = li.names.iter().map(|n| &*self.names[n.0 as usize]).collect();
                    format!("loop {names:?} in e{} ws={} next={next}", expr.0, text(li.whitespace))
                }
                Op::LoopNext { body, .. } => format!("loop_next body={body}"),
                Op::End => "end".to_owned(),
            };
            let _ = writeln!(out, "{i:3} {line}");
        }
        out
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
pub trait Runtime {
    type Value;
    type Error;

    /// Makes `code` and `scope` (a frame's) current. Called whenever the active frame changes.
    fn activate(&mut self, code: &Self::Value, scope: &Self::Value) -> Result<(), Self::Error>;
    /// `emitted` is true where the value is written straight to the output, the only place a
    /// [`Step::Call`] may be returned.
    fn eval(&mut self, expr: ExprId, emitted: bool) -> Result<Step<Self::Value>, Self::Error>;
    /// Converts and writes a value. `lone`: the expression is a whole interpolation (see
    /// [`Op::Emit`]); always true for [`Op::Attr`], meaningless for `tal:content`.
    fn emit(&mut self, value: Self::Value, esc: Escape, lone: bool, out: &mut Output) -> Result<Emitted, Self::Error>;
    fn is_default(&mut self, value: &Self::Value) -> bool;
    fn truthy(&mut self, value: &Self::Value) -> Result<bool, Self::Error>;
    /// The current value of a name (local, then global), as a loop's backup.
    fn lookup(&mut self, name: NameId) -> Result<Option<Self::Value>, Self::Error>;
    /// Binds a name to the runtime's null value (`None`) in the local scope.
    fn set_null(&mut self, name: NameId) -> Result<(), Self::Error>;
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
    /// Restores a loop name: deletes it if there was no backup.
    fn restore(&mut self, name: NameId, backup: Option<Self::Value>) -> Result<(), Self::Error>;
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

/// The single unwind stack (D3): everything a frame has entered and must leave. Loops now;
/// define backups, `on-error` handlers and translation marks join it with their features.
#[derive(Debug)]
enum Unwind<V> {
    Loop { iterator: V, index: Countdown<V>, backups: Vec<Option<V>> },
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

#[allow(clippy::cast_possible_truncation)] // instruction indices fit in u32 (checked at build)
#[allow(clippy::too_many_lines)] // one arm per instruction
fn execute<R: Runtime>(
    program: &Program,
    pc: &mut usize,
    st: &mut VmState<R::Value>,
    rt: &mut R,
) -> Result<Action<R::Value>, VmError<R::Error>> {
    let rt_err = VmError::Runtime;
    loop {
        let Some(op) = program.ops.get(*pc) else { return Err(VmError::Internal("pc out of range")) };
        match *op {
            Op::Text(r) => st.out.push(text(program, r)?),
            Op::Emit { expr, esc, lone } => {
                let value = match value_of(st, rt, expr, true)? {
                    Step::Ready(v) => v,
                    Step::Suspend(v) => return Ok(Action::Suspend(v, *pc as u32)),
                    Step::Call(call) => return Ok(Action::Push(call, *pc as u32 + 1, None)),
                };
                rt.emit(value, esc, lone, &mut st.out).map_err(rt_err)?;
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
                match rt.emit(value, esc, true, &mut st.out).map_err(rt_err)? {
                    Emitted::Value => st.out.push(text(program, suffix)?),
                    Emitted::Nothing => st.out.truncate(mark),
                }
            }
            Op::Content { expr, esc, end } => {
                let value = match value_of(st, rt, expr, true)? {
                    Step::Ready(v) => v,
                    Step::Suspend(v) => return Ok(Action::Suspend(v, *pc as u32)),
                    Step::Call(call) => return Ok(Action::Push(call, end, None)),
                };
                if !rt.is_default(&value) {
                    rt.emit(value, esc, false, &mut st.out).map_err(rt_err)?;
                    *pc = end as usize;
                    continue;
                }
            }
            Op::JumpIfNot { expr, target } => {
                let value = match value_of(st, rt, expr, false)? {
                    Step::Ready(v) => v,
                    Step::Suspend(v) => return Ok(Action::Suspend(v, *pc as u32)),
                    Step::Call(_) => return Err(VmError::Internal("frame call in a condition")),
                };
                if !rt.truthy(&value).map_err(rt_err)? {
                    *pc = target as usize;
                    continue;
                }
            }
            Op::LoopStart { info, expr, next } => {
                let li = program.loops.get(info as usize).ok_or(VmError::Internal("bad loop"))?;
                // Chameleon backs the names up before evaluating the iterable, and binds them
                // to None after registering the loop with `repeat`.
                let mut backups = Vec::with_capacity(li.names.len());
                for &name in &li.names {
                    backups.push(rt.lookup(name).map_err(rt_err)?);
                }
                let iterable = match value_of(st, rt, expr, false)? {
                    Step::Ready(v) => v,
                    Step::Suspend(v) => return Ok(Action::Suspend(v, *pc as u32)),
                    Step::Call(_) => return Err(VmError::Internal("frame call in a repeat")),
                };
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
                let li = program.loops.get(info as usize).ok_or(VmError::Internal("bad loop"))?;
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
            Op::End => {
                let base = st.frames.last().map_or(0, |f| f.unwind_base);
                if st.unwind.len() != base {
                    return Err(VmError::Internal("frame ended with entries on the unwind stack"));
                }
                return Ok(Action::Pop);
            }
        }
        *pc += 1;
    }
}

fn set_index<V>(st: &mut VmState<V>, new: Countdown<V>) {
    if let Some(Unwind::Loop { index, .. }) = st.unwind.last_mut() {
        *index = new;
    }
}

/// A resumed value, or a fresh evaluation.
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
