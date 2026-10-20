# Ported from Chameleon's src/chameleon/tal.py (RepeatItem, RepeatDict), modified: only the
# attributes the walking skeleton provides (index, number, even, odd, parity, start, end,
# length); letter/roman and the rest arrive with Phase 6a.
"""The ``repeat`` variable available in templates."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Iterator
from typing import Any

from chameleon_rs.utils import descriptorint, descriptorstr


class RepeatItem:
    __slots__ = '_iterator', 'length'

    __allow_access_to_unprotected_subobjects__ = True

    def __init__(self, iterator: Iterator[Any], length: int) -> None:
        self.length = length
        self._iterator = iterator

    def __iter__(self) -> Iterator[Any]:
        return self._iterator

    @descriptorint
    def index(self) -> int:
        try:
            remaining = self._iterator.__length_hint__()  # type: ignore[attr-defined]
        except AttributeError:
            remaining = len(tuple(copy.copy(self._iterator)))
        return self.length - remaining - 1

    @descriptorint
    def start(self) -> int:
        return self.index == 0

    @descriptorint
    def end(self) -> int:
        return self.index == self.length - 1

    @descriptorint
    def number(self) -> int:
        return self.index + 1

    @descriptorstr
    def odd(self) -> str:
        return (self.index % 2 == 1 and 'odd') or ''

    @descriptorstr
    def even(self) -> str:
        return (self.index % 2 == 0 and 'even') or ''

    @descriptorstr
    def parity(self) -> str:
        return (self.index % 2 == 0 and 'even') or 'odd'

    def next(self) -> None:
        raise NotImplementedError("Method not implemented (can't update local variable).")


class RepeatDict:
    __slots__ = '__getitem__', '__setitem__'

    def __init__(self, d: dict[Any, RepeatItem]) -> None:
        self.__setitem__ = d.__setitem__
        self.__getitem__ = d.__getitem__

    def __getattr__(self, key: str) -> RepeatItem:
        try:
            return self[key]  # type: ignore[index]
        except KeyError:
            raise AttributeError(key) from None

    def __call__(self, key: Any, iterable: Iterable[Any] | None) -> tuple[Iterator[Any], int]:
        """Materialize the iterable as a list and register a RepeatItem for it."""
        iterable = list(iterable) if iterable is not None else ()
        length = len(iterable)
        iterator = iter(iterable)
        self[key] = RepeatItem(iterator, length)  # type: ignore[index]
        return iterator, length
