# Three-song qualification

The frozen v1 qualification set is Circuit Bloom, Neon Tides, and Glass
Transit. The first two extend `electro_house/v2`; Glass Transit extends
`broken_pulse/v2`. The plan, pinned environment, repeat policy, and broad
performance guardrails live in `qualification/plan.json`.

On the supported macOS 14 lane with Python 3.11, Homebrew FFmpeg 8.1.2, and
LAME 3.100, one command rebuilds compile, render, and master outputs twice,
compares deterministic payload hashes, validates production/provenance/QA/MP3
decode evidence, and builds and validates the Circuit Bloom Ardour bundle:

```sh
songdna qualify --automated-only
```

The command writes `generated/qualification/ledger.json`, all three masters,
and these listening files:

- `generated/circuit_bloom/master/listening.mp3`
- `generated/neon_tides/master/listening.mp3`
- `generated/glass_transit/master/listening.mp3`

It also initializes `generated/qualification/listening-review.json` from the
committed template without overwriting an existing review. `--automated-only`
returns success with status `automated_pass_human_pending` while that review is
pending. This is the CI mode; it is not a musical signoff. A real reviewer must
record findings for headphones, speakers, mono, low volume, arrangement,
distinctness, balance/dynamics, artifacts/transitions, and translation, then
complete the named signoff. The full validator remains non-zero until that is
done:

```sh
songdna qualify --validate-only
```

Use `songdna qualify --validate-only --automated-only` to recheck only the
machine evidence without rebuilding audio. Missing files, changed bytes,
changed source inputs, role/node mismatches, stale provenance, failed QA, bad
decode evidence, repeat-hash drift, or performance-guardrail breaches fail
closed. Generated audio and ledgers remain ignored by Git.

The `ardour-handoff-smoke` CI job separately runs the existing pinned Ardour
save/reopen check. The qualification command validates the bundle and its
create-once/manual-update ownership contract; it does not duplicate a DAW.
The canonical qualification job uploads one seven-day
`songdna-qualification-listening` artifact containing the complete generated
evidence set, including the ledger, review worksheet, three master WAVs and
MP3s, compile/render/master manifests, stems, retained pre-masters, QA, and the
Circuit Bloom handoff. To sign off the exact CI run, check out the run's commit,
extract the artifact contents into `generated/`, complete the worksheet, and
run `songdna qualify --validate-only`. The committed source and contract hashes
in the ledger prevent validation against a different checkout.

## Add a song or style

For a new song in an existing style, copy a song directory, choose a new
`song.id` and seed, keep `extends` pointed at the versioned style, then author
its harmony, motif, form, source declarations, and matching `production.toml`.
Run `songdna validate`, `songdna inspect`, `songdna compile`, and `songdna
render` on the new file.

For a new style, add `styles/<name>/vN/style.toml`. It may extend one existing
versioned style and replace named defaults, patterns, roles, or sections. Point
the song's `extends` at it and ensure `production.role_map` covers the resolved
roles exactly. No compiler branch is needed. Add a focused composition test
when the new grammar behavior itself is new.

The three-song qualification plan is intentionally frozen evidence, not a
registry of every future song. Change it only when deliberately replacing the
reference set, and expect old ledgers and listening signoff to become stale.
