"""Headless camera management, isolated network sessions and a bounded TTS FIFO."""

import asyncio
import json
import secrets
from collections import deque
from dataclasses import asdict, dataclass, field, replace

from aiohttp import WSMsgType, web

from .audio import (
    BUFFER_FRAMES,
    MAX_STREAMS,
    PROTOCOL_VERSION,
    SILENCE,
    decode_frame,
    encode_frame,
)
from .chimes import (
    Chime,
    compose_announcement,
    validate_chime,
    validate_plays,
)
from .config import (
    AudioDevice,
    Paths,
    RoutingSettings,
    ServerSettings,
    camera_from_dict,
    validate_id,
)
from .service import CameraEngine
from .tts import DEFAULT_VOICE, _validate_text, _validate_voice, synthesize
from .vault import ServerSecrets

MAX_QUEUED_SPEECH = 10
MAX_COMPLETED_SPEECH = 64
MAX_PENDING_CONTROL = 32


@dataclass(frozen=True)
class SessionRoute:
    device_id: str
    device: AudioDevice


@dataclass
class AudioSession:
    identifier: str
    name: str
    socket: web.WebSocketResponse
    routes: dict[int, SessionRoute] = field(default_factory=dict)
    control: deque[dict] = field(default_factory=deque)
    requests: asyncio.Queue[dict] = field(
        default_factory=lambda: asyncio.Queue(maxsize=MAX_PENDING_CONTROL)
    )
    audio: dict[int, deque[bytes]] = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    ready: bool = False
    paused: set[int] = field(default_factory=set)
    cleanup: asyncio.Task | None = None
    sent_bytes: int = 0
    received_bytes: int = 0

    def route_id(self, stream_id: int) -> str:
        return f"s{self.identifier}-{stream_id}"

    def reply(self, message: dict) -> None:
        if len(self.control) >= MAX_PENDING_CONTROL:
            raise ValueError("Too many pending audio control requests")
        self.control.append(message)
        self.wake.set()


class NetworkAudio:
    def __init__(self):
        self.speakers: dict[str, deque[bytes]] = {}
        self.microphones: dict[str, tuple[AudioSession, int]] = {}

    def remove(self, session: AudioSession) -> None:
        for stream_id in session.routes:
            identifier = session.route_id(stream_id)
            self.speakers.pop(identifier, None)
            self.microphones.pop(identifier, None)
        session.audio.clear()

    def install(self, session: AudioSession) -> None:
        for stream_id, route in session.routes.items():
            identifier = session.route_id(stream_id)
            if route.device.direction == "speaker":
                self.speakers.setdefault(identifier, deque(maxlen=BUFFER_FRAMES))
            else:
                self.microphones[identifier] = session, stream_id

    def receive(self, session: AudioSession, stream_id: int, pcm: bytes) -> None:
        if stream_id in session.paused:
            return
        route = session.routes.get(stream_id)
        if route is None or route.device.direction != "speaker" or not session.ready:
            raise ValueError("Audio frame references an inactive speaker stream")
        self.speakers[session.route_id(stream_id)].append(pcm)
        session.received_bytes += len(pcm)

    def read_speaker(self, identifier: str) -> bytes:
        queue = self.speakers.get(identifier)
        return queue.popleft() if queue else SILENCE

    def write_microphone(self, identifier: str, pcm: bytes) -> None:
        target = self.microphones.get(identifier)
        if target is not None:
            session, stream_id = target
            if session.ready and stream_id not in session.paused:
                session.audio.setdefault(stream_id, deque(maxlen=BUFFER_FRAMES)).append(
                    pcm
                )
                session.wake.set()

    def flush(self, identifiers: set[str] | None = None) -> None:
        if identifiers is None:
            identifiers = self.speakers.keys() | self.microphones.keys()
        for identifier in identifiers:
            queue = self.speakers.get(identifier)
            if queue is not None:
                queue.clear()
            target = self.microphones.get(identifier)
            if target is not None:
                session, stream_id = target
                session.audio.pop(stream_id, None)


