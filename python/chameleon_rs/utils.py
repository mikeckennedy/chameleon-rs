# Ported from Chameleon's src/chameleon/utils.py (callablestr, callableint, descriptorstr,
# descriptorint, Markup, _resolve_dotted / resolve_dotted, safe_native, limit_string,
# value_repr, create_formatted_exception, the encoding sniffing: xml_prefixes,
# read_encoded, read_bytes, detect_encoding, read_xml_encoding, and the remaining helpers:
# raise_with_traceback, mangle, char2entity, substitute_entity, join, decode_htmlentities,
# DebuggingOutputStream, ListDictProxy, ImportableMarker), modified: type annotations;
# `Scope` and `lookup_attr` are the native ones.
"""Small helpers shared by the template runtime, and template encoding sniffing.

The sniffing stays in Python on purpose (see docs/architecture.md, "Phase 3 amendments"):
the result is decoded with Python's codecs (any encoding a ``<meta>`` tag or XML declaration
names), and the regexes are Python's, so the behaviour is Chameleon's by construction.
"""

from __future__ import annotations

import codecs
import logging
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from enum import Enum
from html import entities as htmlentitydefs
from types import TracebackType
from typing import Any, Literal, NoReturn, Protocol

from chameleon_rs import _native

log = logging.getLogger('chameleon.utils')


def encode_string(s: str) -> bytes:
    return bytes(s, 'utf-8')


xml_prefixes = (
    (codecs.BOM_UTF8, 'utf-8-sig'),
    (codecs.BOM_UTF16_BE, 'utf-16-be'),
    (codecs.BOM_UTF16_LE, 'utf-16-le'),
    (codecs.BOM_UTF16, 'utf-16'),
    (codecs.BOM_UTF32_BE, 'utf-32-be'),
    (codecs.BOM_UTF32_LE, 'utf-32-le'),
    (codecs.BOM_UTF32, 'utf-32'),
)


def _has_encoding(encoding: str) -> bool:
    try:
        ''.encode(encoding)
    except LookupError:
        return False
    else:
        return True


# Precomputed prefix table
_xml_prefixes = tuple(
    (bom, '<?xml'.encode(encoding), encoding) for bom, encoding in reversed(xml_prefixes) if _has_encoding(encoding)
)

_xml_decl = encode_string('<?xml')

# Chameleon's, without its leading `\s*`: `search` retried it from each character of a run of
# whitespace, consuming the rest of the run every time (64K spaces in a template took 2 s to
# compile). Whether it matches, and what its groups capture, is the same. Not used to search:
# see `_search_meta`.
RE_META = re.compile(
    r'<meta\s+http-equiv=["\']?Content-Type["\']?'
    r'\s+content=["\']?([^;]+);\s*charset=([^"\']+)["\']?\s*/?\s*>\s*',
    re.IGNORECASE,
)
# `RE_META` in parts. Up to `charset=` it matches one way only, wherever it starts.
_META_HEAD = re.compile(
    r'<meta\s+http-equiv=["\']?Content-Type["\']?\s+content=["\']?([^;]+);\s*charset=', re.IGNORECASE
)
_META_CHARSET = re.compile(r'[^"\']+')
_META_END = re.compile(r'["\']?\s*/?\s*>')

RE_ENCODING = re.compile(rb'encoding\s*=\s*(?:"|\')(?P<encoding>[\w\-]+)(?:"|\')', re.IGNORECASE)


def read_encoded(data: bytes) -> str:
    return read_bytes(data, 'utf-8')[0]


def read_bytes(body: bytes, default_encoding: str) -> tuple[str, str, str | None]:
    for bom, prefix, encoding in _xml_prefixes:
        if body.startswith(bom):
            document = body.decode(encoding)
            return document, encoding, 'text/xml' if document.startswith('<?xml') else None

        if prefix != encode_string('<?xml') and body.startswith(prefix):
            return body.decode(encoding), encoding, 'text/xml'

    content_type: str | None
    if body.startswith(_xml_decl):
        content_type = 'text/xml'
        encoding = read_xml_encoding(body) or default_encoding
    else:
        content_type, encoding = detect_encoding(body, default_encoding)

    return body.decode(encoding), encoding, content_type


def detect_encoding(body: str | bytes, default_encoding: str) -> tuple[str | None, str]:
    if not isinstance(body, str):
        body = body.decode('ascii', 'ignore')

    match = _search_meta(body)
    if match is not None:
        content_type, encoding = match
        return content_type, encoding

    return None, default_encoding


