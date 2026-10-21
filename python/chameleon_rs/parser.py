# Ported from Chameleon's src/chameleon/parser.py, modified: the regexes and helper functions
# are verbatim (pure Python utilities); `ElementParser` runs the Rust parser (an exact port of
# Chameleon's, D7) over the token stream and builds Chameleon's items from its result.
"""The element parser: a token stream (``iter_xml``) becomes ``(kind, args)`` items with
start and end tags matched, as Chameleon's ``ElementParser`` yields them."""

# ruff: noqa
from __future__ import annotations

import bisect
import re
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from chameleon_rs._native import parse_elements
from chameleon_rs.exc import ParseError, TemplateError
from chameleon_rs.namespaces import XML_NS
from chameleon_rs.tokenize import Token


match_double_hyphen = re.compile(r'--(?!(-)*>)')
match_tag_prefix_and_name = re.compile(
    r'^(?P<prefix></?)(?P<name>([^:\n\t\r ]+:)?[^ \n\t\r>/]+)'
    r'(?P<suffix>(?P<space>\s*)/?>)?',
    re.UNICODE | re.DOTALL,
)
match_single_attribute = re.compile(
    r'(?P<space>\s+)(?!\d)'
    r'(?P<name>[^ =/>\n\t\r]+)'
    r'((?P<eq>\s*=\s*)'
    r'((?P<quote>[\'"])(?P<value>.*?)(?P=quote)|'
    r'(?P<alt_value>[^\s\'">/]+))|'
    r'(?P<simple_value>(?![ \\n\\t\\r]*=)))',
    re.UNICODE | re.DOTALL,
)
match_comment = re.compile(r'^<!--(?P<text>.*)-->$', re.DOTALL)
match_cdata = re.compile(r'^<!\[CDATA\[(?P<text>.*)\]>$', re.DOTALL)
match_declaration = re.compile(r'^<!(?P<text>[^>]+)>$', re.DOTALL)
match_processing_instruction = re.compile(r'^<\?(?P<name>\w+)(?P<text>.*?)\?>', re.DOTALL)
match_xml_declaration = re.compile(r'^<\?xml(?=[ /])', re.DOTALL)


def substitute(regex, repl, token):
    if not isinstance(token, Token):
        token = Token(token)

    return Token(regex.sub(repl, token), token.pos, token.source, token.filename)


def groups(m, token):
    result = []
    for i, group in enumerate(m.groups()):
        if group is not None:
            j, k = m.span(i + 1)
            group = token[j:k]

        result.append(group)

    return tuple(result)


def groupdict(m, token):
    d = m.groupdict()
    for name, value in d.items():
        if value is not None:
            i, j = m.span(name)
            d[name] = token[i:j]

    return d


def match_tag(token, regex=match_tag_prefix_and_name):
    m = regex.match(token)
    d = groupdict(m, token)

    end = m.end()
    token = token[end:]

    attrs = d['attrs'] = []
    for m in match_single_attribute.finditer(token):
        attr = groupdict(m, token)
        alt_value = attr.pop('alt_value', None)
        if alt_value is not None:
            attr['value'] = alt_value
            attr['quote'] = ''
        simple_value = attr.pop('simple_value', None)
        if simple_value is not None:
            attr['quote'] = ''
            attr['value'] = ''
            attr['eq'] = ''
        attrs.append(attr)
        d['suffix'] = token[m.end() :]

    return d


def parse_tag(token, namespace, restricted_namespace):
    node = match_tag(token)

    update_namespace(node['attrs'], namespace)

    if ':' in node['name']:
        prefix = node['name'].split(':')[0]
    else:
        prefix = None

    default = node['namespace'] = namespace.get(prefix, XML_NS)

    node['ns_attrs'] = unpack_attributes(node['attrs'], namespace, default, restricted_namespace)

    node['ns_map'] = namespace

    return node


def update_namespace(attributes, namespace) -> None:
    # possibly update namespaces; we do this in a separate step
    # because this assignment is irrespective of order
    for attribute in attributes:
        name = attribute['name']
        value = attribute['value']
        if name == 'xmlns':
            namespace[None] = value
        elif name.startswith('xmlns:'):
            namespace[name[6:]] = value


