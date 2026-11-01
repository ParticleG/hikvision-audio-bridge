"""Authenticated management UIs and separately scoped network audio access."""

import ipaddress
import json
import os
import secrets
import ssl
import tempfile
from collections.abc import Awaitable, Callable
from importlib.resources import files
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from aiohttp import web

from .config import Paths, WebSettings

MAX_BODY_BYTES = 16 * 1024
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
_STATIC = {
    "/": ("index.html", "text/html"),
    "/index.html": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/common.js": ("common.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
}


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _wildcard(host: str) -> bool:
    return host != "localhost" and ipaddress.ip_address(host).is_unspecified


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(ord(character) <= 32 for character in value)
    ):
        raise ValueError("Invalid origin")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.casefold(), port


class AudioAPI(Protocol):
    access_token: str

    def catalog(self) -> dict: ...

    async def websocket(self, request: web.Request) -> web.StreamResponse: ...


class WebServer:
    """Serve packaged assets and forward authenticated commands to one executor."""

    def __init__(
        self,
        paths: Paths,
        settings: WebSettings,
        execute: Callable[[object], Awaitable[dict]],
        *,
        component: str = "desktop",
        token: str | None = None,
        audio: AudioAPI | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.execute = execute
        if component not in ("server", "desktop"):
            raise ValueError("Unknown WebUI component")
        self.component = component
        self.audio = audio
        self._configured_token = token
        self.url: str | None = None
        self.token: str | None = None
        self._runner: web.AppRunner | None = None
        self._access_identity: tuple[int, int] | None = None
        self._assets: dict[str, tuple[bytes, str]] = {}

    async def start(self) -> None:
        if self._runner is not None:
            return
        if (
            not self.settings.loopback
            and not self.settings.tls_cert
            and not self.settings.allow_insecure_lan
        ):
            raise ValueError("LAN WebUI requires TLS or explicit allow_insecure_lan")
        context = None
        if self.settings.tls_cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.settings.tls_cert, self.settings.tls_key)
        self.paths.prepare()
        self.token = self._configured_token or secrets.token_urlsafe(32)
        try:
            package = files("hikvision_audio_bridge").joinpath("static")
            self._assets = {}
            for path, (name, content_type) in _STATIC.items():
                root = (
                    package
                    if name in ("style.css", "common.js")
                    else package.joinpath(self.component)
                )
                self._assets[path] = root.joinpath(name).read_bytes(), content_type
            application = web.Application(
                client_max_size=MAX_BODY_BYTES, middlewares=[self._security]
            )
            application.on_response_prepare.append(self._prepare_response)
            for path in _STATIC:
                application.router.add_get(path, self._static)
            application.router.add_get("/api/status", self._status)
            application.router.add_post("/api/command", self._command)
            if self.audio is not None:
                application.router.add_get("/api/audio/catalog", self._audio_catalog)
                application.router.add_get("/api/audio/stream", self.audio.websocket)
            self._runner = web.AppRunner(
                application,
                access_log=None,
                auto_decompress=False,
                shutdown_timeout=5,
            )
            await self._runner.setup()
            # Use one loopback family so port 0 cannot allocate two different ports.
            host = self.settings.host
            bind_host = "127.0.0.1" if host == "localhost" else host
            site = web.TCPSite(
                self._runner,
                host=bind_host,
                port=self.settings.port,
                ssl_context=context,
            )
            await site.start()
            port = self._runner.addresses[0][1]
            if _wildcard(host):
                launch_host = (
                    "127.0.0.1" if ipaddress.ip_address(host).version == 4 else "::1"
                )
            else:
                launch_host = host
            if ":" in launch_host:
                launch_host = f"[{launch_host}]"
            scheme = "https" if context is not None else "http"
            self.url = f"{scheme}://{launch_host}:{port}"
            self._write_access()
        except BaseException:
            await self.close()
            raise

    async def _prepare_response(
        self, request: web.Request, response: web.StreamResponse
    ) -> None:
        response.headers.update(_SECURITY_HEADERS)

    def _write_access(self) -> None:
        descriptor, temporary = tempfile.mkstemp(
            dir=self.paths.runtime, prefix=".web-access-"
        )
        try:
            with os.fdopen(descriptor, "w") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump({"url": self.url, "token": self.token}, output)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.paths.web_access)
            info = self.paths.web_access.lstat()
            self._access_identity = info.st_dev, info.st_ino
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def close(self) -> None:
        runner, self._runner = self._runner, None
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            try:
                info = self.paths.web_access.lstat()
            except FileNotFoundError:
                pass
            else:
                if self._access_identity == (info.st_dev, info.st_ino):
                    self.paths.web_access.unlink()
            self._access_identity = None
            self.token = None
            self.url = None
            self._assets = {}

    def _request_origin(self, request: web.Request) -> tuple[str, str, int]:
        hosts = request.headers.getall("Host", [])
        if len(hosts) != 1:
            raise ValueError("Invalid Host header")
        origin = _origin(f"{request.scheme}://{hosts[0]}")
        transport = request.transport
        address = transport.get_extra_info("sockname") if transport else None
        if address is None or origin[2] != address[1]:
            raise ValueError("Invalid Host header")
        if self.settings.loopback:
            allowed = {self.settings.host.casefold(), "localhost", "127.0.0.1", "::1"}
            if origin[1] not in allowed:
                raise ValueError("Invalid Host header")
        elif not _wildcard(self.settings.host):
            try:
                matches = ipaddress.ip_address(origin[1]) == ipaddress.ip_address(
                    self.settings.host
                )
            except ValueError:
                matches = False
            if not matches:
                raise ValueError("Invalid Host header")
        return origin

    @web.middleware
    async def _security(self, request: web.Request, handler) -> web.StreamResponse:
        try:
            try:
                expected = self._request_origin(request)
            except ValueError:
                response = _error("Invalid request host", 400)
            else:
                origins = request.headers.getall("Origin", [])
                try:
                    valid_origin = not origins or (
                        len(origins) == 1 and _origin(origins[0]) == expected
                    )
                except ValueError:
                    valid_origin = False
                if not valid_origin:
                    response = _error("Cross-origin requests are not allowed", 403)
                elif request.path.startswith("/api/") and not self._authorized(request):
                    response = _error("A valid WebUI access token is required", 401)
                    response.headers["WWW-Authenticate"] = "Bearer"
                else:
                    response = await handler(request)
        except web.HTTPException as error:
            message = {
                404: "Not found",
                405: "Method not allowed",
                413: "Command body is too large",
            }.get(error.status, "Request rejected")
            response = _error(message, error.status)
        except Exception:
            # Never let aiohttp log request data or reflect backend exceptions.
            response = _error("The WebUI request failed", 500)
        return response

    def _authorized(self, request: web.Request) -> bool:
        token = self.token
        if request.path.startswith("/api/audio/"):
            token = self.audio.access_token if self.audio is not None else None
        if token is None:
            return False
        values = request.headers.getall("Authorization", [])
        if len(values) != 1:
            return False
        scheme, separator, credential = values[0].partition(" ")
        if not separator or scheme.casefold() != "bearer" or not credential:
            return False
        return secrets.compare_digest(
            credential.encode("utf-8", "replace"), token.encode("ascii")
        )

    async def _static(self, request: web.Request) -> web.Response:
        body, content_type = self._assets[request.path]
        return web.Response(body=body, content_type=content_type, charset="utf-8")

    def _message(self, error: Exception, command: dict | None = None) -> str:
        message = str(error) or "The command could not be completed"
        sensitive = [self.token]
        if command is not None:
            sensitive.extend(command.get(key) for key in ("password", "text", "token"))
        for value in sensitive:
            if isinstance(value, str) and value:
                message = message.replace(value, "[redacted]")
        return message[:1000]

    async def _audio_catalog(self, request: web.Request) -> web.Response:
        assert self.audio is not None
        return web.json_response({"ok": True, "status": self.audio.catalog()})

    async def _status(self, request: web.Request) -> web.Response:
        try:
            status = await self.execute({"op": "status"})
        except (ValueError, RuntimeError) as error:
            return _error(self._message(error), 400)
        return web.json_response({"ok": True, "status": status})

    async def _command(self, request: web.Request) -> web.Response:
        charset = request.charset
        content_types = request.headers.getall("Content-Type", [])
        encodings = request.headers.getall("Content-Encoding", [])
        if (
            len(content_types) != 1
            or request.content_type.lower() != "application/json"
            or (charset is not None and charset.lower() not in ("utf-8", "utf8"))
        ):
            return _error("Commands require application/json encoded as UTF-8", 415)
        if any(value.strip().lower() != "identity" for value in encodings):
            return _error("Compressed command bodies are not accepted", 415)
        if (
            request.content_length is not None
            and request.content_length > MAX_BODY_BYTES
        ):
            return _error("Command body is too large", 413)
        try:
            data = await request.read()
            command = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return _error("Command body must contain valid JSON", 400)
        if not isinstance(command, dict):
            return _error("Command body must be a JSON object", 400)
        try:
            status = await self.execute(command)
        except (ValueError, RuntimeError) as error:
            return _error(self._message(error, command), 400)
        return web.json_response({"ok": True, "status": status})
