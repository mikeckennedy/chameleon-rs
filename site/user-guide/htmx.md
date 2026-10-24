# HTMX

[HTMX](https://htmx.org) swaps part of a page with an HTML fragment the server returns. chameleon-rs has what that needs built in. [Partials](partials.md) (chameleon_partials, built in) are the fragments, and every framework integration can return one on its own. There is nothing extra to install or register.

The pattern is the one chameleon_partials was written for. Put the piece of the page HTMX replaces in its own template. The full page includes it with [render_partial](../reference/render_partial.md#chameleon_rs.render_partial), and the view HTMX calls renders only that template. Because both use the same file, the first page load and the HTMX response can't drift apart.


# One template, two uses

A search box that updates the results as you type. The fragment, `search/results.pt`:

``` html
<ul id="results">
  <li tal:repeat="episode results">${episode}</li>
</ul>
```

The page, `search/index.pt`, includes it:

``` html
<input type="search" name="q" value="${q}"
       hx-get="/search" hx-trigger="input changed delay:300ms"
       hx-target="#results" hx-swap="outerHTML" hx-push-url="true">
${render_partial('search/results.pt', results=results)}
```

The partial renders as a native frame of the page (see [Partials](partials.md)), so including it costs about the same as writing its markup inline.


# Full page or fragment from one view

HTMX sends an `HX-Request: true` header with every request it makes. One view can check it, returning the fragment to HTMX and the whole page to everyone else: a first visit, a reload, or a shared link to a URL that `hx-push-url` put in the address bar. With FastAPI:


``` python
import fastapi

import chameleon_rs.fastapi as fastapi_chameleon

app = fastapi.FastAPI()
fastapi_chameleon.configure(template_folder=template_folder, app=app)

EPISODES = ['Python 3.14', 'Rust for Pythonistas', 'HTMX in practice']


async def find(q: str) -> list[str]:
    return [e for e in EPISODES if q.lower() in e.lower()]


def wants_fragment(request: fastapi.Request) -> bool:
    headers = request.headers
    return 'HX-Request' in headers and 'HX-History-Restore-Request' not in headers


@app.get('/search')
async def search(request: fastapi.Request, q: str = ''):
    results = await find(q)
    if wants_fragment(request):
        return await fastapi_chameleon.response_async('search/results.pt', results=results)
    return await fastapi_chameleon.response_async('search/index.pt', q=q, results=results)
```


``` python
from fastapi.testclient import TestClient

client = TestClient(app)
print(client.get('/search', params={'q': 'py'}, headers={'HX-Request': 'true'}).text)
```


    <ul id="results">
      <li>Python 3.14</li>
      <li>Rust for Pythonistas</li>
    </ul>


When the back button finds a page missing from its history cache, HTMX fetches it again with `HX-History-Restore-Request: true` as well and expects the whole page, which is why `wants_fragment` checks for that header too. If a cache sits in front of the app, send `Vary: HX-Request` so it doesn't serve one response in place of the other.

Every integration has the same [response()](../reference/flask.response.md#chameleon_rs.flask.response) and [response_async()](../reference/robyn.response_async.md#chameleon_rs.robyn.response_async): `chameleon_rs.flask`, `chameleon_rs.starlette`, `chameleon_rs.robyn` and `chameleon_rs.litestar` as well as `chameleon_rs.fastapi`.


# A view that only returns the fragment

When a route exists only for HTMX, such as an `hx-post` that adds a row, put `@template` on the fragment itself:

``` python
@app.post('/todos')                          # the route decorator first...
@fastapi_chameleon.template('todos/list.pt') # ...then @template, naming the fragment
async def add(title: str = fastapi.Form()):
    await db.add_todo(title)
    return {'todos': await db.todos()}
```

A partial is an ordinary template, so the decorators render it like any page, async views included: an `async def` view renders with [render_async](../reference/robyn.render_async.md#chameleon_rs.robyn.render_async) and doesn't block the event loop (see [Async rendering](async.md)).


# From Python, without a framework

`chameleon_rs.render_partial(path, **model)` renders a fragment anywhere: an HTMX view in a framework without an integration, a WebSocket or server-sent events handler pushing HTML, or a test. It returns an [HTML](../reference/partials.HTML.md#chameleon_rs.partials.HTML) object whose `html_text` is the markup:


``` python
import chameleon_rs

fragment = chameleon_rs.render_partial('search/results.pt', results=['HTMX in practice'])
print(fragment.html_text)
```


    <ul id="results">
      <li>HTMX in practice</li>
    </ul>


From Python the path is looked up in the configured template folder only, as with chameleon_partials. The [HTMX fragments](../recipes/htmx-fragments.md) recipe shows the same idea with Flask.
