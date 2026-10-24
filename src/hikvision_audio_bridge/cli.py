"""Command-line controls for the per-user audio service."""

import argparse
import asyncio
import getpass
import json
import sys
from dataclasses import asdict

from .config import Camera, Paths
from .control import call, cleanup, serve
from .credentials import Credentials


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hikvision camera audio devices for PipeWire"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the user-session service")
    commands.add_parser(
        "cleanup",
        help="Remove this service's stale audio devices after an unclean exit",
    )
    commands.add_parser(
        "status", help="Show camera, devices, connection state and errors"
    )
    configure = commands.add_parser(
        "configure", help="Select a camera and save its password to the keyring"
    )
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
    for direction in ("speaker", "microphone"):
        switch = commands.add_parser(
            direction, help=f"Add or remove the system {direction} device"
        )
        switch.add_argument("state", choices=("on", "off"))
    commands.add_parser(
        "reconnect",
        help="Close the current session and reconnect with the saved configuration",
    )
    arguments = parser.parse_args()
    try:
        paths = Paths()
        if arguments.command == "serve":
            asyncio.run(serve(paths))
            return
        if arguments.command == "cleanup":
            asyncio.run(cleanup(paths))
            return
        command = {"op": arguments.command}
        if arguments.command == "configure":
            camera = Camera(arguments.url, arguments.username, arguments.channel)
            # Check service availability before asking for or storing a password.
            asyncio.run(call(paths, {"op": "status"}))
            credentials = Credentials()
            if arguments.reuse_password:
                credentials.get(camera)
            else:
                if not sys.stdin.isatty():
                    raise RuntimeError(
                        "Password entry requires an interactive terminal"
                    )
                password = getpass.getpass(
                    "Camera password (stored in the desktop keyring): "
                )
                credentials.set(camera, password)
                del password
            command["camera"] = asdict(camera)
        elif arguments.command in ("speaker", "microphone"):
            command = {
                "op": "set",
                "device": arguments.command,
                "enabled": arguments.state == "on",
            }
        print(json.dumps(asyncio.run(call(paths, command)), indent=2))
    except (Exception, KeyboardInterrupt) as error:
        print(f"Error: {error or type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
