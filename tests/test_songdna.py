from __future__ import annotations

import copy
import csv
import hashlib
import json
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from songdna.compiler import (  # noqa: E402
    _load_toml, build_arrangement, compile_song, load_inputs, resolve_style,
)
from songdna.errors import ValidationError  # noqa: E402
from songdna.theory import chord_pitches  # noqa: E402
from songdna.validation import validate_song, validate_style  # noqa: E402

try:
    import jsonschema
except ImportError:
    jsonschema = None


BASE_STYLE = ROOT / "styles/electro_house/v2/style.toml"
SECOND_STYLE = ROOT / "styles/broken_pulse/v2/style.toml"
SONGS = tuple(ROOT / "songs" / name / "song.toml" for name in ("circuit_bloom", "neon_tides", "glass_transit", "signal_garden"))
TARGET_GOLDEN = ROOT / "tests/fixtures/v2_target/resolved-arrangement.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _vlq(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    while True:
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset


def conductor_meta(path: Path) -> list[tuple[int, int, bytes]]:
    data = path.read_bytes()
    self_track = 14
    if data[self_track:self_track + 4] != b"MTrk":
        raise AssertionError("missing conductor track")
    size = struct.unpack(">I", data[self_track + 4:self_track + 8])[0]
    cursor = self_track + 8
    end = cursor + size
    tick = 0
    events = []
    while cursor < end:
        delta, cursor = _vlq(data, cursor)
        tick += delta
        if data[cursor] != 0xFF:
            raise AssertionError("conductor contains non-meta event")
        kind = data[cursor + 1]
        length, payload_at = _vlq(data, cursor + 2)
        payload = data[payload_at:payload_at + length]
        events.append((tick, kind, payload))
        cursor = payload_at + length
        if kind == 0x2F:
            break
    return events


