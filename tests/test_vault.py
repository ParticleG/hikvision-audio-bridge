import stat
import tempfile
import unittest
from pathlib import Path

from hikvision_audio_bridge.config import Camera
from hikvision_audio_bridge.vault import ServerSecrets


class VaultTests(unittest.TestCase):
    def test_private_encrypted_credentials_and_distinct_scopes_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets"
            vault = ServerSecrets(path)
            camera = Camera("http://camera.test", "admin")
            secret = "private camera password"
            vault.set(camera, secret)
            admin, audio = vault.token("admin"), vault.token("audio")
            self.assertNotEqual(admin, audio)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for file in path.iterdir():
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
                for value in (secret, admin, audio):
                    self.assertNotIn(value.encode(), file.read_bytes())
            reopened = ServerSecrets(path)
            self.assertEqual(reopened.get(camera), secret)
            self.assertEqual(reopened.token("admin"), admin)
            self.assertEqual(reopened.token("audio"), audio)
            with self.assertRaises(ValueError):
                reopened.token("desktop")

    def test_lost_key_does_not_replace_or_erase_existing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            vault = ServerSecrets(path)
            vault.set(Camera("http://camera.test", "admin"), "private password")
            (encrypted,) = path.glob("*.enc")
            before = encrypted.read_bytes()
            (path / "key").unlink()
            with self.assertRaises(RuntimeError):
                ServerSecrets(path)
            self.assertEqual(encrypted.read_bytes(), before)
            self.assertFalse((path / "key").exists())

    def test_public_or_corrupt_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            vault = ServerSecrets(path)
            camera = Camera("http://camera.test", "admin")
            vault.set(camera, "private password")
            (encrypted,) = path.glob("*.enc")
            encrypted.chmod(0o644)
            with self.assertRaises(RuntimeError):
                vault.get(camera)
            encrypted.chmod(0o600)
            encrypted.write_bytes(b"corrupted ciphertext")
            with self.assertRaises(RuntimeError):
                vault.get(camera)
            (path / "key").chmod(0o644)
            with self.assertRaises(RuntimeError):
                ServerSecrets(path)
