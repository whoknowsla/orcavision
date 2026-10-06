import pytest
from gi.repository import GdkPixbuf


@pytest.fixture
def capture(submodules):
    return submodules.capture


@pytest.mark.parametrize("rect,expected", [
    ((10, 20, 100, 50), (10, 20, 100, 50)),
    ((-10, -20, 100, 50), (0, 0, 90, 30)),
    ((1900, 1000, 100, 200), (1900, 1000, 20, 80)),
    ((2000, 0, 10, 10), None),
    ((0, 0, 0, 10), None),
])
def test_clamp_rect(capture, rect, expected):
    result = capture.clamp_rect(capture.Rect(*rect), 1920, 1080)
    assert result == (capture.Rect(*expected) if expected else None)


@pytest.mark.parametrize("size,max_size,expected", [
    ((2560, 1600), 2048, (2048, 1280)),
    ((1600, 2560), 2048, (1280, 2048)),
    ((800, 600), 2048, (800, 600)),
    ((4000, 3000), 0, (4000, 3000)),
    ((5000, 1), 100, (100, 1)),
])
def test_scaled_size(capture, size, max_size, expected):
    assert capture.scaled_size(*size, max_size) == expected


def _decode(png):
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(png)
    loader.close()
    pixbuf = loader.get_pixbuf()
    return pixbuf.get_width(), pixbuf.get_height()


def test_encode_png_scales_down(capture):
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 400, 200)
    pixbuf.fill(0x336699FF)

    png = capture.encode_png(pixbuf, 100)
    assert png.startswith(b"\x89PNG")
    assert _decode(png) == (100, 50)
    assert _decode(capture.encode_png(pixbuf, 0)) == (400, 200)


def test_accessible_rect_handles_non_components(capture):
    assert capture.accessible_rect(None) is None
    assert capture.accessible_rect(object()) is None


def test_sound_themes(submodules):
    sounds = submodules.sounds
    assert sounds.tone_for("modern", "open").frequency == 1047
    assert sounds.tone_for("unknown", "done") == sounds.tone_for("modern", "done")
    assert sounds.tone_for("none", "open") is None
    assert sounds.is_silent("none")
    assert not sounds.is_silent("minimal")
    assert {name for name, _label in sounds.THEME_CHOICES} == set(sounds.THEMES)
