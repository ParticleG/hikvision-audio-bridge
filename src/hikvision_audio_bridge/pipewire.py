"""PipeWire's native FIFO modules; no default-device or global config changes."""

import asyncio
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

FRAME_BYTES = 320  # 20 ms of 8 kHz mono PCM16.
DEVICE_NAMES = {
    "speaker": "hikvision_audio_speaker",
    "microphone": "hikvision_audio_microphone",
}


async def pactl(*arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "pactl",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(5):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"PipeWire operation failed: {stderr.decode().strip()}")
    return stdout.decode().strip()


@dataclass
class Device:
    module: str
    descriptor: int
    path: Path
    pending: bytearray = field(default_factory=bytearray)

    def flush(self) -> None:
        self.pending.clear()
        while True:
            try:
                if not os.read(self.descriptor, 4096):
                    break
            except BlockingIOError:
                break


class AudioDevices:
    def __init__(self, runtime: Path):
        self.runtime = runtime
        self.devices: dict[str, Device] = {}

    async def recover(self) -> None:
        """Reclaim only this user's service FIFOs after an unclean exit."""
        # This pactl version omits module IDs from JSON; the short format has IDs.
        modules = await pactl("list", "short", "modules")
        owned = {f"file={self.runtime / (name + '.pcm')}" for name in DEVICE_NAMES}
        for line in modules.splitlines():
            fields = line.split("\t", 2)
            if len(fields) != 3:
                continue  # Native PipeWire modules can have multiline arguments.
            identifier, name, arguments = fields
            if name in ("module-pipe-sink", "module-pipe-source"):
                if owned.intersection(shlex.split(arguments)):
                    await pactl("unload-module", identifier)

    async def enable(self, direction: str) -> None:
        if direction in self.devices:
            return
        kind = "sink" if direction == "speaker" else "source"
        path = self.runtime / f"{direction}.pcm"
        path.unlink(missing_ok=True)
        os.mkfifo(path, 0o600)
        descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            module = await pactl(
                "load-module",
                f"module-pipe-{kind}",
                f"file={path}",
                f"{kind}_name={DEVICE_NAMES[direction]}",
                "format=s16le",
                "rate=8000",
                "channels=1",
                "channel_map=mono",
                f'{kind}_properties="device.description=Hikvision-{direction.title()} '
                'node.virtual=true priority.session=0"',
            )
        except BaseException:
            os.close(descriptor)
            path.unlink(missing_ok=True)
            raise
        self.devices[direction] = Device(module, descriptor, path)

    async def disable(self, direction: str) -> None:
        device = self.devices.get(direction)
        if device is None:
            return
        await pactl("unload-module", device.module)
        del self.devices[direction]
        os.close(device.descriptor)
        device.path.unlink(missing_ok=True)

    def flush(self) -> None:
        for device in self.devices.values():
            device.flush()

    def read_speaker(self) -> bytes:
        device = self.devices["speaker"]
        while True:
            try:
                chunk = os.read(device.descriptor, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            device.pending.extend(chunk)
        # Bound live latency. Never replay a long backlog after a network stall.
        if len(device.pending) > FRAME_BYTES * 5:
            del device.pending[: -FRAME_BYTES * 5]
        frame = bytes(device.pending[:FRAME_BYTES])
        del device.pending[:FRAME_BYTES]
        return frame.ljust(FRAME_BYTES, b"\0")

    def write_microphone(self, pcm: bytes) -> None:
        device = self.devices["microphone"]
        try:
            os.write(device.descriptor, pcm)
        except BlockingIOError:
            # No recording application is draining the source; keep only live data.
            device.flush()
            os.write(device.descriptor, pcm)

    async def close(self) -> None:
        errors = []
        for direction in list(self.devices):
            try:
                await self.disable(direction)
            except Exception as error:
                errors.append(error)
                device = self.devices.pop(direction)
                os.close(device.descriptor)
        if errors:
            raise ExceptionGroup("Audio device cleanup failed", errors)