def _search_meta(body: str) -> tuple[str, str] | None:
    r"""``RE_META.search(body).groups()``, in linear time. The regex backtracks its charset
    (``[^"']+``) one character at a time, retrying the ``\s*/?\s*>`` after it from each: cubic
    in a run of whitespace with no ``>`` to end it (2,000 spaces took 4.6 s). The charset it
    settles on is the longest that end follows: the whole run (up to a quote or the end) if the
    end matches after it, else the run up to its last ``>``."""
    pos = 0
    while (head := _META_HEAD.search(body, pos)) is not None:
        start = head.end()
        run = _META_CHARSET.match(body, start)
        if run is not None:
            end = run.end()
            if _META_END.match(body, end):
                return head.group(1), body[start:end]
            close = body.rfind('>', start + 1, end)
            if close != -1:
                return head.group(1), body[start:close]
        pos = head.start() + 1
    return None


def read_xml_encoding(body: bytes) -> str | None:
    if body.startswith(b'<?xml'):
        match = RE_ENCODING.search(body)
        if match is not None:
            return match.group('encoding').decode('ascii')
    return None


class callablestr(str):
    __slots__ = ()

    def __call__(self) -> str:
        return self


class callableint(int):
    __slots__ = ()

    def __call__(self) -> int:
        return self


class _Getter[R](Protocol):
    """A named function of the instance (what ``descriptorstr`` / ``descriptorint`` wrap)."""

    __name__: str

    def __call__(self, context: Any, /) -> R: ...


class descriptorstr:
    __slots__ = '__name__', 'function'

    def __init__(self, function: _Getter[str]) -> None:
        self.function = function
        self.__name__ = function.__name__

    def __get__(self, context: object, cls: type[object]) -> callablestr:
        return callablestr(self.function(context))


class descriptorint:
    __slots__ = '__name__', 'function'

    def __init__(self, function: _Getter[int]) -> None:
        self.function = function
        self.__name__ = function.__name__

    def __get__(self, context: object, cls: type[object]) -> callableint:
        return callableint(self.function(context))


class Markup(str):
    """Wraps a string to always render as structure (what ``structure:`` returns).

    >>> Markup('<br />')
    s'<br />'
    """

    def __html__(self) -> str:
        return str(self)

    def __repr__(self) -> str:
        return f"s'{self}'"


class MarkdownMarkup(Markup):
    """What a ``markdown:`` expression returns: markup in element content, but escaped like any
    string in an attribute value. Its HTML comes from untrusted text and has quoted attributes
    of its own, which would close the attribute it sits in."""

    __slots__ = ()


module_cache: dict[str, Any] = {}


# Taken from zope.dottedname (via Chameleon)
def _resolve_dotted(name: str, module: str | None = None) -> Any:
    name_parts = name.split('.')
    if not name_parts[0]:
        if module is None:
            raise ValueError('relative name without base module')
        module_parts = module.split('.')
        name_parts.pop(0)
        # `name_parts`: Chameleon's `name[0]` (zope.dottedname's list, renamed) is never empty,
        # so `..x` resolved as `.x`.
        while not name_parts[0]:
            module_parts.pop()
            name_parts.pop(0)
        name_parts = module_parts + name_parts

    used = name_parts.pop(0)
    found = __import__(used)
    for n in name_parts:
        used += '.' + n
        try:
            found = getattr(found, n)
        except AttributeError:
            __import__(used)
            found = getattr(found, n)

    return found


def resolve_dotted(dotted: str) -> Any:
    """``import:``: the object a dotted name refers to, imported once and cached."""
    if dotted not in module_cache:
        resolved = _resolve_dotted(dotted)
        module_cache[dotted] = resolved
    return module_cache[dotted]


def scope_iter(scope: dict[str, Any], root: dict[str, Any] | None) -> Iterator[str]:
    """`Scope.__iter__` (the native `Scope` calls this): the local keys, then the root's keys
    not shadowed locally, lazily, so changing the scope while iterating it raises
    ``RuntimeError`` as with Chameleon's generator."""
    yield from dict.__iter__(scope)
    if root is not None:
        for key in root:
            if not dict.__contains__(scope, key):
                yield key


def safe_native(s: str | bytes, encoding: str = 'utf-8') -> str:
    if not isinstance(s, str):
        s = bytes.decode(s, encoding, 'replace')
    return s


def limit_string(s: str, max_length: int = 53) -> str:
    if len(s) > max_length:
        return s[: max_length - 3] + '...'

    return s


