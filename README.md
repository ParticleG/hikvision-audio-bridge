# Hikvision Audio Bridge

A Linux user-session service that exposes Hikvision intercoms as ordinary
PipeWire speaker and microphone devices. Create multiple independently named
virtual devices and connect each to multiple cameras:

- A **speaker** sends application playback to every camera in its routes.
- A **microphone** mixes the audio received from its routed cameras into one local
  microphone. A camera can feed several microphones at once.
- Overlapping routes share one owned ISAPI session per configured camera channel.
- The **WebUI** manages cameras, passwords, devices, routes and connection state.
- **Text to speech** broadcasts Edge TTS audio through selected speaker routes.

No root privileges, kernel driver, vendor SDK, RTSP/video or camera configuration
changes are required. Camera audio is not recorded. Ordinary audio bridging is
local; the optional TTS feature sends the entered text to Microsoft's online service.

## Requirements

- Linux with a logged-in systemd user session, PipeWire, `pipewire-pulse` and
  WirePlumber. The backend uses `module-pipe-sink` and `module-pipe-source`;
  native PulseAudio is not the supported target.
- `pactl` (`libpulse` on Arch Linux), Python 3.13 or newer, and `uv`.
- An unlocked desktop Secret Service, such as GNOME Keyring, for camera passwords.
- **FFmpeg for TTS**, plus network access to Edge TTS. FFmpeg is not needed for
  ordinary camera playback/receive routing. No local voice model or API key is needed.
- Cameras supporting ISAPI two-way audio, Digest authentication, and a channel
  configured for `G.711alaw` or `G.711ulaw` at 8 kHz mono. The bridge never changes
  camera settings or displaces another intercom client's session.

The development target is Arch Linux with PipeWire 1.6.9. Python dependencies are
locked in `uv.lock`; `audioop-lts` supplies the G.711 conversion API.

## Install and run

```sh
uv sync --frozen
make check
make install
systemctl --user start hikvision-audio.service
hikvision-audio web
```

`make install` installs the CLI and user unit and reloads the user service manager.
It does not start the service or enable login startup. Ensure `~/.local/bin` is on
`PATH`. To update an already installed version, run `uv tool install --reinstall .`,
then restart the user service when an audio interruption is acceptable.

To run directly from the checkout:

```sh
uv run hikvision-audio serve
# In a second terminal:
uv run hikvision-audio web
```

Only one instance can own a runtime directory. Readiness means both the private
Unix control socket and WebUI listener are ready, not that cameras have connected.
The service does not change the system's default input or output device.

## WebUI configuration

The default listener is `http://127.0.0.1:8765`. `hikvision-audio web` opens an
authenticated browser session. Direct visits show a token sign-in form.

1. Add cameras with a stable ID, HTTP(S) origin, username, channel and password.
   Passwords go directly into Secret Service, never settings, status or logs.
   Leave a password blank to reuse its existing keyring entry.
2. Add local devices with a unique ID, display name, direction and camera routes.
   Multiple routes are selected with Ctrl/Command in the selection list. An empty
   route list is allowed; it leaves the device present without a camera session.
3. Select the resulting device in your application's audio settings. Technical
   names are `hikvision_audio_<device-id>`; display names preserve spaces and Unicode.
4. Inspect each camera's connection, codec, byte counts, transport error and
   cleanup error. **Reconnect** retries only that camera.

Device switches are independent. Turning a device off unloads it from PipeWire
but retains its definition and routes; removing it deletes the definition.
Applications may choose a different default device after an unload, so use their
own mute controls if you need to preserve selection. Camera deletion is rejected
while any device, including a disabled one, still references it. Removing a camera
configuration does not delete keyring entries shared by its origin and username.

Route edits do not reload unchanged local devices or reconnect unaffected camera
sessions. Changing a device's display name or direction recreates that local
node. Camera identity changes and explicit reconnects finish old-session cleanup
and discard buffered audio before opening a replacement session.

## CLI controls

The original default-camera workflow remains available:

```sh
hikvision-audio configure --url http://camera.example.test --username admin --channel 1
hikvision-audio speaker on
hikvision-audio microphone on
hikvision-audio speaker off
```

It uses camera ID `default` and device IDs `speaker` and `microphone`, retaining
the published technical names `hikvision_audio_speaker` and
`hikvision_audio_microphone`. CLI password entry requires an interactive terminal;
password arguments, environment variables and piped password input are not supported.

For multiple cameras and devices:

