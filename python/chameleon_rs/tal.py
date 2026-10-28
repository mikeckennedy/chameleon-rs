# Ported from Chameleon's src/chameleon/tal.py (RepeatItem, ErrorInfo, the
# constants NAME, DEFINE_RE, SUBST_RE, ATTR_RE, ENTITY_RE, WHITELIST, and verbatim
# split_parts, parse_attributes, parse_substitution, parse_defines, prepare_attributes),
# modified: type annotations on the classes; RepeatItem keeps the repeated items and has Zope's
# `first` and `last` (malthe/chameleon#144).
"""The ``repeat`` variable available in templates."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from chameleon_rs import _native
from chameleon_rs.exc import LanguageError
from chameleon_rs.namespaces import XMLNS_NS
from chameleon_rs.parser import groups
from chameleon_rs.utils import descriptorint, descriptorstr

NAME = r'[a-zA-Z_][-a-zA-Z0-9_]*'
DEFINE_RE = re.compile(r'(?s)\s*(?:(global|local)\s+)?' + rf'({NAME}|\({NAME}(?:,\s*{NAME})*\))\s+(.*)\Z', re.UNICODE)
SUBST_RE = re.compile(r'\s*(?:(text|structure)\s+)?(.*)\Z', re.DOTALL | re.UNICODE)
ATTR_RE = re.compile(r'\s*([^\s{}\'"]+)\s+([^\s].*)\Z', re.DOTALL | re.UNICODE)

ENTITY_RE = re.compile(r'(&(#?)(x?)(\d{1,5}|\w{1,8});)')

WHITELIST = frozenset(
    [
        'define',
        'comment',
        'condition',
        'content',
        'replace',
        'repeat',
        'attributes',
        'on-error',
        'omit-tag',
        'script',
        'switch',
        'case',
        'xmlns',
        'xml',
    ]
)


# Chameleon's attribute-language helpers, verbatim (pure Python; the Rust builder has its own
# port). For code that calls them; differential-tested against Chameleon's.
def split_parts(arg):
    # Break in pieces at undoubled semicolons and
    # change double semicolons to singles:
    i = 0
    while i < len(arg):
        m = ENTITY_RE.search(arg[i:])
        if m is None:
            break
        arg = arg[: i + m.end()] + ';' + arg[i + m.end() :]
        i += m.end()

    arg = arg.replace(';;', '\0')
    parts = arg.split(';')
    parts = [p.replace('\0', ';') for p in parts]
    if len(parts) > 1 and not parts[-1].strip():
        del parts[-1]  # It ended in a semicolon

    return parts


def parse_attributes(clause):
    attrs = []
    seen = set()
    for part in split_parts(clause):
        m = ATTR_RE.match(part)
        if not m:
            name, expr = None, part.strip()
        else:
            name, expr = groups(m, part)

        if name in seen:
            raise LanguageError('Duplicate attribute name in attributes.', part)

        seen.add(name)
        attrs.append((name, expr))

    return attrs


def parse_substitution(clause):
    m = SUBST_RE.match(clause)
    if m is None:
        raise LanguageError('Invalid content substitution syntax.', clause)

    key, expression = groups(m, clause)
    if not key:
        key = 'text'

    return key, expression


def parse_defines(clause):
    """
    Parses a tal:define value.

    # Basic syntax, implicit local
    >>> parse_defines('hello lovely')
    [('local', ('hello',), 'lovely')]

    # Explicit local
    >>> parse_defines('local hello lovely')
    [('local', ('hello',), 'lovely')]

    # With global
    >>> parse_defines('global hello lovely')
    [('global', ('hello',), 'lovely')]

    # Multiple expressions
    >>> parse_defines('hello lovely; tea time')
    [('local', ('hello',), 'lovely'), ('local', ('tea',), 'time')]

    # With multiple names
    >>> parse_defines('(hello, howdy) lovely')
    [('local', ['hello', 'howdy'], 'lovely')]

    # With unicode whitespace
    >>> try:
    ...     s = '\xc2\xa0hello lovely'.decode('utf-8')
    ... except AttributeError:
    ...     s = '\xa0hello lovely'
    >>> parse_defines(s) == [
    ...     ('local', ('hello',), 'lovely')
    ... ]
    True

    """
    defines = []
    for part in split_parts(clause):
        m = DEFINE_RE.match(part)
        if m is None:
            raise LanguageError('Invalid define syntax', part)
        context, name, expr = groups(m, part)
        context = context or 'local'

        if name.startswith('('):
            names = [n.strip() for n in name.strip('()').split(',')]
        else:
            names = (name,)

        defines.append((context, names, expr))

    return defines


def prepare_attributes(attrs, dyn_attributes, i18n_attributes, ns_attributes, drop_ns):
    drop = {
        attribute['name']
        for attribute, (ns, value) in zip(attrs, ns_attributes)
        if ns in drop_ns or (ns == XMLNS_NS and attribute['value'] in drop_ns)
    }

    attributes = []
    normalized = {}

    for attribute in attrs:
        name = attribute['name']

        if name in drop:
            continue

        attributes.append(
            (
                name,
                attribute['value'],
                attribute['quote'],
                attribute['space'],
                attribute['eq'],
                None,
            )
        )

        normalized[name.lower()] = len(attributes) - 1

    for name, expr in dyn_attributes:
        index = normalized.get(name.lower()) if name else None

        if index is not None:
            _, text, quote, space, eq, _ = attributes[index]
            add = attributes.__setitem__
        else:
            text = None
            quote = '"'
            space = ' '
            eq = '='
            index = len(attributes)
            add = attributes.insert
            if name is not None:
                normalized[name.lower()] = len(attributes) - 1

        attribute = name, text, quote, space, eq, expr
        add(index, attribute)

    for name in i18n_attributes:
        attr = name.lower()
        if attr not in normalized:
            attributes.append((name, name, '"', ' ', '=', None))
            normalized[attr] = len(attributes) - 1

    return attributes


_MISSING = object()


def _part(item: Any, name: str | None) -> Any:
    """The part of a repeated item that ``first`` and ``last`` compare: the item itself, or its
    attribute ``name`` (its key ``name``, for a mapping)."""
    if name is None:
        return item
    if isinstance(item, Mapping):
        return item.get(name, _MISSING)
    return getattr(item, name, _MISSING)


class RepeatItem:
    __slots__ = '_items', '_iterator', 'length'

    __allow_access_to_unprotected_subobjects__ = True

    def __init__(self, iterator: Iterator[Any], length: int, items: Sequence[Any] | None = None) -> None:
        self.length = length
        self._iterator = iterator
        # The repeated sequence, for `first` and `last` (Chameleon's RepeatItem has no third
        # argument, and code that makes one without it gets everything else).
        self._items = items

    def __iter__(self) -> Iterator[Any]:
        return self._iterator

    @descriptorint
    def index(self) -> int:
        try:
            remaining = self._iterator.__length_hint__()  # ty: ignore[unresolved-attribute] - else AttributeError
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

    def first(self, name: str | None = None) -> bool:
        """Whether this item starts a run of items with the same ``name`` part (an attribute,
        or a key for a mapping; without a name, the whole item): the first item, or one whose
        part differs from the item before. Zope's ``first``, for grouping a sorted sequence:
        ``<h2 tal:condition="repeat.item.first('category')">``."""
        return self._run_edge(name, -1)

    def last(self, name: str | None = None) -> bool:
        """Whether this item ends a run of items with the same ``name`` part: the last item,
        or one whose part differs from the item after (see ``first``)."""
        return self._run_edge(name, 1)

    def _run_edge(self, name: str | None, step: int) -> bool:
        if self._items is None:
            raise TypeError('first() and last() need the repeated items: RepeatItem(iterator, length, items)')
        index = self.index
        if index == (0 if step < 0 else self.length - 1):
            return True
        part = _part(self._items[index], name)
        # As in Zope: a part missing from either item counts as different.
        return part is _MISSING or part != _part(self._items[index + step], name)

    def next(self) -> None:
        raise NotImplementedError("Method not implemented (can't update local variable).")

    def _letter(self, base: int = ord('a'), radix: int = 26) -> str:
        """Get the iterator position as a lower-case letter."""
        index = self.index
        if index < 0:
            raise TypeError('No iteration position')
        s = ''
        while True:
            index, off = divmod(index, radix)
            s = chr(base + off) + s
            if not index:
                return s

    letter = descriptorstr(_letter)

    @descriptorstr
    def Letter(self) -> str:
        """Get the iterator position as an upper-case letter."""
        return self._letter(base=ord('A'))

    @descriptorstr
    def Roman(
        self,
        rnvalues: tuple[tuple[int, str], ...] = (
            (1000, 'M'),
            (900, 'CM'),
            (500, 'D'),
            (400, 'CD'),
            (100, 'C'),
            (90, 'XC'),
            (50, 'L'),
            (40, 'XL'),
            (10, 'X'),
            (9, 'IX'),
            (5, 'V'),
            (4, 'IV'),
            (1, 'I'),
        ),
    ) -> str:
        """Get the iterator position as an upper-case roman numeral."""
        n = self.index + 1
        s = ''
        for v, r in rnvalues:
            rct, n = divmod(n, v)
            s = s + r * rct
        return s

    @descriptorstr
    def roman(self) -> str:
        """Get the iterator position as a lower-case roman numeral."""
        return self.Roman().lower()


# Chameleon's ``RepeatDict``, natively (``crates/chameleon-py/src/repeat.rs``): the same proxy, holding the
# mapping itself rather than its bound ``__getitem__`` and ``__setitem__``, which on free-threaded Python
# made every instance take references every thread contends on.
RepeatDict = _native.RepeatDict


class ErrorInfo:
    """Information about an exception passed to an on-error handler (bound as ``error``)."""

    def __init__(self, err: BaseException | type[BaseException], position: tuple[Any, Any] = (None, None)) -> None:
        if isinstance(err, Exception):
            self.type: Any = err.__class__
            self.value: Any = err
        else:
            self.type = err
            self.value = None
        self.lineno = position[0]
        self.offset = position[1]
