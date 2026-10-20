"""
Phase 1 spike driver (docs/plan.md, docs/spike-1.md). Measures the cost model behind decisions
D2-D9 using the throwaway `chameleon_spike` module (crates/spike).

Build the spike first:  (cd crates/spike && ../../venv/bin/maturin develop --release)
Run:                    venv/bin/python bench/spike1.py
"""

import asyncio
import sys
import time
import timeit
from pathlib import Path

import chameleon_spike as spike
from chameleon import PageTemplateFile
from chameleon.utils import Scope as ChameleonScope
from chameleon.utils import lookup_attr as chameleon_lookup_attr

sys.path.insert(0, str(Path(__file__).parent))
from bench_render import make_videos
from bench_split import _esc

TEMPLATE = Path(__file__).parent / 'templates' / 'home' / 'listing_loop.pt'
N = 500
results: dict[str, float] = {}


def best_per_call(fn, repeat: int = 7) -> float:
    n, _ = timeit.Timer(fn).autorange()
    return min(timeit.repeat(fn, number=n, repeat=repeat)) / n


def report(label: str, ns: float, note: str = '') -> None:
    results[label] = ns
    print(f'  {label:<58}{ns:10.1f} ns  {note}')


# ---------------------------------------------------------------------------------------
# The listing loop as a spike program. Static text is pre-joined, as the compiler will do.

LOOP_EXPRS = {
    'id': 'video.id',
    'join': "' '.join([])",
    'title': 'video.title',
    'author': 'video.author',
    'views': '"{:,}".format(video.views)',
}

# Chameleon-style fallback functions: every name through getname(), every attribute
# through lookup_attr(), exactly as its generated code does.
LOOP_FALLBACKS = {
    'id': lambda getname, lookup_attr: lookup_attr(getname('video'), 'id'),
    'join': lambda getname, lookup_attr: lookup_attr(' ', 'join')([]),
    'title': lambda getname, lookup_attr: lookup_attr(getname('video'), 'title'),
    'author': lambda getname, lookup_attr: lookup_attr(getname('video'), 'author'),
    'views': lambda getname, lookup_attr: lookup_attr('{:,}', 'format')(lookup_attr(getname('video'), 'views')),
}


def listing_ops(exprs: dict) -> list[tuple]:
    return [
        ('text', '<div class="container videos listing">\n    '),
        ('loop', 'video', 'videos' if isinstance(exprs['id'], str) else (lambda g, la: g('videos')), '\n    '),
        ('text', '<span class="video">\n        <div>\n            <a href="https://www.youtube.com/watch?v='),
        ('emit', exprs['id'], 'attr'),
        ('text', '" target="_blank">\n                <img src="https://img.youtube.com/vi/'),
        ('emit', exprs['id'], 'attr'),
        ('text', '/maxresdefault.jpg"\n                     class="img img-responsive '),
        ('emit', exprs['join'], 'attr'),
        ('text', '"\n                     alt="'),
        ('emit', exprs['title'], 'attr'),
        ('text', '"\n                     title="'),
        ('emit', exprs['title'], 'attr'),
        ('text', '">\n            </a>\n            <a href="https://www.youtube.com/watch?v='),
        ('emit', exprs['id'], 'attr'),
        ('text', '" target="_blank"\n               class="author">'),
        ('emit', exprs['author'], 'text'),
        ('text', '</a>\n            <div class="views">'),
        ('emit', exprs['views'], 'text'),
        ('text', ' views</div>\n        </div>\n    </span>'),
        ('endloop',),
        ('text', '\n</div>\n'),
    ]


