//! Experiments 5 and 7 (D2, D3): a minimal flat VM generic over a `Runtime`, with the
//! `Program` / `VmState` / `Runtime` split. The VM loop knows nothing about Python.

use std::collections::VecDeque;

use crate::expr::Escape;

pub(crate) enum Op {
    Text(String),
    Emit {
        expr: usize,
        escape: Escape,
    },
    /// `end` is the index of the matching `EndLoop`.
    Loop {
        name: usize,
        iter: usize,
        whitespace: String,
        end: usize,
    },
    EndLoop {
        start: usize,
    },
    End,
}

pub(crate) trait Runtime {
    type Value;
    type Error;
    fn eval(&mut self, expr: usize) -> Result<Self::Value, Self::Error>;
    fn is_awaitable(&mut self, value: &Self::Value) -> bool;
    fn emit(&mut self, value: Self::Value, escape: Escape, out: &mut String) -> Result<(), Self::Error>;
    /// Chameleon materializes the iterable (`list(iterable)` in `RepeatDict`).
    fn iter_start(&mut self, value: Self::Value) -> Result<Vec<Self::Value>, Self::Error>;
    /// Assigns `name`, returning the previous value (two-level lookup) as the backup.
    fn define(&mut self, name: usize, value: Self::Value) -> Result<Option<Self::Value>, Self::Error>;
    fn set(&mut self, name: usize, value: Self::Value) -> Result<(), Self::Error>;
    fn restore(&mut self, name: usize, backup: Option<Self::Value>) -> Result<(), Self::Error>;
}

struct LoopState<V> {
    items: VecDeque<V>,
    name: usize,
    backup: Option<V>,
}

/// Per-render state: holds only `'static` values so a suspended render can live in a pyclass.
pub(crate) struct VmState<V> {
    pc: usize,
    loops: Vec<LoopState<V>>,
    pub(crate) out: String,
    /// Set while suspended on an awaitable inside an `Emit`.
    pending: Option<Escape>,
}

impl<V> VmState<V> {
    pub(crate) fn with_capacity(capacity: usize) -> Self {
        Self { pc: 0, loops: Vec::new(), out: String::with_capacity(capacity), pending: None }
    }
}

pub(crate) enum Step<V> {
    Done,
    Suspend(V),
}

pub(crate) fn run<R: Runtime>(
    ops: &[Op],
    st: &mut VmState<R::Value>,
    rt: &mut R,
    resume: Option<R::Value>,
) -> Result<Step<R::Value>, R::Error> {
    if let Some(value) = resume {
        let escape = st.pending.take().expect("resume value given only after a suspension");
        rt.emit(value, escape, &mut st.out)?;
        st.pc += 1;
    }
    loop {
        match &ops[st.pc] {
            Op::Text(s) => st.out.push_str(s),
            Op::Emit { expr, escape } => {
                let value = rt.eval(*expr)?;
                if rt.is_awaitable(&value) {
                    st.pending = Some(*escape);
                    return Ok(Step::Suspend(value));
                }
                rt.emit(value, *escape, &mut st.out)?;
            }
            Op::Loop { name, iter, end, .. } => {
                let iterable = rt.eval(*iter)?;
                let mut items: VecDeque<_> = rt.iter_start(iterable)?.into();
                if let Some(first) = items.pop_front() {
                    let backup = rt.define(*name, first)?;
                    st.loops.push(LoopState { items, name: *name, backup });
                } else {
                    st.pc = *end + 1;
                    continue;
                }
            }
            Op::EndLoop { start } => {
                let top = st.loops.last_mut().expect("EndLoop always follows its Loop");
                if let Some(item) = top.items.pop_front() {
                    if let Op::Loop { whitespace, .. } = &ops[*start] {
                        st.out.push_str(whitespace);
                    }
                    rt.set(top.name, item)?;
                    st.pc = *start + 1;
                    continue;
                }
                let done = st.loops.pop().expect("EndLoop always follows its Loop");
                rt.restore(done.name, done.backup)?;
            }
            Op::End => return Ok(Step::Done),
        }
        st.pc += 1;
    }
}
