from __future__ import annotations

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
from .transforms import transform_motif
from .validation import validate_production, validate_song, validate_style


STYLE_ID_PATTERN = re.compile(r"[a-z0-9_]+/v[0-9]+\Z")


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError(f"unable to load {path}: {exc}") from exc


def _beats_per_bar(numerator: int, denominator: int) -> float:
    return numerator * (4 / denominator)


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


def _add_note(
    arrangement: Arrangement,
    role: str,
    start_beat: float,
    duration_beats: float,
    pitch: int,
    velocity: int,
    channel: int,
) -> None:
    ticks = arrangement.ticks_per_beat
    note = Note(
        role=role,
        start=round(start_beat * ticks),
        duration=max(1, round(duration_beats * ticks)),
        pitch=pitch,
        velocity=velocity,
        channel=channel,
    )
    arrangement.notes_by_role.setdefault(role, []).append(note)


def _emit_fixed_note(
    arrangement: Arrangement,
    role_name: str,
    role: dict[str, Any],
    bar_start: float,
    beats_per_bar: float,
    energy: float,
    rng: random.Random,
    jitter: int,
) -> None:
    if energy < float(role.get("min_energy", 0)):
        return
    for offset in role.get("offsets", [0.0]):
        if float(offset) >= beats_per_bar:
            continue
        _add_note(
            arrangement,
            role_name,
            bar_start + float(offset),
            float(role.get("duration", 0.25)),
            int(role["note"]),
            _velocity(int(role["velocity"]), energy, jitter, rng),
            int(role["channel"]),
        )


def _emit_chord_pulse(
    arrangement: Arrangement,
    role_name: str,
    role: dict[str, Any],
    song: dict[str, Any],
    chord_degree: int,
    bar_start: float,
    beats_per_bar: float,
    energy: float,
    rng: random.Random,
    jitter: int,
) -> None:
    pitch = scale_pitch(
        song["song"]["tonic"], song["song"]["scale"], int(role["octave"]), chord_degree
    )
    for offset in role.get("offsets", [0.5, 1.5, 2.5, 3.5]):
        if float(offset) >= beats_per_bar:
            continue
        _add_note(
            arrangement,
            role_name,
            bar_start + float(offset),
            float(role.get("duration", 0.4)),
            pitch,
            _velocity(int(role["velocity"]), energy, jitter, rng),
            int(role["channel"]),
        )


def _emit_chord(
    arrangement: Arrangement,
    role_name: str,
    role: dict[str, Any],
    song: dict[str, Any],
    chord_degree: int,
    bar_start: float,
    beats_per_bar: float,
    energy: float,
    rng: random.Random,
    jitter: int,
) -> None:
    pitches = chord_pitches(
        song["song"]["tonic"],
        song["song"]["scale"],
        int(role["octave"]),
        chord_degree,
        int(role.get("voices", 3)),
    )
    offsets = role.get("offsets", [0.0])
    duration = float(role.get("duration", beats_per_bar))
    for offset in offsets:
        if float(offset) >= beats_per_bar:
            continue
        for pitch in pitches:
            _add_note(
                arrangement,
                role_name,
                bar_start + float(offset),
                min(duration, beats_per_bar - float(offset)),
                pitch,
                _velocity(int(role["velocity"]), energy, jitter, rng),
                int(role["channel"]),
            )


def _emit_motif(
    arrangement: Arrangement,
    role_name: str,
    role: dict[str, Any],
    song: dict[str, Any],
    section: dict[str, Any],
    section_start: float,
    section_beats: float,
    energy: float,
    rng: random.Random,
    jitter: int,
) -> None:
    transforms = [str(item) for item in section.get("transforms", [])]
    degrees, durations, octave_shift = transform_motif(
        [int(value) for value in song["identity"]["motif_degrees"]],
        [float(value) for value in song["identity"]["motif_durations"]],
        transforms,
    )
    cursor = 0.0
    index = 0
    gate = float(role.get("gate", 0.82))
    while cursor < section_beats:
        duration = durations[index % len(durations)]
        if cursor + duration > section_beats:
            break
        degree = degrees[index % len(degrees)]
        pitch = scale_pitch(
            song["song"]["tonic"],
            song["song"]["scale"],
            int(role["octave"]) + octave_shift,
            degree,
        )
        _add_note(
            arrangement,
            role_name,
            section_start + cursor,
            duration * gate,
            pitch,
            _velocity(int(role["velocity"]), energy, jitter, rng),
            int(role["channel"]),
        )
        cursor += duration
        index += 1


