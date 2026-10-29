# HTMX fragments

HTMX swaps a piece of the page with HTML the server returns. Write that piece as a partial, and you can render it in two places: inside the full page on the first load, and on its own when HTMX asks for it.

The fragment, `todos/list.pt`:

``` html
<ul id="todos">
  <li tal:repeat="todo todos">${todo}</li>
</ul>
```

The page, `todos/index.pt`, includes it:

``` html
<h1>Todos</h1>
<form hx-post="/todos" hx-target="#todos" hx-swap="outerHTML">
  <input name="title"><button>Add</button>
</form>
${render_partial('todos/list.pt', todos=todos)}
```

The views render the page on `GET` and only the fragment on `POST`:


``` python
import flask

import chameleon_rs
import chameleon_rs.flask as chameleon

app = flask.Flask(__name__)
chameleon.configure(template_folder=template_folder)
todos = ['Write docs']


@app.get('/todos')
@chameleon.template('todos/index.pt')
def index():
    return {'todos': todos}


@app.post('/todos')
def add():
    todos.append(flask.request.form['title'])
    return chameleon_rs.render_partial('todos/list.pt', todos=todos).html_text


client = app.test_client()
print(client.post('/todos', data={'title': 'Ship it'}).text)
```


    <ul id="todos">
      <li>Write docs</li>
      <li>Ship it</li>
    </ul>


[chameleon_rs.render_partial()](../reference/render_partial.md#chameleon_rs.render_partial) returns an [HTML](../reference/partials.HTML.md#chameleon_rs.partials.HTML) object. Its `html_text` is the markup. Because the fragment is one template file, the page and the HTMX response can't drift apart.

`chameleon.response('todos/list.pt', todos=todos)` does the same and returns a `flask.Response`, if you'd rather set a content type or status code.

[HTMX](../user-guide/htmx.md) covers more: one view that returns the page or the fragment from the `HX-Request` header, and `@template` on a fragment-only route.
