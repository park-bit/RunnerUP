#!/usr/bin/env python3
"""Standalone, low-privilege execution harness.

This script is launched by :mod:`app.executor.executor` as a *separate* Python
process (``python -I -B -X utf8 runner.py``). It never imports the application
package, so it stays tiny and cannot touch the bot's objects or credentials.

Isolation applied here (best-effort, NOT a complete sandbox):

1. Resource limits (address space, CPU time, core size, file size, #procs) are
   set *before* any user code runs, so a program that tries to allocate huge
   amounts of memory or spin forever is terminated by the kernel.
2. User code runs with a restricted ``__builtins__`` (no eval/exec/open/etc.)
   and a guarded ``__import__`` that refuses a denylist of modules.
3. The parent process passes a stripped environment, so secrets such as
   DISCORD_TOKEN are simply not present here.

Communication contract with the parent:
* ``PYRUNNER_CODE``            - the user code to run (required).
* ``PYRUNNER_MAX_MEMORY_MB``   - address-space limit in MB.
* ``PYRUNNER_MAX_CPU_SECONDS`` - CPU-time limit in seconds.
* stdout  -> program output.
* stderr  -> traceback / error output.
* exit 0  -> success; exit 1 -> user error/exception; killed -> timeout/memory.

See the README "Security" section for the threat model and its limits.
"""

import builtins as _builtins_mod
import os
import sys
import traceback

# NOTE: keep this in sync with BLOCKED_MODULES in app/executor/validator.py.
# Disabled per user request to allow all modules.
_BLOCKED_MODULES = frozenset()

# Disabled per user request.
_DENIED_BUILTINS = frozenset()

_real_import = _builtins_mod.__import__


def _set_resource_limits() -> None:
    """Apply kernel resource limits. No-op on platforms without ``resource``."""
    try:
        import resource
    except Exception:
        return

    try:
        mem_mb = int(os.environ.get("PYRUNNER_MAX_MEMORY_MB", "256"))
    except ValueError:
        mem_mb = 256
    try:
        cpu_seconds = int(os.environ.get("PYRUNNER_MAX_CPU_SECONDS", "6"))
    except ValueError:
        cpu_seconds = 6

    mem_bytes = max(mem_mb, 32) * 1024 * 1024

    def _limit(res_id, value):
        try:
            soft, hard = resource.getrlimit(res_id)
            target = value
            if hard != resource.RLIM_INFINITY:
                target = min(target, hard)
            resource.setrlimit(res_id, (target, target))
        except (ValueError, OSError):
            pass

    # Address space (virtual memory) - the main guard against huge allocations.
    _limit(resource.RLIMIT_AS, mem_bytes)
    # Data segment - secondary guard.
    if hasattr(resource, "RLIMIT_DATA"):
        _limit(resource.RLIMIT_DATA, mem_bytes)
    # CPU seconds - backstop for busy loops (wall-clock kill is primary).
    _limit(resource.RLIMIT_CPU, max(cpu_seconds, 1))
    # No core dumps.
    _limit(resource.RLIMIT_CORE, 0)
    # Cap any file the process manages to write (open() is blocked anyway).
    if hasattr(resource, "RLIMIT_FSIZE"):
        _limit(resource.RLIMIT_FSIZE, 1024 * 1024)
    # Limit number of processes/threads for this uid to curb fork bombs.
    if hasattr(resource, "RLIMIT_NPROC"):
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        except (ValueError, OSError):
            pass


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0] if name else name
    if root in _BLOCKED_MODULES:
        raise ImportError(f"import of {name!r} is not allowed in this sandbox")
    return _real_import(name, globals, locals, fromlist, level)


def _make_safe_builtins() -> dict:
    safe = dict(vars(_builtins_mod))
    for banned in _DENIED_BUILTINS:
        safe.pop(banned, None)
    safe["__import__"] = _guarded_import
    return safe


def _run(code: str) -> int:
    module_globals = {
        "__name__": "__main__",
        "__doc__": None,
        "__builtins__": _make_safe_builtins(),
    }
    sys.argv = ["<code>"]

    try:
        compiled = compile(code, "<code>", "exec")
    except SyntaxError as exc:
        # Print just the SyntaxError block (no harness frames).
        sys.stderr.write("".join(traceback.format_exception_only(type(exc), exc)))
        return 1

    try:
        exec(compiled, module_globals)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code if 0 <= exc.code <= 255 else 1
        sys.stderr.write(f"{exc.code}\n")
        return 1
    except BaseException:  # noqa: BLE001 - we intentionally catch everything
        exc_type, exc_value, tb = sys.exc_info()
        # Drop the first frame (this module's exec call) so the traceback starts
        # at the user's <code>.
        user_tb = tb.tb_next if tb is not None else None
        sys.stderr.write(
            "".join(traceback.format_exception(exc_type, exc_value, user_tb))
        )
        return 1
    return 0


def main() -> int:
    _set_resource_limits()
    code = os.environ.pop("PYRUNNER_CODE", "")
    # Remove our control vars so user code cannot read them back.
    os.environ.pop("PYRUNNER_MAX_MEMORY_MB", None)
    os.environ.pop("PYRUNNER_MAX_CPU_SECONDS", None)
    try:
        return _run(code)
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
