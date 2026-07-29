# Ardour handoff

## Import

1. Compile the song DNA.
2. Create a 48 kHz Ardour session beneath `production/<song-id>/`.
3. Import `generated/<song-id>/song.mid` as one track per MIDI track.
4. Confirm that Ardour reads the tempo and 4/4 meter from the conductor track.
5. Use `markers.csv` to recreate or verify arrangement locations.
6. Route each named role according to the song's `production.toml`.

The environment check performed during initial scaffolding did not find Ardour
or Surge XT on the command path. Installation and plugin discovery are therefore
production prerequisites, not assumed completed framework steps.

## Suggested routing

| Role | Production destination |
|---|---|
| Drum roles | Original synthesized drum rack or individual synth instances |
| `bass` | Monophonic Surge XT patch; separate sub and mid layers if desired |
| `harmony` | Surge XT chord/pluck patch |
| `lead` | Surge XT lead stack |
| `fx_trigger` | Original noise, impact, or transition generator |

Side-chain routing, patch design, resampling, automation, mixing, and mastering
belong in Ardour. Note identity, harmony, form, or section membership belongs in
SongDNA and should be recompiled.

## Drift rule

Never edit files under `generated/`. If an imported MIDI edit changes the
composition, express it in the song DNA or extend the framework vocabulary.
Ephemeral performance experiments may remain in Ardour, but the session notes
should identify them as production-only decisions.

