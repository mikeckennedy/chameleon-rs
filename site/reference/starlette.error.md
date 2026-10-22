## starlette.error()


Ends the current `@template` endpoint with an error page: `template_file` rendered


Usage

``` python
starlette.error(
    template_file,
    status_code=500,
    **template_data,
)
```


with `template_data` and `status_code`, as `text/html`.

Raises [chameleon_rs.web.ErrorPage](web.ErrorPage.md#chameleon_rs.web.ErrorPage), which the decorator turns into the response.
