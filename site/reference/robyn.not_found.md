## robyn.not_found()


Ends the current `@template` handler with a 404 page: `template_file`


Usage

``` python
robyn.not_found(
    template_file=None, *, four04template_file=None, **template_data
)
```


(`errors/404.pt` by default; `four04template_file=` is chameleon-robyn's name for it) rendered as `text/html` with `message` (`'The URL resulted in a 404 response.'`) and `template_data`.

Raises `ChameleonRobynNotFoundException` (a [chameleon_rs.web.NotFound](web.NotFound.md#chameleon_rs.web.NotFound)), which the decorator turns into the response.
