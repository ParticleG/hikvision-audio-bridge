# Bundled status chimes

These WAV files are derived from the Kenney "Interface Sounds" 1.0 pack,
published under Creative Commons Zero (CC0 1.0):
<https://kenney.nl/assets/interface-sounds> and
<https://creativecommons.org/publicdomain/zero/1.0/>. The pack's original
`License.txt` is preserved here verbatim; attribution to Kenney is not
mandatory but is recorded for provenance.

| File | Original asset | Duration |
| --- | --- | --- |
| `success.wav` | `Audio/confirmation_001.ogg` | 0.290 s |
| `warning.wav` | `Audio/question_004.ogg` | 0.333 s |
| `error.wav` | `Audio/error_006.ogg` | 0.500 s |

Each cue was converted once while authoring, never at runtime, from the
official pack with FFmpeg 9:

```sh
ffmpeg -i Audio/confirmation_001.ogg -af volume=-5dB -ac 1 -ar 8000 \
  -c:a pcm_s16le -map_metadata -1 success.wav
```

The result is 8 kHz mono PCM16 WAV with peaks near -6 dBFS, leaving digital
headroom. `chimes.py` strips the WAV header and inserts 200 ms of silence
between the cue and synthesized speech. Physical speaker volume and audibility
depend on the camera and its configuration.
