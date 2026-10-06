"""Setting defaults, prompt building and environment API keys for OrcaVision."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

from .keystore import AUTOMATIC
from .providers import get_provider, provider_names
from .sounds import DEFAULT_THEME

DEFAULT_PROMPT = (
    "Describe this image clearly and concisely for a blind user. "
    "Focus on what is visually present, including text, objects, people, colors, and context. "
    "Mention any visible structure such as headings, menus, buttons, or layouts if it looks "
    "like an interface. "
    "Use natural, descriptive language that sounds good when spoken by a screen reader. "
    "Do not add personal opinions or assumptions."
)

DEFAULTS: dict[str, Any] = {
    "provider": "openai",
    **{f"{p}-model": get_provider(p).default_model for p in provider_names()},
    "ollama-host": "http://127.0.0.1:11434",
    "openai-compatible-base-url": "",
    "key-storage": AUTOMATIC,
    "prompt": DEFAULT_PROMPT,
    "language": "en",
    "max-output-tokens": 300,
    "temperature": 0.5,
    "max-image-size": 2048,
    "request-timeout": 120,
    "sound-theme": DEFAULT_THEME,
    "copy-to-clipboard": True,
}


def build_prompt(prompt: str, language: str, hint: str = "") -> str:
    """Returns prompt plus hint, asking for a reply in language unless it is English."""

    prompt = prompt.strip() or DEFAULT_PROMPT
    if hint:
        prompt = f"{prompt}\n\n{hint}"
    language = (language or "").strip()
    if language and language.lower() not in ("en", "english"):
        return f"{prompt}\n\nRespond in {language}."
    return prompt


def environment_api_key(env_vars: Iterable[str], environ: Mapping[str, str] | None = None) -> str:
    """Returns the key in the first of env_vars that is set, or ""."""

    environ = os.environ if environ is None else environ
    for var in env_vars:
        value = (environ.get(var) or "").strip()
        if value:
            return value
    return ""
