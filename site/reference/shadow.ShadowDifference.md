## shadow.ShadowDifference


One render whose outcome differs between the engines.


Usage

``` python
shadow.ShadowDifference(
    filename,
    served,
    chameleon,
    rust,
    diff,
)
```


## Parameter Attributes


`filename: str`  

`served: str`  
The engine whose outcome the application received (`'chameleon'` or `'rust'`).

`chameleon: str`  
Chameleon's full output, or its exception as `Type: message` (may hold personal data).

`rust: str`  
chameleon-rs's full output, or its exception (`Type: message`).

`diff: str`  
A unified diff of the two (lines split after each `>` and cut at 200 characters), at most 4 KB.
