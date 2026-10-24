"""Fixes from the crash, leak and GC review (2026-10-23): what must not take down a web worker,
wedge a request, or keep memory.

- Renders nested through Python (a value's `__html__` or a helper rendering another template,
  as deep as the data) raise `RecursionError` before the native stack runs out, on a thread
  with a small stack; so does a compile cloning a deep fill-slot body.
- A macro with a code block called in a long `tal:repeat` renders in linear time.
- `str()` of a render error whose template file can't be decoded still shows the message.
- Asynchronous renders: a cancellation during the join of cancelled tasks is not swallowed; a
  failed `@concurrent` call ends the render without waiting for slower calls before it; the
  fan-out is bounded; a `StopIteration` raised by the template is a `RuntimeError` (PEP 479),
  not the render's result; a second awaiter of a suspended render is an error; cancelled
  concurrent calls and partial arguments leave no "never awaited" warnings.
"""

from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import textwrap
import time
import warnings
from pathlib import Path
from typing import Any

import chameleon
import chameleon_rs
import chameleon_rs.config
import chameleon_rs.exc
import pytest
from chameleon_rs import PageTemplate, PageTemplateFile, concurrent
from chameleon_rs.template import render_page_async

LOOP = asyncio.new_event_loop()


def run(awaitable: Any) -> Any:
    return LOOP.run_until_complete(awaitable)


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """`code` in a fresh interpreter (a native stack overflow would kill it, not the tests)."""
    return subprocess.run(
        [sys.executable, '-c', textwrap.dedent(code)], capture_output=True, text=True, check=False, timeout=300
    )


# -- Native stack ----------------------------------------------------------------------------


@pytest.mark.parametrize('stack_kib', [512, 1024, 2048])
def test_renders_nested_through_python_raise_recursion_error_on_small_stacks(stack_kib: int) -> None:
    # The component pattern: an object whose `__html__` renders a template showing its
    # children, nested as deep as the data (a reply thread). Before CPython 3.14 only Python
    # frames are counted, and a 2 MiB thread overflowed at about 270 levels (SIGBUS).
    result = run_python(f"""
        import sys, threading
        from chameleon_rs import PageTemplate

        sys.setrecursionlimit(100_000)
        page = PageTemplate('<div>${{child}}</div>')

        class Node:
            def __init__(self, child):
                self.child = child

            def __html__(self):
                return page.render(child=self.child)

        def main():
            # (Deep enough for any of these stacks; a much deeper chain of objects overflows
            # CPython's own deallocation on a small stack, with or without a render.)
            node = 'leaf'
            for _ in range(1_000):
                node = Node(node)
            try:
                node.__html__()
            except RecursionError:
                print('recursion error')

        threading.stack_size({stack_kib} * 1024)
        thread = threading.Thread(target=main)
        thread.start()
        thread.join()
    """)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == 'recursion error'


def test_shallow_nesting_through_python_still_renders_on_a_small_stack() -> None:
    result = run_python("""
        import threading
        from chameleon_rs import PageTemplate

        page = PageTemplate('<i>${child}</i>')

        class Node:
            def __init__(self, child):
                self.child = child

            def __html__(self):
                return page.render(child=self.child)

        def main():
            node = 'x'
            for _ in range(20):
                node = Node(node)
            print(node.__html__().count('<i>'))

        threading.stack_size(512 * 1024)
        thread = threading.Thread(target=main)
        thread.start()
        thread.join()
    """)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == '20'


@pytest.mark.parametrize('stack_kib', [512, 1024])
def test_a_deep_fill_slot_body_compiles_or_raises_on_a_small_stack(stack_kib: int) -> None:
    # The fill-slot body is copied when it is compiled; the copy recursed with no guard and
    # overflowed a small stack just under the compile's own nesting limit.
    result = run_python(f"""
        import threading
        from chameleon_rs import PageTemplate

        def main():
            for depth in range(40, 400, 10):
                inner = '<b tal:condition="1" tal:attributes="x 1">' * depth + 'x' + '</b>' * depth
                body = (
                    '<div metal:define-macro="m"><p metal:define-slot="s"/></div>'
                    '<div metal:use-macro="template.macros[\\'m\\']">'
                    f'<p metal:fill-slot="s">{{inner}}</p></div>'
                )
                try:
                    PageTemplate(body).render()
                except RecursionError:
                    pass
            print('done')

        threading.stack_size({stack_kib} * 1024)
        thread = threading.Thread(target=main)
        thread.start()
        thread.join()
    """)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == 'done'


