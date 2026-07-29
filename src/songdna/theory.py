from __future__ import annotations

from .errors import ValidationError


NOTE_CLASSES = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3,
    "E": 4, "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8,
    "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11,
}

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "natural_minor": [0, 2, 3, 5, 7, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
}

CHORD_QUALITIES = {
    "major": [0, 4, 7],
    "minor": [0, 3, 7],
    "diminished": [0, 3, 6],
    "augmented": [0, 4, 8],
    "sus2": [0, 2, 7],
    "sus4": [0, 5, 7],
    "power": [0, 7],
}

CHORD_EXTENSIONS = {
    "6": 9,
    "7": 10,
    "maj7": 11,
    "9": 14,
    "add9": 14,
    "11": 17,
    "13": 21,
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
    root_degree: int | None = None,
    *,
    root: str | None = None,
    quality: str = "major",
    inversion: int = 0,
    extensions: list[str] | None = None,
) -> list[int]:
    """Resolve an explicit v2 chord without inferring its quality from a style."""
    if quality not in CHORD_QUALITIES:
        raise ValidationError(f"unsupported chord quality: {quality}")
    if root_degree is None and root is None:
        raise ValidationError("chord requires degree or root")
    if root_degree is not None and root is not None:
        raise ValidationError("chord cannot declare both degree and root")
    if root is not None and root not in NOTE_CLASSES:
        raise ValidationError(f"unknown chord root: {root}")
    root_pitch = (
        scale_pitch(tonic, scale_name, octave, root_degree)
        if root_degree is not None
        else 12 * (octave + 1) + NOTE_CLASSES[root]
    )
    intervals = list(CHORD_QUALITIES[quality])
    for extension in extensions or []:
        if extension not in CHORD_EXTENSIONS:
            raise ValidationError(f"unsupported chord extension: {extension}")
        interval = CHORD_EXTENSIONS[extension]
        if interval not in intervals:
            intervals.append(interval)
    intervals.sort()
    if not 0 <= inversion < len(intervals):
        raise ValidationError(f"chord inversion must be within 0..{len(intervals) - 1}")
    pitches = [root_pitch + interval for interval in intervals]
    for _ in range(inversion):
        pitches.append(pitches.pop(0) + 12)
    pitches.sort()
    if any(not 0 <= pitch <= 127 for pitch in pitches):
        raise ValidationError(f"resolved chord pitch outside 0..127: {pitches}")
    return pitches