def handwritten(videos) -> str:
    out = ['<div class="container videos listing">\n    ']
    a = out.append
    first = True
    for v in videos:
        if not first:
            a('\n    ')
        first = False
        vid = v.id.translate(_esc)
        title = v.title.translate(_esc)
        a(
            f'<span class="video">\n        <div>\n            <a href="https://www.youtube.com/watch?v={vid}" '
            f'target="_blank">\n                <img src="https://img.youtube.com/vi/{vid}/maxresdefault.jpg"\n'
            f'                     class="img img-responsive {" ".join([])}"\n                     alt="{title}"\n'
            f'                     title="{title}">\n            </a>\n            <a href="https://www.youtube.com/'
            f'watch?v={vid}" target="_blank"\n               class="author">{v.author.translate(_esc)}</a>\n'
            f'            <div class="views">{"{:,}".format(v.views)} views</div>\n        </div>\n    </span>'  # noqa: UP032
        )
    a('\n</div>\n')
    return ''.join(out)


def experiment_vm() -> None:
    print('\n[5] End-to-end listing loop, per item (n=500)')
    videos = make_videos(N)
    chameleon = PageTemplateFile(str(TEMPLATE))
    fast = spike.Program(listing_ops(LOOP_EXPRS))
    fallback = spike.Program(listing_ops(LOOP_FALLBACKS))
    builtins = {'len': len}
    want = chameleon(videos=videos)
    for name, got in (
        ('fast path', fast.render({'videos': videos}, builtins)),
        ('fallback', fallback.render({'videos': videos}, builtins)),
    ):
        assert got == want, f'{name} output differs from Chameleon'
    print('  (output of both spike programs is byte-identical to Chameleon)')
    report('Chameleon', best_per_call(lambda: chameleon(videos=videos)) / N * 1e9)
    report('hand-written Python', best_per_call(lambda: handwritten(videos)) / N * 1e9)
    report(
        'spike VM, fallback (Python function per expression)',
        best_per_call(lambda: fallback.render({'videos': videos}, builtins)) / N * 1e9,
    )
    report('spike VM, fast path', best_per_call(lambda: fast.render({'videos': videos}, builtins)) / N * 1e9)
    static = spike.Program([op for op in listing_ops(LOOP_EXPRS) if op[0] != 'emit'])
    report(
        'spike VM, static text and loop only (no expressions)',
        best_per_call(lambda: static.render({'videos': videos}, builtins)) / N * 1e9,
    )


PER_EXPRESSION = [
    # label, fast-path source, fallback function, escape context, plain-Python statement
    ('video.id', 'video.id', LOOP_FALLBACKS['id'], 'attr', 'v.id'),
    ('video.title (4 entities)', 'video.title', LOOP_FALLBACKS['title'], 'attr', 'v.title'),
    ('video.author', 'video.author', LOOP_FALLBACKS['author'], 'text', 'v.author'),
    ("' '.join([])", "' '.join([])", LOOP_FALLBACKS['join'], 'attr', "' '.join([])"),
    (
        '"{:,}".format(video.views)',
        '"{:,}".format(video.views)',
        LOOP_FALLBACKS['views'],
        'text',
        "'{:,}'.format(v.views)",
    ),
    ('video.views (exact int)', 'video.views', lambda g, la: la(g('video'), 'views'), 'text', 'v.views'),
]


def experiment_per_expression() -> None:
    print('\n[5b] Per expression: eval + emit inside the VM (8 copies per item), and plain Python eval only')
    videos = make_videos(N)

    def per_emit(op) -> float:
        prog = spike.Program([('loop', 'video', 'videos', ''), *[op] * 8, ('endloop',)])
        base = spike.Program([('loop', 'video', 'videos', ''), ('endloop',)])
        cost = best_per_call(lambda: prog.render({'videos': videos}, {})) / N * 1e9
        return (cost - best_per_call(lambda: base.render({'videos': videos}, {})) / N * 1e9) / 8

    v = videos[0]
    empty = best_per_call(lambda: None)
    print(f'  {"expression":<32}{"fast":>9}{"fallback":>10}{"saved":>8}{"ratio":>7}{"Python eval":>13}')
    for label, src, fn, esc, stmt in PER_EXPRESSION:
        fast = per_emit(('emit', src, esc))
        slow = per_emit(('emit', fn, esc))
        code = compile(f'lambda: {stmt}', '<expr>', 'eval')
        plain = (best_per_call(eval(code, {'v': v})) - empty) * 1e9
        results[f'per-expr fast {label}'] = fast
        results[f'per-expr saved {label}'] = slow - fast
        print(f'  {label:<32}{fast:8.1f} {slow:9.1f} {slow - fast:7.1f} {slow / fast:6.2f}x {plain:10.1f}')
    print('  (fast/fallback/saved: ns per expression; Python eval: ns for the bare expression, no emit)')


