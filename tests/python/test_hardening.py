"""Fixes from the crash, leak and GC review (2026-10-23): what must not take down a web worker,
wedge a request, or keep memory.

- Renders nested through Python (a value's `__html__` or a helper rendering another template,
  as deep as the data) raise `RecursionError` before the native stack runs out, on a thread
  with a small stack; so does a compile cloning a deep fill-slot body.
- A macro with a code block called in a long `tal:repeat` renders in linear time.
- `str()` of a render error whose template file can't be decoded still shows the message.
- Second review: sparse Markdown tables, nested namespace declarations and long runs of
  whitespace take memory and time linear in the text; sibling partials of discarded templates
  are freed; a `@concurrent` call's own cancellation ends the render at once; a replaced hard
  link stops loading for the other name.
- Asynchronous renders: a cancellation during the join of cancelled tasks is not swallowed; a
  failed `@concurrent` call ends the render without waiting for slower calls before it; the
  fan-out is bounded; a `StopIteration` raised by the template is a `RuntimeError` (PEP 479),
  not the render's result; a second awaiter of a suspended render is an error; cancelled
  concurrent calls and partial arguments leave no "never awaited" warnings.
- Third review (2026-10-29): deep expression trees are freed and collected on small stacks; a
  value of several parts stays whole around a partial; loop countdowns don't wrap; a case and a
  partial's argument await a value once; thrown exceptions reach a render waiting for its
  calls; file identities survive shared inode numbers and package folders; the meta charset
  search is linear; error listings cut subclasses' values; cache directories never run.
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


@pytest.mark.parametrize('shape', ['a + ', 'not ', '-'])
@pytest.mark.parametrize('stack_kib', [192, 256, 384])
def test_an_expression_compiled_elsewhere_raises_on_a_small_stack(shape: str, stack_kib: int) -> None:
    # The compile's nesting budget is measured on the thread that compiles: the deepest
    # expression it allowed, rendered on a worker thread with a smaller stack, overflowed it
    # (SIGBUS) instead of raising.
    result = run_python(f"""
        import threading
        from chameleon_rs import PageTemplate

        pages = []
        for depth in range(50, 2000, 50):
            try:
                pages.append(PageTemplate('<p>${{' + {shape!r} * depth + 'a}}</p>'))
            except RecursionError:
                break

        def main():
            for page in pages:
                try:
                    page.render(a=1)
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


# -- Second review: memory and time a small input can take, cache lifetimes -------------------


def _peak_rss(code: str) -> tuple[str, int]:
    """Runs ``code`` in a fresh interpreter: what it prints, and its peak resident memory."""
    script = textwrap.dedent(code) + textwrap.dedent("""
        import resource, sys
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(peak if sys.platform == 'darwin' else peak * 1024)
    """)
    done = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=120, check=True)
    *out, peak = done.stdout.split()
    return ' '.join(out), int(peak)


def test_sparse_markdown_tables_take_bounded_memory() -> None:
    # Four tables of 700 columns and 700 one-cell rows, 22 KB: comrak filled in two million
    # cells, about 500 MB and 20 MB of HTML. Past the budget, the text converts without tables.
    out, peak = _peak_rss("""
        import chameleon_rs
        from chameleon_rs import PageTemplate
        chameleon_rs.configure(markdown=True, markdown_cache=0)
        one = '|' + 'a|' * 700 + '\\n|' + '-|' * 700 + '\\n' + '|x|\\n' * 700
        text = '\\n\\n'.join([one] * 4)
        html = PageTemplate('${markdown: text}').render(text=text)
        print(len(text), len(html), '<table' in html)
        small = '| a | b |\\n|---|---|\\n| 1 |\\n'
        print('<table>' in PageTemplate('${markdown: text}').render(text=small))
    """)
    size, html, has_table, small_table = out.split()
    assert has_table == 'False' and small_table == 'True'
    assert int(html) < 4 * int(size)
    assert peak < 200 * 1024 * 1024, peak


