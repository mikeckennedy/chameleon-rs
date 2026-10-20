# Ported from Chameleon's src/chameleon/utils.py (callablestr, callableint, descriptorstr,
# descriptorint), modified: only the helpers chameleon-rs needs so far.
"""Small helpers shared by the template runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class callablestr(str):
    __slots__ = ()

    def __call__(self) -> str:
        return self


class callableint(int):
    __slots__ = ()

    def __call__(self) -> int:
        return self


class descriptorstr:
    __slots__ = '__name__', 'function'

    def __init__(self, function: Callable[[Any], str]) -> None:
        self.function = function
        self.__name__ = function.__name__

    def __get__(self, context: object, cls: type[object]) -> callablestr:
        return callablestr(self.function(context))


class descriptorint:
    __slots__ = '__name__', 'function'

    def __init__(self, function: Callable[[Any], int]) -> None:
        self.function = function
        self.__name__ = function.__name__

    def __get__(self, context: object, cls: type[object]) -> callableint:
        return callableint(self.function(context))
