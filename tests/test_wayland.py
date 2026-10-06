import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from gi.repository import GdkPixbuf

TESTS_DIR = Path(__file__).resolve().parent


@pytest.fixture
def w(submodules):
    return submodules.wayland


@pytest.fixture
def Rect(submodules):  # noqa: N802 - mirrors the class name
    return submodules.capture.Rect


def write_png(path, width=40, height=20):
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    pixbuf.fill(0x204060FF)
    pixbuf.savev(str(path), "png", [], [])


def png_bytes(width=40, height=20):
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    pixbuf.fill(0x204060FF)
    return bytes(pixbuf.save_to_bufferv("png", [], [])[1])


@pytest.mark.parametrize("environ,expected", [
    ({"XDG_SESSION_TYPE": "wayland"}, True),
    ({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-0"}, False),
    ({"WAYLAND_DISPLAY": "wayland-0"}, True),
    ({"XDG_SESSION_TYPE": "tty"}, False),
    ({}, False),
])
def test_is_wayland(w, environ, expected):
    assert w.is_wayland(environ) is expected


def names(chain):
    return [backend.name for backend in chain]


def which_only(*tools):
    return lambda tool: f"/usr/bin/{tool}" if tool in tools else None


def test_backends_for_sway(w):
    chain = w.backends({"XDG_CURRENT_DESKTOP": "sway", "SWAYSOCK": "/run/sway.sock"},
                       which_only("grim", "swaymsg", "hyprctl"))
    assert names(chain) == ["grim", "xdg-desktop-portal"]
    assert chain[0]._window_rect is w.sway_window_rect


def test_backends_for_hyprland(w):
    chain = w.backends({"XDG_CURRENT_DESKTOP": "Hyprland", "HYPRLAND_INSTANCE_SIGNATURE": "x",
                        "SWAYSOCK": "/stale"}, which_only("grim", "swaymsg", "hyprctl"))
    assert names(chain) == ["grim", "xdg-desktop-portal"]
    assert chain[0]._window_rect is w.hyprland_window_rect


def test_backends_for_kde_gnome_and_others(w):
    assert names(w.backends({"XDG_CURRENT_DESKTOP": "KDE"}, which_only("spectacle", "grim"))) == [
        "spectacle", "grim", "xdg-desktop-portal"]
    assert names(w.backends({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"},
                            which_only("gnome-screenshot"))) == [
        "gnome-screenshot", "xdg-desktop-portal"]
    assert names(w.backends({"XDG_CURRENT_DESKTOP": "GNOME"}, which_only())) == [
        "xdg-desktop-portal"]
    chain = w.backends({"XDG_CURRENT_DESKTOP": "river"}, which_only("grim"))
    assert names(chain) == ["grim", "xdg-desktop-portal"]
    assert chain[0]._window_rect is None


SWAY_TREE = {
    "type": "root", "focused": False, "nodes": [{
        "type": "output", "focused": False, "nodes": [{
            "type": "workspace", "focused": False, "nodes": [
                {"type": "con", "focused": False, "rect": {"x": 0, "y": 0, "width": 5, "height": 5}},
            ],
            "floating_nodes": [{
                "type": "floating_con", "focused": True,
                "rect": {"x": 10, "y": 20, "width": 800, "height": 600},
                "window_rect": {"x": 2, "y": 30, "width": 796, "height": 568},
            }],
        }],
    }],
}


def fake_run(outputs, calls=None):
    def run(command):
        if calls is not None:
            calls.append(command)
        for prefix, output in outputs.items():
            if command[0] == prefix:
                if isinstance(output, Exception):
                    raise output
                return output
        return b""
    return run


def test_sway_window_rect(w, Rect):
    assert w.sway_window_rect(fake_run({"swaymsg": json.dumps(SWAY_TREE).encode()})) == Rect(
        12, 50, 796, 568)

    workspace_focused = {"type": "workspace", "focused": True, "nodes": []}
    assert w.sway_window_rect(fake_run({"swaymsg": json.dumps(workspace_focused).encode()})) is None
    assert w.sway_window_rect(fake_run({"swaymsg": b"not json"})) is None
    assert w.sway_window_rect(fake_run({"swaymsg": w.CaptureError("no sway")})) is None


def test_hyprland_window_rect(w, Rect):
    window = {"at": [100, 50], "size": [1200, 800], "title": "Files"}
    assert w.hyprland_window_rect(fake_run({"hyprctl": json.dumps(window).encode()})) == Rect(
        100, 50, 1200, 800)
    assert w.hyprland_window_rect(fake_run({"hyprctl": b"{}"})) is None
    assert w.hyprland_window_rect(fake_run({"hyprctl": b"Invalid"})) is None


def test_grim_window_uses_compositor_geometry(w):
    calls = []
    run = fake_run({"swaymsg": json.dumps(SWAY_TREE).encode()}, calls)
    assert w.GrimBackend(w.sway_window_rect).window("/tmp/x.png", run) is True
    assert calls[-1] == ["grim", "-g", "12,50 796x568", "/tmp/x.png"]
    assert w.GrimBackend().window("/tmp/x.png", run) is False


def test_tool_commands(w):
    calls = []
    run = fake_run({}, calls)
    w.SpectacleBackend().desktop("/p.png", run)
    w.SpectacleBackend().window("/p.png", run)
    w.GnomeScreenshotBackend().desktop("/p.png", run)
    w.GnomeScreenshotBackend().window("/p.png", run)
    w.GrimBackend().desktop("/p.png", run)
    assert calls == [
        ["spectacle", "--background", "--nonotify", "--fullscreen", "--output", "/p.png"],
        ["spectacle", "--background", "--nonotify", "--activewindow", "--output", "/p.png"],
        ["gnome-screenshot", "--file", "/p.png"],
        ["gnome-screenshot", "--window", "--file", "/p.png"],
        ["grim", "/p.png"],
    ]


class FakeBackend:
    def __init__(self, error, name, desktop_ok=True, window_ok=False, size=(40, 20)):
        self.error = error
        self.name = name
        self.desktop_ok = desktop_ok
        self.window_ok = window_ok
        self.size = size
        self.calls = []

    def desktop(self, path, run):
        self.calls.append("desktop")
        if not self.desktop_ok:
            raise self.error(f"{self.name} broke")
        write_png(path, *self.size)

    def window(self, path, run):
        self.calls.append("window")
        if self.window_ok:
            write_png(path, *self.size)
        return self.window_ok


@pytest.fixture
def fakes(w):
    return lambda *args, **kwargs: FakeBackend(w.CaptureError, *args, **kwargs)


def test_capture_desktop_falls_through_backends(w, fakes):
    broken, working = fakes("grim", desktop_ok=False), fakes("portal", size=(64, 32))
    pixbuf = w.capture_desktop([broken, working])
    assert (pixbuf.get_width(), pixbuf.get_height()) == (64, 32)
    assert broken.calls == ["desktop"] and working.calls == ["desktop"]


def test_capture_desktop_reports_last_error(w, fakes):
    with pytest.raises(w.CaptureError, match=r"Screen capture failed \(portal broke\)\. On Wayland"):
        w.capture_desktop([fakes("grim", desktop_ok=False), fakes("portal", desktop_ok=False)])


def test_capture_window(w, fakes):
    assert w.capture_window([fakes("portal")]) is None
    pixbuf = w.capture_window([fakes("grim"), fakes("spectacle", window_ok=True, size=(30, 10))])
    assert pixbuf.get_width() == 30


def test_portal_backend_moves_file(w, tmp_path):
    source = tmp_path / "Screenshot from portal.png"
    write_png(source)
    target = tmp_path / "out" / "screen.png"
    target.parent.mkdir()
    w.PortalBackend(lambda: str(source)).desktop(str(target), None)
    assert target.exists() and not source.exists()


def test_clipboard_image(w):
    calls = []

    def run(command):
        calls.append(command)
        if "--list-types" in command:
            return b"text/plain\nimage/jpeg\nimage/png\n"
        return png_bytes(12, 8)

    pixbuf = w.clipboard_image(run)
    assert (pixbuf.get_width(), pixbuf.get_height()) == (12, 8)
    assert calls[-1] == ["wl-paste", "--no-newline", "--type", "image/png"]


@pytest.mark.parametrize("list_output", [b"text/plain\nUTF8_STRING\n", None])
def test_clipboard_without_image(w, list_output):
    def run(command):
        if list_output is None:
            raise w.CaptureError("wl-paste failed: Nothing is copied")
        return list_output

    with pytest.raises(w.CaptureError, match="There is no image on the clipboard."):
        w.clipboard_image(run)


def test_run_tool(w):
    assert w.run_tool([sys.executable, "-c", "print('hi')"]) == b"hi\n"
    with pytest.raises(w.CaptureError, match="failed: oops"):
        w.run_tool([sys.executable, "-c", "import sys; print('oops', file=sys.stderr); sys.exit(3)"])
    with pytest.raises(w.CaptureError, match="is not installed"):
        w.run_tool(["orcavision-no-such-tool"])


PORTAL_SCRIPT = textwrap.dedent("""
    import os, sys, threading, urllib.parse
    sys.path.insert(0, sys.argv[1])
    import conftest
    conftest.install_fake_orca(sys.modules.__setitem__)
    conftest.load_extension_package()
    wayland = sys.modules["orca_user_extension.orcavision.wayland"]
    from gi.repository import Gio, GLib

    address, mode, image_path = sys.argv[2], sys.argv[3], sys.argv[4]
    flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
             | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
    XML = '''<node><interface name="org.freedesktop.portal.Screenshot">
      <method name="Screenshot"><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/>
      <arg type="o" direction="out"/></method></interface></node>'''

    def serve(ready):
        context = GLib.MainContext.new()
        context.push_thread_default()
        bus = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)

        def on_call(conn, sender, path, iface, method, params, invocation):
            _parent, options = params.unpack()
            assert options["interactive"] is False
            handle = ("/org/freedesktop/portal/desktop/request/"
                      + sender[1:].replace(".", "_") + "/" + options["handle_token"])
            invocation.return_value(GLib.Variant("(o)", (handle,)))
            if mode == "ok":
                uri = "file://" + urllib.parse.quote(image_path)
                code, results = 0, {"uri": GLib.Variant("s", uri)}
            else:
                code, results = 1, {}
            conn.emit_signal(sender, handle, "org.freedesktop.portal.Request", "Response",
                             GLib.Variant("(ua{sv})", (code, results)))

        node = Gio.DBusNodeInfo.new_for_xml(XML)
        bus.register_object("/org/freedesktop/portal/desktop", node.interfaces[0], on_call)
        bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                      "RequestName", GLib.Variant("(su)", ("org.freedesktop.portal.Desktop", 0)),
                      None, Gio.DBusCallFlags.NONE, -1, None)
        ready.set()
        GLib.MainLoop.new(context, False).run()

    if mode != "missing":
        ready = threading.Event()
        threading.Thread(target=serve, args=(ready,), daemon=True).start()
        ready.wait(10)

    client = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
    try:
        print("PATH", wayland.portal_screenshot(timeout=10, connection=client))
    except wayland.CaptureError as error:
        print("ERROR", error)
""")


@pytest.mark.parametrize("mode,expected", [
    ("ok", "PATH {image}"),
    ("deny", "ERROR The screenshot was cancelled or not permitted."),
    ("missing", "ERROR The screenshot portal is not available."),
])
def test_portal_screenshot_against_fake_portal(private_bus, tmp_path, mode, expected):
    image = tmp_path / "Screenshot from 2026.png"
    write_png(image)
    result = subprocess.run(
        [sys.executable, "-c", PORTAL_SCRIPT, str(TESTS_DIR), private_bus, mode, str(image)],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": private_bus},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == expected.format(image=image)
