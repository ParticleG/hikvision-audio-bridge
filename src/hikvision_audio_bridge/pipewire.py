"""Owned, independently named PipeWire FIFO devices and bounded PCM I/O."""

import asyncio
import contextlib
import os
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .config import AudioDevice, validate_id

FRAME_BYTES = 320  # 20 ms of 8 kHz mono PCM16.
BUFFER_BYTES = FRAME_BYTES * 5
SILENCE = bytes(FRAME_BYTES)


def device_name(identifier: str) -> str:
    return f"hikvision_audio_{validate_id(identifier)}"


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


def _quote_argument(value: str) -> str:
    """Quote one PulseAudio argument, including nested property-list values."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class Device:
    module: str
    descriptor: int
    path: Path
    settings: AudioDevice
    pending: bytearray = field(default_factory=bytearray)

    def flush(self) -> None:
        self.pending.clear()
        # Bound work even if another process continuously writes into the FIFO.
        for _ in range(16):
            try:
                if not os.read(self.descriptor, 4096):
                    break
            except OSError:
                break


class AudioDevices:
    def __init__(self, runtime: Path):
        self.runtime = runtime
        self.devices: dict[str, Device] = {}
        self._operations: dict[str, asyncio.Task[None]] = {}

    async def recover(self) -> None:
        """Reclaim only matching bridge names and exact private FIFO paths."""
        modules = await pactl("list", "short", "modules")
        for line in modules.splitlines():
            fields = line.split("\t", 2)
            if len(fields) != 3:
                continue
            module, kind, arguments = fields
            if (
                not module.isascii()
                or not module.isdecimal()
                or kind not in ("module-pipe-sink", "module-pipe-source")
            ):
                continue
            try:
                tokens = shlex.split(arguments)
            except ValueError:
                continue
            values = {}
            duplicate = False
            for token in tokens:
                key, separator, value = token.partition("=")
                if separator:
                    duplicate |= key in values
                    values[key] = value
            if duplicate:
                continue
            path = Path(values.get("file", ""))
            identifier = path.stem
            try:
                technical_name = device_name(identifier)
            except ValueError:
                continue
            name_key = "sink_name" if kind == "module-pipe-sink" else "source_name"
            if (
                values.get("file") == str(self.runtime / f"{identifier}.pcm")
                and values.get(name_key) == technical_name
            ):
                await pactl("unload-module", module)

    async def _settle(self, identifier: str) -> None:
        task = self._operations.get(identifier)
        if task is None:
            return
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._operations.get(identifier) is task:
                del self._operations[identifier]

    async def settle(self) -> None:
        """Finish owned operations even when their original caller was cancelled."""
        for identifier in list(self._operations):
            await self._settle(identifier)

    async def enable(self, identifier: str, settings: AudioDevice) -> None:
        validate_id(identifier)
        await self._settle(identifier)
        current = self.devices.get(identifier)
        if current is not None:
            if (current.settings.name, current.settings.direction) == (
                settings.name,
                settings.direction,
            ):
                current.settings = settings
                return
            await self.disable(identifier)
        self._operations[identifier] = asyncio.create_task(
            self._enable(identifier, settings)
        )
        await self._settle(identifier)

    async def _enable(self, identifier: str, settings: AudioDevice) -> None:
        kind = "sink" if settings.direction == "speaker" else "source"
        path = self.runtime / f"{identifier}.pcm"
        try:
            info = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError(f"Refusing to replace an unowned audio FIFO: {path}")
            path.unlink()
        os.mkfifo(path, 0o600)
        try:
            descriptor = os.open(
                path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
            )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        properties = (
            f"device.description={_quote_argument(settings.name)} "
            "node.virtual=true priority.session=0"
        )
        try:
            module = await pactl(
                "load-module",
                f"module-pipe-{kind}",
                f"file={_quote_argument(str(path))}",
                f"{kind}_name={device_name(identifier)}",
                "format=s16le",
                "rate=8000",
                "channels=1",
                "channel_map=mono",
                f"{kind}_properties={_quote_argument(properties)}",
            )
            if not module.isascii() or not module.isdecimal():
                raise RuntimeError("PipeWire returned an invalid module ID")
        except BaseException:
            os.close(descriptor)
            path.unlink(missing_ok=True)
            raise
        self.devices[identifier] = Device(module, descriptor, path, settings)

    async def disable(self, identifier: str) -> None:
        await self._settle(identifier)
        if identifier not in self.devices:
            return
        self._operations[identifier] = asyncio.create_task(self._disable(identifier))
        await self._settle(identifier)

    async def _disable(self, identifier: str) -> None:
        device = self.devices[identifier]
        await pactl("unload-module", device.module)
        del self.devices[identifier]
        os.close(device.descriptor)
        device.path.unlink(missing_ok=True)

    def flush(self, identifiers: set[str] | None = None) -> None:
        for identifier, device in self.devices.items():
            if identifiers is None or identifier in identifiers:
                device.flush()

    def read_speaker(self, identifier: str) -> bytes:
        device = self.devices[identifier]
        for _ in range(16):
            try:
                chunk = os.read(device.descriptor, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            device.pending.extend(chunk)
            if len(device.pending) > BUFFER_BYTES:
                excess = len(device.pending) - BUFFER_BYTES
                del device.pending[: excess + excess % 2]
        length = min(FRAME_BYTES, len(device.pending) // 2 * 2)
        frame = bytes(device.pending[:length])
        del device.pending[:length]
        return frame.ljust(FRAME_BYTES, b"\0")

    def write_microphone(self, identifier: str, pcm: bytes) -> None:
        device = self.devices[identifier]
        try:
            os.write(device.descriptor, pcm)
        except BlockingIOError:
            # A dormant recorder must never create an unbounded audio backlog.
            device.flush()
            with contextlib.suppress(BlockingIOError):
                os.write(device.descriptor, pcm)

    async def close(self) -> None:
        errors = []
        for identifier in list(self._operations):
            try:
                await self._settle(identifier)
            except Exception as error:
                errors.append(error)
        identifiers = list(self.devices)
        if identifiers:
            results = await asyncio.gather(
                *(self.disable(identifier) for identifier in identifiers),
                return_exceptions=True,
            )
            for identifier, result in zip(identifiers, results, strict=True):
                if not isinstance(result, Exception):
                    continue
                errors.append(result)
                device = self.devices.pop(identifier, None)
                if device is not None:
                    try:
                        os.close(device.descriptor)
                    except OSError as error:
                        errors.append(error)
        if errors:
            raise ExceptionGroup("Audio device cleanup failed", errors)
