import asyncio
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from hikvision_audio_bridge.config import Camera, Paths
from hikvision_audio_bridge.desktop import DesktopBridge
from hikvision_audio_bridge.server import (
    MAX_COMPLETED_SPEECH,
    MAX_QUEUED_SPEECH,
    Gateway,
)
from hikvision_audio_bridge.tts import DEFAULT_VOICE
from hikvision_audio_bridge.vault import ServerSecrets


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = Paths(root / "run", root / "config")
        self.paths.prepare()
        self.credentials = ServerSecrets(root / "secrets")
        self.bridge = Gateway(self.paths, self.credentials)
        self.camera = asdict(Camera("http://camera.test", "admin"))

    async def asyncTearDown(self):
        await self.bridge.close()
        self.temporary.cleanup()

    async def save(self, **extra):
        return await self.bridge.dispatch(
            {"op": "camera.save", "id": "front", "camera": self.camera, **extra}
        )

    async def test_browser_password_is_encrypted_not_returned_and_blank_is_rejected(
        self,
    ):
        secret = "private-test-password"
        status = await self.save(password=secret)
        self.assertEqual(self.credentials.get(Camera(**self.camera)), secret)
        self.assertNotIn(secret, json.dumps(status))
        self.assertNotIn(secret, (self.paths.config / "settings.json").read_text())
        for path in self.credentials.directory.iterdir():
            self.assertNotIn(secret.encode(), path.read_bytes())
        with self.assertRaises(ValueError):
            await self.save(password="")
        self.assertEqual(self.credentials.get(Camera(**self.camera)), secret)

    async def test_invalid_configuration_does_not_replace_a_stored_password(self):
        await self.save(password="original-password")
        with self.assertRaises(ValueError):
            await self.bridge.dispatch(
                {
                    "op": "camera.save",
                    "id": "duplicate",
                    "camera": self.camera,
                    "password": "unwanted-password",
                }
            )
        self.assertEqual(
            self.credentials.get(Camera(**self.camera)), "original-password"
        )
        self.assertEqual(set(self.paths.load_server().cameras), {"front"})

    async def test_unknown_fields_and_wrong_role_commands_do_not_mutate_settings(self):
        for command in (
            {"op": "status", "password": "secret"},
            {
                "op": "camera.save",
                "id": "front",
                "camera": self.camera,
                "unexpected": True,
            },
            {"op": "reconnect", "id": "missing"},
            {
                "op": "device.save",
                "id": "speaker",
                "device": {"name": "Test", "direction": "speaker", "enabled": True},
            },
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                await self.bridge.dispatch(command)
        self.assertEqual(self.bridge.settings.cameras, {})

    async def test_disabled_desktop_device_prevents_group_removal(self):
        desktop = DesktopBridge(
            Paths(self.paths.runtime / "desktop", self.paths.config / "desktop")
        )
        try:
            await desktop.dispatch(
                {
                    "op": "group.save",
                    "id": "office",
                    "group": {"name": "Office", "cameras": ["front"]},
                }
            )
            await desktop.dispatch(
                {
                    "op": "device.save",
                    "id": "speaker",
                    "device": {
                        "name": "Speaker",
                        "direction": "speaker",
                        "groups": ["office"],
                        "enabled": False,
                    },
                }
            )
            with self.assertRaises(ValueError):
                await desktop.dispatch({"op": "group.remove", "id": "office"})
            with self.assertRaises(ValueError):
                await desktop.dispatch(
                    {"op": "device.set", "id": "speaker", "enabled": 1}
                )
            self.assertIn("office", desktop.paths.load_desktop().groups)
            self.assertFalse(desktop.settings.devices["speaker"].enabled)
            await desktop.dispatch({"op": "device.remove", "id": "speaker"})
            await desktop.dispatch({"op": "group.remove", "id": "office"})
            self.assertEqual(desktop.paths.load_desktop().groups, {})
        finally:
            await desktop.close()

    async def test_speech_stop_retains_provider_cleanup_after_caller_cancellation(self):
        await self.save()
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def provider(_text, _voice):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        queue = self.bridge.speech
        with patch("hikvision_audio_bridge.server.synthesize", new=provider):
            identifier = queue.enqueue("private announcement", ["front"], DEFAULT_VOICE)
            await asyncio.wait_for(started.wait(), 3)
            cancelling = asyncio.create_task(queue.cancel(identifier))
            try:
                await asyncio.wait_for(cleaning.wait(), 3)
                self.assertFalse(cancelling.done())
                cancelling.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelling
                stopping = asyncio.create_task(queue.stop())
                asyncio.get_running_loop().call_soon(release.set)
                await asyncio.wait_for(stopping, 3)
            finally:
                release.set()
                await asyncio.gather(cancelling, return_exceptions=True)
        self.assertIsNone(queue.worker)
        self.assertIsNone(queue.playing)
        self.assertEqual(queue.status()["last"]["state"], "cancelled")

    async def test_speech_queue_is_bounded_and_does_not_disclose_text(self):
        await self.save()
        started = asyncio.Event()

        async def provider(_text, _voice):
            started.set()
            await asyncio.Event().wait()

        queue = self.bridge.speech
        text = "private queued announcement"
        with patch("hikvision_audio_bridge.server.synthesize", new=provider):
            identifiers = [queue.enqueue(text, ["front"], DEFAULT_VOICE)]
            await asyncio.wait_for(started.wait(), 3)
            for _ in range(MAX_QUEUED_SPEECH):
                identifiers.append(queue.enqueue(text, ["front"], DEFAULT_VOICE))
            with self.assertRaises(ValueError):
                queue.enqueue(text, ["front"], DEFAULT_VOICE)
            self.assertNotIn(text, json.dumps(self.bridge.status()))
            with self.assertRaises(ValueError):
                await self.bridge.dispatch({"op": "camera.remove", "id": "front"})
            await queue.stop()
        self.assertEqual(queue.status()["queue"], [])
        self.assertNotIn(text, (self.paths.config / "settings.json").read_text())
        for identifier in identifiers:
            receipt = await self.bridge.dispatch({"op": "tts.get", "id": identifier})
            self.assertEqual(receipt["state"], "cancelled")
            self.assertNotIn(text, json.dumps(receipt))

    async def test_cancelled_terminal_job_does_not_cancel_another_active_job(self):
        await self.save()
        started = asyncio.Event()

        async def provider(text, _voice):
            if text == "first":
                raise RuntimeError("Provider unavailable")
            started.set()
            await asyncio.Event().wait()

        queue = self.bridge.speech
        with patch("hikvision_audio_bridge.server.synthesize", new=provider):
            first = queue.enqueue("first", ["front"], DEFAULT_VOICE)
            await queue.worker
            second = queue.enqueue("second", ["front"], DEFAULT_VOICE)
            await asyncio.wait_for(started.wait(), 3)
            await queue.cancel(first)
            self.assertEqual(queue.job_status(first)["state"], "error")
            self.assertEqual(queue.job_status(second)["state"], "synthesizing")
            with self.assertRaises(ValueError):
                await queue.cancel("missing")
            self.assertEqual(queue.current.identifier, second)
            await queue.stop()
        self.assertEqual(queue.job_status(second)["state"], "cancelled")
        self.assertEqual(queue.job_status(first)["state"], "error")

    async def test_expired_speech_receipt_is_not_reported_as_success(self):
        await self.save()
        text = "private announcement"

        async def provider(phrase, _voice):
            raise RuntimeError(phrase)

        queue = self.bridge.speech
        identifiers = []
        with patch("hikvision_audio_bridge.server.synthesize", new=provider):
            for _ in range(MAX_COMPLETED_SPEECH + 1):
                identifiers.append(queue.enqueue(text, ["front"], DEFAULT_VOICE))
                await queue.worker
        with self.assertRaises(ValueError):
            await self.bridge.dispatch({"op": "tts.get", "id": identifiers[0]})
        for identifier in (identifiers[1], identifiers[-1]):
            receipt = await self.bridge.dispatch({"op": "tts.get", "id": identifier})
            self.assertEqual(receipt["state"], "error")
            self.assertNotIn(text, json.dumps(receipt))
            self.assertNotIn("text", receipt)
        self.assertEqual(queue.status()["last"]["id"], identifiers[-1])

    async def test_current_speech_has_a_state_before_provider_starts(self):
        await self.save()

        async def provider(_text, _voice):
            await asyncio.Event().wait()

        loop = asyncio.get_running_loop()
        observed = loop.create_future()
        queue = self.bridge.speech
        with patch("hikvision_audio_bridge.server.synthesize", new=provider):
            identifier = queue.enqueue("announcement", ["front"], DEFAULT_VOICE)
            loop.call_soon(
                lambda: observed.set_result(queue.job_status(identifier)["state"])
            )
            state = await observed
            await queue.stop()
        self.assertEqual(state, "synthesizing")

    async def test_speech_failure_redacts_announcement_text(self):
        await self.save()
        text = "private failed announcement"

        async def provider(phrase, _voice):
            raise RuntimeError(phrase)

        with patch("hikvision_audio_bridge.server.synthesize", new=provider):
            self.bridge.speech.enqueue(text, ["front"], DEFAULT_VOICE)
            await self.bridge.speech.worker
        self.assertEqual(self.bridge.speech.status()["last"]["state"], "error")
        self.assertNotIn(text, json.dumps(self.bridge.status()))

    async def test_invalid_speech_input_is_rejected_before_contacting_provider(self):
        await self.save()
        for text, cameras, voice in (
            (None, ["front"], DEFAULT_VOICE),
            ("hello", ["front"], None),
            ("hello", ["missing"], DEFAULT_VOICE),
            ("hello", [], DEFAULT_VOICE),
        ):
            with (
                self.subTest(text=text, cameras=cameras, voice=voice),
                self.assertRaises(ValueError),
            ):
                self.bridge.speech.enqueue(text, cameras, voice)
        self.assertIsNone(self.bridge.speech.worker)
