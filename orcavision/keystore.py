"""API key storage in the user's keyring, never in Orca's settings.

Backends, in the order "Automatic" tries them:

- Secret Service (libsecret): GNOME Keyring, KeePassXC, KDE Wallet with its
  Secret Service support, and other implementations.
- KDE Wallet, through kwalletd's own D-Bus interface, for KDE setups that do
  not provide the Secret Service.
- systemd-creds: a file encrypted with the host's credential key (and the
  TPM when there is one) that only this user on this machine can decrypt.

"Automatic" only picks a storage that is ready: a running keyring with a
usable default collection, or a running KDE Wallet. Starting an idle one could
open a setup wizard or a prompt the user never sees.

Every operation can block on D-Bus, a keyring unlock prompt or a subprocess,
so call them from a worker thread; Secret Service calls give up after a
timeout. available() and present() are quick and may run on the main thread.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable

from gi.repository import Gio, GLib

_KWALLET_TIMEOUT_MS = 120_000  # Opening a wallet may wait for the user's password.
# Long enough to type a keyring password into an unlock prompt.
_SECRET_SERVICE_TIMEOUT_SECONDS = 60
_SYSTEMD_CREDS_TIMEOUT_SECONDS = 30
_SYSTEMD_CREDS_USER_MIN_VERSION = 256

NO_STORAGE = (
    "No working keyring was found. Set up GNOME Keyring, KeePassXC (with Secret "
    "Service integration) or KDE Wallet, or use systemd 256 or later."
)

_KEYRING_STUCK = (
    "The keyring did not respond. It may be waiting for a password prompt that is "
    "not being shown."
)


class KeyStoreError(Exception):
    """A storage failure whose message is suitable for presenting to the user."""


def _bus_call(
    destination: str, path: str, interface: str, method: str, args: GLib.Variant | None
):
    """Makes a short D-Bus call on the session bus and returns the unpacked reply."""

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return bus.call_sync(
        destination, path, interface, method, args, None, Gio.DBusCallFlags.NONE, 2000, None
    ).unpack()


def _name_owned(name: str) -> bool:
    """Returns True if a running program owns name on the session bus."""

    try:
        return bool(
            _bus_call(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "NameHasOwner", GLib.Variant("(s)", (name,)),
            )[0]
        )
    except GLib.Error:
        return False


def _name_activatable(name: str) -> bool:
    """Returns True if the session bus can start a program that owns name."""

    try:
        return name in _bus_call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "ListActivatableNames", None,
        )[0]
    except GLib.Error:
        return False


class Backend:
    """A place to keep API keys."""

    name = ""
    label = ""

    def available(self) -> bool:
        """Returns True if this storage is ready to use; "Automatic" picks only these."""

        raise NotImplementedError

    def present(self) -> bool:
        """Returns True if this storage is installed, so the user may choose it."""

        return self.available()

    def lookup(self, provider: str) -> str:
        """Returns the stored key for provider, or "". Raises KeyStoreError."""

        raise NotImplementedError

    def store(self, provider: str, key: str) -> None:
        """Stores key for provider, replacing any stored key. Raises KeyStoreError."""

        raise NotImplementedError

    def clear(self, provider: str) -> None:
        """Removes the stored key for provider, if any. Raises KeyStoreError."""

        raise NotImplementedError


class SecretServiceBackend(Backend):
    """The freedesktop Secret Service, through libsecret."""

    name = "secret-service"
    label = "the Secret Service keyring"

    def __init__(
        self, collection: str | None = None, timeout: float = _SECRET_SERVICE_TIMEOUT_SECONDS
    ) -> None:
        self._secret = None
        self._schema = None
        self._timeout = timeout
        try:
            import gi  # pylint: disable=import-outside-toplevel

            gi.require_version("Secret", "1")
            from gi.repository import Secret  # pylint: disable=import-outside-toplevel
        except (ImportError, ValueError):
            return
        self._secret = Secret
        self._collection = collection or Secret.COLLECTION_DEFAULT
        self._schema = Secret.Schema.new(
            "org.gnome.Orca.OrcaVision",
            Secret.SchemaFlags.NONE,
            {"provider": Secret.SchemaAttributeType.STRING},
        )

    def present(self) -> bool:
        return self._schema is not None and (
            _name_owned("org.freedesktop.secrets") or _name_activatable("org.freedesktop.secrets")
        )

    def available(self) -> bool:
        """Returns True if a keyring is running and its default collection works.

        Checked without prompting: the default alias must name a collection that
        answers. A missing or broken default collection would make the keyring
        prompt to create or repair one, and that prompt may never be shown.
        """

        if self._schema is None or not _name_owned("org.freedesktop.secrets"):
            return False
        try:
            (path,) = _bus_call(
                "org.freedesktop.secrets", "/org/freedesktop/secrets",
                "org.freedesktop.Secret.Service", "ReadAlias", GLib.Variant("(s)", ("default",)),
            )
            if path == "/":
                return False
            _bus_call(
                "org.freedesktop.secrets", path, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", ("org.freedesktop.Secret.Collection", "Locked")),
            )
        except GLib.Error:
            return False
        return True

    def _run(self, operation: Callable[[Gio.Cancellable], object], failure: str) -> object:
        """Runs a libsecret call, cancelling it if the keyring does not answer in time."""

        cancellable = Gio.Cancellable()
        timer = threading.Timer(self._timeout, cancellable.cancel)
        timer.daemon = True
        timer.start()
        try:
            return operation(cancellable)
        except GLib.Error as error:
            if cancellable.is_cancelled():
                raise KeyStoreError(_KEYRING_STUCK) from error
            raise KeyStoreError(f"{failure}: {error.message}") from error
        finally:
            timer.cancel()

    def lookup(self, provider: str) -> str:
        value = self._run(
            lambda cancellable: self._secret.password_lookup_sync(
                self._schema, {"provider": provider}, cancellable
            ),
            "The keyring could not be read",
        )
        return value or ""

    def store(self, provider: str, key: str) -> None:
        stored = self._run(
            lambda cancellable: self._secret.password_store_sync(
                self._schema,
                {"provider": provider},
                self._collection,
                f"OrcaVision API key for {provider}",
                key,
                cancellable,
            ),
            "The keyring refused the key",
        )
        if not stored:
            raise KeyStoreError("The keyring refused the key.")

    def clear(self, provider: str) -> None:
        self._run(
            lambda cancellable: self._secret.password_clear_sync(
                self._schema, {"provider": provider}, cancellable
            ),
            "The keyring could not be changed",
        )


class KWalletBackend(Backend):
    """KDE Wallet, through kwalletd's D-Bus interface."""

    name = "kwallet"
    label = "KDE Wallet"
    _SERVICES = (
        ("org.kde.kwalletd6", "/modules/kwalletd6"),
        ("org.kde.kwalletd5", "/modules/kwalletd5"),
    )
    _FOLDER = "OrcaVision"
    _APP_ID = "OrcaVision"

    def _service(self) -> tuple[str, str] | None:
        """Returns a running kwalletd, else one the bus can start, else None."""

        for check in (_name_owned, _name_activatable):
            for service, path in self._SERVICES:
                if check(service):
                    return service, path
        return None

    def available(self) -> bool:
        return any(_name_owned(service) for service, _path in self._SERVICES)

    def present(self) -> bool:
        return self._service() is not None

    def _call(self, bus, target, method, signature, args, reply_type):
        service, path = target
        try:
            reply = bus.call_sync(
                service,
                path,
                "org.kde.KWallet",
                method,
                GLib.Variant(signature, args) if signature else None,
                GLib.VariantType.new(reply_type),
                Gio.DBusCallFlags.NONE,
                _KWALLET_TIMEOUT_MS,
                None,
            )
        except GLib.Error as error:
            raise KeyStoreError(f"KDE Wallet did not respond: {error.message}") from error
        return reply.unpack()[0]

    def _with_wallet(self, action: Callable[..., str]) -> str:
        target = self._service()
        if target is None:
            raise KeyStoreError("KDE Wallet is not available.")
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as error:
            raise KeyStoreError("Could not reach the session bus.") from error

        def call(method: str, signature: str, args: tuple, reply_type: str):
            return self._call(bus, target, method, signature, args, reply_type)

        wallet = call("networkWallet", "", (), "(s)")
        handle = call("open", "(sxs)", (wallet, 0, self._APP_ID), "(i)")
        if handle < 0:
            raise KeyStoreError("KDE Wallet could not be opened.")
        try:
            return action(call, handle)
        finally:
            try:
                call("close", "(ibs)", (handle, False, self._APP_ID), "(i)")
            except KeyStoreError:
                pass

    def lookup(self, provider: str) -> str:
        def read(call, handle):
            if not call("hasEntry", "(isss)", (handle, self._FOLDER, provider, self._APP_ID), "(b)"):
                return ""
            return call(
                "readPassword", "(isss)", (handle, self._FOLDER, provider, self._APP_ID), "(s)"
            )

        return self._with_wallet(read)

    def store(self, provider: str, key: str) -> None:
        def write(call, handle):
            args = (handle, self._FOLDER, self._APP_ID)
            if not call("hasFolder", "(iss)", args, "(b)"):
                call("createFolder", "(iss)", args, "(b)")
            result = call(
                "writePassword",
                "(issss)",
                (handle, self._FOLDER, provider, key, self._APP_ID),
                "(i)",
            )
            if result != 0:
                raise KeyStoreError("KDE Wallet refused the key.")
            return ""

        self._with_wallet(write)

    def clear(self, provider: str) -> None:
        def remove(call, handle):
            args = (handle, self._FOLDER, provider, self._APP_ID)
            if call("hasEntry", "(isss)", args, "(b)"):
                call("removeEntry", "(isss)", args, "(i)")
            return ""

        self._with_wallet(remove)


