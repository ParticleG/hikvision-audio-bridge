import json
import stat
import tempfile
import unittest
from pathlib import Path

from hikvision_audio_bridge.config import (
    AudioDevice,
    Camera,
    Paths,
    Settings,
    WebSettings,
)


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

    def test_rejects_invalid_routes_switches_ids_and_secret_fields(self):
        camera = {"url": "http://camera.test", "username": "admin", "channel": 1}
        device = {"name": "Entrance", "direction": "speaker", "cameras": ["front"]}
        for value in (
            {"password": "secret"},
            {"cameras": {"front": {**camera, "password": "secret"}}},
            {"cameras": {"../front": camera}},
            {"devices": {"speaker": device}},
            {"cameras": {"front": camera, "duplicate": camera}},
            {
                "cameras": {
                    "front": camera,
                    "duplicate": {**camera, "url": "http://CAMERA.test:80/"},
                }
            },
            {
                "cameras": {"front": camera},
                "devices": {"one": {**device, "enabled": "false"}},
            },
            {
                "cameras": {"front": camera},
                "devices": {"one": {**device, "cameras": ["front", "front"]}},
            },
            {"devices": {"one": {**device, "direction": "other", "cameras": []}}},
            {"devices": {"one": {**device, "cameras": "front"}}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings.from_dict(value)

    def test_multiple_devices_round_trip_with_private_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths(Path(directory) / "run", Path(directory) / "config")
            paths.prepare()
            settings = Settings(
                cameras={
                    "front": Camera("http://front.test/", "admin"),
                    "back": Camera("https://back.test", "operator", 2),
                },
                devices={
                    "broadcast": AudioDevice("Broadcast", "speaker", ("front", "back")),
                    "listen": AudioDevice("Listen", "microphone", ("front", "back")),
                    "front": AudioDevice("Front", "speaker", ("front",), False),
                },
                web=WebSettings(port=9876),
            )
            paths.save(settings)
            self.assertEqual(paths.load(), settings)
            stored = paths.config / "settings.json"
            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(paths.runtime.stat().st_mode), 0o700)

    def test_migrates_published_settings_preserving_device_identity_and_switches(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths(Path(directory) / "run", Path(directory) / "config")
            paths.prepare()
            stored = paths.config / "settings.json"
            stored.write_text(
                json.dumps(
                    {
                        "camera": {
                            "url": "http://camera.test",
                            "username": "admin",
                            "channel": 1,
                        },
                        "speaker": True,
                        "microphone": False,
                    }
                )
            )
            settings = paths.load()
            self.assertEqual(settings.devices["speaker"].cameras, ("default",))
            self.assertTrue(settings.devices["speaker"].enabled)
            self.assertFalse(settings.devices["microphone"].enabled)
            self.assertEqual(
                settings.cameras["default"], Camera("http://camera.test", "admin")
            )
            paths.save(settings)
            self.assertEqual(paths.load(), settings)
            self.assertNotIn("camera", json.loads(stored.read_text()))

    def test_legacy_migration_does_not_accept_invalid_switches_or_mixed_formats(self):
        for value in (
            {"speaker": True},
            {"microphone": "false"},
            {"camera": None, "cameras": {}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings.migrate(value)

    def test_web_settings_reject_partial_tls_and_invalid_addresses(self):
        for value in (
            {"host": "example.com"},
            {"port": True},
            {"port": 65536},
            {"tls_cert": "cert.pem"},
            {"tls_key": "key.pem"},
            {"token": "secret"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WebSettings.from_dict(value)