def test_nested_namespace_declarations_take_linear_memory() -> None:
    # Each start tag copied its parent's prefix map: 2,000 nested declarations (45 KB) took
    # 330 MB, 8,000 about 5 GB, before the compile rejected the depth.
    out, peak = _peak_rss("""
        from chameleon_rs import PageTemplate
        n = 20_000
        body = ''.join(f'<p xmlns:n{i}="x">' for i in range(n)) + '</p>' * n
        try:
            PageTemplate(body).render()
        except RecursionError:
            print('RecursionError')
    """)
    assert out == 'RecursionError'
    assert peak < 300 * 1024 * 1024, peak


def test_trailing_attribute_whitespace_compiles_in_linear_time() -> None:
    # 64K spaces after an attribute took 8 s: the attribute search, and the `<meta>` charset
    # search (any run of whitespace), each stepped one space at a time, rescanning the rest of
    # the run from each.
    body = '<p a="x"' + ' ' * 64_000 + '/>'
    start = time.perf_counter()
    assert PageTemplate(body).render() == body
    assert time.perf_counter() - start < 1


def test_a_cancelled_concurrent_call_ends_the_render_at_once() -> None:
    @concurrent
    async def cancelled() -> str:
        raise asyncio.CancelledError

    @concurrent
    async def slow() -> str:
        await asyncio.sleep(10)
        return 'never'

    page = PageTemplate('${cancelled()}${slow()}')
    start = time.perf_counter()
    with pytest.raises(asyncio.CancelledError):
        run(page.render_async(cancelled=cancelled, slow=slow))
    assert time.perf_counter() - start < 2


class _Translator:
    page: Any = None

    def __call__(self, msgid: Any, **kwargs: Any) -> str:
        return str(msgid)


def test_sibling_partials_of_discarded_templates_are_freed(tmp_path: Path) -> None:
    # The shared map of templates found next to a caller kept each one, and the settings in
    # its key (a `translate` function per template, here), for the life of the process.
    import weakref

    from chameleon_rs import partials

    (tmp_path / 'page.pt').write_text('${render_partial("card.pt")}')
    (tmp_path / 'card.pt').write_text('card')
    refs = []
    for _ in range(20):
        translator = _Translator()
        page = PageTemplateFile(tmp_path / 'page.pt', translate=translator)
        # A setting referring back to its caller: the cache key held the translator, and so
        # the caller, its partial cache and the sibling, which the weak value never freed.
        translator.page = page
        refs.append(weakref.ref(translator))
        assert page() == 'card'
    del translator, page
    gc.collect()
    assert [ref for ref in refs if ref() is not None] == []
    assert not any(str(tmp_path) in str(template.filename) for template in partials._SIBLINGS.values())


def test_sibling_partials_of_discarded_template_classes_are_freed(tmp_path: Path) -> None:
    # The key held the class, and a class attribute referring back to its caller kept the
    # class, the caller, its partial cache and the sibling alive.
    import weakref

    (tmp_path / 'page.pt').write_text('${render_partial("card.pt")}')
    (tmp_path / 'card.pt').write_text('card')
    refs = []
    for _ in range(20):
        translator = _Translator()
        cls = type('Page', (PageTemplateFile,), {'translator': translator})
        page = cls(tmp_path / 'page.pt')
        translator.page = page
        refs.append(weakref.ref(cls))
        assert page() == 'card'
    del translator, cls, page
    gc.collect()
    assert [ref for ref in refs if ref() is not None] == []


def test_a_changed_mutable_setting_is_not_compiled_into_a_shared_sibling(tmp_path: Path) -> None:
    # Keyed by `id`, one dict shared a sibling compiled with its old contents: the caller saw
    # the new value and its partial the old one.
    (tmp_path / 'page.pt').write_text('${greeting}:${render_partial("card.pt")}')
    (tmp_path / 'card.pt').write_text('${greeting}')
    builtins = {'greeting': 'first'}
    assert PageTemplateFile(tmp_path / 'page.pt', extra_builtins=builtins)() == 'first:first'
    builtins['greeting'] = 'second'
    assert PageTemplateFile(tmp_path / 'page.pt', extra_builtins=builtins)() == 'second:second'


