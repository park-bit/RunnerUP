"""Message processing pipeline: validate -> rate-limit -> execute -> deliver.

This is the orchestration layer between the thin Discord listener and the
execution/formatting building blocks. It is only ever reached AFTER
``code_parser`` has confirmed the message contains a real Python code block, so
none of this work runs for ordinary chatter.

Pipeline order (cheapest, safest gates first):

1. Length / emptiness check      (just ``len()``)
2. Rate limiting                 (in-memory, O(1) amortized)
3. Static security validation    (AST parse; also catches syntax errors)
4. Concurrency slot              (one execution at a time by default)
5. Execute in a subprocess       (bounded CPU / memory / output / wall-clock)
6. Format + deliver              (bot reply and/or webhook; file fallback)

Delivery rules
--------------
* Execution *results* (success/error/timeout/memory/syntax) honor ``OUTPUT_MODE``
  (``bot`` / ``webhook`` / ``both``), with a bot-reply fallback if a webhook-only
  delivery is unavailable or fails - the user is never left without a reply.
* Short *control notices* (rate-limit, busy, too-long, blocked) always go to the
  originating channel as a reply, since they are direct feedback on the user's
  own message.
"""

from __future__ import annotations

import contextlib
import io
import logging

import discord

from app.config import Settings
from app.executor import validator as validation
from app.executor.executor import CodeExecutor
from app.executor.models import ExecutionResult
from app.services.concurrency import ConcurrencyGuard
from app.services.rate_limiter import RateLimiter
from app.services.webhook import WebhookNotifier
from app.services.llm import LLMService
from app.utils import formatter
from app.utils.formatter import DISCORD_MESSAGE_LIMIT, FormattedMessage

log = logging.getLogger("pyrunner.handler")

# Never ping anyone (including the replied-to author) when the bot posts.
_NO_PINGS = discord.AllowedMentions.none()


