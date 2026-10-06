import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent


@pytest.fixture
def ks(submodules):
    return submodules.keystore


class FakeBackend:
    def __init__(self, error, name, available=True, present=True):
        self.error = error
        self.name = name
        self.label = f"the {name} store"
        self._available = available
        self._present = present
        self.keys = {}
        self.lookups = 0
        self.checks = []
        self.fail = False

    def available(self):
        return self._available

    def present(self):
        return self._present

    def lookup(self, provider):
        self.lookups += 1
        if self.fail:
            raise self.error("locked")
        return self.keys.get(provider, "")

    def has_key(self, provider):
        self.checks.append(provider)
        if self.fail:
            raise self.error("locked")
        return provider in self.keys

    def store(self, provider, key):
        if self.fail:
            raise self.error("refused")
        self.keys[provider] = key

    def clear(self, provider):
        self.keys.pop(provider, None)


@pytest.fixture
def fake(ks):
    return lambda *args, **kwargs: FakeBackend(ks.KeyStoreError, *args, **kwargs)


def test_automatic_choice_uses_first_ready_backend(ks, fake):
    first, second = fake("secret-service", available=False), fake("kwallet")
    store = ks.KeyStore([first, second])
    assert store.backend("auto") is second
    assert store.store("openai", " sk-1 ", "auto") is second
    assert second.keys == {"openai": "sk-1"}


def test_explicit_choice_only_needs_the_backend_to_be_present(ks, fake):
    secret = fake("secret-service", available=False)
    wallet = fake("kwallet", available=False, present=False)
    store = ks.KeyStore([secret, wallet])
    assert store.backend("secret-service") is secret
    with pytest.raises(ks.KeyStoreError, match="The chosen key storage, the kwallet store, "
                                               "is not available."):
        store.backend("kwallet")


def test_no_storage(ks, fake):
    with pytest.raises(ks.KeyStoreError, match="No working keyring was found"):
        ks.KeyStore([fake("secret-service", available=False)]).backend("auto")


def test_lookup_caches_found_keys_only(ks, fake):
    backend = fake("secret-service")
    store = ks.KeyStore([backend])
    assert store.lookup("openai", "auto") == ""
    assert store.lookup("openai", "auto") == ""
    assert backend.lookups == 2

    backend.keys["openai"] = "sk-1"
    assert store.lookup("openai", "auto") == "sk-1"
    assert store.lookup("openai", "auto") == "sk-1"
    assert backend.lookups == 3

    store.clear("openai", "auto")
    assert store.lookup("openai", "auto") == ""
    store.store("openai", "sk-2", "auto")
    assert store.lookup("openai", "auto") == "sk-2"


def test_lookup_errors_propagate(ks, fake):
    backend = fake("secret-service")
    backend.fail = True
    with pytest.raises(ks.KeyStoreError, match="locked"):
        ks.KeyStore([backend]).lookup("openai", "auto")


def test_storage_choices_match_backends(ks):
    names = [name for name, _label in ks.STORAGE_CHOICES]
    assert names == ["auto", "secret-service", "kwallet", "systemd-creds"]


@pytest.mark.parametrize("output,expected", [
    ("systemd 261 (261.3-1-arch)\n+PAM", True),
    ("systemd 256 (256)", True),
    ("systemd 255 (255.4)", False),
    ("something else", False),
])
def test_systemd_creds_version_check(ks, monkeypatch, output, expected):
    monkeypatch.setattr(ks.shutil, "which", lambda tool: "/usr/bin/systemd-creds")
    monkeypatch.setattr(ks.subprocess, "run",
                        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, output, ""))
    assert ks.SystemdCredsBackend._check_version() is expected


