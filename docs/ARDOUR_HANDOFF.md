# Ardour handoff

SongDNA uses a self-validating interchange bundle plus a generated,
create-once Ardour Lua bootstrap. The bundle remains the inspectable source;
an Ardour session is never the only copy of composition or production intent.

The supported adapter is intentionally exact:

- Ardour `8.12.0~ds`, Debian package `1:8.12.0+ds-1`
- Debian base `debian@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd`
- `ardour8-new_session` and `ardour8-lua`
- no third-party plugins

Ardour's [Lua API is functional but explicitly subject to change](https://manual.ardour.org/lua-scripting/),
and its command-line Lua host does not expose the Editor's full import workflow.
Session templates preserve track configuration but not media, and generated
session XML would rely on undocumented internals. For those reasons this lane
does not claim automatic refresh, Ardour 9 compatibility, or automated media
import.

## Build and validate

```sh
songdna handoff songs/circuit_bloom/song.toml
songdna handoff-validate generated/circuit_bloom/ardour-handoff
./scripts/ardour_smoke.sh generated/circuit_bloom/ardour-handoff circuit_bloom
```

The handoff directory contains the MIDI conductor/role tracks, marker CSV,
mono WAV stems, compile/render/production/provenance manifests, exact relative
artifact hashes, and two generated Lua programs. Validation fails on an unsafe
path, missing file, digest change, role mismatch, stale production version, or
unsupported Ardour contract before Ardour is invoked.

The smoke script creates a fresh 48 kHz session in the pinned container, runs
the bootstrap once, saves, reopens, and verifies every generated name. Circuit
Bloom currently proves 9 tracks, 2 buses, 11 stereo route connections, and 7
range markers after reopen. Ardour stores locations in its beat-time domain;
the verifier permits at most 8 samples of conversion rounding at 48 kHz (the
Circuit Bloom smoke observed a maximum 5-sample delta, about 0.1 ms).

## Create the working session

The Docker smoke is the reproducible compatibility test. With the same Ardour
version installed locally, create a human-owned session and bootstrap it once:

```sh
ardour8-new_session -s 48000 -m 2 production/circuit_bloom circuit_bloom
ardour8-lua generated/circuit_bloom/ardour-handoff/ardour-bootstrap.lua \
  production/circuit_bloom circuit_bloom
```

The bootstrap requires the session name to equal the SongDNA song ID, requires
48 kHz, and refuses any session that already contains a non-master route. It
then creates stable `SDNA_<song>_role_<role>` audio tracks,
`SDNA_<song>_bus_<bus>` buses, declared routes, and arrangement range markers.
This fail-closed preflight prevents partial refreshes and duplicate objects.

Open the session in Ardour and do the remaining import in the GUI:

1. Import each `stems/<role>.wav` at session start into its matching generated
   role track.
2. Import `midi/song.mid` when editable note data or its conductor tempo/meter
   map is useful. Keep these MIDI tracks separate from the generated audio
   skeleton.
3. Compare the imported structure with `markers/markers.csv` and
   `metadata/arrangement.json`, then add instruments, plugins, automation,
   recordings, and production notes normally.

## Ownership and updates

| Material | Source of truth |
|---|---|
| Form, notes, harmony, tempo, meter | SongDNA source files |
| Bundle files and generated skeleton | Rebuild only; never hand-edit |
| Recordings, regions, plugins, automation, comments, production edits | Ardour session |

Do not run the bootstrap again on an existing working session. After changing
SongDNA, build a new bundle and inspect drift:

```sh
songdna handoff songs/circuit_bloom/song.toml
songdna handoff-drift generated/circuit_bloom/ardour-handoff \
  production/circuit_bloom
```

If the fingerprint changed, snapshot the Ardour session and manually replace
the affected imported MIDI/stem regions or copy the new skeleton into a fresh
session. Preserve human recordings, plugins, automation, comments, and other
production work. Musical or form edits first made in Ardour must be transcribed
back into SongDNA before rebuilding; they are not round-tripped automatically.
