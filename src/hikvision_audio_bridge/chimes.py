"""Packaged rendered status cues prefixed to queued speech announcements."""

import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2
CHIME_TONES = ("success", "attention", "alert", "warning", "error")
CHIME_DURATIONS = ("short", "medium", "long")
MAX_PLAYS = 5
CUE_GAP_BYTES = int(SAMPLE_RATE * 0.2) * SAMPLE_WIDTH
PLAY_GAP_BYTES = int(SAMPLE_RATE * 1.0) * SAMPLE_WIDTH
CHIME_DIRECTORY = Path(__file__).with_name("chimes")


@dataclass(frozen=True)
class Chime:
    tone: str
    duration: str


def validate_chime(value: object) -> Chime:
    if not isinstance(value, dict) or set(value) != {"tone", "duration"}:
        raise ValueError("Chime must contain exactly a tone and a duration")
    tone, duration = value["tone"], value["duration"]
    if not isinstance(tone, str) or tone not in CHIME_TONES:
        raise ValueError(
            "Chime tone must be success, attention, alert, warning or error"
        )
    if not isinstance(duration, str) or duration not in CHIME_DURATIONS:
        raise ValueError("Chime duration must be short, medium or long")
    return Chime(tone, duration)


def validate_plays(value: object) -> int:
    message = f"Plays must be an integer from 1 to {MAX_PLAYS}"
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(message)
    if not 1 <= value <= MAX_PLAYS:
        raise ValueError(message)
    return value


@lru_cache(maxsize=len(CHIME_TONES) * len(CHIME_DURATIONS))
def chime_pcm(chime: Chime) -> bytes:
    """Return the packaged 8 kHz mono PCM16 cue without its WAV header."""
    name = f"{chime.tone}-{chime.duration}"
    try:
        archive = wave.open(str(CHIME_DIRECTORY / f"{name}.wav"), "rb")
    except (OSError, EOFError, wave.Error) as error:
        raise RuntimeError(f"Bundled {name} chime is unavailable") from error
    with archive:
        if (
            archive.getnchannels() != 1
            or archive.getsampwidth() != SAMPLE_WIDTH
            or archive.getframerate() != SAMPLE_RATE
            or archive.getcomptype() != "NONE"
        ):
            raise RuntimeError(f"Bundled {name} chime has an unexpected format")
        frames = archive.readframes(archive.getnframes())
    if not frames:
        raise RuntimeError(f"Bundled {name} chime is empty")
    return frames


def compose_announcement(speech: bytes, chime: Chime | None, plays: int) -> bytes:
    """Join identical cue-plus-speech passes with one second of silence."""
    plays = validate_plays(plays)
    if chime is None:
        pass_pcm = speech
    else:
        pass_pcm = chime_pcm(chime) + bytes(CUE_GAP_BYTES) + speech
    return bytes(PLAY_GAP_BYTES).join([pass_pcm] * plays)
