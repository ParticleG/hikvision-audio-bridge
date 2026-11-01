"""Store the desktop's audio-scoped server token in Secret Service."""

import os

import secretstorage

from .config import APP_ID, BackendSettings


class DesktopCredentials:
    def __init__(self) -> None:
        os.environ.setdefault(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
        )

    @staticmethod
    def _attributes(backend: BackendSettings) -> dict[str, str]:
        return {"application": APP_ID, "server": backend.url, "kind": "audio-access"}

    def get(self, backend: BackendSettings) -> str:
        with secretstorage.dbus_init() as bus:
            collection = secretstorage.get_default_collection(bus)
            if collection.is_locked():
                raise RuntimeError(
                    "The desktop keyring is locked; unlock it and reconnect"
                )
            for item in collection.search_items(self._attributes(backend)):
                if not item.is_locked():
                    return item.get_secret().decode("utf-8")
        raise RuntimeError(
            "No audio access token; configure the backend in the desktop WebUI"
        )

    def set(self, backend: BackendSettings, token: str) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("Audio access token cannot be empty")
        with secretstorage.dbus_init() as bus:
            collection = secretstorage.get_default_collection(bus)
            if collection.is_locked():
                collection.unlock()
            if collection.is_locked():
                raise RuntimeError("The desktop keyring is still locked")
            collection.create_item(
                f"Hikvision audio backend: {backend.url}",
                self._attributes(backend),
                token.encode("utf-8"),
                replace=True,
            )
