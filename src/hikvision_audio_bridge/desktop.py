"""Local PipeWire devices and grouping, with no camera credentials or ISAPI access."""

import asyncio
import contextlib
from dataclasses import asdict, replace

from .config import (
    BackendSettings,
    CameraGroup,
    DesktopDevice,
    DesktopSettings,
    Paths,
    validate_id,
)
from .credentials import DesktopCredentials
from .pipewire import AudioDevices, device_name
from .remote import RemoteAudio


class DesktopBridge:
    def __init__(self, paths: Paths, credentials: DesktopCredentials | None = None):
        self.paths = paths
        self.credentials = credentials or DesktopCredentials()
        self.devices = AudioDevices(paths.runtime)
        self.settings = DesktopSettings()
        self.remote: RemoteAudio | None = None
        self.remote_cleanup: asyncio.Task | None = None
        self.connection = "idle"
        self.error: str | None = None
        self.catalog: dict = {}
        self.lock = asyncio.Lock()
        self.commands = asyncio.Lock()
        self.close_task: asyncio.Task | None = None
        self.closing = False

    def status(self) -> dict:
        catalog = self.remote.catalog if self.remote is not None else self.catalog
        return {
            "component": "desktop",
            "settings": asdict(self.settings),
            "backend": {
                "connection": self.connection,
                "error": self.error,
                "session_id": self.remote.session_id if self.remote else None,
            },
            "cameras": catalog,
            "devices": {
                identifier: {
                    "name": device_name(identifier),
                    "present": identifier in self.devices.devices,
                    "cameras": list(route.cameras),
                }
                for identifier, route in self.settings.routes().items()
            },
        }

    def _failed(self, remote: RemoteAudio, message: str) -> None:
        if self.remote is not remote or self.closing:
            return
        self.connection = "error"
        self.error = message
        with contextlib.suppress(OSError, KeyError):
            self.devices.flush()

    async def _stop_remote(self) -> None:
        if self.remote is not None:
            remote, self.remote = self.remote, None
            self.catalog = remote.catalog
            self.remote_cleanup = asyncio.create_task(remote.close())
        if self.remote_cleanup is not None:
            await asyncio.shield(self.remote_cleanup)
            self.remote_cleanup = None
        self.devices.flush()

    async def _connect(self) -> None:
        backend = self.settings.backend
        if backend is None:
            return
        self.connection = "connecting"
        self.error = None
        token = None
        try:
            token = await asyncio.to_thread(self.credentials.get, backend)
            remote = RemoteAudio(
                backend,
                token,
                self.devices.read_speaker,
                self.devices.write_microphone,
                self.devices.flush,
                lambda message: self._failed(remote, message),
            )
            self.remote = remote
            await remote.start()
            await remote.set_routes(self.settings.routes())
            self.connection = "connected"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = str(error) or type(error).__name__
            if token:
                message = message.replace(token, "[redacted]")
            self.connection = "error"
            self.error = message[:1000]
            await self._stop_remote()

    async def apply(
        self,
        settings: DesktopSettings,
        *,
        persist: bool = True,
        reconnect: bool = False,
    ) -> dict:
        async with self.lock:
            if self.closing:
                raise RuntimeError("Desktop audio service is stopping")
            previous = self.settings
            backend_changed = previous.backend != settings.backend
            if (
                self.remote is not None
                and self.connection == "connected"
                and not backend_changed
            ):
                unknown = {
                    camera
                    for route in settings.routes().values()
                    if route.enabled
                    for camera in route.cameras
                } - self.remote.catalog.keys()
                if unknown:
                    raise ValueError(
                        "Device groups reference unknown backend cameras: "
                        + ", ".join(sorted(unknown))
                    )
            if persist:
                self.paths.save(settings)
            self.settings = settings
            try:
                if backend_changed or reconnect or settings.backend is None:
                    await self._stop_remote()
                    self.connection = "idle"
                    self.error = None
                    if backend_changed:
                        self.catalog = {}
                elif self.remote_cleanup is not None:
                    await asyncio.shield(self.remote_cleanup)
                    self.remote_cleanup = None
                if self.remote is not None:
                    changed = {
                        identifier
                        for identifier in previous.devices.keys()
                        | settings.devices.keys()
                        if previous.devices.get(identifier)
                        != settings.devices.get(identifier)
                    }
                    self.remote.pause(changed)
                await self.devices.settle()
                operations = [
                    self.devices.enable(identifier, device)
                    for identifier, device in settings.devices.items()
                    if device.enabled
                ]
                operations.extend(
                    self.devices.disable(identifier)
                    for identifier in list(self.devices.devices)
                    if identifier not in settings.devices
                    or not settings.devices[identifier].enabled
                )
                if operations:
                    await asyncio.gather(*operations)
                if settings.backend is not None and self.connection == "idle":
                    await self._connect()
                elif self.remote is not None and self.connection == "connected":
                    try:
                        await self.remote.set_routes(settings.routes())
                    except Exception as error:
                        self.connection = "error"
                        self.error = str(error) or type(error).__name__
                        await self._stop_remote()
                return self.status()
            except asyncio.CancelledError:
                self.connection = "error"
                self.error = "Configuration was interrupted; reconnect explicitly"
                await self._stop_remote()
                raise

    async def dispatch(self, command: object) -> dict:
        if not isinstance(command, dict):
            raise ValueError("Control request must be an object")
        operation = command.get("op")
        if operation == "status" and set(command) == {"op"}:
            return self.status()
        async with self.commands:
            if (
                operation == "backend.save"
                and {"op", "backend"} <= set(command)
                and not set(command) - {"op", "backend", "token"}
            ):
                backend = BackendSettings.from_dict(command["backend"])
                if "token" in command:
                    token = command["token"]
                    if not isinstance(token, str) or not token:
                        raise ValueError("Audio access token cannot be empty")
                    await asyncio.to_thread(self.credentials.set, backend, token)
                return await self.apply(
                    replace(self.settings, backend=backend), reconnect=True
                )
            if operation == "backend.remove" and set(command) == {"op"}:
                return await self.apply(replace(self.settings, backend=None))
            if operation == "reconnect" and set(command) == {"op"}:
                return await self.apply(self.settings, reconnect=True)
            if operation == "group.save" and set(command) == {"op", "id", "group"}:
                identifier = validate_id(command["id"])
                group = CameraGroup.from_dict(command["group"])
                return await self.apply(
                    replace(
                        self.settings,
                        groups={**self.settings.groups, identifier: group},
                    )
                )
            if operation == "group.remove" and set(command) == {"op", "id"}:
                identifier = validate_id(command["id"])
                if identifier not in self.settings.groups:
                    raise ValueError("Unknown camera group")
                return await self.apply(
                    replace(
                        self.settings,
                        groups={
                            key: value
                            for key, value in self.settings.groups.items()
                            if key != identifier
                        },
                    )
                )
            if operation == "device.save" and set(command) == {"op", "id", "device"}:
                identifier = validate_id(command["id"])
                device = DesktopDevice.from_dict(command["device"])
                return await self.apply(
                    replace(
                        self.settings,
                        devices={**self.settings.devices, identifier: device},
                    )
                )
            if operation == "device.set" and set(command) == {"op", "id", "enabled"}:
                identifier = validate_id(command["id"])
                if identifier not in self.settings.devices:
                    raise ValueError("Unknown device ID")
                if type(command["enabled"]) is not bool:
                    raise ValueError("Device switches must be booleans")
                device = replace(
                    self.settings.devices[identifier], enabled=command["enabled"]
                )
                return await self.apply(
                    replace(
                        self.settings,
                        devices={**self.settings.devices, identifier: device},
                    )
                )
            if operation == "device.remove" and set(command) == {"op", "id"}:
                identifier = validate_id(command["id"])
                if identifier not in self.settings.devices:
                    raise ValueError("Unknown device ID")
                return await self.apply(
                    replace(
                        self.settings,
                        devices={
                            key: value
                            for key, value in self.settings.devices.items()
                            if key != identifier
                        },
                    )
                )
            raise ValueError("Unknown control command or unexpected fields")

    async def close(self) -> None:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self) -> None:
        self.closing = True
        async with self.lock:
            await self._stop_remote()
            await self.devices.close()
            self.connection = "idle"