def experiment_escaping() -> None:
    """The escaping rules match Chameleon, including its gate and NUL quirks."""
    from chameleon import PageTemplate

    for value in ['a\0b', 'a&\0b', "it's", 'x"y', '<&>', 'plain', 'é & ü']:
        for template, ops in (
            ('<p>${x}</p>', [('text', '<p>'), ('emit', 'x', 'text'), ('text', '</p>')]),
            ('<p title="${x}"/>', [('text', '<p title="'), ('emit', 'x', 'attr'), ('text', '"/>')]),
        ):
            want = PageTemplate(template)(x=value)
            got = spike.Program(ops).render({'x': value}, {})
            assert got == want, (value, template, got, want)
    print('\n[4b] Escaping matches Chameleon for NUL, quotes, entities and non-ASCII (text and attribute)')


def experiment_micro() -> None:
    video = make_videos(1)[0]
    print('\n[1] Name lookup')
    root = spike.Scope({'video': video, 'videos': []})
    child = root.copy()
    report('native Scope, hit in local dict', spike.time_scope_lookup(child, 'video', 1_000_000))
    late_root = spike.Scope({})
    late_child = late_root.copy()
    late_root['video'] = video  # set after the copy: only the root has it
    report('native Scope, local miss then root hit', spike.time_scope_lookup(late_child, 'video', 1_000_000))
    report('native Scope, miss in both', spike.time_scope_lookup(spike.Scope({}).copy(), 'video', 1_000_000))
    report('plain dict get_item (reference)', spike.time_dict_lookup({'video': video}, 'video', 1_000_000))
    report(
        'new root Scope from a 2-item dict, in Rust (per partial call)',
        spike.time_scope_new_root({'video': video, 'classes': []}, 1_000_000),
    )
    small = {'video': video, 'classes': []}
    report('Python: Scope(d) for a 2-item dict (native class)', best_per_call(lambda: spike.Scope(small)) * 1e9)
    report('Python: dict(d) for a 2-item dict (reference)', best_per_call(lambda: dict(small)) * 1e9)
    getname = ChameleonScope({'video': video}).get_name
    report(
        "Chameleon: getname('video') from Python",
        best_per_call(lambda: getname('video')) * 1e9,
        '(includes the Python call)',
    )

    print('\n[2] Attribute access with lookup_attr semantics')
    report('Rust lookup_attr(video, "id")', spike.time_lookup_attr(video, 'id', 1_000_000))
    report(
        'Rust lookup_attr(dict, "id") (falls back to __getitem__)', spike.time_lookup_attr({'id': 1}, 'id', 1_000_000)
    )
    report(
        'Chameleon: lookup_attr(video, "id") from Python',
        best_per_call(lambda: chameleon_lookup_attr(video, 'id')) * 1e9,
    )

    print('\n[3] Fallback: one Python function call per expression')
    nscope = spike.Scope({'video': video})
    report(
        'call f(getname, lookup_attr) -> video.id',
        spike.time_call2(LOOP_FALLBACKS['id'], nscope.get_name, spike.lookup_attr, 1_000_000),
    )

    print('\n[4] Text path: escape + append + final str, per string')
    plain = [f'Author number {i}' for i in range(1000)]
    escapes = [f'Video {i} about Python & "templates" <fast>' for i in range(1000)]
    unicode = [f'Vidéo numéro {i} sur Python' for i in range(1000)]
    report('ASCII, nothing to escape', spike.time_escape(plain, False, 200))
    report('ASCII with & < > " (attribute)', spike.time_escape(escapes, True, 200))
    report(
        'non-ASCII, nothing to escape', spike.time_escape(unicode, False, 200), '(UTF-8 view cached after first pass)'
    )
    report(
        "Python: list.append + ''.join (no escaping)", best_per_call(lambda: ''.join([s for s in plain])) / 1000 * 1e9
    )

    s = ''.join(['é'] * 10_000)
    before = sys.getsizeof(s)
    spike.utf8_encode_len(s)
    after_encode = sys.getsizeof(s)
    spike.utf8_view_len(s)
    after_view = sys.getsizeof(s)
    print(
        f'  non-ASCII str of 10,000 chars: {before:,} B; after encode_utf8: {after_encode:,} B; '
        f'after to_str (cached view): {after_view:,} B'
    )
    fresh = [''.join(['é', str(i)] * 50) for i in range(1000)]
    report(
        'to_str() on fresh non-ASCII strings (creates the cache)',
        best_per_call(lambda: [spike.utf8_view_len(x) for x in fresh]) / 1000 * 1e9,
        '(incl. Python loop)',
    )
    report(
        'encode_utf8() on non-ASCII strings (no cache)',
        best_per_call(lambda: [spike.utf8_encode_len(x) for x in fresh]) / 1000 * 1e9,
        '(incl. Python loop)',
    )

    print('\n[7] Values in VmState: Bound vs Py (store + load)')
    bound, py = spike.time_bound_vs_py([video] * 1000, 2000)
    report('Vec<Bound> store + read', bound)
    report('Vec<Py> store (unbind) + read (bind)', py)

    print('\n[8] Per-render wrapper overhead')
    report('bare native call noop(**kw)', best_per_call(lambda: spike.noop(videos=None, x=1)) * 1e9)

    class Wrapper:
        """What chameleon_rs's PageTemplate.render must do before calling native code."""

        translate = None

        def render(self, encoding=None, **kw):
            translate = kw.get('translate') or self.translate
            kw.setdefault('__translate', translate)
            kw.setdefault('__decode', bytes.decode)
            kw.setdefault('__on_error_handler', None)
            kw.setdefault('target_language', None)
            return spike.noop(**kw)

    w = Wrapper()
    report('Python wrapper render(**kw) -> native', best_per_call(lambda: w.render(videos=None, x=1)) * 1e9)
    empty = PageTemplateFile(str(Path(__file__).parent / 'templates' / 'shared' / 'partials' / 'video_image.pt'))
    v1 = make_videos(1)[0]
    report(
        'Chameleon: full render of video_image.pt (smallest partial)',
        best_per_call(lambda: empty(video=v1, classes=[])) * 1e9,
    )


