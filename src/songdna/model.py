from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import ValidationError


@dataclass(frozen=True)
class Note:
    role: str
    start: int
    duration: int
    pitch: int
    velocity: int
    channel: int
    articulation: str = "normal"
    phrase: str | None = None


@dataclass(frozen=True)
class Marker:
    tick: int
    name: str
    bar: int
    beat: Fraction = Fraction(1)


@dataclass(frozen=True)
class TempoChange:
    tick: int
    bar: int
    beat: Fraction
    bpm: float
    microseconds_per_quarter: int


@dataclass(frozen=True)
class MeterChange:
    tick: int
    bar: int
    numerator: int
    denominator: int


@dataclass
class Arrangement:
    song_id: str
    title: str
    ticks_per_beat: int
    total_bars: int
    bar_start_ticks: tuple[int, ...]
    tempo_map: tuple[TempoChange, ...]
    meter_map: tuple[MeterChange, ...]
    notes_by_role: dict[str, list[Note]] = field(default_factory=dict)
    markers: list[Marker] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    harmony: list[dict[str, Any]] = field(default_factory=list)
    vocals: dict[str, Any] | None = None
    style_lineage: tuple[str, ...] = ()

    @property
    def tempo(self) -> float:
        """Initial tempo, retained for adapters that only display one value."""
        return self.tempo_map[0].bpm

    @property
    def meter_numerator(self) -> int:
        return self.meter_map[0].numerator

    @property
    def meter_denominator(self) -> int:
        return self.meter_map[0].denominator

    @property
    def total_ticks(self) -> int:
        return self.bar_start_ticks[-1]

    def meter_at_bar(self, bar: int) -> MeterChange:
        if not 1 <= bar <= self.total_bars:
            raise ValidationError(f"bar must be within 1..{self.total_bars}: {bar}")
        current = self.meter_map[0]
        for change in self.meter_map:
            if change.bar > bar:
                break
            current = change
        return current

    def position_to_tick(self, bar: int, beat: Fraction | int | str = 1) -> int:
        if not 1 <= bar <= self.total_bars:
            raise ValidationError(f"bar must be within 1..{self.total_bars}: {bar}")
        beat_value = beat if isinstance(beat, Fraction) else Fraction(str(beat))
        meter = self.meter_at_bar(bar)
        # Beat positions are one-based offsets in the meter's denominator unit.
        # Subdivisions within the final beat are valid; numerator + 1 is the
        # (exclusive) boundary of the next bar.
        if beat_value < 1 or beat_value >= meter.numerator + 1:
            raise ValidationError(
                f"beat {beat_value} is outside bar {bar}'s {meter.numerator}/{meter.denominator} meter"
            )
        offset = (beat_value - 1) * Fraction(4, meter.denominator) * self.ticks_per_beat
        if offset.denominator != 1:
            raise ValidationError(f"position bar {bar} beat {beat_value} does not land on an integer tick")
        return self.bar_start_ticks[bar - 1] + offset.numerator

    def tick_to_seconds(self, tick: int) -> float:
        if not 0 <= tick <= self.total_ticks:
            raise ValidationError(f"tick must be within 0..{self.total_ticks}: {tick}")
        elapsed = Fraction(0)
        for index, change in enumerate(self.tempo_map):
            end = self.tempo_map[index + 1].tick if index + 1 < len(self.tempo_map) else tick
            segment_end = min(tick, end)
            if segment_end > change.tick:
                elapsed += Fraction(
                    (segment_end - change.tick) * change.microseconds_per_quarter,
                    self.ticks_per_beat * 1_000_000,
                )
            if tick <= end:
                break
        return float(elapsed)


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
