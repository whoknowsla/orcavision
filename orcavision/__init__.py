"""OrcaVision: describe what is on screen with a vision model, spoken by Orca.

Orca+Ctrl+D describes the active window, Orca+Ctrl+Shift+D the whole desktop
and Orca+Ctrl+Alt+D the focused object. Orca+Ctrl+Alt+K stores API keys in
the user's keyring. Describing the clipboard image, repeating the last
description and cancelling a request have no keys by default; assign them in
Orca's preferences.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gi.repository import GLib

from orca import keybindings
from orca.command import Command, KeyboardCommand
from orca.extension import Extension, ExtensionPreference

# Import names, not submodules: Orca loads this package as
# orca_user_extension.orcavision without registering orca_user_extension, and
# "from . import module" fails in that situation.
from .capture import CaptureError, Shot, encode_png, request_clipboard_image, still_shot
from .config import DEFAULTS, build_prompt, environment_api_key
from .keydialog import ApiKeyDialog
from .keystore import STORAGE_CHOICES, KeyStore, KeyStoreError
from .providers import (
    DescribeError,
    DescribeRequest,
    Provider,
    get_provider,
    key_provider_choices,
    provider_choices,
    provider_names,
)
from .shots import clipboard_shot, desktop_shot, object_shot, window_shot
from .sounds import THEME_CHOICES, WAITING_INTERVAL_MS, is_silent, tone_for

# Time the watchdog allows beyond the request timeout before giving up.
_WATCHDOG_GRACE_SECONDS = 15


@dataclass(frozen=True)
class _Job:
    """Settings for one description request, read on Orca's main thread."""

    provider: str
    model: str
    host: str
    key_storage: str
    # A key left in Orca's settings by an earlier version, until it is moved.
    legacy_key: str
    environment_key: str
    prompt: str
    language: str
    max_output_tokens: int
    temperature: float
    max_image_size: int
    timeout: int
    sound_theme: str
    copy_to_clipboard: bool


