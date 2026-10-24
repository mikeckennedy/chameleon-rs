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

import functools
from collections.abc import Awaitable, Callable, Generator
from typing import Any

__all__ = ['ConcurrentCall', 'concurrent']


class ConcurrentCall:
    """What a ``@concurrent`` helper returns: its awaitable, marked for concurrent awaiting."""

    __slots__ = ('awaitable',)

    def __init__(self, awaitable: Awaitable[Any]) -> None:
        self.awaitable = awaitable

    def __await__(self) -> Generator[Any, None, Any]:
        return self.awaitable.__await__()

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
        return ConcurrentCall(_convert(awaitable.awaitable, convert))
    return _convert(awaitable, convert)


async def _convert(awaitable: Awaitable[Any], convert: Callable[[Any], Any]) -> Any:
    return convert(await awaitable)


async def _await[T](awaitable: Awaitable[T]) -> T:
    # A coroutine around a native awaitable (`asyncio.ensure_future` takes it as is).
    return await awaitable
