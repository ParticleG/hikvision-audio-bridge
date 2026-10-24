"""Store camera passwords only in the desktop Secret Service."""

import os

import secretstorage

from .config import APP_ID, Camera


class Credentials:
    def __init__(self) -> None:
        # systemd user buses exist even in shells without the desktop bus variable.
        os.environ.setdefault(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
        )

    def get(self, camera: Camera) -> str:
        with secretstorage.dbus_init() as bus:
            collection = secretstorage.get_default_collection(bus)
            if collection.is_locked():
                raise RuntimeError(
                    "The desktop keyring is locked; unlock it and reconnect"
                )
            matches = collection.search_items(
                {
                    "application": APP_ID,
                    "server": camera.url,
                    "username": camera.username,
                }
            )
            for item in matches:
                if not item.is_locked():
                    return item.get_secret().decode("utf-8")
        raise RuntimeError(
            "No camera password in the keyring; run 'hikvision-audio configure'"
        )

    def set(self, camera: Camera, password: str) -> None:
        if not password:
            raise ValueError("Camera password cannot be empty")
        with secretstorage.dbus_init() as bus:
            collection = secretstorage.get_default_collection(bus)
            if collection.is_locked():
                collection.unlock()
            if collection.is_locked():
                raise RuntimeError("The desktop keyring is still locked")
            collection.create_item(
                f"Hikvision audio: {camera.username}@{camera.url}",
                {
                    "application": APP_ID,
                    "server": camera.url,
                    "username": camera.username,
                },
                password.encode("utf-8"),
                replace=True,
            )
