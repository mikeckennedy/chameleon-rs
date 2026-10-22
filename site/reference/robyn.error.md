## robyn.error()


Ends the current `@template` handler with an error page: `template_file` rendered as


Usage

``` python
robyn.error(
    template_file,
    status_code=500,
    **template_data,
)
```


`text/html` with `message` (`'The URL resulted in an error.'`), `template_data` and `status_code`.
