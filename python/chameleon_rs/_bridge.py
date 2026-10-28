# The engine interface is ported from Chameleon's src/chameleon/compiler.py
# (`ExpressionEngine.parse`, `ExpressionCompiler.assign_value` / `assign_text` /
# `assign_bool`, `ExpressionTransform.__call__` applying the name rewriting to every
# statement) and the node types of astutil.py (`Symbol`, `Builtin`, `Static`, `Comment`,
# `TokenRef`) as codegen.py emits them, modified: the statements become a Python function,
# and sub-expressions the type asks the engine for are compiled natively.
"""The custom expression-type bridge (D11).

Chameleon lets a template class register its own expression types
(``PageTemplate.expression_types = {..., 'upper': UpperExpr}``): a factory called with the
expression text, returning a callable ``(target, engine) -> list[ast.stmt]`` that assigns the
value to ``target``. Such a type runs here at compile time, exactly as Chameleon calls it; its
statements get Chameleon's name rewriting and become a function the Rust VM calls (at fallback
speed). ``engine.parse(string)`` returns a compiler whose ``assign_value`` / ``assign_text`` /
``assign_bool`` emit a call to a sub-expression the Rust compiler compiles natively, with the
same TALES rules as everywhere else.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Callable, Iterable
from typing import Any, cast

from chameleon_rs._rewrite import PARAMS, Rewriter, aliases_for, function

# (expression text, 'value' | 'text' | 'bool', char_escape or None, s for assign_bool)
Request = tuple[str, str, Any, Any]


class _Engine:
    """What a custom type sees as ``engine``."""

    def __init__(self) -> None:
        self.requests: list[Request] = []

    def parse(self, string: str, handle_errors: bool = True, char_escape: Any = None) -> _Compiler:
        return _Compiler(self, str(string), char_escape)


class _Compiler:
    def __init__(self, engine: _Engine, string: str, char_escape: Any) -> None:
        self.engine = engine
        self.string = string
        self.char_escape = char_escape

    def assign_value(self, target: ast.expr) -> list[ast.stmt]:
        return self._assign(target, 'value', None)

    def assign_text(self, target: ast.expr) -> list[ast.stmt]:
        return self._assign(target, 'text', None)

    def assign_bool(self, target: ast.expr, s: Any) -> list[ast.stmt]:
        return self._assign(target, 'bool', s)

    def _assign(self, target: ast.expr, kind: str, s: Any) -> list[ast.stmt]:
        index = len(self.engine.requests)
        char_escape = None if self.char_escape is None else tuple(self.char_escape)
        self.engine.requests.append((self.string, kind, char_escape, s))
        call = ast.Call(func=ast.Name(id='__rs_sub', ctx=ast.Load()), args=[ast.Constant(index)], keywords=[])
        return [ast.Assign(targets=[target], value=call)]


class _Lower(ast.NodeTransformer):
    """Chameleon's own node types, by class name (they may come from Chameleon's or our
    ``astutil``): a ``Symbol`` becomes a global bound to its value, a ``Builtin`` what that
    name is in Chameleon's render function (see ``_builtin``), a ``Static`` a global evaluated
    once (codegen hoists it to module level); ``Comment`` and ``TokenRef`` statements are
    dropped."""

    def __init__(self, env: dict[str, Any], template_builtins: Iterable[str] = ()) -> None:
        self.env = env
        self.template_builtins = frozenset(template_builtins)

    def _builtin(self, name: str) -> ast.expr:
        """A ``Builtin`` is a bare name in Chameleon's generated code, not rewritten: one of
        the render function's locals (``econtext``, ``rcontext``, ``get``, ``getname``), a
        template builtin (z3c.pt's ``tales``), or a Python builtin. Any other name is left to
        the name rewriting (``translate`` and the other render defaults)."""
        if name in ('econtext', 'rcontext'):
            return ast.Name(id=name, ctx=ast.Load())
        if name in ('get', 'getname'):
            attr = 'get' if name == 'get' else 'get_name'
            return ast.Attribute(value=ast.Name(id='econtext', ctx=ast.Load()), attr=attr, ctx=ast.Load())
        if name in self.template_builtins:
            return ast.Subscript(
                value=ast.Name(id='__rs_tpl', ctx=ast.Load()), slice=ast.Constant(name), ctx=ast.Load()
            )
        if hasattr(builtins, name):
            return self._bind(getattr(builtins, name))
        return ast.Name(id=name, ctx=ast.Load())

    def _bind(self, value: Any) -> ast.Name:
        name = f'__rs_symbol_{len(self.env)}'
        self.env[name] = value
        return ast.Name(id=name, ctx=ast.Load())

    def visit(self, node: ast.AST) -> Any:
        kind = type(node).__name__
        if kind == 'Symbol' and hasattr(node, 'value'):
            return self._bind(node.value)
        if kind == 'Builtin' and hasattr(node, 'id'):
            return self._builtin(cast('str', node.id))
        if kind == 'Static' and hasattr(node, 'value'):
            value = self.visit(cast('ast.AST', node.value))
            expression = ast.Expression(body=value)
            ast.fix_missing_locations(expression)
            namespace = {'__builtins__': builtins, **self.env}
            return self._bind(eval(compile(expression, '<static>', 'eval'), namespace))
        if kind in ('Comment', 'TokenRef'):
            return None
        return super().visit(node)


def _bridge_name(name: str) -> bool:
    """The names the bridge's own statements use (not reachable from template text, whose
    expressions are compiled by the native compiler)."""
    return name in ('__rs_sub', '__rs_result', '__rs_tpl') or name.startswith('__rs_symbol_')


def build(
    factory: Callable[[str], Any],
    expression: str,
    *,
    default_is_marker: bool,
    attrs: dict[str, str] | None,
    lookup_attr: Callable[[Any, str], Any],
    template_builtins: Iterable[str] = (),
) -> tuple[Callable[..., Any], list[Request]]:
    """Runs the custom type and compiles its statements to
    ``f(econtext, rcontext, default_marker, builtins, sub)``. Returns the function and the
    sub-expressions it asked for (index order), for the Rust compiler to compile.

    Exceptions the type raises propagate (``ExpressionError`` drives the Interpolator's retry,
    as in Chameleon).
    """
    engine = _Engine()
    target = ast.Name(id='__rs_result', ctx=ast.Store())
    statements = list(factory(expression)(target, engine))
    env: dict[str, Any] = {'__lookup_attr': lookup_attr, '__rs_attrs': attrs}
    lower = _Lower(env, template_builtins)
    lowered = [s for s in (lower.visit(stmt) for stmt in statements) if s is not None]
    rewriter = Rewriter(aliases_for(default_is_marker, attrs), template_builtins, own=_bridge_name, attributes=False)
    body = [rewriter.visit(stmt) for stmt in lowered]
    body.append(ast.Return(value=ast.Name(id='__rs_result', ctx=ast.Load())))
    # Chameleon generates source from these statements, so contexts do not matter there (a
    # type may load its `target`, a Store name); round-tripping through source does the same.
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    body = ast.parse(ast.unparse(module)).body
    func = function('__rs_custom', (*PARAMS, '__rs_sub'), body, rewriter, env)
    return func, engine.requests