def test_systemd_creds_round_trip(ks, tmp_path):
    backend = ks.SystemdCredsBackend(str(tmp_path / "keys"))
    if not backend.available():
        pytest.skip("systemd-creds --user needs systemd 256 or later")
    assert backend.present()

    assert backend.lookup("openai") == ""
    assert not backend.has_key("openai")
    backend.store("openai", "sk-secret-value")
    assert backend.has_key("openai")
    path = tmp_path / "keys" / "openai.cred"
    assert stat.S_IMODE(os.stat(tmp_path / "keys").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert b"sk-secret-value" not in path.read_bytes()
    assert backend.lookup("openai") == "sk-secret-value"

    backend.store("openai", "sk-replaced")
    assert backend.lookup("openai") == "sk-replaced"
    assert sorted(p.name for p in (tmp_path / "keys").iterdir()) == ["openai.cred"]

    # The credential is bound to its name: copying it to another provider fails.
    shutil.copy(path, tmp_path / "keys" / "gemini.cred")
    with pytest.raises(ks.KeyStoreError, match="systemd-creds failed"):
        backend.lookup("gemini")

    backend.clear("openai")
    backend.clear("openai")
    assert backend.lookup("openai") == ""


# Shared start of the scripts below, which run in a subprocess on a private bus.
PREAMBLE = textwrap.dedent("""
    import os, sys, threading, time
    sys.path.insert(0, sys.argv[1])
    import conftest
    conftest.install_fake_orca(sys.modules.__setitem__)
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = sys.argv[2]
    conftest.load_extension_package()
    ks = sys.modules["orca_user_extension.orcavision.keystore"]
    from gi.repository import Gio, GLib

    def wait_for(check, seconds=10):
        deadline = time.monotonic() + seconds
        while not check() and time.monotonic() < deadline:
            time.sleep(0.1)
        return check()

    def serve(bus_name, path, xml, handler):
        '''Exports a fake D-Bus service from a thread; handler(method, args, invocation).'''
        ready = threading.Event()

        def run():
            context = GLib.MainContext.new()
            context.push_thread_default()
            flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                     | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
            bus = Gio.DBusConnection.new_for_address_sync(sys.argv[2], flags, None, None)
            node = Gio.DBusNodeInfo.new_for_xml(xml)
            bus.register_object(
                path, node.interfaces[0],
                lambda conn, sender, obj, iface, method, params, invocation:
                    handler(method, params.unpack(), invocation))
            bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "RequestName", GLib.Variant("(su)", (bus_name, 0)),
                          None, Gio.DBusCallFlags.NONE, -1, None)
            ready.set()
            GLib.MainLoop.new(context, False).run()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)
""")


def run_script(body, private_bus, *args, env=None):
    result = subprocess.run(
        [sys.executable, "-c", PREAMBLE + textwrap.dedent(body), str(TESTS_DIR), private_bus,
         *map(str, args)],
        capture_output=True, text=True, timeout=90, check=False,
        env={**os.environ, **(env or {})},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.split("\n")[:-1]


@pytest.fixture
def gnome_keyring(private_bus, tmp_path):
    """Starts a throwaway gnome-keyring with its own home; the user's keyrings are untouched."""

    if shutil.which("gnome-keyring-daemon") is None:
        pytest.skip("gnome-keyring-daemon is not installed")
    daemons = []

    def start(broken=False, restart=False):
        """Starts the daemon; restart=True stops it and starts it again, locked."""

        home, runtime = tmp_path / "home", tmp_path / "run"
        keyrings = home / "data" / "keyrings"
        if restart:
            for daemon in daemons:
                daemon.terminate()
                daemon.wait(10)
        else:
            keyrings.mkdir(parents=True)
            runtime.mkdir(mode=0o700)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "XDG_RUNTIME_DIR": str(runtime),
               "XDG_DATA_HOME": str(home / "data"), "DBUS_SESSION_BUS_ADDRESS": private_bus}
        command = ["gnome-keyring-daemon", "--foreground", "--components=secrets",
                   f"--control-directory={runtime}"]
        unlock = not broken and not restart
        if broken:
            # An unreadable login keyring leaves no usable default collection.
            (keyrings / "login.keyring").write_bytes(os.urandom(105))
        elif unlock:
            command.append("--unlock")  # Creates an unlocked login keyring from stdin.
        daemon = subprocess.Popen(command, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        daemon.stdin.write(b"test-password" if unlock else b"")
        daemon.stdin.close()
        daemons.append(daemon)
        return env

    yield start
    for daemon in daemons:
        daemon.terminate()
        daemon.wait(10)


def test_secret_service_with_healthy_login_keyring(gnome_keyring, private_bus):
    env = gnome_keyring()
    lines = run_script("""
        backend = ks.SecretServiceBackend()
        print("available", wait_for(backend.available))
        store = ks.KeyStore([backend, ks.KWalletBackend()])
        print("auto", store.backend("auto").name)
        store.store("openai", "sk-one", "auto")
        print("lookup", backend.lookup("openai"))
        backend.store("openai", "sk-two")
        print("replaced", backend.lookup("openai"))
        print("other", repr(backend.lookup("gemini")))
        print("has", backend.has_key("openai"), backend.has_key("gemini"))
        backend.clear("openai")
        print("cleared", repr(backend.lookup("openai")), backend.has_key("openai"))
    """, private_bus, env=env)
    assert lines == [
        "available True",
        "auto secret-service",
        "lookup sk-one",
        "replaced sk-two",
        "other ''",
        "has True False",
        "cleared '' False",
    ]


def test_broken_secret_service_is_skipped_automatically(gnome_keyring, private_bus, tmp_path):
    env = gnome_keyring(broken=True)
    lines = run_script("""
        backend = ks.SecretServiceBackend()
        print("present", wait_for(backend.present), "available", backend.available())
        store = ks.KeyStore([backend, ks.KWalletBackend(), ks.SystemdCredsBackend(sys.argv[3])])
        try:
            print("auto", store.backend("auto").name)
        except ks.KeyStoreError as error:
            print("auto none")
        print("explicit", store.backend("secret-service").name)
    """, private_bus, tmp_path / "keys", env=env)
    assert lines[0] == "present True available False"
    assert lines[1] in ("auto systemd-creds", "auto none")
    assert lines[2] == "explicit secret-service"


def test_secret_service_that_never_answers_times_out(private_bus):
    lines = run_script("""
        XML = '''<node><interface name="org.freedesktop.Secret.Service">
          <method name="OpenSession"><arg type="s" direction="in"/><arg type="v" direction="in"/>
            <arg type="v" direction="out"/><arg type="o" direction="out"/></method>
          <method name="SearchItems"><arg type="a{ss}" direction="in"/>
            <arg type="ao" direction="out"/><arg type="ao" direction="out"/></method>
          <method name="ReadAlias"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
        </interface></node>'''
        pending = []  # Like a keyring stuck behind a prompt nobody sees: never reply.
        serve("org.freedesktop.secrets", "/org/freedesktop/secrets", XML,
              lambda method, args, invocation: pending.append(invocation))

        backend = ks.SecretServiceBackend(timeout=1)
        print("available", backend.available())
        started = time.monotonic()
        for name, operation in (("store", lambda: backend.store("openai", "sk")),
                                ("lookup", lambda: backend.lookup("openai")),
                                ("clear", lambda: backend.clear("openai"))):
            try:
                operation()
                print(name, "returned")
            except ks.KeyStoreError as error:
                print(name, error)
        print("seconds under 20", time.monotonic() - started < 20)
    """, private_bus)
    stuck = ("The keyring did not respond. It may be waiting for a password prompt "
             "that is not being shown.")
    assert lines == [
        "available False",
        f"store {stuck}",
        f"lookup {stuck}",
        f"clear {stuck}",
        "seconds under 20 True",
    ]


# Signatures copied from kf6_org.kde.KWallet.xml.
KWALLET_XML = """<node><interface name="org.kde.KWallet">
  <method name="networkWallet"><arg type="s" direction="out"/></method>
  <method name="open"><arg type="i" direction="out"/><arg name="wallet" type="s" direction="in"/>
    <arg name="wId" type="x" direction="in"/><arg name="appid" type="s" direction="in"/></method>
  <method name="close"><arg type="i" direction="out"/><arg name="handle" type="i" direction="in"/>
    <arg name="force" type="b" direction="in"/><arg name="appid" type="s" direction="in"/></method>
  <method name="hasFolder"><arg type="b" direction="out"/><arg name="handle" type="i" direction="in"/>
    <arg name="folder" type="s" direction="in"/><arg name="appid" type="s" direction="in"/></method>
  <method name="createFolder"><arg type="b" direction="out"/><arg name="handle" type="i" direction="in"/>
    <arg name="folder" type="s" direction="in"/><arg name="appid" type="s" direction="in"/></method>
  <method name="hasEntry"><arg type="b" direction="out"/><arg name="handle" type="i" direction="in"/>
    <arg name="folder" type="s" direction="in"/><arg name="key" type="s" direction="in"/>
    <arg name="appid" type="s" direction="in"/></method>
  <method name="readPassword"><arg type="s" direction="out"/><arg name="handle" type="i" direction="in"/>
    <arg name="folder" type="s" direction="in"/><arg name="key" type="s" direction="in"/>
    <arg name="appid" type="s" direction="in"/></method>
  <method name="writePassword"><arg type="i" direction="out"/><arg name="handle" type="i" direction="in"/>
    <arg name="folder" type="s" direction="in"/><arg name="key" type="s" direction="in"/>
    <arg name="value" type="s" direction="in"/><arg name="appid" type="s" direction="in"/></method>
  <method name="removeEntry"><arg type="i" direction="out"/><arg name="handle" type="i" direction="in"/>
    <arg name="folder" type="s" direction="in"/><arg name="key" type="s" direction="in"/>
    <arg name="appid" type="s" direction="in"/></method>
</interface></node>"""


def test_kwallet_against_fake_kwalletd(private_bus):
    lines = run_script("""
        folders, calls, open_handles = {}, [], set()

        def handle_call(method, args):
            calls.append(method)
            if method == "networkWallet":
                return "(s)", ("kdewallet",)
            if method == "open":
                assert args == ("kdewallet", 0, "OrcaVision"), args
                open_handles.add(7)
                return "(i)", (7,)
            handle = args[0]
            assert handle in open_handles, (method, args)
            if method == "close":
                open_handles.discard(handle)
                return "(i)", (0,)
            folder = args[1]
            assert folder == "OrcaVision" and args[-1] == "OrcaVision", args
            if method == "hasFolder":
                return "(b)", (folder in folders,)
            if method == "createFolder":
                folders.setdefault(folder, {})
                return "(b)", (True,)
            entries = folders.get(folder, {})
            if method == "hasEntry":
                return "(b)", (args[2] in entries,)
            if method == "readPassword":
                return "(s)", (entries.get(args[2], ""),)
            if method == "writePassword":
                folders[folder][args[2]] = args[3]
                return "(i)", (0,)
            if method == "removeEntry":
                entries.pop(args[2], None)
                return "(i)", (0,)
            raise AssertionError(method)

        def reply(method, args, invocation):
            signature, values = handle_call(method, args)
            invocation.return_value(GLib.Variant(signature, values))

        serve("org.kde.kwalletd6", "/modules/kwalletd6", sys.argv[3], reply)

        store = ks.KeyStore()
        backend = store.backend("auto")
        print("auto", backend.name)
        print("missing", repr(backend.lookup("openai")))
        store.store("openai", "sk-wallet", "auto")
        print("lookup", backend.lookup("openai"))
        print("folders", folders)
        print("has", backend.has_key("openai"), backend.has_key("gemini"))
        store.clear("openai", "auto")
        print("cleared", repr(backend.lookup("openai")))
        print("handles left open", len(open_handles))
        print("calls", ",".join(dict.fromkeys(calls)))
    """, private_bus, KWALLET_XML, env={"PATH": "/nonexistent"})  # hides systemd-creds
    assert lines == [
        "auto kwallet",
        "missing ''",
        "lookup sk-wallet",
        "folders {'OrcaVision': {'openai': 'sk-wallet'}}",
        "has True False",
        "cleared ''",
        "handles left open 0",
        "calls networkWallet,open,hasEntry,close,hasFolder,createFolder,writePassword,"
        "readPassword,removeEntry",
    ]


def test_kwallet_that_is_not_running_is_not_chosen_automatically(private_bus, tmp_path):
    # Installed but not started: starting it could open KDE Wallet's setup wizard.
    (tmp_path / "services" / "org.kde.kwalletd6.service").write_text(
        "[D-BUS Service]\nName=org.kde.kwalletd6\nExec=/bin/false\n")
    lines = run_script("""
        Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "ReloadConfig", None, None, Gio.DBusCallFlags.NONE, -1, None)
        wallet = ks.KWalletBackend()
        print("present", wallet.present(), "available", wallet.available())
        print("dbus owned", ks._name_owned("org.freedesktop.DBus"),
              "secrets", ks._name_owned("org.freedesktop.secrets"),
              ks._name_activatable("org.freedesktop.secrets"))
    """, private_bus)
    assert lines == ["present True available False", "dbus owned True secrets False False"]


def test_stored_uses_cache_and_checks_without_reading(ks, fake):
    backend = fake("secret-service")
    backend.keys = {"openai": "sk-1", "gemini": "g"}
    store = ks.KeyStore([backend])
    assert store.lookup("openai", "auto") == "sk-1"  # Now cached.

    assert store.stored(["openai", "anthropic", "gemini"], "auto") == {"openai", "gemini"}
    assert backend.checks == ["anthropic", "gemini"]
    assert backend.lookups == 1


def test_stored_errors_propagate(ks, fake):
    backend = fake("secret-service")
    backend.fail = True
    with pytest.raises(ks.KeyStoreError, match="locked"):
        ks.KeyStore([backend]).stored(["openai"], "auto")


def test_locked_keyring_is_checked_without_a_prompt(gnome_keyring, private_bus):
    """A locked keyring still shows which keys exist, without asking for a password.

    The private bus has no prompter, so anything that tried to unlock would time out.
    """

    env = gnome_keyring()
    run_script("""
        backend = ks.SecretServiceBackend()
        wait_for(backend.available)
        backend.store("openai", "sk-locked")
    """, private_bus, env=env)

    env = gnome_keyring(restart=True)  # Same keyring file, now locked.
    lines = run_script("""
        backend = ks.SecretServiceBackend(timeout=5)
        print("available", wait_for(backend.available))
        started = time.monotonic()
        print("has", backend.has_key("openai"), backend.has_key("gemini"))
        print("quick", time.monotonic() - started < 4)
    """, private_bus, env=env)
    assert lines == ["available True", "has True False", "quick True"]


def test_entry_from_earlier_version_is_read_and_rewritten(gnome_keyring, private_bus):
    env = gnome_keyring()
    lines = run_script("""
        from gi.repository import Secret
        legacy = Secret.Schema.new("org.gnome.Orca.OrcaVision", Secret.SchemaFlags.NONE,
                                   {"provider": Secret.SchemaAttributeType.STRING})
        backend = ks.SecretServiceBackend()
        wait_for(backend.available)
        Secret.password_store_sync(legacy, {"provider": "openai"}, Secret.COLLECTION_DEFAULT,
                                   "OrcaVision API key for openai", "sk-old", None)
        print("has", backend.has_key("openai"), backend.has_key("gemini"))
        print("lookup", backend.lookup("openai"))
        print("old form left", Secret.password_lookup_sync(legacy, {"provider": "openai"}, None))
    """, private_bus, env=env)
    assert lines == ["has True False", "lookup sk-old", "old form left None"]

    env = gnome_keyring(restart=True)  # Locked: only the rewritten entry can be found now.
    lines = run_script("""
        backend = ks.SecretServiceBackend(timeout=5)
        wait_for(backend.available)
        print("has", backend.has_key("openai"))
    """, private_bus, env=env)
    assert lines == ["has True"]
