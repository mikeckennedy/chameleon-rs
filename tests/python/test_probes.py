"""Probe cases from the Rust Architect's Phase 2 review, kept as differential tests.

Each case renders through both engines and must give the same output, or the same exception
type. Known, deliberate gaps are listed in DEFERRED with the phase that closes them.
"""

from __future__ import annotations

from typing import Any

import chameleon
import chameleon_rs
import pytest


class Html:
    def __html__(self) -> str:
        return '<b>'


class HtmlNone:
    def __html__(self) -> None:
        return None


class HtmlInt:
    def __html__(self) -> int:
        return 5


class StrSub(str):
    pass


class Gen:
    def __iter__(self) -> Any:
        return iter([1, 2])


class Tr:
    def __str__(self) -> str:
        return '<t>'


CASES: list[tuple[str, str, dict[str, Any]]] = []


def case(name: str, body: str, **kw: Any) -> None:
    CASES.append((name, body, kw))


case('comp', '<p>${[x*2 for x in items]}</p>', items=[1, 2])
case('comp2', '<p>${", ".join(str(i) for i in items)}</p>', items=[1, 2])
case('lambda', '<p>${(lambda y: y+1)(2)}</p>')
case('walrus', '<p>${(z := 3)}${z}</p>')
case('loopself', '<p tal:repeat="i [i]">${i}</p>', i=5)
case('loopnone', '<p tal:repeat="i []">x</p>${i}')
case('loopafter', '<div><p tal:repeat="i [1,2]">${i}</p>${i}</div>')
case('loopgen', '<p tal:repeat="i (x for x in [1,2])">${repeat.i.index}${repeat.i.length}</p>')
case('loopgen2', '<p tal:repeat="i g">${i}</p>', g=Gen())
case('loopnoneit', '<p tal:repeat="i None">${i}</p>')
case('unpack', '<p tal:repeat="(a,b) [(1,2),(3,4)]">${a}${b}</p>')
case('unpack_bad', '<p tal:repeat="(a,b) [(1,2,3)]">${a}${b}</p>')
case('nested ws', '<ul>\n  <li tal:repeat="i [1,2]">\n    <b tal:repeat="j [3,4]">${i}${j}</b>\n  </li>\n</ul>')
case('attr none', '<a href="${None}">x</a>')
case('attr none mixed', '<a href="x${None}">x</a>')
case('attr default', '<a href="${default}" title="t">x</a>')
case('attr false', '<a href="${False}">x</a>')
case('attr quote', """<a title='${q}' alt="${q}">x</a>""", q='a"b\'c<&>')
case('attr bare', '<a title=${q}>x</a>', q='a"b')
case('html', '<p>${h}</p><a t="${h}">x</a>', h=Html())
case('strsub', '<p>${s}</p>', s=StrSub('<x>'))
case('bool', '<p>${True}${1.5}${10**30}</p>')
case('bytes', '<p>${b}</p>', b='é<'.encode())
case('bytesbad', '<p>${b}</p>', b=b'\xff')
case('surrogate', '<p>${s}</p>', s='\ud800')
case('nul', '<p>${s}</p><p tal:content="s"/>', s='a\x00b')
case('content default', '<p tal:content="default">keep <b>me</b></p>')
case('content none', '<p tal:content="None">keep</p>')
case('cond', '<p tal:condition="0">no</p><p tal:condition="1">yes</p>')
case('pipe', '<p>${missing | "fb"}</p>')
case('pipe2', '<p>${1/0 | "fb"}</p>')
case('econtext', '<p>${econtext["x"]}</p>', x=1)
case('builtin override', '<p>${len}</p>', len='mine')
case('nameerr', '<p>${nope}</p>')
case('dict attr', '<p>${d.a}</p>', d={'a': 1})
case('slice', '<p>${s[1:]}</p>', s='abc')
case('fstring', '<p>${f"{x}!"}</p>', x=1)
case('entity', '<p>&amp; &nbsp; &copy;</p>')
case('cmp chain', '<p>${1 < x < 3}</p>', x=2)
case('repeat user', '<p tal:repeat="i [1,2]">${i}</p>', repeat=None)
case('translate name', '<p>${translate("hi")}</p>')
case('interp brace', '<p>${ {"a":1}["a"] }</p>')
case('interp nested', '<p>${"}"}</p>')
case('dollar esc', '<p>$${x}</p>', x=1)
case('inline in attr multi', '<a title="${a} and ${b}">x</a>', a='<', b=None)
case('repeat odd', '<p tal:repeat="i [1,2,3]">${repeat.i.odd}${repeat["i"].number}</p>')
case('cond+repeat', '<p tal:repeat="i [1,2,3]" tal:condition="i > 1">${i}</p>')
case('content+repeat', '<p tal:repeat="i [1,2]" tal:content="i">x</p>')
case('text nul gated', '<p>${s}</p>', s='a&\x00b')
case('content nul gated', '<p tal:content="s"/>', s='a&\x00b')
case('bare nonascii', '<a title=${s}>x</a>', s='a\xe9&')
case('bare nonascii surrogate', '<a title=${s}>x</a>', s='a\ud800&')
case('html none text', '<p>${h}</p>', h=HtmlNone())
case('html none attr', '<a t="${h}">x</a>', h=HtmlNone())
case('html int text', '<p>${h}</p>', h=HtmlInt())
case('html int attr', '<a t="${h}">x</a>', h=HtmlInt())
case('obj str', '<p>${o}</p><a t="${o}">x</a>', o=Tr())
case('comment', '<!-- ${s} --><p>x</p>', s='<&>')
case('cdata', '<script>${s}</script>', s='<&>')
case('attr with entity', '<a t="&amp;${s}">x</a>', s='<')
case('attr expr with entity', '<a t="${s + \'&amp;\'}">x</a>', s='<')
case('content expr entity', '<p tal:content="\'&lt;\'">x</p>')
case('int subclass', '<p>${b}</p>', b=True)
case('neg big', '<p>${-2**70}</p>')
case('float', '<p>${1e300*10}${0.1+0.2}</p>')
case('bytes attr', '<a t="${b}">x</a>', b=b'<x>')
case('translate kw', '<p>${o}</p>', o=Tr(), translate=lambda m, **k: 'T')
case('default text', '<p>${default}</p>')
case('default name', '<p>${default}</p>', default='mine')
case('default content name', '<p tal:content="default">d</p>', default='mine')
case('cond default', '<p tal:condition="default">d</p>', default=0)
case('repeat len', '<p tal:repeat="i \'ab\'">${repeat.i.length}${i}</p>')
case('repeat dict', '<p tal:repeat="k {\'a\':1}">${k}</p>')
case('tal:replace', '<p tal:replace="1">x</p>')
case('xmlns', '<html xmlns:tal="http://xml.zope.org/namespaces/tal"><p tal:content="1">x</p></html>')
case('doctype', '<!DOCTYPE html>\n<p>${1}</p>')
case('self close', '<br/><img src="${1}" />')
case('nested repeat same name', '<div tal:repeat="i [1,2]"><b tal:repeat="i [3]">${i}</b>${i}</div>')
case('repeat restore global', '<div tal:repeat="x [1]">${x}</div>${x}', x='g')


DEFERRED = {
    'walrus': 'Phase 8: Chameleon raises SyntaxError from its generated code, we a TypeError',
    'tal:replace': 'Phase 4/6a: tal:replace',
    'doctype': 'Phase 3: declarations',
}


def outcome(engine: Any, body: str, kw: dict[str, Any]) -> tuple[str, Any]:
    try:
        return 'ok', engine.PageTemplate(body).render(**dict(kw))
    except Exception as e:  # noqa: BLE001 - comparing outcomes across engines
        return 'err', e


@pytest.mark.parametrize(('body', 'kw'), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_probe(request: pytest.FixtureRequest, body: str, kw: dict[str, Any]) -> None:
    name = request.node.callspec.id
    if name in DEFERRED:
        pytest.skip(DEFERRED[name])
    want = outcome(chameleon, body, kw)
    got = outcome(chameleon_rs, body, kw)
    if want[0] == 'err':
        assert got[0] == 'err', got
        # The same exception class by name (Chameleon wraps it in a RenderError subclass).
        assert type(got[1]).__name__ in {c.__name__ for c in type(want[1]).__mro__}, (want, got)
    else:
        assert got == want
