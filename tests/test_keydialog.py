import gi
import pytest

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

PROVIDERS = (("openai", "OpenAI"), ("anthropic", "Claude"))


@pytest.fixture
def make_dialog(submodules):
    if not Gtk.init_check()[0]:
        pytest.skip("GTK cannot open a display")
    events = []
    dialogs = []

    def make(current="anthropic"):
        dialog = submodules.keydialog.ApiKeyDialog(
            PROVIDERS,
            current,
            "the test keyring",
            lambda provider, key: events.append(("save", provider, key)),
            lambda provider: events.append(("remove", provider)),
            lambda: events.append(("close",)),
        )
        dialogs.append(dialog)
        return dialog

    yield make, events
    for dialog in dialogs:
        dialog.dialog.destroy()


def test_key_entry_is_masked_and_labelled(make_dialog):
    make, _events = make_dialog
    dialog = make()  # Never shown: no window appears during tests.
    assert dialog.key.get_visibility() is False
    assert dialog.key.get_input_purpose() == Gtk.InputPurpose.PASSWORD
    assert dialog.provider.get_active_id() == "anthropic"

    labels = [child for child in dialog.dialog.get_content_area().get_children()[0].get_children()
              if isinstance(child, Gtk.Label)]
    targets = {label.get_text(): label.get_mnemonic_widget() for label in labels}
    assert targets["API key:"] is dialog.key
    assert targets["Provider:"] is dialog.provider
    assert "Keys are stored in the test keyring, not in Orca's settings." in targets


def test_unknown_provider_falls_back_to_first(make_dialog):
    make, _events = make_dialog
    assert make("ollama").provider.get_active_id() == "openai"


def test_save_hands_over_key_and_clears_entry(make_dialog, submodules):
    make, events = make_dialog
    dialog = make()
    dialog.key.set_text("  sk-typed  ")
    dialog.dialog.response(Gtk.ResponseType.OK)
    assert events == [("close",), ("save", "anthropic", "sk-typed")]
    assert dialog.key.get_text() == ""


def test_remove_and_cancel(make_dialog, submodules):
    make, events = make_dialog
    make().dialog.response(submodules.keydialog.REMOVE_RESPONSE)
    assert events == [("close",), ("remove", "anthropic")]

    events.clear()
    dialog = make()
    dialog.key.set_text("sk-not-saved")
    dialog.dialog.response(Gtk.ResponseType.CANCEL)
    assert events == [("close",)]
    assert dialog.key.get_text() == ""
