"""Pre-execution validation (defense-in-depth).

Two independent checks are exposed:

* :func:`validate_length` - cheap size guard.
* :func:`validate_security` - static AST analysis that rejects obviously
  dangerous code (blocked imports / builtins / dunder-escape attributes) and
  surfaces syntax errors without needing to spawn a subprocess.

IMPORTANT SECURITY NOTE
-----------------------
This AST blocklist is **not** a real sandbox. A determined attacker can defeat
static analysis of Python. It exists to give friendly, fast rejections for the
common cases and as one layer of defense. The actual (still imperfect) isolation
is provided by the subprocess executor: a separate process with restricted
builtins, a stripped environment (so bot credentials are never visible), CPU and
address-space resource limits, and a hard timeout. See the README security
section. The intended deployment is a trusted/private server.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from typing import List, Optional, Set

# --- Result categories -----------------------------------------------------
CATEGORY_TOO_LONG = "too_long"
CATEGORY_EMPTY = "empty"
CATEGORY_SYNTAX = "syntax"
CATEGORY_SECURITY = "security"


@dataclass
class ValidationResult:
    ok: bool
    category: Optional[str] = None
    reason: str = ""
    # For syntax errors: a pre-formatted, Python-like error string for display.
    detail: str = ""


# --- Blocklists (root module names) ----------------------------------------
# These are conservative. Benign compute modules (math, random, json, datetime,
# itertools, functools, collections, statistics, decimal, re, string, ...) are
# intentionally NOT blocked.
BLOCKED_MODULES = frozenset(
    {
        # process / system
        "os", "sys", "subprocess", "signal", "resource", "gc", "atexit",
        "platform", "getpass", "pwd", "grp", "spwd", "crypt", "nt",
        # filesystem
        "shutil", "pathlib", "tempfile", "glob", "fileinput", "linecache",
        "fcntl", "mmap", "pty", "tty", "termios",
        # networking
        "socket", "ssl", "select", "selectors", "asyncio",
        "requests", "urllib", "urllib2", "urllib3", "http", "httpx", "httplib",
        "aiohttp", "websocket", "websockets", "ftplib", "smtplib", "poplib",
        "imaplib", "telnetlib", "nntplib", "xmlrpc", "socketserver", "wsgiref",
        # low-level / FFI
        "ctypes", "cffi", "_ctypes",
        # import machinery / introspection
        "importlib", "imp", "runpy", "pkgutil", "modulefinder", "inspect",
        "builtins", "__builtin__",
        # serialization that can execute code / touch disk
        "pickle", "cpickle", "_pickle", "marshal", "shelve", "dbm", "sqlite3",
        # concurrency (thread/process bombs)
        "multiprocessing", "threading", "_thread", "thread", "concurrent",
        "sched",
        # debuggers / tracing
        "code", "codeop", "pdb", "bdb", "trace", "tracemalloc",
        # misc side effects
        "webbrowser",
    }
)

# Builtins/identifiers that are dangerous when *used*.
BLOCKED_NAMES = frozenset(
    {
        "eval", "exec", "compile", "open", "input", "__import__",
        "breakpoint", "exit", "quit", "globals", "locals", "vars",
        "memoryview", "setattr", "delattr",
    }
)

# Dunder attributes commonly used to escape a restricted namespace.
BLOCKED_ATTRIBUTES = frozenset(
    {
        "__subclasses__", "__bases__", "__base__", "__mro__", "__subclasshook__",
        "__globals__", "__code__", "__closure__", "__func__", "__self__",
        "__dict__", "__class__", "__builtins__", "__import__", "__loader__",
        "__spec__", "__getattribute__", "__reduce__", "__reduce_ex__",
        "__init_subclass__", "__class_getitem__",
    }
)


def validate_length(code: str, max_length: int) -> ValidationResult:
    """Reject code that is empty or exceeds ``max_length`` characters."""
    if code is None or code.strip() == "":
        return ValidationResult(False, CATEGORY_EMPTY, "The code block is empty.")
    if len(code) > max_length:
        return ValidationResult(
            False,
            CATEGORY_TOO_LONG,
            f"Code is {len(code)} characters; the maximum allowed is {max_length}.",
        )
    return ValidationResult(True)


def _format_syntax_error(err: SyntaxError) -> str:
    """Render a SyntaxError similar to how CPython prints it for a script."""
    lineno = err.lineno or 1
    lines: List[str] = [f'  File "<code>", line {lineno}']
    text = (err.text or "").rstrip("\n")
    if text:
        lines.append("    " + text)
        if err.offset and err.offset > 0:
            lines.append("    " + " " * (err.offset - 1) + "^")
    lines.append(f"{type(err).__name__}: {err.msg or 'invalid syntax'}")
    return "\n".join(lines)


class _SecurityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: List[str] = []

    def _add(self, message: str) -> None:
        if message not in self.violations:
            self.violations.append(message)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in BLOCKED_MODULES:
                self._add(f"import of module '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            root = node.module.split(".")[0]
            if root in BLOCKED_MODULES:
                self._add(f"import from module '{node.module}'")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id in BLOCKED_NAMES:
            self._add(f"use of '{node.id}'")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in BLOCKED_ATTRIBUTES:
            self._add(f"access to attribute '{node.attr}'")
        self.generic_visit(node)


def validate_security(code: str) -> ValidationResult:
    """Static safety check. Also surfaces syntax errors.

    Returns ``ok=True`` only when the code parses cleanly AND contains no
    blocked imports / names / attributes.
    """
    if code is None or code.strip() == "":
        return ValidationResult(False, CATEGORY_EMPTY, "The code block is empty.")

    try:
        tree = ast.parse(code, filename="<code>", mode="exec")
    except SyntaxError as err:
        return ValidationResult(
            False,
            CATEGORY_SYNTAX,
            reason=str(err.msg or "invalid syntax"),
            detail=_format_syntax_error(err),
        )

    visitor = _SecurityVisitor()
    visitor.visit(tree)
    if visitor.violations:
        return ValidationResult(
            False,
            CATEGORY_SECURITY,
            reason="; ".join(visitor.violations[:5]),
        )
    return ValidationResult(True)


def validate(code: str, max_length: int) -> ValidationResult:
    """Convenience: length check followed by the security check."""
    length_result = validate_length(code, max_length)
    if not length_result.ok:
        return length_result
    return validate_security(code)


class _DependencyVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.dependencies: Set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in BLOCKED_MODULES and root not in sys.stdlib_module_names:
                self.dependencies.add(root)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            root = node.module.split(".")[0]
            if root not in BLOCKED_MODULES and root not in sys.stdlib_module_names:
                self.dependencies.add(root)
        self.generic_visit(node)


def extract_dependencies(code: str) -> Set[str]:
    """Parse the code and return a set of allowed third-party root modules."""
    if not code or code.strip() == "":
        return set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    
    visitor = _DependencyVisitor()
    visitor.visit(tree)
    return visitor.dependencies


class _InputVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.has_input = False

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "input":
            self.has_input = True
        self.generic_visit(node)


def requires_input(code: str) -> bool:
    """Return True if the code contains a call to `input()`."""
    if not code or code.strip() == "":
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    
    visitor = _InputVisitor()
    visitor.visit(tree)
    return visitor.has_input
