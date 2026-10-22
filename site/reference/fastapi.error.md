## fastapi.error()


Ends the current `@template` view with an error page: `template_file` rendered with


Usage

``` python
fastapi.error(
    template_file,
    status_code=500,
    **template_data,
)
```


`template_data` and `status_code`, as `text/html`.
