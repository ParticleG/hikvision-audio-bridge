import asyncio
import contextlib
import unittest

import audioop
from camera_peer import CameraPeer

from hikvision_audio_bridge.camera import CameraError, CameraSession
from hikvision_audio_bridge.config import Camera


class CameraTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = await CameraPeer().start()
        self.session = CameraSession(
            Camera(self.peer.url, self.peer.username), self.peer.password
        )

    async def asyncTearDown(self):
        await self.session.close()
        await self.peer.stop()

    async def test_duplex_streams_authenticate_and_preserve_pcm_audio(self):
        await self.session.open()
        microphone_frame = asyncio.get_running_loop().create_future()

        def consume(frame):
            if not microphone_frame.done():
                microphone_frame.set_result(frame)

        speaker = asyncio.create_task(self.session.speaker(lambda: b"\x10\x27" * 160))
        microphone = asyncio.create_task(self.session.microphone(consume))
        try:
            async with asyncio.timeout(3):
                await self.peer.received_audio.wait()
                pcm = await microphone_frame
            self.assertEqual(
                self.peer.received[:160], audioop.lin2alaw(b"\x10\x27" * 160, 2)
            )
            self.assertEqual(len(pcm), 320)
            self.assertGreater(audioop.rms(pcm, 2), 3000)
            self.assertEqual(self.peer.opened, 1)
            self.assertEqual(self.peer.speaker_connections, 1)
            self.assertEqual(self.peer.microphone_connections, 1)
        finally:
            for task in (speaker, microphone):
                task.cancel()
            await asyncio.gather(speaker, microphone, return_exceptions=True)
        await self.session.close()
        self.assertEqual(self.peer.closed, 1)

    async def test_delayed_device_error_is_not_lost_after_handshake(self):
        await self.session.open()
        speaker = asyncio.create_task(self.session.speaker(lambda: b"\0" * 320))
        try:
            async with asyncio.timeout(3):
                await self.peer.received_audio.wait()
                self.peer.reject_upload.set()
                with self.assertRaisesRegex(CameraError, "Device Busy"):
                    await speaker
        finally:
            speaker.cancel()
            with contextlib.suppress(asyncio.CancelledError, CameraError):
                await speaker

    async def test_busy_open_does_not_close_another_clients_session(self):
        self.peer.open_failure = True
        with self.assertRaisesRegex(CameraError, "Device Busy"):
            await self.session.open()
        await self.session.close()
        self.assertEqual(self.peer.closed, 0)

    async def test_unsupported_codec_never_opens_or_reconfigures_camera(self):
        self.peer.codec = "AAC"
        with self.assertRaisesRegex(CameraError, "G.711"):
            await self.session.open()
        self.assertEqual(self.peer.opened, 0)
        self.assertEqual(self.peer.closed, 0)

    async def test_close_failure_is_reported(self):
        await self.session.open()
        self.peer.close_failure = True
        with self.assertRaisesRegex(CameraError, "Device Busy"):
            await self.session.close()
        self.peer.close_failure = False
        self.session.opened = False