```sh
hikvision-audio configure --id entrance --url http://entrance.example.test --username admin
hikvision-audio configure --id garage --url https://garage.example.test --username admin

hikvision-audio device save announcements --name "Announcements" --direction speaker --cameras entrance garage
hikvision-audio device save monitoring --name "Monitoring" --direction microphone --cameras entrance garage
hikvision-audio device save garage-only --name "Garage speaker" --direction speaker --cameras garage --disabled

hikvision-audio device on garage-only
hikvision-audio device off monitoring
hikvision-audio device list
hikvision-audio camera list
hikvision-audio reconnect --id entrance
hikvision-audio status
hikvision-audio device remove garage-only
```

`configure --reuse-password` reuses the existing Secret Service entry and explicitly
reconnects the selected camera. `reconnect` without `--id` reconnects all routed
cameras. `device save` creates or replaces the complete device definition.
`camera remove <id>` removes an unreferenced camera.

Status has four top-level sections: `settings`, `cameras`, `devices` and `tts`.
Camera counters and errors are under `cameras.<id>`, not a single global connection
field. Configuration requests apply locally; connection attempts are asynchronous.
On camera failure, enabled virtual devices remain present, other cameras continue,
and no automatic credential retry loop runs. Resolve the cause and reconnect.

## WebUI binding and LAN access

Every API request requires a per-run bearer token, including loopback requests.
The private `0700` runtime directory contains `web-access.json` with mode `0600`.
`hikvision-audio web --print-url` explicitly prints the private login URL for use
in another browser. Treat the URL/token as a password; do not publish or log it.
The browser removes the token fragment from the address bar and keeps the token
only in tab session storage. Restarting the daemon rotates the token.

Non-loopback listeners **require HTTPS** because the WebUI accepts camera passwords.
Supply a certificate trusted by the client and valid for the address it will use:

```sh
hikvision-audio serve --web-host 0.0.0.0 --web-port 8765 \
  --web-tls-cert /absolute/path/certificate.pem \
  --web-tls-key /absolute/path/private-key.pem
```

Use the server's LAN address instead of `127.0.0.1` in the browser. Binding an
explicit LAN IP or IPv6 address is also supported. The browser's origin/Host must
match the bound address and port; this is a direct listener, not a forwarded-header
or reverse-proxy authentication interface. Cross-origin API access is not enabled.
Protect the TLS key with user-only filesystem permissions.

The same options can be persisted in the `web` object in `settings.json`:

```json
{
  "web": {
    "host": "0.0.0.0",
    "port": 8765,
    "tls_cert": "/absolute/path/certificate.pem",
    "tls_key": "/absolute/path/private-key.pem"
  }
}
```

This is a minimal new-installation example. When editing an existing file, retain
its `cameras` and `devices` objects. Stop the service before editing the file;
listener changes take effect on startup. CLI `--web-*` options override saved values
for that invocation only. Port `0` selects an available port, recorded in the
runtime access file. The WebUI itself does not edit listener/TLS settings.

## Text to speech

In the WebUI, enter plain text, choose an Edge voice short name and select one or
more enabled speaker devices. The default is `zh-CN-XiaoxiaoNeural`. Speech uses
Python `edge-tts` with the same provider and MP3 format as the referenced
`wedeep-lark` TTS implementation, then FFmpeg pipes convert it to 8 kHz mono PCM16.
No speech text, MP3 or PCM file is written to disk.

- **Online service:** entered text is sent to Microsoft. Do not enter sensitive text.
  This is an unofficial Edge service interface; availability and voices can change.
- Every selected speaker must have routes, and all selected routes must be
  connected. The bridge rejects incomplete targets instead of silently skipping them.
- Overlapping speaker routes are deduplicated: each target camera receives speech
  once. Speech mixes with ordinary application audio, rather than stopping it.
- Only one speech request runs at a time. **Stop speech** cancels synthesis or
  clears pending speech playback; it does not disable devices or restart cameras.
- Changing affected routes/cameras or losing a target connection stops the speech
  request rather than sending it to an unintended destination.
- Input is limited to 2,000 characters, synthesis/conversion to 30 seconds, and
  decoded audio to 120 seconds. Exceeding a limit reports an error, not truncation.
- Playback completion tracks frames consumed by camera transports. It cannot prove
  when a physical camera finishes its own internal speaker buffer.

## Stored data and lifecycle

Settings live at `$XDG_CONFIG_HOME/hikvision-audio-bridge/settings.json` (normally
under `~/.config`) with mode `0600`. They contain `cameras` keyed by ID, `devices`
keyed by ID, and `web` listener settings. Each device stores `name`, `direction`,
`cameras` and `enabled`. Unknown fields, non-boolean switches, duplicate routes and
unknown camera IDs are rejected. Duplicate canonical origin/channel definitions
are rejected; do not register the same physical channel under different DNS aliases.

