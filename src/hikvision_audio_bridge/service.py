"""Live configuration, independent device switches and camera session ownership."""

import asyncio
import contextlib
from dataclasses import asdict

from .camera import CameraSession
from .config import Paths, Settings
from .credentials import Credentials
from .pipewire import DEVICE_NAMES, AudioDevices


class Bridge:
    def __init__(self, paths: Paths, credentials: Credentials | None = None):
        self.paths = paths
        self.credentials = credentials or Credentials()
        self.devices = AudioDevices(paths.runtime)
        self.settings = Settings()
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.cleanup_task: asyncio.Task[None] | None = None
        self.streams: dict[str, asyncio.Task] = {}
        self.session: CameraSession | None = None
        self.connection = "idle"
        self.error: str | None = None
        self.cleanup_error: str | None = None

    def status(self) -> dict:
        return {
            **asdict(self.settings),
            "connection": self.connection,
            "error": self.error,
            "cleanup_error": self.cleanup_error,
            "devices": {name: DEVICE_NAMES[name] for name in self.devices.devices},
            "codec": self.session.codec if self.session else None,
            "sent_bytes": self.session.sent_bytes if self.session else 0,
            "received_bytes": self.session.received_bytes if self.session else 0,
        }

    async def apply(
        self, settings: Settings, *, persist: bool = True, restart: bool = False
    ) -> dict:
        async with self.lock:
            try:
                if (
                    settings.camera != self.settings.camera
                    or restart
                    or not (settings.speaker or settings.microphone)
                ):
                    await self._stop_worker()
                    self.devices.flush()
                for name in DEVICE_NAMES:
                    if not getattr(settings, name):
                        await self._stop_stream(name)
                        await self.devices.disable(name)
                for name in DEVICE_NAMES:
                    if getattr(settings, name):
                        await self.devices.enable(name)
                self.settings = settings
                if persist:
                    self.paths.save(settings)
                if settings.speaker or settings.microphone:
                    if self.worker is None or self.worker.done():
                        await self._stop_worker()
                        self.devices.flush()
                        self.connection = "connecting"
                        self.error = None
                        self.worker = asyncio.create_task(self._run_camera())
                else:
                    self.connection = "idle"
                return self.status()
            finally:
                self.changed.set()

    async def _stop_stream(self, name: str) -> None:
        task = self.streams.pop(name, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _stop_worker(self) -> None:
        worker = self.worker
        if worker is None:
            return
        if self.cleanup_task is None and not worker.cancelling():
            worker.cancel()
        # Caller cancellation must not become a second cancellation of cleanup.
        (outcome,) = await asyncio.shield(
            asyncio.gather(worker, return_exceptions=True)
        )
        if self.cleanup_task is not None:
            await asyncio.shield(self.cleanup_task)
        if isinstance(outcome, Exception):
            raise outcome
        self.worker = None
        self.cleanup_task = None

    async def _run_camera(self) -> None:
        camera = self.settings.camera
        if camera is None:
            return
        session = None
        waiter = None
        try:
            password = await asyncio.to_thread(self.credentials.get, camera)
            session = CameraSession(camera, password)
            del password
            await session.open()
            self.session = session
            while True:
                async with self.lock:
                    self.changed.clear()
                    if self.settings.speaker and "speaker" not in self.streams:
                        self.streams["speaker"] = asyncio.create_task(
                            session.speaker(self.devices.read_speaker)
                        )
                    if self.settings.microphone and "microphone" not in self.streams:
                        self.streams["microphone"] = asyncio.create_task(
                            session.microphone(self.devices.write_microphone)
                        )
                    self.connection = "connected"
                    waiter = asyncio.create_task(self.changed.wait())
                    watched = dict(self.streams)
                completed, _ = await asyncio.wait(
                    [waiter, *watched.values()], return_when=asyncio.FIRST_COMPLETED
                )
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
                waiter = None
                for name, task in watched.items():
                    if task in completed and self.streams.get(name) is task:
                        await task
                        raise RuntimeError(f"Camera {name} stream stopped unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = str(error) or type(error).__name__
            self.connection = "error"
        finally:
            self.cleanup_task = asyncio.create_task(
                self._finish_camera(session, waiter)
            )
            await asyncio.shield(self.cleanup_task)

    async def _finish_camera(
        self, session: CameraSession | None, waiter: asyncio.Task | None
    ) -> None:
        if waiter is not None:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        for name in list(self.streams):
            await self._stop_stream(name)
        if session is not None:
            try:
                await session.close()
                self.cleanup_error = None
            except Exception as error:
                self.cleanup_error = str(error) or type(error).__name__
        self.session = None
        self.devices.flush()
        self.connection = "error" if self.error or self.cleanup_error else "idle"

    async def close(self) -> None:
        async with self.lock:
            await self._stop_worker()
            await self.devices.close()
