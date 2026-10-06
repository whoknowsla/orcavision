"""Vision-model providers for OrcaVision.

Only the Python standard library is used, so the extension runs inside Orca's
own interpreter without third-party packages.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_ERROR_CHARS = 300


class DescribeError(Exception):
    """An error whose message is suitable for presenting to the user."""


@dataclass(frozen=True)
class DescribeRequest:
    """Everything a provider needs to describe one image."""

    model: str
    prompt: str
    image_png: bytes
    max_output_tokens: int = 300
    temperature: float = 0.5
    timeout: float = 120.0
    api_key: str = ""
    host: str = ""


@dataclass(frozen=True)
class HttpResponse:
    """An HTTP response; body is the parsed JSON, or None if it was not JSON."""

    status: int
    body: Any
    text: str


Transport = Callable[[str, dict, dict, float], HttpResponse]


def post_json(url: str, payload: dict, headers: dict, timeout: float) -> HttpResponse:
    """POSTs JSON and returns the response, including HTTP error responses.

    Raises TimeoutError or another OSError when the server cannot be reached.
    """

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read(MAX_RESPONSE_BYTES + 1)
        error.close()
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise TimeoutError from error
        raise ConnectionError(str(error.reason)) from error

    if len(raw) > MAX_RESPONSE_BYTES:
        raise DescribeError("The response was too large.")
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    return HttpResponse(status, body, text)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _truncate(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > _MAX_ERROR_CHARS:
        return text[:_MAX_ERROR_CHARS] + "..."
    return text


def _error_message(response: HttpResponse) -> str:
    """Returns the error message from a JSON error body, or the raw body text."""

    message = ""
    if isinstance(response.body, dict):
        error = response.body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        elif isinstance(error, str):
            message = error
    return _truncate(message or response.text)


def _drop_temperature(payload: dict) -> bool:
    return payload.pop("temperature", None) is not None


def _use_max_completion_tokens(payload: dict) -> bool:
    if "max_tokens" not in payload:
        return False
    payload["max_completion_tokens"] = payload.pop("max_tokens")
    return True


# Fixes for request parameters some models reject: (word in the 400 error, fix).
# Reasoning models reject sampling parameters, and some OpenAI-style servers
# want max_completion_tokens instead of max_tokens.
_DROP_TEMPERATURE = ("temperature", _drop_temperature)
_MAX_COMPLETION_TOKENS = ("max_completion_tokens", _use_max_completion_tokens)


class Provider:
    """Base class for a vision-model provider."""

    name = ""
    label = ""
    # True if the provider takes an API key (an "<name>-api-key" setting).
    uses_api_key = False
    env_vars: tuple[str, ...] = ()
    default_model = ""
    # The setting holding the server URL, for providers that need one.
    host_setting = ""

    def describe(self, request: DescribeRequest, transport: Transport = post_json) -> str:
        """Returns a description of request.image_png. Raises DescribeError."""

        raise NotImplementedError

    def _send_adapting(  # pylint: disable=too-many-arguments
        self,
        transport: Transport,
        url: str,
        payload: dict,
        headers: dict,
        timeout: float,
        adaptations: tuple[tuple[str, Callable[[dict], bool]], ...],
        unreachable: str = "",
    ) -> HttpResponse:
        """Sends payload, retrying when a 400 error names a parameter we can adapt."""

        response = self._send(transport, url, payload, headers, timeout, unreachable)
        for trigger, adapt in adaptations:
            if (
                response.status == 400
                and trigger in _error_message(response).lower()
                and adapt(payload)
            ):
                response = self._send(transport, url, payload, headers, timeout, unreachable)
        return response

    def _send(
        self,
        transport: Transport,
        url: str,
        payload: dict,
        headers: dict,
        timeout: float,
        unreachable: str = "",
    ) -> HttpResponse:
        try:
            return transport(url, payload, headers, timeout)
        except TimeoutError as error:
            raise DescribeError(f"{self.label} did not answer in time.") from error
        except OSError as error:
            message = unreachable or f"Could not connect to {self.label}: {error}"
            raise DescribeError(message) from error

    def _require_key_and_model(self, request: DescribeRequest) -> None:
        if not request.api_key:
            hint = f", or set {self.env_vars[0]}" if self.env_vars else ""
            raise DescribeError(
                f"{self.label} needs an API key. Press Orca+Ctrl+Alt+K to store one in "
                f"your keyring{hint}."
            )
        if not request.model:
            raise DescribeError(f"Choose a {self.label} model in OrcaVision preferences.")


class OpenAIProvider(Provider):
    """OpenAI Responses API."""

    name = "openai"
    label = "OpenAI"
    uses_api_key = True
    env_vars = ("OPENAI_API_KEY",)
    default_model = "gpt-4o"
    url = "https://api.openai.com/v1/responses"

    def describe(self, request: DescribeRequest, transport: Transport = post_json) -> str:
        self._require_key_and_model(request)
        payload: dict[str, Any] = {
            "model": request.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": request.prompt},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64," + _b64(request.image_png),
                        },
                    ],
                }
            ],
            "max_output_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        headers = {"Authorization": f"Bearer {request.api_key}"}
        response = self._send_adapting(
            transport, self.url, payload, headers, request.timeout, (_DROP_TEMPERATURE,)
        )
        if response.status != 200:
            raise DescribeError(self._status_error(response))
        return self._parse(response.body)

    def _status_error(self, response: HttpResponse) -> str:
        if response.status == 401:
            return "OpenAI rejected the API key."
        if response.status == 429:
            return f"OpenAI rate limit or quota exceeded. {_error_message(response)}"
        return f"OpenAI error {response.status}: {_error_message(response)}"

    @staticmethod
    def _parse(body: Any) -> str:
        if not isinstance(body, dict):
            raise DescribeError("OpenAI returned an invalid response.")

        texts: list[str] = []
        refusals: list[str] = []
        for item in body.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "output_text" and content.get("text"):
                    texts.append(str(content["text"]))
                elif content.get("type") == "refusal" and content.get("refusal"):
                    refusals.append(str(content["refusal"]))

        text = "\n".join(texts).strip()
        if text:
            return text
        if refusals:
            raise DescribeError(f"OpenAI refused: {_truncate(' '.join(refusals))}")
        if body.get("status") == "incomplete":
            reason = (body.get("incomplete_details") or {}).get("reason") or "unknown reason"
            if reason == "max_output_tokens":
                raise DescribeError(
                    "OpenAI ran out of output tokens. Increase the maximum output tokens."
                )
            raise DescribeError(f"OpenAI stopped early: {reason}.")
        raise DescribeError("OpenAI returned no description.")


class AnthropicProvider(Provider):
    """Anthropic Messages API (Claude).

    Raw HTTP rather than the anthropic SDK, because the extension runs in
    Orca's interpreter, where only the standard library can be relied on.
    """

    name = "anthropic"
    label = "Claude"
    uses_api_key = True
    env_vars = ("ANTHROPIC_API_KEY",)
    default_model = "claude-opus-5-5"
    url = "https://api.anthropic.com/v1/messages"

    # Current Claude models always think, and thinking counts toward
    # max_tokens, so allow at least this many tokens. The prompt, not this
    # limit, keeps descriptions short.
    min_max_tokens = 16000
    # Model families that reject sampling parameters with a 400.
    no_sampling_prefixes = (
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-opus-4-7",
        "claude-opus-4-8",
    )
    # Models that accept server-side refusal fallbacks ("fallbacks": "default"),
    # which re-run a declined request on a suitable model within the same call.
    fallback_models = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")

    def describe(self, request: DescribeRequest, transport: Transport = post_json) -> str:
        self._require_key_and_model(request)
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": max(request.max_output_tokens, self.min_max_tokens),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": _b64(request.image_png),
                            },
                        },
                        {"type": "text", "text": request.prompt},
                    ],
                }
            ],
        }
        if not request.model.startswith(self.no_sampling_prefixes):
            payload["temperature"] = min(request.temperature, 1.0)
        headers = {"x-api-key": request.api_key, "anthropic-version": "2023-06-01"}
        if request.model in self.fallback_models:
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
            payload["fallbacks"] = "default"

        response = self._send_adapting(
            transport, self.url, payload, headers, request.timeout, (_DROP_TEMPERATURE,)
        )
        if response.status != 200:
            raise DescribeError(self._status_error(response, request.model))
        return self._parse(response.body)

    @staticmethod
    def _status_error(response: HttpResponse, model: str) -> str:
        message = _error_message(response)
        if response.status == 401:
            return "Claude rejected the API key."
        if response.status == 404:
            return f"Claude model '{model}' was not found."
        if response.status == 429:
            return f"Claude rate limit exceeded. {message}"
        if response.status == 529:
            return "Claude is overloaded. Try again shortly."
        return f"Claude error {response.status}: {message}"

    @staticmethod
    def _parse(body: Any) -> str:
        if not isinstance(body, dict):
            raise DescribeError("Claude returned an invalid response.")

        stop_reason = body.get("stop_reason")
        if stop_reason == "refusal":
            category = (body.get("stop_details") or {}).get("category")
            detail = f" ({category})" if category else ""
            raise DescribeError(f"Claude declined to describe this image{detail}.")

        text = "".join(
            str(block["text"])
            for block in body.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ).strip()
        if text:
            return text
        if stop_reason == "max_tokens":
            raise DescribeError(
                "Claude ran out of output tokens. Increase the maximum output tokens."
            )
        raise DescribeError("Claude returned no description.")


class GeminiProvider(Provider):
    """Google Gemini generateContent API."""

    name = "gemini"
    label = "Gemini"
    uses_api_key = True
    env_vars = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    default_model = "gemini-2.5-flash"
    base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    def describe(self, request: DescribeRequest, transport: Transport = post_json) -> str:
        self._require_key_and_model(request)
        model = request.model.removeprefix("models/")
        url = f"{self.base_url}/{urllib.parse.quote(model, safe='-._~')}:generateContent"
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": request.prompt},
                        {"inline_data": {"mime_type": "image/png", "data": _b64(request.image_png)}},
                    ],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": request.max_output_tokens,
                "temperature": request.temperature,
            },
        }
        headers = {"x-goog-api-key": request.api_key}
        response = self._send(transport, url, payload, headers, request.timeout)
        if response.status != 200:
            raise DescribeError(self._status_error(response, model))
        return self._parse(response.body)

    @staticmethod
    def _status_error(response: HttpResponse, model: str) -> str:
        message = _error_message(response)
        if response.status in (401, 403) or "API_KEY_INVALID" in response.text:
            return "Gemini rejected the API key."
        if response.status == 404:
            return f"Gemini model '{model}' was not found."
        if response.status == 429:
            return f"Gemini rate limit or quota exceeded. {message}"
        return f"Gemini error {response.status}: {message}"

    @staticmethod
    def _parse(body: Any) -> str:
        if not isinstance(body, dict):
            raise DescribeError("Gemini returned an invalid response.")

        candidates = body.get("candidates") or []
        candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(
            str(part["text"])
            for part in parts
            if isinstance(part, dict) and part.get("text") and not part.get("thought")
        ).strip()
        if text:
            return text

        block_reason = (body.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            raise DescribeError(f"Gemini blocked the request: {block_reason}.")
        finish_reason = candidate.get("finishReason")
        if finish_reason == "MAX_TOKENS":
            raise DescribeError(
                "Gemini ran out of output tokens. Increase the maximum output tokens."
            )
        if finish_reason and finish_reason != "STOP":
            raise DescribeError(f"Gemini stopped early: {finish_reason}.")
        raise DescribeError("Gemini returned no description.")


class OllamaProvider(Provider):
    """Local Ollama server."""

    name = "ollama"
    label = "Ollama"
    default_model = ""
    host_setting = "ollama-host"

    def describe(self, request: DescribeRequest, transport: Transport = post_json) -> str:
        host = request.host.strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            raise DescribeError("The Ollama host must start with http:// or https://.")
        if not request.model:
            raise DescribeError(
                "Choose an Ollama model in OrcaVision preferences, for example llava."
            )

        payload = {
            "model": request.model,
            "prompt": request.prompt,
            "images": [_b64(request.image_png)],
            "stream": False,
            "options": {
                "num_predict": request.max_output_tokens,
                "temperature": request.temperature,
            },
        }
        response = self._send(
            transport,
            f"{host}/api/generate",
            payload,
            {},
            request.timeout,
            unreachable=f"Could not connect to Ollama at {host}. Is ollama serve running?",
        )
        if response.status != 200:
            message = _error_message(response)
            if response.status == 404 and "not found" in message.lower():
                raise DescribeError(
                    f"Ollama model '{request.model}' is not installed. "
                    f"Run: ollama pull {request.model}"
                )
            raise DescribeError(f"Ollama error {response.status}: {message}")

        body = response.body
        if not isinstance(body, dict):
            raise DescribeError("Ollama returned an invalid response.")
        if body.get("error"):
            raise DescribeError(f"Ollama error: {_truncate(str(body['error']))}")
        text = body.get("response")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            raise DescribeError("Ollama returned no description.")
        return text


class OpenAICompatibleProvider(Provider):
    """Any server with an OpenAI-style chat completions endpoint.

    Covers OpenRouter, Groq, Mistral, Together, LM Studio, llama.cpp, vLLM,
    LocalAI and similar. The API key is optional for local servers.
    """

    name = "openai-compatible"
    label = "OpenAI-compatible server"
    uses_api_key = True
    env_vars = ("OPENAI_COMPATIBLE_API_KEY",)
    default_model = ""
    host_setting = "openai-compatible-base-url"

    def describe(self, request: DescribeRequest, transport: Transport = post_json) -> str:
        base_url = request.host.strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise DescribeError(
                "Set the base URL of the OpenAI-compatible server in OrcaVision preferences, "
                "for example https://openrouter.ai/api/v1."
            )
        if not request.model:
            raise DescribeError(
                "Choose a model for the OpenAI-compatible server in OrcaVision preferences."
            )

        payload = {
            "model": request.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": request.prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + _b64(request.image_png)
                            },
                        },
                    ],
                }
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        headers = {"Authorization": f"Bearer {request.api_key}"} if request.api_key else {}
        response = self._send_adapting(
            transport,
            f"{base_url}/chat/completions",
            payload,
            headers,
            request.timeout,
            (_DROP_TEMPERATURE, _MAX_COMPLETION_TOKENS),
            unreachable=f"Could not connect to {base_url}.",
        )
        if response.status in (401, 403):
            raise DescribeError("The server rejected the API key.")
        if response.status != 200:
            raise DescribeError(f"Server error {response.status}: {_error_message(response)}")
        return self._parse(response.body)

    @staticmethod
    def _parse(body: Any) -> str:
        if not isinstance(body, dict):
            raise DescribeError("The server returned an invalid response.")

        choices = body.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        text = content.strip() if isinstance(content, str) else ""
        if text:
            return text
        if message.get("refusal"):
            raise DescribeError(f"The model refused: {_truncate(str(message['refusal']))}")
        if choice.get("finish_reason") == "length":
            raise DescribeError(
                "The model ran out of output tokens. Increase the maximum output tokens."
            )
        raise DescribeError("The server returned no description.")


_PROVIDERS: dict[str, Provider] = {
    provider.name: provider
    for provider in (
        OpenAIProvider(),
        AnthropicProvider(),
        GeminiProvider(),
        OllamaProvider(),
        OpenAICompatibleProvider(),
    )
}


def provider_names() -> list[str]:
    """Returns the names of all providers."""

    return list(_PROVIDERS)


def provider_choices() -> tuple[tuple[str, str], ...]:
    """Returns (name, label) pairs for a preferences combo box."""

    return tuple((provider.name, provider.label) for provider in _PROVIDERS.values())


def key_provider_choices() -> tuple[tuple[str, str], ...]:
    """Returns (name, label) pairs for the providers that take an API key."""

    return tuple(
        (provider.name, provider.label) for provider in _PROVIDERS.values() if provider.uses_api_key
    )


def get_provider(name: str) -> Provider:
    """Returns the provider called name. Raises DescribeError if there is none."""

    try:
        return _PROVIDERS[name]
    except KeyError:
        raise DescribeError(f"Unknown provider '{name}'.") from None
