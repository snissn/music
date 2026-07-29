# SongDNA v2 composition decision record

Status: accepted for the pre-alpha tree on 2026-07-29. This is a breaking
language boundary, not an in-place extension of v1.

## Why v2 exists

The v1 prototype proved deterministic style extension, but it had one tempo and
meter, an implicit diatonic triad progression, fixed-offset rhythm embedded in
role declarations, one positional motif, and no style composition. It could not
describe meter-relative positions, non-diatonic harmony, named fills, pickups,
ties, phrase intent, vocal notes, or another style without stretching those
small primitives.

The executable target is `songs/glass_transit/song.toml`, with its compact
expected resolution in `tests/fixtures/v2_target/resolved-arrangement.json`.
It combines tempo and meter changes, an explicit borrowed chord, a seeded fill,
a transformed named motif, bounded tuplets, a pickup, rests, expression, and
optional vocal-performance metadata.

## Normative semantics

### Version and identity

- A song declares `songdna-song/v2` and exactly one versioned style ID.
- A style declares `songdna-style/v2`. A child may name exactly one parent.
  Named defaults, patterns, roles, and sections in the child replace declarations
  with the same name. The compiler emits the full merge and ordered lineage in
  `resolved.json`; cycles and missing declarations fail closed.
- A song ID is metadata. Compiler behavior never branches on song or style IDs.
- Song DNA owns time, pitch, harmony, rhythm, form, motifs, articulation, and
  vocal-performance notes. Production DNA continues to own synths, effects,
  routing, automation, rendering, and mastering.

### Time

- Bars and beats are one-based. A meter change occurs only at the first beat of
  its declared bar. A tempo change may occur on any meter-relative beat.
- A beat position is measured in the active meter's denominator unit. Thus beat
  3 in 5/4 is two quarter notes after the bar boundary, while beat 3 in 7/8 is
  one quarter note after it.
- Pattern/motif offsets and durations are quarter-note units represented by an
  integer or rational string. Denominators are bounded at 16,
  and every value must land on an integer tick at the style PPQ. This explicitly
  supports common tuplets without an unbounded rhythmic expression language.
- Pattern offsets are non-negative. Motif offsets may be negative to express a
  pickup, but the resolved note must remain within the song.
- MIDI tempo is integer microseconds per quarter note. Tick-to-second conversion
  uses those same quantized conductor values, so MIDI, marker CSV, arrangement
  reports, render frame boundaries, and production automation agree.

### Pitch and harmony

- Motifs remain scale-degree authored, but a note may override its exact MIDI
  pitch as a deliberate escape hatch.
- Harmony is an ordered event map beginning at bar 1 beat 1. An event chooses
  exactly one scale degree or explicit pitch-class root, plus quality, optional
  inversion/extensions, and optional borrowed scale. It remains active until
  the next event.
- Unsupported roots, qualities, extensions, inversions, and out-of-range MIDI
  results fail closed. No style is allowed to smuggle a synth or effect setting
  into these declarations.

### Patterns, motifs, and expression

- Styles define named patterns; roles use the generic `pattern`,
  `chord_pattern`, or `motif` generator. Sections select pattern variations and
  deterministic fills by name. Pattern phase is continuous from the section
  boundary, including across odd-meter bars and for patterns longer than a bar.
  Event probability and velocity jitter use the
  song seed plus stable event coordinates, so unrelated role additions do not
  perturb an existing stream.
- Song-local named motifs contain positioned events. Transformations operate on
  those declarations, and generated notes carry phrase IDs. Events may be rests,
  ties, or notes with articulation, velocity, gate, scale degree, or exact pitch.
- Each role declares `monophonic` or `polyphonic` overlap behavior. Resolved
  ordering, duration, channel, velocity, pitch, and song bounds are validated.
- Vocal metadata is timing/text/performance intent only; v2 does not synthesize
  a voice.

## Rebuilding v1 work

There is intentionally no migration runtime. The checked-in v1 style was removed
and both original song files were ported to v2. To port an external pre-alpha v1
file, select a v2 style, move tempo/meter into explicit maps, replace progression
degrees with explicit harmony events, convert the positional motif into a named
event list, then delete and rebuild `generated/<song-id>/`. Old generated data is
not an authoring source and should not be opened as v2.

## Extension proof

`broken_pulse/v2` inherits the base vocabulary but replaces the kick, backbeat,
hat, bass, harmony, FX, role, and section grammar with syncopated declarations.
`glass_transit` is an original third song using it. Tests scan generic compiler
source for both IDs and compile the song, making a name-keyed implementation a
contract failure.

## Compile performance evidence

Command:

```sh
PYTHONPATH=src python3 benchmarks/compile_v2.py
```

Captured on Darwin 25.2.0 arm64 with Python 3.12.9. Peak RSS is the process
high-water mark. Artifact bytes include the six compiler output files.

| song | bars | events | wall seconds | peak RSS | artifact bytes |
|---|---:|---:|---:|---:|---:|
| glass_transit | 24 | 582 | 0.017692 | 26,214,400 | 152,153 |
| neon_tides | 120 | 4,161 | 0.066068 | 33,669,120 | 929,742 |
| circuit_bloom | 144 | 5,360 | 0.084773 | 36,061,184 | 1,193,140 |

The executable guardrail caps each compile at 5 seconds, 64 events per bar,
256 MiB peak RSS, and `1,000,000 + 1,024 * events` output bytes. The long fixture
has over eight times the events of the short fixture while staying well inside
all bounds; there is no unexplained material scaling regression in this baseline.
