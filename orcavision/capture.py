"""Screen and clipboard image capture for OrcaVision (X11).

Capturing touches GDK, which is not thread-safe, so everything here must run
on Orca's main thread, except encode_png(), which only touches the pixbuf it
is given and may run in a worker thread.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Atspi", "2.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Atspi, Gdk, GdkPixbuf  # noqa: E402

_CAPTURE_FAILED = "Screen capture failed. OrcaVision needs an X11 session."


class CaptureError(Exception):
    """A capture failure whose message is suitable for presenting to the user."""


@dataclass(frozen=True)
class Rect:
    """A rectangle in screen coordinates."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Captured:
    """A captured image, plus prompt text to add when it shows more than was asked for."""

    pixbuf: GdkPixbuf.Pixbuf
    hint: str = ""


# A capture that finishes in a worker thread and returns the image.
Shot = Callable[[], Captured]


def still_shot(pixbuf: GdkPixbuf.Pixbuf, hint: str = "") -> Shot:
    """Returns a shot for an image that has already been captured."""

    return lambda: Captured(pixbuf, hint)


def load_pixbuf(path: str) -> GdkPixbuf.Pixbuf:
    """Loads an image file. Safe in a worker thread."""

    try:
        return GdkPixbuf.Pixbuf.new_from_file(path)
    except Exception as error:  # pylint: disable=broad-exception-caught
        raise CaptureError("Could not read the screenshot.") from error


def pixbuf_from_bytes(data: bytes) -> GdkPixbuf.Pixbuf:
    """Decodes image data. Safe in a worker thread."""

    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
    except Exception as error:  # pylint: disable=broad-exception-caught
        raise CaptureError("Could not read the image.") from error
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise CaptureError("Could not read the image.")
    return pixbuf


def clamp_rect(rect: Rect, screen_width: int, screen_height: int) -> Rect | None:
    """Returns the on-screen part of rect, or None if none of it is on screen."""

    left = max(rect.x, 0)
    top = max(rect.y, 0)
    right = min(rect.x + rect.width, screen_width)
    bottom = min(rect.y + rect.height, screen_height)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right - left, bottom - top)


def scaled_size(width: int, height: int, max_size: int) -> tuple[int, int]:
    """Returns width and height scaled down so neither exceeds max_size (0 = no limit)."""

    longest = max(width, height)
    if max_size <= 0 or longest <= max_size:
        return width, height
    factor = max_size / longest
    return max(1, round(width * factor)), max(1, round(height * factor))


def _root_window() -> Gdk.Window:
    root = Gdk.get_default_root_window()
    if root is None:
        raise CaptureError(_CAPTURE_FAILED)
    return root


def _grab(root: Gdk.Window, rect: Rect) -> GdkPixbuf.Pixbuf:
    pixbuf = Gdk.pixbuf_get_from_window(root, rect.x, rect.y, rect.width, rect.height)
    if pixbuf is None:
        raise CaptureError(_CAPTURE_FAILED)
    return pixbuf


def capture_desktop() -> GdkPixbuf.Pixbuf:
    """Returns a screenshot of the whole desktop."""

    root = _root_window()
    return _grab(root, Rect(0, 0, root.get_width(), root.get_height()))


def capture_region(rect: Rect) -> GdkPixbuf.Pixbuf:
    """Returns a screenshot of the on-screen part of rect."""

    root = _root_window()
    visible = clamp_rect(rect, root.get_width(), root.get_height())
    if visible is None:
        raise CaptureError("That area is not visible on screen.")
    return _grab(root, visible)


def accessible_rect(obj: Atspi.Accessible | None) -> Rect | None:
    """Returns the screen extents of an accessible object, or None if unknown."""

    if obj is None:
        return None
    try:
        extents = Atspi.Component.get_extents(obj, Atspi.CoordType.SCREEN)
    except Exception:  # pylint: disable=broad-exception-caught
        # GLib.Error when obj is not a component or has gone away.
        return None
    if extents is None or extents.width <= 0 or extents.height <= 0:
        return None
    return Rect(extents.x, extents.y, extents.width, extents.height)


def _x11_active_window_rect() -> Rect | None:
    """Returns the active window's client area as the X11 window manager reports it."""

    if "X11" not in type(Gdk.Display.get_default()).__name__:
        return None
    try:
        gi.require_version("Wnck", "3.0")
        from gi.repository import Wnck  # pylint: disable=import-outside-toplevel
    except (ImportError, ValueError):
        return None

    screen = Wnck.Screen.get_default()
    if screen is None:
        return None
    screen.force_update()
    window = screen.get_active_window()
    if window is None:
        return None
    x, y, width, height = window.get_client_window_geometry()
    if width <= 0 or height <= 0:
        return None
    return Rect(x, y, width, height)


def active_window_rect(atspi_window: Atspi.Accessible | None) -> Rect | None:
    """Returns the active window's rectangle: from X11 first, else from AT-SPI."""

    return _x11_active_window_rect() or accessible_rect(atspi_window)


def request_clipboard_image(callback: Callable[[GdkPixbuf.Pixbuf | None], Any]) -> None:
    """Asynchronously reads the clipboard image and calls callback with it (or None)."""

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # pylint: disable=import-outside-toplevel

    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.request_image(lambda _clipboard, pixbuf: callback(pixbuf))


def encode_png(pixbuf: GdkPixbuf.Pixbuf, max_size: int) -> bytes:
    """Returns pixbuf as PNG bytes, scaled down to fit max_size (0 = original size)."""

    width, height = pixbuf.get_width(), pixbuf.get_height()
    new_width, new_height = scaled_size(width, height, max_size)
    if (new_width, new_height) != (width, height):
        pixbuf = pixbuf.scale_simple(new_width, new_height, GdkPixbuf.InterpType.BILINEAR)
    ok, data = pixbuf.save_to_bufferv("png", [], [])
    if not ok:
        raise CaptureError("Could not encode the image.")
    return bytes(data)
