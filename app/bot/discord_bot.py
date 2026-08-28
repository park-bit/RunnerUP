"""The Discord client and the intentionally-thin message listener.

The listener does the absolute minimum on every message so that ordinary chat
costs almost nothing::

    if message.author.bot:
        return
    code = extract_python_code(message.content, ...)   # pure, no I/O
    if code is None:
        return
    await handler.handle(message, code)

Only once a real ```` ```python ```` block is found do we hand off to the
pipeline (:class:`~app.bot.message_handler.MessageHandler`). No executor, HTTP,
webhook, subprocess, or expensive AST work is touched for non-code messages.
"""

from __future__ import annotations

import logging

import discord

from app.bot.message_handler import MessageHandler
from app.config import Settings
from app.config import settings as global_settings
from app.utils.code_parser import extract_all_python_code, extract_python_code

log = logging.getLogger("pyrunner.bot")


def build_intents() -> discord.Intents:
    """Return the minimum gateway intents this bot needs.

    ``message_content`` is a PRIVILEGED intent and must ALSO be enabled in the
    Discord Developer Portal (Bot -> Privileged Gateway Intents). Without it,
    ``message.content`` arrives empty and no code blocks can ever be detected.
    """
    intents = discord.Intents.none()
    intents.guilds = True           # guild/channel context
    intents.guild_messages = True   # receive messages in servers
    intents.dm_messages = True      # receive direct messages
    intents.message_content = True  # PRIVILEGED: the actual message text
    return intents


class MockMessage:
    def __init__(self, interaction: discord.Interaction, code: str):
        self.interaction = interaction
        self.author = interaction.user
        self.channel = interaction.channel
        self.content = f"```python\n{code}\n```"
        self.id = interaction.id

    async def reply(self, content=None, **kwargs):
        if not self.interaction.response.is_done():
            await self.interaction.response.send_message(content, **kwargs)
            return await self.interaction.original_response()
        else:
            return await self.interaction.followup.send(content, **kwargs)


class PyRunnerClient(discord.Client):
    """A minimal client that runs Python found in fenced code blocks."""

    def __init__(self, handler: MessageHandler, settings: Settings, **kwargs) -> None:
        kwargs.setdefault("max_messages", None)
        super().__init__(intents=build_intents(), **kwargs)
        self._handler = handler
        self._settings = settings
        self._allow_unmarked = settings.allow_unmarked_blocks()
        self._execute_all = settings.EXECUTE_ALL_BLOCKS
        self.webhook = handler.webhook
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        @self.tree.command(name="code", description="Run Python code directly")
        async def code_command(interaction: discord.Interaction, code: str):
            await interaction.response.defer()
            mock_msg = MockMessage(interaction, code)
            await self._handler.handle(mock_msg, code, self)
        
        await self.tree.sync()

    async def on_ready(self) -> None:
        user = self.user
        log.info(
            "Connected as %s (id=%s) across %d guild(s). "
            "Watching for ```python code blocks.",
            user,
            getattr(user, "id", "?"),
            len(self.guilds),
        )

    async def on_message(self, message: discord.Message) -> None:
        # 1) Ignore all bots (including ourselves). Cheapest possible check,
        #    and it prevents any chance of a self-triggered loop.
        if message.author.bot:
            return

        # 2) Strict detection. Pure string work that returns None for prose,
        #    other languages, or unmarked blocks (unless explicitly allowed).
        
        code = None
        # Check for .py file attachments first
        if message.attachments:
            for attachment in message.attachments:
                if attachment.filename.endswith('.py'):
                    try:
                        code_bytes = await attachment.read()
                        code = code_bytes.decode('utf-8')
                        break
                    except Exception as e:
                        log.warning("Failed to read attachment %s: %s", attachment.filename, e)
        
        # If no valid python file was attached, try parsing the message text
        if code is None:
            content = message.content.strip()
            # If the user used python/py on the first line instead of backticks
            if not self.settings.REQUIRE_PYTHON_CODE_BLOCK and (content.lower().startswith("python\n") or content.lower().startswith("py\n")):
                code = content.split("\n", 1)[1].strip()
            else:
                if self._execute_all:
                    blocks = extract_all_python_code(
                        message.content, allow_unmarked=self._allow_unmarked
                    )
                    if blocks:
                        code = "\n".join(blocks)
                else:
                    extracted = extract_python_code(
                        message.content, allow_unmarked=self._allow_unmarked
                    )
                    if extracted is not None:
                        code = extracted
                    
            if not code:
                return

        # 3) Only now, with confirmed Python in hand, do we spend real work.
        await self._handler.handle(message, code, self)


def build_client(settings: Settings = global_settings) -> PyRunnerClient:
    """Construct and wire the client: executor, limiter, guard, webhook, handler."""
    # Imported lazily so importing this module (e.g. in tests) is cheap and
    # free of side effects.
    from app.executor.executor import build_default_executor
    from app.services.concurrency import ConcurrencyGuard
    from app.services.rate_limiter import RateLimiter
    from app.services.webhook import WebhookNotifier
    from app.services.llm import LLMService

    executor = build_default_executor()
    rate_limiter = RateLimiter(
        per_user=settings.MAX_EXECUTIONS_PER_USER_PER_MINUTE,
        global_limit=settings.MAX_GLOBAL_EXECUTIONS_PER_MINUTE,
        window=settings.RATE_LIMIT_WINDOW_SECONDS,
    )
    guard = ConcurrencyGuard(
        max_concurrent=settings.MAX_CONCURRENT_EXECUTIONS,
        max_queue=settings.MAX_QUEUE_SIZE,
    )
    webhook = WebhookNotifier(settings.DISCORD_WEBHOOK_URL)
    llm_service = LLMService(api_key=settings.GROQ_API_KEY, model=settings.GROQ_MODEL)
    handler = MessageHandler(
        executor=executor,
        rate_limiter=rate_limiter,
        guard=guard,
        webhook=webhook,
        llm_service=llm_service,
        settings=settings,
    )
    return PyRunnerClient(handler, settings)
