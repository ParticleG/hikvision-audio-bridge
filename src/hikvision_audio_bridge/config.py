"""Validated non-secret configuration for independent server and desktop roles."""

import ipaddress
import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .audio import MAX_STREAMS

APP_ID = "hikvision-audio-bridge"


def validate_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value):
        raise ValueError("IDs must use 1-48 lowercase letters, digits, '_' or '-'")
    return value


def _name(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 100
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("Names must contain 1-100 characters without controls")


def _ids(value: tuple[str, ...]) -> None:
    if not isinstance(value, tuple):
        raise ValueError("Routes must be tuples of IDs")
    for identifier in value:
        validate_id(identifier)
    if len(set(value)) != len(value):
        raise ValueError("Routes must contain unique IDs")


def _object(value: object, allowed: set[str], required: tuple[str, ...] = ()) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) - allowed
        or not set(required) <= set(value)
    ):
        raise ValueError("Invalid settings object or unexpected fields")
    return value


def _list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("Routes must be arrays of IDs")
    result = tuple(value)
    _ids(result)
    return result


def _origin(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("URL must be an HTTP(S) origin without credentials")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(ord(character) <= 32 for character in value)
    ):
        raise ValueError("URL must be an HTTP(S) origin without credentials")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid port")
    return urlunsplit(parsed._replace(path=""))


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Camera:
    url: str
    username: str
    channel: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _origin(self.url))
        if (
            not isinstance(self.username, str)
            or not self.username.strip()
            or any(ord(character) < 32 for character in self.username)
        ):
            raise ValueError("Camera username must be nonempty and contain no controls")
        if type(self.channel) is not int or not 1 <= self.channel <= 65535:
            raise ValueError("Camera channel must be an integer from 1 to 65535")


def camera_from_dict(value: object) -> Camera:
    fields = {"url", "username", "channel"}
    return Camera(**_object(value, fields, tuple(fields)))


@dataclass(frozen=True)
class AudioDevice:
    """An expanded logical audio route; never a persisted server-side device."""

    name: str
    direction: str
    cameras: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        _name(self.name)
        if self.direction not in ("speaker", "microphone"):
            raise ValueError("Device direction must be speaker or microphone")
        if type(self.enabled) is not bool:
            raise ValueError("Device switches must be booleans")
        _ids(self.cameras)

    @classmethod
    def from_dict(cls, value: object) -> "AudioDevice":
        value = _object(
            value, {"name", "direction", "cameras", "enabled"}, ("name", "direction")
        )
        return cls(
            value["name"],
            value["direction"],
            _list(value.get("cameras", [])),
            value.get("enabled", True),
        )


@dataclass(frozen=True)
class CameraGroup:
    name: str
    cameras: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _name(self.name)
        _ids(self.cameras)

    @classmethod
    def from_dict(cls, value: object) -> "CameraGroup":
        value = _object(value, {"name", "cameras"}, ("name", "cameras"))
        return cls(value["name"], _list(value["cameras"]))


@dataclass(frozen=True)
class DesktopDevice:
    name: str
    direction: str
    groups: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        _name(self.name)
        if self.direction not in ("speaker", "microphone"):
            raise ValueError("Device direction must be speaker or microphone")
        if type(self.enabled) is not bool:
            raise ValueError("Device switches must be booleans")
        _ids(self.groups)

    @classmethod
    def from_dict(cls, value: object) -> "DesktopDevice":
        value = _object(
            value, {"name", "direction", "groups", "enabled"}, ("name", "direction")
        )
        return cls(
            value["name"],
            value["direction"],
            _list(value.get("groups", [])),
            value.get("enabled", True),
        )