def test_sibling_partials_are_still_shared(tmp_path: Path) -> None:
    (tmp_path / 'page.pt').write_text('${render_partial("card.pt")}')
    (tmp_path / 'card.pt').write_text('card')
    one, two = PageTemplateFile(tmp_path / 'page.pt'), PageTemplateFile(tmp_path / 'page.pt')
    assert one() == two() == 'card'
    assert one.__dict__['_partial_cache']['card.pt'][1] is two.__dict__['_partial_cache']['card.pt'][1]


def test_a_replaced_hard_link_no_longer_loads_for_the_other_name(tmp_path: Path) -> None:
    # `b.pt` loaded by the first spelling seen of its file, `a.pt`, even after a deployment
    # replaced `a.pt` (a new file renamed over it) and `b.pt` kept the old content.
    import os

    from chameleon_rs.starlette import Templates

    (tmp_path / 'a.pt').write_text('first')
    os.link(tmp_path / 'a.pt', tmp_path / 'b.pt')
    engine = Templates(str(tmp_path), auto_reload=True)
    assert engine.get_template('a.pt')() == engine.get_template('b.pt')() == 'first'
    (tmp_path / 'next.pt').write_text('replacement')
    os.replace(tmp_path / 'next.pt', tmp_path / 'a.pt')
    assert engine.get_template('b.pt')() == 'first'
    assert engine.get_template('a.pt')() == 'replacement'


@pytest.mark.parametrize('replaced', ['a', 'b'])
@pytest.mark.parametrize('folder', ['registered', 'caller'])
def test_a_replaced_hard_link_parts_from_the_other_partial(tmp_path: Path, folder: str, replaced: str) -> None:
    # Either name of the link replaced, found in the partials folder or next to the caller: each
    # name renders its own file. A cache hit checked only the template's file, and a shared
    # sibling was reused by the old file's identity.
    import os

    from chameleon_rs import _settings
    from chameleon_rs.partials import render_partial

    (tmp_path / 'a.pt').write_text('first')
    os.link(tmp_path / 'a.pt', tmp_path / 'b.pt')
    if folder == 'registered':
        chameleon_rs.configure(template_folder=str(tmp_path), auto_reload=True)

        def render(name: str) -> str:
            return render_partial(name).html_text

    else:
        (tmp_path / 'page.pt').write_text('${render_partial(name)}')
        page = PageTemplateFile(tmp_path / 'page.pt', auto_reload=True)

        def render(name: str) -> str:
            return page(name=name)

    try:
        assert render('a.pt') == render('b.pt') == 'first'
        (tmp_path / 'next.pt').write_text('replacement')
        os.replace(tmp_path / 'next.pt', tmp_path / f'{replaced}.pt')
        want = {name: (tmp_path / name).read_text() for name in ('a.pt', 'b.pt')}
        assert {name: render(name) for name in want} == want
    finally:
        _settings.clear()


@pytest.mark.parametrize('lookup', ['registered', 'caller', 'templates'])
def test_files_without_inode_numbers_are_not_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lookup: str
) -> None:
    # Filesystems without file IDs (FAT, exFAT, some network or FUSE mounts) report inode 0 for
    # every file: identified by device and inode, every name loaded the first file seen.
    import os

    from chameleon_rs import _settings
    from chameleon_rs.partials import render_partial
    from chameleon_rs.starlette import Templates

    real_stat = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = real_stat(path, *args, **kwargs)
        return os.stat_result((info.st_mode, 0, *tuple(info)[2:]))

    (tmp_path / 'login.pt').write_text('login')
    (tmp_path / 'profile.pt').write_text('profile')
    monkeypatch.setattr(os, 'stat', stat)
    if lookup == 'registered':
        chameleon_rs.configure(template_folder=str(tmp_path))

        def render(name: str) -> str:
            return render_partial(name).html_text

    elif lookup == 'caller':
        (tmp_path / 'page.pt').write_text('${render_partial(name)}')
        page = PageTemplateFile(tmp_path / 'page.pt')

        def render(name: str) -> str:
            return page(name=name)

    else:
        engine = Templates(str(tmp_path))

        def render(name: str) -> str:
            return engine.get_template(name)()

    try:
        assert [render('login.pt'), render('profile.pt'), render('login.pt')] == ['login', 'profile', 'login']
    finally:
        _settings.clear()