def _default_key_directory() -> str:
    return os.path.join(GLib.get_user_data_dir(), "orca", "orcavision-keys")


class SystemdCredsBackend(Backend):
    """Files encrypted with systemd-creds for the current user."""

    name = "systemd-creds"
    label = "an encrypted file (systemd-creds)"

    def __init__(self, directory: str | None = None) -> None:
        self._directory = directory or _default_key_directory()
        self._supported: bool | None = None

    def available(self) -> bool:
        if self._supported is None:
            self._supported = self._check_version()
        return self._supported

    @staticmethod
    def _check_version() -> bool:
        if shutil.which("systemd-creds") is None:
            return False
        try:
            output = subprocess.run(
                ["systemd-creds", "--version"], capture_output=True, text=True,
                timeout=_SYSTEMD_CREDS_TIMEOUT_SECONDS, check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        match = re.match(r"systemd (\d+)", output)
        return bool(match) and int(match.group(1)) >= _SYSTEMD_CREDS_USER_MIN_VERSION

    def _path(self, provider: str) -> str:
        return os.path.join(self._directory, f"{provider}.cred")

    @staticmethod
    def _run(arguments: list[str], data: bytes = b"") -> bytes:
        try:
            result = subprocess.run(
                ["systemd-creds", "--user", *arguments],
                input=data,
                capture_output=True,
                timeout=_SYSTEMD_CREDS_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise KeyStoreError(f"systemd-creds could not run: {error}") from error
        if result.returncode != 0:
            lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise KeyStoreError(f"systemd-creds failed: {lines[-1] if lines else 'unknown error'}")
        return result.stdout

    def lookup(self, provider: str) -> str:
        path = self._path(provider)
        if not os.path.exists(path):
            return ""
        output = self._run(["decrypt", f"--name=orcavision-{provider}", path, "-"])
        return output.decode("utf-8").strip()

    def store(self, provider: str, key: str) -> None:
        try:
            os.makedirs(self._directory, mode=0o700, exist_ok=True)
            os.chmod(self._directory, 0o700)
        except OSError as error:
            raise KeyStoreError(f"Could not create {self._directory}: {error}") from error
        path = self._path(provider)
        temporary = f"{path}.new"
        try:
            self._run(
                ["encrypt", f"--name=orcavision-{provider}", "-", temporary],
                key.encode("utf-8"),
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except OSError as error:
            raise KeyStoreError(f"Could not save the encrypted key: {error}") from error
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def clear(self, provider: str) -> None:
        try:
            os.unlink(self._path(provider))
        except FileNotFoundError:
            pass
        except OSError as error:
            raise KeyStoreError(f"Could not remove the encrypted key: {error}") from error


AUTOMATIC = "auto"

STORAGE_CHOICES = (
    (AUTOMATIC, "Automatic"),
    (SecretServiceBackend.name, "Secret Service keyring (GNOME Keyring, KeePassXC, KDE Wallet)"),
    (KWalletBackend.name, "KDE Wallet"),
    (SystemdCredsBackend.name, "Encrypted file (systemd-creds)"),
)


class KeyStore:
    """Chooses a storage backend and caches keys read from it for the session."""

    def __init__(self, backends: list[Backend] | None = None) -> None:
        self._backends = backends or [
            SecretServiceBackend(),
            KWalletBackend(),
            SystemdCredsBackend(),
        ]
        self._cache: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def backend(self, choice: str) -> Backend:
        """Returns the backend for choice ("auto" or a backend name)."""

        if choice != AUTOMATIC:
            for backend in self._backends:
                if backend.name == choice:
                    if backend.present():
                        return backend
                    raise KeyStoreError(
                        f"The chosen key storage, {backend.label}, is not available."
                    )
        for backend in self._backends:
            if backend.available():
                return backend
        raise KeyStoreError(NO_STORAGE)

    def lookup(self, provider: str, choice: str) -> str:
        """Returns the stored key for provider, or ""."""

        backend = self.backend(choice)
        with self._lock:
            cached = self._cache.get((backend.name, provider))
        if cached:
            return cached
        value = backend.lookup(provider).strip()
        if value:
            with self._lock:
                self._cache[(backend.name, provider)] = value
        return value

    def store(self, provider: str, key: str, choice: str) -> Backend:
        """Stores key for provider and returns the backend that holds it."""

        backend = self.backend(choice)
        backend.store(provider, key.strip())
        with self._lock:
            self._cache[(backend.name, provider)] = key.strip()
        return backend

    def clear(self, provider: str, choice: str) -> Backend:
        """Removes the stored key for provider and returns the backend it was in."""

        backend = self.backend(choice)
        with self._lock:
            self._cache.pop((backend.name, provider), None)
        backend.clear(provider)
        return backend
