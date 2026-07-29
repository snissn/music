from __future__ import annotations

from .errors import ValidationError
from fractions import Fraction
from typing import Any


def transform_motif(
    degrees: list[int], durations: list[float], transforms: list[str]
) -> tuple[list[int], list[float], int]:
    result_degrees = list(degrees)
    result_durations = list(durations)
    octave_shift = 0

    for transform in transforms:
        if transform == "reverse":
            result_degrees.reverse()
            result_durations.reverse()
        elif transform == "octave_up":
            octave_shift += 1
        elif transform == "octave_down":
            octave_shift -= 1
        elif transform == "augment2":
            result_durations = [duration * 2 for duration in result_durations]
        elif transform == "diminish2":
            result_durations = [duration / 2 for duration in result_durations]
        elif transform.startswith("rotate:"):
            try:
                amount = int(transform.split(":", 1)[1]) % len(result_degrees)
            except ValueError as exc:
                raise ValidationError(f"invalid rotate transformation: {transform}") from exc
            result_degrees = result_degrees[amount:] + result_degrees[:amount]
            result_durations = result_durations[amount:] + result_durations[:amount]
        elif transform.startswith("transpose_degree:"):
            try:
                amount = int(transform.split(":", 1)[1])
            except ValueError as exc:
                raise ValidationError(f"invalid transpose transformation: {transform}") from exc
            result_degrees = [degree + amount for degree in result_degrees]
        else:
            raise ValidationError(f"unknown motif transformation: {transform}")

    return result_degrees, result_durations, octave_shift


def transform_motif_events(
    events: list[dict[str, Any]], length: Fraction, transforms: list[str]
) -> tuple[list[dict[str, Any]], Fraction, int]:
    """Transform an expressive motif while keeping its event timing inspectable."""
    result = [dict(event) for event in events]
    result_length = length
    octave_shift = 0
    for transform in transforms:
        if transform == "reverse":
            for event in result:
                at = Fraction(str(event["at"]))
                duration = Fraction(str(event["duration"]))
                event["at"] = str(result_length - at - duration)
            result.sort(key=lambda event: Fraction(str(event["at"])))
        elif transform == "octave_up":
            octave_shift += 1
        elif transform == "octave_down":
            octave_shift -= 1
        elif transform in {"augment2", "diminish2"}:
            factor = Fraction(2) if transform == "augment2" else Fraction(1, 2)
            result_length *= factor
            for event in result:
                event["at"] = str(Fraction(str(event["at"])) * factor)
                event["duration"] = str(Fraction(str(event["duration"])) * factor)
        elif transform.startswith("rotate:"):
            try:
                amount = int(transform.split(":", 1)[1]) % len(result)
            except ValueError as exc:
                raise ValidationError(f"invalid rotate transformation: {transform}") from exc
            payloads = [{key: value for key, value in event.items() if key not in {"at", "duration"}} for event in result]
            payloads = payloads[amount:] + payloads[:amount]
            result = [
                {"at": event["at"], "duration": event["duration"], **payload}
                for event, payload in zip(result, payloads)
            ]
        elif transform.startswith("transpose_degree:"):
            try:
                amount = int(transform.split(":", 1)[1])
            except ValueError as exc:
                raise ValidationError(f"invalid transpose transformation: {transform}") from exc
            for event in result:
                if "degree" in event:
                    event["degree"] = int(event["degree"]) + amount
        else:
            raise ValidationError(f"unknown motif transformation: {transform}")
    for index, event in enumerate(result):
        at = Fraction(str(event["at"]))
        if at < -result_length or at >= result_length:
            raise ValidationError(f"transformed motif event {index} begins outside its bounded cycle")
    return result, result_length, octave_shift
