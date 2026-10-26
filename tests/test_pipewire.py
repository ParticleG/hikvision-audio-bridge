import asyncio
import contextlib
import os
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hikvision_audio_bridge.config import AudioDevice, Paths, Settings
from hikvision_audio_bridge.pipewire import (
    AudioDevices,
    device_name,
)
from hikvision_audio_bridge.service import Bridge


def pcm(value):
    return struct.pack("<160h", *([value] * 160))


class PipeWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name) / "run"
        self.runtime.mkdir()
        self.pactl = AsyncMock(return_value="42")
        self.patcher = patch("hikvision_audio_bridge.pipewire.pactl", new=self.pactl)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.devices = AudioDevices(self.runtime)

    async def asyncTearDown(self):
        with contextlib.suppress(Exception):
            await self.devices.close()
        self.temporary.cleanup()

    def test_universal_names_preserve_the_published_defaults(self):
        self.assertEqual(device_name("speaker"), "hikvision_audio_speaker")
        self.assertEqual(device_name("microphone"), "hikvision_audio_microphone")
        self.assertEqual(device_name("front-door"), "hikvision_audio_front-door")

    async def test_recover_only_unloads_exact_owned_modules(self):
        runtime = self.runtime
        modules = "\n".join(
            [
                f"1\tmodule-pipe-sink\tfile={runtime}/front.pcm "
                "sink_name=hikvision_audio_front",
                f"2\tmodule-pipe-source\tfile={runtime}/listen.pcm "
                "source_name=hikvision_audio_listen",
                f"3\tmodule-pipe-sink\tfile={runtime}/front.pcm sink_name=unrelated",
                f"4\tmodule-pipe-sink\tfile={runtime}/other.pcm "
                "sink_name=hikvision_audio_front",
                f"5\tmodule-pipe-sink\tfile={runtime}/Bad.pcm "
                "sink_name=hikvision_audio_Bad",
                f"6\tmodule-pipe-sink\tfile={runtime}/front.pcm "
                f"file={runtime}/other.pcm sink_name=hikvision_audio_front",
                f"7\tmodule-pipe-sink\tfile={runtime}/double.pcm "
                "file={runtime}/double.pcm sink_name=hikvision_audio_double",
                f"8\tmodule-pipe-sink\tfile={runtime}/quote.pcm "
                'sink_name="unterminated',
                "9\tmodule-native-protocol-unix\tunrelated=value",
                "ten\tmodule-pipe-sink\tfile=/tmp/foreign.pcm "
                "sink_name=hikvision_audio_foreign",
                "11\tmodule-pipe-sink",
            ]
        )
        self.pactl.return_value = modules
        await self.devices.recover()
        unloads = [
            call.args
            for call in self.pactl.await_args_list
            if call.args[:1] == ("unload-module",)
        ]
        self.assertEqual(unloads, [("unload-module", "1"), ("unload-module", "2")])

    async def test_enable_uses_private_fifo_and_universal_technical_name(self):
        device = AudioDevice('Front "Speaker"', "speaker", ("front",))
        await self.devices.enable("front", device)
        path = self.runtime / "front.pcm"
        info = path.lstat()
        self.assertTrue(stat.S_ISFIFO(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)

        await self.devices.disable("front")
        self.assertFalse(path.exists())
        self.assertIn(
            ("unload-module", "42"),
            [call.args for call in self.pactl.await_args_list],
        )

    async def test_route_only_edits_do_not_reload_the_pipewire_module(self):
        device = AudioDevice("Front", "speaker", ("one",))
        await self.devices.enable("front", device)
        changed = AudioDevice("Front", "speaker", ("two",))
        await self.devices.enable("front", changed)
        loads = [
            call.args
            for call in self.pactl.await_args_list
            if call.args[:1] == ("load-module",)
        ]
        self.assertEqual(len(loads), 1)
        self.assertEqual(self.devices.devices["front"].settings, changed)
        self.assertFalse(
            any(
                call.args[:1] == ("unload-module",)
                for call in self.pactl.await_args_list
            )
        )

    async def test_reading_a_speaker_is_bounded_and_returns_20ms_frames(self):
        await self.devices.enable("front", AudioDevice("Front", "speaker", ("front",)))
        descriptor = self.devices.devices["front"].descriptor
        first = pcm(1000)
        second = pcm(2000)
        os.write(descriptor, first + second)
        self.assertEqual(self.devices.read_speaker("front"), first)
        self.assertEqual(self.devices.read_speaker("front"), second)

        values = [pcm(value) for value in range(1, 8)]
        os.write(descriptor, b"".join(values))
        self.assertEqual(self.devices.read_speaker("front"), values[2])
        self.assertEqual(self.devices.read_speaker("front"), values[3])

        self.devices.flush({"front"})
        self.assertEqual(self.devices.read_speaker("front"), bytes(320))

    async def test_microphone_writes_reach_the_fifo(self):
        await self.devices.enable(
            "front", AudioDevice("Front", "microphone", ("front",))
        )
        path = self.runtime / "front.pcm"
        read_descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            frame = pcm(1500)
            self.devices.write_microphone("front", frame)
            self.assertEqual(os.read(read_descriptor, 4096), frame)
        finally:
            os.close(read_descriptor)

    async def test_flush_drains_nonzero_microphone_fifo(self):
        await self.devices.enable(
            "front", AudioDevice("Front", "microphone", ("front",))
        )
        path = self.runtime / "front.pcm"
        read_descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            self.devices.write_microphone("front", pcm(2345))
            self.devices.flush({"front"})
            with self.assertRaises(BlockingIOError):
                os.read(read_descriptor, 4096)
        finally:
            os.close(read_descriptor)

    async def test_cancelled_enable_is_settled_before_device_removal(self):
        paths = Paths(self.runtime, Path(self.temporary.name) / "config")
        paths.prepare()
        bridge = Bridge(paths)
        bridge.devices = self.devices
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_load(*arguments):
            if arguments[0] == "load-module":
                started.set()
                await release.wait()
                return "42"
            return ""

        self.pactl.side_effect = delayed_load
        enabling = asyncio.create_task(
            bridge.apply(
                Settings(
                    devices={
                        "front": AudioDevice("Front", "speaker"),
                    }
                )
            )
        )
        removal = None
        try:
            async with asyncio.timeout(3):
                await started.wait()
                enabling.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await enabling
                removal = asyncio.create_task(bridge.apply(Settings()))
                # Let removal reach its first suspension before the load completes.
                asyncio.get_running_loop().call_soon(release.set)
                await removal
            self.assertFalse(self.devices.devices)
            self.assertFalse((self.runtime / "front.pcm").exists())
            self.pactl.assert_any_await("unload-module", "42")
        finally:
            release.set()
            if removal is not None:
                await asyncio.gather(removal, return_exceptions=True)
            await bridge.close()

    async def test_enable_failure_removes_the_created_fifo(self):
        self.pactl.side_effect = RuntimeError("pactl failed")
        with self.assertRaises(RuntimeError):
            await self.devices.enable(
                "front", AudioDevice("Front", "speaker", ("front",))
            )
        self.assertFalse((self.runtime / "front.pcm").exists())

    async def test_enable_never_replaces_a_foreign_path(self):
        path = self.runtime / "front.pcm"
        path.write_text("not a FIFO")
        with self.assertRaises(RuntimeError):
            await self.devices.enable(
                "front", AudioDevice("Front", "speaker", ("front",))
            )
        self.assertEqual(path.read_text(), "not a FIFO")
        self.assertFalse(
            any(
                call.args[:1] == ("load-module",) for call in self.pactl.await_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