# -- Code-block namespaces -------------------------------------------------------------------


ROW_MACRO = """\
<tal:d condition="False"><metal:m define-macro="row"><?python
y = x * 2
?><b tal:repeat="j range(2)">${y}</b><p metal:define-slot="s"/>${y}</metal:m></tal:d><?python
y = "outer"
?><div tal:repeat="x xs"><metal:u use-macro="template.macros['row']"><i metal:fill-slot="s">${y}</i></metal:u></div>${y}"""


def test_macro_with_code_block_in_a_long_repeat_is_linear() -> None:
    # Each macro call's code-block namespace was kept for the whole render, and each lookup
    # searched them all: 128k items took 1.3 s (10 us each) against 40 ms without the block.
    page = PageTemplate(ROW_MACRO)

    def timed(n: int) -> float:
        start = time.perf_counter()
        out = page.render(xs=range(n))
        assert out.count('<div>') == n
        return time.perf_counter() - start

    timed(1000)
    small = min(timed(4_000) for _ in range(3))
    large = min(timed(32_000) for _ in range(3))
    # Linear is a ratio of 8; quadratic was over 20 (and grows with n).
    assert large / small < 16, (small, large)


def test_code_block_names_still_reach_the_rest_of_their_frame() -> None:
    # The macro's name stays its own across its repeat and the caller's fill, and the caller's
    # survives the macro calls, as in Chameleon.
    want = chameleon.PageTemplate(ROW_MACRO).render(xs=range(3))
    assert PageTemplate(ROW_MACRO).render(xs=range(3)) == want
    assert want.endswith('outer')


# -- Error messages --------------------------------------------------------------------------


def test_render_error_of_a_file_that_is_not_utf8_still_formats(tmp_path: Path) -> None:
    # The source line was read with the locale's encoding: an undecodable file made `str()`
    # of the error raise, so logging lost the message (stock Chameleon does the same).
    path = tmp_path / 'latin.pt'
    path.write_bytes('<?xml version="1.0" encoding="latin-1"?>\n<p>caf\xe9 ${missing}</p>'.encode('latin-1'))
    page = PageTemplateFile(str(path))
    with pytest.raises(NameError) as error:
        page.render()
    text = str(error.value)
    assert 'missing' in text
    assert ' - Source:' in text
    assert 'latin.pt' in text


def test_render_error_message_survives_a_formatter_that_raises() -> None:
    def failing(value: object) -> object:
        raise ZeroDivisionError

    class Failing(PageTemplate):
        pass

    Failing.value_repr = staticmethod(failing)
    page = Failing('<p>${missing}</p>')
    with pytest.raises(NameError) as error:
        page.render(extra=1)
    assert 'missing' in str(error.value)


# -- Asynchronous renders ---------------------------------------------------------------------


def _slow_cleanup(cleaned: list[str]) -> Any:
    @concurrent
    async def slow() -> str:
        try:
            await asyncio.sleep(10)
        finally:
            # A cleanup that awaits (closing a connection): the join stays open meanwhile.
            await asyncio.sleep(0.2)
            cleaned.append('slow')
        return 'never'

    return slow


@concurrent
async def bad() -> str:
    await asyncio.sleep(0.01)
    raise KeyError('bad')


def test_cancellation_during_the_join_is_not_swallowed() -> None:
    cleaned: list[str] = []
    page = PageTemplate('${bad()}${slow()}')

    async def main() -> None:
        task = asyncio.ensure_future(page.render_async(bad=bad, slow=_slow_cleanup(cleaned)))
        await asyncio.sleep(0.1)  # `bad` failed; the render waits for `slow`'s cleanup
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.3)

    # (Cancelling the render cancels the gather it waits on, and so the cleanup itself, as
    # cancelling a `TaskGroup`'s owner does: asyncio's semantics, nothing to assert there.)
    run(main())


