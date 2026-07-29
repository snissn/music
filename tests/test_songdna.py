from __future__ import annotations

import copy
import hashlib
import json
import struct
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from songdna.compiler import _load_toml, build_arrangement, compile_song  # noqa: E402
from songdna.errors import ValidationError  # noqa: E402


STYLE_PATH = ROOT / "styles/electro_house/v1/style.toml"
CIRCUIT_PATH = ROOT / "songs/circuit_bloom/song.toml"
NEON_PATH = ROOT / "songs/neon_tides/song.toml"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SongDNATest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.style = _load_toml(STYLE_PATH)
        cls.circuit = _load_toml(CIRCUIT_PATH)
        cls.neon = _load_toml(NEON_PATH)

    def test_two_songs_are_materially_distinct_extensions(self) -> None:
        self.assertEqual(self.circuit["extends"], self.style["id"])
        self.assertEqual(self.neon["extends"], self.style["id"])
        self.assertNotEqual(self.circuit["song"]["tempo"], self.neon["song"]["tempo"])
        self.assertNotEqual(self.circuit["song"]["tonic"], self.neon["song"]["tonic"])
        self.assertNotEqual(self.circuit["identity"], self.neon["identity"])
        self.assertNotEqual(self.circuit["form"], self.neon["form"])

    def test_style_contains_grammar_not_song_identity(self) -> None:
        serialized = json.dumps(self.style)
        self.assertNotIn("motif_degrees", serialized)
        self.assertNotIn("chord_degrees", serialized)
        self.assertNotIn("circuit_bloom", serialized)
        self.assertNotIn("neon_tides", serialized)

    def test_arrangement_is_deterministic(self) -> None:
        first = build_arrangement(self.style, self.circuit)
        second = build_arrangement(self.style, self.circuit)
        self.assertEqual(first.notes_by_role, second.notes_by_role)
        self.assertEqual(first.markers, second.markers)

    def test_form_bar_counts_and_note_bounds(self) -> None:
        for song in (self.circuit, self.neon):
            with self.subTest(song=song["song"]["id"]):
                arrangement = build_arrangement(self.style, song)
                expected_bars = sum(section["bars"] for section in song["form"])
                self.assertEqual(expected_bars, arrangement.total_bars)
                self.assertEqual(len(song["form"]), len(arrangement.markers))
                self.assertGreater(sum(map(len, arrangement.notes_by_role.values())), 0)
                for notes in arrangement.notes_by_role.values():
                    for note in notes:
                        self.assertGreaterEqual(note.start, 0)
                        self.assertGreater(note.duration, 0)
                        self.assertLessEqual(note.start + note.duration, arrangement.total_ticks)
                        self.assertIn(note.pitch, range(128))
                        self.assertIn(note.velocity, range(1, 128))
                        self.assertIn(note.channel, range(16))

    def test_original_only_policy_rejects_external_audio(self) -> None:
        invalid = copy.deepcopy(self.circuit)
        invalid["sources"]["external_audio"] = ["borrowed.wav"]
        with self.assertRaisesRegex(ValidationError, "cannot declare external audio"):
            build_arrangement(self.style, invalid)

    def test_unknown_transform_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.circuit)
        invalid["form"][0]["transforms"] = ["mystery"]
        invalid["form"][0]["add_roles"] = ["lead"]
        with self.assertRaisesRegex(ValidationError, "unknown motif transformation"):
            build_arrangement(self.style, invalid)

    def test_compile_writes_valid_midi_container_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp_root = Path(temporary)
            (temp_root / "styles/electro_house/v1").mkdir(parents=True)
            (temp_root / "songs/circuit_bloom").mkdir(parents=True)
            (temp_root / "styles/electro_house/v1/style.toml").write_bytes(STYLE_PATH.read_bytes())
            (temp_root / "songs/circuit_bloom/song.toml").write_bytes(CIRCUIT_PATH.read_bytes())

            result = compile_song("songs/circuit_bloom/song.toml", temp_root)
            midi = result.midi_path.read_bytes()
            self.assertEqual(midi[:4], b"MThd")
            length, midi_format, track_count, division = struct.unpack(">IHHH", midi[4:14])
            self.assertEqual(length, 6)
            self.assertEqual(midi_format, 1)
            self.assertGreater(track_count, 1)
            self.assertEqual(division, 480)
            self.assertEqual(midi.count(b"MTrk"), track_count)
            self.assertTrue(result.marker_path.is_file())
            self.assertTrue(result.report_path.is_file())
            self.assertTrue(result.manifest_path.is_file())

    def test_compiled_artifacts_are_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp_root = Path(temporary)
            (temp_root / "styles/electro_house/v1").mkdir(parents=True)
            (temp_root / "songs/circuit_bloom").mkdir(parents=True)
            (temp_root / "styles/electro_house/v1/style.toml").write_bytes(STYLE_PATH.read_bytes())
            (temp_root / "songs/circuit_bloom/song.toml").write_bytes(CIRCUIT_PATH.read_bytes())
            first = compile_song("songs/circuit_bloom/song.toml", temp_root)
            first_hashes = {path.name: digest(path) for path in first.output_dir.iterdir()}
            second = compile_song("songs/circuit_bloom/song.toml", temp_root)
            second_hashes = {path.name: digest(path) for path in second.output_dir.iterdir()}
            self.assertEqual(first_hashes, second_hashes)


if __name__ == "__main__":
    unittest.main()

