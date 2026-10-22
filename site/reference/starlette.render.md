## starlette.render()


Renders a template (path relative to the template folder) to a `str`.


Usage

``` python
starlette.render(
    template_file,
    **template_data,
)
```


Raises [chameleon_rs.web.TemplateError](web.TemplateError.md#chameleon_rs.web.TemplateError) if nothing is configured.
