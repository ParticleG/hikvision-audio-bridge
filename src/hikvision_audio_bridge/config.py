"""Non-secret configuration and private per-user paths."""

import json
import os
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


@dataclass(frozen=True)
class Settings:
    camera: Camera | None = None
    speaker: bool = False
    microphone: bool = False

    @classmethod
    def from_dict(cls, value: object) -> "Settings":
        if not isinstance(value, dict) or set(value) - {
            "camera",
            "speaker",
            "microphone",
        }:
            raise ValueError("Invalid settings object")
        speaker = value.get("speaker", False)
        microphone = value.get("microphone", False)
        if type(speaker) is not bool or type(microphone) is not bool:
            raise ValueError("Device switches must be booleans")
        camera = value.get("camera")
        if camera is not None:
            if not isinstance(camera, dict) or set(camera) != {
                "url",
                "username",
                "channel",
            }:
                raise ValueError("Invalid camera settings")
            if not isinstance(camera["url"], str) or not isinstance(
                camera["username"], str
            ):
                raise ValueError("Camera URL and username must be strings")
            camera = Camera(**camera)
        if (speaker or microphone) and camera is None:
            raise ValueError("Configure a camera before enabling audio devices")
        return cls(camera=camera, speaker=speaker, microphone=microphone)


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
        return Settings.from_dict(json.loads(path.read_text()))

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
