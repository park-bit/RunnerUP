"""Optional Discord webhook delivery.

Direct bot replies are the default. When ``OUTPUT_MODE`` is ``webhook`` or
``both`` this module posts the formatted result to a Discord webhook using an
async ``httpx`` client.

The webhook URL is a secret: it is never logged and never exposed to executed
code (it lives only in this process's config, not in the execution subprocess
environment).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

log = logging.getLogger("pyrunner.webhook")

_WEBHOOK_CONTENT_LIMIT = 2000
_NO_MENTIONS = {"parse": []}


class WebhookNotifier:
    def __init__(self, url: str = "") -> None:
        self._url = url or ""
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def send(
        self,
        content: str,
        *,
        file_text: Optional[str] = None,
        file_name: str = "output.txt",
    ) -> bool:
        """Post to the webhook. Returns True on success, False otherwise.

        Never raises; failures are logged without revealing the URL or payload.
        """
        if not self._url:
            return False
        client = self._get_client()
        try:
            if file_text is not None and len(content) > _WEBHOOK_CONTENT_LIMIT:
                short = content[:_WEBHOOK_CONTENT_LIMIT]
                payload = {"content": short, "allowed_mentions": _NO_MENTIONS}
                files = {
                    "files[0]": (
                        file_name,
                        file_text.encode("utf-8", "replace"),
                        "text/plain",
                    )
                }
                resp = await client.post(
                    self._url,
                    data={"payload_json": json.dumps(payload)},
                    files=files,
                )
            else:
                payload = {
                    "content": content[:_WEBHOOK_CONTENT_LIMIT],
                    "allowed_mentions": _NO_MENTIONS,
                }
                resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 - never crash on delivery
            # Log only the exception type, never the URL/content.
            log.warning("Webhook delivery failed (%s)", type(exc).__name__)
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None
