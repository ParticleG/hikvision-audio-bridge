import asyncio
import math
import struct

import audioop

from hikvision_audio_bridge.audio import SILENCE


def pcm(value):
    return struct.pack("<160h", *([value] * 160))


def magnitude(data, frequency):
    samples = struct.unpack(f"<{len(data) // 2}h", data)
    real = sum(
        sample * math.cos(2 * math.pi * frequency * index / 8000)
        for index, sample in enumerate(samples)
    )
    imaginary = sum(
        sample * math.sin(2 * math.pi * frequency * index / 8000)
        for index, sample in enumerate(samples)
    )
    return 2 * math.hypot(real, imaginary) / len(samples)


async def received(peer, value, start=0):
    expected = audioop.lin2alaw(pcm(value), 2)
    async with asyncio.timeout(3):
        while expected not in peer.received[start:]:
            peer.received_audio.clear()
            await peer.received_audio.wait()


class MemoryAudio:
    def __init__(self):
        self.frames = {}
        self.microphones = {}
        self.flushes = []
        self.activity = asyncio.Event()

    def read_speaker(self, identifier):
        return self.frames.get(identifier, SILENCE)

    def write_microphone(self, identifier, data):
        pending = self.microphones.setdefault(identifier, bytearray())
        pending.extend(data)
        if len(pending) > 32000:
            del pending[:-32000]
        self.activity.set()

    def flush(self, identifiers=None):
        self.flushes.append(None if identifiers is None else set(identifiers))

    async def captured(self, identifier, size=8000):
        async with asyncio.timeout(4):
            while len(self.microphones.get(identifier, b"")) < size:
                self.activity.clear()
                await self.activity.wait()
        return bytes(self.microphones[identifier][-size:])


class MemoryDevices(MemoryAudio):
    def __init__(self):
        super().__init__()
        self.devices = {}

    async def settle(self):
        return None

    async def enable(self, identifier, settings):
        self.devices[identifier] = settings

    async def disable(self, identifier):
        self.devices.pop(identifier, None)

    async def close(self):
        self.devices.clear()


class AudioCredentials:
    def __init__(self, token):
        self.token = token

    def get(self, backend):
        return self.token

    def set(self, backend, token):
        self.token = token
