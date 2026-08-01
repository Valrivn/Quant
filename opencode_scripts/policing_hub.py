"""Local AST policing gate.

Verifies that generated/patch-inserted Python compiles cleanly before it is
allowed to persist, catching malformed syntax injected by repair heuristics.
"""
import ast
from typing import Union


def verify_local_ast(code: Union[str, bytes]) -> bool:
    """Return True if ``code`` parses as valid Python, False otherwise."""
    try:
        ast.parse(code)
        return True
    except (SyntaxError, ValueError, TypeError):
        return False
