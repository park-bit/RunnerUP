"""LLM service to generate code descriptions via Groq API.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger("pyrunner.llm")


class LLMService:
    def __init__(self, api_key: str = "", model: str = "") -> None:
        self._api_key = api_key or settings.GROQ_API_KEY
        self._model = model or settings.GROQ_MODEL
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def describe_code(self, code: str) -> str:
        """Fetch a brief description of the code from the Groq API.
        
        Returns an empty string on failure or if disabled.
        """
        if not self.enabled:
            return ""

        client = self._get_client()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a helpful assistant. Describe the following Python code snippet briefly in 1-3 sentences. Do not include any output or execution results, just describe what the code does.",
                },
                {
                    "role": "user",
                    "content": code,
                },
            ],
            "max_tokens": 150,
            "temperature": 0.3,
        }

        models_to_try = [self._model, "llama-3.1-8b-instant", "llama-3.3-70b-versatile", "gemma2-9b-it"]
        models_to_try = list(dict.fromkeys(models_to_try))
        
        first_error = ""
        for model in models_to_try:
            payload["model"] = model
            try:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                description = data["choices"][0]["message"]["content"]
                return description.strip()
            except httpx.HTTPStatusError as exc:
                log.warning("Groq API error for model %s: %s", model, exc.response.text)
                if not first_error:
                    first_error = f"API error {exc.response.status_code} ({model}): {exc.response.text}"
                if exc.response.status_code not in (400, 429, 404):
                    break
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to generate code description from Groq (model %s): %s", model, type(exc).__name__)
                if not first_error:
                    first_error = str(exc)
                break

        return f"(Failed to generate description: {first_error})"

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None
