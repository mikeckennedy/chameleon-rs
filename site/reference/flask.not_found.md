## flask.not_found()


Ends the current `@template` view with a 404 page: `template_file`


Usage

``` python
flask.not_found(
    template_file=None, *, four04template_file=None, **template_data
)
```


(`errors/404.pt` by default; `four04template_file=` is chameleon-flask's name for it) rendered with `template_data`, as `text/html`.

Raises `FlaskChameleonNotFoundException` (a [chameleon_rs.web.NotFound](web.NotFound.md#chameleon_rs.web.NotFound)), which the decorator turns into the response.
