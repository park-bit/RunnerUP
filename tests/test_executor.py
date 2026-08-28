"""Tests for the subprocess-based code executor.

These spawn a real short-lived Python subprocess running ``runner.py``. The
functional tests use a generous memory limit so they never trip the address
-space guard on machines with unusual virtual-memory behavior; a dedicated
POSIX-only test exercises the memory limit with a deliberately huge allocation.
"""

import asyncio
import os
import sys

import pytest

from app.executor.executor import SubprocessExecutor
from app.executor.models import ExecutionStatus

POSIX = os.name == "posix"


def _run(coro):
    return asyncio.run(coro)


def _make_executor(timeout=5, max_output=8000, max_memory_mb=512):
    return SubprocessExecutor(
        timeout=timeout,
        max_output=max_output,
        max_memory_mb=max_memory_mb,
        python_executable=sys.executable,
    )


def test_normal_output():
    result = _run(_make_executor().execute("print('hello world')"))
    assert result.status == ExecutionStatus.SUCCESS
    assert result.stdout.strip() == "hello world"
    assert result.stderr == ""


def test_no_output():
    result = _run(_make_executor().execute("x = 1 + 1"))
    assert result.status == ExecutionStatus.SUCCESS
    assert result.stdout.strip() == ""


def test_multiline_program_output():
    code = "for i in range(3):\n    print(i)"
    result = _run(_make_executor().execute(code))
    assert result.status == ExecutionStatus.SUCCESS
    assert result.stdout.split() == ["0", "1", "2"]


def test_runtime_exception_is_reported():
    result = _run(_make_executor().execute("raise ValueError('boom')"))
    assert result.status == ExecutionStatus.ERROR
    assert "ValueError" in result.stderr
    assert "boom" in result.stderr


def test_traceback_starts_at_user_code():
    result = _run(_make_executor().execute("raise RuntimeError('x')"))
    # The harness frame must be stripped so the user sees a clean traceback.
    assert "runner.py" not in result.stderr
    assert "<code>" in result.stderr


def test_output_captured_before_exception():
    code = "print('partial')\nraise SystemError('later')"
    result = _run(_make_executor().execute(code))
    assert result.status == ExecutionStatus.ERROR
    assert "partial" in result.stdout
    assert "SystemError" in result.stderr


def test_timeout_on_infinite_loop():
    result = _run(_make_executor(timeout=1).execute("while True:\n    pass"))
    assert result.status == ExecutionStatus.TIMEOUT


def test_excessive_output_is_truncated():
    # 256 is the executor's minimum output cap; produce far more than that.
    ex = _make_executor(max_output=256)
    result = _run(ex.execute("for _ in range(10000):\n    print('A' * 50)"))
    assert result.status == ExecutionStatus.SUCCESS
    assert result.truncated is True
    assert len(result.stdout) <= 256


def test_syntax_error_from_runner():
    # Validation normally catches this first, but the runner must handle it too.
    result = _run(_make_executor().execute("def bad(:\n    pass"))
    assert result.status == ExecutionStatus.ERROR
    assert "SyntaxError" in result.stderr


def test_blocked_import_fails_in_runner():
    # Defense-in-depth: even if validation were bypassed, the runner refuses.
    result = _run(_make_executor().execute("import os\nprint(os.getcwd())"))
    assert result.status == ExecutionStatus.ERROR
    assert ("ImportError" in result.stderr) or ("not allowed" in result.stderr)


def test_restricted_builtin_open_fails_in_runner():
    result = _run(_make_executor().execute("open('/etc/passwd')"))
    assert result.status == ExecutionStatus.ERROR
    # ``open`` is removed from builtins, so referencing it is a NameError.
    assert "NameError" in result.stderr


def test_build_env_excludes_secrets(monkeypatch):
    # The subprocess environment must never carry the bot's secrets, so even a
    # program that could read os.environ (it cannot - os is blocked) would find
    # nothing sensitive.
    monkeypatch.setenv("DISCORD_TOKEN", "super-secret-token")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://secret.example/webhook")
    env = _make_executor()._build_env("print('hi')")
    assert "DISCORD_TOKEN" not in env
    assert "DISCORD_WEBHOOK_URL" not in env
    for value in env.values():
        assert "super-secret-token" not in value
        assert "secret.example" not in value


@pytest.mark.skipif(not POSIX, reason="resource limits are POSIX-only")
def test_memory_limit_terminates_program():
    # Allocate far more than the limit to force termination by the kernel or a
    # MemoryError inside the interpreter.
    ex = _make_executor(timeout=5, max_memory_mb=256)
    result = _run(ex.execute("x = bytearray(1_500_000_000)\nprint(len(x))"))
    assert result.status != ExecutionStatus.SUCCESS
    assert result.status in (ExecutionStatus.MEMORY, ExecutionStatus.ERROR)
