"""Bounded server-side fan-out, grouped capture and equal-gain PCM mixing."""

import asyncio
import struct
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .audio import BUFFER_BYTES, BUFFER_FRAMES, FRAME_BYTES, FRAME_SECONDS, SILENCE
from .config import AudioDevice, RoutingSettings


class AudioIO(Protocol):
    def read_speaker(self, identifier: str) -> bytes: ...

    def write_microphone(self, identifier: str, pcm: bytes) -> None: ...

    def flush(self, identifiers: set[str] | None = None) -> None: ...


def camera_routes(
    settings: RoutingSettings,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    speaker: dict[str, list[str]] = {}
    microphone: dict[str, list[str]] = {}
    for identifier, device in settings.devices.items():
        if device.enabled:
            routes = speaker if device.direction == "speaker" else microphone
            for camera_id in device.cameras:
                routes.setdefault(camera_id, []).append(identifier)
    return (
        {key: tuple(sorted(value)) for key, value in speaker.items()},
        {key: tuple(sorted(value)) for key, value in microphone.items()},
    )


def _frame(pcm: bytes) -> bytes:
    if len(pcm) >= FRAME_BYTES:
        return pcm[:FRAME_BYTES]
    return pcm[: len(pcm) // 2 * 2].ljust(FRAME_BYTES, b"\0")


def mix(frames: list[bytes]) -> bytes:
    """Average active PCM16 frames, ignoring digital silence and avoiding clipping."""
    active = [_frame(frame) for frame in frames if any(frame)]
    if not active:
        return SILENCE
    if len(active) == 1:
        return active[0]
    samples = [struct.unpack("<160h", frame) for frame in active]
    gain = len(samples)
    return struct.pack(
        "<160h",
        *(int(sum(row[index] for row in samples) / gain) for index in range(160)),
    )


class SpeechPlayback:
    def __init__(self, pcm: bytes, targets: tuple[str, ...]):
        self.pcm = pcm
        self.targets = tuple(dict.fromkeys(targets))
        self.frame_count = (len(pcm) + FRAME_BYTES - 1) // FRAME_BYTES
        self.queued = dict.fromkeys(self.targets, 0)
        self.delivered = dict.fromkeys(self.targets, 0)
        self.finished = asyncio.Event()
        self.error: str | None = None
        if not self.targets or not self.frame_count:
            self.finished.set()

    def frame(self, index: int) -> bytes:
        start = index * FRAME_BYTES
        return self.pcm[start : start + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")

    def delivered_frame(self, camera_id: str) -> None:
        self.delivered[camera_id] += 1
        if all(self.delivered[target] >= self.frame_count for target in self.targets):
            self.finished.set()

    def fail(self, message: str) -> None:
        self.error = message
        self.finished.set()


@dataclass(frozen=True)
class SpeechToken:
    playback: SpeechPlayback
    camera_id: str


class AudioRouter:
    def __init__(self, devices: Callable[[], AudioIO]):
        self._devices = devices
        self.routes: dict[str, AudioDevice] = {}
        self.camera_speakers: dict[str, tuple[str, ...]] = {}
        self.microphone_cameras: dict[str, tuple[str, ...]] = {}
        self.outputs: dict[str, deque[bytes]] = {}
        self.camera_inputs: dict[str, bytearray] = {}
        self.pending_delivery: dict[str, SpeechToken] = {}
        self.speech: SpeechPlayback | None = None
        self.task: asyncio.Task | None = None

    def configure(self, settings: RoutingSettings) -> None:
        speaker, microphone = camera_routes(settings)
        changed = {
            identifier
            for identifier in self.routes.keys() | settings.devices.keys()
            if self.routes.get(identifier) != settings.devices.get(identifier)
        }
        if changed:
            self._devices().flush(changed)
        for camera_id in self.camera_speakers.keys() | speaker.keys():
            if self.camera_speakers.get(camera_id) != speaker.get(camera_id):
                self.outputs.pop(camera_id, None)
        self.routes = settings.devices
        self.camera_speakers = speaker
        self.microphone_cameras = {
            identifier: device.cameras
            for identifier, device in settings.devices.items()
            if device.enabled and device.direction == "microphone"
        }
        for camera_id in set(self.camera_inputs) - microphone.keys():
            del self.camera_inputs[camera_id]
        targets = set(speaker)
        if self.speech is not None:
            targets.update(self.speech.targets)
        for camera_id in targets:
            self.outputs.setdefault(camera_id, deque(maxlen=BUFFER_FRAMES))

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            self.tick()
            deadline += FRAME_SECONDS
            delay = deadline - loop.time()
            if delay <= 0:
                deadline = loop.time()
            await asyncio.sleep(max(0, delay))

    def tick(self) -> None:
        devices = self._devices()
        speakers = {
            identifier: devices.read_speaker(identifier)
            for identifier, device in self.routes.items()
            if device.enabled and device.direction == "speaker"
        }
        for camera_id, identifiers in self.camera_speakers.items():
            output = self.outputs.setdefault(camera_id, deque(maxlen=BUFFER_FRAMES))
            output.append(mix([speakers[identifier] for identifier in identifiers]))
        cameras = {
            camera for targets in self.microphone_cameras.values() for camera in targets
        }
        captured = {camera: self._camera_frame(camera) for camera in cameras}
        for identifier, targets in self.microphone_cameras.items():
            devices.write_microphone(
                identifier, mix([captured[camera] for camera in targets])
            )

    def _camera_frame(self, camera_id: str) -> bytes:
        pending = self.camera_inputs.get(camera_id)
        if not pending:
            return SILENCE
        length = min(FRAME_BYTES, len(pending) // 2 * 2)
        pcm = bytes(pending[:length])
        del pending[:length]
        return pcm.ljust(FRAME_BYTES, b"\0")

    def consume_camera(self, camera_id: str, pcm: bytes) -> None:
        pending = self.camera_inputs.setdefault(camera_id, bytearray())
        pending.extend(pcm)
        if len(pending) > BUFFER_BYTES:
            excess = len(pending) - BUFFER_BYTES
            del pending[: excess + excess % 2]

    def read_camera(self, camera_id: str) -> bytes:
        """Pull TTS at upload pace; live-route edits never discard speech progress."""
        token = self.pending_delivery.pop(camera_id, None)
        if token is not None:
            token.playback.delivered_frame(camera_id)
        output = self.outputs.get(camera_id)
        live = output.popleft() if output else SILENCE
        playback = self.speech
        if playback is not None and camera_id in playback.targets:
            index = playback.queued[camera_id]
            if index < playback.frame_count:
                playback.queued[camera_id] = index + 1
                self.pending_delivery[camera_id] = SpeechToken(playback, camera_id)
                return mix([live, playback.frame(index)])
        return live

    def begin_speech(self, playback: SpeechPlayback) -> None:
        self.clear_speech()
        self.speech = playback

    def clear_speech(self) -> None:
        self.speech = None
        self.pending_delivery.clear()

    def reset_camera(self, camera_id: str) -> None:
        self.outputs.pop(camera_id, None)
        self.camera_inputs.pop(camera_id, None)
        self.pending_delivery.pop(camera_id, None)
        if self.speech is not None and camera_id in self.speech.targets:
            self.speech.fail(f"Speech stopped: camera '{camera_id}' is unavailable")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.outputs.clear()
        self.camera_inputs.clear()
        self.clear_speech()