@dataclass(frozen=True)
class SpeechJob:
    identifier: str
    text: str
    cameras: tuple[str, ...]
    voice: str
    chime: Chime | None = None
    plays: int = 1

    def public(self) -> dict:
        return {
            "id": self.identifier,
            "cameras": list(self.cameras),
            "voice": self.voice,
        }


class SpeechQueue:
    def __init__(self, engine: CameraEngine):
        self.engine = engine
        self.pending: deque[SpeechJob] = deque()
        self.current: SpeechJob | None = None
        self.worker: asyncio.Task | None = None
        self.playing: asyncio.Task | None = None
        self.stage = "idle"
        self.completed: dict[str, dict] = {}
        self.closing = False

    @property
    def last(self) -> dict | None:
        return next(reversed(self.completed.values()), None)

    def job_status(self, identifier: str) -> dict:
        if not isinstance(identifier, str):
            raise ValueError("Speech job ID must be a string")
        if self.current is not None and self.current.identifier == identifier:
            return {
                **self.current.public(),
                "state": self.status()["state"],
                "error": None,
            }
        for job in self.pending:
            if job.identifier == identifier:
                return {**job.public(), "state": "queued", "error": None}
        result = self.completed.get(identifier)
        if result is None:
            raise ValueError("Unknown or expired speech job")
        return result

    def _remember(self, result: dict) -> None:
        self.completed[result["id"]] = result
        if len(self.completed) > MAX_COMPLETED_SPEECH:
            del self.completed[next(iter(self.completed))]

    def status(self) -> dict:
        stage = self.stage
        if stage == "playing" and self.engine.router.speech is None:
            stage = "connecting"
        return {
            "state": stage,
            "current": self.current.public() if self.current else None,
            "queue": [job.public() for job in self.pending],
            "last": self.last,
            "capacity": MAX_QUEUED_SPEECH,
        }

    def enqueue(
        self,
        text: str,
        cameras: list[str],
        voice: str,
        chime: object = None,
        plays: object = 1,
    ) -> str:
        if not isinstance(text, str) or not isinstance(voice, str):
            raise ValueError("Speech text and voice must be strings")
        text, voice = _validate_text(text), _validate_voice(voice)
        chime_value = None if chime is None else validate_chime(chime)
        plays_value = validate_plays(plays)
        if self.closing:
            raise RuntimeError("Speech queue is stopping")
        if not isinstance(cameras, list) or not cameras:
            raise ValueError("Select at least one target camera")
        targets = tuple(dict.fromkeys(validate_id(camera) for camera in cameras))
        if set(targets) - self.engine.settings.cameras.keys():
            raise ValueError("Unknown speech target camera")
        if len(self.pending) >= MAX_QUEUED_SPEECH:
            raise ValueError("Speech queue is full")
        job = SpeechJob(
            secrets.token_hex(8), text, targets, voice, chime_value, plays_value
        )
        self.pending.append(job)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())
        return job.identifier

    def uses(self, camera_id: str) -> bool:
        return (self.current is not None and camera_id in self.current.cameras) or any(
            camera_id in job.cameras for job in self.pending
        )

    async def _play(self, job: SpeechJob) -> None:
        pcm = await synthesize(job.text, job.voice)
        self.stage = "playing"
        announcement = compose_announcement(pcm, job.chime, job.plays)
        await self.engine.play_pcm(announcement, job.cameras)

    async def _run(self) -> None:
        try:
            while self.pending and not self.closing:
                job = self.pending.popleft()
                self.current = job
                self.stage = "synthesizing"
                self.playing = asyncio.create_task(self._play(job))
                result = {**job.public(), "state": "completed", "error": None}
                try:
                    await self.playing
                except asyncio.CancelledError:
                    result["state"] = "cancelled"
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                except Exception as error:
                    message = str(error) or type(error).__name__
                    result.update(
                        state="error",
                        error=message.replace(job.text, "[redacted]")[:1000],
                    )
                finally:
                    self._remember(result)
                    self.current = None
                    self.playing = None
                    self.stage = "idle"
        finally:
            self.worker = None

    async def cancel(self, identifier: str) -> None:
        if not isinstance(identifier, str):
            raise ValueError("Speech job ID must be a string")
        for job in self.pending:
            if job.identifier == identifier:
                self.pending.remove(job)
                self._remember({**job.public(), "state": "cancelled", "error": None})
                return
        if self.current is None or self.current.identifier != identifier:
            if identifier in self.completed:
                return
            raise ValueError("Unknown speech job")
        task = self.playing
        if task is not None:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.shield(_finish(task))

    async def stop(self) -> None:
        while self.pending:
            job = self.pending.popleft()
            self._remember({**job.public(), "state": "cancelled", "error": None})
        task = self.playing
        if task is not None and not task.done() and not task.cancelling():
            task.cancel()
        worker = self.worker
        if worker is not None:
            await asyncio.shield(_finish(worker))

    async def close(self) -> None:
        self.closing = True
        await self.stop()


