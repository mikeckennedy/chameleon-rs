# Ported from Chameleon's src/chameleon/utils.py (callablestr, callableint, descriptorstr,
# descriptorint, Markup, _resolve_dotted / resolve_dotted, safe_native, limit_string,
# value_repr, create_formatted_exception, and the encoding sniffing: xml_prefixes,
# read_encoded, read_bytes, detect_encoding, read_xml_encoding), modified: only the helpers
# chameleon-rs needs so far, with type annotations.
"""Small helpers shared by the template runtime, and template encoding sniffing.

The sniffing stays in Python on purpose (see docs/architecture.md, "Phase 3 amendments"):
the result is decoded with Python's codecs (any encoding a ``<meta>`` tag or XML declaration
names), and the regexes are Python's, so the behaviour is Chameleon's by construction.
"""

from __future__ import annotations

import codecs
import logging
import re
from collections.abc import Callable, Iterator
from typing import Any

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

RE_META = re.compile(
    r'\s*<meta\s+http-equiv=["\']?Content-Type["\']?'
    r'\s+content=["\']?([^;]+);\s*charset=([^"\']+)["\']?\s*/?\s*>\s*',
    re.IGNORECASE,
)

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

    match = RE_META.search(body)
    if match is not None:
        content_type, encoding = match.groups()
        return content_type, encoding

    return None, default_encoding


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


class Markup(str):
    """Wraps a string to always render as structure (what ``structure:`` returns).

    >>> Markup('<br />')
    s'<br />'
    """

    def __html__(self) -> str:
        return str(self)

    def __repr__(self) -> str:
        return f"s'{self}'"


module_cache: dict[str, Any] = {}


# Taken from zope.dottedname (via Chameleon)
def _resolve_dotted(name: str, module: str | None = None) -> Any:
    name_parts = name.split('.')
    if not name_parts[0]:
        if module is None:
            raise ValueError('relative name without base module')
        module_parts = module.split('.')
        name_parts.pop(0)
        while not name[0]:
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


def value_repr(value: object) -> str:
    """How a template's arguments are shown in a render error (``BaseTemplate.value_repr``)."""
    if isinstance(value, str):
        short = limit_string(value)
        return short.replace('\n', '\\n')
    if isinstance(value, (int, float)):
        return value  # type: ignore[return-value]
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
    cls: type[object],
    formatter: Callable[..., str],
    base: type[BaseException] = Exception,
) -> BaseException:
    """A copy of ``exc`` whose class derives from ``cls`` and ``base`` (so ``except KeyError``
    still catches it) and whose ``__str__`` is ``formatter``."""
    try:
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
            inst = BaseException.__new__(new)  # type: ignore[type-var]
        except TypeError:
            inst = cls.__new__(new)  # type: ignore[call-overload]

        BaseException.__init__(inst, *exc.args)
        inst.__dict__ = exc.__dict__
        return inst
    except ValueError:
        name = type(exc).__name__
        log.warning("Unable to copy exception of type '%s'.", name)
        raise TypeError(exc) from None