def test_a_template_subclass_that_reloads_is_not_name_cached(tmp_path: Path) -> None:
    # `get_template` remembers names only when templates don't reload: the loader's template
    # class decides (a subclass's own `auto_reload`), not the base class default.
    import os

    from chameleon_rs import PageTemplateLoader
    from chameleon_rs.litestar import ChameleonEngine

    class Reloading(PageTemplateFile):
        auto_reload = True

    (tmp_path / 'a.pt').write_text('first')
    os.link(tmp_path / 'a.pt', tmp_path / 'b.pt')
    engine = ChameleonEngine.from_loader(PageTemplateLoader(str(tmp_path), formats={'xml': Reloading}))
    assert engine.get_template('a.pt').render() == engine.get_template('b.pt').render() == 'first'
    (tmp_path / 'next.pt').write_text('replacement')
    os.replace(tmp_path / 'next.pt', tmp_path / 'a.pt')
    assert engine.get_template('b.pt').render() == 'first'


def test_more_files_than_the_name_caches_hold_compile_once_per_file(tmp_path: Path) -> None:
    # Past 1,024 files the map of file identities was cleared, while the loader kept every
    # template: each new spelling of a file (case, on a case-insensitive filesystem) compiled it
    # again. Identities of files that still exist are kept.
    from chameleon_rs.starlette import Templates

    count = 1030
    for i in range(count):
        (tmp_path / f'file{i:04d}.pt').write_text('x')
    if not (tmp_path / 'FILE0000.PT').exists():
        pytest.skip('a case-sensitive filesystem: each spelling is another file')
    engine = Templates(str(tmp_path), auto_reload=False)
    for pattern in ('file{:04d}.pt', 'FILE{:04d}.PT', 'File{:04d}.pt'):
        for i in range(count):
            assert engine.get_template(pattern.format(i))() == 'x'
        assert len(engine.loader.registry) == count


# -- Third review (2026-10-29) ---------------------------------------------------------------


def test_a_deep_expression_tree_is_freed_and_collected_on_a_small_stack() -> None:
    # An expression tree as deep as the compile allows was dropped, and traversed by the
    # collector, recursively on whatever thread did it: a 64 KiB thread died (SIGBUS).
    for mode in ('drop', 'collect'):
        result = run_python(f"""
            import gc, threading
            from chameleon_rs._native import Program
            depth = 1
            while True:
                try:
                    Program('<p>${{%s}}</p>' % ('string:${{' * (depth * 2) + 'x' + '}}' * (depth * 2)))
                except RecursionError:
                    break
                depth *= 2
            low, high = depth // 2, depth * 2
            while high - low > 1:
                mid = (low + high) // 2
                try:
                    Program('<p>${{%s}}</p>' % ('string:${{' * mid + 'x' + '}}' * mid))
                    low = mid
                except RecursionError:
                    high = mid
            holder = [Program('<p>${{%s}}</p>' % ('string:${{' * low + 'x' + '}}' * low))]
            threading.stack_size(64 * 1024)
            def work():
                holder.clear() if {mode!r} == 'drop' else gc.collect()
            thread = threading.Thread(target=work)
            thread.start()
            thread.join()
            print('ok', low)
        """)
        assert result.returncode == 0, (mode, result.returncode, result.stderr)
        assert result.stdout.startswith('ok'), result.stdout


_MACRO = '<div metal:define-macro="m"><p i18n:translate="">Hi <h1 metal:define-slot="s">def</h1>!</p></div>'
_USE = (
    '<i metal:use-macro="template.macros[\'m\']"><b metal:fill-slot="s"><u tal:on-error="string:ERR">{body}</u></b></i>'
)
# A partial whose own slot body writes a value of several parts.
_INNER = (
    '<div metal:define-macro="m2"><h1 metal:define-slot="s2">def</h1></div>'
    '<i metal:use-macro="template.macros[\'m2\']"><b metal:fill-slot="s2">${1} ${2}</b></i>'
)


