import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

import audioop
from audio_helpers import MemoryAudio, pcm
from camera_peer import CameraPeer

from hikvision_audio_bridge.config import AudioDevice, Camera, RoutingSettings
from hikvision_audio_bridge.service import CameraEngine


async def _pause():
    await asyncio.sleep(0.001)


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await _pause()


class TestCredentials:
    def get(self, camera):
        return CameraPeer.password


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.memory = MemoryAudio()
        self.bridge = CameraEngine(TestCredentials(), self.memory)
        self.first = await CameraPeer().start()
        self.second = await CameraPeer(880).start()

    async def asyncTearDown(self):
        await self.bridge.close()
        await self.first.stop()
        await self.second.stop()

    def settings(self, *, microphone=True):
        return RoutingSettings(
            cameras={
                "front": Camera(self.first.url, self.first.username),
            },
            devices={
                "speaker": AudioDevice("Speaker", "speaker", ("front",)),
                "microphone": AudioDevice(
                    "Microphone", "microphone", ("front",), microphone
                ),
            },
        )

    async def test_disabling_microphone_keeps_speaker_session_and_stream(self):
        settings = self.settings()
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            await self.first.microphone_started.wait()
            changed = replace(
                settings,
                devices={
                    **settings.devices,
                    "microphone": replace(
                        settings.devices["microphone"], enabled=False
                    ),
                },
            )
            await self.bridge.apply(changed)
            self.assertNotIn("microphone", self.bridge.cameras["front"].streams)
            self.first.received_audio.clear()
            await self.first.received_audio.wait()
        self.assertEqual(self.first.opened, 1)
        self.assertEqual(self.first.closed, 0)
        self.assertEqual(self.first.speaker_connections, 1)
        self.assertFalse(self.bridge.cameras["front"].desired_microphone)
        self.assertTrue(self.bridge.cameras["front"].desired_speaker)

    async def test_camera_identity_change_closes_old_session_and_old_audio(self):
        settings = self.settings()
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            await self.first.microphone_started.wait()
            changed = replace(
                settings,
                cameras={"front": Camera(self.second.url, self.second.username)},
            )
            await self.bridge.apply(changed)
            await wait_until(lambda: not self.first.writers)
            old_count = len(self.first.received)
            await self.second.received_audio.wait()
            await self.second.microphone_started.wait()
        self.assertEqual(self.first.closed, 1)
        self.assertEqual(self.second.opened, 1)
        self.assertEqual(len(self.first.received), old_count)

    async def test_removing_all_routes_closes_the_owned_session(self):
        settings = self.settings()
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            self.memory.flushes.clear()
            await self.bridge.apply(
                replace(
                    settings,
                    devices={
                        "speaker": replace(settings.devices["speaker"], enabled=False),
                        "microphone": replace(
                            settings.devices["microphone"], enabled=False
                        ),
                    },
                )
            )
        self.assertEqual(self.first.closed, 1)
        self.assertIn({"speaker", "microphone"}, self.memory.flushes)
        state = self.bridge.cameras["front"]
        self.assertIsNone(state.worker)
        self.assertFalse(state.streams)
        status = self.bridge.status()
        self.assertEqual(status["cameras"]["front"]["connection"], "idle")
        self.assertFalse(status["cameras"]["front"]["error"])
        self.assertFalse(status["cameras"]["front"]["cleanup_error"])

    async def test_reconnect_clears_previous_audio_session_before_opening(self):
        settings = self.settings()
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            self.memory.flushes.clear()
            self.first.received_audio.clear()
            await self.bridge.apply(settings, restart=True)
            await self.first.received_audio.wait()
        self.assertEqual(self.first.opened, 2)
        self.assertEqual(self.first.closed, 1)
        self.assertIn({"speaker", "microphone"}, self.memory.flushes)

    async def test_reconnect_waits_for_inflight_error_cleanup(self):
        await self.bridge.apply(self.settings())
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            session = self.bridge.cameras["front"].session
            self.assertIsNotNone(session)
            assert session is not None
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
                        self.bridge.apply(
                            self.settings(), restart=True, camera_id="front"
                        )
                    )
                    loop.call_soon(release_after_reconnect_starts)
                    self.assertFalse(
                        await cancellation_observed,
                        "Reconnect cancelled the old session's close operation",
                    )
                    await reconnect
                    await self.first.received_audio.wait()
                    self.assertEqual(self.first.closed, 1)
                    self.assertEqual(self.first.opened, 2)
                    self.assertIsNone(self.bridge.cameras["front"].cleanup_error)
                finally:
                    if not release_close.done():
                        release_close.set_result(None)
                    if reconnect is not None:
                        await asyncio.gather(reconnect, return_exceptions=True)

    async def test_cancelled_reconnect_keeps_cleanup_for_the_next_stop(self):
        self.first.allow_close.clear()
        reconnect = None
        try:
            await self.bridge.apply(self.settings())
            async with asyncio.timeout(3):
                await self.first.received_audio.wait()
                self.first.reject_upload.set()
                await self.first.close_started.wait()
                self.first.reject_upload.clear()
                self.first.received_audio.clear()
                reconnect = asyncio.create_task(
                    self.bridge.apply(self.settings(), restart=True, camera_id="front")
                )
                asyncio.get_running_loop().call_soon(reconnect.cancel)
                with self.assertRaises(asyncio.CancelledError):
                    await reconnect
                self.assertEqual(self.first.closed, 0)
                self.assertEqual(self.first.opened, 1)
                reconnect = asyncio.create_task(
                    self.bridge.apply(self.settings(), restart=True, camera_id="front")
                )
                asyncio.get_running_loop().call_soon(self.first.allow_close.set)
                await reconnect
                await self.first.received_audio.wait()
                self.assertEqual(self.first.closed, 1)
                self.assertEqual(self.first.opened, 2)
                self.assertIsNone(self.bridge.cameras["front"].cleanup_error)
        finally:
            self.first.allow_close.set()
            if reconnect is not None:
                await asyncio.gather(reconnect, return_exceptions=True)

    async def test_concurrent_camera_cleanup_survives_caller_cancellation(self):
        settings = RoutingSettings(
            cameras={
                "front": Camera(self.first.url, self.first.username),
                "back": Camera(self.second.url, self.second.username),
            },
            devices={
                "front_speaker": AudioDevice("Front", "speaker", ("front",)),
                "back_speaker": AudioDevice("Back", "speaker", ("back",)),
                "front_mic": AudioDevice("Front mic", "microphone", ("front",)),
                "back_mic": AudioDevice("Back mic", "microphone", ("back",)),
            },
        )
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            await self.second.received_audio.wait()
            await self.first.microphone_started.wait()
            await self.second.microphone_started.wait()

        self.first.allow_close.clear()
        self.second.allow_close.clear()
        restart = None
        try:
            restart = asyncio.create_task(self.bridge.apply(settings, restart=True))
            async with asyncio.timeout(3):
                await self.first.close_started.wait()
                await self.second.close_started.wait()
            restart.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await restart
            self.first.allow_close.set()
            self.second.allow_close.set()
            self.first.received_audio.clear()
            self.second.received_audio.clear()
            await self.bridge.apply(settings, restart=True)
            async with asyncio.timeout(3):
                await self.first.received_audio.wait()
                await self.second.received_audio.wait()
            self.assertEqual(self.first.closed, 1)
            self.assertEqual(self.second.closed, 1)
            self.assertEqual(self.first.opened, 2)
            self.assertEqual(self.second.opened, 2)
            self.assertEqual(set(self.bridge.router.outputs), {"front", "back"})
        finally:
            self.first.allow_close.set()
            self.second.allow_close.set()
            if restart is not None:
                await asyncio.gather(restart, return_exceptions=True)

    async def test_overlapping_routes_share_one_session_per_camera(self):
        settings = RoutingSettings(
            cameras={
                "front": Camera(self.first.url, self.first.username),
                "back": Camera(self.second.url, self.second.username),
            },
            devices={
                "broadcast": AudioDevice("Broadcast", "speaker", ("front", "back")),
                "front_speaker": AudioDevice("Front", "speaker", ("front",)),
                "listen": AudioDevice("Listen", "microphone", ("front", "back")),
                "back_mic": AudioDevice("Back", "microphone", ("back",)),
            },
        )
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await asyncio.gather(
                self.first.received_audio.wait(),
                self.second.received_audio.wait(),
                self.first.microphone_started.wait(),
                self.second.microphone_started.wait(),
            )
            await wait_until(
                lambda: (
                    len(self.memory.microphones.get("listen", b"")) >= 320
                    and len(self.memory.microphones.get("back_mic", b"")) >= 320
                )
            )
        self.assertEqual(self.first.opened, 1)
        self.assertEqual(self.second.opened, 1)
        self.assertEqual(self.first.speaker_connections, 1)
        self.assertEqual(self.second.speaker_connections, 1)
        self.assertEqual(self.first.microphone_connections, 1)
        self.assertEqual(self.second.microphone_connections, 1)
        self.assertEqual(len(self.bridge.cameras["front"].streams), 2)
        self.assertEqual(len(self.bridge.cameras["back"].streams), 2)

    async def test_unrelated_toggle_does_not_retry_or_restart_cameras(self):
        settings = RoutingSettings(
            cameras={
                "front": Camera(self.first.url, self.first.username),
                "back": Camera(self.second.url, self.second.username),
            },
            devices={
                "front_speaker": AudioDevice("Front", "speaker", ("front",)),
                "back_speaker": AudioDevice("Back", "speaker", ("back",)),
                "aux": AudioDevice("Auxiliary", "speaker", ("back",), False),
            },
        )
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            await self.second.received_audio.wait()

        self.first.reject_upload.set()
        async with asyncio.timeout(3):
            await self.first.close_started.wait()
            await wait_until(lambda: self.first.closed == 1)
            await wait_until(lambda: self.bridge.cameras["front"].connection == "error")
        self.first.reject_upload.clear()

        changed = replace(
            settings,
            devices={
                **settings.devices,
                "aux": replace(settings.devices["aux"], enabled=True),
            },
        )
        await self.bridge.apply(changed)
        self.assertEqual(self.first.opened, 1)
        self.assertEqual(self.bridge.cameras["front"].connection, "error")
        self.assertEqual(self.second.opened, 1)
        self.assertEqual(self.second.closed, 0)
        self.assertEqual(self.second.speaker_connections, 1)

        self.first.received_audio.clear()
        await self.bridge.apply(changed, restart=True, camera_id="front")
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
        self.assertEqual(self.first.opened, 2)
        self.assertEqual(self.first.closed, 1)
        self.assertEqual(self.second.opened, 1)
        self.assertEqual(self.second.speaker_connections, 1)
        self.assertIsNone(self.bridge.cameras["front"].cleanup_error)

    async def test_direction_switch_changes_streams_without_reopening(self):
        settings = RoutingSettings(
            cameras={
                "front": Camera(self.first.url, self.first.username),
            },
            devices={
                "control": AudioDevice("Control", "speaker", ("front",)),
            },
        )
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
        changed = replace(
            settings,
            devices={
                "control": replace(settings.devices["control"], direction="microphone"),
            },
        )
        await self.bridge.apply(changed)
        async with asyncio.timeout(3):
            await self.first.microphone_started.wait()
        self.assertEqual(self.first.opened, 1)
        self.assertEqual(self.first.closed, 0)
        self.assertEqual(self.first.speaker_connections, 1)
        self.assertEqual(self.first.microphone_connections, 1)
        state = self.bridge.cameras["front"]
        self.assertEqual(set(state.streams), {"microphone"})
        self.assertFalse(state.desired_speaker)
        self.assertTrue(state.desired_microphone)

        removed = replace(
            changed,
            devices={
                "control": replace(changed.devices["control"], cameras=()),
            },
        )
        await self.bridge.apply(removed)
        self.assertEqual(self.first.closed, 1)
        self.assertIsNone(state.worker)
        self.assertFalse(state.streams)
        self.assertEqual(self.bridge.status()["cameras"]["front"]["connection"], "idle")

    async def test_cleanup_failure_is_reported_separately_from_stream_error(self):
        settings = RoutingSettings(
            cameras={
                "front": Camera(self.first.url, self.first.username),
            },
            devices={
                "speaker": AudioDevice("Speaker", "speaker", ("front",)),
            },
        )
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
        self.first.close_failure = True
        try:
            await self.bridge.apply(
                replace(
                    settings,
                    devices={
                        "speaker": replace(settings.devices["speaker"], enabled=False),
                    },
                )
            )
        finally:
            self.first.close_failure = False
        status = self.bridge.status()["cameras"]["front"]
        self.assertIsNone(status["error"])
        self.assertIn("Device Busy", status["cleanup_error"])
        self.assertEqual(status["connection"], "error")
        self.assertEqual(self.first.closed, 1)

    async def test_failed_reconnect_preserves_previous_cleanup_error(self):
        settings = self.settings()
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
        self.first.close_failure = True
        await self.bridge.apply(replace(settings, devices={}))
        cleanup_error = self.bridge.status()["cameras"]["front"]["cleanup_error"]
        self.assertIsNotNone(cleanup_error)
        self.first.open_failure = True
        await self.bridge.apply(settings, restart=True, camera_id="front")
        await wait_until(lambda: self.bridge.cameras["front"].worker.done())
        status = self.bridge.status()["cameras"]["front"]
        self.assertIsNotNone(status["error"])
        self.assertEqual(status["cleanup_error"], cleanup_error)

    async def test_standalone_speech_deduplicates_targets_and_closes_owned_sessions(
        self,
    ):
        await self.bridge.apply(
            RoutingSettings(
                cameras={
                    "front": Camera(self.first.url, self.first.username),
                    "back": Camera(self.second.url, self.second.username),
                }
            )
        )
        frame = pcm(8000)
        await self.bridge.play_pcm(frame * 2, ("front", "front", "back"))
        encoded = audioop.lin2alaw(frame, 2)
        self.assertEqual(bytes(self.first.received).count(encoded), 2)
        self.assertEqual(bytes(self.second.received).count(encoded), 2)
        self.assertEqual((self.first.opened, self.first.closed), (1, 1))
        self.assertEqual((self.second.opened, self.second.closed), (1, 1))


if __name__ == "__main__":
    unittest.main()
