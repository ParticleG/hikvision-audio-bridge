"""Bounded 20 ms fan-out and equal-gain mixing between devices and cameras."""

import asyncio
import struct
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .config import Settings
from .pipewire import BUFFER_BYTES, FRAME_BYTES, SILENCE

FRAME_SECONDS = 0.02
MAX_OUTPUT_FRAMES = 5


class AudioIO(Protocol):
    def read_speaker(self, identifier: str) -> bytes: ...

    def write_microphone(self, identifier: str, pcm: bytes) -> None: ...

    def flush(self, identifiers: set[str] | None = None) -> None: ...


def camera_routes(
    settings: Settings,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """Return enabled speaker and microphone IDs routed to each camera."""
    speaker: dict[str, list[str]] = {}
    microphone: dict[str, list[str]] = {}
    for identifier in sorted(settings.devices):
        device = settings.devices[identifier]
        if not device.enabled:
            continue
        routes = speaker if device.direction == "speaker" else microphone
        for camera_id in device.cameras:
            routes.setdefault(camera_id, []).append(identifier)
    return (
        {key: tuple(value) for key, value in speaker.items()},
        {key: tuple(value) for key, value in microphone.items()},
    )


def enabled_devices(settings: Settings, direction: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            identifier
            for identifier, device in settings.devices.items()
            if device.enabled and device.direction == direction
        )
    )


def _frame(pcm: bytes) -> bytes:
    if len(pcm) >= FRAME_BYTES:
        return pcm[:FRAME_BYTES]
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return pcm.ljust(FRAME_BYTES, b"\0")


def mix(frames: list[bytes]) -> bytes:
    """Mix active PCM16 frames with equal gain so the result cannot overflow."""
    active = [_frame(frame) for frame in frames if any(frame)]
    if not active:
        return SILENCE
    if len(active) == 1:
        return active[0]
    samples = [struct.unpack("<160h", frame) for frame in active]
    gain = len(samples)
    output = bytearray(FRAME_BYTES)
    for index in range(160):
        total = sum(row[index] for row in samples)
        struct.pack_into("<h", output, index * 2, int(total / gain))
    return bytes(output)


@dataclass(frozen=True)
class SpeechToken:
    playback: "SpeechPlayback"
    camera_id: str


