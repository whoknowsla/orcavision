"""Screen and clipboard capture on Wayland.

Wayland lets no client read the screen directly, so OrcaVision asks the
compositor's own tools first (grim on Sway, Hyprland and other wlroots
compositors, spectacle on KDE, gnome-screenshot on GNOME) and falls back to a
non-interactive xdg-desktop-portal screenshot, which most desktops provide.

Everything here blocks on subprocesses or D-Bus, so it must run in a worker
thread, never on Orca's main thread.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from gi.repository import Gio, GLib

from .capture import CaptureError, Rect, load_pixbuf, pixbuf_from_bytes

if TYPE_CHECKING:
    from gi.repository import GdkPixbuf

_TOOL_TIMEOUT_SECONDS = 20
_PORTAL_TIMEOUT_SECONDS = 60

_PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"

# Runs a command and returns its standard output.
Runner = Callable[[list[str]], bytes]


def is_wayland(environ: Mapping[str, str] | None = None) -> bool:
    """Returns True if the desktop session is a Wayland session."""

    environ = os.environ if environ is None else environ
    session_type = environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type == "x11":
        return False
    return session_type == "wayland" or bool(environ.get("WAYLAND_DISPLAY"))


def run_tool(command: list[str]) -> bytes:
    """Runs command and returns its standard output. Raises CaptureError on failure."""

    try:
        result = subprocess.run(
            command, capture_output=True, timeout=_TOOL_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as error:
        raise CaptureError(f"{command[0]} is not installed.") from error
    except subprocess.TimeoutExpired as error:
        raise CaptureError(f"{command[0]} did not finish in time.") from error
    if result.returncode != 0:
        lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = f": {lines[-1]}" if lines else "."
        raise CaptureError(f"{command[0]} failed{detail}")
    return result.stdout


def grim_geometry(rect: Rect) -> str:
    """Returns rect in grim's -g format."""

    return f"{rect.x},{rect.y} {rect.width}x{rect.height}"


def _find_focused(node: dict[str, Any]) -> dict[str, Any] | None:
    if node.get("focused"):
        return node
    for child in [*node.get("nodes", []), *node.get("floating_nodes", [])]:
        found = _find_focused(child)
        if found is not None:
            return found
    return None


def sway_window_rect(run: Runner) -> Rect | None:
    """Returns the focused Sway window's content area, or None if no window is focused."""

    try:
        node = _find_focused(json.loads(run(["swaymsg", "-r", "-t", "get_tree"])))
        if node is None or node.get("type") not in ("con", "floating_con"):
            return None
        outer, inner = node["rect"], node.get("window_rect") or {}
        rect = Rect(
            outer["x"] + inner.get("x", 0),
            outer["y"] + inner.get("y", 0),
            inner.get("width") or outer["width"],
            inner.get("height") or outer["height"],
        )
    except (CaptureError, ValueError, KeyError, TypeError):
        return None
    return rect if rect.width > 0 and rect.height > 0 else None


def hyprland_window_rect(run: Runner) -> Rect | None:
    """Returns the active Hyprland window's area, or None if there is none."""

    try:
        window = json.loads(run(["hyprctl", "-j", "activewindow"]))
        (x, y), (width, height) = window["at"], window["size"]
    except (CaptureError, ValueError, KeyError, TypeError):
        return None
    return Rect(x, y, width, height) if width > 0 and height > 0 else None


class Backend:
    """A way to take screenshots on Wayland."""

    name = ""

    def desktop(self, path: str, run: Runner) -> None:
        """Saves a screenshot of the whole desktop to path. Raises CaptureError."""

        raise NotImplementedError

    def window(self, path: str, run: Runner) -> bool:  # pylint: disable=unused-argument
        """Saves the active window to path; returns False if this backend cannot."""

        return False


class GrimBackend(Backend):
    """grim, for compositors with the wlroots screencopy protocol."""

    name = "grim"

    def __init__(self, window_rect: Callable[[Runner], Rect | None] | None = None) -> None:
        self._window_rect = window_rect

    def desktop(self, path: str, run: Runner) -> None:
        run(["grim", path])

    def window(self, path: str, run: Runner) -> bool:
        rect = self._window_rect(run) if self._window_rect else None
        if rect is None:
            return False
        run(["grim", "-g", grim_geometry(rect), path])
        return True


class SpectacleBackend(Backend):
    """KDE's spectacle, which KWin allows to take screenshots."""

    name = "spectacle"
    _COMMAND = ("spectacle", "--background", "--nonotify")

    def desktop(self, path: str, run: Runner) -> None:
        run([*self._COMMAND, "--fullscreen", "--output", path])

    def window(self, path: str, run: Runner) -> bool:
        run([*self._COMMAND, "--activewindow", "--output", path])
        return True


class GnomeScreenshotBackend(Backend):
    """gnome-screenshot, which GNOME Shell allows to take screenshots."""

    name = "gnome-screenshot"

    def desktop(self, path: str, run: Runner) -> None:
        run(["gnome-screenshot", "--file", path])

    def window(self, path: str, run: Runner) -> bool:
        run(["gnome-screenshot", "--window", "--file", path])
        return True