def value_repr(value: object) -> str | int | float:
    """How a template's arguments are shown in a render error (``BaseTemplate.value_repr``)."""
    if isinstance(value, str):
        short = limit_string(value)
        return short.replace('\n', '\\n')
    if isinstance(value, (int, float)):
        return value  # a number as is, as in Chameleon
    if isinstance(value, dict):
        return '{...} (%d)' % len(value)  # noqa: UP031 - Chameleon's formatting

    try:
        # Chameleon's trailing comma: the name is shown as a one-element tuple.
        name = (str(getattr(value, '__name__', None)),)
    except:  # noqa: E722 - as in Chameleon
        name = '-'  # type: ignore[assignment]

    return f'<{type(value).__name__} {name} at {hex(abs(id(value)))}>'


def create_formatted_exception(
    exc: BaseException,
    cls: type[BaseException],
    formatter: Callable[..., str],
    base: type[BaseException] = Exception,
) -> BaseException:
    """A copy of ``exc`` whose class derives from ``cls`` and ``base`` (so ``except KeyError``
    still catches it) and whose ``__str__`` is ``formatter``."""
    try:
        new: type[BaseException]
        try:
            new = type(
                cls.__name__,
                (cls, base),
                {
                    '__str__': formatter,
                    '_original__str__': exc.__str__,
                    '__new__': BaseException.__new__,
                    '__module__': cls.__module__,
                },
            )
        except TypeError:
            new = cls

        inst: BaseException
        try:
            inst = BaseException.__new__(new)
        except TypeError:
            inst = cls.__new__(new)

        BaseException.__init__(inst, *exc.args)
        inst.__dict__ = exc.__dict__
        return inst
    except ValueError:
        name = type(exc).__name__
        log.warning("Unable to copy exception of type '%s'.", name)
        raise TypeError(exc) from None


# The rest of Chameleon's utils.py, for code that imports it (the compat package, D7).

Scope = _native.Scope
lookup_attr = _native.lookup_attr


class _Marker(Enum):
    marker = object()


marker: Literal[_Marker.marker] = _Marker.marker


def raise_with_traceback(exc: BaseException, tb: TracebackType | None) -> NoReturn:
    exc.__traceback__ = tb
    raise exc


entity_re = re.compile(r'&(#?)(x?)(\d{1,5}|\w{1,8});')


def mangle(filename: str) -> str:
    """Mangles template filename into top-level Python module name.

    >>> mangle('hello_world.pt')
    'hello_world'

    >>> mangle('foo.bar.baz.pt')
    'foo_bar_baz'

    >>> mangle('foo-bar-baz.pt')
    'foo_bar_baz'

    """

    base, _ext = os.path.splitext(filename)
    return base.replace('.', '_').replace('-', '_')


def char2entity(c: str | bytes | bytearray) -> str:
    cp = ord(c)
    name = htmlentitydefs.codepoint2name.get(cp)
    return f'&{name};' if name is not None else f'&#{cp};'


def substitute_entity(match: re.Match[str], n2cp: Mapping[str, int] = htmlentitydefs.name2codepoint) -> str:
    ent = match.group(3)

    if match.group(1) == '#':
        if match.group(2) == '':
            return chr(int(ent))
        if match.group(2) == 'x':
            return chr(int('0x' + ent, 16))
        return ''
    cp = n2cp.get(ent)

    if cp:
        return chr(cp)
    return match.group()


_concat = ''.join


def join(stream: Iterable[str]) -> str:
    """Concatenate stream.

    >>> print(join(('Hello', ' ', 'world')))
    Hello world
    """

    try:
        return _concat(stream)
    except BaseException:
        # Loop through stream and coerce each element into unicode; this should raise an
        # exception
        for element in stream:
            str(element)

        # In case it didn't, re-raise the original exception
        raise


def decode_htmlentities(string: str) -> str:
    """
    >>> str(decode_htmlentities('&amp;amp;'))
    '&amp;'

    """

    decoded = entity_re.subn(substitute_entity, string)[0]

    # preserve input token data
    return string.replace(string, decoded)


class DebuggingOutputStream(list[str]):
    def append(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError(value)
        super().append(value)


class ListDictProxy[KT, VT]:
    def __init__(self, _l: Sequence[Mapping[KT, VT]]) -> None:
        self._l = _l

    def get(self, key: KT) -> VT | None:
        return self._l[-1].get(key)


class ImportableMarker:
    """A named marker object (Chameleon's type for ``DEFAULT_MARKER``; chameleon-rs's own
    default marker is the native ``_native.DEFAULT_MARKER``, which behaves the same)."""

    def __init__(self, module: str, name: str) -> None:
        self.__module__ = module
        self.name = name

    @property
    def __name__(self) -> str:
        return f'{self.name}_MARKER'

    def __repr__(self) -> str:
        return f'<{self.name}>'
