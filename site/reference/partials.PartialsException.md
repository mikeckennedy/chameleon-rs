## partials.PartialsException


Raised when partials are configured or used incorrectly: registering a folder that does


Usage

``` python
partials.PartialsException()
```


not exist, or rendering a partial from Python before any folder is configured. Errors from the templates themselves (`ValueError` for a missing file) propagate unchanged.
