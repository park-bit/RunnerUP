"""Application entrypoint: the Discord bot and a tiny health server, together.

Render's free *Web Service* expects a process that binds ``$PORT`` and answers
HTTP health checks. A Discord bot, meanwhile, holds a long-lived gateway
websocket. We satisfy both from a single process by running them concurrently on
one asyncio event loop:

* a minimal FastAPI app (``GET /`` and ``GET /health`` -> ``{"status": "ok"}``)
  served by uvicorn, which performs **no** code execution, and
* the discord.py client.

There is no database, no Redis, no background worker, and no self-pinging. All
state (rate-limit windows, the concurrency counter) lives in memory and resets
on restart - expected, documented behavior on the free tier.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import uvicorn
from fastapi import FastAPI

from app.bot.discord_bot import build_client
from app.config import settings

log = logging.getLogger("pyrunner.main")

_VALID_UVICORN_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}


# --------------------------------------------------------------------------- #
# Health app
# --------------------------------------------------------------------------- #
def create_health_app() -> FastAPI:
    """A tiny health app. It performs NO code execution of any kind."""
    app = FastAPI(
        title="discord-python-runner",
        description="Health endpoint for the Discord Python runner bot.",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.api_route("/", methods=["GET", "HEAD"])
    async def root() -> dict:  # noqa: D401 - trivial
        return {"status": "ok"}

    @app.api_route("/health", methods=["GET", "HEAD"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def _configure_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # discord.py is chatty at INFO on the gateway; keep it quieter unless the
    # operator explicitly asked for DEBUG.
    if level > logging.DEBUG:
        logging.getLogger("discord").setLevel(logging.WARNING)


def _uvicorn_log_level() -> str:
    level = settings.LOG_LEVEL.lower()
    return level if level in _VALID_UVICORN_LEVELS else "info"


# --------------------------------------------------------------------------- #
# Concurrent services
# --------------------------------------------------------------------------- #
async def _run_health_server(stop: asyncio.Event) -> None:
    """Serve the health app until ``stop`` is set (or the server dies)."""
    config = uvicorn.Config(
        app=create_health_app(),
        host=settings.HOST,
        port=settings.PORT,
        log_level=_uvicorn_log_level(),
        access_log=False,
    )
    server = uvicorn.Server(config)
    # We manage shutdown centrally, so stop uvicorn from grabbing the signals.
    server.install_signal_handlers = lambda: None  # type: ignore[assignment]

    serve_task = asyncio.create_task(server.serve(), name="uvicorn")
    stop_task = asyncio.create_task(stop.wait(), name="health-stop-wait")
    await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    # Trigger a shutdown of the whole app and unwind cleanly.
    stop.set()
    server.should_exit = True
    if not stop_task.done():
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
    # Re-raise a genuine server failure (e.g. the port is already in use).
    with contextlib.suppress(asyncio.CancelledError):
        await serve_task


async def _run_bot(stop: asyncio.Event) -> None:
    """Run the Discord client until it exits or ``stop`` is set."""
    client = build_client(settings)
    bot_task = asyncio.create_task(
        client.start(settings.DISCORD_TOKEN), name="discord-bot"
    )
    stop_task = asyncio.create_task(stop.wait(), name="bot-stop-wait")
    try:
        done, _pending = await asyncio.wait(
            {bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        # If the bot stopped on its own (usually a fatal error), surface it.
        if bot_task in done and not bot_task.cancelled():
            exc = bot_task.exception()
            if exc is not None:
                raise exc
    finally:
        stop.set()
        if not stop_task.done():
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
        if not client.is_closed():
            with contextlib.suppress(Exception):
                await client.close()
        if not bot_task.done():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await bot_task
        with contextlib.suppress(Exception):
            await client.webhook.close()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def _request_stop() -> None:
        log.info("Shutdown signal received; stopping...")
        stop.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Unavailable on some platforms (e.g. Windows) - fall back to the
            # default KeyboardInterrupt handling in main().
            pass


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
async def _amain() -> int:
    problems = settings.validate()
    if problems:
        for problem in problems:
            log.error("Config error: %s", problem)
        log.error("Refusing to start until the configuration issues above are fixed.")
        return 1

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop)

    log.info(
        "Starting discord-python-runner (health on %s:%d, output mode=%s).",
        settings.HOST,
        settings.PORT,
        settings.OUTPUT_MODE,
    )

    health = asyncio.create_task(_run_health_server(stop), name="health")
    bot = asyncio.create_task(_run_bot(stop), name="bot")
    try:
        await asyncio.gather(bot, health)
    except Exception:
        log.exception("Fatal error; shutting down.")
        stop.set()
        await asyncio.gather(bot, health, return_exceptions=True)
        return 1
    return 0


def main() -> None:
    _configure_logging()
    try:
        exit_code = asyncio.run(_amain())
    except KeyboardInterrupt:
        exit_code = 0
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
