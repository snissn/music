from __future__ import annotations

import math
import re
from typing import Any

from .errors import ValidationError
from .theory import CHORD_EXTENSIONS, CHORD_QUALITIES, NOTE_CLASSES, SCALES
from .timing import fraction
from .transforms import transform_motif


ALLOWED_ORIGINS = {"original_composition", "original_midi", "original_synthesis", "self_recorded"}
GENERATORS = {"pattern", "chord_pattern", "motif"}
METER_DENOMINATORS = {1, 2, 4, 8, 16, 32}
STYLE_ID_PATTERN = re.compile(r"[a-z0-9_]+/v[0-9]+\Z")
SONG_ID_PATTERN = re.compile(r"[A-Za-z0-9_]+\Z")


def _require(mapping: dict[str, Any], keys: set[str], context: str) -> None:
    if not isinstance(mapping, dict):
        raise ValidationError(f"{context} must be a table")
    missing = sorted(keys - mapping.keys())
    if missing:
        raise ValidationError(f"{context} missing required fields: {', '.join(missing)}")


def _only(mapping: dict[str, Any], keys: set[str], context: str) -> None:
    unknown = sorted(set(mapping) - keys)
    if unknown:
        raise ValidationError(f"{context} contains unsupported fields: {', '.join(unknown)}")


def _integer(value: Any, context: str, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{context} must be an integer")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValidationError(f"{context} must be between {minimum} and {maximum}")
    return value


def _positive_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValidationError(f"{context} must be a positive number")
    return float(value)


def _nonempty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context} must be a non-empty string")
    return value


