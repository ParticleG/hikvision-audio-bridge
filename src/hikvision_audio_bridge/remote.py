"""One authenticated, bounded duplex WebSocket from a desktop to the camera server."""

import asyncio
import contextlib
import json
import ssl
from collections.abc import Callable
from dataclasses import asdict
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import WSMsgType

from .audio import FRAME_SECONDS, PROTOCOL_VERSION, SILENCE, decode_frame, encode_frame
from .config import AudioDevice, BackendSettings


class RemoteAudio:
    def __init__(
        self,
        backend: BackendSettings,
        token: str,
        read_speaker: Callable[[str], bytes],
        write_microphone: Callable[[str, bytes], None],
        flush: Callable[[set[str] | None], None],
        failed: Callable[[str], None],
    ):
        self.backend = backend
        self._token = token
        self.read_speaker = read_speaker
        self.write_microphone = write_microphone
        self.flush = flush
        self.failed = failed
        self.catalog: dict = {}
        self.session_id: str | None = None
        self.streams: dict[int, tuple[str, AudioDevice]] = {}
        self._stream_ids: dict[str, int] = {}
        self._next_stream = 1
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._route_updates: dict[int, dict[int, tuple[str, AudioDevice]]] = {}
        self._paused: set[int] = set()
        self._send_lock = asyncio.Lock()
        self._routes_lock = asyncio.Lock()
        self._client: aiohttp.ClientSession | None = None
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self.task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._intentional_close = False

    async def start(self) -> None:
        context = ssl.create_default_context()
        if self.backend.ca_file:
            context.load_verify_locations(self.backend.ca_file)
        self._client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10), trust_env=False
        )
        parsed = urlsplit(self.backend.url)
        url = urlunsplit(
            parsed._replace(
                scheme="wss" if parsed.scheme == "https" else "ws",
                path="/api/audio/stream",
            )
        )
        try:
            async with asyncio.timeout(15):
                self._socket = await self._client.ws_connect(
                    url,
                    headers={"Authorization": f"Bearer {self._token}"},
                    ssl=context,
                    heartbeat=20,
                    compress=0,
                    max_msg_size=256 * 1024,
                )
                await self._socket.send_json(
                    {
                        "op": "hello",
                        "protocol": PROTOCOL_VERSION,
                        "name": self.backend.name,
                    }
                )
                message = await self._socket.receive()
                if message.type != WSMsgType.TEXT:
                    raise RuntimeError("The backend rejected the audio handshake")
                hello = json.loads(message.data)
                if (
                    not isinstance(hello, dict)
                    or hello.get("op") != "hello"
                    or hello.get("protocol") != PROTOCOL_VERSION
                    or not isinstance(hello.get("session_id"), str)
                ):
                    raise RuntimeError("Unsupported backend audio protocol")
                self.session_id = hello["session_id"]
                self._catalog(hello)
            self.task = asyncio.create_task(self._run())
        except BaseException:
            await self.close()
            raise

    def _catalog(self, message: dict) -> None:
        cameras = message.get("cameras")
        if not isinstance(cameras, dict):
            raise RuntimeError("Invalid backend camera catalog")
        self.catalog = cameras

    async def _send_json(self, message: dict) -> None:
        if self._socket is None or self._socket.closed:
            raise RuntimeError("The backend audio connection is closed")
        async with self._send_lock:
            async with asyncio.timeout(2):
                await self._socket.send_json(message)

    async def _request(
        self,
        command: dict,
        routes: dict[int, tuple[str, AudioDevice]] | None = None,
    ) -> dict:
        self._request_id += 1
        identifier = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        if routes is not None:
            self._route_updates[identifier] = routes
        try:
            await self._send_json({**command, "request_id": identifier})
            async with asyncio.timeout(15):
                return await future
        finally:
            self._pending.pop(identifier, None)
            self._route_updates.pop(identifier, None)

    def pause(self, identifiers: set[str]) -> None:
        self._paused.update(
            stream
            for stream, (identifier, _) in self.streams.items()
            if identifier in identifiers
        )

    async def set_routes(self, devices: dict[str, AudioDevice]) -> None:
        async with self._routes_lock:
            proposed: dict[int, tuple[str, AudioDevice]] = {}
            for identifier, device in devices.items():
                if not device.enabled:
                    continue
                if identifier not in self._stream_ids:
                    if self._next_stream > 65535:
                        raise RuntimeError(
                            "Audio stream IDs exhausted; reconnect the backend"
                        )
                    self._stream_ids[identifier] = self._next_stream
                    self._next_stream += 1
                proposed[self._stream_ids[identifier]] = identifier, device
            changed = {
                stream
                for stream in self.streams.keys() | proposed.keys()
                if self.streams.get(stream) != proposed.get(stream)
            }
            self._paused.update(changed)
            try:
                await self._request(
                    {
                        "op": "routes",
                        "streams": [
                            {
                                "stream_id": stream_id,
                                "device_id": identifier,
                                "device": asdict(device),
                            }
                            for stream_id, (identifier, device) in proposed.items()
                        ],
                    },
                    proposed,
                )
            finally:
                self._paused.clear()

    async def _receive(self) -> None:
        assert self._socket is not None
        async for message in self._socket:
            if message.type == WSMsgType.BINARY:
                stream_id, pcm = decode_frame(message.data)
                route = self.streams.get(stream_id)
                if route is None or route[1].direction != "microphone":
                    raise RuntimeError("Backend sent an unknown microphone stream")
                if stream_id in self._paused:
                    continue
                with contextlib.suppress(KeyError, OSError):
                    self.write_microphone(route[0], pcm)
            elif message.type == WSMsgType.TEXT:
                value = json.loads(message.data)
                if not isinstance(value, dict) or value.get("op") != "result":
                    raise RuntimeError("Invalid backend control response")
                identifier = value.get("request_id")
                future = self._pending.get(identifier)
                if future is None or future.done():
                    continue
                if not value.get("ok"):
                    future.set_exception(
                        RuntimeError(value.get("error", "Audio route rejected"))
                    )
                    continue
                status = value.get("status")
                if not isinstance(status, dict):
                    raise RuntimeError("Invalid backend status")
                self._catalog(status)
                proposed = self._route_updates.get(identifier)
                if proposed is not None:
                    changed = {
                        route[0]
                        for stream in self.streams.keys() | proposed.keys()
                        if self.streams.get(stream) != proposed.get(stream)
                        for route in (self.streams.get(stream), proposed.get(stream))
                        if route is not None
                    }
                    self.flush(changed)
                    self.streams = proposed
                future.set_result(status)
            elif message.type == WSMsgType.ERROR:
                raise RuntimeError("Backend audio transport failed")
        raise RuntimeError("Backend disconnected; reconnect explicitly")

    async def _send(self) -> None:
        assert self._socket is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            for stream_id, (identifier, device) in tuple(self.streams.items()):
                if device.direction != "speaker" or stream_id in self._paused:
                    continue
                try:
                    pcm = self.read_speaker(identifier)
                except (KeyError, OSError):
                    pcm = SILENCE
                async with self._send_lock:
                    if stream_id in self._paused or self.streams.get(stream_id) != (
                        identifier,
                        device,
                    ):
                        continue
                    async with asyncio.timeout(2):
                        await self._socket.send_bytes(encode_frame(stream_id, pcm))
            deadline += FRAME_SECONDS
            delay = deadline - loop.time()
            if delay <= 0:
                deadline = loop.time()
            await asyncio.sleep(max(0, delay))

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(2)
            await self._request({"op": "status"})

    async def _run(self) -> None:
        tasks = [
            asyncio.create_task(self._receive()),
            asyncio.create_task(self._send()),
            asyncio.create_task(self._poll()),
        ]
        error = "Backend disconnected; reconnect explicitly"
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            error = str(failure) or type(failure).__name__
            error = error.replace(self._token, "[redacted]")[:1000]
        finally:
            self._cleanup_task = asyncio.create_task(self._cleanup(tasks, error))
            await asyncio.shield(self._cleanup_task)

    async def _cleanup(self, tasks: list[asyncio.Task], error: str) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Backend audio connection closed"))
        if self._socket is not None:
            await self._socket.close()
        if self._client is not None:
            await self._client.close()
        if not self._intentional_close:
            self.failed(error)

    async def close(self) -> None:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self) -> None:
        self._intentional_close = True
        if self.task is not None:
            if (
                self._cleanup_task is None
                and not self.task.done()
                and not self.task.cancelling()
            ):
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            if self._cleanup_task is not None:
                await asyncio.shield(self._cleanup_task)
        else:
            if self._socket is not None:
                await self._socket.close()
            if self._client is not None:
                await self._client.close()
