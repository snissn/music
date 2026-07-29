from __future__ import annotations

import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from songdna.compiler import _load_toml  # noqa: E402
from songdna.errors import SongDNAError, ValidationError  # noqa: E402
from songdna import mastering  # noqa: E402
from songdna.mastering import EXPECTED_FFMPEG_VERSION, EXPECTED_LAME_VERSION, _assert_pre_master, _atomic_replace, _mp3_stream_info, _qa, _sample_qa, _toolchain, _wrap_s24le_as_wav, master_pre_master  # noqa: E402


def _canonical_tools_available() -> bool:
    try:
        return (subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0].split()[2] == EXPECTED_FFMPEG_VERSION and subprocess.run(["ffprobe", "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0].split()[2] == EXPECTED_FFMPEG_VERSION and subprocess.run(["lame", "--version"], capture_output=True, text=True, check=True).stdout.split("version ")[1].split()[0] == EXPECTED_LAME_VERSION)
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

    def test_true_peak_gate_has_no_hidden_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._wav(Path(temporary), "valid.wav")
            with patch("songdna.mastering._loudness", return_value={"integrated_lufs": -14.0, "loudness_range_lu": 5.0, "true_peak_dbtp": -0.99, "threshold_lufs": -24.0}):
                self.assertIn("true_peak_ceiling", _qa(path, self.production["mastering"])["failures"])

    def test_rss_helper_is_safe_without_unix_resource_module(self) -> None:
        with patch.object(mastering, "resource", None):
            self.assertEqual(mastering._peak_rss(), (None, "unavailable: platform has no resource module"))

    def test_raw_s24le_wrapper_writes_classic_readable_wav_with_exact_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "master.s24le"
            raw.write_bytes((b"\x01\x00\x00\xff\xff\xff") * 4)
            wrapped = root / "master.wav"
            _wrap_s24le_as_wav(raw, wrapped, sample_rate=48_000, channels=2, expected_frames=4)
            with wave.open(str(wrapped), "rb") as handle:
                self.assertEqual((handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getnframes()), (2, 3, 48_000, 4))
            with self.assertRaisesRegex(ValidationError, "frame count changed"):
                _wrap_s24le_as_wav(raw, root / "wrong.wav", sample_rate=48_000, channels=2, expected_frames=5)

    def test_mp3_probe_reads_observed_stream_without_wav_conversion(self) -> None:
        probe = subprocess.CompletedProcess(["ffprobe"], 0, '{"streams":[{"channels":2,"sample_rate":"48000","duration":"12.048"}]}', "")
        with patch("songdna.mastering._tool_path", return_value="ffprobe"), patch("songdna.mastering._run", return_value=probe):
            self.assertEqual(_mp3_stream_info(Path("listening.mp3")), {"channels": 2, "sample_rate": 48_000, "duration_seconds": 12.048})

    def test_atomic_replace_preserves_unrelated_previous_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "master"; target.mkdir(); (target / "old").write_text("old")
            unrelated = root / "master.previous"; unrelated.mkdir(); (unrelated / "keep").write_text("keep")
            stage = root / "stage"; stage.mkdir(); (stage / "new").write_text("new")
            _atomic_replace(stage, target)
            self.assertEqual((target / "new").read_text(), "new")
            self.assertEqual((unrelated / "keep").read_text(), "keep")

    def test_toolchain_requires_gpl_configuration_for_both_ff_tools(self) -> None:
        ffmpeg = subprocess.CompletedProcess(["ffmpeg"], 0, "ffmpeg version 8.1.2\nconfiguration: --enable-gpl --enable-libmp3lame", "")
        ffprobe = subprocess.CompletedProcess(["ffprobe"], 0, "ffprobe version 8.1.2\nconfiguration: --enable-gpl", "")
        lame = subprocess.CompletedProcess(["lame"], 0, "LAME 64bits version 3.100", "")
        with patch("songdna.mastering._tool_path", side_effect=["ffmpeg", "ffprobe", "lame"]), patch("songdna.mastering._run", side_effect=[ffmpeg, ffprobe, lame]):
            self.assertIn("ffprobe", _toolchain())
        nongpl_probe = subprocess.CompletedProcess(["ffprobe"], 0, "ffprobe version 8.1.2\nconfiguration: --disable-gpl", "")
        with patch("songdna.mastering._tool_path", side_effect=["ffmpeg", "ffprobe", "lame"]), patch("songdna.mastering._run", side_effect=[ffmpeg, nongpl_probe, lame]):
            with self.assertRaisesRegex(SongDNAError, "GPL-configured"):
                _toolchain()

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
