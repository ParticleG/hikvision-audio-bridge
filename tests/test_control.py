import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from hikvision_audio_bridge.config import Camera, Paths
from hikvision_audio_bridge.control import dispatch
from hikvision_audio_bridge.credentials import Credentials
from hikvision_audio_bridge.service import Bridge


class MemoryCredentials(Credentials):
    def __init__(self):
        self.secrets = {}

    def set(self, camera, password):
        self.secrets[(camera.url, camera.username)] = password

    def get(self, camera):
        return self.secrets[(camera.url, camera.username)]


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = Paths(root / "run", root / "config")
        self.paths.prepare()
        self.credentials = MemoryCredentials()
        self.bridge = Bridge(self.paths, self.credentials)
        self.camera = asdict(Camera("http://camera.test", "admin"))

    async def asyncTearDown(self):
        await self.bridge.close()
        self.temporary.cleanup()

    async def test_browser_password_is_not_persisted_or_returned_and_blank_is_rejected(
        self,
    ):
        status = await dispatch(
            self.bridge,
            {
                "op": "camera.save",
                "id": "front",
                "camera": self.camera,
                "password": "private-test-password",
            },
        )
        self.assertEqual(
            self.credentials.get(Camera(**self.camera)), "private-test-password"
        )
        self.assertNotIn("private-test-password", json.dumps(status))
        self.assertNotIn(
            "private-test-password", (self.paths.config / "settings.json").read_text()
        )
        with self.assertRaises(ValueError):
            await dispatch(
                self.bridge,
                {
                    "op": "camera.save",
                    "id": "front",
                    "camera": self.camera,
                    "password": "",
                },
            )
        self.assertEqual(
            self.credentials.get(Camera(**self.camera)), "private-test-password"
        )

    async def test_invalid_configuration_does_not_replace_a_stored_password(self):
        await dispatch(
            self.bridge,
            {
                "op": "camera.save",
                "id": "front",
                "camera": self.camera,
                "password": "original-password",
            },
        )
        with self.assertRaises(ValueError):
            await dispatch(
                self.bridge,
                {
                    "op": "camera.save",
                    "id": "duplicate",
                    "camera": self.camera,
                    "password": "unwanted-password",
                },
            )
        self.assertEqual(
            self.credentials.get(Camera(**self.camera)), "original-password"
        )
        self.assertEqual(set(self.bridge.settings.cameras), {"front"})

    async def test_camera_cannot_be_removed_while_referenced_even_by_disabled_device(
        self,
    ):
        await dispatch(
            self.bridge, {"op": "camera.save", "id": "front", "camera": self.camera}
        )
        await dispatch(
            self.bridge,
            {
                "op": "device.save",
                "id": "announcement",
                "device": {
                    "name": "Announcement",
                    "direction": "speaker",
                    "cameras": ["front"],
                    "enabled": False,
                },
            },
        )
        with self.assertRaises(ValueError):
            await dispatch(self.bridge, {"op": "camera.remove", "id": "front"})
        self.assertEqual(self.paths.load(), self.bridge.settings)
        await dispatch(self.bridge, {"op": "device.remove", "id": "announcement"})
        await dispatch(self.bridge, {"op": "camera.remove", "id": "front"})
        self.assertEqual(self.paths.load().cameras, {})
        self.assertEqual(self.paths.load().devices, {})

    async def test_unknown_fields_and_non_boolean_switches_cannot_mutate_settings(self):
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
                "device": {"name": "Test", "direction": "speaker", "enabled": 1},
            },
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                await dispatch(self.bridge, command)
        self.assertEqual(self.bridge.settings.cameras, {})
        self.assertEqual(self.bridge.settings.devices, {})
