"""The shared PCM format for desktop, server and camera routing."""

import struct

FRAME_SECONDS = 0.02
FRAME_BYTES = 320
BUFFER_FRAMES = 5
BUFFER_BYTES = FRAME_BYTES * BUFFER_FRAMES
SILENCE = bytes(FRAME_BYTES)
PROTOCOL_VERSION = 1
MAX_STREAMS = 64
FRAME_HEADER = struct.Struct("!I")


def encode_frame(stream_id: int, pcm: bytes) -> bytes:
    if not 1 <= stream_id <= 65535 or len(pcm) != FRAME_BYTES:
        raise ValueError("Audio frames require a valid stream ID and 320 PCM bytes")
    return FRAME_HEADER.pack(stream_id) + pcm


def decode_frame(data: bytes) -> tuple[int, bytes]:
    if len(data) != FRAME_HEADER.size + FRAME_BYTES:
        raise ValueError("Invalid audio frame size")
    stream_id = FRAME_HEADER.unpack_from(data)[0]
    if not 1 <= stream_id <= 65535:
        raise ValueError("Invalid audio stream ID")
    return stream_id, data[FRAME_HEADER.size :]
