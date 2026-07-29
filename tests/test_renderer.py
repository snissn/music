from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from songdna.compiler import _load_toml, build_arrangement  # noqa: E402
from songdna.errors import ValidationError  # noqa: E402
from songdna.renderer import render_arrangement  # noqa: E402


STYLE_PATH = ROOT / "styles/electro_house/v1/style.toml"
SONG_PATH = ROOT / "songs/circuit_bloom/song.toml"
PRODUCTION_PATH = SONG_PATH.with_name("production.toml")


class RendererContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.style = _load_toml(STYLE_PATH)
        self.song = _load_toml(SONG_PATH)
        self.production = _load_toml(PRODUCTION_PATH)
        self.song["form"] = [{"kind": "drop", "bars": 1, "energy_start": 1.0, "energy_end": 1.0}]

    def test_one_bar_contract_is_backend_independent_and_aligned(self) -> None:
        arrangement = build_arrangement(self.style, self.song)
        with tempfile.TemporaryDirectory() as temporary:
            result = render_arrangement(arrangement, self.style, self.production, Path(temporary))
            manifest = json.loads(result.manifest_path.read_text())
            self.assertEqual(manifest["frame_count"], 90_000)  # 4 beats at 128 BPM, 48 kHz
            self.assertEqual(manifest["channel_layout"], {"stems": 1, "preview": 2})
            self.assertEqual(set(manifest["stems"]), set(self.style["roles"]))
            self.assertTrue(all(stem["frames"] == 90_000 for stem in manifest["stems"].values()))
            self.assertTrue(all(stem["non_silent"] for stem in manifest["stems"].values()))
            self.assertTrue(all(stem["peak"] <= 1.0 for stem in manifest["stems"].values()))
            for role in self.style["roles"]:
                with wave.open(str(result.output_dir / "stems" / f"{role}.wav"), "rb") as stem:
                    self.assertEqual(stem.getnchannels(), 1)
                    self.assertEqual(stem.getframerate(), 48_000)
                    self.assertEqual(stem.getsampwidth(), 3)
                    self.assertEqual(stem.getnframes(), 90_000)
            with wave.open(str(result.preview_path), "rb") as preview:
                self.assertEqual(preview.getnchannels(), 2)
                self.assertEqual(preview.getnframes(), 90_000)
            self.assertTrue(result.preview_path.is_file())
            self.assertEqual(manifest["renderer"]["adapter"], "songdna-renderer/v1")

    def test_repeat_render_is_byte_identical_including_manifest(self) -> None:
        arrangement = build_arrangement(self.style, self.song)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = render_arrangement(arrangement, self.style, self.production, root / "one")
            first_hashes = {path.relative_to(first.output_dir): hashlib.sha256(path.read_bytes()).hexdigest() for path in first.output_dir.rglob("*") if path.is_file()}
            second = render_arrangement(arrangement, self.style, self.production, root / "two")
            second_hashes = {path.relative_to(second.output_dir): hashlib.sha256(path.read_bytes()).hexdigest() for path in second.output_dir.rglob("*") if path.is_file()}
            self.assertEqual(first_hashes, second_hashes)

    def test_missing_mapping_fails_before_creating_output(self) -> None:
        arrangement = build_arrangement(self.style, self.song)
        production = copy.deepcopy(self.production)
        del production["role_map"]["lead"]
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "render"
            with self.assertRaisesRegex(ValidationError, "cover style roles exactly"):
                render_arrangement(arrangement, self.style, production, target)
            self.assertFalse(target.exists())

    def test_incompatible_backend_rate_fails_before_creating_output(self) -> None:
        arrangement = build_arrangement(self.style, self.song)
        production = copy.deepcopy(self.production)
        production["session"]["sample_rate"] = 44_100
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "render"
            with self.assertRaisesRegex(ValidationError, "requires sample_rate 48000"):
                render_arrangement(arrangement, self.style, production, target)
            self.assertFalse(target.exists())
