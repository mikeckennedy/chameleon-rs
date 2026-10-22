## robyn.render()


Renders a template (path relative to the template folder) to a `str` (no response):


Usage

``` python
robyn.render(
    template_file,
    **template_data,
)
```


for emails, middleware, error handlers.

Raises `ChameleonRobynException` if nothing is configured.
