"""Encoding sniffing (`read_bytes`, `detect_encoding`, `read_xml_encoding`) against Chameleon's."""

from __future__ import annotations

import codecs
from collections.abc import Callable
from typing import Any

import chameleon.utils
import pytest
from chameleon_rs import utils
from hypothesis import given, settings
from hypothesis import strategies as st

DOCUMENTS = [
    b'',
    b'<html></html>',
    b"<?xml version='1.0' encoding='ascii'?><doc/>",
    b"<?xml\tversion='1.0' encoding='latin-1'?><doc>\xe9</doc>",
    b'<?xml version="1.0"?><doc/>',
    b'<?xml version="1.0" encoding="nope"?><doc/>',
    b'<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1251"/></head></html>',
    b"<META HTTP-EQUIV=content-type CONTENT='text/plain;charset=utf-8'>",
    b'<meta http-equiv="Content-Type" content="text/html; charset=bogus">',
    '<p>é</p>'.encode(),
    b'<p>\xff</p>',
]
for bom, encoding in utils.xml_prefixes:
    DOCUMENTS.append(bom + '<p>x</p>'.encode(encoding.replace('-sig', '')))
    DOCUMENTS.append("<?xml version='1.0' encoding='ascii'?><doc/>".encode(encoding))


def outcome(f: Callable[..., Any], *args: Any) -> Any:
    try:
        return f(*args)
    except Exception as e:  # noqa: BLE001 - the exception type is part of the comparison
        return (type(e).__name__, str(e))


def same(body: bytes, default: str = 'utf-8') -> None:
    assert outcome(utils.read_bytes, body, default) == outcome(chameleon.utils.read_bytes, body, default)
    assert outcome(utils.read_xml_encoding, body) == outcome(chameleon.utils.read_xml_encoding, body)
    for value in (body, body.decode('latin-1')):
        assert outcome(utils.detect_encoding, value, default) == outcome(
            chameleon.utils.detect_encoding, value, default
        )


@pytest.mark.parametrize('body', DOCUMENTS)
def test_documents(body: bytes) -> None:
    same(body)
    same(body, 'ascii')


def test_tables_match() -> None:
    assert utils.xml_prefixes == chameleon.utils.xml_prefixes
    assert utils._xml_prefixes == chameleon.utils._xml_prefixes
    assert utils.read_encoded(DOCUMENTS[6]) == chameleon.utils.read_encoded(DOCUMENTS[6])


PIECES = [
    b'<?xml',
    b' version="1.0"',
    b' encoding="',
    b"encoding='",
    b'ENCODING = "',
    b'utf-8',
    b'latin-1',
    b'cp1252',
    b'ascii',
    b'"',
    b"'",
    b'?>',
    b'<meta',
    b' http-equiv=',
    b'"Content-Type"',
    b'content-type',
    b' content="',
    b'text/html',
    b'; charset=',
    b';charset=',
    b'windows-1251',
    b'/>',
    b'>',
    b' ',
    b'\n',
    b'\xe9',
    b'\xff',
    b'\x00',
    codecs.BOM_UTF8,
    codecs.BOM_UTF16_LE,
    codecs.BOM_UTF16_BE,
    codecs.BOM_UTF32_LE,
]


@settings(max_examples=500, deadline=None, database=None)
@given(st.lists(st.sampled_from(PIECES) | st.binary(max_size=3), max_size=14).map(b''.join))
def test_generated(body: bytes) -> None:
    same(body)
