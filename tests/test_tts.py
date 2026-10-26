import asyncio
import base64
import unittest
from unittest import mock

import audioop

from hikvision_audio_bridge import tts

# 0.2 s of a 440 Hz tone encoded as 24 kHz mono 48 kbit/s MP3. FFmpeg decodes
# the stream to roughly 4 KiB of 8 kHz mono PCM16, including encoder padding.
MP3_FIXTURE = base64.b64decode(
    """
SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYzLjEuMTAyAAAAAAAAAAAAAAD/82TEABkAYoA/WBgA
C9bbdu5bls7Z2u9ib9vQkIYxmMZjCWbLxqDtfh+npKSkpKSkBAEwfB8H+GOD/KBiUdz/KO0B/gR2
gP8CO4f5R3f0e+D4PgQEAQBAMA+D4Pg+BAQOY/o9/8MA+D4Pn0AgCBzE74AYFoMBgxg8RFQy4qmY
XpIBoxCyGA0LiaT4Uhgui2H/82TEHySKElADnqAApGC0mKKGUYNoMA8CS2crAlbQDEJQNAbAzB9J
0eAkIBhQQGDDBsq/4WQg30R0GrgyN/4xogqIKjlC5hW3/5DRzRlSaHOHOJn//IqRUyIsRYxLpdMv
//yLEWMS6XTIvF5EumqX/4NCUJB0r///7FwqVFAqCqoKxFNRLmlOND4AALBQApj/82TEDxpQWih9
3hAABQAZglhoGGER4YLweJhWieHzDGcaNgm5hghiGDGC4YNYGBgCAKmAEAGGACqVNOi1l1Jwrq9N
n45t8bXoTTQhhm1s41datfT/7PfdV4h26dnHrVHLWuX0//fjqgISQUrawl+BAAAwAsAVMAoAQDAe
wO8w0sUTMECAHTA7w1c3k6b9MRr/82TEKCI8LgxS/sRpwc0+CUNVQTJh4xENMEAiyaY7LIvg3ZV0
pet7WvfBNZn/e8e6vqyPlpWPJdR9GsjpSn1va1loyVgvsgjtfR061mhe/3vbz9u8fWrL0pWD+urL
TeyyScne/o2la9LXXfuCyzBkDL0fAMADmAKABRgHoBaYFUBWGJIBuxg0YFcMiYRqpur/82TEIiK0
Kggo/sppHGF/BRxvvGZA3GCnYIIASGggATAX/CLXVhqb1tN/dX7LT+0cptX1ZK06SV1a6MrXRjz/
Na1rDaMM7s9p1Ee19HRk6RsBrUvZXureXvyKburL0bSn3tHKyUdaUv17Wvo2nJWW9o6/cYxxH6Vh
Gl0medfg0ABiQBgBgLswtcBAMEUAkzD/82TEGh1K7hhU/sRoJoMFMEDhJzAOgZkxWKMEPwaPCILS
mZHA75T3H1zpWnL37q+rLen9Y2vWnf572tdKsrrW327p129f5u/ptpPSA3t2Xr9P+NvdWuvyCGMa
xrrLnPujEqSiIW0kCCoRIdfptVfCQACYA2ACGAxAKZhigU+YKKBnGCXhqxnlkdAYSWDgHDT/82TE
JxqqdhQo/sRo+ZylGME5gocXNbtCXWiuT606ff/Xr1/7o2ra7JL9LfpWtaU+9/12Xp9Y3f0r/WKQ
nbbv45u6tUp0pQxnZdf3Lr9KKUUBgSHGoy7TDUTjAQEzDEdDfPIjNooTBHAis0mFa7MK8BHTl2Az
4rMbEDBgFFKBZqHZdx9Xooobaz7n3LXVr03/82TEPxeoWhxS7/ZAkkm3OOdWtdPL+xuxh3/X0J0f
fvvGKUtS6dOi1rPuvGpu6w5fMwAYAiMAcAIDAUAEowG8FZMDqD8zCVQz8xNgRCNYGMyDGewh0wGY
CQMBEAbjAlgGQwAQADMABADzAHQAUCgBKfcinBAt7vp29ntPXV+rTS2z/Hr18tRs95679XiTRs//
82TEYxpIWhwBXxAA/+j//dj3VRIghAYkCYYvDYYSCeYpDfWtA1MQmszLY/TG4+aspM+iSAw4GfxW
mMAo2LoHd4GsoW9gZeAeZAc7IskFhAWGCfBW4CwQAggADDklcSmSZQIIeC4YLQhSIgqHw/RL7n0F
jlCghCxDRcouX80oE+mmThDhzhziZIqQEpfy4aH/82TEfDJiolQxnZgAEy+bkULhoRcixFjEukyZ
F4vGP/TTLiCBummaIIG5dLqReLy0UVJJL//TTNEEDdNM0QQNw+CAKiJ53/8Bh8EAGHwQAYfBABh/
tVaWhIGlTEFNRTQuMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVX/82TENQAAA0gBwAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU=
"""
)


