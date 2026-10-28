"""Chameleon's expression type classes subclassed the way z3c.pt (Zope, Plone) does it (#9):
the classes compile natively when a template names them, and a subclass compiles as
Chameleon's would, through the AST bridge. Each case renders on both engines."""

from __future__ import annotations

import ast
import functools
from typing import Any, ClassVar

import chameleon
import chameleon.astutil
import chameleon.compiler
import chameleon.tales
import chameleon.utils
import chameleon_rs
import chameleon_rs.astutil
import chameleon_rs.compiler
import chameleon_rs.tales
import chameleon_rs.utils
import pytest
from exprtest import render

ENGINES = [chameleon, chameleon_rs]


def subclassing_template(engine: Any) -> Any:
    """A template class with types built as z3c.pt builds its own."""
    tales = engine.tales
    astutil = engine.astutil

    class PythonExpr(tales.PythonExpr):
        # z3c.pt: compiles with `translate`, and `path()` reaches a template builtin.
        builtins: ClassVar[dict[str, Any]] = {
            'path': engine.codegen.template(
                'tales(econtext, rcontext, name)',
                tales=astutil.Builtin('tales'),
                name=ast.Constant('python'),
                mode='eval',
            )
        }

        def __call__(self, target: Any, engine_: Any) -> Any:
            return self.translate(self.expression, target)

        def rewrite(self, node: ast.Name) -> Any:
            builtin = self.builtins.get(node.id)
            if builtin is not None:
                return engine.codegen.template(
                    'get(name) if get(name) is not None else builtin',
                    get=astutil.Builtin('get'),
                    name=ast.Constant(node.id),
                    builtin=builtin,
                    mode='eval',
                )
            return None

        @property
        def transform(self) -> Any:
            return astutil.NameLookupRewriteVisitor(self.rewrite)

    class ExistsExpr(tales.ExistsExpr):
        # z3c.pt: normalizes its text and leaves compiling to the base class.
        def __init__(self, expression: str) -> None:
            super().__init__('nocall:' + expression)

    class NocallExpr(tales.PythonExpr):
        pass

    class NotExpr(tales.NotExpr):
        pass

    class StructureExpr(tales.StructureExpr):
        pass

    class ImportExpr(tales.ImportExpr):
        pass

    class Template(engine.PageTemplate):
        expression_types: ClassVar[dict[str, Any]] = {
            **engine.PageTemplate.expression_types,
            'python': PythonExpr,
            'exists': ExistsExpr,
            'nocall': NocallExpr,
            'not': NotExpr,
            'structure': StructureExpr,
            'import': ImportExpr,
        }

        @property
        def builtins(self) -> dict[str, Any]:
            builtins = {**super().builtins}
            builtins['tales'] = engine.compiler.ExpressionEvaluator(self.engine, builtins)
            return builtins

    return Template


CASES = [
    ('<p>${x.upper()}</p>', {'x': 'a'}),
    ('<p tal:content="x.title">t</p>', {'x': {'title': 'from a key'}}),
    ('<p tal:content="missing | x">t</p>', {'x': 'fallback'}),
    ('<p tal:content="python: path(\'x + 1\')">t</p>', {'x': 1}),
    ('<p tal:content="exists: x">t</p>', {'x': 1}),
    ('<p tal:content="exists: missing">t</p>', {}),
    ('<p tal:content="not: x">t</p>', {'x': 0}),
    ('<p tal:content="structure: x">t</p>', {'x': '<b>b</b>'}),
    ("<p tal:content=\"import: os.path.join('a', 'b')\">t</p>", {}),
    ('<p tal:content="python: 1 +">t</p>', {}),
    ('<p class="a-${x} b-${y}">t</p>', {'x': 1, 'y': 2}),
]


@pytest.mark.parametrize(('source', 'scope'), CASES)
def test_subclassed_types_compile_as_chameleons(source: str, scope: dict[str, Any]) -> None:
    want = render(chameleon, source, scope, subclassing_template(chameleon))
    got = render(chameleon_rs, source, scope, subclassing_template(chameleon_rs))
    assert got == want


def test_named_types_still_compile_natively() -> None:
    """Only a subclass goes through the bridge: the classes themselves stay native."""
    for cls in (
        chameleon_rs.tales.PythonExpr,
        chameleon_rs.tales.ExistsExpr,
        chameleon_rs.tales.NotExpr,
        chameleon_rs.tales.StructureExpr,
        chameleon_rs.tales.ImportExpr,
    ):
        assert chameleon_rs.tales._spec(cls)[0] != 'custom'
        assert chameleon_rs.tales._spec(type('Sub', (cls,), {}))[0] == 'custom'


@pytest.mark.parametrize('engine', ENGINES)
def test_template_engine_and_expression_parser(engine: Any) -> None:
    template = engine.PageTemplate('<p/>')
    parser = template.expression_parser
    assert parser.factories is template.expression_types
    assert parser.default == 'python'
    assert template.engine().__class__ is engine.compiler.ExpressionEngine


@pytest.mark.parametrize('engine', ENGINES)
def test_evaluator_without_a_python_type(engine: Any) -> None:
    """z3c.pt's evaluator knows only `path`; the result still comes out."""
    parser = engine.tales.ExpressionParser({'upper': chameleon_rs_upper(engine)}, 'upper')
    evaluator = engine.compiler.ExpressionEvaluator(
        functools.partial(engine.compiler.ExpressionEngine, parser), {'unused': 1}
    )
    assert evaluator(engine.utils.Scope({'x': 'abc'}), {}, 'upper', 'x') == 'ABC'


def chameleon_rs_upper(engine: Any) -> Any:
    class UpperExpr(engine.tales.TalesExpr):
        def translate(self, expression: str, target: Any) -> Any:
            return engine.codegen.template(
                'target = str(name).upper()', target=target, name=ast.Name(id=expression.strip(), ctx=ast.Load())
            )

    return UpperExpr


@pytest.mark.parametrize('engine', ENGINES)
def test_chameleons_doctest_helper(engine: Any) -> None:
    tales = engine.tales
    assert tales.test(tales.PythonExpr('foo | 2 + 2 | 5')) == 4
    assert tales.test(tales.ExistsExpr('int(None)'), tales.SimpleEngine(tales.PythonExpr)) == 0
