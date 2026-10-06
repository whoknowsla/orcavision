import pytest


@pytest.fixture
def s(submodules, monkeypatch):
    shots = submodules.shots
    monkeypatch.setattr(shots, "_name", lambda obj: {"WINDOW": "Inbox - Mail",
                                                     "BUTTON": "Send"}.get(obj, ""))
    monkeypatch.setattr(shots, "_role", lambda obj: "push button")
    return shots


@pytest.fixture
def x11(s, monkeypatch):
    monkeypatch.setattr(s, "is_wayland", lambda: False)
    monkeypatch.setattr(s, "capture_desktop", lambda: "DESKTOP")
    monkeypatch.setattr(s, "capture_region", lambda rect: ("REGION", rect))
    return s


@pytest.fixture
def wayland(s, monkeypatch):
    calls = []
    monkeypatch.setattr(s, "is_wayland", lambda: True)
    monkeypatch.setattr(s, "backends", lambda: ["CHAIN"])
    monkeypatch.setattr(s, "wayland_desktop", lambda chain: calls.append("desktop") or "W-DESKTOP")
    monkeypatch.setattr(s, "wayland_window", lambda chain: calls.append("window") or None)
    s.calls = calls
    return s


def test_x11_desktop_is_captured_immediately(x11):
    assert x11.desktop_shot()().pixbuf == "DESKTOP"


def test_x11_window(x11, monkeypatch, submodules):
    rect = submodules.capture.Rect(1, 2, 3, 4)
    monkeypatch.setattr(x11, "active_window_rect", lambda window: rect)
    captured = x11.window_shot("WINDOW")()
    assert (captured.pixbuf, captured.hint) == (("REGION", rect), "")


def test_x11_window_without_geometry_falls_back_to_desktop(x11, monkeypatch, submodules):
    monkeypatch.setattr(x11, "active_window_rect", lambda window: None)
    captured = x11.window_shot("WINDOW")()
    assert captured.pixbuf == "DESKTOP"
    assert captured.hint == ('The screenshot shows the whole screen. '
                             'Describe only the window titled "Inbox - Mail".')
    with pytest.raises(submodules.capture.CaptureError, match="No active window found."):
        x11.window_shot(None)


def test_x11_object(x11, monkeypatch, submodules):
    rect = submodules.capture.Rect(5, 6, 7, 8)
    monkeypatch.setattr(x11, "accessible_rect", lambda obj: rect)
    assert x11.object_shot("WINDOW", "BUTTON")().pixbuf == ("REGION", rect)

    monkeypatch.setattr(x11, "accessible_rect", lambda obj: None)
    with pytest.raises(submodules.capture.CaptureError, match="no size on screen"):
        x11.object_shot("WINDOW", "BUTTON")
    with pytest.raises(submodules.capture.CaptureError, match="no focused object"):
        x11.object_shot("WINDOW", None)


def test_wayland_capture_waits_for_worker(wayland):
    shot = wayland.desktop_shot()
    assert wayland.calls == []
    assert shot().pixbuf == "W-DESKTOP"
    assert wayland.calls == ["desktop"]


def test_wayland_window_falls_back_with_hint(wayland, monkeypatch):
    captured = wayland.window_shot("WINDOW")()
    assert wayland.calls == ["window", "desktop"]
    assert (captured.pixbuf, captured.hint) == ("W-DESKTOP", wayland.window_hint("Inbox - Mail"))

    monkeypatch.setattr(wayland, "wayland_window", lambda chain: "W-WINDOW")
    captured = wayland.window_shot("WINDOW")()
    assert (captured.pixbuf, captured.hint) == ("W-WINDOW", "")


def test_wayland_object_names_the_object(wayland, monkeypatch):
    monkeypatch.setattr(wayland, "wayland_window", lambda chain: "W-WINDOW")
    captured = wayland.object_shot("WINDOW", "BUTTON")()
    assert captured.pixbuf == "W-WINDOW"
    assert captured.hint == ('Describe only the push button named "Send", '
                             "not the rest of the screenshot.")


def test_hints_without_names(s):
    assert s.window_hint("").endswith("Describe only the window in front.")
    assert s.object_hint("image", "") == (
        "Describe only the focused image, not the rest of the screenshot.")


def test_clipboard_shot(s, monkeypatch):
    monkeypatch.setattr(s, "is_wayland", lambda: False)
    assert s.clipboard_shot() is None

    monkeypatch.setattr(s, "is_wayland", lambda: True)
    monkeypatch.setattr(s.shutil, "which", lambda tool: None)
    assert s.clipboard_shot() is None

    monkeypatch.setattr(s.shutil, "which", lambda tool: "/usr/bin/wl-paste")
    monkeypatch.setattr(s, "clipboard_image", lambda: "CLIP")
    assert s.clipboard_shot()().pixbuf == "CLIP"


def test_accessible_name_and_role_tolerate_non_accessibles(submodules):
    shots = submodules.shots
    assert shots._name(None) == ""
    assert shots._name(object()) == ""
    assert shots._role(object()) == "object"
