"""Continuous ISAPI intercom streams, with one explicitly owned camera session."""

import asyncio
import contextlib
import ssl
import xml.etree.ElementTree as ET
from collections.abc import Callable

import audioop
import h11
import httpx

from .config import Camera


class CameraError(RuntimeError):
    pass


def parse_xml(data: bytes) -> ET.Element:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise CameraError("Camera returned invalid XML") from error
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    if root.tag == "ResponseStatus" and root.findtext("statusCode") != "1":
        raise CameraError(
            f"Camera rejected request: {root.findtext('statusString', 'unknown error')}"
        )
    return root


def response_parser() -> h11.Connection:
    parser = h11.Connection(h11.CLIENT, max_incomplete_event_size=65536)
    parser.send(h11.Request(method="PUT", target="/", headers=[("Host", "camera")]))
    parser.send(h11.EndOfMessage())
    return parser


async def read_event(reader: asyncio.StreamReader, parser: h11.Connection):
    while True:
        event = parser.next_event()
        if event is not h11.NEED_DATA:
            return event
        parser.receive_data(await reader.read(16384))


class CameraSession:
    def __init__(self, camera: Camera, password: str):
        self.camera = camera
        self.path = f"/ISAPI/System/TwoWayAudio/channels/{camera.channel}"
        self.auth = httpx.DigestAuth(camera.username, password)
        self.tls = ssl.create_default_context()
        self.client = httpx.AsyncClient(
            auth=self.auth,
            timeout=httpx.Timeout(5, read=10),
            verify=self.tls,
            trust_env=False,
            follow_redirects=False,
        )
        self.session_id: str | None = None
        self.opened = False
        self.codec = ""
        self.sent_bytes = 0
        self.received_bytes = 0

    async def request(self, method: str, path: str) -> ET.Element:
        async with asyncio.timeout(5):
            async with self.client.stream(method, self.camera.url + path) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise CameraError("Camera XML response exceeds 64 KiB")
                return parse_xml(bytes(data))

    async def open(self) -> None:
        channels = await self.request("GET", "/ISAPI/System/TwoWayAudio/channels")
        for channel in channels.iter("TwoWayAudioChannel"):
            if channel.findtext("id") == str(self.camera.channel):
                self.codec = channel.findtext("audioCompressionType", "")
                break
        if self.codec not in ("G.711alaw", "G.711ulaw"):
            raise CameraError("The selected channel must use G.711alaw or G.711ulaw")
        opened = await self.request("PUT", self.path + "/open")
        if opened.tag != "TwoWayAudioSession":
            raise CameraError("Camera did not open an audio session")
        self.opened = True
        session = opened.findtext("sessionId", "")
        if not session.isascii() or not session.isdecimal():
            raise CameraError("Camera returned an invalid audio session ID")
        self.session_id = session

    @property
    def audio_url(self) -> str:
        if self.session_id is None:
            raise CameraError("No camera audio session is open")
        return f"{self.camera.url}{self.path}/audioData?sessionId={self.session_id}"

    async def microphone(self, consume_pcm: Callable[[bytes], None]) -> None:
        decode = audioop.alaw2lin if self.codec == "G.711alaw" else audioop.ulaw2lin
        async with self.client.stream("GET", self.audio_url) as response:
            response.raise_for_status()
            content_type = (
                response.headers.get("content-type", "").split(";")[0].lower()
            )
            if content_type not in (
                "audio/basic",
                "application/octet-stream",
                "audio/g711",
            ):
                raise CameraError("Camera microphone did not return a raw audio stream")
            async for chunk in response.aiter_bytes(chunk_size=160):
                self.received_bytes += len(chunk)
                consume_pcm(decode(chunk, 2))
        raise CameraError("Camera microphone stream ended")

    async def _upload_connection(self):
        request = httpx.Request(
            "PUT",
            self.audio_url,
            headers={
                "Content-Length": "0",
                "Content-Type": "application/octet-stream",
                "Connection": "keep-alive",
            },
        )
        flow = self.auth.auth_flow(request)
        try:
            request = next(flow)
            for _ in range(2):
                writer = None
                try:
                    async with asyncio.timeout(5):
                        secure = request.url.scheme == "https"
                        reader, writer = await asyncio.open_connection(
                            request.url.host,
                            request.url.port or (443 if secure else 80),
                            ssl=self.tls if secure else None,
                            limit=65536,
                        )
                        writer.write(b"PUT " + request.url.raw_path + b" HTTP/1.1\r\n")
                        for name, value in request.headers.raw:
                            writer.write(name + b": " + value + b"\r\n")
                        writer.write(b"\r\n")
                        await writer.drain()
                        parser = response_parser()
                        while True:
                            event = await read_event(reader, parser)
                            if isinstance(event, h11.InformationalResponse):
                                continue
                            if not isinstance(event, h11.Response):
                                raise CameraError(
                                    "Camera returned no audio upload handshake"
                                )
                            break
                        response = httpx.Response(
                            event.status_code,
                            headers=list(event.headers),
                            request=request,
                        )
                        if response.status_code == 401:
                            try:
                                request = flow.send(response)
                            except StopIteration:
                                raise CameraError(
                                    "Camera audio authentication failed"
                                ) from None
                        else:
                            response.raise_for_status()
                            return reader, writer, parser, response
                except BaseException:
                    if writer is not None:
                        writer.close()
                    raise
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
            raise CameraError("Camera audio authentication failed")
        finally:
            flow.close()

    async def _upload_result(self, reader, parser, response) -> None:
        # Empty initial 200 is a handshake; a later response is the terminal result.
        if response.headers.get("content-length") == "0":
            end = parser.next_event()
            if not isinstance(end, h11.EndOfMessage):
                raise CameraError("Invalid empty audio handshake")
            remainder, _ = parser.trailing_data
            parser = response_parser()
            if remainder:
                parser.receive_data(remainder)
        body = bytearray()
        while True:
            event = await read_event(reader, parser)
            if isinstance(event, h11.Response):
                if event.status_code != 200:
                    raise CameraError(
                        f"Camera audio upload failed: HTTP {event.status_code}"
                    )
            elif isinstance(event, h11.Data):
                body.extend(event.data)
                if len(body) > 65536:
                    raise CameraError("Camera audio response exceeds 64 KiB")
            elif isinstance(event, h11.EndOfMessage):
                break
            elif isinstance(event, h11.ConnectionClosed):
                raise CameraError("Camera closed without an audio result")
        if body.startswith(b"HTTP/"):
            nested = response_parser()
            nested.receive_data(bytes(body))
            nested.receive_data(b"")
            body.clear()
            while True:
                event = nested.next_event()
                if isinstance(event, h11.Response) and event.status_code != 200:
                    raise CameraError(
                        f"Camera audio upload failed: HTTP {event.status_code}"
                    )
                if isinstance(event, h11.Data):
                    body.extend(event.data)
                if isinstance(event, h11.EndOfMessage):
                    break
                if event is h11.NEED_DATA or isinstance(event, h11.ConnectionClosed):
                    raise CameraError("Incomplete nested camera audio response")
        if body:
            parse_xml(bytes(body))
        raise CameraError("Camera ended the continuous speaker stream")

    async def speaker(self, read_pcm: Callable[[], bytes]) -> None:
        reader, writer, parser, response = await self._upload_connection()
        result = asyncio.create_task(self._upload_result(reader, parser, response))
        encode = audioop.lin2alaw if self.codec == "G.711alaw" else audioop.lin2ulaw
        clock = asyncio.get_running_loop()
        deadline = clock.time()
        try:
            while True:
                if result.done():
                    await result
                frame = encode(read_pcm(), 2)
                writer.write(frame)
                async with asyncio.timeout(2):
                    await writer.drain()
                self.sent_bytes += len(frame)
                deadline += 0.02
                # Never burst queued audio after a network or scheduler pause.
                if deadline < clock.time():
                    deadline = clock.time()
                await asyncio.sleep(max(0, deadline - clock.time()))
        finally:
            result.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await result
            writer.close()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(2):
                    await writer.wait_closed()

    async def close(self) -> None:
        try:
            if self.opened:
                result = await self.request("PUT", self.path + "/close")
                if result.tag != "ResponseStatus":
                    raise CameraError("Camera returned no channel-close result")
                self.opened = False
        finally:
            await self.client.aclose()