def _boom() -> None:
    raise ValueError('boom')


@pytest.mark.parametrize(
    ('body', 'expected'),
    [
        ('${a} ${render_partial("inner.pt")} ${boom()}', '<u>ERR</u>'),
        ('<a href="${a} ${render_partial(\'inner.pt\')} ${boom()}">x</a>', '<a<u>ERR</u>'),
    ],
)
def test_a_partial_inside_a_value_of_several_parts_leaves_it_whole(configured: Path, body: str, expected: str) -> None:
    # A slot body's value of several parts that fails is taken back whole (Chameleon writes it
    # once all parts are evaluated). A native partial in one of the parts whose own slot body
    # had a value of several parts cleared the record of the outer one, and the parts written
    # before the error stayed. The expected outputs are Chameleon's (with chameleon_partials).
    (configured / 'page.pt').write_text(_MACRO + _USE.format(body=body))
    (configured / 'inner.pt').write_text(_INNER)
    page = PageTemplateFile(configured / 'page.pt')
    assert page(a='A', boom=_boom) == f'<div><p>Hi <h1>def</h1>!</p></div><div><p><b><u>{expected}</b>Hi !</p></div>'


def test_a_loop_length_of_the_smallest_int64_does_not_wrap() -> None:
    # `index -= 1; if index > 0` on Python's ints: the countdown never becomes positive. It was
    # an `i64` decremented with wrapping in release builds, so `-2**63` turned into `2**63 - 1`.
    source = '<ul><li tal:repeat="x xs">${x}</li></ul>'

    def repeat(key: str, iterable: Any) -> Any:
        return iter(iterable), -(2**63)

    expected = chameleon.PageTemplate(source).render(xs=[1, 2, 3], repeat=repeat)
    assert (
        PageTemplate(source).render(xs=[1, 2, 3], repeat=repeat)
        == expected
        == '<ul><li>1</li><li>2</li><li>3</li></ul>'
    )


async def _value(value: Any) -> Any:
    return value


def test_a_case_awaits_its_value_once() -> None:
    # `tal:case` evaluates its expression again for the `default` comparison (Chameleon's
    # `__condition is False` retry): a coroutine passed in was awaited twice.
    page = PageTemplate('<div tal:switch="2"><b tal:case="v">one</b><i tal:case="default">d</i></div>')
    assert run(page.render_async(v=_value(1))) == page.render(v=1) == '<div><i>d</i></div>'
    page = PageTemplate('<div tal:switch="1"><b tal:case="v">one</b><i tal:case="default">d</i></div>')
    assert run(page.render_async(v=_value(1))) == '<div><b>one</b></div>'


@pytest.mark.parametrize('concurrent', [False, True])
def test_a_partials_awaitable_argument_can_be_used_twice(configured: Path, concurrent: bool) -> None:
    # A coroutine passed to a native partial was bound as it is: the partial's second use of it
    # raised "cannot reuse already awaited coroutine".
    (configured / 'card.pt').write_text('<i>${v}${v}</i>')
    page = PageTemplate('<p>${render_partial("card.pt", v=v)}</p>', concurrent=concurrent)
    assert run(page.render_async(v=_value(1))) == '<p><i>11</i></p>'


def test_a_partial_not_found_leaves_no_argument_unawaited(configured: Path) -> None:
    page = PageTemplate('<p>${render_partial("missing.pt", v=v)}</p>')
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with pytest.raises(ValueError, match='not found'):
            run(page.render_async(v=_value(1)))
        gc.collect()
    assert not [w for w in caught if 'never awaited' in str(w.message)]


