## flask.template()


Decorates a Flask or Quart view (sync or async) to render a template with what it


Usage

``` python
flask.template(
    template_file=None,
    content_type="text/html",
    status_code=200,
    *,
    mimetype=None,
    concurrent=None,
    offload=False
)
```


returns: bare (`@template`), `@template()` or `@template('home/index.pt', content_type=..., status_code=...)`. See the module docs.

`concurrent=True` (async views): native partials called with an awaitable argument render concurrently ([PageTemplate](PageTemplate.md#chameleon_rs.PageTemplate)'s `concurrent` setting, for this view; `False` turns a template's setting off, `None`, the default, keeps it). `offload=True` (async views, experimental): the page renders in a worker thread (`render_async(offload=True)`).
