# Ported from Chameleon's src/chameleon/utils.py (callablestr, callableint, descriptorstr,
# descriptorint, and the encoding sniffing: xml_prefixes, read_encoded, read_bytes,
# detect_encoding, read_xml_encoding), modified: only the helpers chameleon-rs needs so far,
# with type annotations.
"""Small helpers shared by the template runtime, and template encoding sniffing.

The sniffing stays in Python on purpose (see docs/architecture.md, "Phase 3 amendments"):
the result is decoded with Python's codecs (any encoding a ``<meta>`` tag or XML declaration
names), and the regexes are Python's, so the behaviour is Chameleon's by construction.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Callable
from typing import Any


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
