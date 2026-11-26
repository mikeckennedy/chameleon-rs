"""Opt-in concurrency for ``render_async`` (D9).

``@concurrent`` marks an async helper: in an asynchronous render, a call to it written as a
whole ``${...}`` interpolation (in text, or as part of an attribute value) is not waited for
where it stands. The render starts it as a task, writes a placeholder and goes on; several such
calls on a page therefore run at the same time. Each placeholder gets its value, converted and
escaped as it would have been, when the render reaches its end. Anywhere else (a condition, a
definition, ``tal:content`` / ``tal:replace``, whose value may select the default content, a
whole attribute value, inside ``tal:on-error`` or a translation, where the output may still be
taken back, or inside a native partial) the call is awaited in place, like any awaitable.

A template with the setting ``concurrent=True`` also fans native partials out: a
``render_partial(...)`` call of the page itself (not of a partial) with an awaitable argument
renders as a render of its own, in a task, with a placeholder for its output.

Both change the order in which expressions run (a helper's code runs while the render goes on),
so they are opt-in. An error is raised when the render reaches the end, as soon as a task has
failed (the other tasks cancelled), with the expression's position but without the enclosing
macros'. One render starts at most ``chameleon_rs.config.CONCURRENT_LIMIT`` tasks (64,
``CHAMELEON_CONCURRENT_LIMIT``); further calls are awaited in place.

A synchronous ``render`` sees what the helper returns (a coroutine, wrapped), as it would
without the decorator.
"""

from __future__ import annotations

import asyncio
import functools
import sys
from collections.abc import Awaitable, Callable, Generator
from typing import Any, Optional

__all__ = ['ConcurrentCall', 'concurrent']


class ConcurrentCall:
    """What a ``@concurrent`` helper returns: its awaitable, marked for concurrent awaiting.
    ``convert``, if set, is applied to the awaited value (``structure:`` / ``markdown:`` of the
    call, see ``_then``): kept beside the awaitable, not wrapped around it, so a task is made
    from the helper's own coroutine and cancelling the task before it starts closes that
    coroutine (wrapped in another, it was left never awaited, which Python warns about)."""

    __slots__ = ('awaitable', 'convert')

    def __init__(self, awaitable: Awaitable[Any], convert: Optional[Callable[[Any], Any]] = None) -> None:
        self.awaitable = awaitable
        self.convert = convert

    def __await__(self) -> Generator[Any, None, Any]:
        if self.convert is None:
            return self.awaitable.__await__()
        return self._converted(self.convert)

    def _converted(self, convert: Callable[[Any], Any]) -> Generator[Any, None, Any]:
        value = yield from self.awaitable.__await__()
        return convert(value)

    def __repr__(self) -> str:
        return f'<ConcurrentCall {self.awaitable!r}>'


def concurrent[**P](fn: Callable[P, Awaitable[Any]]) -> Callable[P, ConcurrentCall]:
    """Marks an async helper so that an asynchronous render awaits its calls concurrently
    (see the module docs)."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> ConcurrentCall:
        return ConcurrentCall(fn(*args, **kwargs))

    return wrapper


def _then(awaitable: Awaitable[Any], convert: Callable[[Any], Any]) -> Awaitable[Any]:
    # `structure:` / `markdown:` of an awaitable in an asynchronous render: the expression's
    # final value, which the render awaits like any other (the value awaited, then converted).
    # A `@concurrent` call stays one, so it still runs alongside the others.
    if isinstance(awaitable, ConcurrentCall):
        first = awaitable.convert
        return ConcurrentCall(awaitable.awaitable, convert if first is None else lambda value: convert(first(value)))
    return _convert(awaitable, convert)


async def _convert(awaitable: Awaitable[Any], convert: Callable[[Any], Any]) -> Any:
    return convert(await awaitable)


async def _wait_failed(tasks: list[asyncio.Future[Any]]) -> None:
    # What a render with `@concurrent` placeholders awaits for its tasks: until every one is
    # done, or one failed or was cancelled. `asyncio.wait(..., FIRST_EXCEPTION)` doesn't count a
    # cancelled task as failed, so a helper's own cancellation waited for all the others.
    waiter = asyncio.get_running_loop().create_future()
    left = len(tasks)

    def done(task: asyncio.Future[Any]) -> None:
        nonlocal left
        left -= 1
        if not waiter.done() and (left == 0 or task.cancelled() or task.exception() is not None):
            waiter.set_result(None)

    for task in tasks:
        task.add_done_callback(done)
    try:
        await waiter
    finally:
        for task in tasks:
            task.remove_done_callback(done)


class _Once:
    """A coroutine passed to a native partial in an asynchronous render (``partials.rs``):
    awaited the first time the partial uses it, its result (or error) is the value of every
    later use too, as a ``tal:define`` of it would be. A coroutine itself can be awaited only
    once, so ``<i>${v}${v}</i>`` in the partial would fail on the second ``${v}``."""

    __slots__ = ('awaitable', 'error', 'result', 'waiters')

    def __init__(self, awaitable: Any) -> None:
        # Kept until awaited (`None` after), so an unstarted one can still be closed.
        self.awaitable: Any = awaitable
        self.result: Any = None
        self.error: Optional[BaseException] = None
        # While the first use is awaiting it: the futures of the uses that overlap it
        # (`asyncio.gather` of two reads), which got `None` before the result was there.
        self.waiters: Optional[list[asyncio.Future[None]]] = None

    def __await__(self) -> Generator[Any, Any, Any]:
        awaitable = self.awaitable
        if awaitable is not None:
            self.awaitable = None
            self.waiters = []
            try:
                inner = awaitable.__await__() if hasattr(type(awaitable), '__await__') else awaitable
                self.result = yield from inner
            except Exception as e:  # noqa: BLE001 - kept for every later use, then raised
                self.error = e
            except BaseException:
                # This reader was cancelled or closed, which ended the coroutine with it. That
                # is this reader's alone to see: the others get what awaiting the finished
                # coroutine gives, an error `tal:on-error` can handle and no cancellation of
                # their own.
                self.error = RuntimeError('cannot reuse already awaited coroutine')
                raise
            finally:
                waiters, self.waiters = self.waiters, None
                for waiter in waiters:
                    if not waiter.done():
                        waiter.set_result(None)
        elif self.waiters is not None:
            waiter = asyncio.get_running_loop().create_future()
            self.waiters.append(waiter)
            yield from waiter
        if self.error is not None:
            raise self.error
        return self.result

    def __del__(self) -> None:
        # Never used (`v=fetch()` for a block the partial's condition skipped): closed, as
        # nothing will await it now, and Python otherwise warns that it was never awaited on
        # a render that went well. Only here, when the last holder of the wrapper is gone (a
        # helper may have kept it to await after the partial returned), and only a coroutine
        # nobody else holds (one from the model may be another partial's argument too): the
        # two references counted are this attribute and the call's argument.
        try:
            if self.awaitable is not None and sys.getrefcount(self.awaitable) <= 2:
                self.awaitable.close()
        except Exception:  # noqa: BLE001, S110 - a finalizer, possibly at interpreter exit
            pass


async def _await[T](awaitable: Awaitable[T]) -> T:
    # A coroutine around a native awaitable (`asyncio.ensure_future` takes it as is).
    return await awaitable
