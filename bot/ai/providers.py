"""AI providers. Each one turns (system prompt, user prompt) into a text reply.

Anthropic uses the official `anthropic` SDK. Every other provider speaks the
OpenAI chat-completions protocol and is reached through the `openai` SDK.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..config import Settings

log = logging.getLogger(__name__)


class AIError(Exception):
    """An AI request failed. The message is safe to show to an admin."""


class AIProvider(Protocol):
    name: str
    model: str

    async def complete(self, system: str, user: str) -> str: ...


class AnthropicProvider:
    name = "anthropic"

    # Models that can decline a request with stop_reason "refusal" and support
    # the server-side fallback, which re-runs a declined request on another model.
    _FALLBACK_MODELS = {
        "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
        "claude-fable-5-2", "claude-fable-5-1",
    }
    _FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, api_key: str, model: str, timeout: float, base_url: str | None = None):
        import anthropic

        self._sdk = anthropic
        self.model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, base_url=base_url)

    async def complete(self, system: str, user: str) -> str:
        sdk = self._sdk
        request = dict(
            model=self.model,
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        try:
            # Streamed so that long generations do not hit HTTP timeouts.
            if self.model in self._FALLBACK_MODELS:
                stream_cm = self._client.beta.messages.stream(
                    **request, betas=[self._FALLBACK_BETA], fallbacks="default"
                )
            else:
                stream_cm = self._client.messages.stream(**request)
            async with stream_cm as stream:
                message = await stream.get_final_message()
        except sdk.AuthenticationError as exc:
            raise AIError("The AI provider rejected the API key. Check AI_API_KEY in .env.") from exc
        except sdk.PermissionDeniedError as exc:
            raise AIError(f"The API key is not allowed to use model {self.model}.") from exc
        except sdk.NotFoundError as exc:
            raise AIError(f"Model {self.model!r} was not found. Check AI_MODEL in .env.") from exc
        except sdk.RateLimitError as exc:
            raise AIError("The AI provider is rate limiting requests. Try again in a minute.") from exc
        except sdk.APIStatusError as exc:
            raise AIError(f"The AI provider returned an error ({exc.status_code}): {exc.message}") from exc
        except sdk.APIConnectionError as exc:
            raise AIError("Could not reach the AI provider. Check the network connection.") from exc

        if message.stop_reason == "refusal":
            raise AIError("The AI model declined to process this content.")
        if message.stop_reason == "max_tokens":
            raise AIError("The AI response was cut off because it was too long. Ask for fewer questions.")
        return "".join(block.text for block in message.content if block.type == "text")


class OpenAICompatibleProvider:
    def __init__(self, name: str, api_key: str, model: str, timeout: float, base_url: str | None):
        import openai

        self._sdk = openai
        self.name = name
        self.model = model
        # Local servers such as Ollama ignore the key, but the SDK wants a value.
        self._client = openai.AsyncOpenAI(api_key=api_key or "not-needed", base_url=base_url, timeout=timeout)
        self._json_mode = True

    async def complete(self, system: str, user: str) -> str:
        sdk = self._sdk
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            try:
                response = await self._create(messages, json_mode=self._json_mode)
            except sdk.BadRequestError:
                if not self._json_mode:
                    raise
                # Some OpenAI-compatible servers do not accept response_format.
                log.info("%s rejected JSON mode; retrying without it", self.name)
                response = await self._create(messages, json_mode=False)
                self._json_mode = False
        except sdk.AuthenticationError as exc:
            raise AIError("The AI provider rejected the API key. Check AI_API_KEY in .env.") from exc
        except sdk.PermissionDeniedError as exc:
            raise AIError(f"The API key is not allowed to use model {self.model}.") from exc
        except sdk.NotFoundError as exc:
            raise AIError(f"Model {self.model!r} was not found. Check AI_MODEL / AI_BASE_URL in .env.") from exc
        except sdk.RateLimitError as exc:
            raise AIError("The AI provider is rate limiting requests or the quota is used up.") from exc
        except sdk.APIStatusError as exc:
            raise AIError(f"The AI provider returned an error ({exc.status_code}): {exc.message}") from exc
        except sdk.APIConnectionError as exc:
            raise AIError("Could not reach the AI provider. Check AI_BASE_URL and the network.") from exc

        if not response.choices:
            raise AIError("The AI provider returned an empty response.")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise AIError("The AI response was cut off because it was too long. Ask for fewer questions.")
        if choice.finish_reason == "content_filter":
            raise AIError("The AI provider's content filter blocked this content.")
        return choice.message.content or ""

    async def _create(self, messages: list[dict], json_mode: bool):
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        return await self._client.chat.completions.create(model=self.model, messages=messages, **extra)


def build_provider(settings: Settings) -> AIProvider:
    if settings.ai_provider == "anthropic":
        return AnthropicProvider(
            settings.ai_api_key, settings.ai_model, settings.ai_timeout_seconds, settings.ai_base_url
        )
    return OpenAICompatibleProvider(
        settings.ai_provider, settings.ai_api_key, settings.ai_model,
        settings.ai_timeout_seconds, settings.ai_base_url,
    )