class OrcaVision(Extension):
    """Describes screen content with a vision model and presents it through Orca."""

    GROUP_LABEL = "OrcaVision"
    DESCRIPTION = "Describes the active window, desktop, focused object or clipboard image with a vision model."
    VERSION = "1.0"
    AUTHOR = "Deniz Aygun"

    def __init__(self) -> None:
        self._generation = 0
        self._active_generation: int | None = None
        self._waiting_source_id = 0
        self._watchdog_source_id = 0
        self._last_description = ""
        self._keystore = KeyStore()
        self._key_dialog: ApiKeyDialog | None = None
        super().__init__()

    def get_preferences(self) -> list[ExtensionPreference]:
        return [
            ExtensionPreference.enum(
                "provider", "Provider", provider_choices(), DEFAULTS["provider"]
            ),
            ExtensionPreference.info(
                "API keys are kept in your keyring, never in Orca's settings. Press "
                "Orca+Ctrl+Alt+K to store or remove a key. A key can also come from "
                "OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY or "
                "OPENAI_COMPATIBLE_API_KEY in Orca's environment."
            ),
            ExtensionPreference.enum(
                "key-storage", "Where to store API keys", STORAGE_CHOICES, DEFAULTS["key-storage"]
            ),
            ExtensionPreference.string("openai-model", "OpenAI model", DEFAULTS["openai-model"]),
            ExtensionPreference.string(
                "anthropic-model", "Claude model", DEFAULTS["anthropic-model"]
            ),
            ExtensionPreference.string("gemini-model", "Gemini model", DEFAULTS["gemini-model"]),
            ExtensionPreference.string("ollama-model", "Ollama model", DEFAULTS["ollama-model"]),
            ExtensionPreference.string("ollama-host", "Ollama host", DEFAULTS["ollama-host"]),
            ExtensionPreference.string(
                "openai-compatible-base-url",
                "OpenAI-compatible server URL, for example https://openrouter.ai/api/v1",
                DEFAULTS["openai-compatible-base-url"],
            ),
            ExtensionPreference.string(
                "openai-compatible-model",
                "OpenAI-compatible server model",
                DEFAULTS["openai-compatible-model"],
            ),
            ExtensionPreference.string("prompt", "Prompt", DEFAULTS["prompt"]),
            ExtensionPreference.string("language", "Response language", DEFAULTS["language"]),
            ExtensionPreference.integer(
                "max-output-tokens", "Maximum output tokens", DEFAULTS["max-output-tokens"], 16, 8192
            ),
            ExtensionPreference.floating(
                "temperature", "Temperature", DEFAULTS["temperature"], 0.0, 2.0
            ),
            ExtensionPreference.integer(
                "max-image-size",
                "Maximum image size in pixels (0 for original size)",
                DEFAULTS["max-image-size"],
                0,
                8192,
            ),
            ExtensionPreference.integer(
                "request-timeout", "Request timeout in seconds", DEFAULTS["request-timeout"], 5, 600
            ),
            ExtensionPreference.enum(
                "sound-theme", "Sounds", THEME_CHOICES, DEFAULTS["sound-theme"]
            ),
            ExtensionPreference.boolean(
                "copy-to-clipboard",
                "Copy descriptions to the clipboard",
                DEFAULTS["copy-to-clipboard"],
            ),
        ]

    def _get_commands(self) -> list[Command]:
        orca_ctrl = keybindings.ORCA_CTRL_MODIFIER_MASK
        definitions: list[tuple[str, Callable[[], bool], str, tuple[str, int] | None]] = [
            (
                "orcavision_describe_window",
                self.describe_active_window,
                "Describes the active window",
                ("d", orca_ctrl),
            ),
            (
                "orcavision_describe_desktop",
                self.describe_desktop,
                "Describes the whole desktop",
                ("d", orca_ctrl | keybindings.SHIFT_MODIFIER_MASK),
            ),
            (
                "orcavision_describe_object",
                self.describe_focused_object,
                "Describes the focused object",
                ("d", keybindings.ORCA_CTRL_ALT_MODIFIER_MASK),
            ),
            (
                "orcavision_manage_keys",
                self.manage_api_keys,
                "Stores or removes API keys in the keyring",
                ("k", keybindings.ORCA_CTRL_ALT_MODIFIER_MASK),
            ),
            (
                "orcavision_describe_clipboard",
                self.describe_clipboard_image,
                "Describes the image on the clipboard",
                None,
            ),
            (
                "orcavision_repeat_description",
                self.repeat_last_description,
                "Repeats the last description",
                None,
            ),
            (
                "orcavision_cancel",
                self.cancel_description,
                "Cancels the description in progress",
                None,
            ),
        ]

        commands: list[Command] = []
        for name, function, description, binding in definitions:
            # The desktop and laptop layouts each need their own KeyBinding instance.
            commands.append(
                KeyboardCommand(
                    name,
                    function,
                    self.GROUP_LABEL,
                    description,
                    desktop_keybinding=keybindings.KeyBinding(*binding) if binding else None,
                    laptop_keybinding=keybindings.KeyBinding(*binding) if binding else None,
                )
            )
        return commands

    def describe_active_window(self) -> bool:
        """Describes the active window."""

        self._start("window", lambda: window_shot(self.controller.get_active_window_internal()))
        return True

    def describe_desktop(self) -> bool:
        """Describes the whole desktop."""

        self._start("desktop", desktop_shot)
        return True

    def describe_focused_object(self) -> bool:
        """Describes the object Orca considers focused."""

        self._start(
            "focused object",
            lambda: object_shot(
                self.controller.get_active_window_internal(),
                self.controller.get_current_object_internal(),
            ),
        )
        return True

    def describe_clipboard_image(self) -> bool:
        """Describes the image on the clipboard."""

        shot = clipboard_shot()
        if shot is not None:
            self._start("clipboard image", lambda: shot)
        else:
            request_clipboard_image(self._on_clipboard_image)
        return True

    def repeat_last_description(self) -> bool:
        """Presents the last description again."""

        self.controller.present_message_internal(self._last_description or "No description yet.")
        return True

    def cancel_description(self) -> bool:
        """Cancels the description in progress."""

        if self._active_generation is None:
            self.controller.present_message_internal("No description in progress.")
            return True
        self._end_request()
        self.controller.present_message_internal("Description cancelled.")
        return True

    def manage_api_keys(self) -> bool:
        """Opens the dialog for storing or removing API keys in the keyring."""

        if self._key_dialog is not None:
            self._key_dialog.show()
            return True
        try:
            storage = self._keystore.backend(self._key_storage())
        except KeyStoreError as error:
            self.controller.present_message_internal(str(error))
            return True

        current = self.settings.get("provider", default=DEFAULTS["provider"])
        self._key_dialog = ApiKeyDialog(
            key_provider_choices(),
            current,
            storage.label,
            self._save_key,
            self._remove_key,
            self._on_key_dialog_closed,
        )
        self._key_dialog.show()
        return True

    def on_ready(self) -> None:
        self._move_legacy_keys()

    def on_enabled(self) -> None:
        self._move_legacy_keys()

    def on_shutdown(self) -> None:
        self._end_request()

    def _on_clipboard_image(self, pixbuf: Any) -> None:
        if pixbuf is None:
            self._fail("There is no image on the clipboard.", self._snapshot_job().sound_theme)
            return
        self._start("clipboard image", lambda: still_shot(pixbuf))

    def _key_storage(self) -> str:
        return str(self.settings.get("key-storage", default=DEFAULTS["key-storage"]))

    def _legacy_key(self, provider: str) -> str:
        """Returns a key an earlier version stored in Orca's settings, or ""."""

        return str(self.settings.get(f"{provider}-api-key", default="") or "").strip()

    def _on_key_dialog_closed(self) -> None:
        self._key_dialog = None

    def _present_later(self, message: str) -> bool:
        """Presents message; for GLib.idle_add from a worker thread."""

        self.controller.present_message_internal(message)
        return False

    def _save_key(self, provider: str, key: str) -> None:
        if not key:
            self.controller.present_message_internal("No API key entered.")
            return
        self.settings.reset(f"{provider}-api-key")
        self._change_key_in_background(provider, key, self._key_storage())

    def _remove_key(self, provider: str) -> None:
        self.settings.reset(f"{provider}-api-key")
        self._change_key_in_background(provider, None, self._key_storage())

    def _change_key_in_background(self, provider: str, key: str | None, storage: str) -> None:
        label = get_provider(provider).label

        def change() -> None:
            try:
                if key is None:
                    where = self._keystore.clear(provider, storage).label
                    message = f"{label} API key removed from {where}."
                else:
                    where = self._keystore.store(provider, key, storage).label
                    message = f"{label} API key saved in {where}."
            except KeyStoreError as error:
                message = f"The {label} API key was not changed. {error}"
            GLib.idle_add(self._present_later, message)

        threading.Thread(target=change, name="orcavision-keys", daemon=True).start()

    def _move_legacy_keys(self) -> None:
        """Moves keys an earlier version kept in Orca's settings into the keyring."""

        legacy = {
            name: key for name, _label in key_provider_choices() if (key := self._legacy_key(name))
        }
        if not legacy:
            return
        storage = self._key_storage()

        def move() -> None:
            moved, where, problem = [], "", ""
            for name, key in legacy.items():
                try:
                    where = self._keystore.store(name, key, storage).label
                    moved.append(name)
                except KeyStoreError as error:
                    problem = str(error)
                    break
            GLib.idle_add(self._finish_moving_keys, moved, where, problem)

        threading.Thread(target=move, name="orcavision-keys", daemon=True).start()

    def _finish_moving_keys(self, moved: list[str], where: str, problem: str) -> bool:
        for name in moved:
            self.settings.reset(f"{name}-api-key")
        if moved:
            self.controller.present_message_internal(
                f"OrcaVision moved {len(moved)} API key{'s' if len(moved) > 1 else ''} "
                f"from Orca's settings to {where}."
            )
        if problem:
            self.controller.present_message_internal(
                f"OrcaVision could not move API keys out of Orca's settings. {problem}"
            )
        return False

    def _snapshot_job(self) -> _Job:
        """Reads the settings for a request. Must run on the main thread."""

        def get(key: str, cast: Callable[[Any], Any]) -> Any:
            value = self.settings.get(key, default=DEFAULTS[key])
            try:
                return cast(value)
            except (TypeError, ValueError):
                return DEFAULTS[key]

        name = get("provider", str)
        if name not in provider_names():
            name = DEFAULTS["provider"]
        provider = get_provider(name)
        return _Job(
            provider=name,
            model=get(f"{name}-model", str).strip(),
            host=get(provider.host_setting, str).strip() if provider.host_setting else "",
            key_storage=get("key-storage", str),
            legacy_key=self._legacy_key(name) if provider.uses_api_key else "",
            environment_key=(
                environment_api_key(provider.env_vars) if provider.uses_api_key else ""
            ),
            prompt=get("prompt", str),
            language=get("language", str),
            max_output_tokens=get("max-output-tokens", int),
            temperature=get("temperature", float),
            max_image_size=get("max-image-size", int),
            timeout=max(5, get("request-timeout", int)),
            sound_theme=get("sound-theme", str),
            copy_to_clipboard=get("copy-to-clipboard", bool),
        )

    def _start(self, what: str, grab: Callable[[], Shot]) -> None:
        """Starts a capture on the main thread and finishes it in a worker thread."""

        job = self._snapshot_job()
        try:
            shot = grab()
        except CaptureError as error:
            self._fail(str(error), job.sound_theme)
            return

        # A new request supersedes any request still in progress.
        self._end_request()
        self._generation += 1
        generation = self._active_generation = self._generation

        if is_silent(job.sound_theme):
            self.controller.present_message_internal(f"Describing {what}.")
        else:
            self._play(job.sound_theme, "open")
            self._waiting_source_id = GLib.timeout_add(
                WAITING_INTERVAL_MS, self._on_waiting_tick, job.sound_theme
            )
        self._watchdog_source_id = GLib.timeout_add_seconds(
            job.timeout + _WATCHDOG_GRACE_SECONDS, self._on_watchdog, generation, job.sound_theme
        )
        threading.Thread(
            target=self._run_job,
            args=(generation, job, shot),
            name="orcavision-describe",
            daemon=True,
        ).start()

    def _run_job(self, generation: int, job: _Job, shot: Shot) -> None:
        """Finishes the capture and gets a description. Runs in a worker thread."""

        text, error, moved_key = "", "", False
        try:
            provider = get_provider(job.provider)
            api_key, moved_key = self._api_key(job, provider)
            captured = shot()
            request = DescribeRequest(
                model=job.model,
                prompt=build_prompt(job.prompt, job.language, captured.hint),
                image_png=encode_png(captured.pixbuf, job.max_image_size),
                max_output_tokens=job.max_output_tokens,
                temperature=job.temperature,
                timeout=job.timeout,
                api_key=api_key,
                host=job.host,
            )
            text = provider.describe(request)
        except (DescribeError, CaptureError) as err:
            error = str(err)
        except Exception as err:  # pylint: disable=broad-exception-caught
            error = f"Unexpected error: {err}"
        GLib.idle_add(self._finish, generation, job, text, error, moved_key)

    def _api_key(self, job: _Job, provider: Provider) -> tuple[str, bool]:
        """Returns the API key, and whether a key from Orca's settings was moved.

        Order: a key still in Orca's settings (moved to the keyring on the way),
        the keyring, then the environment. Runs in the worker thread because the
        keyring may block or ask the user to unlock it.
        """

        if not provider.uses_api_key:
            return "", False
        if job.legacy_key:
            try:
                self._keystore.store(job.provider, job.legacy_key, job.key_storage)
            except KeyStoreError:
                return job.legacy_key, False
            return job.legacy_key, True

        problem = ""
        try:
            key = self._keystore.lookup(job.provider, job.key_storage)
        except KeyStoreError as error:
            key, problem = "", str(error)
        if key or job.environment_key:
            return key or job.environment_key, False
        if problem:
            raise DescribeError(f"Could not read the {provider.label} API key. {problem}")
        return "", False

    def _finish(  # pylint: disable=too-many-arguments
        self, generation: int, job: _Job, text: str, error: str, moved_key: bool = False
    ) -> bool:
        """Presents a worker's result on the main thread."""

        if moved_key:
            self.settings.reset(f"{job.provider}-api-key")
        if generation != self._active_generation:
            return False  # Cancelled, timed out or superseded.
        self._end_request()
        if error:
            self._fail(error, job.sound_theme)
            return False

        self._last_description = text
        if job.copy_to_clipboard:
            self.controller.set_clipboard_text_internal(text)
        self._play(job.sound_theme, "done")
        self.controller.present_message_internal(text)
        return False

    def _on_waiting_tick(self, theme: str) -> bool:
        self._play(theme, "waiting")
        return True

    def _on_watchdog(self, generation: int, theme: str) -> bool:
        if generation == self._active_generation:
            self._watchdog_source_id = 0  # Returning False removes this source.
            self._end_request()
            self._fail("The request timed out.", theme)
        return False

    def _end_request(self) -> None:
        """Forgets the request in progress and stops its timers."""

        self._active_generation = None
        for source_id in (self._waiting_source_id, self._watchdog_source_id):
            if source_id:
                GLib.source_remove(source_id)
        self._waiting_source_id = self._watchdog_source_id = 0

    def _fail(self, message: str, theme: str) -> None:
        self._play(theme, "error")
        self.controller.present_message_internal(message)

    def _play(self, theme: str, event: str) -> None:
        tone = tone_for(theme, event)
        if tone is not None:
            self.controller.play_tone_internal(tone.duration, tone.frequency, volume=tone.volume)
