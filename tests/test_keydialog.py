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

    def make(current="anthropic", environment=frozenset()):
        dialog = submodules.keydialog.ApiKeyDialog(
            PROVIDERS,
            current,
            "the test keyring",
            lambda provider, key: events.append(("save", provider, key)),
            lambda provider: events.append(("remove", provider)),
            lambda: events.append(("close",)),
            environment=environment,
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
    assert "Checking which keys are stored." in targets


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


def provider_texts(dialog):
    return [row[0] for row in dialog.provider.get_model()]


def test_remove_button_hidden_until_keys_are_known(make_dialog):
    make, _events = make_dialog
    dialog = make("openai")
    dialog.dialog.show_all()  # Realizes visibility flags; the window is destroyed unseen.
    dialog.dialog.hide()
    assert not dialog.remove_button.get_visible()
    assert provider_texts(dialog) == ["OpenAI", "Claude"]


def test_stored_keys_are_shown_and_remove_follows_the_selection(make_dialog):
    make, _events = make_dialog
    dialog = make("anthropic", environment=frozenset({"anthropic"}))
    dialog.set_stored({"openai"})

    assert dialog.summary.get_text() == "Stored keys: OpenAI."
    assert provider_texts(dialog) == [
        "OpenAI (key stored)",
        "Claude (no stored key, using its environment variable)",
    ]
    assert dialog.provider.get_active_id() == "anthropic"
    assert not dialog.remove_button.get_visible()

    dialog.provider.set_active_id("openai")
    assert dialog.remove_button.get_visible()
    dialog.provider.set_active_id("anthropic")
    assert not dialog.remove_button.get_visible()


def test_no_stored_keys(make_dialog):
    make, _events = make_dialog
    dialog = make("openai")
    dialog.set_stored(set())
    assert dialog.summary.get_text() == "No API keys are stored yet."
    assert provider_texts(dialog) == ["OpenAI (no key)", "Claude (no key)"]
    assert not dialog.remove_button.get_visible()


def test_failed_check_hides_remove(make_dialog):
    make, _events = make_dialog
    dialog = make("openai")
    dialog.set_stored({"openai"})
    assert dialog.remove_button.get_visible()
    dialog.set_check_failed("The keyring is locked.")
    assert dialog.summary.get_text() == (
        "Could not check which keys are stored. The keyring is locked.")
    assert not dialog.remove_button.get_visible()
