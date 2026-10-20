"""
Census of the expressions in a set of real templates, recorded by the reference Chameleon
while it compiles them. Answers: which TALES expression types are used, and which Python AST
node types appear in `python:` expressions (i.e. how much the D4 fast path would cover).

Run: venv/bin/python bench/expression_census.py DIR [DIR ...]
"""

import ast
import collections
import sys
from pathlib import Path

from chameleon import PageTemplateFile, tales

# Node types the D4 fast-path whitelist would lower to Rust (everything else falls back).
FAST_PATH_NODES = {
    'Expression',
    'Name',
    'Load',
    'Constant',
    'Attribute',
    'Subscript',
    'Slice',
    'Call',
    'keyword',
    'BinOp',
    'UnaryOp',
    'BoolOp',
    'Compare',
    'IfExp',
    'Tuple',
    'List',
    'Dict',
    'Add',
    'Sub',
    'Mult',
    'Div',
    'FloorDiv',
    'Mod',
    'Pow',
    'BitAnd',
    'BitOr',
    'BitXor',
    'LShift',
    'RShift',
    'MatMult',
    'Not',
    'USub',
    'UAdd',
    'Invert',
    'And',
    'Or',
    'Eq',
    'NotEq',
    'Lt',
    'LtE',
    'Gt',
    'GtE',
    'Is',
    'IsNot',
    'In',
    'NotIn',
}

prefixes: collections.Counter[str] = collections.Counter()
python_exprs: list[str] = []
failures: list[tuple[Path, str]] = []

_orig_parser_call = tales.ExpressionParser.__call__
_orig_python_translate = tales.PythonExpr.translate


def recording_parser_call(self, expression):
    m = tales.match_prefix(expression)
    prefixes[m.group(1) if m else f'(default: {self.default})'] += 1
    return _orig_parser_call(self, expression)


def recording_python_translate(self, expression, target):
    python_exprs.append(str(expression).strip())
    return _orig_python_translate(self, expression, target)


tales.ExpressionParser.__call__ = recording_parser_call
tales.PythonExpr.translate = recording_python_translate


def main(dirs: list[str]) -> None:
    paths = [p for d in dirs for p in sorted(Path(d).rglob('*.pt'))]
    for path in paths:
        try:
            PageTemplateFile(str(path)).cook_check()
        except Exception as exc:  # noqa: BLE001 - census: record and keep going
            failures.append((path, f'{type(exc).__name__}: {str(exc).splitlines()[0]}'))

    node_counts: collections.Counter[str] = collections.Counter()
    fallback_reasons: collections.Counter[str] = collections.Counter()
    fast = 0
    for src in python_exprs:
        try:
            tree = ast.parse(src.replace('\n', ' '), mode='eval')
        except SyntaxError:
            fallback_reasons['(syntax error: an Interpolator retry candidate)'] += 1
            continue
        kinds = {type(n).__name__ for n in ast.walk(tree)}
        node_counts.update(kinds)
        outside = kinds - FAST_PATH_NODES
        if outside:
            fallback_reasons.update(outside)
        else:
            fast += 1

    parsed = len(python_exprs) - fallback_reasons['(syntax error: an Interpolator retry candidate)']
    print(f'{len(paths)} templates, {len(failures)} failed to compile')
    for path, err in failures:
        print(f'  FAILED {path.name}: {err}')
    print(f'\nexpression types ({sum(prefixes.values())} expressions):')
    for name, count in prefixes.most_common():
        print(f'  {count:6d}  {name}')
    print(f'\npython expressions: {parsed} parsed; fast-path eligible: {fast} ({fast / parsed:.1%})')
    print('constructs that force the fallback (expressions containing each):')
    for name, count in fallback_reasons.most_common():
        print(f'  {count:6d}  {name}')
    print('\nAST node types present (expressions containing each):')
    for name, count in node_counts.most_common():
        print(f'  {count:6d}  {name}')


if __name__ == '__main__':
    main(sys.argv[1:])
