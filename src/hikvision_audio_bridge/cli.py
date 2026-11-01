"""Separate desktop and headless-server command-line entry points."""

import argparse
import asyncio
import getpass
import json
import socket
import sys
import webbrowser
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import quote

from .config import BackendSettings, Paths, WebSettings, validate_id
from .control import call, cleanup, serve, serve_server


def _parser(server: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hikvision camera server"
        if server
        else "Hikvision network audio devices for PipeWire"
    )
    if server:
        parser.add_argument(
            "--state-dir", type=Path, default=Path("/var/lib/hikvision-audio-server")
        )
        parser.add_argument(
            "--runtime-dir", type=Path, default=Path("/run/hikvision-audio-server")
        )
    commands = parser.add_subparsers(dest="command", required=True)
    daemon = commands.add_parser("serve", help="Run this component and its WebUI")
    daemon.add_argument("--web-host", help="Bind IP address")
    daemon.add_argument(
        "--web-port", type=int, help="HTTP(S) port; 0 selects a free port"
    )
    daemon.add_argument("--web-tls-cert", help="PEM certificate file")
    daemon.add_argument("--web-tls-key", help="PEM private key file")
    daemon.add_argument(
        "--allow-insecure-lan",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Explicitly allow plaintext LAN HTTP; passwords, tokens and audio "
            "are exposed in transit"
        ),
    )
    commands.add_parser("status", help="Show this component's current state")
    web = commands.add_parser("web", help="Open this component's authenticated WebUI")
    web.add_argument(
        "--print-url",
        action="store_true",
        help="Print the private login URL; do not share it",
    )
    reconnect = commands.add_parser(
        "reconnect", help="Explicitly reconnect after an error"
    )
    if server:
        reconnect.add_argument(
            "--id", type=validate_id, help="Reconnect one camera only"
        )
        commands.add_parser(
            "audio-token", help="Print the audio-scoped desktop pairing token"
        )
        return parser
    commands.add_parser("cleanup", help="Remove this desktop's stale audio devices")
    connect = commands.add_parser(
        "connect", help="Configure the camera backend and its audio token"
    )
    connect.add_argument("url")
    connect.add_argument("--name", default=socket.gethostname())
    connect.add_argument("--ca-file")
    connect.add_argument("--allow-insecure-lan", action="store_true")
    connect.add_argument(
        "--reuse-token",
        action="store_true",
        help="Reuse the stored Secret Service token",
    )
    commands.add_parser(
        "disconnect", help="Remove the backend connection; retain groups and devices"
    )
    group = commands.add_parser("group", help="Manage local named camera groups")
    group_commands = group.add_subparsers(dest="action", required=True)
    group_commands.add_parser("list")
    group_commands.add_parser("remove").add_argument("id", type=validate_id)
    group_save = group_commands.add_parser("save")
    group_save.add_argument("id", type=validate_id)
    group_save.add_argument("--name")
    group_save.add_argument("--cameras", nargs="*", type=validate_id, default=[])
    device = commands.add_parser(
        "device", help="Manage local virtual speakers and microphones"
    )
    device_commands = device.add_subparsers(dest="action", required=True)
    device_commands.add_parser("list")
    save = device_commands.add_parser("save")
    save.add_argument("id", type=validate_id)
    save.add_argument("--name")
    save.add_argument("--direction", choices=("speaker", "microphone"), required=True)
    save.add_argument("--groups", nargs="*", type=validate_id, default=[])
    save.add_argument("--disabled", action="store_true")
    for action in ("remove", "on", "off"):
        device_commands.add_parser(action).add_argument("id", type=validate_id)
    return parser


def _web_overrides(arguments: argparse.Namespace, settings: WebSettings) -> WebSettings:
    values = {
        key: value
        for key in ("host", "port", "tls_cert", "tls_key")
        if (value := getattr(arguments, f"web_{key}")) is not None
    }
    if arguments.allow_insecure_lan is not None:
        values["allow_insecure_lan"] = arguments.allow_insecure_lan
    return replace(settings, **values)


def _launch_web(paths: Paths, print_url: bool) -> None:
    asyncio.run(call(paths, {"op": "status"}))
    access = json.loads(paths.web_access.read_text())
    url = access["url"].rstrip("/") + "/#token=" + quote(access["token"], safe="")
    if print_url:
        print(url)
    elif not webbrowser.open(url):
        raise RuntimeError("Cannot open a browser; use 'web --print-url' locally")


def _main(server: bool) -> None:
    arguments = _parser(server).parse_args()
    try:
        paths = Paths(arguments.runtime_dir, arguments.state_dir) if server else Paths()
        if arguments.command == "serve":
            settings = paths.load_server() if server else paths.load_desktop()
            web = _web_overrides(arguments, settings.web)
            asyncio.run(serve_server(paths, web) if server else serve(paths, web))
            return
        if arguments.command == "cleanup":
            asyncio.run(cleanup(paths))
            return
        if arguments.command == "web":
            _launch_web(paths, arguments.print_url)
            return
        command = {"op": arguments.command}
        if arguments.command == "audio-token":
            status = asyncio.run(call(paths, {"op": "access.show"}))
            print(status["audio_access_token"])
            return
        if arguments.command == "connect":
            backend = BackendSettings(
                arguments.url,
                arguments.name,
                arguments.ca_file,
                arguments.allow_insecure_lan,
            )
            command = {"op": "backend.save", "backend": asdict(backend)}
            if not arguments.reuse_token:
                if not sys.stdin.isatty():
                    raise RuntimeError(
                        "Audio token entry requires an interactive terminal"
                    )
                command["token"] = getpass.getpass(
                    "Audio access token (stored in Secret Service): "
                )
        elif arguments.command == "disconnect":
            command = {"op": "backend.remove"}
        elif arguments.command == "reconnect" and server and arguments.id is not None:
            command["id"] = arguments.id
        elif arguments.command in ("group", "device"):
            if arguments.action == "list":
                status = asyncio.run(call(paths, {"op": "status"}))
                print(json.dumps(status["settings"][arguments.command + "s"], indent=2))
                return
            command = {
                "op": f"{arguments.command}.{arguments.action}",
                "id": arguments.id,
            }
            if arguments.action in ("on", "off"):
                command.update(op="device.set", enabled=arguments.action == "on")
            elif arguments.action == "save" and arguments.command == "group":
                command["group"] = {
                    "name": arguments.name or arguments.id,
                    "cameras": arguments.cameras,
                }
            elif arguments.action == "save":
                command["device"] = {
                    "name": arguments.name or arguments.id,
                    "direction": arguments.direction,
                    "groups": arguments.groups,
                    "enabled": not arguments.disabled,
                }
        print(json.dumps(asyncio.run(call(paths, command)), indent=2))
    except (Exception, KeyboardInterrupt) as error:
        print(f"Error: {error or type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


def main() -> None:
    _main(False)


def server_main() -> None:
    _main(True)


if __name__ == "__main__":
    main()
