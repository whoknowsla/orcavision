"""A small dialog for storing API keys in the keyring.

The key is typed into a password entry, handed to a callback and cleared; it
never reaches Orca's settings. The dialog runs on Orca's main thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

REMOVE_RESPONSE = 1


class ApiKeyDialog:
    """Asks for a provider and its API key."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        providers: tuple[tuple[str, str], ...],
        current: str,
        storage_label: str,
        on_save: Callable[[str, str], None],
        on_remove: Callable[[str], None],
        on_close: Callable[[], None],
    ) -> None:
        self._on_save = on_save
        self._on_remove = on_remove
        self._on_close = on_close

        self.dialog = Gtk.Dialog(title="OrcaVision API Keys")
        self.dialog.add_button("_Remove Stored Key", REMOVE_RESPONSE)
        self.dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.dialog.add_button("_Save", Gtk.ResponseType.OK)
        self.dialog.set_default_response(Gtk.ResponseType.OK)

        self.provider = Gtk.ComboBoxText()
        for name, label in providers:
            self.provider.append(name, label)
        if not self.provider.set_active_id(current):
            self.provider.set_active(0)
        provider_label = Gtk.Label.new_with_mnemonic("_Provider:")
        provider_label.set_xalign(0)
        provider_label.set_mnemonic_widget(self.provider)

        self.key = Gtk.Entry(
            visibility=False,
            input_purpose=Gtk.InputPurpose.PASSWORD,
            activates_default=True,
            hexpand=True,
        )
        key_label = Gtk.Label.new_with_mnemonic("API _key:")
        key_label.set_xalign(0)
        key_label.set_mnemonic_widget(self.key)

        note = Gtk.Label(
            label=f"Keys are stored in {storage_label}, not in Orca's settings.",
            wrap=True,
            xalign=0,
        )

        grid = Gtk.Grid(row_spacing=6, column_spacing=12, margin=12)
        grid.attach(provider_label, 0, 0, 1, 1)
        grid.attach(self.provider, 1, 0, 1, 1)
        grid.attach(key_label, 0, 1, 1, 1)
        grid.attach(self.key, 1, 1, 1, 1)
        grid.attach(note, 0, 2, 2, 1)
        self.dialog.get_content_area().add(grid)
        self.dialog.connect("response", self._on_response)

    def show(self) -> None:
        """Shows the dialog with the key entry focused."""

        self.dialog.show_all()
        self.key.grab_focus()
        self.dialog.present_with_time(int(time.time()))

    def _on_response(self, dialog: Gtk.Dialog, response: int) -> None:
        provider = self.provider.get_active_id() or ""
        key = self.key.get_text().strip()
        self.key.set_text("")
        dialog.destroy()
        self._on_close()
        if response == Gtk.ResponseType.OK:
            self._on_save(provider, key)
        elif response == REMOVE_RESPONSE:
            self._on_remove(provider)
