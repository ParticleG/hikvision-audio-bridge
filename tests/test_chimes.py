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
    CHIME_DURATIONS,
    CHIME_TONES,
    Chime,
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

    def test_packaged_cues_match_every_tone_and_duration(self):
        expected = {
            f"{tone}-{duration}.wav"
            for tone in CHIME_TONES
            for duration in CHIME_DURATIONS
        }
        packaged = sorted(CHIME_DIRECTORY.glob("*.wav"))
        self.assertEqual({path.name for path in packaged}, expected)
        self.assertEqual(len(packaged), len(CHIME_TONES) * len(CHIME_DURATIONS))
        cues = set()
        for path in packaged:
            with self.subTest(packaged=path.name):
                with wave.open(str(path)) as archive:
                    self.assertEqual(archive.getnchannels(), 1)
                    self.assertEqual(archive.getsampwidth(), 2)
                    self.assertEqual(archive.getframerate(), 8000)
                    self.assertEqual(archive.getcomptype(), "NONE")
                    frames = archive.readframes(archive.getnframes())
                self.assertTrue(frames)
                self.assertEqual(len(frames) % 2, 0)
                samples = struct.unpack(f"<{len(frames) // 2}h", frames)
                peak = max(abs(sample) for sample in samples)
                self.assertGreater(peak, 0)
                # Rendered cues keep headroom instead of sitting on the int16 rails.
                self.assertLess(peak, 32767)
                cues.add(frames)
        self.assertEqual(len(cues), len(expected))
        for tone in CHIME_TONES:
            for duration in CHIME_DURATIONS:
                with self.subTest(tone=tone, duration=duration):
                    self.assertIn(chime_pcm(Chime(tone, duration)), cues)

    async def test_invalid_chime_and_plays_are_rejected_before_playback(self):
        queue = self.bridge.speech
        invalid_chimes = (
            "success",
            {},
            {"tone": "success"},
            {"duration": "short"},
            {"tone": "success", "duration": "short", "extra": 1},
            {"tone": 1, "duration": "short"},
            {"tone": "success", "duration": 2},
            {"tone": "chime", "duration": "short"},
            {"tone": "success", "duration": "forever"},
            ["success", "short"],
            None,
        )
        invalid_plays = (0, 6, True, 1.5, "2", None)
        valid_chime = {"tone": "success", "duration": "short"}

        async def provider(_text, _voice):
            raise AssertionError("synthesis must not start")

        with patch("hikvision_audio_bridge.server.synthesize", new=provider):
            for chime in invalid_chimes:
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
                    if chime is not None:
                        with self.assertRaises(ValueError):
                            queue.enqueue("hello", ["front"], DEFAULT_VOICE, chime)
            for plays in invalid_plays:
                with self.subTest(plays=plays):
                    with self.assertRaises(ValueError):
                        await self.bridge.dispatch(
                            {
                                "op": "tts.speak",
                                "text": "hello",
                                "cameras": ["front"],
                                "chime": valid_chime,
                                "plays": plays,
                            }
                        )
                    with self.assertRaises(ValueError):
                        queue.enqueue(
                            "hello",
                            ["front"],
                            DEFAULT_VOICE,
                            valid_chime,
                            plays,
                        )
        self.assertIsNone(queue.worker)
        self.assertIsNone(queue.current)
        self.assertEqual(queue.stage, "idle")
        self.assertEqual(queue.status()["queue"], [])
        self.assertEqual(queue.completed, {})
        self.assertEqual(self.played, [])

    async def test_omitted_chime_and_plays_play_speech_only(self):
        queue = self.bridge.speech
        with (
            patch("hikvision_audio_bridge.server.synthesize", new=self.provider),
            self.recording_play(),
        ):
            result = await self.bridge.dispatch(
                {"op": "tts.speak", "text": "hello", "cameras": ["front"]}
            )
            await asyncio.wait_for(queue.worker, 3)
            identifier = queue.enqueue("hello", ["front"], DEFAULT_VOICE)
            await asyncio.wait_for(queue.worker, 3)
        self.assertEqual(self.played, [(SPEECH, ("front",)), (SPEECH, ("front",))])
        for job in (result["queued_job"], identifier):
            with self.subTest(job=job):
                receipt = queue.job_status(job)
                self.assertEqual(set(receipt), SPEECH_KEYS)
                self.assertEqual(receipt["state"], "completed")

    async def test_cancelling_synthesis_never_plays_a_cue(self):
        queue = self.bridge.speech
        started = asyncio.Event()

        async def provider(_text, _voice):
            started.set()
            await asyncio.Event().wait()

        with (
            patch("hikvision_audio_bridge.server.synthesize", new=provider),
            self.recording_play(),
        ):
            identifier = queue.enqueue(
                "hello",
                ["front"],
                DEFAULT_VOICE,
                {"tone": "error", "duration": "short"},
            )
            worker = queue.worker
            await asyncio.wait_for(started.wait(), 3)
            await queue.cancel(identifier)
            await asyncio.wait_for(worker, 3)
        self.assertEqual(self.played, [])
        self.assertEqual(queue.job_status(identifier)["state"], "cancelled")
