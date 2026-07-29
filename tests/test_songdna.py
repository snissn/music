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

from songdna.compiler import _load_toml, build_arrangement, compile_song, load_inputs  # noqa: E402
from songdna.errors import ValidationError  # noqa: E402
from songdna.validation import validate_production, validate_song, validate_style  # noqa: E402

try:
    import jsonschema
except ImportError:  # Optional test-only dependency; CI installs it via .[test].
    jsonschema = None


STYLE_PATH = ROOT / "styles/electro_house/v1/style.toml"
CIRCUIT_PATH = ROOT / "songs/circuit_bloom/song.toml"
NEON_PATH = ROOT / "songs/neon_tides/song.toml"
PRODUCTION_SCHEMA_PATH = ROOT / "schemas/production.schema.json"


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

    def test_production_contract_requires_exact_role_coverage_and_safe_origins(self) -> None:
        production = _load_toml(CIRCUIT_PATH.with_name("production.toml"))
        validate_production(production, self.circuit, self.style)
        invalid = copy.deepcopy(production)
        del invalid["role_map"]["lead"]
        with self.assertRaisesRegex(ValidationError, "cover style roles exactly"):
            validate_production(invalid, self.circuit, self.style)
        invalid = copy.deepcopy(production)
        invalid["role_map"]["lead"]["origin"] = "third_party_sample"
        with self.assertRaisesRegex(ValidationError, "unsafe origin"):
            validate_production(invalid, self.circuit, self.style)
        invalid = copy.deepcopy(production)
        invalid["session"]["sample_rate"] = "48000"
        with self.assertRaisesRegex(ValidationError, "sample_rate must be an integer"):
            validate_production(invalid, self.circuit, self.style)
        invalid = copy.deepcopy(production)
        invalid["session"]["bit_depth"] = "24"
        with self.assertRaisesRegex(ValidationError, "bit_depth must be an integer"):
            validate_production(invalid, self.circuit, self.style)

    def test_published_contract_versions_match_runtime_contracts(self) -> None:
        for name, expected in (("song.schema.json", "songdna-song/v1"), ("style.schema.json", "songdna-style/v1"), ("production.schema.json", "songdna-production/v2")):
            schema = json.loads((ROOT / "schemas" / name).read_text())
            self.assertEqual(schema["properties"]["schema"]["const"], expected)
        self.assertTrue(PRODUCTION_SCHEMA_PATH.is_file())

    def test_runtime_fails_closed_on_fields_forbidden_by_published_schemas(self) -> None:
        invalid_song = copy.deepcopy(self.circuit)
        invalid_song["unpublished_extension"] = True
        with self.assertRaisesRegex(ValidationError, "unsupported fields"):
            validate_song(invalid_song, self.style)
        invalid_style = copy.deepcopy(self.style)
        invalid_style["roles"]["lead"]["unpublished_extension"] = True
        with self.assertRaisesRegex(ValidationError, "unsupported fields"):
            validate_style(invalid_style)

    def test_generator_specific_contracts_fail_closed(self) -> None:
        invalid = copy.deepcopy(self.style)
        del invalid["roles"]["kick"]["note"]
        with self.assertRaisesRegex(ValidationError, "role kick.note"):
            validate_style(invalid)
        invalid = copy.deepcopy(self.style)
        invalid["roles"]["kick"]["offsets"] = ["not-a-number"]
        with self.assertRaisesRegex(ValidationError, r"offsets\[0\]"):
            validate_style(invalid)

    def test_late_role_notes_are_clipped_to_the_final_bar(self) -> None:
        style = copy.deepcopy(self.style)
        style["sections"]["intro"]["roles"] = ["kick", "bass"]
        for role in ("kick", "bass"):
            style["roles"][role]["offsets"] = [3.9]
            style["roles"][role]["duration"] = 1.0
        song = copy.deepcopy(self.circuit)
        song["form"] = [{"kind": "intro", "bars": 1, "energy_start": 1.0, "energy_end": 1.0}]
        arrangement = build_arrangement(style, song)
        self.assertTrue(arrangement.notes_by_role["kick"])
        self.assertTrue(arrangement.notes_by_role["bass"])
        for notes in arrangement.notes_by_role.values():
            for note in notes:
                self.assertLessEqual(note.start + note.duration, arrangement.total_ticks)

    def test_form_role_overrides_reject_unknown_names(self) -> None:
        invalid = copy.deepcopy(self.circuit)
        invalid["form"][0]["remove_roles"] = ["kikc"]
        with self.assertRaisesRegex(ValidationError, "remove_roles references unknown roles: kikc"):
            validate_song(invalid, self.style)

    def test_style_path_traversal_is_rejected_before_loading_style_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            song_path = root / "song.toml"
            song_path.write_text("extends = \"../../outside\"\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "invalid style id"):
                load_inputs(song_path, root)

    @unittest.skipIf(jsonschema is None, "install .[test] to validate published JSON Schemas")
    def test_shipped_toml_fixtures_validate_against_published_json_schemas(self) -> None:
        fixtures = (
            (ROOT / "schemas/style.schema.json", [STYLE_PATH]),
            (ROOT / "schemas/song.schema.json", [CIRCUIT_PATH, NEON_PATH]),
            (ROOT / "schemas/production.schema.json", [CIRCUIT_PATH.with_name("production.toml"), NEON_PATH.with_name("production.toml")]),
        )
        for schema_path, paths in fixtures:
            validator = jsonschema.Draft202012Validator(json.loads(schema_path.read_text()))
            for path in paths:
                with self.subTest(schema=schema_path.name, fixture=path):
                    validator.validate(_load_toml(path))

    @unittest.skipIf(jsonschema is None, "install .[test] to compare runtime and JSON Schema contracts")
    def test_runtime_and_schema_share_representative_v1_boundaries(self) -> None:
        def assert_pair(schema_name: str, payload: dict, runtime, valid: bool) -> None:
            validator = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas" / schema_name).read_text()))
            if valid:
                runtime(payload)
                validator.validate(payload)
            else:
                with self.assertRaises(ValidationError):
                    runtime(payload)
                with self.assertRaises(jsonschema.ValidationError):
                    validator.validate(payload)

        style = copy.deepcopy(self.style)
        style["name"] = "Electro House"
        style["defaults"].update({"meter_denominator": 32, "velocity_jitter": 0})
        assert_pair("style.schema.json", style, validate_style, True)
        style["defaults"]["meter_denominator"] = 64
        assert_pair("style.schema.json", style, validate_style, False)
        style = copy.deepcopy(self.style)
        style["roles"]["lead"]["gate"] = 0
        assert_pair("style.schema.json", style, validate_style, False)
        style = copy.deepcopy(self.style)
        style["roles"]["lead"]["note"] = "not-a-note"
        assert_pair("style.schema.json", style, validate_style, False)
        style = copy.deepcopy(self.style)
        style["roles"]["kick"]["offsets"] = [-0.1]
        assert_pair("style.schema.json", style, validate_style, False)

        song = copy.deepcopy(self.circuit)
        song["song"].update({"meter_numerator": 1, "meter_denominator": 32})
        assert_pair("song.schema.json", song, lambda value: validate_song(value, self.style), True)
        song["identity"]["motif_durations"][0] = "not-a-number"
        assert_pair("song.schema.json", song, lambda value: validate_song(value, self.style), False)
        song = copy.deepcopy(self.circuit)
        song["sources"]["entries"][0]["owner"] = " "
        assert_pair("song.schema.json", song, lambda value: validate_song(value, self.style), False)

        production = _load_toml(CIRCUIT_PATH.with_name("production.toml"))
        assert_pair("production.schema.json", production, lambda value: validate_production(value, self.circuit, self.style), True)
        production = copy.deepcopy(production)
        production["role_map"]["lead"]["description"] = " "
        assert_pair("production.schema.json", production, lambda value: validate_production(value, self.circuit, self.style), False)

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
            (temp_root / "songs/circuit_bloom/production.toml").write_bytes(CIRCUIT_PATH.with_name("production.toml").read_bytes())

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
            manifest = json.loads(result.manifest_path.read_text())
            self.assertEqual(manifest["compiler"]["name"], "songdna")
            self.assertEqual(manifest["compiler"]["version"], "0.1.0")

    def test_compiled_artifacts_are_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp_root = Path(temporary)
            (temp_root / "styles/electro_house/v1").mkdir(parents=True)
            (temp_root / "songs/circuit_bloom").mkdir(parents=True)
            (temp_root / "styles/electro_house/v1/style.toml").write_bytes(STYLE_PATH.read_bytes())
            (temp_root / "songs/circuit_bloom/song.toml").write_bytes(CIRCUIT_PATH.read_bytes())
            (temp_root / "songs/circuit_bloom/production.toml").write_bytes(CIRCUIT_PATH.with_name("production.toml").read_bytes())
            first = compile_song("songs/circuit_bloom/song.toml", temp_root)
            first_hashes = {path.name: digest(path) for path in first.output_dir.iterdir()}
            second = compile_song("songs/circuit_bloom/song.toml", temp_root)
            second_hashes = {path.name: digest(path) for path in second.output_dir.iterdir()}
            self.assertEqual(first_hashes, second_hashes)


if __name__ == "__main__":
    unittest.main()
