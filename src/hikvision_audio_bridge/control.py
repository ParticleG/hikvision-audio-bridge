"""Private Unix-socket control plane; no TCP listener or credential transport."""

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import socket
import struct
from dataclasses import asdict, replace

from .config import Paths, Settings
from .service import Bridge


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
    if operation == "configure" and set(command) == {"op", "camera"}:
        settings = Settings.from_dict(
            {**asdict(bridge.settings), "camera": command["camera"]}
        )
        return await bridge.apply(settings)
    if operation == "set" and set(command) == {"op", "device", "enabled"}:
        device = command["device"]
        if (
            device not in ("speaker", "microphone")
            or type(command["enabled"]) is not bool
        ):
            raise ValueError("Invalid device switch")
        if command["enabled"] and bridge.settings.camera is None:
            raise ValueError("Configure a camera before enabling audio devices")
        return await bridge.apply(
            replace(bridge.settings, **{device: command["enabled"]})
        )
    if operation == "reconnect" and set(command) == {"op"}:
        return await bridge.apply(bridge.settings, restart=True)
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


async def serve(paths: Paths) -> None:
    with instance_lock(paths):
        paths.socket.unlink(missing_ok=True)
        bridge = Bridge(paths)
        stopping = asyncio.Event()
        handlers: set[asyncio.Task] = set()
        commands = asyncio.Lock()
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
                    async with commands:
                        status = await dispatch(bridge, command)
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
            await bridge.apply(paths.load(), persist=False)
            server = await asyncio.start_unix_server(handle, paths.socket, limit=65536)
            paths.socket.chmod(0o600)
            if address := os.environ.get("NOTIFY_SOCKET"):
                if address.startswith("@"):
                    address = "\0" + address[1:]
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                    notifier.sendto(b"READY=1", address)
            print("Hikvision audio bridge ready", flush=True)
            async with server:
                await stopping.wait()
        finally:
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            await bridge.close()
            paths.socket.unlink(missing_ok=True)
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
