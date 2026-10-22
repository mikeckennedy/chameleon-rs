## litestar.response()


Renders a template into a `litestar.Response` (every keyword but `content_type` and


Usage

``` python
litestar.response(
    template_file, content_type="text/html", status_code=None, **template_data
)
```


`status_code` is the model). `status_code=None`: the route's (200, 201 for `@post`).
