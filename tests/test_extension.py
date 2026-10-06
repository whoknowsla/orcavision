import types

import pytest


class FakeGLib:
    def __init__(self):
        self.timeouts = {}
        self.removed = []
        self._next_id = 1

    def _add(self, interval, function, *args):
        source_id = self._next_id
        self._next_id += 1
        self.timeouts[source_id] = (interval, function, args)
        return source_id

    timeout_add = _add
    timeout_add_seconds = _add

    def idle_add(self, function, *args):
        function(*args)
        return 0

    def source_remove(self, source_id):
        self.removed.append(source_id)
        self.timeouts.pop(source_id, None)

    def fire(self, source_id):
        _interval, function, args = self.timeouts[source_id]
        return function(*args)


class DeferredThreads:
    """Stands in for the threading module; threads run only when run_all() is called."""

    def __init__(self):
        self.pending = []

    def Thread(self, target, args=(), **_kwargs):  # noqa: N802 - mirrors threading.Thread
        threads = self

        class _Thread:
            def start(self):
                threads.pending.append((target, args))

        return _Thread()

    def run_all(self):
        pending, self.pending = self.pending, []
        for target, args in pending:
            target(*args)


class FakeKeyStore:
    def __init__(self):
        self.keys = {}
        self.error = None
        self.calls = []

    def backend(self, choice):
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(name="fake", label="the test keyring")

    def lookup(self, provider, choice):
        self.calls.append(("lookup", provider, choice))
        self.backend(choice)
        return self.keys.get(provider, "")

    def store(self, provider, key, choice):
        self.calls.append(("store", provider, key, choice))
        backend = self.backend(choice)
        self.keys[provider] = key
        return backend

    def clear(self, provider, choice):
        self.calls.append(("clear", provider, choice))
        backend = self.backend(choice)
        self.keys.pop(provider, None)
        return backend

    def stored(self, providers, choice):
        self.calls.append(("stored", tuple(providers), choice))
        self.backend(choice)
        return {name for name in providers if name in self.keys}


class FakeDialog:
    def __init__(self, providers, current, storage_label, on_save, on_remove, on_close,
                 environment=frozenset()):
        self.providers = providers
        self.current = current
        self.storage_label = storage_label
        self.on_save = on_save
        self.on_remove = on_remove
        self.on_close = on_close
        self.environment = environment
        self.shown = 0
        self.stored = None
        self.check_failed = None

    def show(self):
        self.shown += 1

    def set_stored(self, stored):
        self.stored = stored

    def set_check_failed(self, message):
        self.check_failed = message


