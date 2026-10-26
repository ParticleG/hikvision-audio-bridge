"""Online Edge speech synthesis converted to the bridge's PCM audio."""

import asyncio
import contextlib
import re

import edge_tts

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
REQUEST_TIMEOUT = 30.0
MAX_TEXT_CHARACTERS = 2000
MAX_MP3_BYTES = 2 * 1024 * 1024
MAX_PCM_BYTES = 120 * 8000 * 2  # 120 seconds of mono PCM16 at 8 kHz.
FFMPEG = "ffmpeg"

_VOICE_PATTERN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+")
_FFMPEG_ARGUMENTS = (
    "-hide_banner",
    "-loglevel",
    "error",
    "-nostats",
    "-f",
    "mp3",
    "-i",
    "pipe:0",
    "-vn",
    "-map_metadata",
    "-1",
    "-ac",
    "1",
    "-ar",
    "8000",
    "-acodec",
    "pcm_s16le",
    "-f",
    "s16le",
    "pipe:1",
)


class TTSError(RuntimeError):
    """Speech synthesis failed without exposing the spoken text."""


def _validate_text(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("text must be str")
    normalized = text.strip()
    if not normalized:
        raise ValueError("text cannot be empty")
    if len(normalized) > MAX_TEXT_CHARACTERS:
        raise ValueError(f"text must be at most {MAX_TEXT_CHARACTERS} characters")
    for character in normalized:
        code = ord(character)
        if (
            (code < 0x20 and character not in "\t\n\r")
            or 0xD800 <= code <= 0xDFFF
            or code in (0xFFFE, 0xFFFF)
        ):
            raise ValueError("text contains unsupported XML control characters")
    return normalized


def _validate_voice(voice: str) -> str:
    if not isinstance(voice, str):
        raise TypeError("voice must be str")
    normalized = voice.strip()
    if (
        not normalized
        or len(normalized) > 100
        or _VOICE_PATTERN.fullmatch(normalized) is None
    ):
        raise ValueError("voice must be an Edge voice short name")
    return normalized


async def _spawn_ffmpeg() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        FFMPEG,
        *_FFMPEG_ARGUMENTS,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _pump_mp3(process: asyncio.subprocess.Process, stream) -> None:
    stdin = process.stdin
    assert stdin is not None
    written = 0
    try:
        async for chunk in stream:
            if not isinstance(chunk, dict) or chunk.get("type") != "audio":
                continue
            data = chunk.get("data")
            if not isinstance(data, bytes):
                raise TTSError("Edge speech synthesis failed")
            if not data:
                continue
            written += len(data)
            if written > MAX_MP3_BYTES:
                raise TTSError("Edge TTS returned too much audio")
            try:
                stdin.write(data)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise TTSError("FFmpeg could not decode speech audio") from None
        if written == 0:
            raise TTSError("Edge TTS returned no audio")
    finally:
        with contextlib.suppress(OSError):
            stdin.close()
        with contextlib.suppress(OSError):
            await stdin.wait_closed()


async def _read_pcm(process: asyncio.subprocess.Process) -> bytes:
    stdout = process.stdout
    assert stdout is not None
    output = bytearray()
    while chunk := await stdout.read(65536):
        if len(output) + len(chunk) > MAX_PCM_BYTES:
            raise TTSError("decoded speech exceeds the maximum duration")
        output.extend(chunk)
    if len(output) % 2:
        raise TTSError("FFmpeg produced invalid PCM audio")
    return bytes(output)


async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
    stderr = process.stderr
    assert stderr is not None
    while await stderr.read(65536):
        pass


async def _close_stdin(process: asyncio.subprocess.Process) -> None:
    stdin = process.stdin
    if stdin is None or stdin.is_closing():
        return
    stdin.close()
    with contextlib.suppress(OSError):
        await stdin.wait_closed()


async def _close_stream(stream) -> None:
    if stream is None:
        return
    with contextlib.suppress(Exception):
        await stream.aclose()


async def _run_pipeline(process: asyncio.subprocess.Process, stream) -> bytes:
    tasks = [
        asyncio.create_task(_pump_mp3(process, stream)),
        asyncio.create_task(_read_pcm(process)),
        asyncio.create_task(_drain_stderr(process)),
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    pcm = tasks[1].result()
    await process.wait()
    if process.returncode:
        raise TTSError("FFmpeg could not decode speech audio")
    if not pcm:
        raise TTSError("FFmpeg produced no audio")
    return pcm


async def _cleanup(process, stream) -> None:
    if process is not None:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await _close_stdin(process)
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
    await _close_stream(stream)


async def _synthesize_pcm(text: str, voice: str) -> bytes:
    stream = None
    process = None
    try:
        communicate = edge_tts.Communicate(text, voice)
        stream = communicate.stream()
        async with asyncio.timeout(REQUEST_TIMEOUT):
            spawn = asyncio.create_task(_spawn_ffmpeg())
            try:
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    # Keep the spawn alive long enough to take ownership of the
                    # process; cleanup then kills and reaps it.
                    with contextlib.suppress(Exception):
                        process = await spawn
                    raise
            except FileNotFoundError:
                raise TTSError("FFmpeg is not installed") from None
            return await _run_pipeline(process, stream)
    finally:
        cleanup = asyncio.create_task(_cleanup(process, stream))
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
            except Exception:
                break
        if cleanup.done() and not cleanup.cancelled():
            with contextlib.suppress(Exception):
                cleanup.result()
        if interrupted:
            raise asyncio.CancelledError


async def synthesize(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    """Return s16le, 8 kHz, mono PCM synthesized from plain text."""
    normalized_text = _validate_text(text)
    normalized_voice = _validate_voice(voice)
    try:
        pcm = await _synthesize_pcm(normalized_text, normalized_voice)
    except TimeoutError:
        raise TTSError("Edge speech synthesis timed out") from None
    except TTSError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        raise TTSError("Edge speech synthesis failed") from None
    return pcm
