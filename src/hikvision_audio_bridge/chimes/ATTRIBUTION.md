# Bundled status chimes

These WAV files are rendered from
[simple-notification-sounds](https://github.com/Stieneee/simple-notification-sounds)
by Tyler Stiene, commit `b518f14c651265f28d9dc9df1793a48294186d9e`, released under
the MIT License. The library synthesizes its sounds with Tone.js in a browser and
does not ship audio files. Its license text is preserved verbatim in
`LICENSE.txt`.

| Tone        | `short` | `medium` | `long`  |
| ----------- | ------- | -------- | ------- |
| `success`   | 0.508 s | 0.762 s  | 1.594 s |
| `attention` | 0.600 s | 0.849 s  | 1.454 s |
| `alert`     | 0.596 s | 0.630 s  | 1.315 s |
| `warning`   | 0.521 s | 0.772 s  | 2.169 s |
| `error`     | 0.447 s | 0.646 s  | 1.790 s |

Files were rendered once while authoring. The server never synthesizes, downloads
or decodes cues at runtime:

1. Install the library's locked dependencies with `npm ci` (`tone` 15.0.4,
   `standardized-audio-context` 25.3.77).
2. In Chromium, call each unmodified `src/sounds/<tone>.js` export with its
   duration inside `Tone.Offline(callback, 4, 1, 48000)`: a 4 s, mono, 48 kHz
   offline render.
3. Trim samples quieter than 0.1% of the clip peak (-60 dB) from both ends and
   keep a 10 ms tail.
4. Resample to 8 kHz with FFmpeg's soxr resampler, normalize each cue to a -3
   dBFS peak, and store mono PCM16 WAV:

```sh
ffmpeg -f f32le -ar 48000 -ac 1 -i trimmed.f32 \
  -af aresample=8000:resampler=soxr:precision=28 -f f32le -ac 1 cue.8k.f32
ffmpeg -f f32le -ar 8000 -ac 1 -i cue.8k.f32 -af volume=<gain>dB \
  -c:a pcm_s16le -map_metadata -1 -fflags +bitexact -flags:a +bitexact \
  <tone>-<duration>.wav
```

Each cue is peak-normalized to -3 dBFS so it stays audible on camera speakers
while leaving headroom. The library's own renders peak between about -5 and -12
dBFS, and resampling to 8 kHz changed those peaks by less than 1 dB; the
per-cue normalization therefore deliberately does not preserve the library's
relative loudness.
The long attention and warning cues contain random noise bursts; re-rendering
produces equivalent but not bit-identical files. Physical speaker volume and
audibility depend on the camera and its configuration.
