import asyncio
import json
import struct
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

import audioop
from camera_peer import CameraPeer

from hikvision_audio_bridge.config import AudioDevice, Camera, Paths, Settings
from hikvision_audio_bridge.credentials import Credentials
from hikvision_audio_bridge.pipewire import SILENCE, AudioDevices
from hikvision_audio_bridge.service import Bridge


async def _pause():
    await asyncio.sleep(0.001)


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await _pause()


def pcm(value):
    return struct.pack("<160h", *([value] * 160))


class TestCredentials(Credentials):
    def get(self, camera):
        del camera
        return CameraPeer.password


class MemoryDevices:
    """Replace only PipeWire I/O; camera sockets and lifecycle remain real."""

    def __init__(self):
        self.devices = {}
        self.frames = {}
        self.reads = {}
        self.microphones = {}
        self.flushes = []

    async def settle(self):
        pass

    async def enable(self, identifier, settings):
        self.devices[identifier] = settings

    async def disable(self, identifier):
        self.devices.pop(identifier, None)

    def flush(self, identifiers=None):
        self.flushes.append(None if identifiers is None else set(identifiers))

    def read_speaker(self, identifier):
        if identifier not in self.devices:
            raise KeyError(identifier)
        self.reads[identifier] = self.reads.get(identifier, 0) + 1
        return self.frames.get(identifier, SILENCE)

    def write_microphone(self, identifier, pcm):
        if identifier not in self.devices:
            raise KeyError(identifier)
        self.microphones.setdefault(identifier, bytearray()).extend(pcm)

    async def close(self):
        self.devices.clear()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.paths = Paths(directory / "run", directory / "config")
        self.paths.prepare()
        self.memory = MemoryDevices()
        self.bridge = Bridge(self.paths, TestCredentials())
        self.bridge.devices = cast(AudioDevices, self.memory)
        self.first = await CameraPeer().start()
        self.second = await CameraPeer(880).start()

    async def asyncTearDown(self):
        await self.bridge.close()
        await self.first.stop()
        await self.second.stop()
        self.temporary.cleanup()

    def settings(self, *, microphone=True):
        return Settings(
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
        self.assertFalse(changed.devices["microphone"].enabled)
        self.assertTrue(changed.devices["speaker"].enabled)
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
        self.assertEqual(self.paths.load(), changed)

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
        settings = Settings(
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
        settings = Settings(
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
        status = self.bridge.status()
        self.assertEqual(
            set(status["devices"]),
            {"broadcast", "front_speaker", "listen", "back_mic"},
        )
        self.assertEqual(
            status["devices"]["broadcast"]["name"],
            "hikvision_audio_broadcast",
        )

    async def test_unrelated_toggle_does_not_retry_or_restart_cameras(self):
        settings = Settings(
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
        settings = Settings(
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
        settings = Settings(
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

    async def test_status_exposes_per_camera_and_tts_contract(self):
        settings = self.settings()
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
            await self.first.microphone_started.wait()
            await wait_until(
                lambda: (
                    self.bridge.cameras["front"].session is not None
                    and self.bridge.cameras["front"].session.sent_bytes > 0
                    and self.bridge.cameras["front"].session.received_bytes > 0
                    and self.bridge.cameras["front"].connection == "connected"
                )
            )
        status = self.bridge.status()
        self.assertEqual(set(status), {"settings", "cameras", "devices", "tts"})
        self.assertEqual(status["settings"], asdict(settings))
        self.assertEqual(
            status["devices"],
            {
                "speaker": {"name": "hikvision_audio_speaker"},
                "microphone": {"name": "hikvision_audio_microphone"},
            },
        )
        camera = status["cameras"]["front"]
        self.assertEqual(camera["connection"], "connected")
        self.assertEqual(camera["codec"], "G.711alaw")
        self.assertIsNone(camera["error"])
        self.assertIsNone(camera["cleanup_error"])
        self.assertGreater(camera["sent_bytes"], 0)
        self.assertGreater(camera["received_bytes"], 0)
        self.assertEqual(
            status["tts"],
            {"state": "idle", "error": None, "devices": []},
        )

    async def test_speech_is_routed_once_per_target_and_plays_to_completion(self):
        settings = Settings(
            cameras={
                "front": Camera(self.first.url, self.first.username),
                "back": Camera(self.second.url, self.second.username),
            },
            devices={
                "one": AudioDevice("One", "speaker", ("front", "back")),
                "two": AudioDevice("Two", "speaker", ("front",)),
            },
        )
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await asyncio.gather(
                self.first.received_audio.wait(),
                self.second.received_audio.wait(),
            )
            await wait_until(
                lambda: (
                    self.bridge.cameras["front"].connection == "connected"
                    and self.bridge.cameras["back"].connection == "connected"
                )
            )
        frame = pcm(8000)
        synth = AsyncMock(return_value=frame * 2)
        with patch("hikvision_audio_bridge.service.synthesize", new=synth):
            await self.bridge.speak("hello", ["one", "two"])
            self.assertEqual(self.bridge.speech_cameras, ("front", "back"))
            self.assertEqual(self.bridge.tts_devices, ["one", "two"])
            await wait_until(lambda: self.bridge.speech_playback is not None)
            playback = self.bridge.speech_playback
            self.assertIsNotNone(playback)
            assert playback is not None
            await playback.finished.wait()
            await wait_until(lambda: self.bridge.tts_state == "idle")
        synth.assert_awaited_once()
        self.assertEqual(playback.targets, ("front", "back"))
        self.assertEqual(self.first.speaker_connections, 1)
        self.assertEqual(self.second.speaker_connections, 1)
        encoded = audioop.lin2alaw(frame, 2)
        self.assertEqual(bytes(self.first.received).count(encoded), 2)
        self.assertEqual(bytes(self.second.received).count(encoded), 2)
        status = self.bridge.status()["tts"]
        self.assertEqual(status, {"state": "idle", "error": None, "devices": []})

    async def test_speak_validates_targets_without_calling_the_provider(self):
        settings = Settings(
            cameras={
                "front": Camera(self.first.url, self.first.username),
            },
            devices={
                "speaker": AudioDevice("Speaker", "speaker", ("front",), False),
                "microphone": AudioDevice("Microphone", "microphone", ("front",)),
            },
        )
        await self.bridge.apply(settings)
        async with asyncio.timeout(3):
            await self.first.microphone_started.wait()
        synth = AsyncMock(return_value=pcm(1000))
        with patch("hikvision_audio_bridge.service.synthesize", new=synth):
            for devices in ([], ["missing"], ["speaker"], ["microphone"]):
                with self.subTest(devices=devices):
                    with self.assertRaises(ValueError):
                        await self.bridge.speak("hello", devices)
        synth.assert_not_awaited()
        self.assertEqual(self.bridge.tts_state, "idle")

    async def test_stop_speech_cancels_synthesis_and_rejects_overlap(self):
        await self.bridge.apply(self.settings())
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocking_synth(_text, _voice):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch("hikvision_audio_bridge.service.synthesize", new=blocking_synth):
            status = await self.bridge.speak("hello", ["speaker"])
            self.assertEqual(status["tts"]["state"], "synthesizing")
            await started.wait()
            with self.assertRaises(ValueError):
                await self.bridge.speak("again", ["speaker"])
            status = await self.bridge.stop_speech()
            self.assertEqual(status["tts"]["state"], "idle")
            self.assertEqual(status["tts"]["devices"], [])
            await cancelled.wait()
        self.assertIsNone(self.bridge.speech_task)
        self.assertIsNone(self.bridge.speech_playback)

    async def test_stop_speech_waits_for_provider_cleanup(self):
        await self.bridge.apply(self.settings())
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()

        async def delayed_cleanup(_text, _voice):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        with patch("hikvision_audio_bridge.service.synthesize", new=delayed_cleanup):
            await self.bridge.speak("hello", ["speaker"])
            await started.wait()
            stop = asyncio.create_task(self.bridge.stop_speech())
            try:
                async with asyncio.timeout(3):
                    await cleaning.wait()
                    self.assertFalse(stop.done())
                    release.set()
                    await stop
            finally:
                release.set()
                await asyncio.gather(stop, return_exceptions=True)
        self.assertIsNone(self.bridge.speech_task)

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

    async def test_close_cancels_inflight_synthesis_and_owned_sessions(self):
        await self.bridge.apply(self.settings())
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocking_synth(_text, _voice):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch("hikvision_audio_bridge.service.synthesize", new=blocking_synth):
            await self.bridge.speak("hello", ["speaker"])
            await started.wait()
            await self.bridge.close()
            await cancelled.wait()
        self.assertIsNone(self.bridge.speech_task)
        self.assertFalse(self.bridge.cameras)
        self.assertFalse(self.memory.devices)
        self.assertEqual(self.first.closed, 1)

    async def test_speech_errors_do_not_expose_spoken_text(self):
        await self.bridge.apply(self.settings())
        async with asyncio.timeout(3):
            await self.first.received_audio.wait()
        text = "private spoken announcement"

        async def failing_synth(phrase, _voice):
            raise RuntimeError(phrase)

        with patch("hikvision_audio_bridge.service.synthesize", new=failing_synth):
            await self.bridge.speak(text, ["speaker"])
            await wait_until(
                lambda: (
                    self.bridge.tts_state == "error" and self.bridge.speech_task is None
                )
            )
        status = self.bridge.status()
        self.assertEqual(status["tts"]["devices"], ["speaker"])
        self.assertNotIn(text, json.dumps(status))
        self.assertNotIn(text, (self.paths.config / "settings.json").read_text())

    async def test_route_change_cancels_speech_before_it_can_leak(self):
        settings = Settings(
            cameras={
                "front": Camera(self.first.url, self.first.username),
                "back": Camera(self.second.url, self.second.username),
            },
            devices={
                "one": AudioDevice("One", "speaker", ("front",)),
                "back_speaker": AudioDevice("Back", "speaker", ("back",)),
                "listen": AudioDevice("Listen", "microphone", ("front", "back")),
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
                    self.bridge.cameras["front"].connection == "connected"
                    and self.bridge.cameras["back"].connection == "connected"
                )
            )
        frame = pcm(8000)
        synth = AsyncMock(return_value=frame * 64)
        with patch("hikvision_audio_bridge.service.synthesize", new=synth):
            await self.bridge.speak("announcement", ["one"])
            await wait_until(lambda: self.bridge.tts_state == "playing")
            before = len(self.second.received)
            changed = replace(
                settings,
                devices={
                    **settings.devices,
                    "one": replace(settings.devices["one"], cameras=("back",)),
                },
            )
            await self.bridge.apply(changed)
            self.assertEqual(self.bridge.tts_state, "idle")
            self.assertIsNone(self.bridge.speech_playback)
            await wait_until(lambda: len(self.second.received) >= before + 160 * 3)
        encoded = audioop.lin2alaw(frame, 2)
        self.assertNotIn(encoded, bytes(self.second.received[before:]))


if __name__ == "__main__":
    unittest.main()
