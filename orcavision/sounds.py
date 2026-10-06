"""Notification tones for OrcaVision, played through Orca's tone generator.

Each theme has a tone for when a request starts, a short beep repeated while
waiting, a tone when the description arrives, and a tone for errors.
"""

from __future__ import annotations

from dataclasses import dataclass

WAITING_INTERVAL_MS = 2000


@dataclass(frozen=True)
class Tone:
    """A generated tone: duration in seconds, frequency in Hz, volume 0.0-1.0."""

    duration: float
    frequency: int
    volume: float


THEMES: dict[str, dict[str, Tone]] = {
    "modern": {
        "open": Tone(0.15, 1047, 0.5),
        "waiting": Tone(0.1, 880, 0.25),
        "done": Tone(0.18, 392, 0.5),
        "error": Tone(0.3, 220, 0.5),
    },
    "minimal": {
        "open": Tone(0.08, 880, 0.3),
        "waiting": Tone(0.06, 880, 0.15),
        "done": Tone(0.1, 349, 0.3),
        "error": Tone(0.2, 220, 0.3),
    },
    "none": {},
}

THEME_CHOICES = (
    ("modern", "Modern"),
    ("minimal", "Minimal"),
    ("none", "None (speak status messages instead)"),
)

DEFAULT_THEME = "modern"


def tone_for(theme: str, event: str) -> Tone | None:
    """Returns the tone for event ("open", "waiting", "done", "error"), if any."""

    return THEMES.get(theme, THEMES[DEFAULT_THEME]).get(event)


def is_silent(theme: str) -> bool:
    """Returns True if theme plays no tones."""

    return not THEMES.get(theme, THEMES[DEFAULT_THEME])
