import asyncio
import struct
import tempfile
import unittest
import wave
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from hikvision_audio_bridge.chimes import (
    CHIME_DIRECTORY,
    CHIME_NAMES,
    chime_pcm,
)
from hikvision_audio_bridge.config import Camera, Paths
from hikvision_audio_bridge.server import Gateway
from hikvision_audio_bridge.tts import DEFAULT_VOICE
from hikvision_audio_bridge.vault import ServerSecrets

SPEECH = b"\x30\x40" * 400
SPEECH_KEYS = {"id", "cameras", "voice", "state", "error"}


class ChimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = Paths(root / "run", root / "config")
        self.paths.prepare()
        self.bridge = Gateway(self.paths, ServerSecrets(root / "secrets"))
        await self.bridge.dispatch(
            {
                "op": "camera.save",
                "id": "front",
                "camera": asdict(Camera("http://camera.test", "admin")),
            }
        )
        self.played = []

    async def asyncTearDown(self):
        await self.bridge.close()
        self.temporary.cleanup()

    async def provider(self, text, voice):
        self.assertEqual((text, voice), ("hello", DEFAULT_VOICE))
        return SPEECH

    def recording_play(self):
        async def play(pcm, targets):
            self.played.append((pcm, tuple(targets)))

        return patch.object(self.bridge.engine, "play_pcm", new=play)

    def test_bundled_cues_are_short_eight_kilohertz_mono_pcm(self):
        cues = {}
        for name in CHIME_NAMES:
            with self.subTest(chime=name):
                pcm = chime_pcm(name)
                self.assertTrue(pcm)
                self.assertEqual(len(pcm) % 2, 0)
                samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
                peak = max(abs(sample) for sample in samples)
                # Audible on a camera speaker with headroom rather than clipping.
                self.assertGreater(peak, 0.05 * 32768)
                self.assertLess(peak, 0.75 * 32768)
                self.assertTrue(0.05 <= len(pcm) / 16000 <= 1.0)
                cues[name] = pcm
        self.assertEqual(len(set(cues.values())), len(CHIME_NAMES))
        packaged = sorted(CHIME_DIRECTORY.glob("*.wav"))
        self.assertEqual(len(packaged), len(CHIME_NAMES))
        for path in packaged:
            with self.subTest(packaged=path.name), wave.open(str(path)) as archive:
                self.assertEqual(archive.getnchannels(), 1)
                self.assertEqual(archive.getsampwidth(), 2)
                self.assertEqual(archive.getframerate(), 8000)

    async def test_invalid_chime_is_rejected_before_any_playback(self):
        queue = self.bridge.speech

        async def provider(_text, _voice):
            raise AssertionError("synthesis must not start")

        with patch("hikvision_audio_bridge.server.synthesize", new=provider):
            for chime in ("chime", "", "SUCCESS", 1, True, b"success", None):
                with self.subTest(chime=chime):
                    with self.assertRaises(ValueError):
                        await self.bridge.dispatch(
                            {
                                "op": "tts.speak",
                                "text": "hello",
                                "cameras": ["front"],
                                "chime": chime,
                            }
                        )
            for chime in ("chime", "", "SUCCESS", 1, True, b"success"):
                with self.subTest(enqueue=chime), self.assertRaises(ValueError):
                    queue.enqueue("hello", ["front"], DEFAULT_VOICE, chime)
        self.assertIsNone(queue.worker)
        self.assertEqual(queue.status()["queue"], [])
        self.assertEqual(queue.completed, {})
        self.assertEqual(self.played, [])

    async def test_omitted_chime_plays_speech_only_with_unchanged_receipts(self):
        queue = self.bridge.speech
        with (
            patch("hikvision_audio_bridge.server.synthesize", new=self.provider),
            self.recording_play(),
        ):
            identifier = queue.enqueue("hello", ["front"], DEFAULT_VOICE)
            await queue.worker
        self.assertEqual(self.played, [(SPEECH, ("front",))])
        receipt = queue.job_status(identifier)
        self.assertEqual(set(receipt), SPEECH_KEYS)
        self.assertEqual(receipt["state"], "completed")
        self.assertNotIn("chime", receipt)

    async def test_cancelling_synthesis_never_plays_a_chime_alone(self):
        queue = self.bridge.speech
        started = asyncio.Event()

        async def provider(_text, _voice):
            started.set()
            await asyncio.Event().wait()

        with (
            patch("hikvision_audio_bridge.server.synthesize", new=provider),
            self.recording_play(),
        ):
            identifier = queue.enqueue("hello", ["front"], DEFAULT_VOICE, "error")
            worker = queue.worker
            await asyncio.wait_for(started.wait(), 3)
            await queue.cancel(identifier)
            await asyncio.wait_for(worker, 3)
        self.assertEqual(self.played, [])
        self.assertEqual(queue.job_status(identifier)["state"], "cancelled")