def portal_screenshot(
    timeout: int = _PORTAL_TIMEOUT_SECONDS, connection: Gio.DBusConnection | None = None
) -> str:
    """Takes a non-interactive screenshot through xdg-desktop-portal.

    Returns the path of the file the portal saved. Some desktops ask the user
    for permission the first time.
    """

    try:
        bus = connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as error:
        raise CaptureError("Could not reach the session bus.") from error

    token = f"orcavision{uuid.uuid4().hex}"
    sender = bus.get_unique_name().lstrip(":").replace(".", "_")
    request_paths = {f"{_PORTAL_PATH}/request/{sender}/{token}"}
    context = GLib.MainContext.new()
    loop = GLib.MainLoop.new(context, False)
    response: dict[str, Any] = {}

    def on_response(_bus, _sender, path, _interface, _signal, parameters, *_args):
        if path in request_paths:
            response["code"], results = parameters.unpack()
            response["uri"] = results.get("uri", "")
            loop.quit()

    # Signals are delivered to the thread-default context at subscription time.
    context.push_thread_default()
    subscription = bus.signal_subscribe(
        None,
        "org.freedesktop.portal.Request",
        "Response",
        None,
        None,
        Gio.DBusSignalFlags.NONE,
        on_response,
    )
    try:
        options = {
            "handle_token": GLib.Variant("s", token),
            "interactive": GLib.Variant("b", False),
            "modal": GLib.Variant("b", False),
        }
        try:
            reply = bus.call_sync(
                _PORTAL_BUS_NAME,
                _PORTAL_PATH,
                "org.freedesktop.portal.Screenshot",
                "Screenshot",
                GLib.Variant("(sa{sv})", ("", options)),
                GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error as error:
            raise CaptureError("The screenshot portal is not available.") from error
        # Old portals ignore handle_token and return a different request path.
        request_paths.add(reply.unpack()[0])

        timer = GLib.timeout_source_new_seconds(timeout)
        timer.set_callback(lambda *_args: loop.quit() or False)
        timer.attach(context)
        loop.run()
        timer.destroy()
    finally:
        bus.signal_unsubscribe(subscription)
        context.pop_thread_default()

    code, uri = response.get("code"), response.get("uri", "")
    if code is None:
        raise CaptureError("The screenshot portal did not answer.")
    if code == 1:
        raise CaptureError("The screenshot was cancelled or not permitted.")
    if code != 0 or not uri.startswith("file://"):
        raise CaptureError("The screenshot portal failed.")
    return urllib.parse.unquote(urllib.parse.urlparse(uri).path)


class PortalBackend(Backend):
    """xdg-desktop-portal's Screenshot interface (whole desktop only)."""

    name = "xdg-desktop-portal"

    def __init__(self, screenshot: Callable[[], str] = portal_screenshot) -> None:
        self._screenshot = screenshot

    def desktop(self, path: str, run: Runner) -> None:
        # Move the portal's file so screenshots do not pile up in the user's folders.
        try:
            shutil.move(self._screenshot(), path)
        except OSError as error:
            raise CaptureError("Could not read the portal's screenshot.") from error


def backends(
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[Backend]:
    """Returns the screenshot backends to try for this desktop, best first."""

    environ = os.environ if environ is None else environ
    desktops = environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")
    found: list[Backend] = []

    if which("grim"):
        if environ.get("HYPRLAND_INSTANCE_SIGNATURE") and which("hyprctl"):
            found.append(GrimBackend(hyprland_window_rect))
        elif environ.get("SWAYSOCK") and which("swaymsg"):
            found.append(GrimBackend(sway_window_rect))
    if "KDE" in desktops and which("spectacle"):
        found.append(SpectacleBackend())
    if "GNOME" in desktops and which("gnome-screenshot"):
        found.append(GnomeScreenshotBackend())
    if which("grim") and not any(isinstance(backend, GrimBackend) for backend in found):
        found.append(GrimBackend())
    found.append(PortalBackend())
    return found


def capture_desktop(chain: list[Backend], run: Runner = run_tool) -> GdkPixbuf.Pixbuf:
    """Returns a screenshot of the desktop from the first backend that works."""

    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="orcavision-") as directory:
        path = os.path.join(directory, "screen.png")
        for backend in chain:
            try:
                backend.desktop(path, run)
                return load_pixbuf(path)
            except CaptureError as error:
                errors.append(str(error))
    last = f" ({errors[-1]})" if errors else ""
    raise CaptureError(
        f"Screen capture failed{last}. On Wayland, install grim, spectacle or "
        "gnome-screenshot, or check that xdg-desktop-portal is running."
    )


def capture_window(chain: list[Backend], run: Runner = run_tool) -> GdkPixbuf.Pixbuf | None:
    """Returns a screenshot of the active window, or None if no backend can take one."""

    with tempfile.TemporaryDirectory(prefix="orcavision-") as directory:
        path = os.path.join(directory, "window.png")
        for backend in chain:
            try:
                if backend.window(path, run):
                    return load_pixbuf(path)
            except CaptureError:
                continue
    return None


def clipboard_image(run: Runner = run_tool) -> GdkPixbuf.Pixbuf:
    """Returns the clipboard image using wl-paste."""

    try:
        types = run(["wl-paste", "--list-types"]).decode("utf-8", "replace").split()
    except CaptureError as error:
        raise CaptureError("There is no image on the clipboard.") from error
    image_type = "image/png" if "image/png" in types else next(
        (mime for mime in types if mime.startswith("image/")), None
    )
    if image_type is None:
        raise CaptureError("There is no image on the clipboard.")
    return pixbuf_from_bytes(run(["wl-paste", "--no-newline", "--type", image_type]))