def test_an_exception_thrown_into_the_placeholder_wait_is_raised() -> None:
    # `throw()` of an `Exception` while the render waited for its concurrent calls was dropped,
    # and the render went on waiting; `GeneratorExit` with calls pending yielded again, which a
    # closing coroutine may not do.
    @concurrent
    async def slow() -> str:
        await asyncio.sleep(10)
        return 'never'

    async def drive(error: BaseException) -> BaseException | None:
        awaitable = render_page_async(PageTemplate('${slow()}${slow()}'), None, {'slow': slow}, False)
        assert not isinstance(awaitable, str)
        iterator = awaitable.__await__()
        next(iterator)
        try:
            yielded = iterator.throw(error)
        except BaseException as e:  # noqa: BLE001 - what the render raised
            return e
        # The other tasks are cancelled and joined first: the error comes after.
        try:
            iterator.send(None)
        except BaseException as e:  # noqa: BLE001
            return e
        del yielded
        return None

    raised = run(drive(ValueError('thrown')))
    assert isinstance(raised, ValueError), raised
    assert str(raised) == 'thrown'
    assert isinstance(run(drive(GeneratorExit())), GeneratorExit)


def test_a_key_that_changes_rcontext_while_a_macro_is_called_is_no_internal_error() -> None:
    # `before_macro` iterated `rcontext` itself while hashing each key against the scope: a key
    # whose `__hash__` added an entry made PyO3's iterator panic (`InternalError`).
    class Growing:
        rcontext: Any = None

        def __hash__(self) -> int:
            if Growing.rcontext is not None and 'grown' not in Growing.rcontext:
                Growing.rcontext['grown'] = 1
            return 1

    library = PageTemplate('<b metal:define-macro="m">m</b>')
    page = PageTemplate(
        '<p tal:define="global g 1; dummy python: rcontext.__setitem__(k, 2) or setattr(type(k), \'rcontext\', rcontext)">'
        '<i metal:use-macro="library.macros[\'m\']"/></p>'
    )
    assert page(library=library, k=Growing()) == '<p><b>m</b></p>'


def test_a_default_inside_i18n_target_reads_the_render_default() -> None:
    # `default` inside `i18n:target` is `target_language`; with none passed it is the render
    # default (`None`), as in Chameleon. It raised `NameError`: the alias was not counted as a
    # mention of the name, so the render's scope lacked the defaults.
    for source in ('<div i18n:target="x">${default}</div>', '<div i18n:target="x">${python: default}</div>'):
        assert PageTemplate(source)(x=1) == chameleon.PageTemplate(source)(x=1) == '<div></div>'
        assert PageTemplate(source)(x=1, target_language='de') == '<div>de</div>'


def test_an_out_of_range_surrogate_block_is_refused() -> None:
    from chameleon_rs import _native

    for base in (0xF101, 0xFFFFF800, 0xFFFFFFFF):
        with pytest.raises(ValueError, match='private-use block'):
            _native.Program('<p/>', surrogate_base=base)


def test_the_meta_charset_search_is_linear_in_the_number_of_heads() -> None:
    from chameleon.utils import RE_META
    from chameleon_rs.utils import _search_meta

    body = '<meta http-equiv=Content-Type content=' * 50_000 + ';x'
    start = time.perf_counter()
    assert _search_meta(body) is None
    assert time.perf_counter() - start < 1  # was minutes
    for text in (
        '<meta http-equiv="Content-Type" content="text/html; charset=latin-1" />',
        '<META HTTP-EQUIV=Content-Type CONTENT="a<meta http-equiv=Content-Type content=b; charset=x>',
        "<meta http-equiv='Content-Type' content=';charset=utf-8'>",
        '<meta http-equiv=Content-Type content=a; charset=b <meta http-equiv=Content-Type content=c; charset=d>',
    ):
        match = RE_META.search(text)
        assert _search_meta(text) == (match.groups() if match else None)