class FakeProvider:
    name = "openai"
    label = "OpenAI"
    uses_api_key = True
    env_vars = ("OPENAI_API_KEY",)
    host_setting = ""

    def __init__(self, submodules, result="A text editor with an open file."):
        self.submodules = submodules
        self.result = result
        self.requests = []

    def describe(self, request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def env(ext_module, submodules, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    glib = FakeGLib()
    threads = DeferredThreads()
    provider = FakeProvider(submodules)
    monkeypatch.setattr(ext_module, "GLib", glib)
    monkeypatch.setattr(ext_module, "threading", threads)
    monkeypatch.setattr(ext_module, "get_provider", lambda name: provider)
    captured = submodules.capture.Captured
    monkeypatch.setattr(ext_module, "desktop_shot", lambda: lambda: captured("DESKTOP"))
    monkeypatch.setattr(ext_module, "clipboard_shot", lambda: None)
    monkeypatch.setattr(ext_module, "encode_png", lambda pixbuf, size: b"png:" + repr(
        (pixbuf, size)).encode())

    dialogs = []
    monkeypatch.setattr(ext_module, "ApiKeyDialog",
                        lambda *args, **kwargs: dialogs.append(FakeDialog(*args, **kwargs))
                        or dialogs[-1])

    extension = ext_module.OrcaVision()
    keystore = extension._keystore = FakeKeyStore()
    return types.SimpleNamespace(mod=ext_module, ext=extension, glib=glib, threads=threads,
                                 provider=provider, keystore=keystore, dialogs=dialogs,
                                 controller=extension.controller, sub=submodules,
                                 KeyStoreError=submodules.keystore.KeyStoreError)


def test_commands_and_default_keys(ext_module):
    extension = ext_module.OrcaVision()
    commands = {command.name: command for command in extension._get_commands()}
    kb = ext_module.keybindings

    window = commands["orcavision_describe_window"]
    assert window.function == extension.describe_active_window
    assert window.group_label == "OrcaVision"
    assert (window.desktop_keybinding.keysymstring, window.desktop_keybinding.modifiers) == (
        "d", kb.ORCA_CTRL_MODIFIER_MASK)
    assert window.laptop_keybinding is not window.desktop_keybinding

    desktop = commands["orcavision_describe_desktop"]
    assert desktop.desktop_keybinding.modifiers == (
        kb.ORCA_CTRL_MODIFIER_MASK | kb.SHIFT_MODIFIER_MASK)
    assert desktop.laptop_keybinding.modifiers == desktop.desktop_keybinding.modifiers

    focused = commands["orcavision_describe_object"]
    assert focused.desktop_keybinding.modifiers == kb.ORCA_CTRL_ALT_MODIFIER_MASK

    keys = commands["orcavision_manage_keys"]
    assert keys.function == extension.manage_api_keys
    assert (keys.desktop_keybinding.keysymstring, keys.desktop_keybinding.modifiers) == (
        "k", kb.ORCA_CTRL_ALT_MODIFIER_MASK)

    for name in ("orcavision_describe_clipboard", "orcavision_repeat_description",
                 "orcavision_cancel"):
        assert commands[name].desktop_keybinding is None
        assert commands[name].laptop_keybinding is None


def test_preferences_and_defaults(env):
    preferences = {p.key: p for p in env.ext.get_preferences() if p.key}
    assert preferences["provider"].default == "openai"
    assert preferences["openai-model"].default == "gpt-4o"
    assert preferences["anthropic-model"].default == "claude-opus-5-5"
    assert preferences["key-storage"].default == "auto"
    assert preferences["key-storage"].kwargs["options"] == env.sub.keystore.STORAGE_CHOICES
    assert not [key for key in preferences if key.endswith("-api-key")]
    assert preferences["sound-theme"].kwargs["options"] == env.sub.sounds.THEME_CHOICES
    assert {"gemini-model", "ollama-model", "ollama-host", "prompt",
            "language", "max-output-tokens", "temperature", "max-image-size",
            "request-timeout", "copy-to-clipboard"} <= set(preferences)


def test_describe_desktop_success(env):
    assert env.ext.describe_desktop() is True
    modern = env.sub.sounds.THEMES["modern"]
    assert env.controller.tones == [modern["open"].frequency]
    assert len(env.glib.timeouts) == 2  # waiting beep and watchdog

    env.threads.run_all()

    request = env.provider.requests[0]
    assert request.model == "gpt-4o"
    assert request.api_key == "sk-env"
    assert request.image_png == b"png:('DESKTOP', 2048)"
    assert request.prompt == env.sub.config.DEFAULT_PROMPT
    assert env.controller.messages == ["A text editor with an open file."]
    assert env.controller.clipboard == "A text editor with an open file."
    assert env.controller.tones[-1] == modern["done"].frequency
    assert env.glib.timeouts == {}

    env.ext.repeat_last_description()
    assert env.controller.messages[-1] == "A text editor with an open file."


def test_settings_override_defaults(env):
    env.ext.settings.values.update({
        "openai-model": "gpt-4o-mini", "language": "German", "key-storage": "kwallet",
        "max-image-size": 0, "copy-to-clipboard": False, "sound-theme": "none",
    })
    env.ext.describe_desktop()
    assert env.controller.messages == ["Describing desktop."]
    assert env.controller.tones == []
    env.threads.run_all()

    request = env.provider.requests[0]
    assert request.model == "gpt-4o-mini"
    assert env.keystore.calls == [("lookup", "openai", "kwallet")]
    assert request.prompt.endswith("Respond in German.")
    assert request.image_png == b"png:('DESKTOP', 0)"
    assert env.controller.clipboard is None


def test_provider_error_is_presented(env):
    env.provider.result = env.sub.providers.DescribeError("OpenAI rejected the API key.")
    env.ext.describe_desktop()
    env.threads.run_all()
    assert env.controller.messages == ["OpenAI rejected the API key."]
    assert env.controller.tones[-1] == env.sub.sounds.THEMES["modern"]["error"].frequency
    assert env.controller.clipboard is None


def test_unexpected_error_is_presented(env):
    env.provider.result = KeyError("boom")
    env.ext.describe_desktop()
    env.threads.run_all()
    assert env.controller.messages == ["Unexpected error: 'boom'"]


def test_describe_active_window_passes_hint_to_prompt(env, monkeypatch):
    seen = []

    def window_shot(window):
        seen.append(window)
        return lambda: env.sub.capture.Captured("WINDOW", "Describe only the window.")

    monkeypatch.setattr(env.mod, "window_shot", window_shot)
    env.controller.active_window = "ATSPI-WINDOW"
    env.ext.settings.values["language"] = "German"

    env.ext.describe_active_window()
    env.threads.run_all()
    assert seen == ["ATSPI-WINDOW"]
    request = env.provider.requests[0]
    assert request.image_png == b"png:('WINDOW', 2048)"
    assert request.prompt.endswith(
        "\n\nDescribe only the window.\n\nRespond in German.")


def test_capture_failure_does_not_start_request(env, monkeypatch):
    def window_shot(window):
        raise env.sub.capture.CaptureError("No active window found.")

    monkeypatch.setattr(env.mod, "window_shot", window_shot)
    env.ext.describe_active_window()
    assert env.controller.messages == ["No active window found."]
    assert env.threads.pending == []
    assert env.glib.timeouts == {}


def test_worker_capture_failure_is_presented(env, monkeypatch):
    def failing_shot():
        raise env.sub.capture.CaptureError("Screen capture failed.")

    monkeypatch.setattr(env.mod, "desktop_shot", lambda: failing_shot)
    env.ext.describe_desktop()
    env.threads.run_all()
    assert env.controller.messages == ["Screen capture failed."]
    assert env.provider.requests == []


def test_focused_object_uses_window_and_object(env, monkeypatch):
    seen = []
    monkeypatch.setattr(env.mod, "object_shot", lambda window, obj: seen.append(
        (window, obj)) or (lambda: env.sub.capture.Captured("OBJECT")))
    env.controller.active_window = "WINDOW"
    env.controller.current_object = "BUTTON"

    env.ext.describe_focused_object()
    env.threads.run_all()
    assert seen == [("WINDOW", "BUTTON")]
    assert env.provider.requests[0].image_png == b"png:('OBJECT', 2048)"


def test_clipboard_image_through_gtk(env, monkeypatch):
    callbacks = []
    monkeypatch.setattr(env.mod, "request_clipboard_image", callbacks.append)

    env.ext.describe_clipboard_image()
    callbacks[0](None)
    assert env.controller.messages == ["There is no image on the clipboard."]

    env.ext.describe_clipboard_image()
    callbacks[1]("CLIP")
    env.threads.run_all()
    assert env.provider.requests[0].image_png == b"png:('CLIP', 2048)"


def test_clipboard_image_through_wl_paste(env, monkeypatch):
    monkeypatch.setattr(env.mod, "clipboard_shot",
                        lambda: lambda: env.sub.capture.Captured("WL-CLIP"))
    monkeypatch.setattr(env.mod, "request_clipboard_image",
                        lambda callback: pytest.fail("GTK clipboard should not be used"))
    env.ext.describe_clipboard_image()
    env.threads.run_all()
    assert env.provider.requests[0].image_png == b"png:('WL-CLIP', 2048)"


def test_providers_without_keys_get_none(env, monkeypatch):
    monkeypatch.setattr(env.provider, "uses_api_key", False)
    env.ext.describe_desktop()
    env.threads.run_all()
    assert env.provider.requests[0].api_key == ""


def test_cancel_discards_result(env):
    env.ext.cancel_description()
    assert env.controller.messages == ["No description in progress."]

    env.ext.describe_desktop()
    env.ext.cancel_description()
    assert env.controller.messages[-1] == "Description cancelled."
    assert env.glib.timeouts == {}

    env.threads.run_all()
    assert env.controller.messages[-1] == "Description cancelled."
    assert env.controller.clipboard is None


def test_new_request_supersedes_old_one(env):
    env.ext.describe_desktop()
    env.ext.describe_desktop()
    assert len(env.glib.timeouts) == 2

    env.threads.run_all()
    assert env.controller.messages == ["A text editor with an open file."]


def test_watchdog_times_out_request(env):
    env.ext.describe_desktop()
    watchdog_id = max(env.glib.timeouts)
    interval = env.glib.timeouts[watchdog_id][0]
    assert interval == 120 + 15

    assert env.glib.fire(watchdog_id) is False
    assert env.controller.messages == ["The request timed out."]

    env.threads.run_all()
    assert env.controller.messages == ["The request timed out."]


def test_waiting_beep_repeats(env):
    env.ext.describe_desktop()
    waiting_id = min(env.glib.timeouts)
    assert env.glib.fire(waiting_id) is True
    assert env.controller.tones[-1] == env.sub.sounds.THEMES["modern"]["waiting"].frequency


def test_shutdown_stops_timers(env):
    env.ext.describe_desktop()
    env.ext.on_shutdown()
    assert env.glib.timeouts == {}
    env.threads.run_all()
    assert env.controller.messages == []


def test_keyring_key_wins_over_environment(env):
    env.keystore.keys["openai"] = "sk-stored"
    env.ext.describe_desktop()
    env.threads.run_all()
    assert env.provider.requests[0].api_key == "sk-stored"


def test_keyring_error_falls_back_to_environment(env):
    env.keystore.error = env.KeyStoreError("The keyring is locked.")
    env.ext.describe_desktop()
    env.threads.run_all()
    assert env.provider.requests[0].api_key == "sk-env"


def test_keyring_error_without_environment_key(env, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    env.keystore.error = env.KeyStoreError("The keyring is locked.")
    env.ext.describe_desktop()
    env.threads.run_all()
    assert env.provider.requests == []
    assert env.controller.messages == ["Could not read the OpenAI API key. The keyring is locked."]


def test_key_left_in_settings_is_used_and_moved(env):
    env.ext.settings.values["openai-api-key"] = "sk-legacy"
    env.ext.describe_desktop()
    env.threads.run_all()
    assert env.provider.requests[0].api_key == "sk-legacy"
    assert env.keystore.keys == {"openai": "sk-legacy"}
    assert "openai-api-key" not in env.ext.settings.values


def test_key_left_in_settings_stays_when_keyring_fails(env):
    env.keystore.error = env.KeyStoreError("No keyring was found.")
    env.ext.settings.values["openai-api-key"] = "sk-legacy"
    env.ext.describe_desktop()
    env.threads.run_all()
    assert env.provider.requests[0].api_key == "sk-legacy"
    assert env.ext.settings.values["openai-api-key"] == "sk-legacy"


def test_on_ready_moves_keys_out_of_settings(env):
    env.ext.on_ready()
    assert env.threads.pending == []

    env.ext.settings.values.update({"openai-api-key": "a", "anthropic-api-key": " b "})
    env.ext.on_ready()
    env.threads.run_all()
    assert env.keystore.keys == {"openai": "a", "anthropic": "b"}
    assert not [key for key in env.ext.settings.values if key.endswith("-api-key")]
    assert env.controller.messages == [
        "OrcaVision moved 2 API keys from Orca's settings to the test keyring."]


def test_on_enabled_reports_when_keys_cannot_move(env):
    env.keystore.error = env.KeyStoreError("No keyring was found.")
    env.ext.settings.values["gemini-api-key"] = "g"
    env.ext.on_enabled()
    env.threads.run_all()
    assert env.ext.settings.values["gemini-api-key"] == "g"
    assert env.controller.messages == [
        "OrcaVision could not move API keys out of Orca's settings. No keyring was found."]


def test_manage_keys_opens_one_dialog(env):
    env.ext.settings.values["provider"] = "anthropic"
    env.ext.manage_api_keys()
    env.ext.manage_api_keys()
    assert len(env.dialogs) == 1
    dialog = env.dialogs[0]
    assert dialog.shown == 2
    assert dialog.current == "anthropic"
    assert dialog.storage_label == "the test keyring"
    assert [name for name, _label in dialog.providers] == [
        "openai", "anthropic", "gemini", "openai-compatible"]

    dialog.on_close()
    env.ext.manage_api_keys()
    assert len(env.dialogs) == 2


def test_manage_keys_without_storage(env):
    env.keystore.error = env.KeyStoreError("No keyring was found.")
    env.ext.manage_api_keys()
    assert env.dialogs == []
    assert env.controller.messages == ["No keyring was found."]


def test_saving_and_removing_keys(env):
    env.ext.manage_api_keys()
    dialog = env.dialogs[0]

    dialog.on_save("openai", "")
    assert env.controller.messages[-1] == "No API key entered."

    env.ext.settings.values["openai-api-key"] = "old-plaintext"
    dialog.on_save("openai", "sk-new")
    assert "openai-api-key" not in env.ext.settings.values
    env.threads.run_all()
    assert env.keystore.keys == {"openai": "sk-new"}
    assert env.controller.messages[-1] == "OpenAI API key saved in the test keyring."

    dialog.on_remove("openai")
    env.threads.run_all()
    assert env.keystore.keys == {}
    assert env.controller.messages[-1] == "OpenAI API key removed from the test keyring."

    env.keystore.error = env.KeyStoreError("The keyring refused the key.")
    dialog.on_save("openai", "sk-other")
    env.threads.run_all()
    assert env.controller.messages[-1] == (
        "The OpenAI API key was not changed. The keyring refused the key.")


def test_manage_keys_shows_which_keys_are_stored(env):
    env.keystore.keys["anthropic"] = "sk-stored"
    env.ext.manage_api_keys()
    dialog = env.dialogs[0]
    assert dialog.stored is None  # Checked in the background, after the dialog appears.
    # The fake provider reads OPENAI_API_KEY, which the env fixture sets.
    assert dialog.environment == {"openai", "anthropic", "gemini", "openai-compatible"}

    env.threads.run_all()
    assert dialog.stored == {"anthropic"}
    assert ("stored", ("openai", "anthropic", "gemini", "openai-compatible"), "auto") in (
        env.keystore.calls)


def test_manage_keys_reports_failed_check(env):
    env.ext.manage_api_keys()
    env.keystore.error = env.KeyStoreError("The keyring is locked.")
    env.threads.run_all()
    assert env.dialogs[0].check_failed == "The keyring is locked."
    assert env.dialogs[0].stored is None


def test_check_result_is_ignored_after_dialog_closes(env):
    env.ext.manage_api_keys()
    dialog = env.dialogs[0]
    dialog.on_close()
    env.threads.run_all()
    assert dialog.stored is None