async def _finish(task: asyncio.Task) -> None:
    await asyncio.gather(task, return_exceptions=True)


class Gateway:
    def __init__(self, paths: Paths, vault: ServerSecrets):
        self.paths = paths
        self.vault = vault
        self.access_token = vault.token("audio")
        self.admin_token = vault.token("admin")
        self.settings = ServerSettings()
        self.audio = NetworkAudio()
        self.engine = CameraEngine(vault, self.audio)
        self.sessions: dict[str, AudioSession] = {}
        self.speech = SpeechQueue(self.engine)
        self.lock = asyncio.Lock()
        self.closing = False
        self.close_task: asyncio.Task | None = None

    async def start(self, settings: ServerSettings) -> None:
        self.settings = settings
        await self.engine.apply(self._routing())

    def _routing(self) -> RoutingSettings:
        return RoutingSettings(
            self.settings.cameras,
            {
                session.route_id(stream_id): route.device
                for session in self.sessions.values()
                for stream_id, route in session.routes.items()
            },
        )

    def catalog(self) -> dict:
        return {"protocol": PROTOCOL_VERSION, **self.engine.status()}

    def status(self) -> dict:
        return {
            "component": "server",
            "settings": asdict(self.settings),
            **self.engine.status(),
            "sessions": {
                identifier: {
                    "name": session.name,
                    "routes": {
                        route.device_id: asdict(route.device)
                        for route in session.routes.values()
                    },
                    "received_bytes": session.received_bytes,
                    "sent_bytes": session.sent_bytes,
                }
                for identifier, session in self.sessions.items()
            },
            "tts": self.speech.status(),
        }

    async def dispatch(self, command: object) -> dict:
        if not isinstance(command, dict):
            raise ValueError("Control request must be an object")
        operation = command.get("op")
        if operation == "status" and set(command) == {"op"}:
            return self.status()
        if operation == "tts.get" and set(command) == {"op", "id"}:
            return self.speech.job_status(command["id"])
        if operation == "session.disconnect" and set(command) == {"op", "id"}:
            session = self.sessions.get(command["id"])
            if session is None:
                raise ValueError("Unknown audio session")
            await session.socket.close(
                code=1000, message=b"Disconnected by administrator"
            )
            await self.detach(session)
            return self.status()
        async with self.lock:
            if self.closing:
                raise RuntimeError("Camera server is stopping")
            if (
                operation == "camera.save"
                and {"op", "id", "camera"} <= set(command)
                and not set(command) - {"op", "id", "camera", "password"}
            ):
                identifier = validate_id(command["id"])
                camera = camera_from_dict(command["camera"])
                settings = replace(
                    self.settings, cameras={**self.settings.cameras, identifier: camera}
                )
                if "password" in command:
                    password = command["password"]
                    if not isinstance(password, str) or not password:
                        raise ValueError("Camera password must be a nonempty string")
                    await asyncio.to_thread(self.vault.set, camera, password)
                self.paths.save(settings)
                self.settings = settings
                await self.engine.apply(
                    self._routing(), restart=True, camera_id=identifier
                )
            elif operation == "camera.remove" and set(command) == {"op", "id"}:
                identifier = validate_id(command["id"])
                if identifier not in self.settings.cameras:
                    raise ValueError("Unknown camera ID")
                if self.speech.uses(identifier) or any(
                    identifier in route.device.cameras
                    for session in self.sessions.values()
                    for route in session.routes.values()
                ):
                    raise ValueError(
                        "Remove active routes and speech jobs "
                        "before deleting this camera"
                    )
                settings = replace(
                    self.settings,
                    cameras={
                        key: value
                        for key, value in self.settings.cameras.items()
                        if key != identifier
                    },
                )
                self.paths.save(settings)
                self.settings = settings
                await self.engine.apply(self._routing())
            elif operation == "reconnect" and set(command) in ({"op"}, {"op", "id"}):
                identifier = validate_id(command["id"]) if "id" in command else None
                if identifier is not None and identifier not in self.settings.cameras:
                    raise ValueError("Unknown camera ID")
                await self.engine.apply(
                    self._routing(), restart=True, camera_id=identifier
                )
            elif (
                operation == "tts.speak"
                and {"op", "text", "cameras"} <= set(command)
                and not set(command)
                - {"op", "text", "cameras", "voice", "chime", "plays"}
            ):
                if "chime" in command:
                    validate_chime(command["chime"])
                if "plays" in command:
                    validate_plays(command["plays"])
                identifier = self.speech.enqueue(
                    command["text"],
                    command["cameras"],
                    command.get("voice", DEFAULT_VOICE),
                    command.get("chime"),
                    command.get("plays", 1),
                )
                return {**self.status(), "queued_job": identifier}
            elif operation == "tts.cancel" and set(command) == {"op", "id"}:
                await self.speech.cancel(command["id"])
            elif operation == "tts.stop" and set(command) == {"op"}:
                await self.speech.stop()
            elif operation == "access.show" and set(command) == {"op"}:
                return {**self.status(), "audio_access_token": self.access_token}
            else:
                raise ValueError("Unknown control command or unexpected fields")
            return self.status()

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        if self.closing:
            raise web.HTTPServiceUnavailable()
        socket = web.WebSocketResponse(
            heartbeat=20, timeout=3, max_msg_size=256 * 1024, compress=False
        )
        await socket.prepare(request)
        session = None
        tasks: list[asyncio.Task] = []
        try:
            message = await socket.receive(timeout=5)
            if message.type != WSMsgType.TEXT:
                raise ValueError("An audio hello message is required")
            hello = json.loads(message.data)
            if (
                not isinstance(hello, dict)
                or set(hello) != {"op", "protocol", "name"}
                or hello["op"] != "hello"
                or type(hello["protocol"]) is not int
                or hello["protocol"] != PROTOCOL_VERSION
            ):
                raise ValueError("Unsupported audio protocol")
            name = hello["name"]
            if (
                not isinstance(name, str)
                or not name.strip()
                or len(name) > 100
                or any(ord(character) < 32 for character in name)
            ):
                raise ValueError("Invalid desktop session name")
            session = AudioSession(secrets.token_hex(8), name, socket)
            async with self.lock:
                if self.closing:
                    raise RuntimeError("Camera server is stopping")
                self.sessions[session.identifier] = session
            session.reply(
                {"op": "hello", "session_id": session.identifier, **self.catalog()}
            )
            tasks = [
                asyncio.create_task(self._receive(session)),
                asyncio.create_task(self._commands(session)),
                asyncio.create_task(self._send(session)),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.CancelledError:
            raise
        except (ValueError, RuntimeError, TimeoutError, ConnectionError):
            await socket.close(
                code=1008, message=b"Audio session rejected or disconnected"
            )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if session is not None:
                await self.detach(session)
            await socket.close()
        return socket

    async def _receive(self, session: AudioSession) -> None:
        async for message in session.socket:
            if message.type == WSMsgType.BINARY:
                stream_id, pcm = decode_frame(message.data)
                self.audio.receive(session, stream_id, pcm)
            elif message.type == WSMsgType.TEXT:
                value = json.loads(message.data)
                if (
                    not isinstance(value, dict)
                    or type(value.get("request_id")) is not int
                ):
                    raise ValueError("Invalid audio control request")
                if session.requests.full():
                    raise ValueError("Too many pending audio control requests")
                session.requests.put_nowait(value)
            elif message.type == WSMsgType.ERROR:
                raise RuntimeError("Audio connection failed")

    async def _commands(self, session: AudioSession) -> None:
        # Camera cleanup may block; receiving unaffected PCM must not wait for it.
        while True:
            value = await session.requests.get()
            identifier = value["request_id"]
            try:
                if value.get("op") == "routes" and set(value) == {
                    "op",
                    "request_id",
                    "streams",
                }:
                    await self._set_routes(session, value["streams"])
                elif value.get("op") != "status" or set(value) != {"op", "request_id"}:
                    raise ValueError("Unknown audio control operation")
                session.reply(
                    {
                        "op": "result",
                        "request_id": identifier,
                        "ok": True,
                        "status": self.catalog(),
                    }
                )
                session.ready = True
                session.paused.clear()
            except ValueError as error:
                session.reply(
                    {
                        "op": "result",
                        "request_id": identifier,
                        "ok": False,
                        "error": str(error),
                    }
                )

    async def _set_routes(self, session: AudioSession, streams: object) -> None:
        if not isinstance(streams, list) or len(streams) > MAX_STREAMS:
            raise ValueError("A session supports at most 64 audio streams")
        routes: dict[int, SessionRoute] = {}
        device_ids: set[str] = set()
        for stream in streams:
            if not isinstance(stream, dict) or set(stream) != {
                "stream_id",
                "device_id",
                "device",
            }:
                raise ValueError("Invalid audio stream definition")
            stream_id = stream["stream_id"]
            if (
                type(stream_id) is not int
                or not 1 <= stream_id <= 65535
                or stream_id in routes
            ):
                raise ValueError("Invalid or duplicate stream ID")
            device_id = validate_id(stream["device_id"])
            if device_id in device_ids:
                raise ValueError("Duplicate device ID")
            device_ids.add(device_id)
            device = AudioDevice.from_dict(stream["device"])
            if not device.enabled:
                raise ValueError("Only enabled devices may register audio streams")
            routes[stream_id] = SessionRoute(device_id, device)
        async with self.lock:
            if self.closing or self.sessions.get(session.identifier) is not session:
                raise RuntimeError("Audio session is closed")
            for route in routes.values():
                if set(route.device.cameras) - self.settings.cameras.keys():
                    raise ValueError("Audio route references an unknown camera")
            session.paused = {
                stream
                for stream in session.routes.keys() | routes.keys()
                if session.routes.get(stream) != routes.get(stream)
            }
            for stream in session.paused:
                identifier = session.route_id(stream)
                self.audio.speakers.pop(identifier, None)
                self.audio.microphones.pop(identifier, None)
                session.audio.pop(stream, None)
            session.routes = routes
            self.audio.install(session)
            await self.engine.apply(self._routing())

    async def _send(self, session: AudioSession) -> None:
        while True:
            await session.wake.wait()
            session.wake.clear()
            while session.control or any(session.audio.values()):
                if session.control:
                    async with asyncio.timeout(2):
                        await session.socket.send_json(session.control.popleft())
                    continue
                for stream_id in tuple(session.audio):
                    queue = session.audio.get(stream_id)
                    if not queue:
                        continue
                    pcm = queue.popleft()
                    async with asyncio.timeout(2):
                        await session.socket.send_bytes(encode_frame(stream_id, pcm))
                    session.sent_bytes += len(pcm)
                    if session.control:
                        break

    async def detach(self, session: AudioSession) -> None:
        if session.cleanup is None:
            session.cleanup = asyncio.create_task(self._detach(session))
        await asyncio.shield(session.cleanup)

    async def _detach(self, session: AudioSession) -> None:
        async with self.lock:
            if self.sessions.get(session.identifier) is not session:
                return
            session.ready = False
            self.audio.remove(session)
            del self.sessions[session.identifier]
            if not self.closing:
                await self.engine.apply(self._routing())

    async def close(self) -> None:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self) -> None:
        self.closing = True
        await self.speech.close()
        sessions = list(self.sessions.values())
        await asyncio.gather(
            *(session.socket.close() for session in sessions), return_exceptions=True
        )
        await asyncio.gather(*(self.detach(session) for session in sessions))
        await self.engine.close()
