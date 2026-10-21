# Ported from Chameleon's src/chameleon/compiler.py (NameTransform, the render function's
# bindings of `translate`, `decode` and `on_error_handler`), astutil.py
# (NameLookupRewriteVisitor, ItemLookupOnAttributeErrorVisitor) and tales.py
# (transform_attribute), modified: the rewritten expression becomes a function taking the
# scope, instead of code inlined into a generated render function.
"""The exact fallback for Python expressions the native fast path does not cover (D4)."""

from __future__ import annotations

import ast
import builtins
import dis
import sys
import warnings
from collections.abc import Callable, Iterable
from typing import Any

# Names the compiler keeps for itself; expressions see the render's own objects.
INTERNALS = frozenset({'econtext', 'rcontext'})

# Locals of every Chameleon render function, bound from the scope when it starts. Expressions
# read them as plain names (a comprehension may even rebind them locally).
DEFAULTS = {'translate': '__translate', 'decode': '__decode', 'on_error_handler': '__on_error_handler'}

BUILTINS = frozenset(builtins.__dict__)

# The parameters of a compiled expression: the scope, the render context, the default marker
# (the `default` alias) and the template builtins' values.
PARAMS = ('econtext', 'rcontext', '__rs_default', '__rs_tpl')


def _name_error(name: str) -> Any:
    raise NameError(f'name {name!r} is not defined')


# Module globals of Chameleon's generated code that a `__` name in an expression can reach,
# and the helper that keeps our own names (`__rs_*`, `__lookup_attr`) out of reach.
MODULE_GLOBALS = {'__default': sys.intern('__default__'), '__rs_name_error': _name_error}


def _ours(name: str) -> bool:
    """A name of the compiled function itself (parameters, globals): Chameleon's generated
    code has none of these, so an expression naming one gets a `NameError`, as there."""
    return name.startswith('__rs_') or name == '__lookup_attr'


def _econtext() -> ast.Name:
    return ast.Name(id='econtext', ctx=ast.Load())


def _tpl(name: str) -> ast.Subscript:
    return ast.Subscript(value=ast.Name(id='__rs_tpl', ctx=ast.Load()), slice=ast.Constant(name), ctx=ast.Load())


class Rewriter(ast.NodeTransformer):
    """`NameLookupRewriteVisitor(NameTransform(...))` plus `transform_attribute`, recording
    the scope names the code reads and writes (D9).

    Like Chameleon's visitor, scopes do not chain: a nested lambda sees only its own
    parameters, and a `def` or an import adds its name to the scope it appears in for good.
    """

    def __init__(
        self, aliases: dict[str, str], template_builtins: Iterable[str] = (), own: Callable[[str], bool] | None = None
    ) -> None:
        self.scopes: list[set[str]] = [set()]
        # Names our own generated code uses (the custom-type bridge), left alone.
        self.own = own or (lambda name: False)
        # Compile-time aliases (`default`, `attrs`): name to the parameter or global holding it.
        self.aliases = aliases
        self.template_builtins = frozenset(template_builtins)
        self.reads: set[str] = set()
        self.writes: set[str] = set()
        self.dynamic = False
        # The render-function locals the code uses (`translate`, ...).
        self.locals: set[str] = set()

    def visit_arg(self, node: ast.arg) -> ast.arg:
        self.scopes[-1].add(node.arg)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.scopes[-1].add(node.name)
        return self.generic_visit(node)

    def visit_alias(self, node: ast.alias) -> ast.AST:
        self.scopes[-1].add(node.asname if node.asname is not None else node.name)
        return self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> ast.AST:
        self.scopes.append(set())
        try:
            return self.generic_visit(node)
        finally:
            self.scopes.pop()

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        # The object is visited; the `__lookup_attr` name is ours and stays as it is.
        return ast.Call(
            func=ast.Name(id='__lookup_attr', ctx=ast.Load()),
            args=[self.visit(node.value), ast.Constant(node.attr)],
            keywords=[],
        )

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.scopes[-1]:
            return node
        return self.transform(node)

    def transform(self, node: ast.Name) -> ast.AST:
        name = node.id
        if name.startswith('__'):
            # Left alone: a parameter of the render function if it is a template builtin.
            if name in self.template_builtins and isinstance(node.ctx, ast.Load):
                return _tpl(name)
            if _ours(name) and not self.own(name) and isinstance(node.ctx, ast.Load):
                error = ast.Name(id='__rs_name_error', ctx=ast.Load())
                return ast.Call(func=error, args=[ast.Constant(name)], keywords=[])
            return node
        if name in INTERNALS:
            if isinstance(node.ctx, ast.Load):
                self.dynamic = True
            return node
        if name in DEFAULTS:
            self.locals.add(name)
            self.reads.add(DEFAULTS[name])
            return node
        if isinstance(node.ctx, ast.Store):
            self.writes.add(name)
            return ast.Subscript(value=_econtext(), slice=ast.Constant(name), ctx=ast.Store())
        if name in self.aliases:
            return ast.Name(id=self.aliases[name], ctx=ast.Load())
        self.reads.add(name)
        get = ast.Attribute(value=_econtext(), attr='get', ctx=ast.Load())
        if name in self.template_builtins:
            return ast.Call(func=get, args=[ast.Constant(name), _tpl(name)], keywords=[])
        if name in BUILTINS:
            return ast.Call(func=get, args=[ast.Constant(name), ast.Name(id=name, ctx=ast.Load())], keywords=[])
        get_name = ast.Attribute(value=_econtext(), attr='get_name', ctx=ast.Load())
        return ast.Call(func=get_name, args=[ast.Constant(name)], keywords=[])


