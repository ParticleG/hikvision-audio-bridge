"""One owned camera session per channel, shared by logical network audio routes."""

import asyncio
import contextlib
from dataclasses import dataclass, field

from .camera import CameraSession
from .config import Camera, RoutingSettings, validate_id
from .router import AudioIO, AudioRouter, SpeechPlayback, camera_routes
from .vault import CameraCredentials


async def _settle(task: asyncio.Task) -> None:
    await asyncio.gather(task, return_exceptions=True)


@dataclass
class _CameraState:
    camera_id: str
    camera: Camera
    desired_speaker: bool = False
    desired_microphone: bool = False
    speaker_devices: tuple[str, ...] = ()
    microphone_devices: tuple[str, ...] = ()
    worker: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    stop_task: asyncio.Task | None = None
    session: CameraSession | None = None
    streams: dict[str, asyncio.Task] = field(default_factory=dict)
    stream_stops: dict[str, asyncio.Task] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    connection: str = "idle"
    error: str | None = None
    cleanup_error: str | None = None
    codec: str | None = None
    sent_bytes: int = 0
    received_bytes: int = 0
    closing: bool = False


class CameraEngine:
    def __init__(self, credentials: CameraCredentials, devices: AudioIO):
        self.credentials = credentials
        self.devices = devices
        self.router = AudioRouter(lambda: self.devices)
        self.settings = RoutingSettings()
        self.lock = asyncio.Lock()
        self.cameras: dict[str, _CameraState] = {}
        self.speech_cameras: tuple[str, ...] = ()
        self.speech_playback: SpeechPlayback | None = None
        self.speech_cleanup: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None
        self.closing = False

    def status(self) -> dict:
        cameras = {}
        for identifier, camera in self.settings.cameras.items():
            state = self.cameras.get(identifier) or _CameraState(identifier, camera)
            session = state.session
            cameras[identifier] = {
                "connection": state.connection,
                "error": state.error,
                "cleanup_error": state.cleanup_error,
                "codec": (session.codec or None) if session else state.codec,
                "sent_bytes": session.sent_bytes if session else state.sent_bytes,
                "received_bytes": session.received_bytes
                if session
                else state.received_bytes,
            }
        return {"cameras": cameras}

    async def apply(
        self,
        settings: RoutingSettings,
        *,
        restart: bool = False,
        camera_id: str | None = None,
    ) -> dict:
        if not isinstance(settings, RoutingSettings):
            raise TypeError("settings must be a RoutingSettings instance")
        if camera_id is not None:
            validate_id(camera_id)
        async with self.lock:
            if self.closing:
                raise RuntimeError("Camera server is stopping")
            previous = self.settings
            restart_ids: set[str] = set()
            if restart:
                restart_ids = set(settings.cameras) | set(previous.cameras)
                if camera_id is not None:
                    if camera_id not in restart_ids:
                        raise ValueError("Unknown camera ID")
                    restart_ids = {camera_id}
            if self.speech_playback is not None and any(
                identifier in restart_ids
                or previous.cameras.get(identifier) != settings.cameras.get(identifier)
                for identifier in self.speech_cameras
            ):
                self.speech_playback.fail("Speech target changed or was reconnected")
                self.speech_cameras = ()
                self.router.clear_speech()
            self.settings = settings
            await self._reconcile(restart_ids)
            return self.status()

    async def _reconcile(self, restart_ids: set[str]) -> None:
        """Run while holding lock; speech changes reuse the current route snapshot."""
        settings = self.settings
        self.router.configure(settings)
        for identifier, camera in settings.cameras.items():
            self.cameras.setdefault(identifier, _CameraState(identifier, camera))
        pending = [
            self._ensure_stop(state)
            for state in self.cameras.values()
            if state.stop_task is not None
            or (state.worker is not None and state.worker.done())
        ]
        if pending:
            await asyncio.gather(*(asyncio.shield(task) for task in pending))
        speaker, microphone = camera_routes(settings)
        stop_ids: set[str] = set()
        reset_ids: set[str] = set()
        for identifier, state in self.cameras.items():
            if identifier not in settings.cameras:
                stop_ids.add(identifier)
                continue
            if state.worker is None and state.connection in ("connecting", "connected"):
                state.connection = "idle"
            if (
                identifier in restart_ids
                or state.camera != settings.cameras[identifier]
            ):
                stop_ids.add(identifier)
                reset_ids.add(identifier)
            elif not (
                speaker.get(identifier)
                or microphone.get(identifier)
                or identifier in self.speech_cameras
            ):
                if state.worker is not None:
                    stop_ids.add(identifier)
        stops = [self._ensure_stop(self.cameras[identifier]) for identifier in stop_ids]
        if stops:
            await asyncio.gather(*(asyncio.shield(task) for task in stops))
        for identifier in list(self.cameras):
            if identifier not in settings.cameras:
                del self.cameras[identifier]
        for identifier in reset_ids:
            state = self.cameras.get(identifier)
            if state is not None:
                state.camera = settings.cameras[identifier]
                state.error = None
                state.codec = None
                state.sent_bytes = 0
                state.received_bytes = 0
                state.connection = "idle"
                state.closing = False
        self.router.configure(settings)
        for identifier, state in self.cameras.items():
            state.desired_speaker = (
                bool(speaker.get(identifier)) or identifier in self.speech_cameras
            )
            state.desired_microphone = bool(microphone.get(identifier))
            state.speaker_devices = speaker.get(identifier, ())
            state.microphone_devices = microphone.get(identifier, ())
        syncs = [
            self._sync_streams(state)
            for identifier, state in self.cameras.items()
            if state.worker is not None
            and identifier not in stop_ids
            and not state.closing
        ]
        if syncs:
            await asyncio.gather(*syncs)
        if (
            any(device.enabled for device in settings.devices.values())
            or self.speech_cameras
        ):
            self.router.start()
        else:
            await self.router.close()
        for identifier, state in self.cameras.items():
            if not (state.desired_speaker or state.desired_microphone):
                continue
            if identifier in reset_ids or (
                state.worker is None and state.connection == "idle"
            ):
                self._start_camera(state, settings.cameras[identifier])

    async def _sync_streams(self, state: _CameraState) -> None:
        async with state.lock:
            await self._sync_streams_locked(state)

    async def _sync_streams_locked(self, state: _CameraState) -> None:
        session = state.session
        if state.closing or session is None or not session.opened:
            return
        changed = False
        for name, desired in (
            ("speaker", state.desired_speaker),
            ("microphone", state.desired_microphone),
        ):
            stop = state.stream_stops.get(name)
            if stop is not None and stop.done():
                state.stream_stops.pop(name, None)
                stop = None
            task = state.streams.get(name)
            if desired:
                if task is not None:
                    continue
                if stop is not None:
                    await asyncio.shield(stop)
                    state.stream_stops.pop(name, None)
                if state.closing:
                    return
                camera_id = state.camera_id
                if name == "speaker":
                    task = asyncio.create_task(
                        session.speaker(
                            lambda camera_id=camera_id: self.router.read_camera(
                                camera_id
                            )
                        )
                    )
                else:
                    task = asyncio.create_task(
                        session.microphone(
                            lambda pcm, camera_id=camera_id: self.router.consume_camera(
                                camera_id, pcm
                            )
                        )
                    )
                state.streams[name] = task
                changed = True
            elif task is not None:
                del state.streams[name]
                task.cancel()
                stop = asyncio.create_task(_settle(task))
                state.stream_stops[name] = stop
                changed = True
                await asyncio.shield(stop)
                state.stream_stops.pop(name, None)
        if changed:
            state.changed.set()

    def _start_camera(self, state: _CameraState, camera: Camera) -> None:
        state.camera = camera
        state.error = None
        state.connection = "connecting"
        state.codec = None
        state.sent_bytes = 0
        state.received_bytes = 0
        state.session = None
        state.streams.clear()
        state.stream_stops.clear()
        state.closing = False
        state.changed.clear()
        state.ready.clear()
        state.worker = asyncio.create_task(self._run_camera(state))

    def _ensure_stop(self, state: _CameraState) -> asyncio.Task:
        task = state.stop_task
        if task is None or task.done():
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.exception()
            task = asyncio.create_task(self._stop_state(state))
            state.stop_task = task
        return task

    async def _stop_state(self, state: _CameraState) -> None:
        try:
            worker = state.worker
            if worker is not None:
                if state.cleanup_task is None and not worker.cancelling():
                    worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                if state.cleanup_task is not None:
                    await asyncio.shield(state.cleanup_task)
            elif state.cleanup_task is not None:
                await asyncio.shield(state.cleanup_task)
            if state.stream_stops:
                await asyncio.gather(
                    *state.stream_stops.values(), return_exceptions=True
                )
        finally:
            state.worker = None
            state.cleanup_task = None
            state.session = None
            state.streams.clear()
            state.stream_stops.clear()
            if state.connection in ("connecting", "connected"):
                state.connection = "idle"
            state.ready.set()
            if state.stop_task is asyncio.current_task():
                state.stop_task = None

    async def _run_camera(self, state: _CameraState) -> None:
        session = None
        waiter = None
        try:
            password = await asyncio.to_thread(self.credentials.get, state.camera)
            session = CameraSession(state.camera, password)
            del password
            state.session = session
            await session.open()
            while True:
                async with state.lock:
                    if state.closing or not (
                        state.desired_speaker or state.desired_microphone
                    ):
                        return
                    if state.session is not session:
                        return
                    await self._sync_streams_locked(state)
                    state.connection = "connected"
                    state.ready.set()
                    state.changed.clear()
                    waiter = asyncio.create_task(state.changed.wait())
                    watched = dict(state.streams)
                completed, _ = await asyncio.wait(
                    [waiter, *watched.values()], return_when=asyncio.FIRST_COMPLETED
                )
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
                waiter = None
                for name, task in watched.items():
                    if task not in completed:
                        continue
                    try:
                        await task
                    except asyncio.CancelledError:
                        if state.streams.get(name) is task:
                            raise
                    except Exception:
                        if state.streams.get(name) is task:
                            raise
                    else:
                        if state.streams.get(name) is task:
                            raise RuntimeError(
                                f"Camera {name} stream stopped unexpectedly"
                            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.error = str(error) or type(error).__name__
            state.connection = "error"
        finally:
            state.closing = True
            state.ready.set()
            state.cleanup_task = asyncio.create_task(
                self._finish_camera(state, session, waiter)
            )
            await asyncio.shield(state.cleanup_task)

    async def _finish_camera(
        self,
        state: _CameraState,
        session: CameraSession | None,
        waiter: asyncio.Task | None,
    ) -> None:
        if waiter is not None:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        async with state.lock:
            state.closing = True
            tasks = list(state.streams.values())
            state.streams.clear()
            stops = list(state.stream_stops.values())
            state.stream_stops.clear()
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if stops:
            await asyncio.gather(*stops, return_exceptions=True)
        self.router.reset_camera(state.camera_id)
        local_error = None
        try:
            self.devices.flush(
                set(state.speaker_devices) | set(state.microphone_devices)
            )
        except Exception as error:
            local_error = str(error) or type(error).__name__
            state.cleanup_error = local_error
        if session is not None:
            owned = session.opened
            try:
                await session.close()
                if owned:
                    state.cleanup_error = local_error
            except Exception as error:
                state.cleanup_error = str(error) or type(error).__name__
            state.codec = session.codec or state.codec
            state.sent_bytes = session.sent_bytes
            state.received_bytes = session.received_bytes
        state.session = None
        if state.connection != "error":
            state.connection = "error" if state.error or state.cleanup_error else "idle"
        state.closing = False
        state.changed.set()
        state.ready.set()

    async def play_pcm(self, pcm: bytes, targets: tuple[str, ...]) -> None:
        playback = SpeechPlayback(pcm, targets)
        async with self.lock:
            if self.closing or self.speech_playback is not None:
                raise RuntimeError("The speech player is unavailable")
            if (
                not pcm
                or len(pcm) % 2
                or not targets
                or set(targets) - self.settings.cameras.keys()
            ):
                raise ValueError("Speech requires PCM16 audio and known target cameras")
            self.speech_playback = playback
            self.speech_cameras = playback.targets
        try:
            async with self.lock:
                await self._reconcile(set())
                states = [self.cameras[target] for target in playback.targets]
            async with asyncio.timeout(10):
                await asyncio.gather(*(state.ready.wait() for state in states))
            async with self.lock:
                if (
                    self.speech_playback is not playback
                    or self.speech_cameras != playback.targets
                ):
                    raise RuntimeError("Speech targets changed")
                for state in states:
                    if state.connection != "connected" or state.closing:
                        raise RuntimeError(
                            f"Camera route is not connected: {state.camera_id}"
                        )
                self.router.begin_speech(playback)
            async with asyncio.timeout(len(pcm) / 16000 + 15):
                await playback.finished.wait()
            if playback.error:
                raise RuntimeError(playback.error)
        finally:
            self.speech_cleanup = asyncio.create_task(self._finish_speech(playback))
            await asyncio.shield(self.speech_cleanup)

    async def _finish_speech(self, playback: SpeechPlayback) -> None:
        async with self.lock:
            if self.speech_playback is playback:
                self.speech_playback = None
                self.speech_cameras = ()
                self.router.clear_speech()
                if not self.closing:
                    await self._reconcile(set())

    async def close(self) -> None:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self) -> None:
        self.closing = True
        if self.speech_playback is not None:
            self.speech_playback.fail("Camera server is stopping")
        if self.speech_cleanup is not None:
            await asyncio.shield(self.speech_cleanup)
        async with self.lock:
            stops = [self._ensure_stop(state) for state in self.cameras.values()]
            if stops:
                await asyncio.gather(*(asyncio.shield(task) for task in stops))
            self.cameras.clear()
            await self.router.close()
