"""Tests for the length and AST-based security validator."""

from app.executor.validator import (
    CATEGORY_EMPTY,
    CATEGORY_SECURITY,
    CATEGORY_SYNTAX,
    CATEGORY_TOO_LONG,
    validate,
    validate_length,
    validate_security,
)


# --- allowed code ----------------------------------------------------------
def test_plain_arithmetic_passes():
    assert validate_security("x = 1 + 2\nprint(x)").ok is True


def test_safe_imports_pass():
    code = "import math, random, json\nfrom datetime import datetime\nprint(math.pi)"
    assert validate_security(code).ok is True


def test_comprehensions_and_functions_pass():
    code = "def f(n):\n    return [i * i for i in range(n)]\nprint(f(5))"
    assert validate_security(code).ok is True


# --- dangerous imports -----------------------------------------------------
def test_import_os_blocked():
    res = validate_security("import os\nprint(os.getcwd())")
    assert res.ok is False
    assert res.category == CATEGORY_SECURITY


def test_import_submodule_blocked():
    res = validate_security("import os.path")
    assert res.ok is False
    assert res.category == CATEGORY_SECURITY


def test_from_subprocess_blocked():
    res = validate_security("from subprocess import run\nrun(['ls'])")
    assert res.ok is False
    assert res.category == CATEGORY_SECURITY


def test_import_socket_blocked():
    res = validate_security("import socket")
    assert res.ok is False
    assert res.category == CATEGORY_SECURITY


# --- dangerous builtins ----------------------------------------------------
def test_eval_blocked():
    res = validate_security("eval('1 + 1')")
    assert res.ok is False
    assert res.category == CATEGORY_SECURITY


def test_exec_blocked():
    res = validate_security("exec('x = 1')")
    assert res.ok is False
    assert res.category == CATEGORY_SECURITY


def test_open_blocked():
    res = validate_security("open('/etc/passwd').read()")
    assert res.ok is False
    assert res.category == CATEGORY_SECURITY


def test_dunder_escape_attribute_blocked():
    res = validate_security("print(().__class__.__bases__)")
    assert res.ok is False
    assert res.category == CATEGORY_SECURITY


# --- syntax errors ---------------------------------------------------------
def test_syntax_error_is_reported():
    res = validate_security("def broken(:\n    pass")
    assert res.ok is False
    assert res.category == CATEGORY_SYNTAX
    assert "SyntaxError" in res.detail
    assert "<code>" in res.detail


def test_unterminated_string_is_syntax_error():
    res = validate_security("print('unterminated)")
    assert res.ok is False
    assert res.category == CATEGORY_SYNTAX


# --- length / emptiness ----------------------------------------------------
def test_length_ok():
    assert validate_length("print(1)", 5000).ok is True


def test_length_too_long():
    res = validate_length("x" * 100, 50)
    assert res.ok is False
    assert res.category == CATEGORY_TOO_LONG


def test_length_empty():
    res = validate_length("", 5000)
    assert res.ok is False
    assert res.category == CATEGORY_EMPTY


def test_security_empty():
    res = validate_security("   \n  ")
    assert res.ok is False
    assert res.category == CATEGORY_EMPTY


# --- combined convenience --------------------------------------------------
def test_validate_checks_length_before_security():
    # Too long -> reported as length, security never reached.
    res = validate("import os", max_length=3)
    assert res.ok is False
    assert res.category == CATEGORY_TOO_LONG


def test_validate_reaches_security_when_length_ok():
    res = validate("import os", max_length=5000)
    assert res.ok is False
    assert res.category == CATEGORY_SECURITY
