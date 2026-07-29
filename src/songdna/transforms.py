from __future__ import annotations

from .errors import ValidationError


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
            amount = int(transform.split(":", 1)[1]) % len(result_degrees)
            result_degrees = result_degrees[amount:] + result_degrees[:amount]
            result_durations = result_durations[amount:] + result_durations[:amount]
        elif transform.startswith("transpose_degree:"):
            amount = int(transform.split(":", 1)[1])
            result_degrees = [degree + amount for degree in result_degrees]
        else:
            raise ValidationError(f"unknown motif transformation: {transform}")

    return result_degrees, result_durations, octave_shift

