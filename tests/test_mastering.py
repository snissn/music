from __future__ import annotations

import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from songdna.compiler import _load_toml  # noqa: E402
from songdna.errors import ValidationError  # noqa: E402
from songdna.mastering import EXPECTED_FFMPEG_VERSION, EXPECTED_LAME_VERSION, _assert_pre_master, _sample_qa, master_pre_master  # noqa: E402


def _canonical_tools_available() -> bool:
    try:
        return (subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0].split()[2] == EXPECTED_FFMPEG_VERSION and subprocess.run(["lame", "--version"], capture_output=True, text=True, check=True).stdout.split("version ")[1].split()[0] == EXPECTED_LAME_VERSION)
    except (FileNotFoundError, IndexError, subprocess.CalledProcessError):
        return False


class MasteringFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.production = _load_toml(ROOT / "songs/circuit_bloom/production.toml")

    def _wav(self, root: Path, name: str, *, rate: int = 48000, channels: int = 2, amplitude: float = 0.2) -> Path:
        path = root / name
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels); handle.setsampwidth(3); handle.setframerate(rate)
            payload = bytearray()
            for index in range(rate):
                value = round(amplitude * math.sin(index * 2 * math.pi * 440 / rate) * 8_388_607)
                encoded = value.to_bytes(4, "little", signed=True)[:3]
                payload.extend(encoded * channels)
            handle.writeframes(payload)
        return path

    def test_wrong_format_fails_before_tool_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._wav(Path(temporary), "wrong.wav", rate=44_100)
            with self.assertRaisesRegex(ValidationError, "sample_rate"):
                _assert_pre_master(path, self.production["mastering"])

    def test_silence_and_clipping_are_rejected_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            silent = self._wav(root, "silent.wav", amplitude=0.0)
            clipped = self._wav(root, "clipped.wav", amplitude=1.0)
            self.assertTrue(_sample_qa(silent)["silent"])
            self.assertGreaterEqual(_sample_qa(clipped)["sample_peak_dbfs"], -0.01)
            with self.assertRaisesRegex(ValidationError, "pre-master QA failed: silence"):
                master_pre_master(silent, self.production, root / "silent-master")
            with self.assertRaisesRegex(ValidationError, "pre-master QA failed: clipping"):
                master_pre_master(clipped, self.production, root / "clipped-master")

    @unittest.skipUnless(_canonical_tools_available(), "exact canonical FFmpeg/LAME tools are unavailable")
    def test_valid_fixture_exports_traceable_decodable_wav_and_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = master_pre_master(self._wav(root, "pre.wav"), self.production, root / "master")
            self.assertTrue(result.master_path.is_file()); self.assertTrue(result.mp3_path.is_file())
            manifest = __import__("json").loads(result.manifest_path.read_text())
            self.assertEqual(manifest["master"]["qa"], "qa.json")
            self.assertEqual(manifest["listening_mp3"]["source_master_sha256"], manifest["master"]["sha256"])
            self.assertTrue(__import__("json").loads((result.output_dir / "qa.json").read_text())["releaseable"])
