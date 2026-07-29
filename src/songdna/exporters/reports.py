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
    beats_per_bar = arrangement.meter_numerator * (4 / arrangement.meter_denominator)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["section", "bar", "beat", "seconds"])
        for marker in arrangement.markers:
            beat = marker.tick / arrangement.ticks_per_beat
            bar = int(beat // beats_per_bar) + 1
            seconds = beat * 60 / arrangement.tempo
            writer.writerow([marker.name, bar, f"{beat + 1:.3f}", f"{seconds:.3f}"])


def write_arrangement_report(arrangement: Arrangement, path: Path) -> None:
    role_counts = {role: len(notes) for role, notes in sorted(arrangement.notes_by_role.items())}
    payload = {
        "schema": "songdna-arrangement/v1",
        "song_id": arrangement.song_id,
        "title": arrangement.title,
        "tempo": arrangement.tempo,
        "meter": f"{arrangement.meter_numerator}/{arrangement.meter_denominator}",
        "ticks_per_beat": arrangement.ticks_per_beat,
        "total_bars": arrangement.total_bars,
        "total_ticks": arrangement.total_ticks,
        "note_counts": role_counts,
        "sections": arrangement.sections,
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