Existing single-camera settings are read into the new model automatically. The
camera becomes `default`; device IDs, technical names and enabled switches are
preserved. The next configuration save writes the new format. Existing keyring
entries continue to use camera origin and username, independent of camera IDs.

Socket, lock, token and PCM FIFOs live under
`$XDG_RUNTIME_DIR/hikvision-audio-bridge`. `XDG_RUNTIME_DIR` and a logged-in desktop
session are required. FIFOs carry audio in memory. HTTPS camera connections retain
certificate/hostname validation; use a trusted network for HTTP-only cameras.

```sh
systemctl --user enable hikvision-audio.service  # Optional login startup.
systemctl --user stop hikvision-audio.service
journalctl --user -u hikvision-audio.service
```

Restarting restores saved device switches, so disable microphones before stopping
if they must not resume. Unattended boot without an unlocked desktop keyring is
not supported. Shutdown cancels TTS, closes owned sessions and unloads local devices.
Session cleanup is shared and shielded from request cancellation; camera cleanup
runs concurrently, and cleanup errors remain distinct from stream errors.

`hikvision-audio cleanup` and the unit's `ExecStopPost` reclaim only module names
and FIFO paths owned by this runtime directory. Cleanup refuses to operate while
the instance lock is held. Force-killing cannot guarantee closing remote sessions;
firmware may retain ownership until timeout. Unknown remote sessions and unrelated
PipeWire modules are never closed or unloaded.

## Audio behavior and validation

Audio is narrowband 8 kHz mono: PCM16 locally and G.711 on the wire. One shared
20 ms routing clock reads each local speaker once and fans its samples out. Each
camera input is consumed once and shared among its microphone routes. Concurrent
non-silent inputs are averaged to avoid clipping. Live queues retain at most five
frames, dropping stale audio rather than replaying a backlog after a stall.

The microphone uses ISAPI `GET /audioData`; the speaker uses continuous
`PUT /audioData`. An initial empty upload HTTP 200 is only a handshake; later
HTTP/XML errors remain monitored. There is no software echo cancellation or hard
latency guarantee. Camera buffering, PipeWire scheduling and physical feedback
still affect duplex operation. Each camera's firmware must support this transport.

```sh
make check
uv build
```

The offline `unittest` suite covers strict/private settings and migration, real
loopback Digest/duplex streams, session ownership, isolated camera failures,
independent devices, fanout/mixing, bounded queues, stale-audio cleanup, cancellation,
TTS subprocess cleanup, and HTTP authentication/origin/body boundaries. Tests do not
contact physical cameras, access real credentials or modify desktop audio devices.
FFmpeg is required for the offline TTS decoding tests.

A separate end-to-end smoke exercised two loopback cameras, four real PipeWire
virtual devices, browser camera/password/device/route configuration, real Secret
Service entries, CLI access, playback fanout, 440/880 Hz microphone mixing, Edge
TTS broadcast/cancellation, and targeted recovery without restarting the other
camera. HTTPS startup from saved settings, anonymous-access rejection and plaintext
LAN refusal were also exercised. Shutdown removed temporary modules and credentials,
left system audio defaults unchanged, and left the existing user service running.
The built wheel was checked for all WebUI assets. This establishes local integration,
not physical-camera firmware compatibility.

A forced-exit recovery smoke created four named nodes with quoted/Unicode
descriptions in a runtime path containing spaces, killed the isolated daemon with
`SIGKILL`, then ran the real `cleanup` command. All four stale owned modules were
removed; an unrelated pipe module in the same directory and all pre-existing
modules remained. The existing service PID and default audio devices were unchanged.
The test-only sentinel module and temporary files were removed afterward.

## References

- [PipeWire pipe sink](https://docs.pipewire.org/page_pulse_module_pipe_sink.html)
- [PipeWire pipe source](https://docs.pipewire.org/page_pulse_module_pipe_source.html)
- [PipeWire Unix pipe tunnel](https://docs.pipewire.org/page_module_pipe_tunnel.html)
- [edge-tts](https://github.com/rany2/edge-tts)
- [Hikvision ISAPI Developer Guide, 2019 mirror](https://raw.githubusercontent.com/loozhengyuan/hikvision-sdk/master/resources/isapi.pdf),
  sections 8.3 and 15.10.187: open, concurrent PUT/GET audioData, close.
