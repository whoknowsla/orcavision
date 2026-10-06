"""Test helpers: fake Orca modules and loading the extension the way Orca does."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "orcavision"
MODULE_NAME = "orca_user_extension.orcavision"

# Keep tests away from the user's session bus, so no test can read or change
# the real keyring or pop up prompts. Tests that need D-Bus start a private bus.
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/orcavision-tests"


class FakeKeyBinding:
    def __init__(self, keysymstring, modifiers, click_count=1):
        self.keysymstring = keysymstring
        self.modifiers = modifiers
        self.click_count = click_count


class FakeKeyboardCommand:
    def __init__(self, name, function, group_label, description="",
                 desktop_keybinding=None, laptop_keybinding=None, **_kwargs):
        self.name = name
        self.function = function
        self.group_label = group_label
        self.description = description
        self.desktop_keybinding = desktop_keybinding
        self.laptop_keybinding = laptop_keybinding


class FakePreference:
    def __init__(self, kind, key, label, default=None, *args, **kwargs):
        self.kind = kind
        self.key = key
        self.label = label
        self.default = default
        self.args = args
        self.kwargs = kwargs

    @classmethod
    def info(cls, message):
        return cls("info", "", message)

    @classmethod
    def boolean(cls, key, label, default=False):
        return cls("boolean", key, label, default)

    @classmethod
    def string(cls, key, label, default=""):
        return cls("string", key, label, default)

    @classmethod
    def integer(cls, key, label, default=0, minimum=0, maximum=100):
        return cls("integer", key, label, default, minimum, maximum)

    @classmethod
    def floating(cls, key, label, default=0.0, minimum=0.0, maximum=1.0):
        return cls("float", key, label, default, minimum, maximum)

    @classmethod
    def enum(cls, key, label, options, default):
        return cls("enum", key, label, default, options=options)


class FakeSettings:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        return True

    def reset(self, key):
        self.values.pop(key, None)
        return True


class FakeController:
    def __init__(self):
        self.messages = []
        self.tones = []
        self.clipboard = None
        self.active_window = None
        self.current_object = None

    def present_message_internal(self, message):
        self.messages.append(message)
        return True

    def play_tone_internal(self, duration, frequency, volume=1.0, wave="sine", interrupt=True):
        self.tones.append(frequency)
        return True

    def set_clipboard_text_internal(self, text):
        self.clipboard = text
        return True

    def get_active_window_internal(self):
        return self.active_window

    def get_current_object_internal(self):
        return self.current_object


class FakeExtension:
    GROUP_LABEL = ""

    def __init__(self):
        self.settings = FakeSettings()
        self.controller = FakeController()


def install_fake_orca(setitem):
    """Registers fake orca modules; setitem(name, module) stores each one."""

    orca = types.ModuleType("orca")
    keybindings = types.ModuleType("orca.keybindings")
    keybindings.KeyBinding = FakeKeyBinding
    keybindings.SHIFT_MODIFIER_MASK = 1 << 0
    keybindings.CTRL_MODIFIER_MASK = 1 << 2
    keybindings.ALT_MODIFIER_MASK = 1 << 3
    keybindings.ORCA_MODIFIER_MASK = 1 << 8
    keybindings.ORCA_CTRL_MODIFIER_MASK = (1 << 8) | (1 << 2)
    keybindings.ORCA_CTRL_ALT_MODIFIER_MASK = (1 << 8) | (1 << 2) | (1 << 3)
    command = types.ModuleType("orca.command")
    command.Command = object
    command.KeyboardCommand = FakeKeyboardCommand
    extension = types.ModuleType("orca.extension")
    extension.Extension = FakeExtension
    extension.ExtensionPreference = FakePreference
    orca.keybindings = keybindings
    orca.command = command
    orca.extension = extension
    for name, module in (
        ("orca", orca),
        ("orca.keybindings", keybindings),
        ("orca.command", command),
        ("orca.extension", extension),
    ):
        setitem(name, module)


def load_extension_package():
    """Imports the package under the module name Orca's extension loader uses."""

    for name in [n for n in sys.modules if n == MODULE_NAME or n.startswith(MODULE_NAME + ".")]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ext_module(monkeypatch):
    install_fake_orca(lambda name, module: monkeypatch.setitem(sys.modules, name, module))
    module = load_extension_package()
    yield module
    for name in [n for n in sys.modules if n == MODULE_NAME or n.startswith(MODULE_NAME + ".")]:
        del sys.modules[name]


@pytest.fixture
def submodules(ext_module):
    """The extension's submodules, by short name."""

    return types.SimpleNamespace(
        providers=sys.modules[MODULE_NAME + ".providers"],
        config=sys.modules[MODULE_NAME + ".config"],
        capture=sys.modules[MODULE_NAME + ".capture"],
        sounds=sys.modules[MODULE_NAME + ".sounds"],
        shots=sys.modules[MODULE_NAME + ".shots"],
        wayland=sys.modules[MODULE_NAME + ".wayland"],
        keystore=sys.modules[MODULE_NAME + ".keystore"],
        keydialog=sys.modules[MODULE_NAME + ".keydialog"],
    )


_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir={directory}</listen>
  <servicedir>{directory}/services</servicedir>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@pytest.fixture
def private_bus(tmp_path):
    """Starts an empty session bus and yields its address.

    Services can be made activatable by writing .service files into
    tmp_path / "services" and calling org.freedesktop.DBus.ReloadConfig.
    """

    if shutil.which("dbus-daemon") is None:
        pytest.skip("dbus-daemon is not installed")
    (tmp_path / "services").mkdir()
    config = tmp_path / "bus.conf"
    config.write_text(_BUS_CONFIG.format(directory=tmp_path))
    daemon = subprocess.Popen(
        ["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        yield daemon.stdout.readline().strip()
    finally:
        daemon.terminate()
        daemon.wait(10)
