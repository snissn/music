from __future__ import annotations

from typing import Any

from .errors import ValidationError
from .theory import NOTE_CLASSES, SCALES


ALLOWED_ORIGINS = {"original_composition", "original_midi", "original_synthesis", "self_recorded"}
GENERATORS = {"fixed_note", "chord_pulse", "chord", "motif"}


def _require(mapping: dict[str, Any], keys: set[str], context: str) -> None:
    missing = sorted(keys - mapping.keys())
    if missing:
        raise ValidationError(f"{context} missing required fields: {', '.join(missing)}")


def validate_style(style: dict[str, Any]) -> None:
    _require(style, {"schema", "id", "defaults", "roles", "sections"}, "style")
    if style["schema"] != "songdna-style/v1":
        raise ValidationError(f"unsupported style schema: {style['schema']}")
    defaults = style["defaults"]
    _require(defaults, {"ticks_per_beat", "meter_numerator", "meter_denominator"}, "style.defaults")
    if int(defaults["ticks_per_beat"]) <= 0:
        raise ValidationError("ticks_per_beat must be positive")

    for name, role in style["roles"].items():
        _require(role, {"generator", "channel", "velocity"}, f"role {name}")
        if role["generator"] not in GENERATORS:
            raise ValidationError(f"role {name} uses unknown generator {role['generator']}")
        if not 0 <= int(role["channel"]) <= 15:
            raise ValidationError(f"role {name} channel must be 0..15")
        if not 1 <= int(role["velocity"]) <= 127:
            raise ValidationError(f"role {name} velocity must be 1..127")

    role_names = set(style["roles"])
    for name, section in style["sections"].items():
        _require(section, {"roles"}, f"section {name}")
        unknown = set(section["roles"]) - role_names
        if unknown:
            raise ValidationError(f"section {name} references unknown roles: {sorted(unknown)}")


def validate_song(song: dict[str, Any], style: dict[str, Any]) -> None:
    _require(song, {"schema", "extends", "song", "identity", "form", "sources"}, "song DNA")
    if song["schema"] != "songdna-song/v1":
        raise ValidationError(f"unsupported song schema: {song['schema']}")
    if song["extends"] != style["id"]:
        raise ValidationError(
            f"song extends {song['extends']!r}, but resolved style id is {style['id']!r}"
        )

    metadata = song["song"]
    _require(metadata, {"id", "title", "seed", "tempo", "tonic", "scale"}, "song")
    if not str(metadata["id"]).replace("_", "").isalnum():
        raise ValidationError("song id must contain only letters, digits, and underscores")
    if not 30 <= float(metadata["tempo"]) <= 300:
        raise ValidationError("tempo must be between 30 and 300 BPM")
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
    if not identity["motif_degrees"]:
        raise ValidationError("motif_degrees must not be empty")
    if len(identity["motif_degrees"]) != len(identity["motif_durations"]):
        raise ValidationError("motif degrees and durations must have equal lengths")
    if any(float(duration) <= 0 for duration in identity["motif_durations"]):
        raise ValidationError("motif durations must be positive")
    if not identity["chord_degrees"]:
        raise ValidationError("chord_degrees must not be empty")

    if not song["form"]:
        raise ValidationError("form must contain at least one section")
    for index, section in enumerate(song["form"]):
        _require(section, {"kind", "bars", "energy_start", "energy_end"}, f"form[{index}]")
        if section["kind"] not in style["sections"]:
            raise ValidationError(f"form[{index}] uses unknown section kind {section['kind']}")
        if int(section["bars"]) <= 0:
            raise ValidationError(f"form[{index}] bars must be positive")
        for key in ("energy_start", "energy_end"):
            if not 0 <= float(section[key]) <= 1:
                raise ValidationError(f"form[{index}].{key} must be between 0 and 1")

    sources = song["sources"]
    _require(sources, {"policy", "external_audio", "entries"}, "sources")
    if sources["policy"] != "original_only":
        raise ValidationError("v1 requires sources.policy = 'original_only'")
    if sources["external_audio"]:
        raise ValidationError("original_only songs cannot declare external audio")
    if not sources["entries"]:
        raise ValidationError("sources.entries must document the composition provenance")
    for entry in sources["entries"]:
        _require(entry, {"role", "origin", "owner"}, "sources entry")
        if entry["origin"] not in ALLOWED_ORIGINS:
            raise ValidationError(f"source origin is not rights-clean: {entry['origin']}")