@dataclass(frozen=True)
class WebSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    tls_cert: str | None = None
    tls_key: str | None = None
    allow_insecure_lan: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.host, str):
            raise ValueError("Web host must be an IP address or localhost")
        if self.host != "localhost":
            try:
                ipaddress.ip_address(self.host)
            except ValueError:
                raise ValueError(
                    "Web host must be an IP address or localhost"
                ) from None
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("Web port must be an integer from 0 to 65535")
        if type(self.allow_insecure_lan) is not bool:
            raise ValueError("allow_insecure_lan must be a boolean")
        for path in (self.tls_cert, self.tls_key):
            if path is not None and (not isinstance(path, str) or not path.strip()):
                raise ValueError(
                    "TLS certificate and key paths must be nonempty strings"
                )
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ValueError("Configure both web TLS certificate and key")

    @property
    def loopback(self) -> bool:
        return is_loopback(self.host)

    @classmethod
    def from_dict(cls, value: object) -> "WebSettings":
        return cls(
            **_object(
                value, {"host", "port", "tls_cert", "tls_key", "allow_insecure_lan"}
            )
        )


@dataclass(frozen=True)
class BackendSettings:
    url: str
    name: str = "Desktop"
    ca_file: str | None = None
    allow_insecure_lan: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _origin(self.url))
        _name(self.name)
        if type(self.allow_insecure_lan) is not bool:
            raise ValueError("allow_insecure_lan must be a boolean")
        if self.ca_file is not None and (
            not isinstance(self.ca_file, str) or not self.ca_file.strip()
        ):
            raise ValueError("CA file must be a nonempty path")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme == "http"
            and not is_loopback(parsed.hostname or "")
            and not self.allow_insecure_lan
        ):
            raise ValueError("LAN audio requires HTTPS or explicit allow_insecure_lan")

    @classmethod
    def from_dict(cls, value: object) -> "BackendSettings":
        return cls(
            **_object(value, {"url", "name", "ca_file", "allow_insecure_lan"}, ("url",))
        )


def _validate_cameras(cameras: dict[str, Camera]) -> None:
    channels = set()
    for identifier, camera in cameras.items():
        validate_id(identifier)
        origin = urlsplit(camera.url)
        channel = (
            origin.scheme,
            origin.hostname,
            origin.port or (443 if origin.scheme == "https" else 80),
            camera.channel,
        )
        if channel in channels:
            raise ValueError("A camera channel must have only one camera ID")
        channels.add(channel)


@dataclass(frozen=True)
class RoutingSettings:
    cameras: dict[str, Camera] = field(default_factory=dict)
    devices: dict[str, AudioDevice] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_cameras(self.cameras)
        for identifier, device in self.devices.items():
            validate_id(identifier)
            if set(device.cameras) - self.cameras.keys():
                raise ValueError(f"Device '{identifier}' references an unknown camera")


@dataclass(frozen=True)
class ServerSettings:
    cameras: dict[str, Camera] = field(default_factory=dict)
    web: WebSettings = field(default_factory=WebSettings)

    def __post_init__(self) -> None:
        _validate_cameras(self.cameras)

    @classmethod
    def from_dict(cls, value: object) -> "ServerSettings":
        value = _object(value, {"cameras", "web"})
        cameras = value.get("cameras", {})
        if not isinstance(cameras, dict):
            raise ValueError("Cameras must be an object keyed by ID")
        return cls(
            {key: camera_from_dict(item) for key, item in cameras.items()},
            WebSettings.from_dict(value.get("web", {})),
        )


