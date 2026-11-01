"""Packaged CC0 status chimes prefixed to one spoken announcement."""

import wave
from functools import lru_cache
from pathlib import Path

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2
GAP_SECONDS = 0.2
GAP_BYTES = int(SAMPLE_RATE * GAP_SECONDS) * SAMPLE_WIDTH
CHIME_NAMES = ("success", "warning", "error")
CHIME_DIRECTORY = Path(__file__).with_name("chimes")


def validate_chime(chime: object) -> str:
    if not isinstance(chime, str) or chime not in CHIME_NAMES:
        raise ValueError("Chime must be success, warning or error")
    return chime


@lru_cache(maxsize=len(CHIME_NAMES))
def chime_pcm(chime: str) -> bytes:
    """Return the packaged 8 kHz mono PCM16 cue without its WAV header."""
    chime = validate_chime(chime)
    try:
        archive = wave.open(str(CHIME_DIRECTORY / f"{chime}.wav"), "rb")
    except (OSError, wave.Error) as error:
        raise RuntimeError(f"Bundled {chime} chime is unavailable") from error
    with archive:
        if (
            archive.getnchannels() != 1
            or archive.getsampwidth() != SAMPLE_WIDTH
            or archive.getframerate() != SAMPLE_RATE
        ):
            raise RuntimeError(f"Bundled {chime} chime has an unexpected format")
        frames = archive.readframes(archive.getnframes())
    if not frames:
        raise RuntimeError(f"Bundled {chime} chime is empty")
    return frames


def prepend_chime(chime: str | None, pcm: bytes) -> bytes:
    """Put one cue and a 200 ms pause ahead of unchanged speech PCM."""
    if chime is None:
        return pcm
    return chime_pcm(chime) + bytes(GAP_BYTES) + pcm