def test_timeout_during_the_join_is_a_timeout() -> None:
    page = PageTemplate('${bad()}${slow()}')

    async def main() -> None:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await page.render_async(bad=bad, slow=_slow_cleanup([]))
        await asyncio.sleep(0.3)

    run(main())


def test_a_failed_concurrent_call_does_not_wait_for_slower_calls_before_it() -> None:
    @concurrent
    async def slow() -> str:
        await asyncio.sleep(10)
        return 'never'

    page = PageTemplate('<div>${slow()}</div><div>${bad()}</div>')
    start = time.perf_counter()
    with pytest.raises(KeyError) as error:
        run(page.render_async(bad=bad, slow=slow))
    assert time.perf_counter() - start < 2
    assert 'Expression: "bad()"' in str(error.value)


def test_concurrent_values_still_fill_in_document_order() -> None:
    @concurrent
    async def after(delay: float, value: str) -> str:
        await asyncio.sleep(delay)
        return value

    page = PageTemplate('<i tal:repeat="n range(5)">${after(0.05 - n / 100, n)}</i>')
    assert run(page.render_async(after=after)) == '\n'.join(f'<i>{n}</i>' for n in range(5))


def test_concurrent_fan_out_is_bounded() -> None:
    running = 0
    most = 0

    @concurrent
    async def load(n: int) -> int:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.001)
        running -= 1
        return n

    count = chameleon_rs.config.CONCURRENT_LIMIT * 3
    page = PageTemplate('<i tal:repeat="n range(count)">${load(n)}</i>')
    out = run(page.render_async(load=load, count=count))
    assert out == '\n'.join(f'<i>{n}</i>' for n in range(count))
    assert most <= chameleon_rs.config.CONCURRENT_LIMIT + 1, most


def test_stop_iteration_from_an_async_render_is_runtime_error() -> None:
    # Raised out of the awaitable's `__next__`, a `StopIteration` read as the render returning:
    # `await render_async()` gave the exception's args instead of raising.
    page = PageTemplate('<p>${next(iter([]))}</p>')
    with pytest.raises(RuntimeError, match='coroutine raised StopIteration') as error:
        run(page.render_async())
    assert isinstance(error.value.__cause__, StopIteration)
    with pytest.raises(StopIteration):
        page.render()


def test_a_second_awaiter_of_a_suspended_render_is_an_error() -> None:
    async def aw(value: Any) -> Any:
        await asyncio.sleep(0.01)
        return value

    page = PageTemplate('${aw(1)}${aw(2)}')

    async def main() -> None:
        awaitable = render_page_async(page, None, {'aw': aw}, False)
        assert not isinstance(awaitable, str)

        async def wait() -> Any:
            return await awaitable

        first = asyncio.ensure_future(wait())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match='being awaited already'):
            await wait()
        assert await first == '12'

    run(main())


def _never_awaited(caught: list[warnings.WarningMessage]) -> list[str]:
    return [str(w.message) for w in caught if 'never awaited' in str(w.message)]


def test_cancelled_concurrent_calls_leave_no_never_awaited_warning() -> None:
    @concurrent
    async def slow() -> str:
        await asyncio.sleep(10)
        return 'never'

    def fails() -> str:
        raise KeyError('now')

    # The render fails in the same step that started the tasks: they are cancelled before
    # they ever run.
    page = PageTemplate('${slow()}${slow()}${fails()}')
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with pytest.raises(KeyError):
            run(page.render_async(slow=slow, fails=fails))
        run(asyncio.sleep(0))
        gc.collect()
    assert _never_awaited(caught) == []


