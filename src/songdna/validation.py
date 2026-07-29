from __future__ import annotations

from typing import Any

from .errors import ValidationError
from .theory import NOTE_CLASSES, SCALES
from .transforms import transform_motif


ALLOWED_ORIGINS = {"original_composition", "original_midi", "original_synthesis", "self_recorded"}
GENERATORS = {"fixed_note", "chord_pulse", "chord", "motif"}


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
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise ValidationError(f"{context} must be a positive number")
    return float(value)


def _validate_role(role: dict[str, Any], name: str) -> None:
    generator = role["generator"]
    if generator == "fixed_note":
        _integer(role.get("note"), f"role {name}.note", 0, 127)
    elif generator in {"chord_pulse", "chord", "motif"}:
        _integer(role.get("octave"), f"role {name}.octave")
    if generator in {"fixed_note", "chord_pulse", "chord"}:
        offsets = role.get("offsets", [0.0])
        if not isinstance(offsets, list):
            raise ValidationError(f"role {name}.offsets must be a list")
        for index, offset in enumerate(offsets):
            if isinstance(offset, bool) or not isinstance(offset, (int, float)) or offset < 0:
                raise ValidationError(f"role {name}.offsets[{index}] must be a non-negative number")
        _positive_number(role.get("duration", 0.25), f"role {name}.duration")
    if "min_energy" in role:
        value = role["min_energy"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValidationError(f"role {name}.min_energy must be between 0 and 1")
    if generator == "chord":
        _integer(role.get("voices", 3), f"role {name}.voices", 1)
    if generator == "motif" and "gate" in role:
        gate = role["gate"]
        if isinstance(gate, bool) or not isinstance(gate, (int, float)) or not 0 < gate <= 1:
            raise ValidationError(f"role {name}.gate must be between 0 (exclusive) and 1")


def validate_style(style: dict[str, Any]) -> None:
    _require(style, {"schema", "id", "defaults", "roles", "sections"}, "style")
    _only(style, {"schema", "id", "name", "defaults", "roles", "sections"}, "style")
    if style["schema"] != "songdna-style/v1":
        raise ValidationError(f"unsupported style schema: {style['schema']}")
    defaults = style["defaults"]
    _require(defaults, {"ticks_per_beat", "meter_numerator", "meter_denominator"}, "style.defaults")
    _only(defaults, {"ticks_per_beat", "meter_numerator", "meter_denominator", "velocity_jitter"}, "style.defaults")
    _integer(defaults["ticks_per_beat"], "style.defaults.ticks_per_beat", 1)
    _integer(defaults["meter_numerator"], "style.defaults.meter_numerator", 1)
    denominator = _integer(defaults["meter_denominator"], "style.defaults.meter_denominator", 1)
    if denominator <= 0 or denominator & (denominator - 1):
        raise ValidationError("meter_denominator must be a positive power of two")

    if not isinstance(style["roles"], dict) or not style["roles"]:
        raise ValidationError("style.roles must be a non-empty table")
    if not isinstance(style["sections"], dict) or not style["sections"]:
        raise ValidationError("style.sections must be a non-empty table")
    for name, role in style["roles"].items():
        _require(role, {"generator", "channel", "velocity"}, f"role {name}")
        _only(role, {"generator", "channel", "velocity", "note", "offsets", "duration", "min_energy", "octave", "voices", "gate"}, f"role {name}")
        if role["generator"] not in GENERATORS:
            raise ValidationError(f"role {name} uses unknown generator {role['generator']}")
        _integer(role["channel"], f"role {name}.channel", 0, 15)
        _integer(role["velocity"], f"role {name}.velocity", 1, 127)
        _validate_role(role, name)

    role_names = set(style["roles"])
    for name, section in style["sections"].items():
        _require(section, {"roles"}, f"section {name}")
        _only(section, {"roles"}, f"section {name}")
        if not isinstance(section["roles"], list):
            raise ValidationError(f"section {name}.roles must be a list")
        unknown = set(section["roles"]) - role_names
        if unknown:
            raise ValidationError(f"section {name} references unknown roles: {sorted(unknown)}")


def validate_song(song: dict[str, Any], style: dict[str, Any]) -> None:
    _require(song, {"schema", "extends", "song", "identity", "form", "sources"}, "song DNA")
    _only(song, {"schema", "extends", "song", "identity", "form", "sources"}, "song DNA")
    if song["schema"] != "songdna-song/v1":
        raise ValidationError(f"unsupported song schema: {song['schema']}")
    if song["extends"] != style["id"]:
        raise ValidationError(
            f"song extends {song['extends']!r}, but resolved style id is {style['id']!r}"
        )

    metadata = song["song"]
    _require(metadata, {"id", "title", "seed", "tempo", "tonic", "scale"}, "song")
    _only(metadata, {"id", "title", "seed", "tempo", "tonic", "scale", "meter_numerator", "meter_denominator"}, "song")
    if not str(metadata["id"]).replace("_", "").isalnum():
        raise ValidationError("song id must contain only letters, digits, and underscores")
    if isinstance(metadata["tempo"], bool) or not isinstance(metadata["tempo"], (int, float)) or not 30 <= metadata["tempo"] <= 300:
        raise ValidationError("tempo must be between 30 and 300 BPM")
    _integer(metadata["seed"], "song.seed")
    if metadata["tonic"] not in NOTE_CLASSES:
        raise ValidationError(f"unknown tonic: {metadata['tonic']}")
    if metadata["scale"] not in SCALES:
        raise ValidationError(f"unknown scale: {metadata['scale']}")

    identity = song["identity"]
    _require(
        identity,
        {"motif_degrees", "motif_durations", "chord_degrees"},
        "identity",
    )
    _only(identity, {"motif_degrees", "motif_durations", "chord_degrees"}, "identity")
    if not identity["motif_degrees"]:
        raise ValidationError("motif_degrees must not be empty")
    if len(identity["motif_degrees"]) != len(identity["motif_durations"]):
        raise ValidationError("motif degrees and durations must have equal lengths")
    if any(float(duration) <= 0 for duration in identity["motif_durations"]):
        raise ValidationError("motif durations must be positive")
    if not identity["chord_degrees"]:
        raise ValidationError("chord_degrees must not be empty")

    if not isinstance(song["form"], list) or not song["form"]:
        raise ValidationError("form must contain at least one section")
    for index, section in enumerate(song["form"]):
        _require(section, {"kind", "bars", "energy_start", "energy_end"}, f"form[{index}]")
        _only(section, {"kind", "bars", "energy_start", "energy_end", "transforms", "add_roles", "remove_roles"}, f"form[{index}]")
        if section["kind"] not in style["sections"]:
            raise ValidationError(f"form[{index}] uses unknown section kind {section['kind']}")
        _integer(section["bars"], f"form[{index}].bars", 1)
        for key in ("energy_start", "energy_end"):
            if isinstance(section[key], bool) or not isinstance(section[key], (int, float)) or not 0 <= section[key] <= 1:
                raise ValidationError(f"form[{index}].{key} must be between 0 and 1")
        transforms = section.get("transforms", [])
        if not isinstance(transforms, list) or not all(isinstance(item, str) for item in transforms):
            raise ValidationError(f"form[{index}].transforms must be a list of strings")
        transform_motif([0], [1.0], transforms)
        for field in ("add_roles", "remove_roles"):
            if field in section and (not isinstance(section[field], list) or not all(isinstance(item, str) for item in section[field])):
                raise ValidationError(f"form[{index}].{field} must be a list of strings")

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
        if entry["origin"] not in ALLOWED_ORIGINS:
            raise ValidationError(f"source origin is not rights-clean: {entry['origin']}")


def validate_production(production: dict[str, Any], song: dict[str, Any], style: dict[str, Any]) -> None:
    """Validate the v1 production declaration without claiming to render audio."""
    _require(production, {"schema", "song", "session", "role_map"}, "production DNA")
    _only(production, {"schema", "song", "session", "role_map"}, "production DNA")
    if production["schema"] != "songdna-production/v1":
        raise ValidationError(f"unsupported production schema: {production['schema']}")
    if production["song"] != song["song"]["id"]:
        raise ValidationError("production song must match song DNA id")

    session = production["session"]
    _require(session, {"daw", "sample_rate", "bit_depth"}, "production.session")
    _only(session, {"daw", "sample_rate", "bit_depth"}, "production.session")
    if not isinstance(session["daw"], str) or not session["daw"].strip():
        raise ValidationError("production.session.daw must be a non-empty string")
    if int(session["sample_rate"]) <= 0:
        raise ValidationError("production.session.sample_rate must be positive")
    if int(session["bit_depth"]) not in {16, 24, 32}:
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
        if declaration["origin"] not in ALLOWED_ORIGINS:
            raise ValidationError(f"production role {role} has unsafe origin: {declaration['origin']}")
        if not str(declaration["owner"]).strip() or not str(declaration["description"]).strip():
            raise ValidationError(f"production role {role} requires owner and description")