class SongDNAV2ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.style = resolve_style(ROOT, "electro_house/v2")
        cls.broken = resolve_style(ROOT, "broken_pulse/v2")
        cls.circuit = _load_toml(SONGS[0])
        cls.neon = _load_toml(SONGS[1])
        cls.glass = _load_toml(SONGS[2])
        cls.signal = _load_toml(SONGS[3])

    def test_signal_garden_is_short_straight_and_hat_driven(self) -> None:
        arrangement = build_arrangement(self.style, self.signal)
        self.assertEqual(arrangement.total_bars, 80)
        self.assertEqual([(item.numerator, item.denominator) for item in arrangement.meter_map], [(4, 4)])
        self.assertEqual(len(arrangement.tempo_map), 1)
        self.assertTrue(all(section["bars"] == 8 for section in arrangement.sections))
        self.assertGreater(len(arrangement.notes_by_role["closed_hat"]), 2 * len(arrangement.notes_by_role["kick"]))

    def test_target_fixture_matches_readable_golden(self) -> None:
        arrangement = build_arrangement(self.broken, self.glass)
        actual = {
            "song_id": arrangement.song_id,
            "style_lineage": list(arrangement.style_lineage),
            "total_bars": arrangement.total_bars,
            "total_ticks": arrangement.total_ticks,
            "tempo": [[item.bar, str(item.beat), item.tick, item.microseconds_per_quarter] for item in arrangement.tempo_map],
            "meter": [[item.bar, item.tick, item.numerator, item.denominator] for item in arrangement.meter_map],
            "markers": [[item.name, item.bar, item.tick] for item in arrangement.markers],
            "harmony_ticks": [item["tick"] for item in arrangement.harmony],
            "note_counts": {role: len(notes) for role, notes in sorted(arrangement.notes_by_role.items())},
        }
        self.assertEqual(actual, json.loads(TARGET_GOLDEN.read_text()))
        pulse_start = arrangement.bar_start_ticks[4]
        self.assertTrue(any(note.start == pulse_start - 240 for note in arrangement.notes_by_role["lead"]))

    def test_tempo_meter_conductor_markers_and_report_agree_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            shutil.copytree(ROOT / "styles", checkout / "styles")
            shutil.copytree(ROOT / "songs", checkout / "songs")
            result = compile_song("songs/glass_transit/song.toml", checkout)
            arrangement = build_arrangement(self.broken, self.glass)
            meta = conductor_meta(result.midi_path)
            tempos = [(tick, int.from_bytes(payload, "big")) for tick, kind, payload in meta if kind == 0x51]
            meters = [(tick, payload[0], 2 ** payload[1]) for tick, kind, payload in meta if kind == 0x58]
            markers = [(tick, payload.decode()) for tick, kind, payload in meta if kind == 0x06]
            self.assertEqual(tempos, [(item.tick, item.microseconds_per_quarter) for item in arrangement.tempo_map])
            self.assertEqual(meters, [(item.tick, item.numerator, item.denominator) for item in arrangement.meter_map])
            self.assertEqual(markers, [(item.tick, item.name) for item in arrangement.markers])
            report = json.loads(result.report_path.read_text())
            self.assertEqual([item["tick"] for item in report["tempo_map"]], [item.tick for item in arrangement.tempo_map])
            self.assertEqual([item["tick"] for item in report["meter_map"]], [item.tick for item in arrangement.meter_map])
            self.assertEqual(len(report["events"]), sum(report["note_counts"].values()))
            self.assertEqual(
                report["events"],
                sorted(report["events"], key=lambda event: (event["start_tick"], event["role"], event["pitch"], event["duration_ticks"])),
            )
            with result.marker_path.open() as marker_file:
                rows = list(csv.DictReader(marker_file))
            self.assertEqual([int(row["tick"]) for row in rows], [item.tick for item in arrangement.markers])
            for row, marker in zip(rows, arrangement.markers):
                self.assertAlmostEqual(float(row["seconds"]), arrangement.tick_to_seconds(marker.tick), places=6)

    def test_bar_beat_boundaries_are_meter_relative_and_tempo_is_piecewise(self) -> None:
        arrangement = build_arrangement(self.broken, self.glass)
        self.assertEqual(arrangement.position_to_tick(9, "1"), 15_360)
        self.assertEqual(arrangement.position_to_tick(9, "3"), 16_320)
        self.assertEqual(arrangement.position_to_tick(9, "11/2"), 17_520)
        self.assertEqual(arrangement.position_to_tick(10, "1"), 17_760)
        self.assertAlmostEqual(arrangement.tick_to_seconds(16_320), 17.586194, places=6)
        with self.assertRaisesRegex(ValidationError, "outside bar 9's 5/4 meter"):
            arrangement.position_to_tick(9, "6")

    def test_explicit_harmony_supports_quality_inversion_extensions_and_borrowing(self) -> None:
        self.assertEqual(
            chord_pitches("C", "major", 4, 0, quality="minor", inversion=1, extensions=["7", "9"]),
            [63, 67, 70, 72, 74],
        )
        self.assertEqual(chord_pitches("C", "major", 4, root="Db", quality="major"), [61, 65, 68])
        with self.assertRaisesRegex(ValidationError, "unknown chord root"):
            chord_pitches("C", "major", 4, root="H", quality="major")

    def test_pattern_probability_fills_motifs_and_expression_are_deterministic(self) -> None:
        first = build_arrangement(self.broken, self.glass)
        second = build_arrangement(self.broken, self.glass)
        self.assertEqual(first.notes_by_role, second.notes_by_role)
        # Broken kick fill adds a sixteenth-grid tail in each selected fourth bar.
        fill_bar_start = first.bar_start_ticks[8 - 1]
        kick_starts = {note.start - fill_bar_start for note in first.notes_by_role["kick"] if fill_bar_start <= note.start < first.bar_start_ticks[8]}
        self.assertTrue({1440, 1560, 1680} <= kick_starts)
        self.assertTrue(any(note.articulation == "accent" for note in first.notes_by_role["lead"]))
        self.assertTrue(all(note.phrase for note in first.notes_by_role["lead"]))
        circuit = build_arrangement(self.style, self.circuit)
        self.assertTrue(any(note.articulation == "legato" and note.duration >= 960 for note in circuit.notes_by_role["lead"]))

    def test_patterns_keep_section_relative_phase_across_bar_boundaries(self) -> None:
        arrangement = build_arrangement(self.broken, self.glass)
        opening = arrangement.sections[0]
        fx_starts = [
            note.start - opening["start_tick"]
            for note in arrangement.notes_by_role["fx_trigger"]
            if opening["start_tick"] <= note.start < opening["end_tick"]
        ]
        self.assertEqual(fx_starts, [0, 3840])

    def test_event_order_ranges_overlap_policy_and_duration_hold(self) -> None:
        for style, song in ((self.style, self.circuit), (self.style, self.neon), (self.broken, self.glass), (self.style, self.signal)):
            arrangement = build_arrangement(style, song)
            for role, notes in arrangement.notes_by_role.items():
                self.assertEqual(notes, sorted(notes, key=lambda note: (note.start, note.pitch, note.duration)))
                for note in notes:
                    self.assertGreaterEqual(note.start, 0)
                    self.assertGreater(note.duration, 0)
                    self.assertLessEqual(note.start + note.duration, arrangement.total_ticks)
                    self.assertIn(note.pitch, range(128))
                    self.assertIn(note.velocity, range(1, 128))
                if style["roles"][role].get("overlap") == "monophonic":
                    self.assertTrue(all(right.start >= left.start + left.duration for left, right in zip(notes, notes[1:])))

    def test_invalid_timing_chords_ranges_and_v1_fail_closed(self) -> None:
        invalid = copy.deepcopy(self.glass)
        invalid["timeline"]["tempo"][1]["beat"] = "6"
        with self.assertRaisesRegex(ValidationError, "outside bar 9's 5/4 meter"):
            build_arrangement(self.broken, invalid)
        invalid = copy.deepcopy(self.glass)
        invalid["identity"]["harmony"][0]["quality"] = "mystery"
        with self.assertRaisesRegex(ValidationError, "unsupported chord quality"):
            validate_song(invalid, self.broken)
        invalid = copy.deepcopy(self.glass)
        invalid["identity"]["harmony"][0]["inversion"] = 99
        with self.assertRaisesRegex(ValidationError, "inversion must be between"):
            validate_song(invalid, self.broken)
        invalid = copy.deepcopy(self.glass)
        invalid["identity"]["motifs"]["glass"]["events"][0]["note"] = 128
        with self.assertRaisesRegex(ValidationError, "must be between"):
            validate_song(invalid, self.broken)
        invalid = copy.deepcopy(self.glass)
        invalid["schema"] = "songdna-song/v1"
        with self.assertRaisesRegex(ValidationError, "unsupported song schema"):
            validate_song(invalid, self.broken)

    def test_style_inheritance_is_single_parent_inspectable_and_cycle_safe(self) -> None:
        self.assertEqual(self.broken["lineage"], ["electro_house/v2", "broken_pulse/v2"])
        self.assertEqual(self.broken["roles"]["kick"]["pattern"], "broken_kick")
        self.assertIn("four_floor", self.broken["patterns"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for ident, parent in (("one/v2", "two/v2"), ("two/v2", "one/v2")):
                path = root / "styles" / ident / "style.toml"
                path.parent.mkdir(parents=True)
                path.write_text(f'schema = "songdna-style/v2"\nid = "{ident}"\nextends = "{parent}"\n')
            with self.assertRaisesRegex(ValidationError, "circular style inheritance"):
                resolve_style(root, "one/v2")

    def test_second_style_and_third_song_need_no_core_name_branch(self) -> None:
        arrangement = build_arrangement(self.broken, self.glass)
        self.assertGreater(sum(map(len, arrangement.notes_by_role.values())), 0)
        compiler = (ROOT / "src/songdna/compiler.py").read_text()
        self.assertNotIn("broken_pulse", compiler)
        self.assertNotIn("glass_transit", compiler)
        self.assertNotEqual(self.broken["sections"], self.style["sections"])
        self.assertNotEqual(self.broken["roles"]["kick"]["pattern"], self.style["roles"]["kick"]["pattern"])

    def test_composition_contract_excludes_production_fields(self) -> None:
        invalid = copy.deepcopy(self.glass)
        invalid["identity"]["synth_patch"] = "forbidden"
        with self.assertRaisesRegex(ValidationError, "unsupported fields"):
            validate_song(invalid, self.broken)
        invalid_style = copy.deepcopy(self.style)
        invalid_style["roles"]["lead"]["reverb"] = 0.5
        with self.assertRaisesRegex(ValidationError, "unsupported fields"):
            validate_style(invalid_style)

    @unittest.skipIf(jsonschema is None, "install .[test] to validate published schemas")
    def test_all_shipped_v2_fixtures_validate_against_published_schemas(self) -> None:
        style_validator = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas/style.schema.json").read_text()))
        song_validator = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas/song.schema.json").read_text()))
        production_validator = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas/production.schema.json").read_text()))
        for path in (BASE_STYLE, SECOND_STYLE):
            style_validator.validate(_load_toml(path))
        for path in SONGS:
            song_validator.validate(_load_toml(path))
            production_validator.validate(_load_toml(path.with_name("production.toml")))

    @unittest.skipIf(jsonschema is None, "install .[test] to exercise schema failures")
    def test_schema_and_runtime_fail_closed_on_ambiguous_or_production_leaking_dna(self) -> None:
        song_validator = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas/song.schema.json").read_text()))
        style_validator = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas/style.schema.json").read_text()))
        invalid_song = copy.deepcopy(self.glass)
        invalid_song["identity"]["harmony"][0]["root"] = "A"
        with self.assertRaises(jsonschema.ValidationError):
            song_validator.validate(invalid_song)
        with self.assertRaisesRegex(ValidationError, "exactly one"):
            validate_song(invalid_song, self.broken)
        invalid_song = copy.deepcopy(self.glass)
        invalid_song["identity"]["harmony"][0]["root"] = "H"
        invalid_song["identity"]["harmony"][0].pop("degree")
        with self.assertRaises(jsonschema.ValidationError):
            song_validator.validate(invalid_song)
        invalid_song = copy.deepcopy(self.glass)
        invalid_song["identity"]["motifs"]["glass"]["length"] = "not-a-rational"
        with self.assertRaises(jsonschema.ValidationError):
            song_validator.validate(invalid_song)
        invalid_style = copy.deepcopy(_load_toml(BASE_STYLE))
        invalid_style["roles"]["lead"]["plugin"] = "not-composition"
        with self.assertRaises(jsonschema.ValidationError):
            style_validator.validate(invalid_style)
        with self.assertRaisesRegex(ValidationError, "unsupported fields"):
            validate_style(invalid_style)

    def test_compiled_artifacts_are_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            shutil.copytree(ROOT / "styles", checkout / "styles")
            shutil.copytree(ROOT / "songs", checkout / "songs")
            first = compile_song("songs/glass_transit/song.toml", checkout)
            first_hashes = {path.name: digest(path) for path in first.output_dir.iterdir()}
            second = compile_song("songs/glass_transit/song.toml", checkout)
            second_hashes = {path.name: digest(path) for path in second.output_dir.iterdir()}
            self.assertEqual(first_hashes, second_hashes)
            self.assertEqual(first.total_notes, 582)


if __name__ == "__main__":
    unittest.main()
