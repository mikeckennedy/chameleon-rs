## PageTemplate.render()


Render template to string.


Usage

``` python
PageTemplate.render(
    encoding=None,
    **kw,
)
```


If provided, the `encoding` argument overrides the template default value. Additional keyword arguments are passed as template variables; `translate` overrides the template's translation function and `target_language` is the language passed to it when no `i18n:target` applies.
