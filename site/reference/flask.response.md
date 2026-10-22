## flask.response()


Renders a template to a `flask.Response`. (Unlike `@template`, `mimetype` is not a


Usage

``` python
flask.response(
    template_file, content_type="text/html", status_code=200, **template_data
)
```


keyword here: as in chameleon-flask, every other keyword is the model.)
