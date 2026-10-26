"""Non-secret configuration and private per-user paths."""

import ipaddress
import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

APP_ID = "hikvision-audio-bridge"


@dataclass(frozen=True)
class Camera:
    url: str
    username: str
    channel: int = 1

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Camera URL must be an HTTP(S) origin without credentials")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid camera port")
        if not self.username.strip() or any(ord(c) < 32 for c in self.username):
            raise ValueError("Camera username must be nonempty and contain no controls")
        if type(self.channel) is not int or not 1 <= self.channel <= 65535:
            raise ValueError("Camera channel must be an integer from 1 to 65535")
        object.__setattr__(self, "url", urlunsplit(parsed._replace(path="")))


def validate_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value):
        raise ValueError("IDs must use 1-48 lowercase letters, digits, '_' or '-'")
    return value


@dataclass(frozen=True)
class AudioDevice:
    name: str
    direction: str
    cameras: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or len(self.name) > 100
            or any(ord(character) < 32 for character in self.name)
        ):
            raise ValueError(
                "Device names must contain 1-100 characters without controls"
            )
        if self.direction not in ("speaker", "microphone"):
            raise ValueError("Device direction must be speaker or microphone")
        if type(self.enabled) is not bool:
            raise ValueError("Device switches must be booleans")
        if not isinstance(self.cameras, tuple):
            raise ValueError("Device cameras must be a tuple")
        for identifier in self.cameras:
            validate_id(identifier)
        if len(set(self.cameras)) != len(self.cameras):
            raise ValueError("Device camera routes must be unique")

    @classmethod
    def from_dict(cls, value: object) -> "AudioDevice":
        if (
            not isinstance(value, dict)
            or set(value) - {"name", "direction", "cameras", "enabled"}
            or not {"name", "direction"} <= set(value)
            or not isinstance(value.get("cameras", []), list)
        ):
            raise ValueError("Invalid audio device settings")
        return cls(
            name=value["name"],
            direction=value["direction"],
            cameras=tuple(value.get("cameras", [])),
            enabled=value.get("enabled", True),
        )


@dataclass(frozen=True)
class WebSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    tls_cert: str | None = None
    tls_key: str | None = None

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
        for path in (self.tls_cert, self.tls_key):
            if path is not None and (not isinstance(path, str) or not path.strip()):
                raise ValueError(
                    "TLS certificate and key paths must be nonempty strings"
                )
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ValueError("Configure both web TLS certificate and key")

    @property
    def loopback(self) -> bool:
        return self.host == "localhost" or ipaddress.ip_address(self.host).is_loopback

    @classmethod
    def from_dict(cls, value: object) -> "WebSettings":
        if not isinstance(value, dict) or set(value) - {
            "host",
            "port",
            "tls_cert",
            "tls_key",
        }:
            raise ValueError("Invalid web settings")
        return cls(**value)


def camera_from_dict(value: object) -> Camera:
    if not isinstance(value, dict) or set(value) != {"url", "username", "channel"}:
        raise ValueError("Invalid camera settings")
    if not isinstance(value["url"], str) or not isinstance(value["username"], str):
        raise ValueError("Camera URL and username must be strings")
    return Camera(**value)


@dataclass(frozen=True)
class Settings:
    cameras: dict[str, Camera] = field(default_factory=dict)
    devices: dict[str, AudioDevice] = field(default_factory=dict)
    web: WebSettings = field(default_factory=WebSettings)

    def __post_init__(self) -> None:
        channels = set()
        for identifier, camera in self.cameras.items():
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
        for identifier, device in self.devices.items():
            validate_id(identifier)
            if set(device.cameras) - self.cameras.keys():
                raise ValueError(f"Device '{identifier}' references an unknown camera")

    @classmethod
    def from_dict(cls, value: object) -> "Settings":
        if not isinstance(value, dict) or set(value) - {"cameras", "devices", "web"}:
            raise ValueError("Invalid settings object")
        cameras = value.get("cameras", {})
        devices = value.get("devices", {})
        if not isinstance(cameras, dict) or not isinstance(devices, dict):
            raise ValueError("Cameras and devices must be objects keyed by ID")
        return cls(
            cameras={key: camera_from_dict(item) for key, item in cameras.items()},
            devices={key: AudioDevice.from_dict(item) for key, item in devices.items()},
            web=WebSettings.from_dict(value.get("web", {})),
        )

    @classmethod
    def migrate(cls, value: object) -> "Settings":
        """Read the published single-camera format without retaining a second model."""
        if not isinstance(value, dict) or not set(value) & {
            "camera",
            "speaker",
            "microphone",
        }:
            return cls.from_dict(value)
        if set(value) - {"camera", "speaker", "microphone"}:
            raise ValueError("Invalid legacy settings object")
        switches = {name: value.get(name, False) for name in ("speaker", "microphone")}
        if any(type(enabled) is not bool for enabled in switches.values()):
            raise ValueError("Device switches must be booleans")
        camera = value.get("camera")
        if camera is None:
            if any(switches.values()):
                raise ValueError("Configure a camera before enabling audio devices")
            return cls()
        return cls(
            cameras={"default": camera_from_dict(camera)},
            devices={
                name: AudioDevice(
                    f"Hikvision-{name.title()}", name, ("default",), enabled
                )
                for name, enabled in switches.items()
            },
        )


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

    def load(self) -> Settings:
        path = self.config / "settings.json"
        if not path.exists():
            return Settings()
        return Settings.migrate(json.loads(path.read_text()))

    def save(self, settings: Settings) -> None:
        descriptor, temporary = tempfile.mkstemp(dir=self.config, prefix=".settings-")
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(asdict(settings), output, indent=2)
                output.write("\n")
            os.replace(temporary, self.config / "settings.json")
        finally:
            Path(temporary).unlink(missing_ok=True)


def _runtime_root() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value:
        raise RuntimeError(
            "XDG_RUNTIME_DIR is required; run in a logged-in user session"
        )
    return Path(value)
