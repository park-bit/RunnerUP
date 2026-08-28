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
    async def handle(self, message: discord.Message, code: str, client: discord.Client = None) -> None:
        """Top-level guard so a failure never bubbles into the event loop."""
        try:
            await self._process(message, code, client)
        except Exception:  # noqa: BLE001 - the listener must never crash
            log.exception(
                "Unhandled error while processing message %s",
                getattr(message, "id", "?"),
            )

    # -- pipeline -------------------------------------------------------------
    async def _process(self, message: discord.Message, code: str, client: discord.Client = None) -> None:
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

        # 3.1) Auto-install dependencies
        deps = validation.extract_dependencies(code)
        if deps:
            import asyncio
            import sys
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install", "-q", *deps,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=60.0)
                if proc.returncode != 0:
                    log.warning("Pip install failed with return code %s", proc.returncode)
            except asyncio.TimeoutError:
                log.warning("Pip install timed out for dependencies: %s", deps)
            except Exception as e:
                log.warning("Failed to install dependencies %s: %s", deps, e)

        # 3.2) Interactive input
        stdin_input = ""
        if client and validation.requires_input(code):
            import asyncio
            
            future = asyncio.Future()
            
            class InputModal(discord.ui.Modal, title='Provide Input for Execution'):
                text_input = discord.ui.TextInput(
                    label='Standard Input',
                    style=discord.TextStyle.paragraph,
                    placeholder='Type your input here...',
                    required=False
                )

                async def on_submit(self, interaction: discord.Interaction):
                    await interaction.response.send_message("Input received! Executing...", ephemeral=True)
                    if not future.done():
                        future.set_result(self.text_input.value)

            class InputView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=20.0)

                @discord.ui.button(label='Provide Input', style=discord.ButtonStyle.primary)
                async def provide_input(self, interaction: discord.Interaction, button: discord.ui.Button):
                    if interaction.user.id != message.author.id:
                        await interaction.response.send_message("Only the author can provide input.", ephemeral=True)
                        return
                    await interaction.response.send_modal(InputModal())

            import re
            prompts = re.findall(r'input\s*\(\s*(["\'])(.*?)\1\s*\)', code)
            prompt_hints = [p[1] for p in prompts if p[1]]
            
            if prompt_hints:
                hints_text = "\n".join(f"- `{h}`" for h in prompt_hints)
                prompt_text = f"**Input Required:** Your code asks for:\n{hints_text}\n\nYou can either **type your input directly in chat** below, or click the button. (times out in 20s)"
            else:
                prompt_text = "**Input Required:** Your code uses `input()`. You can either **type your input directly in chat** below, or click the button. (times out in 20s)"

            view = InputView()
            prompt_msg = await self._reply(
                message, 
                prompt_text, 
                view=view
            )
            
            def check(m):
                return m.author == message.author and m.channel == message.channel
            
            async def wait_for_message():
                try:
                    msg = await client.wait_for('message', check=check, timeout=20.0)
                    if not future.done():
                        future.set_result(msg.content)
                except asyncio.TimeoutError:
                    pass

            msg_task = asyncio.create_task(wait_for_message())
            
            try:
                stdin_input = await asyncio.wait_for(future, timeout=20.0)
            except asyncio.TimeoutError:
                if prompt_msg:
                    await prompt_msg.edit(content="❌ **Input not provided.** Execution cancelled.", view=None)
                return
            finally:
                if not msg_task.done():
                    msg_task.cancel()
                if prompt_msg and future.done() and not future.cancelled():
                    try:
                        await prompt_msg.delete()
                    except Exception:
                        pass

        # 4) Concurrency slot: at most one execution at a time (+ tiny queue).
        if not await self.guard.acquire():
            await self._reply(message, formatter.busy_message())
            return

        # 5) Execute (bounded), releasing the slot no matter what happens.
        # Fire off LLM description concurrently in the background if configured.
        try:
            if self.llm_service and self.llm_service.enabled and self.webhook.enabled:
                import asyncio
                
                async def fetch_and_send_description(code_text: str):
                    desc = await self.llm_service.describe_code(code_text)
                    color = 0xed4245 if desc.startswith("(Failed") else 0x5865F2
                    embed = {
                        "title": "Code Description",
                        "description": desc,
                        "color": color,
                    }
                    await self.webhook.send(embeds=[embed])
                
                asyncio.create_task(fetch_and_send_description(code))
                
            result = await self._execute_with_typing(message, code, stdin_input)
        finally:
            self.guard.release()

        # 6) Format + deliver the execution result.
        formatted = formatter.format_execution(
            result,
            timeout_seconds=s.MAX_EXECUTION_TIME,
            max_output_length=s.MAX_OUTPUT_LENGTH,
        )
        await self._deliver_result(message, formatted)

    async def _execute_with_typing(
        self, message: discord.Message, code: str, stdin_input: str = ""
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
            return await self.executor.execute(code, stdin_input)
        finally:
            if typing_cm is not None:
                with contextlib.suppress(Exception):
                    await typing_cm.__aexit__(None, None, None)

    # -- delivery -------------------------------------------------------------
    async def _deliver_result(
        self, message: discord.Message, formatted: FormattedMessage
    ) -> None:
        """Deliver an execution result honoring ``OUTPUT_MODE``."""
        mode = self.settings.OUTPUT_MODE
        want_bot = mode in ("bot", "both")
        want_webhook = mode in ("webhook", "both")

        # If LLM is enabled, webhook is strictly for descriptions and bot is strictly for output.
        if self.llm_service and self.llm_service.enabled:
            # Always send execution result to channel
            await self._send_channel(message, formatted)
            return

        # Fallback to original behavior if LLM is not enabled
        delivered_webhook = False
        if want_webhook and self.webhook.enabled:
            delivered_webhook = await self.webhook.send(
                formatted.content,
                file_text=formatted.file_text,
                file_name=formatted.file_name,
            )

        if want_bot or not delivered_webhook:
            await self._send_channel(message, formatted)

    async def _send_channel(
        self, message: discord.Message, formatted: FormattedMessage
    ) -> None:
        content = formatted.content
        try:
            files = []
            # Only attach output.txt if the content exceeds Discord's message limit
            needs_file = formatted.file_text is not None and len(content) > DISCORD_MESSAGE_LIMIT
            
            if needs_file:
                files.append(self._as_file(formatted.file_text, formatted.file_name))
            
            for fname, fbytes in formatted.images:
                files.append(discord.File(io.BytesIO(fbytes), filename=fname))

            embed = discord.Embed(description=content[:4096], color=0x2b2d31)
            
            if len(content) <= DISCORD_MESSAGE_LIMIT:
                if files:
                    await message.reply(embed=embed, files=files, allowed_mentions=_NO_PINGS)
                else:
                    await message.reply(embed=embed, allowed_mentions=_NO_PINGS)
                return

            if needs_file:
                fallback_embed = discord.Embed(
                    description=f"⚠️ Output too long. See attached `{formatted.file_name}`.",
                    color=0x2b2d31
                )
                await message.reply(
                    embed=fallback_embed,
                    files=files,
                    allowed_mentions=_NO_PINGS,
                )
            else:
                fallback_embed = discord.Embed(description="⚠️ Output too long, but no file available.", color=0xed4245)
                await message.reply(embed=fallback_embed, allowed_mentions=_NO_PINGS)
        except discord.HTTPException as exc:
            log.warning("Failed to deliver result to channel: %s", type(exc).__name__)

    async def _reply(self, message: discord.Message, content: str, **kwargs) -> Optional[discord.Message]:
        """Send a short control/notice message to the originating channel."""
        try:
            return await message.reply(
                content[:DISCORD_MESSAGE_LIMIT], allowed_mentions=_NO_PINGS, **kwargs
            )
        except discord.HTTPException as exc:
            log.warning("Failed to send notice: %s", type(exc).__name__)
            return None

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