def constant_key(value: Any) -> Any:
    """CPython's `_PyCode_ConstantKey`, for the constants `ast.parse` produces: equal values
    of the same type are one constant (`0.0` and `-0.0` are not)."""
    if isinstance(value, (float, complex)):
        return (type(value), repr(value))
    if isinstance(value, tuple):
        return (tuple, tuple(constant_key(v) for v in value))
    return (type(value), value)


def constant(cache: dict[Any, Any], value: Any) -> Any:
    """The template's one object for this constant. Chameleon compiles a template into one
    module, whose compiler merges equal constants (a tuple's items too), so `${x is 1.5}` and
    a `1.5` elsewhere in the template are the same object."""
    if type(value) is tuple:
        value = tuple(constant(cache, v) for v in value)
    return cache.setdefault(constant_key(value), value)


_NO_OPS = frozenset({'RESUME', 'NOP', 'CACHE'})


def fold(cache: dict[Any, Any], node: ast.expr) -> tuple[bool, Any]:
    """Whether the running CPython's compiler folds ``node`` (an operation on constants) into
    one constant, and that constant (from the template's cache). The code is compiled, never
    run, so ``10 ** 10 ** 10`` or ``1 / 0`` (which CPython leaves unfolded) cost nothing; the
    folding rules and size limits are therefore exactly this interpreter's."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        code = compile(ast.Expression(body=node), '<fold>', 'eval')
    ops = [i for i in dis.get_instructions(code) if i.opname not in _NO_OPS]
    if len(ops) == 2 and ops[0].opname in ('LOAD_CONST', 'LOAD_SMALL_INT') and ops[1].opname == 'RETURN_VALUE':
        return True, constant(cache, ops[0].argval)
    return False, None


class _Constants(ast.NodeTransformer):
    def __init__(self, cache: dict[Any, Any]) -> None:
        self.cache = cache

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        node.value = constant(self.cache, node.value)
        return node


def aliases_for(default_is_marker: bool, attrs: dict[str, str] | None) -> dict[str, str]:
    aliases = {}
    if default_is_marker:
        aliases['default'] = '__rs_default'
    if attrs is not None:
        aliases['attrs'] = '__rs_attrs'
    return aliases


def function(
    name: str,
    params: Iterable[str],
    body: list[ast.stmt],
    rewriter: Rewriter,
    env: dict[str, Any],
) -> Callable[..., Any]:
    """Compiles ``def name(*params): body`` (already rewritten) in a module whose globals are
    ``env``, binding the render-function locals the body uses first.

    If CPython rejects the tree (a walrus whose target became ``econtext['x']``), the code is
    unparsed and compiled from source, which raises the ``SyntaxError`` Chameleon's generated
    module raises.
    """
    bindings: list[ast.stmt] = [
        ast.Assign(
            targets=[ast.Name(id=local, ctx=ast.Store())],
            value=ast.Subscript(value=_econtext(), slice=ast.Constant(DEFAULTS[local]), ctx=ast.Load()),
        )
        for local in sorted(rewriter.locals)
    ]
    arguments = ast.arguments(
        posonlyargs=[], args=[ast.arg(arg=p) for p in params], kwonlyargs=[], kw_defaults=[], defaults=[]
    )
    definition = ast.FunctionDef(name=name, args=arguments, body=bindings + body, decorator_list=[], type_params=[])
    module = ast.Module(body=[definition], type_ignores=[])
    ast.fix_missing_locations(module)
    try:
        code = compile(module, '<template expression>', 'exec')
    except (TypeError, ValueError):
        code = compile(ast.unparse(module), '<template expression>', 'exec')
    # Like the namespace Chameleon execs its generated module in: `__builtins__` is the dict.
    namespace = {'__builtins__': builtins.__dict__, **MODULE_GLOBALS, **env}
    exec(code, namespace)  # noqa: S102 - code built from the template's own expressions
    return namespace[name]


def compile_expression(
    tree: ast.Expression,
    default_is_marker: bool,
    attrs: dict[str, str] | None,
    lookup_attr: Callable[[Any, str], Any],
    template_builtins: Iterable[str] = (),
    constants: dict[Any, Any] | None = None,
) -> tuple[Callable[..., Any], list[str], list[str], bool]:
    """Compile a parsed expression to ``f(econtext, rcontext, default_marker, builtins)``.

    ``default_is_marker`` and ``attrs`` are the compile-time aliases in effect where the
    expression appears (``attrs`` is the element's static attributes, or ``None`` outside
    every element); ``template_builtins`` the names of the template builtins.

    Returns the function, the scope names it reads and writes (sorted), and whether it uses
    ``econtext`` or ``rcontext`` as a value (then any name may be read). ``constants`` is the
    template's constant cache (see `constant`).

    The function's globals hold only builtins, ``lookup_attr`` and the ``attrs`` dict, never
    the template, so a template cannot be kept alive through its own expressions (D4).
    """
    if constants is not None:
        tree = _Constants(constants).visit(tree)
    rewriter = Rewriter(aliases_for(default_is_marker, attrs), template_builtins)
    body = rewriter.visit(tree.body)
    func = function(
        '__rs_expression',
        PARAMS,
        [ast.Return(value=body)],
        rewriter,
        {'__lookup_attr': lookup_attr, '__rs_attrs': attrs},
    )
    return func, sorted(rewriter.reads), sorted(rewriter.writes), rewriter.dynamic


def build(
    tree: ast.Expression,
    default_is_marker: bool,
    attrs: dict[str, str] | None,
    lookup_attr: Callable[[Any, str], Any],
) -> Callable[..., Any]:
    """The function alone (the Phase 4 interface; benchmarks load old builds that call it)."""
    return compile_expression(tree, default_is_marker, attrs, lookup_attr)[0]