# ---------------------------------------------------------------------------------------
# Experiment 6: async


async def fetch(n, delay=0.01):
    await asyncio.sleep(delay)
    return n


ASYNC_OPS = [
    ('text', 'a'),
    ('emit', 'fetch(1)', 'text'),
    ('text', 'b'),
    ('emit', 'sync_value', 'text'),
    ('emit', 'fetch(2)', 'text'),
    ('text', 'c'),
]


async def render_async(program, vars):
    """The D9 shape: an `async def` around the native awaitable, so create_task accepts it."""
    return await program.render_async(vars, {})


def experiment_async() -> None:
    print('\n[6] Async proof')
    program = spike.Program(ASYNC_OPS)

    async def basic():
        lags = []
        stop = False

        async def ticker():
            while not stop:
                t0 = time.perf_counter()
                await asyncio.sleep(0.001)
                lags.append(time.perf_counter() - t0 - 0.001)

        t = asyncio.create_task(ticker())
        out = await asyncio.create_task(render_async(program, {'fetch': fetch, 'sync_value': '<x>'}))
        stop = True
        await t
        return out, max(lags)

    out, worst_lag = asyncio.run(basic())
    assert out == 'a1b&lt;x&gt;2c', out
    print(f'  render via create_task: {out!r} (correct); ticker kept running, worst lag {worst_lag * 1e3:.2f} ms')

    async def cancellation():
        state = {'cleaned_up': False}

        async def slow(n):
            try:
                await asyncio.sleep(10)
            finally:
                state['cleaned_up'] = True
            return n

        task = asyncio.create_task(render_async(program, {'fetch': slow, 'sync_value': ''}))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return 'CancelledError', state['cleaned_up']
        return 'no error', state['cleaned_up']

    outcome, cleaned = asyncio.run(cancellation())
    assert outcome == 'CancelledError' and cleaned, (outcome, cleaned)
    print('  cancellation: task.cancel() -> throw() delegated -> inner awaitable cancelled, CancelledError raised')

    async def failure():
        async def boom(n):
            await asyncio.sleep(0)
            raise ValueError(f'boom {n}')

        try:
            await render_async(program, {'fetch': boom, 'sync_value': ''})
        except ValueError as e:
            return str(e)

    assert asyncio.run(failure()) == 'boom 1'
    print('  exception from an awaited value propagates out of the render')

    async def many():
        t0 = time.perf_counter()
        outs = await asyncio.gather(
            *[render_async(program, {'fetch': lambda n: fetch(n, 0.02), 'sync_value': str(i)}) for i in range(100)]
        )
        return time.perf_counter() - t0, outs

    elapsed, outs = asyncio.run(many())
    assert outs[7] == 'a1b72c'
    print(
        f'  100 concurrent renders, each awaiting 2 x 20 ms: {elapsed * 1e3:.0f} ms total '
        f'(sequential would be {100 * 2 * 20:,} ms)'
    )

    async def reentry():
        aw = program.render_async({'fetch': fetch, 'sync_value': ''}, {})
        await aw
        try:
            await aw
        except RuntimeError as e:
            return str(e)

    print(f'  awaiting a finished render again: RuntimeError({asyncio.run(reentry())!r})')