class MessageHandler:
    """Owns the per-message execution pipeline and result delivery."""

    def __init__(
        self,
        *,
        executor: CodeExecutor,
        rate_limiter: RateLimiter,
        guard: ConcurrencyGuard,
        webhook: WebhookNotifier,
        llm_service: LLMService | None = None,
        settings: Settings,
    ) -> None:
        self.executor = executor
        self.rate_limiter = rate_limiter
        self.guard = guard
        self.webhook = webhook
        self.llm_service = llm_service
        self.settings = settings

    # -- entry point ----------------------------------------------------------
    async def handle(self, message: discord.Message, code: str) -> None:
        """Top-level guard so a failure never bubbles into the event loop."""
        try:
            await self._process(message, code)
        except Exception:  # noqa: BLE001 - the listener must never crash
            log.exception(
                "Unhandled error while processing message %s",
                getattr(message, "id", "?"),
            )

    # -- pipeline -------------------------------------------------------------
    async def _process(self, message: discord.Message, code: str) -> None:
        s = self.settings

        # 1) Size / emptiness. The cheapest possible gate.
        length = validation.validate_length(code, s.MAX_CODE_LENGTH)
        if not length.ok:
            if length.category == validation.CATEGORY_TOO_LONG:
                await self._reply(
                    message, formatter.code_too_long_message(s.MAX_CODE_LENGTH)
                )
            # Empty blocks are silently ignored (the parser already drops them).
            return

        # 2) Rate limiting (per-user + global). No execution work yet.
        decision = self.rate_limiter.try_acquire(message.author.id)
        if not decision.allowed:
            await self._reply(
                message, formatter.rate_limited_message(decision.scope or "user")
            )
            return

        # 3) Static validation: AST security scan (also reports syntax errors)
        #    before we pay for a subprocess.
        security = validation.validate_security(code)
        if not security.ok:
            if security.category == validation.CATEGORY_SYNTAX:
                await self._deliver_result(
                    message, formatter.format_syntax_error(security.detail)
                )
            elif security.category == validation.CATEGORY_SECURITY:
                await self._reply(message, formatter.blocked_message(security.reason))
            return

        # 4) Concurrency slot: at most one execution at a time (+ tiny queue).
        if not await self.guard.acquire():
            await self._reply(message, formatter.busy_message())
            return

        # 5) Execute (bounded), releasing the slot no matter what happens.
        # Run LLM description concurrently if configured.
        try:
            if self.llm_service and self.llm_service.enabled and self.webhook.enabled:
                import asyncio
                result, description = await asyncio.gather(
                    self._execute_with_typing(message, code),
                    self.llm_service.describe_code(code),
                )
            else:
                result = await self._execute_with_typing(message, code)
                description = ""
        finally:
            self.guard.release()

        # 6) Format + deliver the execution result.
        formatted = formatter.format_execution(
            result,
            timeout_seconds=s.MAX_EXECUTION_TIME,
            max_output_length=s.MAX_OUTPUT_LENGTH,
        )
        await self._deliver_result(message, formatted, description=description)

    async def _execute_with_typing(
        self, message: discord.Message, code: str
    ) -> ExecutionResult:
        """Run the code, showing a typing indicator while it works.

        The typing indicator is purely cosmetic; if it cannot be started (e.g.
        missing permissions) execution proceeds anyway.
        """
        typing_cm = None
        try:
            typing_cm = message.channel.typing()
            await typing_cm.__aenter__()
        except Exception:  # noqa: BLE001 - typing is optional
            typing_cm = None
        try:
            return await self.executor.execute(code)
        finally:
            if typing_cm is not None:
                with contextlib.suppress(Exception):
                    await typing_cm.__aexit__(None, None, None)

    # -- delivery -------------------------------------------------------------
    async def _deliver_result(
        self, message: discord.Message, formatted: FormattedMessage, *, description: str = ""
    ) -> None:
        """Deliver an execution result honoring ``OUTPUT_MODE``."""
        mode = self.settings.OUTPUT_MODE
        want_bot = mode in ("bot", "both")
        want_webhook = mode in ("webhook", "both")

        delivered_webhook = False
        if self.webhook.enabled:
            if description:
                # If we generated a description, send it to the webhook
                delivered_webhook = await self.webhook.send(f"**Code Description:**\n{description}")
            elif want_webhook:
                # Otherwise, if webhook output is requested, send the output to the webhook
                delivered_webhook = await self.webhook.send(
                    formatted.content,
                    file_text=formatted.file_text,
                    file_name=formatted.file_name,
                )

        # Send to the channel when requested, or as a fallback whenever the
        # webhook path did not actually deliver anything. If a description was
        # sent to the webhook, we definitely want the bot to reply with the output.
        if want_bot or not delivered_webhook or description:
            await self._send_channel(message, formatted)

    async def _send_channel(
        self, message: discord.Message, formatted: FormattedMessage
    ) -> None:
        content = formatted.content
        try:
            if len(content) <= DISCORD_MESSAGE_LIMIT:
                await message.reply(content, allowed_mentions=_NO_PINGS)
                return
            if formatted.file_text is not None:
                header = self._file_header(content)
                attachment = self._as_file(formatted.file_text, formatted.file_name)
                await message.reply(
                    header, file=attachment, allowed_mentions=_NO_PINGS
                )
                return
            # No file fallback available -> hard-trim to Discord's limit.
            await message.reply(
                content[:DISCORD_MESSAGE_LIMIT], allowed_mentions=_NO_PINGS
            )
        except discord.HTTPException as exc:
            log.warning("Failed to deliver result to channel: %s", type(exc).__name__)

    async def _reply(self, message: discord.Message, content: str) -> None:
        """Send a short control/notice message to the originating channel."""
        try:
            await message.reply(
                content[:DISCORD_MESSAGE_LIMIT], allowed_mentions=_NO_PINGS
            )
        except discord.HTTPException as exc:
            log.warning("Failed to send notice: %s", type(exc).__name__)

    @staticmethod
    def _as_file(text: str, name: str) -> discord.File:
        data = io.BytesIO(text.encode("utf-8", "replace"))
        return discord.File(data, filename=name or "output.txt")

    @staticmethod
    def _file_header(content: str) -> str:
        first_line = content.split("\n", 1)[0].strip() or "Result"
        header = (
            f"{first_line}\n"
            "📎 Output was too long to display here; see the attached file."
        )
        return header[:DISCORD_MESSAGE_LIMIT]
