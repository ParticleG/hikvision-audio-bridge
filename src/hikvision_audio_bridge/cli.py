"""Command-line controls for the per-user audio service."""

import argparse
import asyncio
import getpass
import json
import sys
import webbrowser
from dataclasses import asdict, replace
from urllib.parse import quote

from .config import Camera, Paths, Settings, validate_id
from .control import call, cleanup, serve
from .credentials import Credentials


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hikvision camera audio routing and WebUI for PipeWire"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    daemon = commands.add_parser("serve", help="Run the user-session service and WebUI")
    daemon.add_argument("--web-host", help="Bind IP address; non-loopback requires TLS")
    daemon.add_argument(
        "--web-port", type=int, help="HTTP(S) port; 0 selects a free port"
    )
    daemon.add_argument("--web-tls-cert", help="PEM certificate file")
    daemon.add_argument("--web-tls-key", help="PEM private key file")
    commands.add_parser(
        "cleanup", help="Remove this service's stale devices after an unclean exit"
    )
    commands.add_parser("status", help="Show devices, camera connections and TTS state")
    web = commands.add_parser("web", help="Open the authenticated WebUI in a browser")
    web.add_argument(
        "--print-url",
        action="store_true",
        help="Print the private login URL without opening a browser; do not share it",
    )
    configure = commands.add_parser(
        "configure", help="Add/update a camera and save its password to the keyring"
    )
    configure.add_argument("--id", type=validate_id, default="default")
    configure.add_argument(
        "--url", required=True, help="HTTP(S) camera origin, without credentials"
    )
    configure.add_argument("--username", default="admin")
    configure.add_argument("--channel", type=int, default=1)
    configure.add_argument(
        "--reuse-password",
        action="store_true",
        help="Use this camera's existing keyring password",
    )
    camera = commands.add_parser("camera", help="List or remove configured cameras")
    camera_commands = camera.add_subparsers(dest="action", required=True)
    camera_commands.add_parser("list")
    camera_commands.add_parser("remove").add_argument("id", type=validate_id)
    device = commands.add_parser("device", help="Manage named virtual audio devices")
    device_commands = device.add_subparsers(dest="action", required=True)
    device_commands.add_parser("list")
    save = device_commands.add_parser("save", help="Create or replace a device")
    save.add_argument("id", type=validate_id)
    save.add_argument("--name", help="Display name shown in application audio settings")
    save.add_argument("--direction", choices=("speaker", "microphone"), required=True)
    save.add_argument("--cameras", nargs="*", type=validate_id, default=[])
    save.add_argument("--disabled", action="store_true")
    for action in ("remove", "on", "off"):
        device_commands.add_parser(action).add_argument("id", type=validate_id)
    for direction in ("speaker", "microphone"):
        switch = commands.add_parser(
            direction, help=f"Toggle the default or named {direction} device"
        )
        switch.add_argument("state", choices=("on", "off"))
        switch.add_argument("--id", type=validate_id, default=direction)
    reconnect = commands.add_parser("reconnect", help="Explicitly reconnect cameras")
    reconnect.add_argument("--id", type=validate_id, help="Reconnect only this camera")
    return parser


def _configure(paths: Paths, arguments: argparse.Namespace) -> dict:
    camera = Camera(arguments.url, arguments.username, arguments.channel)
    # Validate against the live configuration before asking for a secret.
    status = asyncio.run(call(paths, {"op": "status"}))
    settings = Settings.from_dict(status["settings"])
    replace(settings, cameras={**settings.cameras, arguments.id: camera})
    credentials = Credentials()
    if arguments.reuse_password:
        credentials.get(camera)
    else:
        if not sys.stdin.isatty():
            raise RuntimeError("Password entry requires an interactive terminal")
        password = getpass.getpass("Camera password (stored in the desktop keyring): ")
        credentials.set(camera, password)
        del password
    return {"op": "camera.save", "id": arguments.id, "camera": asdict(camera)}


def _switch(paths: Paths, arguments: argparse.Namespace) -> dict:
    status = asyncio.run(call(paths, {"op": "status"}))
    identifier = arguments.id
    device = status["settings"]["devices"].get(identifier)
    if device is not None:
        if device["direction"] != arguments.command:
            raise ValueError("The selected device has a different audio direction")
        return {
            "op": "device.set",
            "id": identifier,
            "enabled": arguments.state == "on",
        }
    if identifier != arguments.command:
        raise ValueError("Unknown device ID; use 'device save' to create it")
    if "default" not in status["settings"]["cameras"]:
        raise ValueError(
            "Configure the default camera before enabling its audio devices"
        )
    return {
        "op": "device.save",
        "id": identifier,
        "device": {
            "name": f"Hikvision-{arguments.command.title()}",
            "direction": arguments.command,
            "cameras": ["default"],
            "enabled": arguments.state == "on",
        },
    }


def main() -> None:
    arguments = _parser().parse_args()
    try:
        paths = Paths()
        if arguments.command == "serve":
            overrides = {
                name: value
                for name in ("host", "port", "tls_cert", "tls_key")
                if (value := getattr(arguments, f"web_{name}")) is not None
            }
            web_settings = replace(paths.load().web, **overrides) if overrides else None
            asyncio.run(serve(paths, web_settings))
            return
        if arguments.command == "cleanup":
            asyncio.run(cleanup(paths))
            return
        if arguments.command == "web":
            asyncio.run(call(paths, {"op": "status"}))
            access = json.loads(paths.web_access.read_text())
            url = (
                access["url"].rstrip("/") + "/#token=" + quote(access["token"], safe="")
            )
            if arguments.print_url:
                print(url)
            elif not webbrowser.open(url):
                raise RuntimeError(
                    "Cannot open a browser; use 'web --print-url' locally"
                )
            return
        command = {"op": arguments.command}
        if arguments.command == "configure":
            command = _configure(paths, arguments)
        elif arguments.command in ("speaker", "microphone"):
            command = _switch(paths, arguments)
        elif arguments.command == "reconnect":
            if arguments.id is not None:
                command["id"] = arguments.id
        elif arguments.command in ("camera", "device"):
            if arguments.action == "list":
                status = asyncio.run(call(paths, {"op": "status"}))
                print(json.dumps(status["settings"][arguments.command + "s"], indent=2))
                return
            command = {
                "op": f"{arguments.command}.{arguments.action}",
                "id": arguments.id,
            }
            if arguments.action in ("on", "off"):
                command["op"] = "device.set"
                command["enabled"] = arguments.action == "on"
            elif arguments.command == "device" and arguments.action == "save":
                command["device"] = {
                    "name": arguments.name or arguments.id,
                    "direction": arguments.direction,
                    "cameras": arguments.cameras,
                    "enabled": not arguments.disabled,
                }
        print(json.dumps(asyncio.run(call(paths, command)), indent=2))
    except (Exception, KeyboardInterrupt) as error:
        print(f"Error: {error or type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