def unpack_attributes(attributes, namespace, default, restricted_namespace):
    namespaced = OrderedDict()

    for index, attribute in enumerate(attributes):
        name = attribute['name']
        value = attribute['value']

        if ':' in name:
            prefix = name.split(':')[0]
            name = name[len(prefix) + 1 :]
            try:
                ns = namespace[prefix]
            except KeyError:
                if restricted_namespace:
                    raise KeyError('Undefined namespace prefix: %s.' % prefix)
                else:
                    ns = default
        else:
            ns = default
        namespaced[ns, name] = value

    return namespaced


def identify(string) -> str:
    if string.startswith('<'):
        if string.startswith('<!--'):
            m = match_double_hyphen.search(string[4:])
            if m is not None:
                raise ParseError(
                    "The string '--' is not allowed in a comment.",
                    string[4 + m.start() : 4 + m.end()],
                )
            return 'comment'
        if string.startswith('<![CDATA['):
            return 'cdata'
        if string.startswith('<!'):
            return 'declaration'
        if string.startswith('<?xml'):
            return 'xml_declaration'
        if string.startswith('<?'):
            return 'processing_instruction'
        if string.startswith('</'):
            return 'end_tag'
        if string.endswith('/>'):
            return 'empty_tag'
        if string.endswith('>'):
            return 'start_tag'
        return 'error'
    return 'text'


class ElementParser:
    """Parses tokens into elements.

    The tokens are parsed together by the native parser; the items' strings are slices of
    the tokens, as in Chameleon: a slice of a ``Token`` is a ``Token`` at its position in
    that token's source, a slice of a plain string a plain string.
    """

    def __init__(
        self, stream: Iterable[str], default_namespaces: Mapping[str | None, str], restricted_namespace: bool = True
    ) -> None:
        self.stream = stream
        self.queue: list[tuple[str, tuple[Any, ...]]] = []
        self.namespaces = [dict(default_namespaces)]
        self.restricted_namespace = restricted_namespace

    def __iter__(self) -> Iterator[tuple[str, tuple[Any, ...]]]:
        tokens = list(self.stream)
        convert = _Convert(tokens)
        try:
            items = parse_elements(
                ''.join(tokens), [len(t) for t in tokens], self.namespaces[0], self.restricted_namespace
            )
        except TemplateError as exc:
            token = exc.token
            raise type(exc)(exc.args[0], convert.tok((str(token), token.pos))) from None
        self.queue = [convert.item(item) for item in items]
        return iter(self.queue)


class _Convert:
    """Native items (``(text, pos)`` tokens, positions in the tokens joined) to Chameleon's:
    slices of the original tokens."""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.starts = []
        at = 0
        for t in tokens:
            self.starts.append(at)
            at += len(t)

    def tok(self, value: Any) -> Any:
        if value is None:
            return None
        text, pos = value
        if pos is None:
            return text
        i = max(bisect.bisect_right(self.starts, pos) - 1, 0)
        if not text and i > 0 and self.starts[i] == pos:
            # An empty slice where two tokens meet is the end of the first (Chameleon's
            # empty groups are at the ends of tags).
            i -= 1
        token = self.tokens[i]
        if not isinstance(token, Token):
            return text
        return Token(text, token.pos + pos - self.starts[i], token.source, token.filename)

    def tag(self, tag: dict[str, Any] | None) -> dict[str, Any] | None:
        if tag is None:
            return None
        tok = self.tok
        node = {key: tok(tag[key]) for key in ('prefix', 'name', 'suffix', 'space')}
        node['attrs'] = [{key: tok(value) for key, value in attr.items()} for attr in tag['attrs']]
        node['namespace'] = tok(tag['namespace'])
        node['ns_attrs'] = OrderedDict(((tok(ns), tok(name)), tok(value)) for (ns, name), value in tag['ns_attrs'])
        node['ns_map'] = {tok(prefix): tok(value) for prefix, value in tag['ns_map'].items()}
        return node

    def item(self, item: tuple[str, tuple[Any, ...]]) -> tuple[str, tuple[Any, ...]]:
        kind, args = item
        if kind == 'element':
            start, end, children = args
            return kind, (self.tag(start), self.tag(end), [self.item(child) for child in children])
        if kind == 'start_tag':
            return kind, (self.tag(args[0]),)
        if kind == 'processing_instruction':
            return kind, ({key: self.tok(value) for key, value in args[0].items()},)
        return kind, (self.tok(args[0]),)
