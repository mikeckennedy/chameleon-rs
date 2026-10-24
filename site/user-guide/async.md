# Async rendering

Chameleon renders synchronously. In an async web app (FastAPI, Starlette, Quart, Litestar, Robyn) that means a render runs on the event loop, and nothing else in that worker runs until it finishes. chameleon-rs keeps [render()](../reference/flask.render.md#chameleon_rs.flask.render) exactly as it was and adds [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async), which renders on the event loop without holding it while it waits for data.

The framework integrations use it for you: an `async def` view decorated with `@template` renders with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async). This page explains what that does, and the opt-in extras built on it.


# [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async)

`await template.render_async(**model)` renders like [render()](../reference/flask.render.md#chameleon_rs.flask.render), except that when a value the template uses is awaitable (a coroutine, task or future), the render awaits it right there and continues with the result. While it waits, the event loop serves other requests.


``` python
import asyncio

from chameleon_rs import PageTemplate

page = PageTemplate("""\
<h1>${title}</h1>
<ul>
  <li tal:repeat="episode episodes">${episode}</li>
</ul>
""")


async def latest_episodes():
    await asyncio.sleep(0.01)   # a database query, say
    return ['Rust for Python devs', 'Async templates']


print(await page.render_async(title='Latest', episodes=latest_episodes()))
```


    <h1>Latest</h1>
    <ul>
      <li>Rust for Python devs</li>
      <li>Async templates</li>
    </ul>


`latest_episodes()` is passed unawaited. The render reaches `tal:repeat`, awaits it, and repeats over the list it returns.

With no awaitable values, [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) gives exactly [render()](../reference/flask.render.md#chameleon_rs.flask.render)'s output. That's tested against every Chameleon fixture template and the render fuzzers, so switching a view from [render()](../reference/flask.render.md#chameleon_rs.flask.render) to [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) changes nothing until you pass it something to await.

> **Tip: Loading data in the view is still a good default**
>
> You don't have to hand coroutines to the template. Loading the data in the view (or a view model) with `asyncio.gather` keeps data access in one place and makes the template a pure function of its model:
>
> ``` python
> async def episode_page(number: int):
>     episode, related = await asyncio.gather(db.episode(number), db.related(number))
>     return {'episode': episode, 'related': related}
> ```
>
> [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) still matters then: the integrations need no change in your views, and a big page can render in a thread (see [Offloading](#offloading-a-render-to-a-thread)). Without that, the render itself runs on the loop: an async render yields only where a value suspends, so a page that is all CPU work (a 500,000-row table) holds the loop until it is done, as a synchronous render would. Passing awaitables to the template is useful when a value is only sometimes used (a sidebar behind a `tal:condition`), so it is never loaded when the page doesn't show it.


# What gets awaited

Only an expression's **final value** is awaited:

| Template | With `episodes=load()` (a coroutine) |
|----|----|
| `${episodes}`, `tal:content="episodes"` | Awaited, then inserted. |
| `tal:repeat="e episodes"` | Awaited, then repeated over. |
| `tal:condition="episodes"` | Awaited, and the condition tested on the result. |
| `tal:define="items episodes"` | Awaited once; `items` is the result. |
| `${episodes.count}` | Not awaited: `.count` is looked up on the coroutine, an `AttributeError` as in a sync render. |
| `${len(episodes)}` | Not awaited: `len()` gets the coroutine. |

So pass the awaitable the template needs as a whole, or `tal:define` it first and use the result:


``` python
page = PageTemplate("""\
<p tal:define="items episodes">${len(items)} episodes, the first is ${items[0]}.</p>
""")
print(await page.render_async(episodes=latest_episodes()))
```


    <p>2 episodes, the first is Rust for Python devs.</p>


Async iterables (an `async def` generator) aren't supported in `tal:repeat`, as they aren't iterable in a sync render either. Collect them in a helper first: `[x async for x in source]`.


# Errors, macros and partials

Everything in the language works across a wait:

- **Macros and slots.** A layout's macro and the page's slot fills can await values.
- **Partials.** `${render_partial('shared/card.pt', item=item)}` renders natively, and the partial can await values of its own.
- **`tal:on-error`.** An exception raised by an awaited value is raised where the expression is, so the nearest `tal:on-error` handles it, as for any other error.
- **Cancellation.** Cancelling the task that runs the render cancels the value being awaited.


``` python
async def broken():
    raise LookupError('the recommendations service is down')


page = PageTemplate("""\
<aside tal:on-error="string:Recommendations are unavailable.">
  <p tal:repeat="r recommendations">${r}</p>
</aside>
""")
print(await page.render_async(recommendations=broken()))
```


    <aside>Recommendations are unavailable.</aside>


# Running helpers concurrently

By default the render awaits one value at a time, in document order. A page that calls three slow helpers waits for each in turn. Two opt-in settings let them run at the same time. Both change the order in which your code runs, which is why they're off by default.


## `@chameleon_rs.concurrent` helpers

Decorate an async helper with `@chameleon_rs.concurrent`, and each call to it written as a whole `${...}` starts as a task. The render writes a placeholder and keeps going, and each placeholder is filled in, escaped as usual, when the render reaches the end:


``` python
import time

import chameleon_rs


@chameleon_rs.concurrent
async def fetch(name, delay):
    await asyncio.sleep(delay)
    return f'{name} ({delay}s)'


page = PageTemplate("""\
<p>${fetch('downloads', 0.2)}</p>
<p>${fetch('reviews', 0.2)}</p>
<p>${fetch('sponsors', 0.2)}</p>
""")

start = time.perf_counter()
print(await page.render_async(fetch=fetch))
print(f'{time.perf_counter() - start:.1f}s, not 0.6s')
```


    <p>downloads (0.2s)</p>
    <p>reviews (0.2s)</p>
    <p>sponsors (0.2s)</p>

    0.2s, not 0.6s


A call runs concurrently only where its output can simply be written later: a whole `${...}` in text, or one inside an attribute value. Anywhere its value decides what the template does (a condition, a `tal:define`, `tal:content` / `tal:replace`, which may choose the default content) or where the output might be taken back (inside `tal:on-error` or a translation), it is awaited in place, like any awaitable.

If a concurrent call raises, the render waits for nothing else: the error is raised as soon as the render has reached its end and that call has failed, even if calls before it in the page are still running. The other tasks are cancelled and their `finally` blocks run before the error is raised, as in a `TaskGroup`. If the request is cancelled meanwhile, the `CancelledError` (or `asyncio.timeout`'s `TimeoutError`) wins over the template error. In a sync [render()](../reference/flask.render.md#chameleon_rs.flask.render) the decorator has no effect.

One render starts at most 64 tasks for concurrent calls and partials (set `CHAMELEON_CONCURRENT_LIMIT` to change it). Past that, each further call is awaited where it stands, so a `tal:repeat` over request-sized data can't start an unbounded number of queries at once.


## Partials in parallel: `concurrent=True`

A template created with `concurrent=True` also renders its `render_partial(...)` calls concurrently, when a call has an awaitable argument. Each such partial renders as a render of its own, in a task:


``` python
import pathlib, tempfile

folder = pathlib.Path(tempfile.mkdtemp())
(folder / 'card.pt').write_text('<div class="card">${data}</div>')
chameleon_rs.configure(template_folder=str(folder))


async def load(name):
    await asyncio.sleep(0.2)
    return name


page = PageTemplate("""\
<section tal:repeat="name names">${render_partial('card.pt', data=load(name))}</section>
""", concurrent=True)

start = time.perf_counter()
print(await page.render_async(names=['a', 'b', 'c'], load=load))
print(f'{time.perf_counter() - start:.1f}s')
```


    <section><div class="card">a</div></section>
    <section><div class="card">b</div></section>
    <section><div class="card">c</div></section>

    0.2s


The integrations take the same setting on the decorator: `@template('page.pt', concurrent=True)`, and `TemplateResponse_async(..., concurrent=True)` for Starlette's [Templates](../reference/starlette.Templates.md#chameleon_rs.starlette.Templates).


# Offloading a render to a thread

Experimental. `render_async(..., offload=True)` (or `@template(..., offload=True)`) runs the render in a worker thread (the event loop's default executor) and awaits it. The render is synchronous there, so awaitable values aren't awaited: load the data first.

- **On free-threaded Python (3.14t, 3.15t)** renders then run in parallel with the event loop and with each other. chameleon-rs ships free-threaded wheels and doesn't turn the GIL back on.
- **On a regular (GIL) build** it helps only pages dominated by long text, whose escaping releases the GIL. For most pages, a thread hop costs more than it saves.
- **Cancellation**: a render still waiting for a thread is dropped when its request is cancelled (a client disconnect), but one already running in a thread can't be interrupted: it finishes and its result is discarded.


# In the web frameworks

Every integration renders `async def` views with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async), so all of the above applies to the model a view returns:

| Integration | `async def` view | Plain `def` view |
|----|----|----|
| [Flask and Quart](flask.md) | [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) (Flask's async views, Quart) | [render()](../reference/flask.render.md#chameleon_rs.flask.render) |
| [FastAPI](fastapi.md) | [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) on the event loop | [render()](../reference/flask.render.md#chameleon_rs.flask.render) in FastAPI's thread pool |
| [Starlette](starlette.md) | [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async), and `await templates.TemplateResponse_async(...)` | [render()](../reference/flask.render.md#chameleon_rs.flask.render) in Starlette's thread pool |
| [Robyn](robyn.md) | [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) on Robyn's event loop | [render()](../reference/flask.render.md#chameleon_rs.flask.render) on Robyn's worker thread |
| [Litestar](litestar.md) | [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) with `@template`; [ChameleonEngine](../reference/litestar.ChameleonEngine.md#chameleon_rs.litestar.ChameleonEngine) (Litestar's [Template](../reference/litestar.Template.md#chameleon_rs.litestar.Template) responses) always renders synchronously | [render()](../reference/flask.render.md#chameleon_rs.flask.render), in a thread with `sync_to_thread=True` |

Each module also has [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) and [response_async()](../reference/robyn.response_async.md#chameleon_rs.robyn.response_async), the coroutine versions of [render()](../reference/flask.render.md#chameleon_rs.flask.render) and [response()](../reference/flask.response.md#chameleon_rs.flask.response).

> **Note: A difference from the original libraries**
>
> chameleon-flask, fastapi-chameleon and chameleon-robyn rendered async views synchronously, so an awaitable in the model was inserted as the object itself (`<coroutine object ...>`). The built-ins await it. That includes test doubles such as `AsyncMock()` in a model.


# Things to know

- **`markdown:` and other expression types** follow the same rule: the expression's final value is awaited. Load Markdown text before rendering rather than passing a coroutine for `markdown:` to convert.
- **A subclass that overrides [render()](../reference/flask.render.md#chameleon_rs.flask.render)** keeps its override: [render_async()](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) calls it, synchronously, so awaitable values aren't awaited. A monkeypatched [PageTemplate.render](../reference/PageTemplate.render.md#chameleon_rs.PageTemplate.render) (to time renders, say) counts as an override. See [Migrating](migrating.md#timing-renders).
- **[render()](../reference/flask.render.md#chameleon_rs.flask.render) never awaits.** In a sync render a coroutine in the model is just an object, as in Chameleon.
