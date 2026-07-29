# Architecture

## Layers

SongDNA separates reusable grammar from song identity and production:

```text
core primitives -> versioned style -> song DNA -> generated interchange -> DAW production
```

### Core primitives

The Python package implements genre-neutral operations: diatonic pitch
resolution, chord construction, motif transformation, deterministic velocity
variation, arrangement expansion, validation, and MIDI/report export.

### Versioned style

`styles/electro_house/v1/style.toml` maps named musical roles to primitive
generators and declares which roles normally participate in each section kind.
It contains no melody, progression, audio, preset, or song title.

Changing existing style behavior can change every extending song. Breaking
behavior therefore requires a new style version rather than an in-place semantic
change.

### Song DNA

A song owns its tempo, tonal center, seed, motif, progression, form, energy
curves, transformations, and provenance. The DNA is compact because rhythmic
role grammar comes from the selected style.

### Generated interchange

Compilation is deterministic. MIDI, markers, reports, resolved inputs, and
hashes can be deleted and recreated. They are build artifacts, not authoring
surfaces.

### Production

The DAW session selects instruments and captures human decisions that cannot or
should not be reduced to the compositional grammar. `production.toml` records
the intended role mapping without making a particular plugin part of the song's
identity.

## Why data composition instead of class inheritance

The `extends` relationship is user-facing vocabulary. Internally, a song does
not inherit an opaque tree of mutable objects. It references one immutable,
versioned style and applies explicit section-level additions, removals, and
motif transformations. The fully resolved inputs are emitted for inspection.

## Determinism

The song seed initializes an isolated pseudorandom generator. The compiler uses
it only for bounded expressive variation. Identical source files and compiler
versions produce byte-identical output artifacts.

## Current boundaries

Version 1 deliberately supports one tempo and meter per song, diatonic harmony,
four generic generator primitives, and MIDI interchange. Future versions can
add tempo maps, borrowed chords, motif libraries local to a song, automation
lanes, alternate exporters, or additional style packs without putting synth
patches or rendered audio into the core representation.