def thresholds() -> None:
    print('\nGo / no-go (thresholds registered in docs/plan.md before the spike)')
    item = results['spike VM, fast path']
    chameleon = results['Chameleon']
    static = results['spike VM, static text and loop only (no expressions)']
    fallback_item = results['spike VM, fallback (Python function per expression)']
    fast_exprs = item - static
    fallback_exprs = fallback_item - static
    print(
        f'  1. listing item {item:.0f} ns vs 600 ns -> {"GO" if item <= 600 else "NO-GO"} '
        f'({chameleon / item:.1f}x faster than Chameleon)'
    )
    print(f'     fallback-only VM: {fallback_item:.0f} ns -> {"GO" if fallback_item <= 600 else "NO-GO"} on its own')
    ratio = fallback_exprs / fast_exprs
    print(
        f'  2. expressions, fast {fast_exprs:.0f} vs fallback {fallback_exprs:.0f} ns per item: {ratio:.1f}x '
        f'vs 3x -> {"GO" if ratio >= 3 else "NO-GO"}'
    )
    saved = [v for k, v in results.items() if k.startswith('per-expr saved')]
    print(
        f'     fast path saves {min(saved):.0f}-{max(saved):.0f} ns per expression '
        f'(new D4 criterion: revisit if under 20 ns)'
    )
    print('  3. async proof: see [6] (all assertions passed)')


if __name__ == '__main__':
    print(f'Python {sys.version.split()[0]}, GIL enabled: {sys._is_gil_enabled()}')
    experiment_vm()
    experiment_per_expression()
    experiment_escaping()
    experiment_micro()
    experiment_async()
    thresholds()
