"""Same-UID Unix control and shared process lifecycle for both independent roles."""

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import socket
import struct
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Protocol

from .config import Paths, WebSettings
from .web import WebServer


class Controller(Protocol):
    async def dispatch(self, command: object) -> dict: ...

    async def close(self) -> None: ...


async def call(paths: Paths, command: dict) -> dict:
    try:
        async with asyncio.timeout(20):
            reader, writer = await asyncio.open_unix_connection(
                paths.socket, limit=1024 * 1024
            )
            try:
                writer.write(json.dumps(command).encode() + b"\n")
                await writer.drain()
                response = json.loads(await reader.readline())
            finally:
                writer.close()
                await writer.wait_closed()
    except (FileNotFoundError, ConnectionRefusedError):
        raise RuntimeError("The component is not running; start its service") from None
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Control request failed"))
    return response["status"]


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
            raise RuntimeError("This audio component is already running") from None
        yield
    finally:
        os.close(descriptor)


async def cleanup(paths: Paths) -> None:
    from .pipewire import AudioDevices

    with instance_lock(paths):
        await AudioDevices(paths.runtime).recover()
        paths.socket.unlink(missing_ok=True)
        paths.web_access.unlink(missing_ok=True)


async def serve(paths: Paths, web_settings: WebSettings | None = None) -> None:
    from .desktop import DesktopBridge

    with instance_lock(paths):
        settings = paths.load_desktop()
        if web_settings is not None:
            settings = replace(settings, web=web_settings)
        desktop = DesktopBridge(paths)
        web_server = WebServer(
            paths, settings.web, desktop.dispatch, component="desktop"
        )

        async def initialize() -> None:
            await desktop.devices.recover()
            await desktop.apply(settings, persist=False)

        await _run(paths, desktop, web_server, initialize)


async def serve_server(paths: Paths, web_settings: WebSettings | None = None) -> None:
    from .server import Gateway
    from .vault import ServerSecrets

    with instance_lock(paths):
        settings = paths.load_server()
        if web_settings is not None:
            settings = replace(settings, web=web_settings)
        gateway = Gateway(paths, ServerSecrets(paths.config / "secrets"))
        web_server = WebServer(
            paths,
            settings.web,
            gateway.dispatch,
            component="server",
            token=gateway.admin_token,
            audio=gateway,
        )
        await _run(paths, gateway, web_server, lambda: gateway.start(settings))


async def _run(
    paths: Paths,
    controller: Controller,
    web_server: WebServer,
    initialize: Callable[[], Awaitable[None]],
) -> None:
    paths.socket.unlink(missing_ok=True)
    stopping = asyncio.Event()
    handlers: set[asyncio.Task] = set()
    server: asyncio.Server | None = None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        if task is not None:
            handlers.add(task)
        command = None
        try:
            try:
                peer = writer.get_extra_info("socket")
                _, uid, _ = struct.unpack(
                    "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if uid != os.getuid():
                    raise PermissionError("Control access requires the same user")
                async with asyncio.timeout(20):
                    command = json.loads(await reader.readline())
                    status = await controller.dispatch(command)
                response = {"ok": True, "status": status}
            except Exception as error:
                response = {
                    "ok": False,
                    "error": web_server._message(
                        error, command if isinstance(command, dict) else None
                    ),
                }
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
        await initialize()
        server = await asyncio.start_unix_server(handle, paths.socket, limit=65536)
        paths.socket.chmod(0o600)
        await web_server.start()
        if address := os.environ.get("NOTIFY_SOCKET"):
            if address.startswith("@"):
                address = "\0" + address[1:]
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                notifier.sendto(b"READY=1", address)
        print(f"Hikvision audio {web_server.component} ready", flush=True)
        async with server:
            await stopping.wait()
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        try:
            await controller.close()
        finally:
            await web_server.close()
            paths.socket.unlink(missing_ok=True)
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
