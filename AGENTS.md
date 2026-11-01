# Repository Guidelines

## Project Overview

One Python distribution with independently deployed camera-server and PipeWire-desktop components. The server centralizes Hikvision ISAPI channel ownership, logical client sessions, live mixing and queued Edge TTS. Each desktop owns its camera groups, multiple virtual devices and a separate WebUI. Audio is narrowband two-way audio, not RTSP/video. Neither component records audio or changes camera configuration.

## Architecture and Ownership

Modules live in `src/hikvision_audio_bridge/`:

- `server.py: Gateway` owns server settings, scoped tokens, network sessions and `SpeechQueue`. `NetworkAudio` implements bounded PCM endpoints. Each WebSocket has an independent receive loop, ordered control worker and bounded sender; slow camera cleanup must not block unaffected PCM reception.
- `service.py: CameraEngine` owns one `CameraSession` per configured physical channel, independent speaker/microphone tasks and shielded cleanup. It accepts a `CameraCredentials` provider and `AudioIO`; it does not own PipeWire, persistence or a desktop keyring.
- `router.py: AudioRouter` fans speaker PCM out, mixes camera capture and overlays speech. TTS advances at camera upload pace and completes only after transport consumption. Live route changes must not discard speech progress or redirect job targets.
- `desktop.py: DesktopBridge` owns desktop settings, PipeWire lifecycle and one `remote.py: RemoteAudio` WebSocket. Groups expand/deduplicate into session-local routes. Identical device IDs on different desktops must never collide at the server.
- `web.py` serves `static/server/` or `static/desktop/`, sharing only `static/common.js` and CSS. Server admin, server audio and desktop UI credentials are separate. There is no JavaScript build step.
- `control.py` hosts independent desktop/server daemons and same-UID Unix control. `cli.py` provides `hikvision-audio` (`main`) and `hikvision-audio-server` (`server_main`).

Camera identity changes/reconnects finish old-session cleanup and flush affected buffers before replacement. Unrelated device changes, slow clients and individual camera failures must not restart healthy streams. Camera/backend errors require explicit reconnect; never add automatic credential retry loops.

Audio is PCM16 little-endian, 8 kHz mono. `audio.py` is authoritative for 20 ms / 320-byte frames, five-frame live buffers and the multiplexed WebSocket frame header. G.711 frames contain 160 bytes. Keep pacing, PipeWire format and wire framing consistent. Average active inputs to avoid clipping and discard stale live audio.

Speaker ISAPI uses continuous `PUT /audioData`; microphone uses `GET /audioData`. `httpx` handles Digest requests; upload uses raw asyncio sockets and `h11`. Its initial empty HTTP 200 is only a handshake: preserve monitoring of later HTTP/XML errors. Do not assume physical cameras support independent simultaneous owners or session-isolated channel close.

## Development Commands

Run from the repository root:

| Command | Purpose |
| --- | --- |
| `uv sync --frozen` | Install locked dependencies and dev tools; also `make sync`. |
| `make check` | Ruff format check, Ruff lint, `ty check src`, then unittest discovery. |
| `uv run ruff format .` | Apply Python formatting. |
| `uv build` | Build wheel/sdist into `dist/`; there is no `make build`. |
| `uv run hikvision-audio --help` | Inspect desktop commands without starting it. |
| `uv run hikvision-audio-server --help` | Inspect headless server commands. |
| `make install` | Install only the desktop CLI/user unit and reload systemd; does not start or enable it. |

For an installed desktop, update with `uv tool install --reinstall .`. The user unit expects `%h/.local/bin/hikvision-audio`. The server system unit expects `/opt/hikvision-audio/.venv/bin/hikvision-audio-server`, running as `hikvision-audio`. Both report readiness when control and WebUI listeners exist, not when cameras authenticate. Server CLI commands must use the service UID and matching state/runtime paths.

Do not restart an installed deployment merely to test checkout changes. Use isolated directories and unique PipeWire IDs; preserve audio defaults and unrelated modules. Detailed installation, authentication and migration instructions are in `README.md`.

## Code Conventions and Lifecycle

