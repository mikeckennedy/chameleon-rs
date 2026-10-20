# Ported from Chameleon's src/chameleon/i18n.py (simple_translate, _interp_regex),
# modified: only the default translation function.
"""Translation support."""

from __future__ import annotations

import re
from collections.abc import Mapping

NAME_RE = r'[a-zA-Z][-a-zA-Z0-9_]*'

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

        def replace(match: re.Match[str]) -> str:
            whole, param1, param2 = match.groups()
            return str(mapping.get(param1 or param2, whole))

        return _interp_regex.sub(replace, default)

    return default
