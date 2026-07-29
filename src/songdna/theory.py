from __future__ import annotations

from .errors import ValidationError


NOTE_CLASSES = {
    "C": 0,
    "C#": 1,
    "Db": 1,
    "D": 2,
    "D#": 3,
    "Eb": 3,
    "E": 4,
    "F": 5,
    "F#": 6,
    "Gb": 6,
    "G": 7,
    "G#": 8,
    "Ab": 8,
    "A": 9,
    "A#": 10,
    "Bb": 10,
    "B": 11,
}

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "natural_minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
}


def scale_pitch(tonic: str, scale_name: str, octave: int, degree: int) -> int:
    """Resolve a zero-based scale degree; degrees may extend across octaves."""
    if tonic not in NOTE_CLASSES:
        raise ValidationError(f"unknown tonic: {tonic}")
    if scale_name not in SCALES:
        raise ValidationError(f"unknown scale: {scale_name}")
    scale = SCALES[scale_name]
    octave_shift, normalized = divmod(degree, len(scale))
    pitch = 12 * (octave + 1 + octave_shift) + NOTE_CLASSES[tonic] + scale[normalized]
    if not 0 <= pitch <= 127:
        raise ValidationError(f"resolved MIDI pitch outside 0..127: {pitch}")
    return pitch


def chord_pitches(
    tonic: str,
    scale_name: str,
    octave: int,
    root_degree: int,
    voices: int = 3,
) -> list[int]:
    return [
        scale_pitch(tonic, scale_name, octave, root_degree + (voice * 2))
        for voice in range(voices)
    ]

