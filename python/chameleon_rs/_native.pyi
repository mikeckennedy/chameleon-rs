# Type stubs for the PyO3 extension module `chameleon_rs._native` (crates/chameleon-py/src).
# Keep each signature in step with its `#[pyfunction]` / `#[pymethods]` definition.

from collections.abc import Generator, Iterable, Iterator, Mapping
from typing import Any, Literal, final, type_check_only

from chameleon_rs.template import PageTemplate
from chameleon_rs.utils import Markup

@final
@type_check_only
class _TestingModule:
    """`chameleon_rs._native._testing`: tokens and element trees for the differential tests
    (testing.rs). The keyword options are `Program`'s."""

    @staticmethod
    def tokens(body: str, mode: str = 'xml') -> list[tuple[str, int]]: ...
    @staticmethod
    def parse(
        body: str, namespaces: dict[str | None, str], restricted_namespace: bool = True, mode: str = 'xml'
    ) -> list[tuple[str, tuple[Any, ...]]]: ...
    @staticmethod
    def tree(body: str, **options: Any) -> str: ...
    @staticmethod
    def inventory(body: str, **options: Any) -> list[tuple[str, str, int | None]]: ...
    @staticmethod
    def tokenizer_pattern(name: str) -> str | None: ...
    @staticmethod
    def char_class(name: str) -> str: ...
    @staticmethod
    def parse_count(body: str) -> int: ...
    @staticmethod
    def token_count(body: str) -> int: ...
    @staticmethod
    def free_variables(body: str, **options: Any) -> list[tuple[str, list[str] | None, list[str] | None]]: ...
    @staticmethod
    def fast_path_coverage(body: str, load: bool = False, **options: Any) -> tuple[int, int]: ...

_testing: _TestingModule

def core_version() -> str: ...
def tokenize_ranges(body: str, mode: str = 'xml') -> list[tuple[int, int]]: ...
def parse_elements(
    body: str, lengths: list[int], namespaces: dict[str | None, str], restricted_namespace: bool = True
) -> list[tuple[str, tuple[Any, ...]]]: ...
def lookup_attr(obj: object, name: str) -> Any: ...
def render_page(template: PageTemplate, encoding: str | None, kw: dict[str, Any]) -> str: ...
def render_page_async(
    template: PageTemplate, encoding: str | None, kw: dict[str, Any], concurrent: bool = False
) -> str | RenderAwaitable: ...

@final
class RenderAwaitable(Generator[Any, Any, str]):
    """A render that suspends on awaitable values (what ``render_async`` awaits). It is its
    own iterator: awaiting it gives the rendered ``str``. (At runtime it is a
    ``collections.abc.Generator`` through the ABC's structural check.)"""

    def __await__(self) -> RenderAwaitable: ...
    def __iter__(self) -> RenderAwaitable: ...
    def __next__(self) -> Any: ...
    def send(self, value: Any) -> Any: ...
    def throw(self, typ: Any, val: Any = None, tb: Any = None) -> Any: ...
    def close(self) -> None: ...

@final
class DefaultMarker:
    @property
    def __name__(self) -> str: ...

DEFAULT_MARKER: DefaultMarker

@final
class Program:
    def __init__(
        self,
        source: str,
        filename: str = '<string>',
        *,
        expression_types: Iterable[tuple[str, tuple[Any, ...]]] | None = None,
        default_expression: str | None = None,
        builtins: Iterable[str] | None = None,
        fast_path: bool = True,
        default_marker: Any = None,
        token_lengths: list[int] | None = None,
        surrogate_base: int | None = None,
        **options: Any,
    ) -> None: ...
    def render(
        self,
        variables: dict[str, Any],
        defaults: dict[str, Any] | None = None,
        builtins: dict[str, Any] | None = None,
    ) -> str: ...
    def render_scope(self, scope: Scope, builtins: dict[str, Any] | None = None) -> str: ...
    def dump(self) -> str: ...
    @property
    def macro_names(self) -> list[str]: ...

@final
class Macro:
    """A macro of a compiled template (or, with ``name`` ``None``, the whole template)."""

    def __init__(self, program: Program, name: str | None, builtins: dict[str, Any]) -> None: ...
    @property
    def include(self) -> Macro: ...
    def __call__(
        self,
        stream: list[str],
        econtext: Mapping[str, Any],
        rcontext: dict[str, Any],
        domain: str | None = None,
        context: str | None = None,
        target_language: Any = None,
    ) -> None: ...

@final
class SlotFill:
    """A ``metal:fill-slot`` body registered for a macro call (``econtext['__slot_<name>']``)."""

    def __call__(self, stream: list[str], econtext: Mapping[str, Any], rcontext: dict[str, Any]) -> None: ...

class Scope(dict[str, Any]):
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...
    def __getitem__(self, key: str, /) -> Any: ...
    def __contains__(self, key: object, /) -> bool: ...
    def __iter__(self) -> Iterator[str]: ...
    def get(self, key: object, default: Any = None) -> Any: ...
    def get_name(self, key: str) -> Any: ...
    @property
    def vars(self) -> Scope: ...
    def copy(self) -> Scope: ...
    def set_local(self, name: str, value: Any) -> None: ...
    def set_global(self, name: str, value: Any) -> None: ...
    def setLocal(self, name: str, value: Any) -> None: ...
    def setGlobal(self, name: str, value: Any) -> None: ...

@final
class SubEval:
    """Evaluates a custom expression type's native sub-expressions (see _bridge.py)."""

    def __call__(self, index: int) -> Any: ...

@final
class MarkdownProfile:
    """One ``markdown:``-style expression prefix: its name and how it converts."""

    def __init__(
        self,
        name: str,
        *,
        raw_html: Literal['escape', 'allow'],
        images: bool,
        inline: bool,
        heading_ids: str | None,
        classes: list[tuple[str, str]],
        table: bool,
        strikethrough: bool,
        tasklist: bool,
        autolink: bool,
        footnotes: bool,
    ) -> None: ...
    @property
    def name(self) -> str: ...
    def convert(self, text: object) -> Markup:
        """``text`` converted to HTML (through the cache, as in a template)."""

def set_markdown_cache(size: int) -> None: ...
def markdown_cache_info() -> tuple[int, int, int]: ...
def markdown_cache_limits() -> tuple[int, int, int]: ...
