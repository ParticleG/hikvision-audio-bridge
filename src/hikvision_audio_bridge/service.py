"""Live configuration, independent camera sessions and audio routing."""

import asyncio
import contextlib
from dataclasses import asdict, dataclass, field

from .camera import CameraSession
from .config import Camera, Paths, Settings, validate_id
from .credentials import Credentials
from .pipewire import AudioDevices, device_name
from .router import AudioRouter, SpeechPlayback, camera_routes
from .tts import DEFAULT_VOICE, TTSError, synthesize


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
    connection: str = "idle"
    error: str | None = None
    cleanup_error: str | None = None
    codec: str | None = None
    sent_bytes: int = 0
    received_bytes: int = 0
    closing: bool = False


class Bridge:
    def __init__(self, paths: Paths, credentials: Credentials | None = None):
        self.paths = paths
        self.credentials = credentials or Credentials()
        self.devices = AudioDevices(paths.runtime)
        self.router = AudioRouter(lambda: self.devices)
        self.settings = Settings()
        self.lock = asyncio.Lock()
        self.cameras: dict[str, _CameraState] = {}
        self.tts_state = "idle"
        self.tts_error: str | None = None
        self.tts_devices: list[str] = []
        self.speech_task: asyncio.Task | None = None
        self.speech_generation = 0
        self.speech_playback: SpeechPlayback | None = None
        self.speech_devices: tuple[str, ...] = ()
        self.speech_cameras: tuple[str, ...] = ()
        self.close_task: asyncio.Task | None = None

    def status(self) -> dict:
        cameras = {}
        for identifier in self.settings.cameras:
            state = self.cameras.get(identifier)
            if state is None:
                cameras[identifier] = {
                    "connection": "idle",
                    "error": None,
                    "cleanup_error": None,
                    "codec": None,
                    "sent_bytes": 0,
                    "received_bytes": 0,
                }
                continue
            session = state.session
            codec = (session.codec or None) if session is not None else state.codec
            cameras[identifier] = {
                "connection": state.connection,
                "error": state.error,
                "cleanup_error": state.cleanup_error,
                "codec": codec,
                "sent_bytes": (
                    session.sent_bytes if session is not None else state.sent_bytes
                ),
                "received_bytes": (
                    session.received_bytes
                    if session is not None
                    else state.received_bytes
                ),
            }
        return {
            "settings": asdict(self.settings),
            "cameras": cameras,
            "devices": {
                identifier: {"name": device_name(identifier)}
                for identifier in self.settings.devices
            },
            "tts": {
                "state": self.tts_state,
                "error": self.tts_error,
                "devices": list(self.tts_devices),
            },
        }

    async def apply(
        self,
        settings: Settings,
        *,
        persist: bool = True,
        restart: bool = False,
        camera_id: str | None = None,
    ) -> dict:
        if not isinstance(settings, Settings):
            raise TypeError("settings must be a Settings instance")
        if camera_id is not None:
            validate_id(camera_id)
        async with self.lock:
            previous = self.settings
            restart_ids = self._restart_ids(previous, settings, restart, camera_id)
            if persist:
                self.paths.save(settings)
            if self._speech_affected(previous, settings, restart_ids):
                self._cancel_speech()
            self.settings = settings
            self.router.configure(settings)

            for identifier in settings.cameras:
                if identifier not in self.cameras:
                    self.cameras[identifier] = _CameraState(
                        identifier, settings.cameras[identifier]
                    )

            pending = []
            for state in self.cameras.values():
                if state.stop_task is not None or (
                    state.worker is not None and state.worker.done()
                ):
                    pending.append(self._ensure_stop(state))
            if pending:
                await asyncio.gather(*(asyncio.shield(task) for task in pending))

            speaker, microphone = camera_routes(settings)
            stop_ids: set[str] = set()
            reset_ids: set[str] = set()
            for identifier, state in self.cameras.items():
                if identifier not in settings.cameras:
                    stop_ids.add(identifier)
                    continue
                if state.worker is None and state.connection in (
                    "connecting",
                    "connected",
                ):
                    state.connection = "idle"
                if (
                    identifier in restart_ids
                    or state.camera != settings.cameras[identifier]
                ):
                    stop_ids.add(identifier)
                    reset_ids.add(identifier)
                elif not (speaker.get(identifier) or microphone.get(identifier)):
                    if state.worker is not None:
                        stop_ids.add(identifier)

            stops = [
                self._ensure_stop(self.cameras[identifier])
                for identifier in sorted(stop_ids)
            ]
            if stops:
                await asyncio.gather(*(asyncio.shield(task) for task in stops))

            for identifier in list(self.cameras):
                if identifier not in settings.cameras:
                    del self.cameras[identifier]
            for identifier in reset_ids:
                state = self.cameras.get(identifier)
                if state is None:
                    continue
                state.camera = settings.cameras[identifier]
                state.error = None
                state.codec = None
                state.sent_bytes = 0
                state.received_bytes = 0
                state.connection = "idle"
                state.closing = False

            # A cleanup task may have reset router outputs after the first
            # configuration; recreate any missing path before streams restart.
            self.router.configure(settings)

            for identifier, state in self.cameras.items():
                state.desired_speaker = bool(speaker.get(identifier))
                state.desired_microphone = bool(microphone.get(identifier))
                state.speaker_devices = speaker.get(identifier, ())
                state.microphone_devices = microphone.get(identifier, ())

            syncs = [
                self._sync_streams(state)
                for identifier, state in self.cameras.items()
                if identifier in settings.cameras
                and state.worker is not None
                and identifier not in stop_ids
                and not state.closing
            ]
            if syncs:
                await asyncio.gather(*syncs)

            await self._reconcile_devices(settings)

            if any(device.enabled for device in settings.devices.values()):
                self.router.start()
            else:
                await self.router.close()

            starts = []
            for identifier, state in self.cameras.items():
                if identifier not in settings.cameras:
                    continue
                if not (state.desired_speaker or state.desired_microphone):
                    continue
                if identifier in reset_ids:
                    starts.append(identifier)
                elif state.worker is None and state.connection == "idle":
                    starts.append(identifier)
            for identifier in starts:
                self._start_camera(
                    self.cameras[identifier], settings.cameras[identifier]
                )
            return self.status()

    def _restart_ids(
        self,
        previous: Settings,
        settings: Settings,
        restart: bool,
        camera_id: str | None,
    ) -> set[str]:
        if not restart:
            return set()
        if camera_id is not None:
            if camera_id not in settings.cameras and camera_id not in previous.cameras:
                raise ValueError("Unknown camera ID")
            return {camera_id}
        return set(settings.cameras) | set(previous.cameras)

    def _speech_affected(
        self, previous: Settings, settings: Settings, restart_ids: set[str]
    ) -> bool:
        if (
            self.speech_task is None
            or self.speech_task.done()
            or not self.speech_cameras
        ):
            return False
        for identifier in self.speech_devices:
            before = previous.devices.get(identifier)
            after = settings.devices.get(identifier)
            if before is None or after is None:
                return True
            if not after.enabled or after.direction != "speaker":
                return True
            if before.cameras != after.cameras:
                return True
        old_speaker, _ = camera_routes(previous)
        new_speaker, _ = camera_routes(settings)
        for identifier in self.speech_cameras:
            if previous.cameras.get(identifier) != settings.cameras.get(identifier):
                return True
            if old_speaker.get(identifier) != new_speaker.get(identifier):
                return True
        return bool(set(self.speech_cameras) & restart_ids)

    async def _reconcile_devices(self, settings: Settings) -> None:
        await self.devices.settle()
        operations = []
        for identifier, device in settings.devices.items():
            if device.enabled:
                operations.append(self.devices.enable(identifier, device))
        for identifier in list(self.devices.devices):
            device = settings.devices.get(identifier)
            if device is None or not device.enabled:
                operations.append(self.devices.disable(identifier))
        if operations:
            await asyncio.gather(*operations)

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
                    if state.closing:
                        return
                    if not (state.desired_speaker or state.desired_microphone):
                        return
                    if state.session is not session:
                        return
                    await self._sync_streams_locked(state)
                    state.connection = "connected"
                    state.changed.clear()
                    waiter = asyncio.create_task(state.changed.wait())
                    watched = dict(state.streams)
                completed, _ = await asyncio.wait(
                    [waiter, *watched.values()],
                    return_when=asyncio.FIRST_COMPLETED,
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
        if (
            self.speech_task is not None
            and not self.speech_task.done()
            and state.camera_id in self.speech_cameras
        ):
            self._cancel_speech(
                f"Speech stopped: camera '{state.camera_id}' is unavailable"
            )
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

    async def speak(
        self,
        text: str,
        device_ids: list[str],
        voice: str = DEFAULT_VOICE,
    ) -> dict:
        async with self.lock:
            if self.speech_task is not None and not self.speech_task.done():
                raise ValueError("Speech synthesis is already in progress")
            if not isinstance(device_ids, (list, tuple)) or not device_ids:
                raise ValueError("Select at least one speaker device")
            selected: list[str] = []
            for identifier in device_ids:
                if not isinstance(identifier, str):
                    raise ValueError("Device IDs must be strings")
                if identifier not in selected:
                    selected.append(identifier)
            targets: list[str] = []
            for identifier in selected:
                device = self.settings.devices.get(identifier)
                if device is None:
                    raise ValueError(f"Unknown audio device: {identifier}")
                if not device.enabled:
                    raise ValueError(f"Audio device is disabled: {identifier}")
                if device.direction != "speaker":
                    raise ValueError(f"Audio device is not a speaker: {identifier}")
                if not device.cameras:
                    raise ValueError(
                        f"Speaker has no connected camera routes: {identifier}"
                    )
                for camera_id in device.cameras:
                    state = self.cameras.get(camera_id)
                    if (
                        state is None
                        or state.connection != "connected"
                        or not state.desired_speaker
                        or state.session is None
                        or not state.session.opened
                        or "speaker" not in state.streams
                        or state.streams["speaker"].done()
                    ):
                        raise ValueError(f"Camera route is not connected: {camera_id}")
                    if camera_id not in targets:
                        targets.append(camera_id)
            self.speech_generation += 1
            token = self.speech_generation
            self.speech_task = asyncio.create_task(
                self._speak(token, text, voice, tuple(targets))
            )
            self.speech_devices = tuple(selected)
            self.speech_cameras = tuple(targets)
            self.speech_playback = None
            self.router.clear_speech()
            self.tts_state = "synthesizing"
            self.tts_error = None
            self.tts_devices = selected
            return self.status()

    async def _speak(
        self,
        token: int,
        text: str,
        voice: str,
        targets: tuple[str, ...],
    ) -> None:
        playback = None
        try:
            try:
                pcm = await synthesize(text, voice)
            except asyncio.CancelledError:
                raise
            except (TTSError, TypeError, ValueError) as error:
                await self._speech_error(token, str(error) or type(error).__name__)
                return
            except Exception:
                await self._speech_error(token, "Speech synthesis failed")
                return
            if not pcm:
                await self._speech_error(token, "Speech synthesis returned no audio")
                return
            async with self.lock:
                if not self._speech_current(token):
                    return
                for camera_id in targets:
                    state = self.cameras.get(camera_id)
                    if (
                        state is None
                        or state.connection != "connected"
                        or not state.desired_speaker
                        or state.session is None
                        or not state.session.opened
                        or "speaker" not in state.streams
                        or state.streams["speaker"].done()
                    ):
                        self.tts_state = "error"
                        self.tts_error = (
                            f"Speech target is no longer connected: {camera_id}"
                        )
                        self.speech_generation += 1
                        self.speech_cameras = ()
                        self.router.clear_speech()
                        self.speech_playback = None
                        return
                playback = SpeechPlayback(pcm, targets)
                self.speech_playback = playback
                self.router.begin_speech(playback)
                self.tts_state = "playing"
            await playback.finished.wait()
            async with self.lock:
                if not self._speech_current(token):
                    return
                if self.speech_playback is not playback:
                    return
                self.router.clear_speech()
                self.speech_playback = None
                self.speech_cameras = ()
                self.tts_state = "idle"
                self.tts_error = None
                self.tts_devices = []
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._speech_error(token, "Speech playback failed")
        finally:
            if self.speech_task is asyncio.current_task():
                self.speech_task = None

    def _speech_current(self, token: int) -> bool:
        return (
            token == self.speech_generation
            and self.speech_task is asyncio.current_task()
        )

    async def _speech_error(self, token: int, message: str) -> None:
        async with self.lock:
            if not self._speech_current(token):
                return
            self.speech_generation += 1
            self.speech_cameras = ()
            self.router.clear_speech()
            self.speech_playback = None
            self.tts_state = "error"
            self.tts_error = message

    def _cancel_speech(self, error: str | None = None) -> None:
        task = self.speech_task
        self.speech_generation += 1
        self.speech_playback = None
        self.speech_devices = ()
        self.speech_cameras = ()
        self.router.clear_speech()
        self.tts_state = "error" if error else "idle"
        self.tts_error = error
        if error is None:
            self.tts_devices = []
        if task is not None and not task.done() and not task.cancelling():
            task.cancel()

    async def stop_speech(self) -> dict:
        async with self.lock:
            task = self.speech_task
            self._cancel_speech()
            if task is not None:
                await asyncio.shield(_settle(task))
            return self.status()

    async def close(self) -> None:
        task = self.close_task
        if task is None:
            task = asyncio.create_task(self._close())
            self.close_task = task
        await asyncio.shield(task)

    async def _close(self) -> None:
        async with self.lock:
            speech = self.speech_task
            self._cancel_speech()
            if speech is not None:
                await asyncio.gather(speech, return_exceptions=True)
            states = list(self.cameras.values())
            stops = [self._ensure_stop(state) for state in states]
            if stops:
                await asyncio.gather(*(asyncio.shield(task) for task in stops))
            for state in states:
                self.router.reset_camera(state.camera_id)
            self.cameras.clear()
            await self.router.close()
            await self.devices.close()
