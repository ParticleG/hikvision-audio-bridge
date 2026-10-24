# Hikvision Audio Bridge

A standalone Linux user-session service that exposes a Hikvision intercom as a
PipeWire speaker and microphone. Ordinary applications can select these devices
in their audio settings. No kernel driver, root privileges, virtual video device,
cloud service or audio recording is involved.

## Requirements

- Linux with a logged-in systemd user session, PipeWire, `pipewire-pulse` and
  WirePlumber. The audio backend uses PipeWire's `module-pipe-sink` and
  `module-pipe-source`; native PulseAudio is not the supported target.
- `pactl` (`libpulse` on Arch Linux), Python 3.13 or newer, and `uv`.
- An unlocked desktop Secret Service, such as GNOME Keyring. Passwords are stored
  there, never in configuration, command arguments or environment variables.
- A camera supporting ISAPI two-way audio, Digest authentication and a selected
  channel configured for `G.711alaw` or `G.711ulaw`, 8 kHz mono. The bridge does not
  change camera configuration or displace another intercom owner's session.

The initial target is Arch Linux, PipeWire 1.6.9 and GNOME Keyring. The application
uses `audioop-lts` for G.711 conversion and PipeWire for application-side format
conversion. It does not need FFmpeg, GStreamer or the vendor's binary SDK.

## Install and run

From the project directory:

```sh
uv sync --frozen
make check
make install
systemctl --user start hikvision-audio.service
```

`make install` installs the CLI with `uv tool install`, installs the user unit and
reloads the user service manager. It does not start the service or enable login
startup. Ensure `~/.local/bin` is on your `PATH`. If the tool is already installed,
use `uv tool install --reinstall .` when updating it.
The unit uses systemd readiness notification: `start` returns after the local
control socket is ready, not after the camera has authenticated.

Configure a camera in an interactive terminal; the password prompt does not echo:

```sh
hikvision-audio configure --url http://camera.example.test --username admin --channel 1
hikvision-audio speaker on
hikvision-audio microphone on
hikvision-audio status
```

Select **Hikvision-Speaker** as the application's output and
**Hikvision-Microphone** as its input. Their stable technical names are
`hikvision_audio_speaker` and `hikvision_audio_microphone`. Neither is made the
system default automatically. The regular monitor source belonging to the speaker
is not the camera microphone.

To run from the checkout without installing:

```sh
uv run hikvision-audio serve
# In a second terminal, prefix the control commands with: uv run
```

Only one instance can own the user-session devices. The private Unix control
socket accepts the same Unix user only; there is no HTTP or TCP control listener.

## Live controls

```sh
# Each direction is independent. "off" removes that device from the system.
hikvision-audio speaker off
hikvision-audio microphone off
hikvision-audio speaker on
hikvision-audio microphone on

# Switch cameras without restarting the service or PipeWire.
# Omit --reuse-password to enter/store a new password for that origin and user.
hikvision-audio configure --url https://another-camera.example.test --username admin --channel 1 --reuse-password

# Retry after a network failure or after unlocking the desktop keyring.
hikvision-audio reconnect
hikvision-audio status
```

Enabling/disabling one direction does not reopen the other direction's active
session. Both directions share one camera/channel. Switching camera closes the
old audio streams and owned session before opening the new one, purges queued
samples, and retains enabled device names. There is a brief interruption while
connecting; this is not a seamless crossfade. Disabling both directions closes
the session. A successful configuration command means the local setting was
applied; connection attempts are asynchronous. Check `connection`, `error`,
`cleanup_error`, `sent_bytes` and `received_bytes` in `status`.

On authentication, network or device failure, the transport stops and the error
is reported in `status`; enabled virtual devices remain present but receive no
camera audio. There is no hidden fallback or automatic credential retry loop.
Use `reconnect` after addressing the cause, or turn the affected devices off.

Because disabling removes a device, an application may switch to another default
microphone/output or require you to select the virtual device again when enabling.
Use application-level mute when you want a device to remain selected instead.

## Service lifecycle and stored data

```sh
# Optional: restore the service when you log in.
systemctl --user enable hikvision-audio.service

systemctl --user stop hikvision-audio.service
journalctl --user -u hikvision-audio.service
```

