from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Note:
    role: str
    start: int
    duration: int
    pitch: int
    velocity: int
    channel: int


@dataclass(frozen=True)
class Marker:
    tick: int
    name: str


@dataclass
class Arrangement:
    song_id: str
    title: str
    tempo: float
    meter_numerator: int
    meter_denominator: int
    ticks_per_beat: int
    total_bars: int
    notes_by_role: dict[str, list[Note]] = field(default_factory=dict)
    markers: list[Marker] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_ticks(self) -> int:
        beats_per_bar = self.meter_numerator * (4 / self.meter_denominator)
        return round(self.total_bars * beats_per_bar * self.ticks_per_beat)


@dataclass(frozen=True)
class CompileResult:
    song_id: str
    output_dir: Path
    midi_path: Path
    marker_path: Path
    report_path: Path
    manifest_path: Path
    total_bars: int
    total_notes: int

