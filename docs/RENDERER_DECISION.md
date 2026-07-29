# Renderer decision: canonical v1 lane

SongDNA selects a hybrid adapter design. The canonical implementation is the
in-framework `builtin-deterministic-synth` backend; richer renderers may later
implement the same `songdna-renderer/v1` manifest contract, but are not needed
to compile, test, or render the reference songs.

| Lane | macOS/Linux headless | deterministic boundary | rights and pinning | offline speed/isolation | decision |
| --- | --- | --- | --- | --- | --- |
| In-framework synthesis | Python 3.11+; no GUI or system audio | byte-identical WAVs for the same Python/runtime inputs | repository code only, versioned palette and patch hashes | no process/plugin isolation required; conservative baseline speed | canonical |
| Pinned synth/plugin host | host availability and platform packaging vary | host, plugin and preset binaries all become the boundary | each binary/preset needs a redistributable license, checksum, and CVE/update policy | can be faster/richer, but requires subprocess isolation and failure handling | deferred optional backend |
| Hybrid adapter | canonical lane works everywhere above | each backend declares its own exact version boundary | optional backends must declare every binary, preset and asset | optional hosts are isolated; canonical remains CI-capable | selected architecture |

The canonical palette is original oscillator/noise synthesis, not sampled audio.
It has no third-party audio assets, codecs, plugins, or presets. Renderer code
uses the repository MIT license and Python's standard library. WAV is written
directly as uncompressed PCM, so there is no codec dependency.

`songdna render` stages all output in a sibling temporary directory and only
publishes it after stem, preview, and manifest checks pass. Missing role maps,
non-original mappings, incompatible sample rates, unmapped roles, invalid DSP
values, clipping, and CLI output paths outside `generated/` fail closed.

The manifest records adapter/backend/patch versions and hashes, exact frames,
sample rate/channels/bit depth, role provenance, asset/license inventory, WAV
hashes, and the measured render boundary. The preview/stem substrate executes
the declared deterministic pre-master graph: routing, gain/pan and filter
automation, kick-keyed sidechain ducking, and wet-only delay/reverb sends. It
does not do mastering, MP3 encoding, plugin discovery, or Ardour generation.