Settings, including the selected camera and both enable switches, are saved to
`$XDG_CONFIG_HOME/hikvision-audio-bridge/settings.json` (normally under
`~/.config`) with mode `0600`. New installations start with both devices disabled.
A restart restores saved switches, so disable the microphone before stopping if
you do not want it to resume at the next start. An unlocked keyring is required
for connection; an unattended system boot without a desktop session is not the
supported deployment model.

The control socket and PCM FIFOs live under
`$XDG_RUNTIME_DIR/hikvision-audio-bridge`, in a `0700` directory. FIFOs transfer
samples in memory; no audio files are created. Credentials are Secret Service
items with application attribute `hikvision-audio-bridge`, keyed by camera origin
and username. No password option, stdin pipe or plaintext credential backend is
provided. HTTPS uses normal certificate and hostname validation; use a trusted
network for cameras restricted to HTTP.

Normal shutdown cancels both streams, attempts to close the owned camera session
with its own deadline, and unloads the virtual devices. Session cleanup runs in a
shared task shielded from control-request cancellation. Reconnect, camera changes
and subsequent stop requests wait for that task instead of cancelling an in-flight
close, even when cleanup began because a stream failed. The systemd unit runs
`hikvision-audio cleanup` after exit to reclaim local modules following a crash;
that command refuses to operate while an instance holds the service lock. The
next start also reclaims only modules using this service's exact FIFO paths.
Force-killing the process cannot guarantee closing a remote camera session;
firmware may retain it until timeout. Cleanup never closes an unknown remote
session or unloads unrelated audio modules.

## Audio behavior and limits

- Wire format: raw 8 kHz mono G.711; host-side device format: PCM16. This is
  narrowband intercom audio, not a high-fidelity music output.
- Speaker packets contain 20 ms of audio. Queues are bounded and old samples are
  dropped rather than replayed after a stall. Silence maintains an enabled
  speaker stream when no application is playing.
- The microphone uses ISAPI `GET /audioData`, not RTSP or a video stream. Support
  is model/firmware-dependent and must be checked on the target camera.
- The initial empty upload HTTP 200 is only a handshake. Terminal HTTP/XML errors
  during continuous playback are monitored rather than discarded.
- No software echo cancellation is added. Simultaneous speaker/microphone use can
  feed back; camera echo cancellation, physical placement, volume and PTZ muting
  affect the result. Only one intercom client may be permitted by the camera.
- Latency includes camera buffering, network transport and PipeWire scheduling.
  On the validation machine, a newly activated recording stream initially emitted
  approximately one second of silence before continuous audio. No hard latency
  guarantee is made.

## Validation

```sh
make check
uv build
```

The `unittest` suite uses real loopback HTTP/TCP peers for Digest authentication,
concurrent speaker/microphone streaming, late device errors, session ownership,
independent toggles, live camera changes and reconnect during in-flight cleanup.
Latch-based regressions also cover cancellation of a reconnect request while the
old session is closing. Normal tests do not change desktop audio devices, read
real credentials or contact a physical camera.

An end-to-end smoke run on the actual PipeWire instance additionally exercised the
real CLI/daemon, temporary Secret Service entries, `pacat` playback, `parec`
recording, node creation/removal, camera switching and shutdown. A first peer's
440 Hz microphone changed to a second peer's 880 Hz microphone. System default
input/output devices were unchanged, and temporary modules and test keyring items
were removed. This proves the local audio integration, not every camera firmware.

The installed systemd service was also exercised through start/restart and
immediate CLI access. A forced-process-exit smoke verified local module recovery,
and a cleanup request against a running instance was correctly refused. Physical
camera authentication still requires the operator to enter its password locally.

## Protocol references

- [PipeWire pipe sink](https://docs.pipewire.org/page_pulse_module_pipe_sink.html)
- [PipeWire pipe source](https://docs.pipewire.org/page_pulse_module_pipe_source.html)
- [PipeWire Unix pipe tunnel](https://docs.pipewire.org/page_module_pipe_tunnel.html)
- [Hikvision ISAPI Developer Guide, 2019 mirror](https://raw.githubusercontent.com/loozhengyuan/hikvision-sdk/master/resources/isapi.pdf),
  sections 8.3 and 15.10.187: open, concurrent PUT/GET audioData, close.
