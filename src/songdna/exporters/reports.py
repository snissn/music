from __future__ import annotations

import csv
import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Any

from ..model import Arrangement


def write_markers(arrangement: Arrangement, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["section", "bar", "beat", "tick", "seconds"])
        for marker in arrangement.markers:
            writer.writerow([
                marker.name, marker.bar, str(marker.beat), marker.tick,
                f"{arrangement.tick_to_seconds(marker.tick):.6f}",
            ])


def write_arrangement_report(arrangement: Arrangement, path: Path) -> None:
    role_counts = {role: len(notes) for role, notes in sorted(arrangement.notes_by_role.items())}
    events = [
        {
            "role": note.role,
            "start_tick": note.start,
            "duration_ticks": note.duration,
            "pitch": note.pitch,
            "velocity": note.velocity,
            "channel": note.channel,
            "articulation": note.articulation,
            "phrase": note.phrase,
        }
        for _role, notes in sorted(arrangement.notes_by_role.items())
        for note in notes
    ]
    events.sort(key=lambda event: (event["start_tick"], event["role"], event["pitch"], event["duration_ticks"]))
    payload = {
        "schema": "songdna-arrangement/v2",
        "song_id": arrangement.song_id,
        "title": arrangement.title,
        "ticks_per_beat": arrangement.ticks_per_beat,
        "total_bars": arrangement.total_bars,
        "total_ticks": arrangement.total_ticks,
        "note_counts": role_counts,
        "events": events,
        "sections": arrangement.sections,
        "style_lineage": list(arrangement.style_lineage),
        "tempo_map": [
            {
                "bar": item.bar, "beat": str(item.beat), "tick": item.tick,
                "bpm": item.bpm, "microseconds_per_quarter": item.microseconds_per_quarter,
                "seconds": arrangement.tick_to_seconds(item.tick),
            }
            for item in arrangement.tempo_map
        ],
        "meter_map": [
            {
                "bar": item.bar, "tick": item.tick,
                "numerator": item.numerator, "denominator": item.denominator,
                "seconds": arrangement.tick_to_seconds(item.tick),
            }
            for item in arrangement.meter_map
        ],
        "harmony": arrangement.harmony,
        "vocals": arrangement.vocals,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_rights_report(song: dict[str, Any], path: Path) -> None:
    payload = {
        "schema": "songdna-rights/v1",
        "song_id": song["song"]["id"],
        "policy": song["sources"]["policy"],
        "external_audio": song["sources"]["external_audio"],
        "entries": song["sources"]["entries"],
        "status": "rights-clean-source-declarations-validated",
        "note": "This report validates declared provenance; it is not a global melody-similarity guarantee.",
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(song_id: str, paths: list[Path], path: Path) -> None:
    from songdna import __version__

    payload = {
        "schema": "songdna-manifest/v1",
        "song_id": song_id,
        "compiler": {"name": "songdna", "version": __version__},
        "toolchain": {"python": platform.python_version(), "implementation": platform.python_implementation()},
        "artifacts": {
            artifact.name: {"sha256": sha256(artifact), "bytes": artifact.stat().st_size}
            for artifact in sorted(paths)
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
