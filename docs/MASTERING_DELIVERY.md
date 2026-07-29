# Mastering and delivery lane

`songdna master songs/<id>/song.toml` consumes the renderer's stereo
`generated/<id>/render/preview.wav` and atomically publishes `master/` only
after every required stage succeeds. It retains the exact pre-master, makes a
48 kHz/24-bit stereo WAV master, creates a 320 kbps listening MP3, decodes that
MP3 again, and writes both machine-readable and readable QA.

The delivery policy lives in each song's `production.toml`: v1 targets -14
LUFS +/-1 LU, has a strict -1 dBTP ceiling (the processing stage targets -1.2
dBTP for measurement safety), declares 50 ms end fades, explicitly
keeps dither `none` because the canonical PCM path remains 24-bit, and uses
`mp3/lame-cbr` at its declared bitrate. A failed
format, silent or clipped pre-master, unavailable stage, meter breach, invalid
sample, DC breach, or MP3 decode failure publishes no releaseable delivery.

## Supported canonical environment

The current canonical lane is Homebrew FFmpeg **8.1.2** built with GPL and
libmp3lame, plus LAME **3.100**. Their paths can be explicitly set with
`SONGDNA_FFMPEG` and `SONGDNA_LAME`; their versions are verified at runtime
and recorded in the delivery manifest. The binary checksum is not yet pinned,
so byte-identical MP3 is only claimed within the recorded same-environment
boundary. GitHub Actions runs the actual two-reference-song pipeline on
macOS-14 after installing and verifying these versions; it is not an optional
or mocked CI lane.

FFmpeg's `loudnorm` filter performs the declared integrated-loudness and
true-peak mastering stage and the corresponding QA measurement. LAME performs
the listening encode. FFmpeg is GPL-2.0-or-later in this configured build;
LAME is LGPL-2.0-or-later. No proprietary plugins or fallback encoders are in
the canonical path.

Automated QA is a delivery gate, not a musical-quality judgment. Human
listening and translation remain outside this issue's scope.