def build_arrangement(style: dict[str, Any], song: dict[str, Any]) -> Arrangement:
    validate_style(style)
    validate_song(song, style)

    defaults = style["defaults"]
    numerator = int(song["song"].get("meter_numerator", defaults["meter_numerator"]))
    denominator = int(song["song"].get("meter_denominator", defaults["meter_denominator"]))
    if denominator <= 0 or denominator & (denominator - 1):
        raise ValidationError("meter denominator must be a positive power of two")
    beats_per_bar = _beats_per_bar(numerator, denominator)
    rng = random.Random(int(song["song"]["seed"]))
    jitter = int(defaults.get("velocity_jitter", 0))
    total_bars = sum(int(section["bars"]) for section in song["form"])
    arrangement = Arrangement(
        song_id=song["song"]["id"],
        title=song["song"]["title"],
        tempo=float(song["song"]["tempo"]),
        meter_numerator=numerator,
        meter_denominator=denominator,
        ticks_per_beat=int(defaults["ticks_per_beat"]),
        total_bars=total_bars,
    )

    current_bar = 0
    chord_degrees = [int(value) for value in song["identity"]["chord_degrees"]]
    for section_index, section in enumerate(song["form"]):
        kind = section["kind"]
        bars = int(section["bars"])
        section_start_beat = current_bar * beats_per_bar
        arrangement.markers.append(
            Marker(round(section_start_beat * arrangement.ticks_per_beat), f"{section_index + 1:02d}_{kind}")
        )
        arrangement.sections.append(
            {
                "index": section_index + 1,
                "kind": kind,
                "start_bar": current_bar + 1,
                "bars": bars,
                "energy_start": float(section["energy_start"]),
                "energy_end": float(section["energy_end"]),
                "transforms": list(section.get("transforms", [])),
            }
        )
        section_roles = set(style["sections"][kind]["roles"])
        section_roles.update(section.get("add_roles", []))
        section_roles.difference_update(section.get("remove_roles", []))
        unknown_roles = section_roles - style["roles"].keys()
        if unknown_roles:
            raise ValidationError(f"section {kind} overrides unknown roles: {sorted(unknown_roles)}")

        for role_name in sorted(section_roles):
            role = style["roles"][role_name]
            generator = role["generator"]
            if generator == "motif":
                average_energy = (
                    float(section["energy_start"]) + float(section["energy_end"])
                ) / 2
                _emit_motif(
                    arrangement,
                    role_name,
                    role,
                    song,
                    section,
                    section_start_beat,
                    bars * beats_per_bar,
                    average_energy,
                    rng,
                    jitter,
                )
                continue

            for local_bar in range(bars):
                absolute_bar = current_bar + local_bar
                bar_start = absolute_bar * beats_per_bar
                energy = _section_energy(section, local_bar)
                chord_degree = chord_degrees[absolute_bar % len(chord_degrees)]
                if generator == "fixed_note":
                    _emit_fixed_note(
                        arrangement, role_name, role, bar_start, beats_per_bar, energy, rng, jitter
                    )
                elif generator == "chord_pulse":
                    _emit_chord_pulse(
                        arrangement,
                        role_name,
                        role,
                        song,
                        chord_degree,
                        bar_start,
                        beats_per_bar,
                        energy,
                        rng,
                        jitter,
                    )
                elif generator == "chord":
                    _emit_chord(
                        arrangement,
                        role_name,
                        role,
                        song,
                        chord_degree,
                        bar_start,
                        beats_per_bar,
                        energy,
                        rng,
                        jitter,
                    )
        current_bar += bars

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
    style_path = root_path / "styles" / extension / "style.toml"
    style = _load_toml(style_path)
    production_path = input_path.with_name("production.toml")
    production = _load_toml(production_path)
    validate_style(style)
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
    write_manifest(
        arrangement.song_id,
        [midi_path, marker_path, report_path, rights_path, resolved_path],
        manifest_path,
    )

    return CompileResult(
        song_id=arrangement.song_id,
        output_dir=output_dir,
        midi_path=midi_path,
        marker_path=marker_path,
        report_path=report_path,
        manifest_path=manifest_path,
        total_bars=arrangement.total_bars,
        total_notes=sum(len(notes) for notes in arrangement.notes_by_role.values()),
    )
