from __future__ import annotations

import copy
from dataclasses import replace
from fractions import Fraction
import json
import random
import re
import tomllib
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .exporters.midi import write_midi
from .exporters.reports import (
    write_arrangement_report,
    write_manifest,
    write_markers,
    write_rights_report,
)
from .model import Arrangement, CompileResult, Marker, Note
from .theory import chord_pitches, scale_pitch
from .timing import build_timeline, fraction
from .transforms import transform_motif_events
from .validation import validate_production, validate_song, validate_style


STYLE_ID_PATTERN = re.compile(r"[a-z0-9_]+/v[0-9]+\Z")


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError(f"unable to load {path}: {exc}") from exc


def _style_document(raw: dict[str, Any], expected_id: str) -> None:
    if not isinstance(raw, dict):
        raise ValidationError("style document must be a table")
    allowed = {"schema", "id", "name", "extends", "defaults", "patterns", "roles", "sections"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValidationError(f"style contains unsupported fields: {', '.join(unknown)}")
    if raw.get("schema") != "songdna-style/v2":
        raise ValidationError(f"unsupported style schema: {raw.get('schema')}")
    if raw.get("id") != expected_id:
        raise ValidationError(f"style file for {expected_id!r} declares id {raw.get('id')!r}")
    parent = raw.get("extends")
    if parent is not None and (not isinstance(parent, str) or not STYLE_ID_PATTERN.fullmatch(parent)):
        raise ValidationError("style.extends must be one versioned parent style id")
    for key in ("defaults", "patterns", "roles", "sections"):
        if key in raw and not isinstance(raw[key], dict):
            raise ValidationError(f"style.{key} must be a table")


def resolve_style(root: Path | str, style_id: str, lineage: tuple[str, ...] = ()) -> dict[str, Any]:
    """Resolve one transparent parent chain; entry declarations replace by name."""
    if not STYLE_ID_PATTERN.fullmatch(style_id):
        raise ValidationError(f"invalid style id: {style_id!r}")
    if style_id in lineage:
        cycle = " -> ".join((*lineage, style_id))
        raise ValidationError(f"circular style inheritance: {cycle}")
    root_path = Path(root).resolve()
    raw = _load_toml(root_path / "styles" / style_id / "style.toml")
    _style_document(raw, style_id)
    parent_id = raw.get("extends")
    if parent_id:
        parent = resolve_style(root_path, parent_id, (*lineage, style_id))
        resolved: dict[str, Any] = {
            "schema": "songdna-style/v2",
            "id": style_id,
            "name": raw.get("name", style_id),
            "extends": parent_id,
            "lineage": [*parent.get("lineage", [parent_id]), style_id],
            "defaults": copy.deepcopy(parent["defaults"]),
            "patterns": copy.deepcopy(parent["patterns"]),
            "roles": copy.deepcopy(parent["roles"]),
            "sections": copy.deepcopy(parent["sections"]),
        }
        for key in ("defaults", "patterns", "roles", "sections"):
            resolved[key].update(copy.deepcopy(raw.get(key, {})))
    else:
        missing = [key for key in ("defaults", "patterns", "roles", "sections") if key not in raw]
        if missing:
            raise ValidationError(f"root style missing required fields: {', '.join(missing)}")
        resolved = copy.deepcopy(raw)
        resolved["lineage"] = [style_id]
    validate_style(resolved)
    return resolved


def _ticks(value: Fraction, arrangement: Arrangement, context: str) -> int:
    result = value * arrangement.ticks_per_beat
    if result.denominator != 1:
        raise ValidationError(f"{context} does not land on an integer tick")
    return result.numerator


def _velocity(base: int, energy: float, jitter: int, rng: random.Random) -> int:
    energy_scale = 0.72 + (0.28 * energy)
    return max(1, min(127, round(base * energy_scale) + rng.randint(-jitter, jitter)))


def _section_energy(section: dict[str, Any], bar: int) -> float:
    bars = int(section["bars"])
    if bars <= 1:
        return float(section["energy_end"])
    ratio = bar / (bars - 1)
    return float(section["energy_start"]) + ratio * (
        float(section["energy_end"]) - float(section["energy_start"])
    )


def _event_rng(seed: int, *coordinates: object) -> random.Random:
    return random.Random(":".join([str(seed), *(str(value) for value in coordinates)]))


def _add_note(
    arrangement: Arrangement,
    role_name: str,
    role: dict[str, Any],
    start: int,
    duration: int,
    pitch: int,
    velocity: int,
    articulation: str,
    phrase: str | None,
    tie: bool,
) -> None:
    if start < 0 or start >= arrangement.total_ticks:
        raise ValidationError(f"role {role_name} note begins outside the song: tick {start}")
    duration = min(duration, arrangement.total_ticks - start)
    if duration < 1:
        raise ValidationError(f"role {role_name} note has no representable duration")
    notes = arrangement.notes_by_role.setdefault(role_name, [])
    if tie:
        candidates = [
            (index, note) for index, note in enumerate(notes)
            if note.pitch == pitch and note.start + note.duration == start
        ]
        if not candidates:
            raise ValidationError(f"role {role_name} tie has no adjacent note with pitch {pitch}")
        index, previous = candidates[-1]
        notes[index] = replace(previous, duration=previous.duration + duration, articulation="legato")
        return
    note = Note(
        role=role_name, start=start, duration=duration, pitch=pitch,
        velocity=velocity, channel=int(role["channel"]), articulation=articulation,
        phrase=phrase,
    )
    notes.append(note)


def _resolved_harmony(song: dict[str, Any], arrangement: Arrangement) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    identity = song["identity"]
    for index, chord in enumerate(identity["harmony"]):
        tick = arrangement.position_to_tick(int(chord["bar"]), fraction(chord.get("beat", 1), f"identity.harmony[{index}].beat"))
        item = dict(chord)
        item.update({"tick": tick, "beat": str(chord.get("beat", 1))})
        resolved.append(item)
    if resolved[0]["tick"] != 0:
        raise ValidationError("identity.harmony must begin at bar 1 beat 1")
    if any(left["tick"] >= right["tick"] for left, right in zip(resolved, resolved[1:])):
        raise ValidationError("identity.harmony events must be strictly ordered and unique")
    return resolved


def _chord_at(arrangement: Arrangement, tick: int, song: dict[str, Any], octave: int) -> list[int]:
    chord = arrangement.harmony[0]
    for event in arrangement.harmony:
        if int(event["tick"]) > tick:
            break
        chord = event
    borrowed = chord.get("borrowed_from")
    return chord_pitches(
        song["identity"]["tonic"],
        borrowed or song["identity"]["scale"],
        octave,
        int(chord["degree"]) if "degree" in chord else None,
        root=chord.get("root"), quality=str(chord["quality"]),
        inversion=int(chord.get("inversion", 0)),
        extensions=[str(value) for value in chord.get("extensions", [])],
    )


def _emit_pattern(
    arrangement: Arrangement,
    role_name: str,
    role: dict[str, Any],
    pattern: dict[str, Any],
    song: dict[str, Any],
    bar: int,
    anchor_tick: int,
    energy: float,
    seed_coordinates: tuple[object, ...],
    jitter: int,
) -> None:
    if energy < float(role.get("min_energy", 0)):
        return
    bar_start = arrangement.bar_start_ticks[bar - 1]
    bar_end = arrangement.bar_start_ticks[bar]
    length = _ticks(fraction(pattern["length"], "pattern.length"), arrangement, "pattern.length")
    # Patterns run continuously from the section boundary. Selecting a fill
    # changes the declaration used in that bar, but does not reset its phase.
    cycle = max(0, (bar_start - anchor_tick) // length)
    while anchor_tick + cycle * length < bar_end:
        cycle_start = anchor_tick + cycle * length
        for event_index, event in enumerate(pattern["events"]):
            start = cycle_start + _ticks(fraction(event["at"], "pattern event.at", allow_zero=True), arrangement, "pattern event.at")
            if start < bar_start or start >= bar_end:
                continue
            rng = _event_rng(int(song["song"]["seed"]), *seed_coordinates, cycle, event_index)
            if rng.random() > float(event.get("probability", 1.0)):
                continue
            duration = _ticks(fraction(event["duration"], "pattern event.duration"), arrangement, "pattern event.duration")
            gate = float(event.get("gate", role.get("gate", 1.0)))
            duration = max(1, min(round(duration * gate), bar_end - start))
            base_velocity = int(event.get("velocity", role["velocity"])) + int(event.get("velocity_delta", 0))
            velocity = _velocity(max(1, min(127, base_velocity)), energy, jitter, rng)
            articulation = str(event.get("articulation", "normal"))
            if role["generator"] == "pattern":
                if role.get("pitch_mode", "fixed") == "fixed":
                    pitches = [int(event.get("note", role["note"]))]
                else:
                    pitches = [_chord_at(arrangement, start, song, int(role["octave"]))[0]]
            else:
                pitches = _chord_at(arrangement, start, song, int(role["octave"]))
                if "degree" in event:
                    pitches = [pitches[int(event["degree"]) % len(pitches)]]
            if event.get("rest"):
                continue
            for pitch in pitches:
                _add_note(arrangement, role_name, role, start, duration, pitch, velocity, articulation, None, bool(event.get("tie")))
        cycle += 1


def _emit_motif(
    arrangement: Arrangement,
    role_name: str,
    role: dict[str, Any],
    song: dict[str, Any],
    section: dict[str, Any],
    section_index: int,
    start_tick: int,
    end_tick: int,
    average_energy: float,
    jitter: int,
) -> None:
    motif_name = str(section.get("motif", next(iter(song["identity"]["motifs"]))))
    motif = song["identity"]["motifs"][motif_name]
    events, motif_length, octave_shift = transform_motif_events(
        motif["events"], fraction(motif["length"], f"motif {motif_name}.length"),
        [str(item) for item in section.get("transforms", [])],
    )
    length_ticks = _ticks(motif_length, arrangement, f"motif {motif_name}.length")
    phrase_ticks = sum(
        arrangement.bar_start_ticks[bar] - arrangement.bar_start_ticks[bar - 1]
        for bar in range(
            int(section["start_bar"]),
            min(
                arrangement.total_bars + 1,
                int(section["start_bar"]) + int(section.get("phrase_bars", section["bars"])),
            ),
        )
    ) or end_tick - start_tick
    cycle = 0
    while start_tick + cycle * length_ticks < end_tick:
        cycle_start = start_tick + cycle * length_ticks
        phrase_index = max(0, (cycle_start - start_tick) // max(1, phrase_ticks)) + 1
        phrase = f"{section_index:02d}_{motif_name}_p{phrase_index:02d}"
        for event_index, event in enumerate(events):
            event_start = cycle_start + _ticks(
                fraction(event["at"], f"motif {motif_name}.events[{event_index}].at", allow_zero=True, allow_negative=True),
                arrangement, f"motif {motif_name}.events[{event_index}].at",
            )
            if event_start >= end_tick:
                continue
            duration = min(
                _ticks(fraction(event["duration"], "motif event.duration"), arrangement, "motif event.duration"),
                end_tick - event_start,
            )
            if event.get("rest"):
                continue
            if "note" in event:
                pitch = int(event["note"])
            else:
                pitch = scale_pitch(
                    song["identity"]["tonic"], song["identity"]["scale"],
                    int(role["octave"]) + octave_shift, int(event.get("degree", 0)),
                )
            rng = _event_rng(int(song["song"]["seed"]), section_index, role_name, cycle, event_index)
            velocity = _velocity(
                int(event.get("velocity", role["velocity"])) + int(event.get("velocity_delta", 0)),
                average_energy, jitter, rng,
            )
            gate = float(event.get("gate", role.get("gate", 1.0)))
            articulation = str(event.get("articulation", "normal"))
            if articulation == "staccato":
                gate = min(gate, 0.5)
            elif articulation == "legato":
                gate = max(gate, 0.98)
            _add_note(
                arrangement, role_name, role, event_start, max(1, round(duration * gate)),
                pitch, velocity, articulation, phrase, bool(event.get("tie")),
            )
        cycle += 1


def _validate_resolved_notes(arrangement: Arrangement, style: dict[str, Any]) -> None:
    for role_name, notes in arrangement.notes_by_role.items():
        notes.sort(key=lambda note: (note.start, note.pitch, note.duration))
        for note in notes:
            if note.start < 0 or note.duration < 1 or note.start + note.duration > arrangement.total_ticks:
                raise ValidationError(f"role {role_name} resolved an unsafe note range")
            if not 0 <= note.pitch <= 127 or not 1 <= note.velocity <= 127 or not 0 <= note.channel <= 15:
                raise ValidationError(f"role {role_name} resolved unsafe MIDI data")
        if style["roles"][role_name].get("overlap") == "monophonic":
            for left, right in zip(notes, notes[1:]):
                if right.start < left.start + left.duration:
                    raise ValidationError(f"role {role_name} violates monophonic overlap policy")


def build_arrangement(style: dict[str, Any], song: dict[str, Any]) -> Arrangement:
    validate_style(style)
    validate_song(song, style)
    total_bars = sum(int(section["bars"]) for section in song["form"])
    starts, meter_map, tempo_map = build_timeline(song, style, total_bars)
    arrangement = Arrangement(
        song_id=str(song["song"]["id"]), title=str(song["song"]["title"]),
        ticks_per_beat=int(style["defaults"]["ticks_per_beat"]), total_bars=total_bars,
        bar_start_ticks=starts, tempo_map=tempo_map, meter_map=meter_map,
        style_lineage=tuple(style.get("lineage", [style["id"]])),
    )
    arrangement.harmony = _resolved_harmony(song, arrangement)
    if "vocals" in song:
        arrangement.vocals = copy.deepcopy(song["vocals"])
        for index, event in enumerate(arrangement.vocals["events"]):
            tick = arrangement.position_to_tick(
                int(event["bar"]), fraction(event["beat"], f"vocals.events[{index}].beat")
            )
            event["tick"] = tick
            event["seconds"] = arrangement.tick_to_seconds(tick)
    jitter = int(style["defaults"].get("velocity_jitter", 0))
    current_bar = 1
    for section_index, source_section in enumerate(song["form"], 1):
        section = dict(source_section)
        section["start_bar"] = current_bar
        kind = str(section["kind"])
        bars = int(section["bars"])
        start_tick = arrangement.bar_start_ticks[current_bar - 1]
        end_tick = arrangement.bar_start_ticks[current_bar + bars - 1]
        marker = Marker(start_tick, f"{section_index:02d}_{kind}", current_bar, Fraction(1))
        arrangement.markers.append(marker)
        arrangement.sections.append({
            "index": section_index, "kind": kind, "start_bar": current_bar, "bars": bars,
            "start_tick": start_tick, "end_tick": end_tick,
            "start_seconds": arrangement.tick_to_seconds(start_tick),
            "energy_start": float(section["energy_start"]), "energy_end": float(section["energy_end"]),
            "motif": section.get("motif"), "transforms": list(section.get("transforms", [])),
            "patterns": dict(section.get("patterns", {})),
            "fills": dict(section.get("fills", {})),
            "fill_every": section.get("fill_every"),
            "phrase_bars": section.get("phrase_bars"),
        })
        section_roles = set(style["sections"][kind]["roles"])
        section_roles.update(section.get("add_roles", []))
        section_roles.difference_update(section.get("remove_roles", []))
        patterns = dict(section.get("patterns", {}))
        fills = dict(section.get("fills", {}))
        fill_every = int(section.get("fill_every", bars + 1))
        for role_name in sorted(section_roles):
            role = style["roles"][role_name]
            if role["generator"] == "motif":
                _emit_motif(
                    arrangement, role_name, role, song, section, section_index, start_tick, end_tick,
                    (float(section["energy_start"]) + float(section["energy_end"])) / 2, jitter,
                )
                continue
            for local_bar in range(bars):
                absolute_bar = current_bar + local_bar
                pattern_name = str(patterns.get(role_name, role["pattern"]))
                if role_name in fills and (local_bar + 1) % fill_every == 0:
                    pattern_name = str(fills[role_name])
                _emit_pattern(
                    arrangement, role_name, role, style["patterns"][pattern_name], song,
                    absolute_bar, start_tick, _section_energy(section, local_bar),
                    (section_index, role_name, pattern_name), jitter,
                )
        current_bar += bars
    _validate_resolved_notes(arrangement, style)
    return arrangement


def load_inputs(song_path: Path | str, root: Path | str = ".") -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root_path = Path(root).resolve()
    input_path = Path(song_path)
    if not input_path.is_absolute():
        input_path = root_path / input_path
    song = _load_toml(input_path)
    extension = str(song.get("extends", ""))
    if not STYLE_ID_PATTERN.fullmatch(extension):
        raise ValidationError(f"invalid style id: {extension!r}")
    style = resolve_style(root_path, extension)
    production = _load_toml(input_path.with_name("production.toml"))
    validate_song(song, style)
    validate_production(production, song, style)
    return song, style, production


def compile_song(song_path: Path | str, root: Path | str = ".") -> CompileResult:
    root_path = Path(root).resolve()
    input_path = Path(song_path)
    if not input_path.is_absolute():
        input_path = root_path / input_path
    song, style, production = load_inputs(input_path, root_path)
    arrangement = build_arrangement(style, song)
    output_dir = root_path / "generated" / arrangement.song_id
    output_dir.mkdir(parents=True, exist_ok=True)
    midi_path = output_dir / "song.mid"
    marker_path = output_dir / "markers.csv"
    report_path = output_dir / "arrangement.json"
    rights_path = output_dir / "rights.json"
    resolved_path = output_dir / "resolved.json"
    manifest_path = output_dir / "manifest.json"
    write_midi(arrangement, midi_path)
    write_markers(arrangement, marker_path)
    write_arrangement_report(arrangement, report_path)
    write_rights_report(song, rights_path)
    resolved_path.write_text(
        json.dumps({"style": style, "song": song, "production": production}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_manifest(arrangement.song_id, [midi_path, marker_path, report_path, rights_path, resolved_path], manifest_path)
    return CompileResult(
        song_id=arrangement.song_id, output_dir=output_dir, midi_path=midi_path,
        marker_path=marker_path, report_path=report_path, manifest_path=manifest_path,
        total_bars=arrangement.total_bars,
        total_notes=sum(len(notes) for notes in arrangement.notes_by_role.values()),
    )
