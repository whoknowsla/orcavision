"""Chooses how to capture what the user asked for, on X11 or Wayland.

These functions run on Orca's main thread. On X11 they grab the screen at
once, which is fast; on Wayland they return a shot that runs the capture
tools later in the worker thread. When the requested window or object cannot
be captured on its own, the shot captures the whole desktop instead and adds
a hint to the prompt naming what to describe.
"""

from __future__ import annotations

import shutil

from gi.repository import Atspi

from .capture import (
    Captured,
    CaptureError,
    Shot,
    accessible_rect,
    active_window_rect,
    capture_desktop,
    capture_region,
    still_shot,
)
from .wayland import backends, clipboard_image, is_wayland
from .wayland import capture_desktop as wayland_desktop
from .wayland import capture_window as wayland_window


def _name(obj: Atspi.Accessible | None) -> str:
    try:
        return (Atspi.Accessible.get_name(obj) or "").strip() if obj is not None else ""
    except Exception:  # pylint: disable=broad-exception-caught
        return ""


def _role(obj: Atspi.Accessible) -> str:
    try:
        return (Atspi.Accessible.get_role_name(obj) or "").strip() or "object"
    except Exception:  # pylint: disable=broad-exception-caught
        return "object"


def window_hint(title: str) -> str:
    """Returns the prompt hint for a desktop screenshot that stands in for a window."""

    if title:
        return (
            "The screenshot shows the whole screen. "
            f'Describe only the window titled "{title}".'
        )
    return "The screenshot shows the whole screen. Describe only the window in front."


def object_hint(role: str, name: str) -> str:
    """Returns the prompt hint for a screenshot that contains the focused object."""

    target = f'the {role} named "{name}"' if name else f"the focused {role}"
    return f"Describe only {target}, not the rest of the screenshot."


def desktop_shot() -> Shot:
    """Returns a shot of the whole desktop."""

    if is_wayland():
        chain = backends()
        return lambda: Captured(wayland_desktop(chain))
    return still_shot(capture_desktop())


def window_shot(window: Atspi.Accessible | None) -> Shot:
    """Returns a shot of the active window (window is Orca's active window)."""

    fallback_hint = window_hint(_name(window))
    if is_wayland():
        chain = backends()

        def shot() -> Captured:
            pixbuf = wayland_window(chain)
            if pixbuf is not None:
                return Captured(pixbuf)
            return Captured(wayland_desktop(chain), fallback_hint)

        return shot

    rect = active_window_rect(window)
    if rect is not None:
        return still_shot(capture_region(rect))
    if window is None:
        raise CaptureError("No active window found.")
    return still_shot(capture_desktop(), fallback_hint)


def object_shot(window: Atspi.Accessible | None, obj: Atspi.Accessible | None) -> Shot:
    """Returns a shot of the focused object."""

    if obj is None:
        raise CaptureError("There is no focused object.")
    if not is_wayland():
        rect = accessible_rect(obj)
        if rect is None:
            raise CaptureError("The focused object has no size on screen.")
        return still_shot(capture_region(rect))

    # Wayland apps do not know where their windows are, so object positions
    # cannot be trusted. Capture the window and name the object instead.
    hint = object_hint(_role(obj), _name(obj))
    window_part = window_shot(window)
    return lambda: Captured(window_part().pixbuf, hint)


def clipboard_shot() -> Shot | None:
    """Returns a shot of the clipboard image when wl-paste must read it, else None.

    On Wayland only the focused app may read the clipboard through GTK, so
    wl-paste is used when it is installed. Otherwise the caller reads the
    clipboard through GTK.
    """

    if is_wayland() and shutil.which("wl-paste"):
        return lambda: Captured(clipboard_image())
    return None

