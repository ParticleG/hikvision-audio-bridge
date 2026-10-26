import json
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from hikvision_audio_bridge.config import Paths, WebSettings
from hikvision_audio_bridge.web import MAX_BODY_BYTES, WebServer


def bridge_status():
    return {
        "settings": {
            "cameras": {
                "front": {
                    "url": "http://192.0.2.10",
                    "username": "admin",
                    "channel": 1,
                },
                "back": {
                    "url": "http://192.0.2.11",
                    "username": "viewer",
                    "channel": 2,
                },
            },
            "devices": {
                "living": {
                    "name": "Living room speaker",
                    "direction": "speaker",
                    "cameras": ["front"],
                    "enabled": True,
                },
                "kitchen": {
                    "name": "Kitchen microphone",
                    "direction": "microphone",
                    "cameras": ["front", "back"],
                    "enabled": False,
                },
            },
            "web": {
                "host": "127.0.0.1",
                "port": 8765,
                "tls_cert": None,
                "tls_key": None,
            },
        },
        "cameras": {
            "front": {
                "connection": "connected",
                "error": None,
                "cleanup_error": None,
                "codec": "G.711",
                "sent_bytes": 640,
                "received_bytes": 1280,
            },
            "back": {
                "connection": "error",
                "error": "Connection refused",
                "cleanup_error": None,
                "codec": None,
                "sent_bytes": 0,
                "received_bytes": 0,
            },
        },
        "devices": {
            "living": {"name": "hikvision_audio_living"},
            "kitchen": {"name": "hikvision_audio_kitchen"},
        },
        "tts": {"state": "idle", "error": None, "devices": []},
    }


class StubExecutor:
    def __init__(self):
        self.calls: list[object] = []
        self.status = bridge_status()
        self.error: Exception | None = None

    async def __call__(self, command):
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return self.status


class WebServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.paths = Paths(root / "run", root / "config")
        self.executor = StubExecutor()
        self.server: WebServer | None = None
        self.url = ""
        self.token = ""
        self.origin = ""
        self.client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10), trust_env=False
        )

    async def asyncTearDown(self):
        await self.client.close()
        if self.server is not None:
            await self.server.close()
        self.directory.cleanup()

    async def start(self, settings=None):
        server = WebServer(
            self.paths,
            settings or WebSettings(host="127.0.0.1", port=0),
            self.executor,
        )
        self.server = server
        await server.start()
        access = json.loads(self.paths.web_access.read_text())
        self.url = access["url"]
        self.token = access["token"]
        parsed = urlsplit(self.url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        return server

    def auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    async def request(
        self,
        method,
        path,
        *,
        headers=None,
        data=None,
        command=None,
        origin=None,
    ):
        request_headers = headers.copy() if headers is not None else {}
        if origin is not None:
            request_headers["Origin"] = origin
        if command is not None:
            data = json.dumps(command).encode()
            if not request_headers.get("Content-Type"):
                request_headers["Content-Type"] = "application/json"
        async with self.client.request(
            method, f"{self.url}{path}", headers=request_headers, data=data
        ) as response:
            raw = await response.read()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = None
            return response.status, response.headers, payload, raw

    async def test_start_and_close_manage_private_access_file_and_listener(self):
        server = await self.start()
        url = server.url
        token = server.token
        assert url is not None
        assert token is not None
        access_path = self.paths.web_access
        self.assertTrue(access_path.exists())
        self.assertEqual(stat.S_IMODE(access_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.runtime.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.paths.config.stat().st_mode), 0o700)
        access = json.loads(access_path.read_text())
        self.assertEqual(set(access), {"url", "token"})
        self.assertEqual(access["url"], url)
        self.assertEqual(access["token"], token)
        self.assertNotIn(self.token, url)
        parsed = urlsplit(url)
        self.assertEqual(parsed.scheme, "http")
        self.assertGreater(parsed.port or 0, 0)
        self.assertEqual(list(self.paths.runtime.glob(".web-access-*")), [])

        status, headers, payload, raw = await self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), raw)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Set-Cookie", headers)
        self.assertIsNone(payload)

        await server.close()
        self.assertFalse(access_path.exists())
        self.assertIsNone(server.token)
        self.assertIsNone(server.url)
        with self.assertRaises(aiohttp.ClientError):
            await self.request("GET", "/")
        await server.close()

    async def test_static_routes_are_fixed_and_hardened(self):
        await self.start()
        assets = {
            "/": "text/html",
            "/index.html": "text/html",
            "/app.js": "javascript",
            "/style.css": "text/css",
        }
        for path, content_type in assets.items():
            with self.subTest(path=path):
                status, headers, _, raw = await self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(content_type, headers["Content-Type"].lower())
                self.assertNotIn(self.token.encode(), raw)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
                self.assertIn("camera=()", headers["Permissions-Policy"])
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                self.assertNotIn("Set-Cookie", headers)

        for path in (
            "/web.py",
            "/config.py",
            "/web-access.json",
            "/control.sock",
            "/static/index.html",
        ):
            with self.subTest(path=path):
                status, headers, payload, raw = await self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertIsNotNone(payload)
                self.assertFalse(payload["ok"])
                self.assertNotIn(b"root:", raw)
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    async def test_api_requires_a_single_valid_bearer_token(self):
        await self.start()
        cases = [
            ("missing", {}),
            ("wrong", {"Authorization": "Bearer " + "x" * len(self.token)}),
            ("wrong scheme", {"Authorization": "Basic abc"}),
            ("query only", {}),
        ]
        for name, headers in cases:
            path = "/api/status"
            if name == "query only":
                path += f"?token={self.token}"
            with self.subTest(case=name):
                status, response_headers, payload, raw = await self.request(
                    "GET", path, headers=headers
                )
                self.assertEqual(status, 401)
                self.assertFalse(payload["ok"])
                self.assertEqual(response_headers["WWW-Authenticate"], "Bearer")
                self.assertNotIn(self.token, raw.decode())
                self.assertNotIn("Access-Control-Allow-Origin", response_headers)
        status, _, payload, _ = await self.request(
            "POST",
            "/api/command",
            command={"op": "status"},
        )
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.executor.calls, [])

        status, _, payload, _ = await self.request(
            "GET",
            "/api/status",
            headers={"Authorization": f"bearer {self.token}"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.executor.calls, [{"op": "status"}])

    async def test_cross_origin_and_unexpected_hosts_are_rejected(self):
        await self.start()
        port = urlsplit(self.url).port or 0
        evil = "https://evil.example"
        status, headers, payload, _ = await self.request(
            "GET", "/api/status", headers=self.auth_headers(), origin=evil
        )
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _, _, _ = await self.request(
            "POST",
            "/api/command",
            headers=self.auth_headers(),
            command={"op": "status"},
            origin=evil,
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.executor.calls, [])

        status, _, payload, _ = await self.request(
            "GET", "/api/status", headers=self.auth_headers(), origin=self.origin
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, _, payload, _ = await self.request(
            "POST",
            "/api/command",
            headers=self.auth_headers(),
            command={"op": "status"},
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        calls = len(self.executor.calls)
        self.assertEqual(calls, 2)

        for origin in (
            "null",
            f"{self.origin}/",
            f"http://127.0.0.1:{port + 1}",
            "https://127.0.0.1:" + str(port),
        ):
            with self.subTest(origin=origin):
                status, _, _, _ = await self.request(
                    "GET",
                    "/api/status",
                    headers=self.auth_headers(),
                    origin=origin,
                )
                self.assertEqual(status, 403)
        self.assertEqual(len(self.executor.calls), calls)

        for host in (f"evil.example:{port}", f"127.0.0.1:{port + 1}"):
            with self.subTest(host=host):
                headers = self.auth_headers()
                headers["Host"] = host
                status, _, _, _ = await self.request(
                    "GET", "/api/status", headers=headers
                )
                self.assertEqual(status, 400)
        self.assertEqual(len(self.executor.calls), calls)

        headers = self.auth_headers()
        headers["Host"] = f"localhost:{port}"
        status, _, payload, _ = await self.request(
            "GET", "/api/status", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    async def test_command_body_requires_bounded_utf8_json(self):
        await self.start()
        valid = json.dumps({"op": "status"}).encode()
        cases = [
            ("missing content type", {}, valid, 415),
            (
                "wrong content type",
                {"Content-Type": "text/plain"},
                valid,
                415,
            ),
            (
                "unsupported charset",
                {"Content-Type": "application/json; charset=iso-8859-1"},
                valid,
                415,
            ),
            (
                "compressed body",
                {
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                valid,
                415,
            ),
            (
                "malformed json",
                {"Content-Type": "application/json"},
                b'{"op":"camera.save","password":"Malformed-Secret"',
                400,
            ),
            (
                "empty body",
                {"Content-Type": "application/json"},
                b"",
                400,
            ),
        ]
        for name, headers, data, expected in cases:
            with self.subTest(case=name):
                status, response_headers, payload, raw = await self.request(
                    "POST",
                    "/api/command",
                    headers={**self.auth_headers(), **headers},
                    data=data,
                )
                self.assertEqual(status, expected)
                self.assertFalse(payload["ok"])
                self.assertNotIn("Malformed-Secret", raw.decode())
                self.assertIn(
                    "default-src 'none'", response_headers["Content-Security-Policy"]
                )

        for value in ([], "status", 3):
            with self.subTest(value=value):
                status, _, payload, _ = await self.request(
                    "POST",
                    "/api/command",
                    headers=self.auth_headers(),
                    command=value,
                )
                self.assertEqual(status, 400)
                self.assertFalse(payload["ok"])
        status, _, payload, _ = await self.request(
            "POST",
            "/api/command",
            headers={
                **self.auth_headers(),
                "Content-Type": "application/json",
            },
            data=b"null",
        )
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.executor.calls, [])

        oversized = {
            "op": "status",
            "padding": "x" * (MAX_BODY_BYTES + 1024),
        }
        status, headers, payload, raw = await self.request(
            "POST",
            "/api/command",
            headers=self.auth_headers(),
            command=oversized,
        )
        self.assertEqual(status, 413)
        self.assertFalse(payload["ok"])
        self.assertNotIn(b"x" * 256, raw)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.executor.calls, [])

        status, _, payload, _ = await self.request(
            "POST",
            "/api/command",
            headers={
                **self.auth_headers(),
                "Content-Type": "application/json; charset=UTF-8",
            },
            data=valid,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.executor.calls, [{"op": "status"}])

    async def test_execute_errors_redact_secrets_and_hide_unexpected_details(self):
        await self.start()
        password = "Camera-Password-7284"
        text = "private spoken message"
        self.executor.error = RuntimeError(
            f"rejected token={self.token} password={password}"
        )
        command = {
            "op": "camera.save",
            "id": "front",
            "camera": {
                "url": "http://192.0.2.10",
                "username": "admin",
                "channel": 1,
            },
            "password": password,
        }
        status, headers, payload, raw = await self.request(
            "POST",
            "/api/command",
            headers=self.auth_headers(),
            command=command,
        )
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertNotIn(self.token, raw.decode())
        self.assertNotIn(password, raw.decode())
        self.assertIn("[redacted]", payload["error"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

        self.executor.error = RuntimeError(f"rejected {text}")
        status, _, payload, raw = await self.request(
            "POST",
            "/api/command",
            headers=self.auth_headers(),
            command={
                "op": "tts.speak",
                "text": text,
                "devices": ["living"],
                "voice": "zh-CN-XiaoxiaoNeural",
            },
        )
        self.assertEqual(status, 400)
        self.assertNotIn(text, raw.decode())
        self.assertIn("[redacted]", payload["error"])

        self.executor.error = LookupError("private implementation detail")
        status, _, payload, raw = await self.request(
            "GET", "/api/status", headers=self.auth_headers()
        )
        self.assertEqual(status, 500)
        self.assertNotIn("private implementation detail", raw.decode())

    async def test_methods_and_unknown_paths_do_not_dispatch(self):
        await self.start()
        cases = [
            ("GET", "/api/command", 405),
            ("POST", "/api/status", 405),
            ("OPTIONS", "/api/command", 405),
            ("GET", "/api/unknown", 404),
            ("POST", "/", 405),
            ("GET", "/missing", 404),
        ]
        for method, path, expected in cases:
            with self.subTest(method=method, path=path):
                status, headers, payload, _ = await self.request(
                    method,
                    path,
                    headers=self.auth_headers(),
                    command={"op": "status"} if method == "POST" else None,
                    origin=self.origin,
                )
                self.assertEqual(status, expected)
                self.assertFalse(payload["ok"])
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _, payload, _ = await self.request("GET", "/api/unknown", headers={})
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.executor.calls, [])

    async def test_nonloopback_binding_requires_tls(self):
        for host in ("0.0.0.0", "::", "192.0.2.10"):
            with self.subTest(host=host):
                server = WebServer(
                    self.paths,
                    WebSettings(host=host, port=0),
                    self.executor,
                )
                with self.assertRaisesRegex(ValueError, "TLS"):
                    await server.start()
                self.assertIsNone(server.url)
                self.assertIsNone(server.token)
                self.assertFalse(self.paths.web_access.exists())
                await server.close()

    async def test_start_failure_cleans_up_before_the_access_file_exists(self):
        root = Path(self.directory.name)
        server = WebServer(
            self.paths,
            WebSettings(
                host="127.0.0.1",
                port=0,
                tls_cert=str(root / "missing-cert.pem"),
                tls_key=str(root / "missing-key.pem"),
            ),
            self.executor,
        )
        with self.assertRaises(OSError):
            await server.start()
        self.assertIsNone(server.url)
        self.assertFalse(self.paths.web_access.exists())
        await server.close()


if __name__ == "__main__":
    unittest.main()
