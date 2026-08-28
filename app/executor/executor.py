"""Asynchronous, resource-bounded code executor.

:class:`CodeExecutor` is the abstraction the rest of the app depends on, so a
stronger sandbox (a hardened container, gVisor/nsjail, a microVM, or a remote
execution service) can be dropped in later without touching the bot.

:class:`SubprocessExecutor` is the default Render-free implementation. Each call
spawns exactly one short-lived ``python`` subprocess running
``app/executor/runner.py``. There is **no** process pool and **no** worker pool:
concurrency is capped elsewhere (see :mod:`app.services.rate_limiter`), and this
class is designed to be invoked one execution at a time.

Guarantees provided here:
* Output is drained continuously but only kept up to ``max_output`` bytes per
  stream, so a program that prints forever cannot exhaust memory.
* A wall-clock timeout kills the whole process group (SIGKILL) so busy loops
  such as ``while True: pass`` cannot hang the service.
* The subprocess environment is stripped to a minimal allowlist, so secrets
  (DISCORD_TOKEN, webhook URL, etc.) are never visible to executed code.
"""

from __future__ import annotations

import abc
import asyncio
import os
import signal
import sys
import time
import tempfile
import glob
from pathlib import Path
from typing import Dict, List, Optional

from app.executor.models import ExecutionResult, ExecutionStatus

_RUNNER_PATH = str(Path(__file__).resolve().parent / "runner.py")
_IS_POSIX = os.name == "posix"

# Signal numbers, tolerant of platforms that lack some of them.
_SIGKILL = int(getattr(signal, "SIGKILL", 9))
_SIGXCPU = int(getattr(signal, "SIGXCPU", 24))
_SIGSEGV = int(getattr(signal, "SIGSEGV", 11))
_SIGABRT = int(getattr(signal, "SIGABRT", 6))
_SIGBUS = int(getattr(signal, "SIGBUS", 7))


class _Flag:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = False

    def set(self) -> None:
        self.value = True


def _decode(buffer: bytearray) -> str:
    return bytes(buffer).decode("utf-8", errors="replace")


class CodeExecutor(abc.ABC):
    """Abstract execution backend."""

    @abc.abstractmethod
    async def execute(self, code: str, stdin_input: str = "") -> ExecutionResult:
        """Run ``code`` and return a bounded :class:`ExecutionResult`."""
        raise NotImplementedError