- Python 3.13+, modern annotations, package-relative imports, `snake_case`, `PascalCase` and uppercase constants. Ruff uses 88 columns and `E`, `F`, `I`, `UP`, `B`, `ASYNC`.
- Configuration uses frozen dataclasses and strict validation. `ServerSettings` persists cameras/web; `DesktopSettings` persists backend/groups/devices/web; `RoutingSettings` is internal expanded routing, never persisted. Reject unknown fields, non-boolean switches, invalid references and duplicate physical channels/routes.
- Preserve legacy desktop migration and stable device IDs/names/switches. The first desktop save backs up old non-secret settings privately. Never silently move camera passwords into desktop settings.
- Keep FIFO callbacks synchronous and nonblocking. Move blocking Secret Service/vault operations through `asyncio.to_thread` when needed. Dependencies are explicit; no DI framework.
- Preserve shared, shielded `CameraEngine` stop/close tasks, WebSocket cleanup and retained TTS tasks. Caller cancellation must not abandon camera close or provider/FFmpeg cleanup. Settle pending PipeWire enables before reconciliation; stop/pause affected streams before deleting devices.
- `SpeechQueue` permits one active job and ten waiting jobs. Cancel/stop must wait for cleanup; queued text is never included in status or persisted. Synthesis remains in `tts.py`; the engine only consumes PCM.
- Use `ValueError` for invalid control input and `CameraError` / `RuntimeError` for operational failures. Keep transport `error` distinct from `cleanup_error`; do not hide failed cleanup.

## Security and Runtime Boundaries

- Server state defaults to `/var/lib/hikvision-audio-server`, runtime to `/run/hikvision-audio-server`. `vault.py: ServerSecrets` stores Fernet-encrypted camera passwords and distinct persistent admin/audio tokens. Key/ciphertext files are owner-only; the service account and host/container root remain trusted. Losing the key must not silently regenerate over existing ciphertext.
- Desktop settings default to `$XDG_CONFIG_HOME/hikvision-audio-bridge/settings.json`, mode `0600`. Socket, lock and FIFOs live under `$XDG_RUNTIME_DIR/hikvision-audio-bridge`, mode `0700`; `XDG_RUNTIME_DIR` and a logged-in desktop are required. `credentials.py: DesktopCredentials` stores only backend audio tokens in Secret Service.
- Headless server operation must not require PipeWire, desktop Secret Service or login prompts. Desktop operation must never open ISAPI sessions or require camera passwords.
- Non-loopback HTTP listeners/backend URLs are refused unless `allow_insecure_lan` is explicitly true. That flag permits plaintext, not arbitrary trust: deployed LAN HTTP/WS must be firewall-restricted to the approved subnet. Retain bearer authentication, same-origin checks and HTTPS certificate/hostname validation.
- Management tokens, passwords and speech text must not appear in logs, command arguments or environment variables. The private runtime `web-access.json` is `0600`; login URLs and `audio-token` output are sensitive. Browser logout clears displayed secrets.
- Use `uv.lock` and Hatchling. `audioop-lts` supplies G.711 conversion. Keep `.venv/`, caches, bytecode and `dist/` out of source changes. FFmpeg is required for online TTS and offline decoding tests, not ordinary live routing.

## Testing and QA

Use stdlib `unittest`, including `IsolatedAsyncioTestCase`, not pytest:

```sh
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s tests -p 'test_network.py' -v
```

Use discovery: tests import `camera_peer` and `audio_helpers` as top-level helpers. `CameraPeer` serves real loopback Digest HTTP/TCP; network tests use actual WebSockets and replace only desktop hardware/credentials. Normal tests must not contact physical cameras, alter desktop audio or read real credentials.

Cover shared camera ownership, identical client device IDs, fanout/grouped capture, independent directions/routes, slow cleanup, explicit recovery, cancellation-safe close, TTS FIFO/cancel and role authorization. Prefer event/latch-based races over sleeps. For deterministic mixed-tone assertions use frequencies with whole periods per 20 ms frame; bounded stale-frame dropping can otherwise change long-window phase coherence. No coverage threshold or CI is configured; `make check` type-checks only `src/`.

Changed PipeWire, Secret Service, browser, LAN/TLS, TTS and systemd paths require separate runtime smoke checks. Real PipeWire volume/resampling means exact encoded-byte comparisons belong at the PCM/network boundary, not across arbitrary desktop processing. Check actual audio content, owned resource cleanup and preservation of existing services/default devices. Loopback peers cannot establish physical-camera firmware compatibility or physical audibility.
