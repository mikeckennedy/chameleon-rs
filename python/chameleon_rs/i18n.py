# Ported from Chameleon's src/chameleon/i18n.py (NAME_RE, WHITELIST, _interp_regex,
# fast_translate when zope.i18n is installed, simple_translate, parse_attributes), modified:
# type annotations.
"""Translation support."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from chameleon_rs.exc import CompilationError

NAME_RE = r'[a-zA-Z][-a-zA-Z0-9_]*'

WHITELIST = frozenset(
    [
        'translate',
        'domain',
        'context',
        'target',
        'source',
        'attributes',
        'data',
        'name',
        'mode',
        'xmlns',
        'xml',
        'comment',
        'ignore',
        'ignore-attributes',
    ]
)

_interp_regex = re.compile(r'(?<!\$)(\$(?:(%(n)s)|{(%(n)s)}))' % ({'n': NAME_RE}))  # noqa: UP031 - verbatim port


def simple_translate(
    msgid: str,
    domain: str | None = None,
    mapping: Mapping[str, object] | None = None,
    context: str | None = None,
    target_language: str | None = None,
    default: str | None = None,
) -> str:
    if default is None:
        default = getattr(msgid, 'default', msgid)

    if mapping is None:
        mapping = getattr(msgid, 'mapping', None)

    if mapping:
        return _interpolate(default, mapping)

    return default


def _interpolate(default: Any, mapping: Any) -> str:
    """`simple_translate`'s interpolation of a true `mapping` into `default` (also called by
    the native conversion, which has read both already)."""

    def replace(match: re.Match[str]) -> str:
        whole, param1, param2 = match.groups()
        return str(mapping.get(param1 or param2, whole))

    return _interp_regex.sub(replace, default)


try:  # pragma: no cover - only when zope.i18n is installed
    from zope.i18n import interpolate, translate  # type: ignore[import-not-found]
    from zope.i18nmessageid import Message  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    pass
else:  # pragma: no cover

    def fast_translate(
        msgid: str | None,
        domain: str | None = None,
        mapping: Mapping[str, object] | None = None,
        context: str | None = None,
        target_language: str | None = None,
        default: str | None = None,
    ) -> str | None:
        if msgid is None:
            return None

        if target_language is not None or context is not None:
            result: str = translate(
                msgid,
                domain=domain,
                mapping=mapping,
                context=context,
                target_language=target_language,
                default=default,
            )
            if result != msgid:
                return result

        if isinstance(msgid, Message):
            default = msgid.default
            mapping = msgid.mapping

        if default is None:
            default = str(msgid)

        if not isinstance(default, str):
            return default

        return interpolate(default, mapping)  # type: ignore[no-any-return]


def parse_attributes(attrs: str, xml: bool = True) -> dict[str, str | None]:
    d: dict[str, str | None] = {}

    specs = [spec for spec in attrs.split(';') if spec]

    for spec in specs:
        if ',' in spec:
            raise CompilationError('Attribute must not contain comma. Use semicolon to list multiple attributes', spec)
        parts = spec.split()
        msgid: str | None
        if len(parts) == 2:
            attr, msgid = parts
        elif len(parts) == 1:
            attr = parts[0]
            msgid = None
        else:
            raise CompilationError('Illegal i18n:attributes specification.', spec)
        if not xml:
            attr = attr.lower()
        attr = attr.strip()
        if attr in d:
            raise CompilationError('Attribute may only be specified once in i18n:attributes', attr)
        d[attr] = msgid

    return d