class SubprocessExecutor(CodeExecutor):
    def __init__(
        self,
        *,
        timeout: int = 5,
        max_output: int = 8000,
        max_memory_mb: int = 256,
        python_executable: Optional[str] = None,
        runner_path: Optional[str] = None,
    ) -> None:
        self.timeout = max(1, int(timeout))
        self.max_output = max(256, int(max_output))
        self.max_memory_mb = max(32, int(max_memory_mb))
        self.python_executable = python_executable or sys.executable
        self.runner_path = runner_path or _RUNNER_PATH

    # -- process configuration ------------------------------------------------
    @property
    def _command(self) -> List[str]:
        # -E: ignore environment variables (like PYTHONPATH)
        # -B: don't write .pyc files
        # -X utf8: force UTF-8 stdio regardless of locale
        return [self.python_executable, "-E", "-B", "-X", "utf8", self.runner_path]

    def _build_env(self, code: str) -> Dict[str, str]:
        # Deliberately minimal: no DISCORD_TOKEN / webhook / secrets are ever
        # placed here, so executed code cannot read them.
        env = {
            "PYRUNNER_CODE": code,
            "PYRUNNER_MAX_MEMORY_MB": str(self.max_memory_mb),
            "PYRUNNER_MAX_CPU_SECONDS": str(self.timeout + 1),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
        }
        if not _IS_POSIX:
            # On Windows the interpreter needs SystemRoot to start. This is not
            # a secret; secrets are still excluded. (Production runs on Linux;
            # this only helps local Windows development / test runs.)
            for key in ("SystemRoot", "SYSTEMROOT", "SystemDrive"):
                value = os.environ.get(key)
                if value:
                    env[key] = value
        return env

    async def _spawn(self, code: str, cwd: str, use_stdin: bool) -> asyncio.subprocess.Process:
        kwargs: dict = dict(
            stdin=asyncio.subprocess.PIPE if use_stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_env(code),
            cwd=cwd,
        )
        if _IS_POSIX:
            # New session -> the child is a process-group leader we can kill
            # wholesale (including anything it manages to spawn).
            kwargs["start_new_session"] = True
        return await asyncio.create_subprocess_exec(*self._command, **kwargs)

    # -- output draining ------------------------------------------------------
    async def _drain(
        self, stream: Optional[asyncio.StreamReader], buffer: bytearray, truncated: _Flag
    ) -> None:
        if stream is None:
            return
        cap = self.max_output
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = cap - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated.set()
            else:
                # Keep reading to drain the pipe (avoids the child blocking on a
                # full pipe) but discard the bytes so memory stays bounded.
                truncated.set()

    # -- process lifecycle ----------------------------------------------------
    def _kill(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            if _IS_POSIX:
                try:
                    os.killpg(os.getpgid(proc.pid), _SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    async def _safe_wait(self, proc: asyncio.subprocess.Process, timeout: float) -> bool:
        if proc.returncode is not None:
            return True
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- classification -------------------------------------------------------
    def _classify(
        self, returncode: Optional[int], timed_out: bool, killed_by_us: bool, stderr_text: str
    ) -> ExecutionStatus:
        if timed_out:
            return ExecutionStatus.TIMEOUT
        if returncode is None:
            return ExecutionStatus.INTERNAL_ERROR
        if returncode == 0:
            return ExecutionStatus.SUCCESS
        if returncode > 0:
            # runner.py returns 1 for a user exception / syntax error.
            if "MemoryError" in stderr_text:
                return ExecutionStatus.MEMORY
            return ExecutionStatus.ERROR
        # Negative => terminated by a signal.
        sig = -returncode
        if killed_by_us or sig == _SIGXCPU:
            return ExecutionStatus.TIMEOUT
        if sig in (_SIGKILL, _SIGSEGV, _SIGABRT, _SIGBUS):
            # Most likely OOM / address-space limit hit below the Python level.
            return ExecutionStatus.MEMORY
        return ExecutionStatus.ERROR

    # -- public API -----------------------------------------------------------
    async def execute(self, code: str, stdin_input: str = "") -> ExecutionResult:
        start = time.perf_counter()
        
        # Inject matplotlib override if needed to save plots automatically
        if "matplotlib" in code or "pyplot" in code:
            code = (
                "import matplotlib\n"
                "matplotlib.use('Agg')\n"
                "import matplotlib.pyplot\n"
                "matplotlib.style.use('dark_background')\n"
                "def _hooked_show(*args, **kwargs):\n"
                "    import uuid\n"
                "    matplotlib.pyplot.savefig(f'plot_{uuid.uuid4().hex}.png')\n"
                "matplotlib.pyplot.show = _hooked_show\n"
            ) + code

        # Inject input hook to echo stdin, simulating a real terminal
        if "input" in code:
            code = (
                "import builtins\n"
                "_orig_input = builtins.input\n"
                "def _hooked_input(prompt=''):\n"
                "    val = _orig_input(prompt)\n"
                "    print(val)\n"
                "    return val\n"
                "builtins.input = _hooked_input\n"
            ) + code

        stdout = bytearray()
        stderr = bytearray()
        truncated = _Flag()
        timed_out = False
        killed_by_us = False
        images = []

        with tempfile.TemporaryDirectory() as cwd:
            try:
                proc = await self._spawn(code, cwd, bool(stdin_input))
            except Exception as exc:  # noqa: BLE001
                return ExecutionResult(
                    status=ExecutionStatus.INTERNAL_ERROR,
                    duration=time.perf_counter() - start,
                    message=f"Failed to start execution process: {exc}",
                )

            if stdin_input and proc.stdin:
                try:
                    proc.stdin.write(stdin_input.encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                except Exception:
                    pass

            try:
                drain = asyncio.gather(
                    self._drain(proc.stdout, stdout, truncated),
                    self._drain(proc.stderr, stderr, truncated),
                )
                try:
                    await asyncio.wait_for(drain, timeout=self.timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    killed_by_us = True
                    self._kill(proc)
                    await self._safe_wait(proc, 3.0)
                else:
                    if not await self._safe_wait(proc, 1.0):
                        killed_by_us = True
                        self._kill(proc)
                        await self._safe_wait(proc, 3.0)
            except Exception as exc:  # noqa: BLE001 - never let the bot crash
                killed_by_us = True
                self._kill(proc)
                await self._safe_wait(proc, 3.0)
                return ExecutionResult(
                    status=ExecutionStatus.INTERNAL_ERROR,
                    stdout=_decode(stdout),
                    stderr=_decode(stderr),
                    duration=time.perf_counter() - start,
                    truncated=truncated.value,
                    message=f"Execution harness error: {exc}",
                )
            
            # Read generated images from the temp directory (up to 5)
            for img_path in glob.glob(os.path.join(cwd, "*.png")) + glob.glob(os.path.join(cwd, "*.jpg")):
                if len(images) >= 5:
                    break
                try:
                    with open(img_path, "rb") as f:
                        images.append((os.path.basename(img_path), f.read()))
                except Exception:
                    pass

        duration = time.perf_counter() - start
        out_text = _decode(stdout)
        err_text = _decode(stderr)
        status = self._classify(proc.returncode, timed_out, killed_by_us, err_text)

        if status == ExecutionStatus.MEMORY and not err_text:
            err_text = "MemoryError: the program exceeded the memory limit and was terminated."
        if status == ExecutionStatus.INTERNAL_ERROR and not err_text:
            err_text = "The execution process terminated unexpectedly."

        return ExecutionResult(
            status=status,
            stdout=out_text,
            stderr=err_text,
            duration=duration,
            truncated=truncated.value,
            images=images,
        )


def build_default_executor() -> SubprocessExecutor:
    """Construct a :class:`SubprocessExecutor` from application settings."""
    from app.config import settings

    return SubprocessExecutor(
        timeout=settings.MAX_EXECUTION_TIME,
        max_output=settings.MAX_OUTPUT_LENGTH,
        max_memory_mb=settings.MAX_MEMORY_MB,
    )
