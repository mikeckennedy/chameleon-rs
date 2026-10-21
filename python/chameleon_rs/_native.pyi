from collections.abc import Iterable, Iterator, Mapping
from typing import Any, final

# Differential-test helpers (tokens and element trees); see crates/chameleon-py/src/testing.rs.
_testing: Any

def core_version() -> str: ...
def lookup_attr(obj: object, name: str) -> Any: ...

@final
class DefaultMarker: ...

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
        **options: Any,
    ) -> None: ...
    def render(
        self,
        variables: dict[str, Any],
        partials: Mapping[str, Program] | None = None,
        defaults: Mapping[str, Any] | None = None,
        builtins: Mapping[str, Any] | None = None,
    ) -> str: ...
    def dump(self) -> str: ...
    @property
    def macro_names(self) -> list[str]: ...

@final
class Macro:
    """A macro of a compiled template (or, with ``name`` ``None``, the whole template)."""

    def __init__(self, program: Program, name: str | None, builtins: Mapping[str, Any]) -> None: ...
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
    def __getitem__(self, key: str) -> Any: ...
    def __contains__(self, key: object) -> bool: ...
    def __iter__(self) -> Iterator[str]: ...
    def get(self, key: str, default: Any = None) -> Any: ...  # type: ignore[override]
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
