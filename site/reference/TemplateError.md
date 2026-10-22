## TemplateError


An error raised by Chameleon.


Usage

``` python
TemplateError()
```


> > > from chameleon_rs.config import SOURCE_EXPRESSION_MARKER_LENGTH from chameleon_rs.tokenize import Token token = Token('token') message = 'message'

Make sure the exceptions can be copied:

> > > from copy import copy copy(TemplateError(message, token)) TemplateError('message', 'token')

And pickle/unpickled:

> > > from pickle import dumps, loads loads(dumps(TemplateError(message, token), -1)) TemplateError('message', 'token')
