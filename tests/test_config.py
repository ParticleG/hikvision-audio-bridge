import json
import stat
import tempfile
import unittest
from pathlib import Path

from hikvision_audio_bridge.config import Camera, Paths, Settings


class ConfigTests(unittest.TestCase):
    def test_credentials_cannot_be_embedded_in_camera_url(self):
        for value in (
            "http://admin:password@camera.test",
            "file:///tmp/camera",
            "https://camera.test/other",
            "http://camera.test?password=secret",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Camera(value, "admin")

    def test_device_switches_require_boolean_and_configured_camera(self):
        for value in (
            {"speaker": True},
            {"microphone": "false"},
            {"password": "secret"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings.from_dict(value)

    def test_saved_settings_are_private_and_contain_no_password_field(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths(Path(directory) / "run", Path(directory) / "config")
            paths.prepare()
            settings = Settings(Camera("http://camera.test/", "admin"), True, False)
            paths.save(settings)
            self.assertEqual(paths.load(), settings)
            stored = paths.config / "settings.json"
            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(paths.runtime.stat().st_mode), 0o700)
            self.assertEqual(
                set(json.loads(stored.read_text())["camera"]),
                {"url", "username", "channel"},
            )
