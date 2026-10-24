"""Loopback ISAPI peer used to exercise real Digest and duplex TCP streams."""

import asyncio
import contextlib
import hashlib
import math
import re
import struct
from urllib.parse import urlsplit

import audioop

OK = (
    b"<ResponseStatus><statusCode>1</statusCode>"
    b"<statusString>OK</statusString></ResponseStatus>"
)
BUSY = (
    b"<ResponseStatus><statusCode>4</statusCode>"
    b"<statusString>Device Busy</statusString></ResponseStatus>"
)


class CameraPeer:
    password = "test-camera-password"
    username = "test-admin"

    def __init__(self, frequency=440):
        self.frequency = frequency
        self.server = None
        self.writers = set()
        self.tasks = set()
        self.opened = 0
        self.closed = 0
        self.active = False
        self.received = bytearray()
        self.speaker_started = asyncio.Event()
        self.microphone_started = asyncio.Event()
        self.received_audio = asyncio.Event()
        self.reject_upload = asyncio.Event()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.allow_close.set()
        self.speaker_connections = 0
        self.microphone_connections = 0
        self.open_failure = False
        self.close_failure = False
        self.codec = "G.711alaw"
        self.requests = []

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def authenticated(self, method, uri, header):
        values = {
            name: quoted or plain
            for name, quoted, plain in re.findall(
                r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header
            )
        }

        def digest(value):
            return hashlib.md5(value.encode()).hexdigest()

        ha1 = digest(f"{self.username}:loopback:{self.password}")
        ha2 = digest(f"{method}:{uri}")
        expected = digest(
            f"{ha1}:loopback-nonce:{values.get('nc')}:{values.get('cnonce')}:auth:{ha2}"
        )
        return values.get("uri") == uri and values.get("response") == expected

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            first, *lines = raw.decode().strip().split("\r\n")
            method, uri, _ = first.split(" ")
            headers = {
                name.lower(): value
                for name, value in (line.split(": ", 1) for line in lines)
            }
            if not self.authenticated(method, uri, headers.get("authorization", "")):
                writer.write(
                    b"HTTP/1.1 401 Unauthorized\r\n"
                    b'WWW-Authenticate: Digest realm="loopback", '
                    b'nonce="loopback-nonce", qop="auth", algorithm=MD5\r\n'
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            self.requests.append((method, uri))
            path = urlsplit(uri).path
            if path.endswith("/channels"):
                body = (
                    "<TwoWayAudioChannelList "
                    "xmlns='http://www.hikvision.com/ver20/XMLSchema'>"
                    "<TwoWayAudioChannel><id>1</id><enabled>false</enabled>"
                    f"<audioCompressionType>{self.codec}</audioCompressionType>"
                    "</TwoWayAudioChannel></TwoWayAudioChannelList>"
                ).encode()
            elif path.endswith("/open"):
                self.opened += 1
                if self.open_failure or self.active:
                    body = BUSY
                else:
                    self.active = True
                    body = (
                        f"<TwoWayAudioSession><sessionId>{self.opened}</sessionId>"
                        "</TwoWayAudioSession>"
                    ).encode()
            elif path.endswith("/close"):
                self.close_started.set()
                await self.allow_close.wait()
                self.closed += 1
                self.active = False
                body = BUSY if self.close_failure else OK
            elif path.endswith("/audioData") and method == "PUT":
                if headers.get("content-length") != "0":
                    raise AssertionError(
                        "ISAPI upload must use the raw zero-length handshake"
                    )
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n"
                    b"Connection: keep-alive\r\n\r\n"
                )
                await writer.drain()
                self.speaker_connections += 1
                self.speaker_started.set()
                while chunk := await reader.read(160):
                    self.received.extend(chunk)
                    self.received_audio.set()
                    if self.reject_upload.is_set():
                        self.respond(writer, BUSY)
                        await writer.drain()
                        break
                return
            elif path.endswith("/audioData") and method == "GET":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: audio/basic\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n"
                )
                self.microphone_connections += 1
                self.microphone_started.set()
                position = 0
                while True:
                    pcm = struct.pack(
                        "<160h",
                        *(
                            round(
                                6000
                                * math.sin(
                                    2 * math.pi * self.frequency * (position + i) / 8000
                                )
                            )
                            for i in range(160)
                        ),
                    )
                    position += 160
                    encoded = audioop.lin2alaw(pcm, 2)
                    writer.write(f"{len(encoded):x}\r\n".encode() + encoded + b"\r\n")
                    await writer.drain()
                    # Socket integration follows the platform's real audio clock.
                    await asyncio.sleep(0.02)
            else:
                raise AssertionError(f"Unexpected camera request: {method} {path}")
            self.respond(writer, body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.tasks.discard(task)
            self.writers.discard(writer)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    @staticmethod
    def respond(writer, body):
        writer.write(
            (
                "HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode()
            + body
        )