def _unit_interval(value: Any, context: str, *, exclusive_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{context} must be a finite number")
    lower_ok = value > 0 if exclusive_zero else value >= 0
    if not lower_ok or value > 1:
        qualifier = "between 0 (exclusive) and 1" if exclusive_zero else "between 0 and 1"
        raise ValidationError(f"{context} must be {qualifier}")
    return float(value)


def _validate_event(event: Any, context: str, *, motif: bool) -> None:
    if not isinstance(event, dict):
        raise ValidationError(f"{context} must be a table")
    allowed = {"at", "duration", "note", "degree", "velocity", "velocity_delta", "probability", "gate", "articulation", "tie", "rest"}
    _only(event, allowed, context)
    _require(event, {"at", "duration"}, context)
    fraction(event["at"], f"{context}.at", allow_zero=True, allow_negative=motif)
    fraction(event["duration"], f"{context}.duration")
    if "note" in event:
        _integer(event["note"], f"{context}.note", 0, 127)
    if "degree" in event:
        _integer(event["degree"], f"{context}.degree")
    if "velocity" in event:
        _integer(event["velocity"], f"{context}.velocity", 1, 127)
    if "velocity_delta" in event:
        _integer(event["velocity_delta"], f"{context}.velocity_delta", -126, 126)
    if "probability" in event:
        _unit_interval(event["probability"], f"{context}.probability")
    if "gate" in event:
        _unit_interval(event["gate"], f"{context}.gate", exclusive_zero=True)
    if "articulation" in event and event["articulation"] not in {"normal", "accent", "staccato", "legato"}:
        raise ValidationError(f"{context}.articulation is unsupported")
    for flag in ("tie", "rest"):
        if flag in event and not isinstance(event[flag], bool):
            raise ValidationError(f"{context}.{flag} must be a boolean")
    if event.get("rest") and ({"note", "degree"} & set(event)):
        raise ValidationError(f"{context} rest cannot declare pitch")
    if event.get("tie") and event.get("rest"):
        raise ValidationError(f"{context} cannot be both a tie and rest")


def _validate_role(role: dict[str, Any], name: str, patterns: set[str]) -> None:
    _require(role, {"generator", "channel", "velocity"}, f"role {name}")
    _only(role, {"generator", "channel", "velocity", "pattern", "note", "octave", "gate", "min_energy", "pitch_mode", "overlap"}, f"role {name}")
    generator = role["generator"]
    if not isinstance(generator, str) or generator not in GENERATORS:
        raise ValidationError(f"role {name} uses unknown generator {generator!r}")
    _integer(role["channel"], f"role {name}.channel", 0, 15)
    _integer(role["velocity"], f"role {name}.velocity", 1, 127)
    if "gate" in role:
        _unit_interval(role["gate"], f"role {name}.gate", exclusive_zero=True)
    if "min_energy" in role:
        _unit_interval(role["min_energy"], f"role {name}.min_energy")
    if role.get("overlap", "polyphonic") not in {"polyphonic", "monophonic"}:
        raise ValidationError(f"role {name}.overlap must be polyphonic or monophonic")
    if generator in {"pattern", "chord_pattern"}:
        pattern = _nonempty_string(role.get("pattern"), f"role {name}.pattern")
        if pattern not in patterns:
            raise ValidationError(f"role {name} references unknown pattern {pattern}")
    if generator == "pattern":
        mode = role.get("pitch_mode", "fixed")
        if mode not in {"fixed", "chord_root"}:
            raise ValidationError(f"role {name}.pitch_mode is unsupported")
        if mode == "fixed":
            _integer(role.get("note"), f"role {name}.note", 0, 127)
        else:
            _integer(role.get("octave"), f"role {name}.octave", -1, 9)
    elif generator in {"chord_pattern", "motif"}:
        _integer(role.get("octave"), f"role {name}.octave", -1, 9)


def validate_style(style: dict[str, Any]) -> None:
    _require(style, {"schema", "id", "defaults", "patterns", "roles", "sections"}, "style")
    _only(style, {"schema", "id", "name", "extends", "lineage", "defaults", "patterns", "roles", "sections"}, "style")
    if style["schema"] != "songdna-style/v2":
        raise ValidationError(f"unsupported style schema: {style['schema']}")
    if not isinstance(style["id"], str) or not STYLE_ID_PATTERN.fullmatch(style["id"]):
        raise ValidationError("style.id must be a versioned style identifier")
    if "name" in style:
        _nonempty_string(style["name"], "style.name")
    defaults = style["defaults"]
    _require(defaults, {"ticks_per_beat"}, "style.defaults")
    _only(defaults, {"ticks_per_beat", "velocity_jitter"}, "style.defaults")
    _integer(defaults["ticks_per_beat"], "style.defaults.ticks_per_beat", 1)
    if "velocity_jitter" in defaults:
        _integer(defaults["velocity_jitter"], "style.defaults.velocity_jitter", 0)

    if not isinstance(style["patterns"], dict) or not style["patterns"]:
        raise ValidationError("style.patterns must be a non-empty table")
    for name, pattern in style["patterns"].items():
        _require(pattern, {"length", "events"}, f"pattern {name}")
        _only(pattern, {"length", "events"}, f"pattern {name}")
        length = fraction(pattern["length"], f"pattern {name}.length")
        if not isinstance(pattern["events"], list) or not pattern["events"]:
            raise ValidationError(f"pattern {name}.events must be a non-empty list")
        for index, event in enumerate(pattern["events"]):
            _validate_event(event, f"pattern {name}.events[{index}]", motif=False)
            if fraction(event["at"], f"pattern {name}.events[{index}].at", allow_zero=True) >= length:
                raise ValidationError(f"pattern {name}.events[{index}] begins outside its pattern")
    if not isinstance(style["roles"], dict) or not style["roles"]:
        raise ValidationError("style.roles must be a non-empty table")
    if not isinstance(style["sections"], dict) or not style["sections"]:
        raise ValidationError("style.sections must be a non-empty table")
    for name, role in style["roles"].items():
        _validate_role(role, name, set(style["patterns"]))

    role_names = set(style["roles"])
    for name, section in style["sections"].items():
        _require(section, {"roles"}, f"section {name}")
        _only(section, {"roles"}, f"section {name}")
        if not isinstance(section["roles"], list):
            raise ValidationError(f"section {name}.roles must be a list")
        if not all(isinstance(role, str) for role in section["roles"]):
            raise ValidationError(f"section {name}.roles must be a list of strings")
        unknown = set(section["roles"]) - role_names
        if unknown:
            raise ValidationError(f"section {name} references unknown roles: {sorted(unknown)}")


def validate_song(song: dict[str, Any], style: dict[str, Any]) -> None:
    _require(song, {"schema", "extends", "song", "timeline", "identity", "form", "sources"}, "song DNA")
    _only(song, {"schema", "extends", "song", "timeline", "identity", "form", "sources", "vocals"}, "song DNA")
    if song["schema"] != "songdna-song/v2":
        raise ValidationError(f"unsupported song schema: {song['schema']}")
    if not isinstance(song["extends"], str) or not STYLE_ID_PATTERN.fullmatch(song["extends"]):
        raise ValidationError("song.extends must be a versioned style identifier")
    if song["extends"] != style["id"]:
        raise ValidationError(
            f"song extends {song['extends']!r}, but resolved style id is {style['id']!r}"
        )

    metadata = song["song"]
    _require(metadata, {"id", "title", "seed"}, "song")
    _only(metadata, {"id", "title", "seed"}, "song")
    if not isinstance(metadata["id"], str) or not SONG_ID_PATTERN.fullmatch(metadata["id"]):
        raise ValidationError("song id must contain only letters, digits, and underscores")
    _nonempty_string(metadata["title"], "song.title")
    _integer(metadata["seed"], "song.seed")

    timeline = song["timeline"]
    _require(timeline, {"tempo", "meter"}, "timeline")
    _only(timeline, {"tempo", "meter"}, "timeline")
    if not isinstance(timeline["tempo"], list) or not timeline["tempo"]:
        raise ValidationError("timeline.tempo must be a non-empty list")
    if not isinstance(timeline["meter"], list) or not timeline["meter"]:
        raise ValidationError("timeline.meter must be a non-empty list")
    for index, event in enumerate(timeline["tempo"]):
        _require(event, {"bar", "bpm"}, f"timeline.tempo[{index}]")
        _only(event, {"bar", "beat", "bpm"}, f"timeline.tempo[{index}]")
        _integer(event["bar"], f"timeline.tempo[{index}].bar", 1)
        fraction(event.get("beat", 1), f"timeline.tempo[{index}].beat")
        if isinstance(event["bpm"], bool) or not isinstance(event["bpm"], (int, float)) or not 30 <= event["bpm"] <= 300:
            raise ValidationError(f"timeline.tempo[{index}].bpm must be between 30 and 300")
    for index, event in enumerate(timeline["meter"]):
        _require(event, {"bar", "numerator", "denominator"}, f"timeline.meter[{index}]")
        _only(event, {"bar", "numerator", "denominator"}, f"timeline.meter[{index}]")
        _integer(event["bar"], f"timeline.meter[{index}].bar", 1)
        _integer(event["numerator"], f"timeline.meter[{index}].numerator", 1, 32)
        if _integer(event["denominator"], f"timeline.meter[{index}].denominator", 1) not in METER_DENOMINATORS:
            raise ValidationError(f"timeline.meter[{index}].denominator is unsupported")
    if any(left["bar"] >= right["bar"] for left, right in zip(timeline["meter"], timeline["meter"][1:])):
        raise ValidationError("timeline.meter events must be strictly ordered and unique")

    identity = song["identity"]
    _require(identity, {"tonic", "scale", "harmony", "motifs"}, "identity")
    _only(identity, {"tonic", "scale", "harmony", "motifs"}, "identity")
    if identity["tonic"] not in NOTE_CLASSES:
        raise ValidationError(f"unknown tonic: {identity['tonic']}")
    if identity["scale"] not in SCALES:
        raise ValidationError(f"unknown scale: {identity['scale']}")
    harmony = identity["harmony"]
    if not isinstance(harmony, list) or not harmony:
        raise ValidationError("identity.harmony must be a non-empty list")
    for index, chord in enumerate(harmony):
        _require(chord, {"bar", "quality"}, f"identity.harmony[{index}]")
        _only(chord, {"bar", "beat", "degree", "root", "quality", "inversion", "extensions", "borrowed_from"}, f"identity.harmony[{index}]")
        _integer(chord["bar"], f"identity.harmony[{index}].bar", 1)
        fraction(chord.get("beat", 1), f"identity.harmony[{index}].beat")
        if ("degree" in chord) == ("root" in chord):
            raise ValidationError(f"identity.harmony[{index}] requires exactly one of degree or root")
        if "degree" in chord:
            _integer(chord["degree"], f"identity.harmony[{index}].degree")
        elif chord["root"] not in NOTE_CLASSES:
            raise ValidationError(f"identity.harmony[{index}].root is unknown")
        if chord["quality"] not in CHORD_QUALITIES:
            raise ValidationError(f"unsupported chord quality: {chord['quality']}")
        extensions = chord.get("extensions", [])
        if not isinstance(extensions, list) or any(item not in CHORD_EXTENSIONS for item in extensions):
            raise ValidationError(f"identity.harmony[{index}] contains unsupported chord extensions")
        tone_intervals = set(CHORD_QUALITIES[chord["quality"]])
        tone_intervals.update(CHORD_EXTENSIONS[item] for item in extensions)
        _integer(
            chord.get("inversion", 0),
            f"identity.harmony[{index}].inversion",
            0,
            len(tone_intervals) - 1,
        )
        if "borrowed_from" in chord and chord["borrowed_from"] not in SCALES:
            raise ValidationError(f"identity.harmony[{index}].borrowed_from is unsupported")
    motifs = identity["motifs"]
    if not isinstance(motifs, dict) or not motifs:
        raise ValidationError("identity.motifs must be a non-empty table")
    for name, motif in motifs.items():
        _require(motif, {"length", "events"}, f"motif {name}")
        _only(motif, {"length", "events"}, f"motif {name}")
        motif_length = fraction(motif["length"], f"motif {name}.length")
        if not isinstance(motif["events"], list) or not motif["events"]:
            raise ValidationError(f"motif {name}.events must be non-empty")
        for index, event in enumerate(motif["events"]):
            _validate_event(event, f"motif {name}.events[{index}]", motif=True)
            event_at = fraction(event["at"], f"motif {name}.events[{index}].at", allow_zero=True, allow_negative=True)
            if event_at < -motif_length or event_at >= motif_length:
                raise ValidationError(f"motif {name}.events[{index}] begins outside its bounded cycle")

    if not isinstance(song["form"], list) or not song["form"]:
        raise ValidationError("form must contain at least one section")
    for index, section in enumerate(song["form"]):
        _require(section, {"kind", "bars", "energy_start", "energy_end"}, f"form[{index}]")
        _only(section, {"kind", "bars", "energy_start", "energy_end", "motif", "transforms", "add_roles", "remove_roles", "patterns", "fills", "fill_every", "phrase_bars"}, f"form[{index}]")
        if not isinstance(section["kind"], str) or section["kind"] not in style["sections"]:
            raise ValidationError(f"form[{index}] uses unknown section kind {section['kind']}")
        _integer(section["bars"], f"form[{index}].bars", 1)
        for key in ("energy_start", "energy_end"):
            if isinstance(section[key], bool) or not isinstance(section[key], (int, float)) or not math.isfinite(section[key]) or not 0 <= section[key] <= 1:
                raise ValidationError(f"form[{index}].{key} must be between 0 and 1")
        if "motif" in section and section["motif"] not in motifs:
            raise ValidationError(f"form[{index}] references unknown motif {section['motif']}")
        transforms = section.get("transforms", [])
        if not isinstance(transforms, list) or not all(isinstance(item, str) for item in transforms):
            raise ValidationError(f"form[{index}].transforms must be a list of strings")
        transform_motif([0], [1.0], transforms)
        for field in ("add_roles", "remove_roles"):
            if field in section and (not isinstance(section[field], list) or not all(isinstance(item, str) for item in section[field])):
                raise ValidationError(f"form[{index}].{field} must be a list of strings")
            if field in section:
                unknown_roles = sorted(set(section[field]) - set(style["roles"]))
                if unknown_roles:
                    raise ValidationError(f"form[{index}].{field} references unknown roles: {', '.join(unknown_roles)}")
        for field in ("patterns", "fills"):
            mapping = section.get(field, {})
            if not isinstance(mapping, dict) or any(role not in style["roles"] or pattern not in style["patterns"] for role, pattern in mapping.items()):
                raise ValidationError(f"form[{index}].{field} must map known roles to known patterns")
        if "fill_every" in section:
            _integer(section["fill_every"], f"form[{index}].fill_every", 1)
        if "phrase_bars" in section:
            _integer(section["phrase_bars"], f"form[{index}].phrase_bars", 1)

    total_bars = sum(int(section["bars"]) for section in song["form"])
    for group in (timeline["tempo"], timeline["meter"], harmony):
        if any(int(event["bar"]) > total_bars for event in group):
            raise ValidationError("timeline and harmony events must fall within the song form")

    if "vocals" in song:
        vocals = song["vocals"]
        _require(vocals, {"language", "events"}, "vocals")
        _only(vocals, {"language", "range", "events"}, "vocals")
        _nonempty_string(vocals["language"], "vocals.language")
        if "range" in vocals:
            _nonempty_string(vocals["range"], "vocals.range")
        if not isinstance(vocals["events"], list):
            raise ValidationError("vocals.events must be a list")
        for index, event in enumerate(vocals["events"]):
            _require(event, {"bar", "beat", "text"}, f"vocals.events[{index}]")
            _only(event, {"bar", "beat", "text", "delivery"}, f"vocals.events[{index}]")
            _integer(event["bar"], f"vocals.events[{index}].bar", 1, total_bars)
            fraction(event["beat"], f"vocals.events[{index}].beat")
            _nonempty_string(event["text"], f"vocals.events[{index}].text")
            if "delivery" in event:
                _nonempty_string(event["delivery"], f"vocals.events[{index}].delivery")

    sources = song["sources"]
    _require(sources, {"policy", "external_audio", "entries"}, "sources")
    _only(sources, {"policy", "external_audio", "entries"}, "sources")
    if sources["policy"] != "original_only":
        raise ValidationError("v2 requires sources.policy = 'original_only'")
    if not isinstance(sources["external_audio"], list) or not isinstance(sources["entries"], list):
        raise ValidationError("sources.external_audio and sources.entries must be lists")
    if sources["external_audio"]:
        raise ValidationError("original_only songs cannot declare external audio")
    if not sources["entries"]:
        raise ValidationError("sources.entries must document the composition provenance")
    for entry in sources["entries"]:
        _require(entry, {"role", "origin", "owner"}, "sources entry")
        _only(entry, {"role", "origin", "owner"}, "sources entry")
        _nonempty_string(entry["role"], "sources entry.role")
        _nonempty_string(entry["owner"], "sources entry.owner")
        if not isinstance(entry["origin"], str) or entry["origin"] not in ALLOWED_ORIGINS:
            raise ValidationError(f"source origin is not rights-clean: {entry['origin']}")


def _validate_mastering(mastering: Any) -> None:
    """Validate the deliberately small, portable delivery policy."""
    if not isinstance(mastering, dict):
        raise ValidationError("production.mastering must be a table")
    _require(mastering, {"version", "sample_rate", "bit_depth", "channels", "target_lufs", "lufs_tolerance", "true_peak_dbtp", "fade_frames", "dither", "codec", "mp3_bitrate_kbps"}, "production.mastering")
    _only(mastering, {"version", "sample_rate", "bit_depth", "channels", "target_lufs", "lufs_tolerance", "true_peak_dbtp", "fade_frames", "dither", "codec", "mp3_bitrate_kbps"}, "production.mastering")
    if mastering["version"] != "songdna-mastering/v1":
        raise ValidationError("unsupported production.mastering version")
    if _integer(mastering["sample_rate"], "production.mastering.sample_rate") != 48_000:
        raise ValidationError("production.mastering.sample_rate must be 48000")
    if _integer(mastering["bit_depth"], "production.mastering.bit_depth") != 24:
        raise ValidationError("production.mastering.bit_depth must be 24")
    if _integer(mastering["channels"], "production.mastering.channels") != 2:
        raise ValidationError("production.mastering.channels must be 2")
    target = mastering["target_lufs"]
    ceiling = mastering["true_peak_dbtp"]
    for field, value, lower, upper in (("target_lufs", target, -70.0, -5.0), ("true_peak_dbtp", ceiling, -9.0, 0.0)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValidationError(f"production.mastering.{field} must be between {lower:g} and {upper:g}")
    if isinstance(mastering["lufs_tolerance"], bool) or not isinstance(mastering["lufs_tolerance"], (int, float)) or not math.isfinite(mastering["lufs_tolerance"]) or not 0 < mastering["lufs_tolerance"] <= 20:
        raise ValidationError("production.mastering.lufs_tolerance must be between 0 (exclusive) and 20")
    _integer(mastering["fade_frames"], "production.mastering.fade_frames", 1, 480_000)
    if mastering["dither"] != "none":
        raise ValidationError("production.mastering.dither must be 'none' for the 24-bit-to-24-bit canonical path")
    if mastering["codec"] != "mp3/lame-cbr":
        raise ValidationError("production.mastering.codec must be 'mp3/lame-cbr'")
    _integer(mastering["mp3_bitrate_kbps"], "production.mastering.mp3_bitrate_kbps", 32, 320)


def validate_production(production: dict[str, Any], song: dict[str, Any], style: dict[str, Any]) -> None:
    """Validate production ownership and the portable v2 graph declaration."""
    _require(production, {"schema", "song", "session", "role_map", "graph", "mastering"}, "production DNA")
    _only(production, {"schema", "song", "session", "role_map", "graph", "mastering"}, "production DNA")
    if production["schema"] != "songdna-production/v2":
        raise ValidationError(f"unsupported production schema: {production['schema']}")
    if production["song"] != song["song"]["id"]:
        raise ValidationError("production song must match song DNA id")
    _validate_mastering(production["mastering"])

    session = production["session"]
    _require(session, {"daw", "sample_rate", "bit_depth"}, "production.session")
    _only(session, {"daw", "sample_rate", "bit_depth"}, "production.session")
    if not isinstance(session["daw"], str) or not session["daw"].strip():
        raise ValidationError("production.session.daw must be a non-empty string")
    _integer(session["sample_rate"], "production.session.sample_rate", 1)
    bit_depth = _integer(session["bit_depth"], "production.session.bit_depth")
    if bit_depth not in {16, 24, 32}:
        raise ValidationError("production.session.bit_depth must be one of 16, 24, or 32")

    role_map = production["role_map"]
    if not isinstance(role_map, dict):
        raise ValidationError("production.role_map must be a table")
    expected_roles = set(style["roles"])
    if set(role_map) != expected_roles:
        missing = sorted(expected_roles - set(role_map))
        extra = sorted(set(role_map) - expected_roles)
        detail = []
        if missing:
            detail.append(f"missing roles: {', '.join(missing)}")
        if extra:
            detail.append(f"unknown roles: {', '.join(extra)}")
        raise ValidationError("production.role_map must cover style roles exactly (" + "; ".join(detail) + ")")
    for role, declaration in role_map.items():
        if not isinstance(declaration, dict):
            raise ValidationError(f"production role {role} must be a table")
        _require(declaration, {"origin", "owner", "description"}, f"production role {role}")
        _only(declaration, {"origin", "owner", "description"}, f"production role {role}")
        if not isinstance(declaration["origin"], str) or declaration["origin"] not in ALLOWED_ORIGINS:
            raise ValidationError(f"production role {role} has unsafe origin: {declaration['origin']}")
        _nonempty_string(declaration["owner"], f"production role {role}.owner")
        _nonempty_string(declaration["description"], f"production role {role}.description")
    # Import lazily to keep the graph module free of validation dependencies.
    from .production import resolve_graph
    resolve_graph(production, expected_roles)
