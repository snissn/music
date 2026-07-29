from __future__ import annotations

import math
import re
from typing import Any

from .errors import ValidationError
from .theory import NOTE_CLASSES, SCALES
from .transforms import transform_motif


ALLOWED_ORIGINS = {"original_composition", "original_midi", "original_synthesis", "self_recorded"}
GENERATORS = {"fixed_note", "chord_pulse", "chord", "motif"}
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


def _validate_role(role: dict[str, Any], name: str) -> None:
    generator = role["generator"]
    if not isinstance(generator, str) or generator not in GENERATORS:
        raise ValidationError(f"role {name} uses unknown generator {generator!r}")
    if "note" in role:
        _integer(role["note"], f"role {name}.note", 0, 127)
    if "octave" in role:
        _integer(role["octave"], f"role {name}.octave")
    if "offsets" in role:
        offsets = role["offsets"]
        if not isinstance(offsets, list):
            raise ValidationError(f"role {name}.offsets must be a list")
        for index, offset in enumerate(offsets):
            if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not math.isfinite(offset) or offset < 0:
                raise ValidationError(f"role {name}.offsets[{index}] must be a non-negative number")
    if "duration" in role:
        _positive_number(role["duration"], f"role {name}.duration")
    if "min_energy" in role:
        value = role["min_energy"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValidationError(f"role {name}.min_energy must be between 0 and 1")
    if "voices" in role:
        _integer(role["voices"], f"role {name}.voices", 1)
    if "gate" in role:
        gate = role["gate"]
        if isinstance(gate, bool) or not isinstance(gate, (int, float)) or not math.isfinite(gate) or not 0 < gate <= 1:
            raise ValidationError(f"role {name}.gate must be between 0 (exclusive) and 1")
    if generator == "fixed_note":
        _integer(role.get("note"), f"role {name}.note", 0, 127)
    elif generator in {"chord_pulse", "chord", "motif"}:
        _integer(role.get("octave"), f"role {name}.octave")
    if generator in {"fixed_note", "chord_pulse", "chord"}:
        _positive_number(role.get("duration", 0.25), f"role {name}.duration")
    if generator == "chord":
        _integer(role.get("voices", 3), f"role {name}.voices", 1)


def validate_style(style: dict[str, Any]) -> None:
    _require(style, {"schema", "id", "defaults", "roles", "sections"}, "style")
    _only(style, {"schema", "id", "name", "defaults", "roles", "sections"}, "style")
    if style["schema"] != "songdna-style/v1":
        raise ValidationError(f"unsupported style schema: {style['schema']}")
    if not isinstance(style["id"], str) or not STYLE_ID_PATTERN.fullmatch(style["id"]):
        raise ValidationError("style.id must be a v1 style identifier")
    if "name" in style:
        _nonempty_string(style["name"], "style.name")
    defaults = style["defaults"]
    _require(defaults, {"ticks_per_beat", "meter_numerator", "meter_denominator"}, "style.defaults")
    _only(defaults, {"ticks_per_beat", "meter_numerator", "meter_denominator", "velocity_jitter"}, "style.defaults")
    _integer(defaults["ticks_per_beat"], "style.defaults.ticks_per_beat", 1)
    _integer(defaults["meter_numerator"], "style.defaults.meter_numerator", 1)
    denominator = _integer(defaults["meter_denominator"], "style.defaults.meter_denominator", 1)
    if denominator not in METER_DENOMINATORS:
        raise ValidationError("meter_denominator must be one of 1, 2, 4, 8, 16, or 32")
    if "velocity_jitter" in defaults:
        _integer(defaults["velocity_jitter"], "style.defaults.velocity_jitter", 0)

    if not isinstance(style["roles"], dict) or not style["roles"]:
        raise ValidationError("style.roles must be a non-empty table")
    if not isinstance(style["sections"], dict) or not style["sections"]:
        raise ValidationError("style.sections must be a non-empty table")
    for name, role in style["roles"].items():
        _require(role, {"generator", "channel", "velocity"}, f"role {name}")
        _only(role, {"generator", "channel", "velocity", "note", "offsets", "duration", "min_energy", "octave", "voices", "gate"}, f"role {name}")
        _integer(role["channel"], f"role {name}.channel", 0, 15)
        _integer(role["velocity"], f"role {name}.velocity", 1, 127)
        _validate_role(role, name)

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
    _require(song, {"schema", "extends", "song", "identity", "form", "sources"}, "song DNA")
    _only(song, {"schema", "extends", "song", "identity", "form", "sources"}, "song DNA")
    if song["schema"] != "songdna-song/v1":
        raise ValidationError(f"unsupported song schema: {song['schema']}")
    if not isinstance(song["extends"], str) or not STYLE_ID_PATTERN.fullmatch(song["extends"]):
        raise ValidationError("song.extends must be a v1 style identifier")
    if song["extends"] != style["id"]:
        raise ValidationError(
            f"song extends {song['extends']!r}, but resolved style id is {style['id']!r}"
        )

    metadata = song["song"]
    _require(metadata, {"id", "title", "seed", "tempo", "tonic", "scale"}, "song")
    _only(metadata, {"id", "title", "seed", "tempo", "tonic", "scale", "meter_numerator", "meter_denominator"}, "song")
    if not isinstance(metadata["id"], str) or not SONG_ID_PATTERN.fullmatch(metadata["id"]):
        raise ValidationError("song id must contain only letters, digits, and underscores")
    _nonempty_string(metadata["title"], "song.title")
    if isinstance(metadata["tempo"], bool) or not isinstance(metadata["tempo"], (int, float)) or not 30 <= metadata["tempo"] <= 300:
        raise ValidationError("tempo must be between 30 and 300 BPM")
    _integer(metadata["seed"], "song.seed")
    if "meter_numerator" in metadata:
        _integer(metadata["meter_numerator"], "song.meter_numerator", 1)
    if "meter_denominator" in metadata:
        denominator = _integer(metadata["meter_denominator"], "song.meter_denominator", 1)
        if denominator not in METER_DENOMINATORS:
            raise ValidationError("song.meter_denominator must be one of 1, 2, 4, 8, 16, or 32")
    if not isinstance(metadata["tonic"], str) or metadata["tonic"] not in NOTE_CLASSES:
        raise ValidationError(f"unknown tonic: {metadata['tonic']}")
    if not isinstance(metadata["scale"], str) or metadata["scale"] not in SCALES:
        raise ValidationError(f"unknown scale: {metadata['scale']}")

    identity = song["identity"]
    _require(
        identity,
        {"motif_degrees", "motif_durations", "chord_degrees"},
        "identity",
    )
    _only(identity, {"motif_degrees", "motif_durations", "chord_degrees"}, "identity")
    if not isinstance(identity["motif_degrees"], list) or not identity["motif_degrees"]:
        raise ValidationError("motif_degrees must not be empty")
    if not isinstance(identity["motif_durations"], list):
        raise ValidationError("motif_durations must be a list")
    if not isinstance(identity["chord_degrees"], list) or not identity["chord_degrees"]:
        raise ValidationError("chord_degrees must not be empty")
    if len(identity["motif_degrees"]) != len(identity["motif_durations"]):
        raise ValidationError("motif degrees and durations must have equal lengths")
    for index, degree in enumerate(identity["motif_degrees"]):
        _integer(degree, f"motif_degrees[{index}]")
    for index, duration in enumerate(identity["motif_durations"]):
        _positive_number(duration, f"motif_durations[{index}]")
    for index, degree in enumerate(identity["chord_degrees"]):
        _integer(degree, f"chord_degrees[{index}]")

    if not isinstance(song["form"], list) or not song["form"]:
        raise ValidationError("form must contain at least one section")
    for index, section in enumerate(song["form"]):
        _require(section, {"kind", "bars", "energy_start", "energy_end"}, f"form[{index}]")
        _only(section, {"kind", "bars", "energy_start", "energy_end", "transforms", "add_roles", "remove_roles"}, f"form[{index}]")
        if not isinstance(section["kind"], str) or section["kind"] not in style["sections"]:
            raise ValidationError(f"form[{index}] uses unknown section kind {section['kind']}")
        _integer(section["bars"], f"form[{index}].bars", 1)
        for key in ("energy_start", "energy_end"):
            if isinstance(section[key], bool) or not isinstance(section[key], (int, float)) or not math.isfinite(section[key]) or not 0 <= section[key] <= 1:
                raise ValidationError(f"form[{index}].{key} must be between 0 and 1")
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

    sources = song["sources"]
    _require(sources, {"policy", "external_audio", "entries"}, "sources")
    _only(sources, {"policy", "external_audio", "entries"}, "sources")
    if sources["policy"] != "original_only":
        raise ValidationError("v1 requires sources.policy = 'original_only'")
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


def validate_production(production: dict[str, Any], song: dict[str, Any], style: dict[str, Any]) -> None:
    """Validate production ownership and the portable v2 graph declaration."""
    _require(production, {"schema", "song", "session", "role_map", "graph"}, "production DNA")
    _only(production, {"schema", "song", "session", "role_map", "graph"}, "production DNA")
    if production["schema"] != "songdna-production/v2":
        raise ValidationError(f"unsupported production schema: {production['schema']}")
    if production["song"] != song["song"]["id"]:
        raise ValidationError("production song must match song DNA id")

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
