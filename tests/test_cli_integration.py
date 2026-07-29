from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstalledCLIIntegrationTest(unittest.TestCase):
    def test_clean_venv_installs_and_runs_all_commands_for_both_songs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            checkout = work / "checkout"
            shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns(".git", ".venv", "generated", "*.egg-info", "__pycache__"))
            venv = work / "venv"
            subprocess.run([sys.executable, "-m", "venv", venv], check=True)
            python = venv / "bin" / "python"
            songdna = venv / "bin" / "songdna"
            clean_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            subprocess.run([python, "-m", "pip", "install", "."], cwd=checkout, env=clean_env, check=True, capture_output=True, text=True)
            for song in ("circuit_bloom", "neon_tides"):
                for command in ("validate", "inspect", "compile"):
                    result = subprocess.run([songdna, command, f"songs/{song}/song.toml"], cwd=checkout, env=clean_env, check=True, capture_output=True, text=True)
                    self.assertEqual(json.loads(result.stdout)["song_id"], song)
            self.assertIn("compiler", json.loads((checkout / "generated/circuit_bloom/manifest.json").read_text()))

    def test_invalid_production_fails_closed_with_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "styles", root / "styles")
            shutil.copytree(ROOT / "songs", root / "songs")
            production = root / "songs/circuit_bloom/production.toml"
            production.write_text(production.read_text().replace("[role_map.lead]", "[role_map.lead]\nunsafe = true"))
            result = subprocess.run([sys.executable, "-m", "songdna", "validate", "songs/circuit_bloom/song.toml", "--root", str(root)], env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("unsupported fields", result.stderr)

    def test_validate_rejects_semantically_invalid_transform_with_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "styles", root / "styles")
            shutil.copytree(ROOT / "songs", root / "songs")
            song = root / "songs/circuit_bloom/song.toml"
            song.write_text(song.read_text().replace('transforms = ["diminish2"]', 'transforms = ["not-a-transform"]'))
            result = subprocess.run([sys.executable, "-m", "songdna", "validate", "songs/circuit_bloom/song.toml", "--root", str(root)], env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("unknown motif transformation", result.stderr)
