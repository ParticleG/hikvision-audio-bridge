import json
import stat
import tempfile
import unittest
from pathlib import Path

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

    def test_server_rejects_secret_fields_invalid_ids_and_duplicate_channels(self):
        camera = {"url": "http://camera.test", "username": "admin", "channel": 1}
        for value in (
            {"password": "secret"},
            {"cameras": {"front": {**camera, "password": "secret"}}},
            {"cameras": {"../front": camera}},
            {"devices": {}},
            {"cameras": {"front": camera, "duplicate": camera}},
            {
                "cameras": {
                    "front": camera,
                    "duplicate": {**camera, "url": "http://CAMERA.test:80/"},
                }
            },
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ServerSettings.from_dict(value)

    def test_desktop_rejects_unknown_groups_non_boolean_switches_and_camera_credentials(
        self,
    ):
        device = {"name": "Entrance", "direction": "speaker", "groups": ["office"]}
        group = {"name": "Office", "cameras": ["front"]}
        for value in (
            {"cameras": {}},
            {"password": "secret"},
            {"devices": {"speaker": device}},
            {
                "groups": {"office": group},
                "devices": {"one": {**device, "enabled": "false"}},
            },
            {
                "groups": {"office": group},
                "devices": {"one": {**device, "groups": ["office", "office"]}},
            },
            {"devices": {"one": {**device, "direction": "other", "groups": []}}},
            {"devices": {"one": {**device, "groups": "office"}}},
            {"groups": {"office": {"name": "Office", "cameras": ["front", "front"]}}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DesktopSettings.from_dict(value)

    def test_group_expansion_deduplicates_overlapping_cameras(self):
        settings = DesktopSettings(
            groups={
                "one": CameraGroup("One", ("front", "back")),
                "two": CameraGroup("Two", ("back", "other")),
            },
            devices={
                "speaker": DesktopDevice("Broadcast", "speaker", ("one", "two")),
                "mic": DesktopDevice("Monitor", "microphone", ("two",), False),
            },
        )
        self.assertEqual(
            settings.routes()["speaker"].cameras, ("front", "back", "other")
        )
        self.assertEqual(settings.routes()["mic"].cameras, ("back", "other"))
        self.assertFalse(settings.routes()["mic"].enabled)

    def test_multiple_devices_round_trip_with_private_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths(Path(directory) / "run", Path(directory) / "config")
            settings = DesktopSettings(
                BackendSettings("https://backend.test"),
                {"office": CameraGroup("Office", ("front", "back"))},
                {
                    "broadcast": DesktopDevice("Broadcast", "speaker", ("office",)),
                    "listen": DesktopDevice("Listen", "microphone", ("office",), False),
                },
                WebSettings(port=9876),
            )
            paths.save(settings)
            self.assertEqual(paths.load_desktop(), settings)
            self.assertEqual(
                stat.S_IMODE((paths.config / "settings.json").stat().st_mode), 0o600
            )
            self.assertEqual(stat.S_IMODE(paths.runtime.stat().st_mode), 0o700)

    def test_legacy_migration_preserves_routes_identity_switches_and_private_backup(
        self,
    ):
        for previous in (
            {
                "camera": {
                    "url": "http://camera.test",
                    "username": "admin",
                    "channel": 1,
                },
                "speaker": True,
                "microphone": False,
            },
            {
                "cameras": {
                    "front": {
                        "url": "http://camera.test",
                        "username": "admin",
                        "channel": 1,
                    }
                },
                "devices": {
                    "speaker": {
                        "name": "Original speaker",
                        "direction": "speaker",
                        "cameras": ["front"],
                    },
                    "microphone": {
                        "name": "Original microphone",
                        "direction": "microphone",
                        "cameras": ["front"],
                        "enabled": False,
                    },
                },
            },
        ):
            with (
                self.subTest(previous=previous),
                tempfile.TemporaryDirectory() as directory,
            ):
                paths = Paths(Path(directory) / "run", Path(directory) / "config")
                paths.prepare()
                stored = paths.config / "settings.json"
                stored.write_text(json.dumps(previous))
                settings = paths.load_desktop()
                self.assertEqual(set(settings.devices), {"speaker", "microphone"})
                camera = "default" if "camera" in previous else "front"
                self.assertEqual(settings.routes()["speaker"].cameras, (camera,))
                self.assertTrue(settings.devices["speaker"].enabled)
                self.assertFalse(settings.devices["microphone"].enabled)
                if "devices" in previous:
                    self.assertEqual(
                        settings.devices["speaker"].name, "Original speaker"
                    )
                self.assertIsNone(settings.backend)
                self.assertEqual(json.loads(stored.read_text()), previous)
                paths.save(settings)
                (backup,) = paths.config.glob("settings-legacy-*.json")
                self.assertEqual(json.loads(backup.read_text()), previous)
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
                self.assertEqual(paths.load_desktop(), settings)
                self.assertNotIn("cameras", json.loads(stored.read_text()))

    def test_legacy_migration_does_not_accept_invalid_switches_or_mixed_formats(self):
        for value in (
            {"speaker": True},
            {"microphone": "false"},
            {"camera": None, "cameras": {}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DesktopSettings.migrate(value)

    def test_lan_http_requires_explicit_desktop_opt_in(self):
        with self.assertRaises(ValueError):
            BackendSettings("http://192.0.2.10:8765")
        permitted = BackendSettings("http://192.0.2.10:8765", allow_insecure_lan=True)
        self.assertEqual(permitted.url, "http://192.0.2.10:8765")
        with self.assertRaises(ValueError):
            BackendSettings.from_dict(
                {"url": "https://backend.test", "token": "secret"}
            )

    def test_web_settings_reject_partial_tls_invalid_addresses_and_non_boolean_opt_in(
        self,
    ):
        for value in (
            {"host": "example.com"},
            {"port": True},
            {"port": 65536},
            {"tls_cert": "cert.pem"},
            {"tls_key": "key.pem"},
            {"token": "secret"},
            {"allow_insecure_lan": "true"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WebSettings.from_dict(value)
