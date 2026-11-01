import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import aiohttp
import audioop
from audio_helpers import AudioCredentials, MemoryDevices, magnitude, pcm, received
from camera_peer import CameraPeer

from hikvision_audio_bridge.chimes import chime_pcm
from hikvision_audio_bridge.config import (
    BackendSettings,
    Camera,
    CameraGroup,
    DesktopDevice,
    DesktopSettings,
    Paths,
    ServerSettings,
    WebSettings,
)
from hikvision_audio_bridge.desktop import DesktopBridge
from hikvision_audio_bridge.server import Gateway
from hikvision_audio_bridge.tts import DEFAULT_VOICE
from hikvision_audio_bridge.vault import ServerSecrets
from hikvision_audio_bridge.web import WebServer


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.front, self.back = (
            await CameraPeer(400).start(),
            await CameraPeer(800).start(),
        )
        paths = Paths(self.root / "server-run", self.root / "server-state")
        paths.prepare()
        self.vault = ServerSecrets(paths.config / "secrets")
        cameras = {
            "front": Camera(self.front.url, self.front.username),
            "back": Camera(self.back.url, self.back.username),
        }
        for camera in cameras.values():
            self.vault.set(camera, CameraPeer.password)
        self.gateway = Gateway(paths, self.vault)
        settings = ServerSettings(cameras, WebSettings(port=0))
        await self.gateway.start(settings)
        self.web = WebServer(
            paths,
            settings.web,
            self.gateway.dispatch,
            component="server",
            token=self.gateway.admin_token,
            audio=self.gateway,
        )
        await self.web.start()
        self.clients = []

    async def asyncTearDown(self):
        self.front.allow_close.set()
        self.back.allow_close.set()
        for desktop in self.clients:
            await desktop.close()
        await self.gateway.close()
        await self.web.close()
        await self.front.stop()
        await self.back.stop()
        self.directory.cleanup()

    async def desktop(self, groups, devices, frames):
        index = len(self.clients)
        paths = Paths(
            self.root / f"desktop-{index}-run", self.root / f"desktop-{index}-state"
        )
        paths.prepare()
        desktop = DesktopBridge(paths, AudioCredentials(self.gateway.access_token))
        desktop.devices = MemoryDevices()
        desktop.devices.frames.update(frames)
        self.clients.append(desktop)
        settings = DesktopSettings(
            BackendSettings(self.web.url, f"Desktop {index}"), groups, devices
        )
        await desktop.apply(settings, persist=False)
        self.assertEqual(desktop.connection, "connected", desktop.error)
        return desktop

    async def test_same_device_ids_share_camera_and_preserve_duplex_audio_on_departure(
        self,
    ):
        first = await self.desktop(
            {"office": CameraGroup("Office", ("front", "back"))},
            {
                "speaker": DesktopDevice("Speaker", "speaker", ("office",)),
                "microphone": DesktopDevice("Microphone", "microphone", ("office",)),
            },
            {"speaker": pcm(1000)},
        )
        second = await self.desktop(
            {"office": CameraGroup("Office", ("front",))},
            {
                "speaker": DesktopDevice("Speaker", "speaker", ("office",)),
                "microphone": DesktopDevice("Microphone", "microphone", ("office",)),
            },
            {"speaker": pcm(3000)},
        )
        await received(self.front, 2000)
        await received(self.back, 1000)
        mixed, single = await asyncio.gather(
            first.devices.captured("microphone"), second.devices.captured("microphone")
        )
        self.assertGreater(magnitude(mixed, 400), 1800)
        self.assertGreater(magnitude(mixed, 800), 1800)
        self.assertGreater(magnitude(single, 400), 4000)
        self.assertLess(magnitude(single, 800), 250)
        self.assertEqual((self.front.opened, self.back.opened), (1, 1))
        self.assertEqual(
            (self.front.microphone_connections, self.back.microphone_connections),
            (1, 1),
        )
        checkpoint = len(self.front.received)
        await first.close()
        await asyncio.wait_for(self.back.close_started.wait(), 3)
        await received(self.front, 3000, checkpoint)
        self.assertEqual((self.front.opened, self.front.closed), (1, 0))
        self.assertEqual(second.connection, "connected")

    async def test_slow_route_cleanup_does_not_block_other_streams_in_the_same_session(
        self,
    ):
        desktop = await self.desktop(
            {
                "front": CameraGroup("Front", ("front",)),
                "back": CameraGroup("Back", ("back",)),
            },
            {
                "main": DesktopDevice("Main", "speaker", ("front",)),
                "other": DesktopDevice("Other", "speaker", ("back",)),
            },
            {"main": pcm(1000), "other": pcm(2000)},
        )
        await received(self.front, 1000)
        await received(self.back, 2000)
        self.back.allow_close.clear()
        changed = replace(
            desktop.settings,
            devices={
                **desktop.settings.devices,
                "other": replace(desktop.settings.devices["other"], enabled=False),
            },
        )
        updating = asyncio.create_task(desktop.apply(changed, persist=False))
        try:
            await asyncio.wait_for(self.back.close_started.wait(), 3)
            checkpoint = len(self.front.received)
            desktop.devices.frames["main"] = pcm(3000)
            async with asyncio.timeout(1):
                await received(self.front, 3000, checkpoint)
            self.assertEqual((self.front.opened, self.front.closed), (1, 0))
        finally:
            self.back.allow_close.set()
            await updating
        self.assertEqual(desktop.connection, "connected", desktop.error)

    async def test_audio_scope_cannot_read_management_or_change_camera_configuration(
        self,
    ):
        async with aiohttp.ClientSession(trust_env=False) as client:
            headers = {"Authorization": f"Bearer {self.gateway.access_token}"}
            async with client.get(
                self.web.url + "/api/status", headers=headers
            ) as response:
                self.assertEqual(response.status, 401)
            async with client.post(
                self.web.url + "/api/command",
                headers=headers,
                json={"op": "camera.remove", "id": "front"},
            ) as response:
                self.assertEqual(response.status, 401)
            async with client.get(
                self.web.url + "/api/audio/catalog", headers=headers
            ) as response:
                catalog = (await response.json())["status"]
                self.assertEqual(set(catalog["cameras"]), {"front", "back"})
                self.assertNotIn("settings", catalog)
                self.assertNotIn(CameraPeer.password, str(catalog))
            headers = {"Authorization": f"Bearer {self.gateway.admin_token}"}
            async with client.get(
                self.web.url + "/api/audio/catalog", headers=headers
            ) as response:
                self.assertEqual(response.status, 401)
        self.assertIn("front", self.gateway.settings.cameras)
        self.assertEqual(self.front.opened, 0)

    async def test_queued_speech_mixes_in_order_and_cancelled_job_is_not_synthesized(
        self,
    ):
        await self.desktop(
            {"office": CameraGroup("Office", ("front",))},
            {"speaker": DesktopDevice("Speaker", "speaker", ("office",))},
            {"speaker": pcm(3000)},
        )
        await received(self.front, 3000)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def provider(text, _voice):
            calls.append(text)
            if text == "first":
                started.set()
                await release.wait()
            return pcm({"first": 15000, "second": 25000}[text]) * 3

        queue = self.gateway.speech
        with patch("hikvision_audio_bridge.server.synthesize", new=provider):
            first_id = queue.enqueue("first", ["front"], DEFAULT_VOICE)
            await asyncio.wait_for(started.wait(), 3)
            cancelled = queue.enqueue("cancelled", ["front"], DEFAULT_VOICE)
            second_id = queue.enqueue("second", ["front"], DEFAULT_VOICE)
            await queue.cancel(cancelled)
            release.set()
            await asyncio.wait_for(queue.worker, 3)
        self.assertEqual(calls, ["first", "second"])
        first = audioop.lin2alaw(pcm(9000), 2)
        second = audioop.lin2alaw(pcm(14000), 2)
        unmixed_first = audioop.lin2alaw(pcm(15000), 2)
        unmixed_second = audioop.lin2alaw(pcm(25000), 2)
        async with asyncio.timeout(3):
            while (
                self.front.received.count(second)
                + self.front.received.count(unmixed_second)
                < 3
            ):
                self.front.received_audio.clear()
                await self.front.received_audio.wait()
        data = bytes(self.front.received)
        # A live frame can miss a mix tick; the speech must still be complete.
        self.assertEqual(data.count(first) + data.count(unmixed_first), 3)
        self.assertEqual(data.count(second) + data.count(unmixed_second), 3)
        self.assertLess(data.index(first), data.index(second))
        self.assertEqual((self.front.opened, self.front.closed), (1, 0))
        async with aiohttp.ClientSession(trust_env=False) as client:
            for identifier, expected in (
                (first_id, "completed"),
                (cancelled, "cancelled"),
                (second_id, "completed"),
            ):
                async with client.post(
                    self.web.url + "/api/command",
                    headers={"Authorization": f"Bearer {self.gateway.admin_token}"},
                    json={"op": "tts.get", "id": identifier},
                ) as response:
                    self.assertEqual(response.status, 200, await response.text())
                    receipt = (await response.json())["status"]
                    self.assertEqual(receipt["id"], identifier)
                    self.assertEqual(receipt["state"], expected)

    async def test_status_chimes_precede_speech_on_every_camera_transport(self):
        speech = pcm(18000) * 5

        async def provider(_text, _voice):
            return speech

        async with aiohttp.ClientSession(trust_env=False) as client:
            for chime in (None, "success", "warning", "error"):
                with self.subTest(chime=chime):
                    checkpoints = [
                        len(peer.received) for peer in (self.front, self.back)
                    ]
                    command = {
                        "op": "tts.speak",
                        "text": "Transport check",
                        "cameras": ["front", "back"],
                    }
                    if chime is not None:
                        command["chime"] = chime
                    with patch(
                        "hikvision_audio_bridge.server.synthesize", new=provider
                    ):
                        async with client.post(
                            self.web.url + "/api/command",
                            headers={
                                "Authorization": f"Bearer {self.gateway.admin_token}"
                            },
                            json=command,
                        ) as response:
                            self.assertEqual(
                                response.status, 200, await response.text()
                            )
                            identifier = (await response.json())["status"]["queued_job"]
                        await asyncio.wait_for(self.gateway.speech.worker, 5)
                    expected = speech
                    if chime is not None:
                        expected = chime_pcm(chime) + bytes(3200) + speech
                    encoded = audioop.lin2alaw(expected, 2)
                    for peer, checkpoint in zip(
                        (self.front, self.back), checkpoints, strict=True
                    ):
                        self.assertIn(encoded, peer.received[checkpoint:])
                        self.assertEqual(peer.opened, peer.closed)
                    receipt = self.gateway.speech.job_status(identifier)
                    self.assertEqual(receipt["state"], "completed")

    async def test_cancelling_during_chime_does_not_send_speech_to_cameras(self):
        speech = pcm(18000) * 5
        cue_start = audioop.lin2alaw(chime_pcm("error")[:320], 2)

        async def provider(_text, _voice):
            return speech

        with patch("hikvision_audio_bridge.server.synthesize", new=provider):
            identifier = self.gateway.speech.enqueue(
                "Cancel during cue", ["front", "back"], DEFAULT_VOICE, "error"
            )
            async with asyncio.timeout(5):
                while cue_start not in self.front.received:
                    self.front.received_audio.clear()
                    await self.front.received_audio.wait()
            await self.gateway.speech.cancel(identifier)
        for peer in (self.front, self.back):
            self.assertNotIn(audioop.lin2alaw(speech[:320], 2), peer.received)
            self.assertEqual(peer.opened, peer.closed)
        self.assertEqual(
            self.gateway.speech.job_status(identifier)["state"], "cancelled"
        )
