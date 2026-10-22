## web.TemplateNotFound


No template of that name, or a name reaching outside the template folder: raised by


Usage

``` python
web.TemplateNotFound()
```


[chameleon_rs.starlette.Templates](starlette.Templates.md#chameleon_rs.starlette.Templates) (the `Jinja2Templates` drop-in), with the message `Template not found: {name}.`; code moving from Jinja catches it where it caught `jinja2.TemplateNotFound`. A `ValueError` (as Chameleon's loader raises) and a `LookupError`. [PageTemplateLoader](PageTemplateLoader.md#chameleon_rs.PageTemplateLoader) and the other integrations raise Chameleon's plain `ValueError`, unchanged.
