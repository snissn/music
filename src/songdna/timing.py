from __future__ import annotations

from fractions import Fraction
import re
from typing import Any

from .errors import ValidationError
from .model import Arrangement, MeterChange, TempoChange


RATIONAL_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:/(?:[1-9]|1[0-6]))?\Z")


def fraction(value: Any, context: str, *, allow_zero: bool = False, allow_negative: bool = False) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(f"{context} must be an integer or rational string")
    if isinstance(value, str) and not RATIONAL_PATTERN.fullmatch(value):
        raise ValidationError(f"{context} must be a valid rational value")
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValidationError(f"{context} must be a valid rational value") from exc
    if result.denominator > 16:
        raise ValidationError(f"{context} denominator must be at most 16")
    if (not allow_negative and result < 0) or (not allow_zero and result == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValidationError(f"{context} must be {qualifier}")
    return result


def _bar_starts(total_bars: int, meters: list[dict[str, Any]], ticks_per_beat: int) -> tuple[tuple[int, ...], tuple[MeterChange, ...]]:
    by_bar = {int(item["bar"]): item for item in meters}
    if 1 not in by_bar:
        raise ValidationError("timeline.meter must begin at bar 1")
    starts = [0]
    changes: list[MeterChange] = []
    current = by_bar[1]
    for bar in range(1, total_bars + 1):
        if bar in by_bar:
            current = by_bar[bar]
            changes.append(MeterChange(starts[-1], bar, int(current["numerator"]), int(current["denominator"])))
        bar_ticks = Fraction(int(current["numerator"]) * 4, int(current["denominator"])) * ticks_per_beat
        if bar_ticks.denominator != 1:
            raise ValidationError(f"meter at bar {bar} does not produce an integer tick boundary")
        starts.append(starts[-1] + bar_ticks.numerator)
    return tuple(starts), tuple(changes)


def build_timeline(song: dict[str, Any], style: dict[str, Any], total_bars: int) -> tuple[tuple[int, ...], tuple[MeterChange, ...], tuple[TempoChange, ...]]:
    ticks = int(style["defaults"]["ticks_per_beat"])
    timeline = song["timeline"]
    starts, meter_map = _bar_starts(total_bars, timeline["meter"], ticks)
    shell = Arrangement(
        song_id=str(song["song"]["id"]), title=str(song["song"]["title"]),
        ticks_per_beat=ticks, total_bars=total_bars, bar_start_ticks=starts,
        tempo_map=(TempoChange(0, 1, Fraction(1), 120.0, 500_000),), meter_map=meter_map,
    )
    changes: list[TempoChange] = []
    for index, item in enumerate(timeline["tempo"]):
        bar = int(item["bar"])
        beat = fraction(item.get("beat", 1), f"timeline.tempo[{index}].beat")
        tick = shell.position_to_tick(bar, beat)
        bpm = float(item["bpm"])
        microseconds = round(60_000_000 / bpm)
        if not 1 <= microseconds <= 0xFFFFFF:
            raise ValidationError(f"timeline.tempo[{index}] cannot be represented by MIDI")
        changes.append(TempoChange(tick, bar, beat, bpm, microseconds))
    if not changes or changes[0].tick != 0:
        raise ValidationError("timeline.tempo must begin at bar 1 beat 1")
    if any(left.tick >= right.tick for left, right in zip(changes, changes[1:])):
        raise ValidationError("timeline.tempo events must be strictly ordered and unique")
    return starts, meter_map, tuple(changes)
