# Architecture

## Layers

SongDNA separates reusable grammar from song identity and production:

```text
core primitives -> versioned style -> song DNA -> generated interchange -> DAW production
```

### Core primitives

The Python package implements genre-neutral operations: exact time maps,
scale and explicit chord resolution, pattern/motif transformation, deterministic
event variation, arrangement expansion, validation, and MIDI/report export.

### Versioned style

`styles/electro_house/v2/style.toml` maps named musical roles to generic
pattern, chord-pattern, and motif generators and declares which roles normally
participate in each section kind.
It contains no melody, progression, audio, preset, or song title.

Changing existing style behavior can change every extending song. Breaking
behavior therefore requires a new style version rather than an in-place semantic
change.

### Song DNA

A song owns tempo/meter maps, tonal center, seed, explicit harmony, named motifs,
form, energy curves, transformations, vocal-performance notes, and provenance.
The DNA is compact because rhythmic role grammar comes from the selected style.

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

The `extends` relationship is user-facing vocabulary. A song references one
immutable, versioned style. A style may reference one parent, with named child
declarations replacing parent declarations. Cycles and multiple parents are
invalid. The ordered lineage and fully resolved inputs are emitted for inspection.

## Determinism

The song seed and stable event coordinates initialize isolated pseudorandom
generators. The compiler uses them only for bounded probability and expressive
variation. Identical source files and compiler versions produce byte-identical
output artifacts.

## Current boundaries

Version 2 has exact tempo/meter maps, explicit and borrowed harmony, bounded
rational rhythms, named patterns/fills/motifs, expression metadata, one-parent
style composition, and MIDI interchange. It deliberately excludes synth patches,
effects, DAW automation, rendered audio, voice synthesis, and generative ML.
See `COMPOSITION_V2.md` for the normative boundary and rebuild guidance.