def test_cancelled_partial_arguments_leave_no_never_awaited_warning(tmp_path: Path, configured: Path) -> None:
    (tmp_path / 'card.pt').write_text('<b>${data}</b>')

    async def load(value: Any) -> Any:
        await asyncio.sleep(0.01)
        return value

    def fails() -> str:
        raise KeyError('now')

    page = PageTemplate(
        '${render_partial("card.pt", data=load(1))}${render_partial("card.pt", data=load(2))}${fails()}',
        concurrent=True,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with pytest.raises(KeyError):
            run(page.render_async(load=load, fails=fails))
        run(asyncio.sleep(0))
        gc.collect()
    assert _never_awaited(caught) == []


# -- Panics and interned names -----------------------------------------------------------------


class _Mutating:
    """A key whose hash changes the scope it is looked up in: PyO3's dict iterator panicked
    (`PanicException`, a `BaseException` that `except Exception` misses)."""

    def __init__(self, scope: Any) -> None:
        self.scope = scope

    def __hash__(self) -> int:
        self.scope.pop('b', None)
        return 1

    def __eq__(self, other: object) -> bool:
        return self is other


def test_a_scope_changed_while_copied_is_no_panic() -> None:
    from chameleon_rs._native import Scope

    scope = Scope()
    scope['a'] = 1
    scope[_Mutating(scope)] = 2  # ty: ignore[invalid-assignment] - a key that is not a str
    scope['b'] = 3
    try:
        scope.copy()
    except Exception:  # noqa: BLE001, S110 - any ordinary exception is fine; a panic is not
        pass


def test_a_scope_changed_during_a_macro_call_is_no_panic() -> None:
    layout = PageTemplate('<div metal:define-macro="m">${a}</div>')
    page = PageTemplate('<p tal:define="q grab(econtext)"><x metal:use-macro="layout.macros[\'m\']"/></p>')

    def grab(econtext: Any) -> str:
        econtext[_Mutating(econtext)] = 1
        econtext['b'] = 1
        return ''

    try:
        page.render(layout=layout, a=1, grab=grab)
    except Exception:  # noqa: BLE001, S110 - any ordinary exception is fine; a panic is not
        pass


def _mortal_interning() -> bool:
    is_immortal = getattr(sys, '_is_immortal', None)
    return is_immortal is not None and not is_immortal(sys.intern('chameleon_rs_probe_' + str(id(object()))))


@pytest.mark.skipif(not _mortal_interning(), reason='this CPython interns only immortally (3.12, free-threaded)')
@pytest.mark.parametrize(
    'body',
    [
        '<p>${%(n)s|nothing}</p>',
        '<p tal:define="%(n)s 1">${%(n)s}</p>',
        '<p tal:repeat="%(n)s [1]">${%(n)s}</p>',
        '<p>${o.%(n)s|nothing}</p>',
        '<p>${%(n)s.x.y|nothing}</p>',
    ],
)
def test_names_of_discarded_templates_are_freed(body: str) -> None:
    # Each distinct name a template used stayed for the life of the process: interned
    # immortally, by PyO3 and by CPython's parser. Templates generated per request grew without
    # bound.
    import uuid

    def churn(n: int) -> None:
        for _ in range(n):
            PageTemplate(body % {'n': 'qq' + uuid.uuid4().hex}).render(o=1)

    churn(300)
    gc.collect()
    before = sys.getallocatedblocks()
    churn(2000)
    gc.collect()
    assert (sys.getallocatedblocks() - before) / 2000 < 0.25


def test_bare_names_and_paths_compile_as_python_does() -> None:
    class O:
        x = type('Y', (), {'y': 'deep'})()
        match = 'soft keyword'

    body = (
        '${o.x.y} ${o.match} ${True} ${None|"none"} ${missing|"fallback"} ${_|"u"} ${o.__class__.__name__}'
        ' ${o.missing|"m"} ${nothing}'
    )
    want = chameleon.PageTemplate(body).render(o=O(), _='under')
    assert PageTemplate(body).render(o=O(), _='under') == want
    for bad in ('${o.if}', '${o..x}', '${o.}', '${if}'):
        with pytest.raises(chameleon_rs.exc.TemplateError):
            PageTemplate(bad).render(o=O())
