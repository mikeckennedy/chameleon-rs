## web.model_of()


The template model a view returned: a `dict` as is; any other `Mapping` as a


Usage

``` python
web.model_of(value)
```


`dict`; a dataclass instance as its fields (shallow: field values are not converted); an object with a callable `model_dump()` (Pydantic, duck-typed) as what it returns. `None` for anything else.
