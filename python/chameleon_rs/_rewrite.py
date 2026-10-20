# Ported from Chameleon's src/chameleon/compiler.py (NameTransform), astutil.py
# (NameLookupRewriteVisitor, ItemLookupOnAttributeErrorVisitor) and tales.py
# (transform_attribute), modified: the rewritten expression becomes a lambda taking the scope,
# instead of code inlined into a generated render function.
"""The exact fallback for Python expressions the native fast path does not cover (D4)."""

from __future__ import annotations

import ast
import builtins
from collections.abc import Callable
from typing import Any

# Names the compiler keeps for itself; expressions see the render's own objects.
INTERNALS = frozenset({'econtext', 'rcontext'})

# Names bound from the scope at the start of every Chameleon render function.
DEFAULTS = {'translate': '__translate', 'decode': '__decode', 'on_error_handler': '__on_error_handler'}

BUILTINS = frozenset(builtins.__dict__)


def _econtext() -> ast.Name:
    return ast.Name(id='econtext', ctx=ast.Load())


class _Rewriter(ast.NodeTransformer):
    def __init__(self, default_is_marker: bool) -> None:
        self.scopes: list[set[str]] = [set()]
        self.default_is_marker = default_is_marker

    def visit_arg(self, node: ast.arg) -> ast.arg:
        self.scopes[-1].add(node.arg)
        return node

    def visit_Lambda(self, node: ast.Lambda) -> ast.AST:
        self.scopes.append(set())
        try:
            return self.generic_visit(node)
        finally:
            self.scopes.pop()

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        call = ast.Call(
            func=ast.Name(id='__lookup_attr', ctx=ast.Load()),
            args=[node.value, ast.Constant(node.attr)],
            keywords=[],
        )
        return self.generic_visit(call)

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.scopes[-1]:
            return node
        return self._transform(node)

    def _transform(self, node: ast.Name) -> ast.AST:
        name = node.id
        if name.startswith('__') or name in INTERNALS:
            return node
        if isinstance(node.ctx, ast.Store):
            return ast.Subscript(value=_econtext(), slice=ast.Constant(name), ctx=ast.Store())
        if name in DEFAULTS:
            return ast.Subscript(value=_econtext(), slice=ast.Constant(DEFAULTS[name]), ctx=ast.Load())
        if name == 'default' and self.default_is_marker:
            return ast.Name(id='__default', ctx=ast.Load())
        if name in BUILTINS:
            get = ast.Attribute(value=_econtext(), attr='get', ctx=ast.Load())
            return ast.Call(func=get, args=[ast.Constant(name), ast.Name(id=name, ctx=ast.Load())], keywords=[])
        get_name = ast.Attribute(value=_econtext(), attr='get_name', ctx=ast.Load())
        return ast.Call(func=get_name, args=[ast.Constant(name)], keywords=[])


def build(tree: ast.Expression, default_is_marker: bool, lookup_attr: Callable[[Any, str], Any]) -> Callable[..., Any]:
    """Compile a parsed expression to ``f(econtext, rcontext, __default)``.

    The function's globals hold only builtins and ``lookup_attr``, never the template, so a
    template cannot be kept alive through its own expressions (D4).
    """
    body = _Rewriter(default_is_marker).visit(tree.body)
    params = [ast.arg(arg=name) for name in ('econtext', 'rcontext', '__default')]
    arguments = ast.arguments(posonlyargs=[], args=params, kwonlyargs=[], kw_defaults=[], defaults=[])
    module = ast.Expression(body=ast.Lambda(args=arguments, body=body))
    ast.fix_missing_locations(module)
    code = compile(module, '<template expression>', 'eval')
    return eval(code, {'__builtins__': builtins, '__lookup_attr': lookup_attr})
