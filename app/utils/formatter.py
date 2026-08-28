"""Formatting of execution results into Discord-ready messages.

Pure functions only. The delivery layer (``message_handler``) decides whether a
message is sent inline or, when it would exceed Discord's 2000-character limit,
as a file attachment - using the ``file_text`` carried on
:class:`FormattedMessage`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.executor.models import ExecutionResult, ExecutionStatus

# Discord hard limit for a single message's content.
DISCORD_MESSAGE_LIMIT = 2000
_ZERO_WIDTH_SPACE = "​"
_TRUNCATION_NOTE = (
    "\n\n⚠️ Output was truncated because it exceeded the maximum allowed size."
)


@dataclass
class FormattedMessage:
    content: str
    # Full, un-fenced output for the file fallback when ``content`` is too long.
    file_text: Optional[str] = None
    file_name: str = "output.txt"
    # Generated images as (filename, bytes)
    images: list[tuple[str, bytes]] = None

    def __post_init__(self):
        if self.images is None:
            self.images = []


def _sanitize_for_block(text: str) -> str:
    """Prevent embedded triple backticks from breaking our code fence."""
    return text.replace("```", "``" + _ZERO_WIDTH_SPACE + "`")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if not text:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _with_note(content: str, truncated: bool) -> str:
    return content + _TRUNCATION_NOTE if truncated else content


def format_execution(
    result: ExecutionResult,
    *,
    timeout_seconds: int,
    max_output_length: int,
) -> FormattedMessage:
    """Turn an :class:`ExecutionResult` into a Discord message."""
    status = result.status

    if status == ExecutionStatus.TIMEOUT:
        return FormattedMessage(
            f"⏱️ Execution timed out after {timeout_seconds} seconds."
        )

    if status == ExecutionStatus.INTERNAL_ERROR:
        return FormattedMessage(
            "❌ An internal error occurred while running your code. Please try again."
        )

    duration = f"{result.duration:.2f}s"

    if status == ExecutionStatus.SUCCESS:
        body, was_truncated = _truncate(result.stdout, max_output_length)
        truncated = was_truncated or result.truncated
        if body.strip() == "":
            if result.images:
                content = f"✅ Execution complete\n\n⏱️ {duration}"
            else:
                content = f"✅ Execution complete\n\nNo output.\n\n⏱️ {duration}"
            return FormattedMessage(_with_note(content, truncated), images=result.images)
        content = (
            "✅ Execution complete\n\n"
            f"```\n{_sanitize_for_block(body)}\n```\n\n"
            f"⏱️ {duration}"
        )
        return FormattedMessage(_with_note(content, truncated), images=result.images)

    # ERROR or MEMORY -> show program output (if any) followed by the traceback.
    combined = ""
    if result.stdout.strip():
        combined += result.stdout.rstrip("\n") + "\n"
    combined += result.stderr
    if combined.strip() == "":
        combined = "Execution failed with no output."

    body, was_truncated = _truncate(combined, max_output_length)
    truncated = was_truncated or result.truncated
    content = (
        "❌ Execution failed\n\n"
        f"```\n{_sanitize_for_block(body)}\n```\n\n"
        f"⏱️ {duration}"
    )
    return FormattedMessage(_with_note(content, truncated), images=result.images)


def format_syntax_error(detail: str) -> FormattedMessage:
    """Message shown when validation rejects code for a syntax error."""
    body = detail or "SyntaxError: invalid syntax"
    content = f"❌ Execution failed\n\n```\n{_sanitize_for_block(body)}\n```"
    return FormattedMessage(content, file_text=body)


# --- Control / rejection messages ------------------------------------------
def rate_limited_message(scope: str = "user") -> str:
    if scope == "global":
        return (
            "⚠️ The bot is handling too many requests right now. "
            "Please try again shortly."
        )
    return "⚠️ You are executing code too quickly. Please wait a moment."


def busy_message() -> str:
    return "⚠️ Another execution is currently running. Please try again shortly."


def code_too_long_message(max_length: int) -> str:
    return (
        f"⚠️ Your code is too long. The maximum allowed length is "
        f"{max_length} characters."
    )


def blocked_message(reason: str = "") -> str:
    base = "🚫 Your code was blocked before running for safety reasons."
    if reason:
        return f"{base}\nReason: {reason}"
    return base