class FakeStream:
    """Provider stream with optional blocking after its finite messages."""

    def __init__(self, messages=(), *, block=False, gate_close=False):
        self.messages = list(messages)
        self.block = block
        self.gate_close = gate_close
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self._blocker = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            self.started.set()
            return self.messages.pop(0)
        if self.block:
            self.started.set()
            await self._blocker.wait()
        raise StopAsyncIteration

    async def aclose(self):
        self.close_started.set()
        if self.gate_close:
            try:
                await self.release_close.wait()
            except asyncio.CancelledError:
                self.close_cancelled.set()
                raise
        self.closed.set()


class ExplodingStream:
    def __init__(self, error):
        self.error = error
        self.closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self.error

    async def aclose(self):
        self.closed.set()


def provider(stream, recorded):
    class Communicate:
        def __init__(self, text, voice):
            recorded.append((text, voice))

        def stream(self):
            return stream

    return mock.patch.object(tts.edge_tts, "Communicate", Communicate)


def capture_processes(processes):
    original = tts._spawn_ffmpeg

    async def spawn():
        process = await original()
        processes.append(process)
        return process

    return mock.patch.object(tts, "_spawn_ffmpeg", spawn)


class TTSTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_text_is_rejected_before_network_or_ffmpeg(self):
        recorded = []
        spawned = []

        async def never_spawn():
            spawned.append(True)
            raise AssertionError("FFmpeg must not start for invalid text")

        with (
            provider(FakeStream(), recorded),
            mock.patch.object(tts, "_spawn_ffmpeg", never_spawn),
        ):
            for value in (
                "",
                " \t\n ",
                "bell\x07",
                "null\x00",
                "lone\ud800surrogate",
                "non\ufffechar",
                "x" * (tts.MAX_TEXT_CHARACTERS + 1),
            ):
                with self.assertRaises(ValueError):
                    await tts.synthesize(value)
            with self.assertRaises(TypeError):
                await tts.synthesize(None)  # ty: ignore[invalid-argument-type]

        self.assertEqual(recorded, [])
        self.assertEqual(spawned, [])
        self.assertEqual(
            tts._validate_text("x" * tts.MAX_TEXT_CHARACTERS),
            "x" * tts.MAX_TEXT_CHARACTERS,
        )

    async def test_invalid_voice_is_rejected_before_network_or_ffmpeg(self):
        recorded = []
        spawned = []

        async def never_spawn():
            spawned.append(True)
            raise AssertionError("FFmpeg must not start for invalid voice")

        with (
            provider(FakeStream(), recorded),
            mock.patch.object(tts, "_spawn_ffmpeg", never_spawn),
        ):
            for voice in (
                "",
                "zh CN",
                "zh-CN-",
                "-XiaoxiaoNeural",
                "zh-CN-XiaoxiaoNeural!",
                "<speak>",
                "Microsoft Server Speech Text to Speech Voice (zh-CN, Xiaoxiao)",
                "zh-" + "a" * 100,
            ):
                with self.assertRaises(ValueError):
                    await tts.synthesize("hello", voice)
            with self.assertRaises(TypeError):
                await tts.synthesize("hello", None)  # ty: ignore[invalid-argument-type]

        self.assertEqual(recorded, [])
        self.assertEqual(spawned, [])

    async def test_real_ffmpeg_decodes_provider_mp3_to_pcm(self):
        stream = FakeStream(
            [
                {"type": "audio", "data": MP3_FIXTURE[:768]},
                {"type": "audio", "data": MP3_FIXTURE[768:]},
                {"type": "SentenceBoundary", "text": "ignored metadata"},
            ]
        )
        recorded = []
        processes = []

        with provider(stream, recorded), capture_processes(processes):
            pcm = await tts.synthesize("  A & B < C > D  ")

        # 0.2 s at 8 kHz mono is 3200 bytes; encoder padding stays well below
        # one second, while 24 kHz or stereo output would exceed this range.
        self.assertGreater(len(pcm), 1600)
        self.assertLess(len(pcm), 8000)
        self.assertEqual(len(pcm) % 2, 0)
        self.assertGreater(audioop.rms(pcm, 2), 1000)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        self.assertTrue(stream.closed.is_set())

    async def test_empty_provider_response_is_an_error(self):
        stream = FakeStream()
        recorded = []
        processes = []

        with provider(stream, recorded), capture_processes(processes):
            with self.assertRaisesRegex(tts.TTSError, "returned no audio"):
                await tts.synthesize("hello")

        self.assertTrue(stream.closed.is_set())
        self.assertTrue(processes)
        self.assertIsNotNone(processes[0].returncode)

    async def test_invalid_mp3_fails_explicitly_and_reaps_ffmpeg(self):
        stream = FakeStream([{"type": "audio", "data": b"not an mp3" * 100}])
        recorded = []
        processes = []

        with provider(stream, recorded), capture_processes(processes):
            with self.assertRaises(tts.TTSError) as caught:
                await tts.synthesize("private spoken words")

        self.assertIn("FFmpeg", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(stream.closed.is_set())
        self.assertTrue(processes)
        self.assertIsNotNone(processes[0].returncode)

    async def test_provider_failure_hides_text_and_reaps_process(self):
        stream = ExplodingStream(RuntimeError("provider secret: private text"))
        recorded = []
        processes = []

        with provider(stream, recorded), capture_processes(processes):
            with self.assertRaises(tts.TTSError) as caught:
                await tts.synthesize("private spoken words")

        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(stream.closed.is_set())
        self.assertTrue(processes)
        self.assertIsNotNone(processes[0].returncode)

    async def test_missing_ffmpeg_is_reported(self):
        stream = FakeStream([{"type": "audio", "data": MP3_FIXTURE}])
        recorded = []

        async def missing():
            raise FileNotFoundError

        with (
            provider(stream, recorded),
            mock.patch.object(tts, "_spawn_ffmpeg", missing),
        ):
            with self.assertRaisesRegex(tts.TTSError, "not installed"):
                await tts.synthesize("hello")

        self.assertTrue(stream.closed.is_set())

    async def test_timeout_closes_stream_and_reaps_ffmpeg(self):
        stream = FakeStream(block=True)
        recorded = []
        processes = []

        with provider(stream, recorded), capture_processes(processes):
            with mock.patch.object(tts, "REQUEST_TIMEOUT", 0.05):
                async with asyncio.timeout(3):
                    with self.assertRaisesRegex(tts.TTSError, "timed out"):
                        await tts.synthesize("hello")

        self.assertTrue(stream.closed.is_set())
        self.assertTrue(processes)
        self.assertIsNotNone(processes[0].returncode)

    async def test_cancellation_during_spawn_reaps_ffmpeg(self):
        stream = FakeStream([{"type": "audio", "data": MP3_FIXTURE}])
        recorded = []
        processes = []
        spawn_started = asyncio.Event()
        release_spawn = asyncio.Event()
        original = tts._spawn_ffmpeg

        async def slow_spawn():
            process = await original()
            processes.append(process)
            spawn_started.set()
            await release_spawn.wait()
            return process

        with (
            provider(stream, recorded),
            mock.patch.object(tts, "_spawn_ffmpeg", slow_spawn),
        ):
            task = asyncio.create_task(tts.synthesize("hello"))
            await asyncio.wait_for(spawn_started.wait(), 2)
            task.cancel()
            release_spawn.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)

        self.assertTrue(stream.closed.is_set())
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)

    async def test_cleanup_finishes_before_cancellation_propagates(self):
        stream = FakeStream([{"type": "audio", "data": MP3_FIXTURE}], gate_close=True)
        recorded = []
        processes = []

        with provider(stream, recorded), capture_processes(processes):
            task = asyncio.create_task(tts.synthesize("hello"))
            await asyncio.wait_for(stream.close_started.wait(), 2)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
                if task.done() or stream.close_cancelled.is_set():
                    break
            self.assertFalse(stream.close_cancelled.is_set())
            self.assertFalse(task.done())
            stream.release_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)

        self.assertTrue(stream.closed.is_set())
        self.assertTrue(processes)
        self.assertIsNotNone(processes[0].returncode)

    async def test_cancellation_closes_stream_and_reaps_ffmpeg(self):
        stream = FakeStream([{"type": "audio", "data": MP3_FIXTURE}], block=True)
        recorded = []
        processes = []

        with provider(stream, recorded), capture_processes(processes):
            task = asyncio.create_task(tts.synthesize("hello"))
            await asyncio.wait_for(stream.started.wait(), 2)
            self.assertEqual(len(processes), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(stream.closed.is_set())
        self.assertIsNotNone(processes[0].returncode)

    async def test_oversized_mp3_is_rejected(self):
        stream = FakeStream([{"type": "audio", "data": b"x" * 9}])
        recorded = []
        processes = []

        with (
            provider(stream, recorded),
            capture_processes(processes),
            mock.patch.object(tts, "MAX_MP3_BYTES", 8),
        ):
            with self.assertRaisesRegex(tts.TTSError, "too much audio"):
                await tts.synthesize("hello")

        self.assertTrue(stream.closed.is_set())
        self.assertTrue(processes)
        self.assertIsNotNone(processes[0].returncode)

    async def test_oversized_pcm_is_rejected_without_truncation(self):
        stream = FakeStream([{"type": "audio", "data": MP3_FIXTURE}])
        recorded = []
        processes = []

        with (
            provider(stream, recorded),
            capture_processes(processes),
            mock.patch.object(tts, "MAX_PCM_BYTES", 100),
        ):
            with self.assertRaisesRegex(tts.TTSError, "maximum duration"):
                await tts.synthesize("hello")

        self.assertTrue(stream.closed.is_set())
        self.assertTrue(processes)
        self.assertIsNotNone(processes[0].returncode)


if __name__ == "__main__":
    unittest.main()
