# SongDNA

SongDNA is a small, deterministic composition framework. A versioned style pack
defines reusable musical grammar; a song file extends that grammar with its own
motif, harmony, form, energy curve, and provenance declarations. Compilation
produces DAW-neutral MIDI and arrangement artifacts.

The framework is the source of truth for musical bones. Ardour sessions are a
production layer for instruments, performance, sound design, automation,
mixing, and mastering.

## Quick start

SongDNA has no runtime dependencies beyond Python 3.11, 3.12, or 3.13.

```sh
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/songdna validate songs/circuit_bloom/song.toml
.venv/bin/songdna inspect songs/neon_tides/song.toml
.venv/bin/songdna compile songs/circuit_bloom/song.toml
.venv/bin/songdna compile songs/neon_tides/song.toml
.venv/bin/songdna compile songs/glass_transit/song.toml
.venv/bin/songdna render songs/circuit_bloom/song.toml
.venv/bin/songdna render songs/neon_tides/song.toml --stems
.venv/bin/songdna master songs/circuit_bloom/song.toml
```

For editable development, replace `pip install .` with `pip install -e .`; then run:

```sh
python3 -m venv .venv
.venv/bin/python -m unittest discover -s tests -v
```

Generated outputs are written beneath `generated/<song-id>/`:

- `song.mid`: Type 1 standard MIDI, with a conductor track and one track per role.
- `markers.csv`: Section positions in bars, beats, and seconds.
- `arrangement.json`: Resolved structural report and per-role note counts.
- `rights.json`: Validated source declarations.
- `resolved.json`: Exact style and song inputs used by the compiler.
- `manifest.json`: Artifact sizes and SHA-256 digests.
- `render/`: exact-frame 48 kHz/24-bit WAV stems, a deterministic preview WAV,
  and `render-manifest.json` (from `songdna render`).
- `master/`: retained pre-master, 48 kHz/24-bit stereo master WAV, 320 kbps
  listening MP3, `qa.json`, readable `qa.md`, and `delivery-manifest.json`
  (from `songdna master`). The canonical mastering path requires FFmpeg 8.1.2
  and LAME 3.100; it fails closed if either is absent or a different version.

The manifest records the SongDNA compiler version plus Python implementation and
version. Builds are byte-identical when run with the same declared inputs and
toolchain. `validate` and `inspect` do not write artifacts; `compile` writes
only under `generated/<song-id>/`. Invalid DNA and missing inputs exit with code
2; argparse usage errors also use its standard non-zero exit status.

Generated and production directories are intentionally ignored by Git. Commit
the style pack, song DNA, production mapping, documentation, and tests. Add
audio separately with an explicit storage policy if the project later needs it.

## Extension contract

Every song declares a versioned style:

```toml
schema = "songdna-song/v2"
extends = "electro_house/v2"
```

Resolution is explicit and inspectable:

1. A style defines named patterns, roles, generic generators, and section-role defaults.
2. A style may replace declarations from one versioned parent; cycles and multiple parents are rejected.
3. The song supplies explicit tempo/meter and harmony maps, named motifs, form, and identity data.
4. A section may select pattern variations/fills, add or remove roles, and transform a motif.
5. The compiler rejects ambiguous positions, unknown declarations, unsafe events, or unsafe provenance.

Generated files must not be hand-edited. Compositionally important changes
made while experimenting in a DAW should be represented back in the song TOML
before the next compile.

## Original-source policy

Version 2 accepts only these declared origins:

- `original_composition`
- `original_midi`
- `original_synthesis`
- `self_recorded`

The compiler rejects external audio whenever `policy = "original_only"`.
This provides a reproducible provenance check, not a guarantee that no short
phrase could coincidentally resemble any music ever written.

`production.toml` is a v2 production-intent declaration, not a DAW session. It
names the matching song, session target, rights-clean role ownership, and a
declared portable pre-master graph. The canonical graph supports deterministic
routing, gain/pan, filter automation, kick-keyed sidechain ducking, and wet-only
delay/reverb sends; it does not load plugins or require a DAW project.

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the model and
[COMPOSITION_V2.md](docs/COMPOSITION_V2.md) for exact language semantics,
the breaking rebuild boundary, extension proof, and compile performance evidence.
[ARDOUR_HANDOFF.md](docs/ARDOUR_HANDOFF.md) for the production boundary.
See [RENDERER_DECISION.md](docs/RENDERER_DECISION.md) for the canonical
headless renderer, determinism boundary, license inventory, and optional-backend
policy.
