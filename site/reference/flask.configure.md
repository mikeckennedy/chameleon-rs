## flask.configure()


Configures where templates live and how they load: for the whole process (the same as


Usage

``` python
flask.configure(
    template_folder=None,
    *,
    auto_reload=None,
    restricted_namespace=None,
    app=None
)
```


[chameleon_rs.configure](configure.md#chameleon_rs.configure): for views and for [render_partial](render_partial.md#chameleon_rs.render_partial)), or, with `app`, for that Flask app's views only (stored in `app.extensions['chameleon_rs']`). Settings left out keep their current values.

Raises `FlaskChameleonException` if the folder is missing or not a directory.
