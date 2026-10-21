# Ported from Chameleon's src/chameleon/tokenize.py (`Token`, `iter_xml`, `iter_text`; itself
# based on Paul Prescod's XML lexing recipe, PSF License), modified: `iter_xml` runs the Rust
# tokenizer (an exact port of Chameleon's `XML_SPE` pattern) instead of the regex; type
# annotations.
"""Template tokens: ``Token`` is a ``str`` that remembers where it came from (its code-point
position in the template source, the source and the filename); ``iter_xml`` and ``iter_text``
split a template into tokens as Chameleon does."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Self, SupportsIndex, cast, overload

from chameleon_rs._native import tokenize_ranges


def iter_xml(body: str, filename: str | None = None) -> Iterator[Token]:
    for start, end in tokenize_ranges(body):
        yield Token(body[start:end], start, body, filename)


def iter_text(body: str, filename: str | None = None) -> Iterator[Token]:
    yield Token(body, 0, body, filename)


class Token(str):
    __slots__ = 'filename', 'pos', 'source'

    pos: int
    source: str | None
    filename: str

    def __new__(cls, string: str, pos: int = 0, source: str | None = None, filename: str | None = None) -> Self:
        inst = str.__new__(cls, string)
        inst.pos = pos
        inst.source = source
        # convert pathlib.Path to a str, since we rely on this being a string downstream
        inst.filename = filename or ''
        return inst

    @overload  # type: ignore[override]
    def __getitem__(self, index: slice) -> Token: ...

    @overload
    def __getitem__(self, index: SupportsIndex) -> str: ...

    def __getitem__(self, index: SupportsIndex | slice) -> str:
        s = str.__getitem__(self, index)
        if isinstance(index, slice):
            return Token(s, self.pos + (index.start or 0), self.source, self.filename)
        return s

    def __add__(self, other: str | None) -> Token:  # type: ignore[override]
        if other is None:
            return self

        return Token(str.__add__(self, other), self.pos, self.source, self.filename)

    def __eq__(self, other: object) -> bool:
        return str.__eq__(self, other)

    def __hash__(self) -> int:
        return str.__hash__(self)

    def replace(self, old: str, new: str, count: SupportsIndex = -1, /) -> Token:
        s = str.replace(self, old, new, count)
        return Token(s, self.pos, self.source, self.filename)

    def split(self, sep: str | None = None, maxsplit: SupportsIndex = -1) -> list[Token]:  # type: ignore[override]
        l_ = str.split(self, sep, maxsplit)
        pos = self.pos
        for i, s in enumerate(l_):
            l_[i] = Token(s, pos, self.source, self.filename)
            pos += len(s)
        return cast('list[Token]', l_)

    def strip(self, chars: str | None = None, /) -> Token:
        return self.lstrip(chars).rstrip(chars)

    def lstrip(self, chars: str | None = None, /) -> Token:
        s = str.lstrip(self, chars)
        return Token(s, self.pos + len(self) - len(s), self.source, self.filename)

    def rstrip(self, chars: str | None = None, /) -> Token:
        s = str.rstrip(self, chars)
        return Token(s, self.pos, self.source, self.filename)

    @property
    def location(self) -> tuple[int, int]:
        if self.source is None:
            return 0, self.pos

        body = self.source[: self.pos]
        line = body.count('\n')
        return line + 1, self.pos - body.rfind('\n', 0) - 1
