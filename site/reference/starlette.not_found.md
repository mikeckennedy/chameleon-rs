## starlette.not_found()


Ends the current `@template` endpoint with a 404 page: `template_file` rendered with


Usage

``` python
starlette.not_found(
    template_file="errors/404.pt",
    **template_data,
)
```


`template_data`, as `text/html`.

Raises [chameleon_rs.web.NotFound](web.NotFound.md#chameleon_rs.web.NotFound), which the decorator turns into the response.
