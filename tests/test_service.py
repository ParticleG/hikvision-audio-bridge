import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from camera_peer import CameraPeer

from hikvision_audio_bridge.config import Camera, Paths, Settings
from hikvision_audio_bridge.credentials import Credentials
from hikvision_audio_bridge.service import Bridge


class TestCredentials(Credentials):
    def get(self, camera):
        return CameraPeer.password


class MemoryDevices:
    """Replace only PipeWire I/O; camera sockets and lifecycle remain real."""

    def __init__(self):
        self.devices = {}

    async def enable(self, name):
        self.devices[name] = True

    async def disable(self, name):
        self.devices.pop(name, None)

    def flush(self):
        pass

    def read_speaker(self):
        return b"\x10\x27" * 160

    def write_microphone(self, pcm):
        pass

    async def close(self):
        self.devices.clear()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.paths = Paths(directory / "run", directory / "config")
        self.paths.prepare()
        self.bridge = Bridge(self.paths, TestCredentials())
        self.bridge.devices = MemoryDevices()
        self.first = await CameraPeer().start()
        self.second = await CameraPeer(880).start()
        self.settings = Settings(
            Camera(self.first.url, self.first.username), True, True
        )

    async def asyncTearDown(self):
        await self.bridge.close()
        await self.first.stop()
        await self.second.stop()
        self.temporary.cleanup()

    async def test_disabling_microphone_does_not_reopen_or_stop_speaker(self):
        await self.bridge.apply(self.settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            await self.first.microphone_started.wait()
            await self.bridge.apply(replace(self.settings, microphone=False))
            self.first.received_audio.clear()
            await self.first.received_audio.wait()
        self.assertEqual(self.first.opened, 1)
        self.assertEqual(self.first.closed, 0)
        self.assertEqual(self.first.speaker_connections, 1)
        self.assertEqual(self.bridge.settings.microphone, False)
        self.assertTrue(self.bridge.settings.speaker)

    async def test_camera_switch_closes_old_session_and_stops_old_audio(self):
        await self.bridge.apply(self.settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            changed = replace(
                self.settings, camera=Camera(self.second.url, self.second.username)
            )
            await self.bridge.apply(changed)
            old_count = len(self.first.received)
            await self.second.received_audio.wait()
            await self.second.microphone_started.wait()
        self.assertEqual(self.first.closed, 1)
        self.assertEqual(self.second.opened, 1)
        self.assertEqual(len(self.first.received), old_count)
        self.assertEqual(self.paths.load(), changed)

    async def test_disabling_both_closes_owned_session(self):
        await self.bridge.apply(self.settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            await self.bridge.apply(
                replace(self.settings, speaker=False, microphone=False)
            )
        self.assertEqual(self.first.closed, 1)
        self.assertEqual(self.bridge.status()["connection"], "idle")
        self.assertFalse(self.bridge.streams)

    async def test_reconnect_clears_previous_audio_session_before_opening_new_one(self):
        await self.bridge.apply(self.settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            self.first.received_audio.clear()
            await self.bridge.apply(self.settings, restart=True)
            await self.first.received_audio.wait()
        self.assertEqual(self.first.opened, 2)
        self.assertEqual(self.first.closed, 1)

    async def test_reconnect_waits_for_inflight_error_cleanup(self):
        await self.bridge.apply(self.settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            session = self.bridge.session
            original_close = session.close
            loop = asyncio.get_running_loop()
            closing = asyncio.Event()
            release_close = loop.create_future()
            cancellation_observed = loop.create_future()
            reconnect = None

            async def gated_close():
                closing.set()
                await release_close
                await original_close()

            def release_after_reconnect_starts():
                cancellation_observed.set_result(release_close.cancelled())
                if not release_close.done():
                    release_close.set_result(None)

            with patch.object(session, "close", gated_close):
                try:
                    self.first.reject_upload.set()
                    await closing.wait()
                    self.first.reject_upload.clear()
                    self.first.received_audio.clear()
                    reconnect = asyncio.create_task(
                        self.bridge.apply(self.settings, restart=True)
                    )
                    # FIFO scheduling runs reconnect up to its first suspension,
                    # then releases the close gate without a wall-clock sleep.
                    loop.call_soon(release_after_reconnect_starts)
                    self.assertFalse(
                        await cancellation_observed,
                        "Reconnect cancelled the old session's close operation",
                    )
                    await reconnect
                    await self.first.received_audio.wait()
                    self.assertEqual(self.first.closed, 1)
                    self.assertEqual(self.first.opened, 2)
                    self.assertIsNone(self.bridge.cleanup_error)
                finally:
                    if not release_close.done():
                        release_close.set_result(None)
                    if reconnect is not None:
                        await asyncio.gather(reconnect, return_exceptions=True)

    async def test_cancelled_reconnect_keeps_cleanup_available_to_the_next_stop(self):
        self.first.allow_close.clear()
        reconnect = None
        try:
            await self.bridge.apply(self.settings)
            async with asyncio.timeout(3):
                await self.first.received_audio.wait()
                self.first.reject_upload.set()
                await self.first.close_started.wait()
                self.first.reject_upload.clear()
                self.first.received_audio.clear()
                reconnect = asyncio.create_task(
                    self.bridge.apply(self.settings, restart=True)
                )
                asyncio.get_running_loop().call_soon(reconnect.cancel)
                with self.assertRaises(asyncio.CancelledError):
                    await reconnect
                self.assertEqual(self.first.closed, 0)
                self.assertEqual(self.first.opened, 1)
                reconnect = asyncio.create_task(
                    self.bridge.apply(self.settings, restart=True)
                )
                asyncio.get_running_loop().call_soon(self.first.allow_close.set)
                await reconnect
                await self.first.received_audio.wait()
                self.assertEqual(self.first.closed, 1)
                self.assertEqual(self.first.opened, 2)
                self.assertIsNone(self.bridge.cleanup_error)
        finally:
            self.first.allow_close.set()
            if reconnect is not None:
                await asyncio.gather(reconnect, return_exceptions=True)
