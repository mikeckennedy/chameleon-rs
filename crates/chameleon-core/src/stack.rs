//! A native stack budget for the compile stages that recurse over what they read (nested
//! elements, nested expression prefixes, long Python expressions).
//!
//! A Rust stack overflow can't be caught: it kills the host process. Chameleon raises
//! `RecursionError` on the same templates (CPython's recursion limit), so a recursive step
//! calls [`enter`], which fails once the recursion has used [`BUDGET`] bytes of stack since the
//! outermost guarded call on this thread, or once less than [`RESERVE`] of the thread's stack is
//! left (a small thread stack, or one the caller had mostly used already). Both are measured,
//! not depth counts, so they hold whatever the frame sizes of the build (debug, release, PGO)
//! and however the stages nest inside each other. The budget also keeps a compile shallow
//! enough for the later stages, which walk the result without a guard.

use std::cell::Cell;

/// Stack the guarded recursion may use: well within a 2 MiB thread stack (Rust's default for
/// spawned threads, which servers such as Granian run Python on), leaving room for the
/// Python frames above it, and deeper than Chameleon goes at the default recursion limit.
pub const BUDGET: usize = 1024 * 1024;

/// Stack a guarded step needs left: room for the frames until the next guarded step, and for
/// the later stages (each about as deep as the compile that passed, measured).
pub const RESERVE: usize = 32 * 1024;

thread_local! {
    /// The stack address at the outermost guarded call (`0`: none active), and the depth.
    static BASE: Cell<(usize, usize)> = const { Cell::new((0, 0)) };
}

/// The recursion went past [`BUDGET`], or left less than [`RESERVE`] of the stack.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("maximum recursion depth exceeded while compiling the template")]
pub struct TooDeep;

/// Held for the duration of one recursive step.
#[derive(Debug)]
#[must_use]
pub struct Guard(());

/// Enters one recursive step: `Err(TooDeep)` once the recursion uses more than [`BUDGET`], or
/// less than [`RESERVE`] of the stack is left.
pub fn enter() -> Result<Guard, TooDeep> {
    // `None` where the platform doesn't tell; the budget still applies.
    if stacker::remaining_stack().is_some_and(|left| left < RESERVE) {
        return Err(TooDeep);
    }
    let here = address();
    BASE.with(|cell| {
        let (base, depth) = cell.get();
        if depth == 0 {
            cell.set((here, 1));
            return Ok(Guard(()));
        }
        // The stack grows down (x86_64, aarch64). A base deeper than this frame (a lower
        // address), left by a compile suspended on another greenlet of this thread, counts as
        // nothing used, never a false error: the remaining-stack check above still holds.
        if base.saturating_sub(here) > BUDGET {
            return Err(TooDeep);
        }
        cell.set((base, depth + 1));
        Ok(Guard(()))
    })
}

impl Drop for Guard {
    fn drop(&mut self) {
        BASE.with(|cell| {
            let (base, depth) = cell.get();
            cell.set(if depth <= 1 { (0, 0) } else { (base, depth - 1) });
        });
    }
}

/// Stack a render needs left when it starts (see [`render_room`]): its own frames (the VM, a
/// value's conversion, the Python calls an expression makes; a level of nesting through Python
/// costs about 8 KiB on CPython 3.13) and the Python frames above it, with room to spare, but
/// small enough that a thread with a small stack (512 KiB) can still nest a few dozen levels.
pub const RENDER_RESERVE: usize = 128 * 1024;

/// Stack a clone of a compiled subtree may use ([`deep_clone`]): the guarded compile nests at
/// most [`BUDGET`] of `visit` frames, and the derived `Clone` takes about 2.5 times that per
/// element (five `Node::clone` frames of ~1.8 KiB against one `visit_element` of ~3.9 KiB).
const CLONE_STACK: usize = 4 * BUDGET;

/// `value.clone()` for a compiled tree as deep as the guarded compile allows, on a stack of its
/// own when the thread's has less than that left: the derived `Clone` recurses with no guard,
/// and on a thread with a small stack (512 KiB to 1 MiB) a fill-slot body nested just under
/// the compile's limit overflowed it, killing the process.
pub fn deep_clone<T: Clone>(value: &T) -> T {
    stacker::maybe_grow(CLONE_STACK, CLONE_STACK, || value.clone())
}

/// Whether a render may start on this thread's stack: `Err(TooDeep)` once less than
/// [`RENDER_RESERVE`] is left. The VM itself doesn't recurse (macros and slots are frames on
/// its own stack), but a render can be nested through Python: a value whose `__html__` or a
/// helper that renders another template, which may render another, as deep as the data goes.
/// Each level costs a few KiB of native stack, and CPython before 3.14 counts only Python
/// frames, so without this a deep enough nesting on a thread with a small stack (a server's
/// worker thread) would overflow it and kill the process instead of raising `RecursionError`.
pub fn render_room() -> Result<(), TooDeep> {
    // `None` where the platform doesn't tell: CPython's own limit is all there is.
    if stacker::remaining_stack().is_some_and(|left| left < RENDER_RESERVE) {
        return Err(TooDeep);
    }
    Ok(())
}

/// The address of a local in the caller's frame: how far the stack has grown.
#[inline(never)]
fn address() -> usize {
    let marker = 0u8;
    std::ptr::from_ref(std::hint::black_box(&marker)).addr()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn recurse(n: usize) -> Result<usize, TooDeep> {
        let _guard = enter()?;
        let pad = std::hint::black_box([0u8; 512]);
        if n == 0 { Ok(usize::from(pad[0])) } else { recurse(n - 1).map(|d| d + 1) }
    }

    #[test]
    fn shallow_recursion_passes_and_resets() {
        assert_eq!(recurse(100), Ok(100));
        assert_eq!(BASE.with(Cell::get), (0, 0));
    }

    #[test]
    fn a_small_stack_is_too_deep_before_it_overflows() {
        // Less stack than the budget: the stack left is what stops it.
        let deep = std::thread::Builder::new()
            .stack_size(192 * 1024)
            .spawn(|| (recurse(1_000_000), BASE.with(Cell::get)))
            .expect("spawn");
        assert_eq!(deep.join().expect("join"), (Err(TooDeep), (0, 0)));
    }

    #[test]
    fn deep_recursion_is_too_deep_and_resets() {
        let deep = std::thread::Builder::new()
            .stack_size(4 * BUDGET)
            .spawn(|| (recurse(1_000_000), BASE.with(Cell::get)))
            .expect("spawn");
        assert_eq!(deep.join().expect("join"), (Err(TooDeep), (0, 0)));
    }
}
