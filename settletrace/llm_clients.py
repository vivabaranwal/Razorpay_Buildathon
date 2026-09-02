"""LLM clients for the explanation layer.

Three providers behind one tiny interface: send a system prompt and a user
prompt, get back a string. That is the entire surface the explanation layer
needs, which is why swapping providers changes nothing about how
reconciliation works - the model only ever produces advisory prose.

Gemini and Groq are called over plain HTTPS with ``httpx`` rather than through
their SDKs. The request is one POST with a small JSON body, so an SDK would add
a dependency and a version-compatibility risk for no benefit. Anthropic keeps
its SDK because it was already there and already tested.

Every client returns ``None`` rather than raising. A failed explanation is not
a failed reconciliation, and the caller degrades to template text.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30.0

# Generous relative to the two or three sentences actually wanted, because
# current models spend most of this budget on internal reasoning before
# emitting anything: at 300 tokens Gemini used 286 on thought and had 10 left
# for the answer, which arrived truncated mid-sentence. The prompt, not the
# ceiling, is what keeps explanations short.
MAX_OUTPUT_TOKENS = 2000


class AuthFailure(Exception):
    """The provider rejected the key.

    Distinguished from a transient failure because it is permanent until the
    key changes: the header badge downgrades on this, but not on a timeout.
    """


class LLMClient(Protocol):
    """What the explanation layer needs from a model provider."""

    name: str
    model: str

    def complete(self, system: str, user: str) -> str | None:
        """Return the model's text, or None if the call failed."""
        ...

    async def complete_async(self, system: str, user: str) -> str | None:
        ...


def _is_auth_error(status: int) -> bool:
    # 400 is included for Gemini, which returns it for a malformed API key
    # rather than the 401 most providers use.
    return status in (400, 401, 403)


class GeminiClient:
    """Google Gemini via the Generative Language REST API.

    Chosen as the default because aistudio.google.com issues a key for free
    with no card, which matters more here than model capability - the job is
    three sentences of plain-language explanation.
    """

    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self.model = model

    def _payload(self, system: str, user: str) -> dict:
        return {
            # Gemini takes the system prompt as its own field rather than as a
            # message role, unlike the OpenAI-shaped APIs.
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
                "temperature": 0.3,
            },
        }

    @property
    def _url(self) -> str:
        return f"{self.BASE}/{self.model}:generateContent"

    def _headers(self) -> dict:
        # The key travels as a header, not a query parameter: a URL ends up in
        # proxy logs and error messages, and this one would carry the secret.
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    @staticmethod
    def _extract(data: dict) -> str | None:
        candidates = data.get("candidates") or []
        if not candidates:
            return None

        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()

        # A response cut off at the token ceiling arrives as a half sentence.
        # Better a complete template than a fragment shown under an
        # "AI-generated" label, so this reads as failure.
        if candidate.get("finishReason") == "MAX_TOKENS":
            logger.warning("Gemini response hit the token ceiling; using fallback")
            return None
        return text or None

    def complete(self, system: str, user: str) -> str | None:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = client.post(
                self._url, headers=self._headers(), json=self._payload(system, user)
            )
            if _is_auth_error(response.status_code):
                raise AuthFailure(f"Gemini rejected the key ({response.status_code})")
            response.raise_for_status()
            return self._extract(response.json())

    async def complete_async(self, system: str, user: str) -> str | None:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                self._url, headers=self._headers(), json=self._payload(system, user)
            )
            if _is_auth_error(response.status_code):
                raise AuthFailure(f"Gemini rejected the key ({response.status_code})")
            response.raise_for_status()
            return self._extract(response.json())


class GroqClient:
    """Groq via its OpenAI-compatible chat completions endpoint."""

    name = "groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self.model = model

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, system: str, user: str) -> dict:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.3,
        }

    @staticmethod
    def _extract(data: dict) -> str | None:
        choices = data.get("choices") or []
        if not choices:
            return None

        choice = choices[0]
        if choice.get("finish_reason") == "length":
            logger.warning("Groq response hit the token ceiling; using fallback")
            return None
        return (choice.get("message", {}).get("content") or "").strip() or None

    def complete(self, system: str, user: str) -> str | None:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = client.post(
                self.URL, headers=self._headers(), json=self._payload(system, user)
            )
            if _is_auth_error(response.status_code):
                raise AuthFailure(f"Groq rejected the key ({response.status_code})")
            response.raise_for_status()
            return self._extract(response.json())

    async def complete_async(self, system: str, user: str) -> str | None:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                self.URL, headers=self._headers(), json=self._payload(system, user)
            )
            if _is_auth_error(response.status_code):
                raise AuthFailure(f"Groq rejected the key ({response.status_code})")
            response.raise_for_status()
            return self._extract(response.json())


class AnthropicClient:
    """Anthropic via the official SDK, which is already a dependency."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic

        self.model = model
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=1
        )
        self._async_client = anthropic.AsyncAnthropic(
            api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=1
        )

    @staticmethod
    def _extract(response) -> str | None:
        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        return text or None

    def _wrap(self, exc: Exception) -> Exception:
        name = type(exc).__name__.lower()
        if "authentication" in name or "permission" in name:
            return AuthFailure(str(exc))
        return exc

    def complete(self, system: str, user: str) -> str | None:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            raise self._wrap(exc) from exc
        return self._extract(response)

    async def complete_async(self, system: str, user: str) -> str | None:
        try:
            response = await self._async_client.messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            raise self._wrap(exc) from exc
        return self._extract(response)


def build_client(provider: str, api_key: str, model: str) -> LLMClient | None:
    """Construct the client for ``provider``, or None if it cannot be built.

    Never raises: a missing package, an unknown provider name or a malformed
    key all degrade to template explanations rather than taking the app down.
    """
    if not api_key:
        return None

    try:
        if provider == "gemini":
            return GeminiClient(api_key, model)
        if provider == "groq":
            return GroqClient(api_key, model)
        if provider == "anthropic":
            return AnthropicClient(api_key, model)
    except ImportError:
        logger.warning(
            "The %s client's package is not installed; using fallback "
            "explanations.",
            provider,
        )
        return None
    except Exception:
        logger.exception(
            "Could not initialise the %s client; using fallback explanations.",
            provider,
        )
        return None

    logger.warning(
        "Unknown LLM_PROVIDER %r; expected gemini, groq or anthropic. Using "
        "fallback explanations.",
        provider,
    )
    return None
