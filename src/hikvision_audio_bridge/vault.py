"""Private authenticated encryption for an unattended camera server.

The service account and host/container root are trusted: the decryption key lives
beside the ciphertext with owner-only permissions. Nothing is written in plaintext
except that random key; camera passwords and access tokens stay encrypted at rest.
"""

import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

from .config import Camera, write_private


class CameraCredentials(Protocol):
    def get(self, camera: Camera) -> str: ...


def _private_bytes(path: Path) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise RuntimeError("Credential files must be regular owner-only files")
    return path.read_bytes()


class ServerSecrets:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("Credential directory must be owned by the service user")
        directory.chmod(0o700)
        key_path = directory / "key"
        if not key_path.exists():
            if any(directory.glob("*.enc")):
                raise RuntimeError(
                    "Credential key is missing; restore it before starting"
                )
            descriptor = os.open(
                key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(Fernet.generate_key())
        self._fernet = Fernet(_private_bytes(key_path))

    def _path(self, identifier: str) -> Path:
        digest = hashlib.sha256(identifier.encode()).hexdigest()
        return self.directory / f"{digest}.enc"

    def _get(self, identifier: str) -> str:
        try:
            data = _private_bytes(self._path(identifier))
        except FileNotFoundError:
            raise RuntimeError(
                "No stored credential; configure it in the server WebUI"
            ) from None
        try:
            return self._fernet.decrypt(data).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError):
            raise RuntimeError("Stored credential cannot be decrypted") from None

    def _set(self, identifier: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("Credential must be a nonempty string")
        write_private(self._path(identifier), self._fernet.encrypt(value.encode()))

    @staticmethod
    def _camera_key(camera: Camera) -> str:
        return f"camera\0{camera.url}\0{camera.username}"

    def get(self, camera: Camera) -> str:
        return self._get(self._camera_key(camera))

    def set(self, camera: Camera, password: str) -> None:
        self._set(self._camera_key(camera), password)

    def token(self, scope: str) -> str:
        if scope not in ("admin", "audio"):
            raise ValueError("Unknown access scope")
        identifier = f"access\0{scope}"
        if not self._path(identifier).exists():
            self._set(identifier, secrets.token_urlsafe(32))
        return self._get(identifier)