@dataclass(frozen=True)
class DesktopSettings:
    backend: BackendSettings | None = None
    groups: dict[str, CameraGroup] = field(default_factory=dict)
    devices: dict[str, DesktopDevice] = field(default_factory=dict)
    web: WebSettings = field(default_factory=WebSettings)

    def __post_init__(self) -> None:
        for identifier in self.groups:
            validate_id(identifier)
        for identifier, device in self.devices.items():
            validate_id(identifier)
            if set(device.groups) - self.groups.keys():
                raise ValueError(f"Device '{identifier}' references an unknown group")
        if len(self.devices) > MAX_STREAMS:
            raise ValueError(
                f"A desktop supports at most {MAX_STREAMS} virtual devices"
            )

    def routes(self) -> dict[str, AudioDevice]:
        return {
            identifier: AudioDevice(
                device.name,
                device.direction,
                tuple(
                    dict.fromkeys(
                        camera
                        for group in device.groups
                        for camera in self.groups[group].cameras
                    )
                ),
                device.enabled,
            )
            for identifier, device in self.devices.items()
        }

    @classmethod
    def from_dict(cls, value: object) -> "DesktopSettings":
        value = _object(value, {"backend", "groups", "devices", "web"})
        groups, devices = value.get("groups", {}), value.get("devices", {})
        if not isinstance(groups, dict) or not isinstance(devices, dict):
            raise ValueError("Groups and devices must be objects keyed by ID")
        backend = value.get("backend")
        return cls(
            BackendSettings.from_dict(backend) if backend is not None else None,
            {key: CameraGroup.from_dict(item) for key, item in groups.items()},
            {key: DesktopDevice.from_dict(item) for key, item in devices.items()},
            WebSettings.from_dict(value.get("web", {})),
        )

    @classmethod
    def migrate(cls, value: object) -> "DesktopSettings":
        """Convert published local routes; old camera settings are backed up on save."""
        if not isinstance(value, dict) or not set(value) & {
            "camera",
            "cameras",
            "speaker",
            "microphone",
        }:
            return cls.from_dict(value)
        if set(value) & {"camera", "speaker", "microphone"}:
            value = _object(value, {"camera", "speaker", "microphone"})
            switches = {
                name: value.get(name, False) for name in ("speaker", "microphone")
            }
            if any(type(enabled) is not bool for enabled in switches.values()):
                raise ValueError("Device switches must be booleans")
            camera = value.get("camera")
            if camera is None:
                if any(switches.values()):
                    raise ValueError("Configure a camera before enabling audio devices")
                return cls()
            camera_from_dict(camera)
            value = {
                "cameras": {"default": camera},
                "devices": {
                    name: {
                        "name": f"Hikvision-{name.title()}",
                        "direction": name,
                        "cameras": ["default"],
                        "enabled": enabled,
                    }
                    for name, enabled in switches.items()
                },
            }
        value = _object(value, {"cameras", "devices", "web"})
        cameras, devices = value.get("cameras", {}), value.get("devices", {})
        if not isinstance(cameras, dict) or not isinstance(devices, dict):
            raise ValueError("Cameras and devices must be objects keyed by ID")
        routes = RoutingSettings(
            {key: camera_from_dict(item) for key, item in cameras.items()},
            {key: AudioDevice.from_dict(item) for key, item in devices.items()},
        )
        return cls(
            groups={
                key: CameraGroup(device.name, device.cameras)
                for key, device in routes.devices.items()
            },
            devices={
                key: DesktopDevice(
                    device.name, device.direction, (key,), device.enabled
                )
                for key, device in routes.devices.items()
            },
            web=WebSettings.from_dict(value.get("web", {})),
        )


def write_private(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class Paths:
    runtime: Path = field(default_factory=lambda: _runtime_root() / APP_ID)
    config: Path = field(
        default_factory=lambda: (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / APP_ID
        )
    )

    @property
    def socket(self) -> Path:
        return self.runtime / "control.sock"

    @property
    def web_access(self) -> Path:
        return self.runtime / "web-access.json"

    def prepare(self) -> None:
        for path in (self.runtime, self.config):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError(
                    f"Directory must be owned by the current user: {path}"
                )
            path.chmod(0o700)

    def read_settings(self) -> dict:
        path = self.config / "settings.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def load_desktop(self) -> DesktopSettings:
        return DesktopSettings.migrate(self.read_settings())

    def load_server(self) -> ServerSettings:
        return ServerSettings.from_dict(self.read_settings())

    def save(self, settings: ServerSettings | DesktopSettings) -> None:
        self.prepare()
        previous = self.read_settings()
        if isinstance(settings, DesktopSettings) and set(previous) & {
            "camera",
            "cameras",
            "speaker",
            "microphone",
        }:
            descriptor, _ = tempfile.mkstemp(
                dir=self.config, prefix="settings-legacy-", suffix=".json"
            )
            with os.fdopen(descriptor, "w") as output:
                json.dump(previous, output, indent=2)
                output.write("\n")
        write_private(
            self.config / "settings.json",
            (json.dumps(asdict(settings), indent=2) + "\n").encode(),
        )


def _runtime_root() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value:
        raise RuntimeError(
            "XDG_RUNTIME_DIR is required; run in a logged-in user session"
        )
    return Path(value)
