"""Checks the extension against the Orca installed on this machine, if any.

Runs in a subprocess so the real orca package does not mix with the fakes in
conftest.py. Builds the preferences and commands with Orca's real classes and
reads the metadata the way Orca's User Extensions page does.
"""

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "orcavision"

SCRIPT = textwrap.dedent("""
    import importlib.util, sys
    import orca.command_manager  # import first to avoid a circular import in orca.extension
    from orca import keybindings
    from orca.command import KeyboardCommand
    from orca.extension import ExtensionPreference
    from orca.extension_loader import ExtensionLoader

    package_dir = sys.argv[1]
    metadata = ExtensionLoader.get_metadata(package_dir)
    assert ExtensionLoader.get_class_name(package_dir) == "OrcaVision", metadata
    print("metadata", metadata)

    name = "orca_user_extension.orcavision"
    spec = importlib.util.spec_from_file_location(
        name, package_dir + "/__init__.py", submodule_search_locations=[package_dir])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    # Skip Extension.__init__, which registers with a running Orca's D-Bus service.
    extension = object.__new__(module.OrcaVision)
    preferences = extension.get_preferences()
    assert all(isinstance(p, ExtensionPreference) for p in preferences)
    assert not [p for p in preferences if p.key.endswith("-api-key")]
    commands = extension._get_commands()
    assert all(isinstance(c, KeyboardCommand) for c in commands)
    bound = {c.get_name(): c.get_default_keybinding(True) for c in commands}
    assert all(c.get_default_keybinding(False) is not c.get_default_keybinding(True)
               for c in commands if c.has_default_keybinding())
    assert bound["orcavision_describe_window"].modifiers == keybindings.ORCA_CTRL_MODIFIER_MASK
    assert bound["orcavision_describe_desktop"].modifiers == (
        keybindings.ORCA_CTRL_MODIFIER_MASK | keybindings.SHIFT_MODIFIER_MASK)
    assert bound["orcavision_cancel"] is None
    assert bound["orcavision_manage_keys"].keysymstring == "k"
    assert bound["orcavision_manage_keys"].modifiers == keybindings.ORCA_CTRL_ALT_MODIFIER_MASK
    print("ok", len(preferences), len(commands))
""")


def test_against_installed_orca():
    if importlib.util.find_spec("orca") is None:
        pytest.skip("Orca is not installed")
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(PACKAGE_DIR)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "ok 18 7" in result.stdout, result.stdout
