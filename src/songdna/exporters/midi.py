from __future__ import annotations

import struct
from pathlib import Path

from ..model import Arrangement, Note


def _vlq(value: int) -> bytes:
    if value < 0:
        raise ValueError("MIDI delta times cannot be negative")
    buffer = value & 0x7F
    result = bytearray([buffer])
    while value >> 7:
        value >>= 7
        buffer = (value & 0x7F) | 0x80
        result.insert(0, buffer)
    return bytes(result)


def _meta(kind: int, payload: bytes) -> bytes:
    return bytes([0xFF, kind]) + _vlq(len(payload)) + payload


def _track_chunk(events: list[tuple[int, int, bytes]]) -> bytes:
    body = bytearray()
    previous_tick = 0
    for tick, _priority, event in sorted(events, key=lambda item: (item[0], item[1])):
        body.extend(_vlq(tick - previous_tick))
        body.extend(event)
        previous_tick = tick
    body.extend(_vlq(0))
    body.extend(_meta(0x2F, b""))
    return b"MTrk" + struct.pack(">I", len(body)) + body


def _conductor_track(arrangement: Arrangement) -> bytes:
    events: list[tuple[int, int, bytes]] = [(0, 0, _meta(0x03, b"SongDNA Conductor"))]
    events.extend(
        (change.tick, 1, _meta(0x51, change.microseconds_per_quarter.to_bytes(3, "big")))
        for change in arrangement.tempo_map
    )
    events.extend(
        (
            change.tick, 2,
            _meta(0x58, bytes([change.numerator, change.denominator.bit_length() - 1, 24, 8])),
        )
        for change in arrangement.meter_map
    )
    events.extend((marker.tick, 3, _meta(0x06, marker.name.encode("utf-8"))) for marker in arrangement.markers)
    return _track_chunk(events)


def _note_track(role: str, notes: list[Note]) -> bytes:
    events: list[tuple[int, int, bytes]] = [(0, 0, _meta(0x03, role.encode("utf-8")))]
    for note in notes:
        on = bytes([0x90 | note.channel, note.pitch, note.velocity])
        off = bytes([0x80 | note.channel, note.pitch, 0])
        events.append((note.start, 2, on))
        events.append((note.start + note.duration, 1, off))
    return _track_chunk(events)


def write_midi(arrangement: Arrangement, path: Path) -> None:
    role_tracks = [
        _note_track(role, notes)
        for role, notes in sorted(arrangement.notes_by_role.items())
        if notes
    ]
    tracks = [_conductor_track(arrangement), *role_tracks]
    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), arrangement.ticks_per_beat)
    path.write_bytes(header + b"".join(tracks))