def test_long_values_of_subclasses_are_cut_in_render_errors() -> None:
    # The 53-character cut of the error's argument listing trusted `len()` of a `str` subclass
    # and left numbers to their own formatting, so a subclass could print any length.
    from chameleon_rs.utils import value_repr

    class Lying(str):
        def __len__(self) -> int:
            return 1

    class Big(int):
        def __str__(self) -> str:
            return 'X' * 100_000

    assert value_repr(Lying('S' * 1000)) == 'S' * 50 + '...'
    assert value_repr(Big(1)) == 'X' * 50 + '...'
    assert value_repr(True) is True
    assert value_repr(12) == 12
    with pytest.raises(NameError) as error:
        PageTemplate('${missing}')(big=Big(1), lying=Lying('S' * 100_000))
    assert len(str(error.value)) < 1000


def test_a_cache_directory_is_never_executed(tmp_path: Path) -> None:
    # `CHAMELEON_CACHE` makes templates carry Chameleon's `ModuleLoader` (its tests check
    # that), but chameleon-rs compiles natively and never loads a module from the directory: a
    # file planted there must not run.
    cache = tmp_path / 'cache'
    cache.mkdir()
    for name in ('__init__.py', 'hello.py', 'f00000.py'):
        (cache / name).write_text('raise SystemExit("executed")\n')
    (tmp_path / 'hello.pt').write_text('<p>${1}</p>')
    result = subprocess.run(
        [
            sys.executable,
            '-c',
            textwrap.dedent(f"""
                from chameleon_rs import PageTemplate, PageTemplateFile
                print(PageTemplateFile({str(tmp_path / 'hello.pt')!r})(), PageTemplate('<i/>', debug=True)())
            """),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**__import__('os').environ, 'CHAMELEON_CACHE': str(cache), 'CHAMELEON_DEBUG': '1'},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ['<p>1</p>', '<i/>']
    assert sorted(p.name for p in cache.iterdir()) == ['__init__.py', 'f00000.py', 'hello.py']


def test_files_with_one_shared_inode_number_are_not_one_file(tmp_path: Path, monkeypatch: Any) -> None:
    # Some FUSE, overlay and network mounts give different files the same nonzero inode: every
    # template was taken for the first one loaded.
    import os

    from chameleon_rs import partials
    from chameleon_rs.starlette import Templates

    (tmp_path / 'login.pt').write_text('login')
    (tmp_path / 'profile.pt').write_text('profile')
    real = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = real(path, *args, **kwargs)
        return os.stat_result((info.st_mode, 12345, 1, *tuple(info)[3:]))

    monkeypatch.setattr(os, 'stat', stat)
    engine = Templates(str(tmp_path))
    assert (engine.get_template('login.pt')(), engine.get_template('profile.pt')()) == ('login', 'profile')
    chameleon_rs.configure(template_folder=str(tmp_path))
    try:
        assert partials.render_partial('profile.pt').html_text == 'profile'
        assert partials.render_partial('login.pt').html_text == 'login'
    finally:
        chameleon_rs._settings.clear()


def test_a_package_folder_first_in_the_search_path_is_not_shadowed(tmp_path: Path, monkeypatch: Any) -> None:
    # The name cache identified a name by the first file found in the search path's directories,
    # skipping its package folders, which the loader tries in order: a hard link in the directory
    # to a file the package also has was served the package's file.
    import os

    from chameleon_rs.starlette import Templates

    package = tmp_path / 'mixpkg'
    (package / 'tpl').mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'tpl' / 'a.pt').write_text('package a')
    directory = tmp_path / 'dir'
    directory.mkdir()
    (directory / 'a.pt').write_text('directory a')
    os.link(directory / 'a.pt', directory / 'b.pt')
    monkeypatch.syspath_prepend(str(tmp_path))
    engine = Templates(['mixpkg:tpl', str(directory)])
    assert engine.get_template('a.pt')() == 'package a'
    assert engine.get_template('b.pt')() == 'directory a'