class SpeechPlayback:
    """Synthesized PCM delivered once to every deduplicated target camera."""

    def __init__(self, pcm: bytes, targets: tuple[str, ...]):
        self.pcm = pcm
        self.targets = tuple(dict.fromkeys(targets))
        self.frame_count = (len(pcm) + FRAME_BYTES - 1) // FRAME_BYTES if pcm else 0
        self.queued = dict.fromkeys(self.targets, 0)
        self.delivered = dict.fromkeys(self.targets, 0)
        self.finished = asyncio.Event()
        if not self.targets or not self.frame_count:
            self.finished.set()

    def frame(self, index: int) -> bytes:
        start = index * FRAME_BYTES
        return self.pcm[start : start + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")

    def delivered_frame(self, camera_id: str) -> None:
        self.delivered[camera_id] += 1
        self.check_complete()

    def check_complete(self) -> None:
        if all(
            self.queued[camera_id] >= self.frame_count
            and self.delivered[camera_id] >= self.frame_count
            for camera_id in self.targets
        ):
            self.finished.set()


@dataclass
class Frame:
    pcm: bytes
    token: SpeechToken | None = None


class CameraOutput:
    def __init__(self) -> None:
        self.frames: deque[Frame] = deque()

    def push(self, frame: Frame, *, protected: bool = False) -> bool:
        if len(self.frames) >= MAX_OUTPUT_FRAMES:
            if protected:
                return False
            for index, existing in enumerate(self.frames):
                if existing.token is None:
                    del self.frames[index]
                    break
            else:
                return False
        self.frames.append(frame)
        return True

    def pop(self) -> Frame:
        return self.frames.popleft()

    def clear_speech(self) -> None:
        self.frames = deque(frame for frame in self.frames if frame.token is None)


class AudioRouter:
    def __init__(self, devices: Callable[[], AudioIO]):
        self._devices = devices
        self.speakers: tuple[str, ...] = ()
        self.microphones: tuple[str, ...] = ()
        self.camera_speakers: dict[str, tuple[str, ...]] = {}
        self.camera_microphones: dict[str, tuple[str, ...]] = {}
        self.microphone_cameras: dict[str, tuple[str, ...]] = {}
        self.outputs: dict[str, CameraOutput] = {}
        self.camera_inputs: dict[str, bytearray] = {}
        self.pending_delivery: dict[str, SpeechToken] = {}
        self.speech: SpeechPlayback | None = None
        self.task: asyncio.Task | None = None

    def configure(self, settings: Settings) -> None:
        """Install routes, discarding queued audio for changed camera paths."""
        speaker, microphone = camera_routes(settings)
        microphones = enabled_devices(settings, "microphone")
        old_speaker = self.camera_speakers
        old_microphone = self.camera_microphones
        flush_ids: set[str] = set()
        for camera_id in set(old_speaker) | set(speaker):
            if old_speaker.get(camera_id) != speaker.get(camera_id):
                flush_ids.update(old_speaker.get(camera_id, ()))
                flush_ids.update(speaker.get(camera_id, ()))
        for camera_id in set(old_microphone) | set(microphone):
            if old_microphone.get(camera_id) != microphone.get(camera_id):
                flush_ids.update(old_microphone.get(camera_id, ()))
                flush_ids.update(microphone.get(camera_id, ()))
        if flush_ids:
            try:
                self._devices().flush(flush_ids)
            except (KeyError, OSError):
                pass
        self.speakers = enabled_devices(settings, "speaker")
        self.microphones = microphones
        self.camera_speakers = speaker
        self.camera_microphones = microphone
        self.microphone_cameras = {
            identifier: tuple(
                sorted(
                    camera_id
                    for camera_id, devices in microphone.items()
                    if identifier in devices
                )
            )
            for identifier in microphones
        }
        for camera_id in set(old_speaker) | set(speaker):
            if old_speaker.get(camera_id) != speaker.get(camera_id):
                self.outputs.pop(camera_id, None)
                self.pending_delivery.pop(camera_id, None)
        for camera_id in set(old_microphone) | set(microphone):
            if old_microphone.get(camera_id) != microphone.get(camera_id):
                self.camera_inputs.pop(camera_id, None)
        for camera_id in set(self.camera_inputs) - set(microphone):
            del self.camera_inputs[camera_id]
        for camera_id in speaker:
            self.outputs.setdefault(camera_id, CameraOutput())

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
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                deadline = loop.time()
                await asyncio.sleep(0)

    def tick(self) -> None:
        """One routing quantum: read each speaker and each camera input once."""
        devices = self._devices()
        speaker_frames: dict[str, bytes] = {}
        for identifier in self.speakers:
            try:
                speaker_frames[identifier] = devices.read_speaker(identifier)
            except (KeyError, OSError):
                speaker_frames[identifier] = SILENCE
        routed_cameras: set[str] = set()
        for cameras in self.microphone_cameras.values():
            routed_cameras.update(cameras)
        camera_frames = {
            camera_id: self._camera_frame(camera_id)
            for camera_id in sorted(routed_cameras)
        }
        cameras = set(self.camera_speakers)
        if self.speech is not None:
            cameras.update(self.speech.targets)
        for camera_id in sorted(cameras):
            output = self.outputs.get(camera_id)
            if output is None:
                continue
            sources = [
                speaker_frames.get(identifier, SILENCE)
                for identifier in self.camera_speakers.get(camera_id, ())
            ]
            playback = self.speech
            if playback is not None and camera_id in playback.targets:
                index = playback.queued[camera_id]
                if index < playback.frame_count:
                    if len(output.frames) >= MAX_OUTPUT_FRAMES:
                        continue
                    token = SpeechToken(playback, camera_id)
                    frame = Frame(mix([*sources, playback.frame(index)]), token)
                    if output.push(frame, protected=True):
                        playback.queued[camera_id] = index + 1
                        playback.check_complete()
                    continue
            mixed = mix(sources)
            if any(mixed):
                output.push(Frame(mixed))
        for identifier in self.microphones:
            cameras = self.microphone_cameras.get(identifier, ())
            frames = [camera_frames.get(camera_id, SILENCE) for camera_id in cameras]
            try:
                devices.write_microphone(identifier, mix(frames))
            except (KeyError, OSError):
                pass

    def _camera_frame(self, camera_id: str) -> bytes:
        pending = self.camera_inputs.get(camera_id)
        if pending is None:
            return SILENCE
        length = min(FRAME_BYTES, len(pending) // 2 * 2)
        if not length:
            return SILENCE
        pcm = bytes(pending[:length])
        del pending[:length]
        return pcm.ljust(FRAME_BYTES, b"\0")

    def consume_camera(self, camera_id: str, pcm: bytes) -> None:
        pending = self.camera_inputs.setdefault(camera_id, bytearray())
        pending.extend(pcm)
        if len(pending) > BUFFER_BYTES:
            excess = len(pending) - BUFFER_BYTES
            excess += excess % 2
            del pending[:excess]

    def read_camera(self, camera_id: str) -> bytes:
        """Return the next camera frame; confirms the previous frame was sent."""
        token = self.pending_delivery.pop(camera_id, None)
        if token is not None:
            token.playback.delivered_frame(token.camera_id)
        output = self.outputs.get(camera_id)
        if output is None or not output.frames:
            return SILENCE
        frame = output.pop()
        if frame.token is not None:
            self.pending_delivery[camera_id] = frame.token
        return frame.pcm

    def begin_speech(self, playback: SpeechPlayback) -> None:
        self.clear_speech()
        self.speech = playback
        for camera_id in playback.targets:
            self.outputs.setdefault(camera_id, CameraOutput())

    def clear_speech(self) -> None:
        self.speech = None
        self.pending_delivery.clear()
        for output in self.outputs.values():
            output.clear_speech()

    def reset_camera(self, camera_id: str) -> None:
        self.outputs.pop(camera_id, None)
        self.camera_inputs.pop(camera_id, None)
        self.pending_delivery.pop(camera_id, None)

    async def close(self) -> None:
        task = self.task
        self.task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.outputs.clear()
        self.camera_inputs.clear()
        self.pending_delivery.clear()
        self.speech = None
