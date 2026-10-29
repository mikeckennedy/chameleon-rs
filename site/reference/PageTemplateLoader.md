## PageTemplateLoader


Loads and caches page templates from one or more directories by filename (`loader['page.pt']`).


Usage

``` python
PageTemplateLoader()
```


Accepts a single directory path (`str` or `os.PathLike`) or an iterable of paths as `search_path`, and passes extra keyword arguments (such as `auto_reload` or `strict_tags`) to each template it constructs.


## Methods

| Name | Description |
|----|----|
| [load()](#load) | Load and return a template file. |

------------------------------------------------------------------------


#### load()


Load and return a template file.


Usage

``` python
load(filename, format=None)
```


The format parameter determines will parse the file. Valid options are `xml` and `text`.