def test_a_registered_partial_subclass_or_a_lookalike_class_is_not_lowered_natively() -> None:
    # Only Chameleon's very classes, and exact partials of them, take the native fast path: a
    # `functools.partial` subclass and a class that only names itself after Chameleon's went
    # native, ignoring what they do.
    import functools

    import chameleon.tales
    from chameleon_rs.tales import _spec

    class Partial(functools.partial):  # type: ignore[type-arg]
        pass

    class Lookalike:
        __module__ = 'chameleon.tales'
        __qualname__ = 'PythonExpr'

    assert _spec(Partial(chameleon.tales.StringExpr))[0] == 'custom'
    assert _spec(Lookalike)[0] == 'custom'
    assert _spec(chameleon.tales.PythonExpr) == ('python',)
    assert _spec(functools.partial(chameleon.tales.StringExpr, braces_required=True)) == ('string', True)


def test_markdown_longer_than_the_profiles_limit_is_refused() -> None:
    chameleon_rs.configure(markdown={'markdown': True, 'md-short': {'max_length': 10}, 'md-any': {'max_length': None}})
    try:
        page = PageTemplate('${markdown: t}')
        assert page(t='*hi*') == '<p><em>hi</em></p>\n'
        with pytest.raises(ValueError, match='max_length of 1000000'):
            page(t='x' * 1_000_001)
        with pytest.raises(ValueError, match='max_length of 10'):
            PageTemplate('${md-short: t}')(t='x' * 11)
        assert len(PageTemplate('${md-any: t}')(t='x' * 1_000_001)) > 1_000_000
        with pytest.raises(TypeError, match='max_length'):
            chameleon_rs.configure(markdown={'markdown': {'max_length': -1}})
    finally:
        chameleon_rs._settings.clear()


@pytest.mark.skipif(not getattr(sys, '_is_gil_enabled', lambda: True)(), reason='functions defer refcounts here')
def test_a_reloaded_programs_fallback_functions_are_freed_without_the_collector(tmp_path: Path) -> None:
    # Each fallback function's globals held the function itself, a cycle: a reload left the old
    # program's functions for the collector. (Free-threaded builds give functions deferred
    # reference counts, so only the collector frees them there, whatever holds them.)
    import os
    import types

    def functions() -> int:
        return sum(isinstance(o, types.FunctionType) and '__rs_attrs' in o.__globals__ for o in gc.get_objects())

    path = tmp_path / 'page.pt'
    stamp = time.time_ns()
    path.write_text('<p>${(lambda: 0)()}</p>')
    os.utime(path, ns=(stamp, stamp))
    page = PageTemplateFile(path, auto_reload=True)
    page()
    gc.collect()
    before = functions()
    gc.disable()
    try:
        for i in range(1, 6):
            stamp += 1_000_000_000
            path.write_text('<p>${(lambda: ' + str(i) + ')()}</p>')
            os.utime(path, ns=(stamp, stamp))
            assert page() == f'<p>{i}</p>'
        assert functions() == before
    finally:
        gc.enable()


@pytest.mark.parametrize('case', ['limit', 'formatter'])
def test_a_helper_module_failing_to_import_is_raised_and_retried(case: str) -> None:
    # The concurrency limit fell back to 64 for good when its module failed to import (and was
    # never read again); a render error whose formatter failed to import raised that import
    # error instead of its own.
    result = run_python(f"""
        import asyncio, sys
        import chameleon_rs, chameleon_rs.config, chameleon_rs.exc
        from chameleon_rs import PageTemplate, concurrent

        @concurrent
        async def load():
            return 1

        name = 'chameleon_rs.config' if {case!r} == 'limit' else 'chameleon_rs.exc'
        page = PageTemplate('${{load()}}' if {case!r} == 'limit' else '${{1 // 0}}')
        render = (lambda: asyncio.run(page.render_async(load=load))) if {case!r} == 'limit' else page
        real = sys.modules[name]
        sys.modules[name] = None
        try:
            render()
        except BaseException as e:
            print(type(e).__name__)
        sys.modules[name] = real
        try:
            print(render())
        except BaseException as e:
            print(type(e).__name__, 'Expression' in str(e))
    """)
    assert result.returncode == 0, result.stderr
    if case == 'limit':
        assert result.stdout.split() == ['ModuleNotFoundError', '1']
    else:
        assert result.stdout.split() == ['ZeroDivisionError', 'ZeroDivisionError', 'True']
