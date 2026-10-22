## flask.template()


Decorates a Flask or Quart view (sync or async) to render a template with what it


Usage

``` python
flask.template(
    template_file=None,
    content_type="text/html",
    status_code=200,
    *,
    mimetype=None
)
```


returns: bare (`@template`), `@template()` or `@template('home/index.pt', content_type=..., status_code=...)`. See the module docs.
