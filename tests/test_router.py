import struct
import unittest

from hikvision_audio_bridge.config import AudioDevice, Camera, Settings
from hikvision_audio_bridge.pipewire import SILENCE
from hikvision_audio_bridge.router import (
    MAX_OUTPUT_FRAMES,
    AudioRouter,
    SpeechPlayback,
    mix,
)


def pcm(value):
    return struct.pack("<160h", *([value] * 160))


def sample(frame, index=0):
    return struct.unpack_from("<h", frame, index * 2)[0]


class FakeDevices:
    def __init__(self):
        self.frames = {}
        self.reads = {}
        self.microphones = {}
        self.devices = {}
        self.flushes = []

    def read_speaker(self, identifier):
        self.reads[identifier] = self.reads.get(identifier, 0) + 1
        value = self.frames.get(identifier, SILENCE)
        if isinstance(value, list):
            return value.pop(0) if value else SILENCE
        return value

    def write_microphone(self, identifier, pcm):
        self.microphones.setdefault(identifier, bytearray()).extend(pcm)

    def flush(self, identifiers=None):
        self.flushes.append(None if identifiers is None else set(identifiers))


def settings(devices):
    return Settings(
        cameras={
            "c1": Camera("http://c1.test", "admin"),
            "c2": Camera("http://c2.test", "admin"),
        },
        devices=devices,
    )


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.devices = FakeDevices()
        self.router = AudioRouter(lambda: self.devices)

    async def asyncTearDown(self):
        await self.router.close()

    def test_one_speaker_frame_is_read_once_and_fanned_out(self):
        self.router.configure(
            settings(
                {
                    "s1": AudioDevice("S1", "speaker", ("c1", "c2")),
                    "s2": AudioDevice("S2", "speaker", ("c1",)),
                }
            )
        )
        self.devices.frames["s1"] = pcm(1000)
        self.devices.frames["s2"] = pcm(3000)
        self.router.tick()
        self.assertEqual(self.devices.reads["s1"], 1)
        self.assertEqual(self.devices.reads["s2"], 1)
        self.assertEqual(sample(self.router.read_camera("c1")), 2000)
        self.assertEqual(sample(self.router.read_camera("c2")), 1000)

        self.router.tick()
        self.assertEqual(self.devices.reads["s1"], 2)
        self.assertEqual(sample(self.router.read_camera("c2")), 1000)

    def test_camera_input_is_consumed_once_for_multiple_microphones(self):
        self.router.configure(
            settings(
                {
                    "m1": AudioDevice("M1", "microphone", ("c1", "c2")),
                    "m2": AudioDevice("M2", "microphone", ("c1", "c2")),
                }
            )
        )
        self.router.consume_camera("c1", pcm(1000))
        self.router.consume_camera("c2", pcm(3000))
        self.router.tick()
        expected = pcm(2000)
        self.assertEqual(bytes(self.devices.microphones["m1"]), expected)
        self.assertEqual(bytes(self.devices.microphones["m2"]), expected)

        self.devices.microphones.clear()
        self.router.tick()
        self.assertEqual(bytes(self.devices.microphones["m1"]), SILENCE)
        self.assertEqual(bytes(self.devices.microphones["m2"]), SILENCE)

    def test_equal_gain_mix_cannot_overflow(self):
        self.assertEqual(sample(mix([pcm(1000), pcm(3000)])), 2000)
        self.assertEqual(sample(mix([pcm(30000), pcm(30000)])), 30000)
        self.assertEqual(sample(mix([SILENCE, pcm(2000)])), 2000)
        self.assertEqual(mix([]), SILENCE)
        self.assertEqual(mix([SILENCE]), SILENCE)

    def test_route_edits_flush_old_and_new_device_fifos(self):
        first = settings(
            {
                "s1": AudioDevice("S1", "speaker", ("c1",)),
                "m1": AudioDevice("M1", "microphone", ("c1",)),
            }
        )
        self.router.configure(first)
        self.devices.flushes.clear()
        second = settings(
            {
                "s1": AudioDevice("S1", "speaker", ("c2",)),
                "m1": AudioDevice("M1", "microphone", ("c2",)),
            }
        )
        self.router.configure(second)
        self.assertEqual(self.devices.flushes, [{"s1", "m1"}])
        self.devices.flushes.clear()
        self.router.configure(second)
        self.assertEqual(self.devices.flushes, [])

    def test_route_edits_drop_queued_audio_for_the_old_path(self):
        first = settings({"s1": AudioDevice("S1", "speaker", ("c1", "c2"))})
        self.router.configure(first)
        self.devices.frames["s1"] = pcm(1000)
        self.router.tick()
        self.router.tick()
        self.assertEqual(sample(self.router.read_camera("c1")), 1000)

        second = settings({"s1": AudioDevice("S1", "speaker", ("c2",))})
        self.router.configure(second)
        self.assertEqual(self.router.read_camera("c1"), SILENCE)

        self.router.consume_camera("c1", pcm(2000))
        self.router.configure(
            settings({"m1": AudioDevice("M1", "microphone", ("c2",))})
        )
        self.router.tick()
        self.assertEqual(bytes(self.devices.microphones["m1"]), SILENCE)

    def test_speech_targets_are_deduplicated_and_complete_after_delivery(self):
        self.router.configure(
            settings({"s1": AudioDevice("S1", "speaker", ("c1", "c2"))})
        )
        first = pcm(8000)
        second = pcm(4000)
        playback = SpeechPlayback(first + second, ("c1", "c1", "c2"))
        self.assertEqual(playback.targets, ("c1", "c2"))
        self.router.begin_speech(playback)
        self.router.tick()
        self.router.tick()
        self.assertEqual(playback.queued, {"c1": 2, "c2": 2})
        self.assertFalse(playback.finished.is_set())

        self.assertEqual(self.router.read_camera("c1"), first)
        self.assertEqual(playback.delivered["c1"], 0)
        self.assertEqual(self.router.read_camera("c1"), second)
        self.assertEqual(playback.delivered["c1"], 1)
        self.assertFalse(playback.finished.is_set())
        self.assertEqual(self.router.read_camera("c1"), SILENCE)
        self.assertEqual(playback.delivered["c1"], 2)
        self.assertFalse(playback.finished.is_set())

        self.assertEqual(self.router.read_camera("c2"), first)
        self.assertEqual(self.router.read_camera("c2"), second)
        self.assertEqual(self.router.read_camera("c2"), SILENCE)
        self.assertTrue(playback.finished.is_set())

    def test_clearing_speech_removes_queued_speech_frames(self):
        self.router.configure(settings({"s1": AudioDevice("S1", "speaker", ("c1",))}))
        playback = SpeechPlayback(pcm(8000), ("c1",))
        self.router.begin_speech(playback)
        self.router.tick()
        self.router.clear_speech()
        self.assertIsNone(self.router.speech)
        self.assertFalse(playback.finished.is_set())
        self.assertEqual(self.router.read_camera("c1"), SILENCE)

    def test_camera_output_is_bounded_and_discards_old_live_audio(self):
        self.router.configure(settings({"s1": AudioDevice("S1", "speaker", ("c1",))}))
        self.devices.frames["s1"] = [pcm(index) for index in range(1, 8)]
        for _ in range(7):
            self.router.tick()
        values = [
            sample(self.router.read_camera("c1")) for _ in range(MAX_OUTPUT_FRAMES)
        ]
        self.assertEqual(values, [3, 4, 5, 6, 7])
        self.assertEqual(self.router.read_camera("c1"), SILENCE)

    def test_speech_completion_requires_a_next_camera_read(self):
        self.router.configure(settings({"s1": AudioDevice("S1", "speaker", ("c1",))}))
        playback = SpeechPlayback(pcm(8000), ("c1",))
        self.router.begin_speech(playback)
        self.router.tick()
        self.assertFalse(playback.finished.is_set())
        self.router.read_camera("c1")
        self.assertFalse(playback.finished.is_set())
        self.router.read_camera("c1")
        self.assertTrue(playback.finished.is_set())


if __name__ == "__main__":
    unittest.main()
