# Repository Guidelines

## Project Overview

A Linux user-session service routing multiple Hikvision ISAPI intercoms through independently named PipeWire speaker and microphone devices, with a configuration WebUI and optional online Edge TTS. It uses narrowband two-way audio, not RTSP/video, and neither records audio nor changes camera configuration. No root privileges or vendor SDK are required.

## Architecture & Data Flow

Modules below live in `src/hikvision_audio_bridge/`:

```mermaid
flowchart LR
    CLI["cli.py"] -->|Unix socket / JSON| Control["control.py"]
    UI["static/ WebUI"] -->|HTTP(S) / JSON| Web["web.py"]
    Web --> Control
    Control --> Bridge["service.py: Bridge"]
    Bridge --> Router["router.py: AudioRouter"]
    Bridge --> Session["camera.py: one CameraSession per channel"]
    Apps["Applications"] <-->|PipeWire / PCM FIFOs| Devices["pipewire.py: AudioDevices"]
    Devices <-->|PCM16| Router
    Router <-->|PCM16| Session
    Bridge --> TTS["tts.py: Edge TTS / FFmpeg"]
    TTS -->|PCM16| Router
    Session <-->|ISAPI / G.711| Camera["Cameras"]
```

- `Bridge` owns settings and per-camera worker/stream/cleanup state. Devices have stable IDs, independent switches and many-to-many camera routes. Overlapping routes share sessions; unrelated device changes or camera failures must not restart healthy streams. Camera changes/reconnects finish owned-session cleanup and flush affected buffers before opening a replacement.
- Audio is 8 kHz mono: PCM16 locally, G.711 A-law or mu-law on the wire. Speaker frames are 20 ms: 320 PCM bytes become 160 encoded bytes. Keep frame size, PipeWire format and pacing consistent; bounded buffers discard stale audio.
- `AudioRouter` reads each local speaker once per 20 ms tick before fanout and consumes each camera input once before microphone mixing. Preserve bounded queues and deduplicated TTS camera targets. TTS completion tracks transport consumption, not merely enqueued frames.
- Speaker uses continuous `PUT /audioData`; microphone uses `GET /audioData`. `httpx` handles Digest-authenticated requests; speaker upload uses raw asyncio sockets and `h11`. Its initial empty HTTP 200 is only a handshake: preserve monitoring of later HTTP/XML errors.
- Unix control uses newline-delimited JSON, a single-instance lock and same-UID peer checks. The WebUI shares its serialized dispatcher, requires bearer-token authentication and same-origin requests, defaults to loopback and requires TLS for nonloopback binding. Camera failures leave enabled virtual devices present and report per-camera errors; recovery requires explicit reconnect rather than an automatic credential retry loop.

## Key Directories

| Directory | Purpose |
| --- | --- |
| `src/hikvision_audio_bridge/` | CLI, WebUI server/assets, routing, daemon orchestration, ISAPI transport, PipeWire, TTS and credentials. |
| `tests/` | Configuration, audio, lifecycle, TTS and HTTP security regressions with loopback peers. |
| `systemd/` | User-service unit and post-exit local-device cleanup. |

## Development Commands

Run from the repository root:

| Command | Purpose |
| --- | --- |
| `uv sync --frozen` | Install locked dependencies, including dev tools; also available as `make sync`. |
| `uv build` | Build wheel and source distribution into `dist/`; there is no `make build`. |
| `make check` | Ruff format check, Ruff lint, `ty check src`, then unittest discovery. |
| `uv run ruff format .` | Apply formatting. |
| `uv run ruff check .` | Run lint independently. |
| `uv run hikvision-audio --help` | Inspect commands without starting the service. |
| `uv run hikvision-audio serve` | Run the daemon in the foreground. |
| `uv run hikvision-audio status` | Inspect a running daemon from another terminal. |
| `uv run hikvision-audio web` | Open the authenticated WebUI for the running daemon. |
| `make install` | Install the CLI/user unit and reload systemd; does not start or enable the service. |

For an already installed CLI, update with `uv tool install --reinstall .`. The unit expects `%h/.local/bin/hikvision-audio`; start it explicitly with `systemctl --user start hikvision-audio.service`. Readiness means the control socket and WebUI listener are available, not that cameras authenticated. Do not restart a running deployment merely to test source changes; use isolated runtime/config paths and unique device IDs. Camera configuration, LAN/TLS and live-control examples are in `README.md`.

## Code Conventions & Common Patterns

