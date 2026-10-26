"""Same-user Unix control and authenticated WebUI for the audio bridge."""

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import socket
import struct
from dataclasses import replace

from .config import AudioDevice, Paths, WebSettings, camera_from_dict, validate_id
from .service import Bridge
from .tts import DEFAULT_VOICE
from .web import WebServer


async def call(paths: Paths, command: dict) -> dict:
    try:
        async with asyncio.timeout(15):
            reader, writer = await asyncio.open_unix_connection(
                paths.socket, limit=65536
            )
            try:
                writer.write(json.dumps(command).encode() + b"\n")
                await writer.drain()
                response = json.loads(await reader.readline())
            finally:
                writer.close()
                await writer.wait_closed()
    except (FileNotFoundError, ConnectionRefusedError):
        raise RuntimeError(
            "The service is not running; start 'hikvision-audio serve'"
        ) from None
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Control request failed"))
    return response["status"]


async def dispatch(bridge: Bridge, command: object) -> dict:
    if not isinstance(command, dict):
        raise ValueError("Control request must be an object")
    operation = command.get("op")
    if operation == "status" and set(command) == {"op"}:
        return bridge.status()
    if (
        operation == "camera.save"
        and {"op", "id", "camera"} <= set(command)
        and not set(command) - {"op", "id", "camera", "password"}
    ):
        identifier = validate_id(command["id"])
        camera = camera_from_dict(command["camera"])
        settings = replace(
            bridge.settings,
            cameras={**bridge.settings.cameras, identifier: camera},
        )
        if "password" in command:
            password = command["password"]
            if not isinstance(password, str) or not password:
                raise ValueError("Camera password must be a nonempty string")
            await asyncio.to_thread(bridge.credentials.set, camera, password)
        return await bridge.apply(settings, restart=True, camera_id=identifier)
    if operation == "camera.remove" and set(command) == {"op", "id"}:
        identifier = validate_id(command["id"])
        if identifier not in bridge.settings.cameras:
            raise ValueError("Unknown camera ID")
        if any(
            identifier in device.cameras for device in bridge.settings.devices.values()
        ):
            raise ValueError(
                "Remove the camera from all device routes before deleting it"
            )
        return await bridge.apply(
            replace(
                bridge.settings,
                cameras={
                    key: camera
                    for key, camera in bridge.settings.cameras.items()
                    if key != identifier
                },
            )
        )
    if operation == "device.save" and set(command) == {"op", "id", "device"}:
        identifier = validate_id(command["id"])
        device = AudioDevice.from_dict(command["device"])
        return await bridge.apply(
            replace(
                bridge.settings,
                devices={**bridge.settings.devices, identifier: device},
            )
        )
    if operation == "device.set" and set(command) == {"op", "id", "enabled"}:
        identifier = validate_id(command["id"])
        if identifier not in bridge.settings.devices:
            raise ValueError("Unknown device ID")
        if type(command["enabled"]) is not bool:
            raise ValueError("Device switches must be booleans")
        device = replace(
            bridge.settings.devices[identifier], enabled=command["enabled"]
        )
        return await bridge.apply(
            replace(
                bridge.settings,
                devices={**bridge.settings.devices, identifier: device},
            )
        )
    if operation == "device.remove" and set(command) == {"op", "id"}:
        identifier = validate_id(command["id"])
        if identifier not in bridge.settings.devices:
            raise ValueError("Unknown device ID")
        return await bridge.apply(
            replace(
                bridge.settings,
                devices={
                    key: device
                    for key, device in bridge.settings.devices.items()
                    if key != identifier
                },
            )
        )
    if operation == "reconnect" and set(command) in ({"op"}, {"op", "id"}):
        identifier = validate_id(command["id"]) if "id" in command else None
        if identifier is not None and identifier not in bridge.settings.cameras:
            raise ValueError("Unknown camera ID")
        return await bridge.apply(bridge.settings, restart=True, camera_id=identifier)
    if (
        operation == "tts.speak"
        and {"op", "text", "devices"} <= set(command)
        and not set(command) - {"op", "text", "devices", "voice"}
    ):
        return await bridge.speak(
            command["text"], command["devices"], command.get("voice", DEFAULT_VOICE)
        )
    if operation == "tts.stop" and set(command) == {"op"}:
        return await bridge.stop_speech()
    raise ValueError("Unknown control command or unexpected fields")


@contextlib.contextmanager
def instance_lock(paths: Paths):
    paths.prepare()
    descriptor = os.open(
        paths.runtime / "service.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("An audio bridge service is already running") from None
        yield
    finally:
        os.close(descriptor)


async def cleanup(paths: Paths) -> None:
    # Never unload another still-running instance's devices.
    with instance_lock(paths):
        bridge = Bridge(paths)
        await bridge.devices.recover()
        paths.socket.unlink(missing_ok=True)
        paths.web_access.unlink(missing_ok=True)


async def serve(paths: Paths, web_settings: WebSettings | None = None) -> None:
    with instance_lock(paths):
        paths.socket.unlink(missing_ok=True)
        bridge = Bridge(paths)
        stopping = asyncio.Event()
        handlers: set[asyncio.Task] = set()
        commands = asyncio.Lock()
        settings = paths.load()

        async def execute(command: object) -> dict:
            async with commands:
                return await dispatch(bridge, command)

        web_server = WebServer(paths, web_settings or settings.web, execute)
        server: asyncio.Server | None = None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopping.set)

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            task = asyncio.current_task()
            if task is not None:
                handlers.add(task)
            try:
                peer = writer.get_extra_info("socket")
                _, uid, _ = struct.unpack(
                    "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if uid != os.getuid():
                    raise PermissionError("Control access requires the same user")
                async with asyncio.timeout(15):
                    command = json.loads(await reader.readline())
                    status = await execute(command)
                    response = {"ok": True, "status": status}
            except Exception as error:
                response = {"ok": False, "error": str(error) or type(error).__name__}
            try:
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
                if task is not None:
                    handlers.discard(task)

        try:
            await bridge.devices.recover()
            await bridge.apply(settings, persist=False)
            server = await asyncio.start_unix_server(handle, paths.socket, limit=65536)
            paths.socket.chmod(0o600)
            await web_server.start()
            if address := os.environ.get("NOTIFY_SOCKET"):
                if address.startswith("@"):
                    address = "\0" + address[1:]
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                    notifier.sendto(b"READY=1", address)
            print("Hikvision audio bridge ready", flush=True)
            async with server:
                await stopping.wait()
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()
            await web_server.close()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            try:
                await bridge.close()
            finally:
                paths.socket.unlink(missing_ok=True)
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(sig)
