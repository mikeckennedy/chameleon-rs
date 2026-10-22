## PageTemplate.render_async()


Render template to string, awaiting awaitable values (D9).


Usage

``` python
PageTemplate.render_async(
    encoding=None,
    *,
    offload=False,
    **kw,
)
```


The same as [render](flask.render.md#chameleon_rs.flask.render), except that when an expression's final value is awaitable (a coroutine, a task, a future, anything `await` accepts), the render suspends until the value is awaited and continues with its result. Values inside an expression are not awaited (`coro().title` is the coroutine's attribute), and a condition is tested on the awaited value. An error raised by an awaited value is raised where the expression is, so `tal:on-error` handles it; cancellation passes through.

Opt-in concurrency: calls of `@chameleon_rs.concurrent` helpers written as a whole `${...}` are awaited concurrently, and with the template setting `concurrent=True` native partials called with an awaitable argument render concurrently (see `chameleon_rs._async`).

Experimental, opt-in: `offload=True` runs [render](flask.render.md#chameleon_rs.flask.render) (synchronous: awaitable values are not awaited) in the event loop's default executor, a worker thread, and awaits it. On a free-threaded build renders then run in parallel with the event loop and each other; on a GIL build it helps only renders dominated by long text, whose escaping releases the GIL (docs/plan.md, Phase 11 results). `offload` is therefore not a template variable here.