- Ruff uses 88-column formatting and rules `E`, `F`, `I`, `UP`, `B`, `ASYNC`. Follow existing `snake_case` functions, `PascalCase` classes, uppercase constants, `_private_helpers`, package-relative imports and modern annotations (`X | None`, built-in generics).
- Configuration uses frozen dataclasses and strict `from_dict` validation. `Settings` contains camera/device maps and `WebSettings`; reject unknown keys, non-boolean switches, duplicate channels/routes and unknown camera references. Keep one-way legacy settings migration and the published default device identities working.
- Concurrency uses `asyncio` tasks, locks and events. Keep FIFO callbacks synchronous and nonblocking; move blocking Secret Service operations through `asyncio.to_thread`.
- Preserve shared, shielded cleanup in `Bridge._ensure_stop` / `_stop_state` / `_finish_camera`. Caller cancellation must not cancel session close. Settle pending PipeWire operations before reconciling devices, stop streams before removing their devices, and reclaim only matching owned module names/FIFO paths. Retain cancelled TTS tasks until provider/subprocess cleanup completes.
- Use `ValueError` for invalid input and `CameraError` / `RuntimeError` for operational failures. Preserve cancellation propagation and separate transport `error` from `cleanup_error`; do not hide failed cleanup.
- Dependencies are passed explicitly, without a DI framework: `Paths` selects storage, `Bridge` accepts credentials, and tests replace `Bridge.devices` with `MemoryDevices`.

## Important Files

- `pyproject.toml` and `uv.lock`: Python/dependency constraints, Hatchling packaging, tooling and console-script registration.
- `Makefile`: authoritative setup, QA and installation commands.
- `src/hikvision_audio_bridge/cli.py`: `hikvision-audio` entry point (`main`); command additions also affect validation/dispatch in `control.py`.
- `src/hikvision_audio_bridge/config.py`: `Camera`, `AudioDevice`, `Settings`, `WebSettings`, `Paths`, strict schemas, migration and atomic writes. `credentials.py` is the Secret Service boundary.
- `src/hikvision_audio_bridge/router.py`: 20 ms fanout/mixing and bounded speech/live buffers; `service.py` owns lifecycle and route reconciliation.
- `src/hikvision_audio_bridge/web.py` and `static/`: aiohttp server, token/origin/TLS boundary and packaged plain-JavaScript UI; no Node.js build step.
- `src/hikvision_audio_bridge/tts.py`: Edge MP3 synthesis and in-memory FFmpeg conversion, bounded input/output, cancellation-safe process ownership.
- `systemd/hikvision-audio.service`: readiness notification, PipeWire dependency and `ExecStopPost` cleanup.
- `README.md`: operational setup, supported audio behavior, manual validation notes and protocol references.

## Runtime/Tooling Preferences

- Use Python **3.13+** and **uv** with `uv.lock`; packaging uses Hatchling. `audioop-lts` supplies the G.711 conversion API removed from the standard library. Keep `.venv/`, caches, bytecode and `dist/` out of source changes.
- Real operation needs a logged-in Linux user session, PipeWire, `pipewire-pulse`, WirePlumber, `pactl` and an unlocked desktop Secret Service. TTS and its offline decoding tests also require FFmpeg; live synthesis needs network access. Native PulseAudio and unattended boot without a desktop keyring are not supported deployment targets.
- Settings live at `$XDG_CONFIG_HOME/hikvision-audio-bridge/settings.json`, mode `0600`. Socket, lock, PCM FIFOs and per-run `web-access.json` live under `$XDG_RUNTIME_DIR/hikvision-audio-bridge`, mode `0700`; the access file is `0600` and `XDG_RUNTIME_DIR` is required.
- Camera passwords belong only in Secret Service. Accept them through the interactive CLI or authenticated WebUI; never add them to settings, status, command arguments, environment variables or logs. WebUI credentials require TLS outside loopback. Camera HTTPS retains certificate/hostname validation. Do not log speech text or management tokens.

## Testing & QA

Use stdlib `unittest`, including `IsolatedAsyncioTestCase`, not pytest:

```sh
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s tests -p 'test_service.py' -v
```

Use discovery: tests import `camera_peer` as a top-level helper. `tests/camera_peer.py` serves real loopback HTTP/TCP with Digest authentication; service tests substitute only desktop audio and credentials. Normal tests must not contact physical cameras, alter desktop devices or read real credentials.

The suites cover strict/private configuration and migration, duplex audio, late upload errors, session ownership, independent devices, many-camera fanout/mixing, bounded queues, stale audio, targeted reconnect, cancellation, TTS conversion/cleanup and HTTP trust boundaries. Follow event/latch-based race tests rather than sleep-based timing. No coverage threshold or CI pipeline is configured; `make check` is the local QA gate and type-checks only `src/`.

PipeWire/FIFO integration, Secret Service prompts, WebUI browser interactions, TLS deployment, online TTS and systemd recovery need manual smoke checks when those paths change. The README records the separate real-device/loopback-peer smoke. No reusable end-to-end script is committed; loopback tests do not establish physical-camera firmware compatibility.
