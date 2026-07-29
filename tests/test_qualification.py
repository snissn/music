from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from songdna.errors import ValidationError
from songdna.qualification import (
    LISTENING_SCHEMA,
    _check_artifacts,
    _check_deterministic_repeat,
    _check_performance,
    build_qualification,
    validate_qualification,
    validate_listening_review,
)


class QualificationContractTest(unittest.TestCase):
    def test_build_preflight_preserves_unowned_render_and_master_outputs(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        for output_name in ("render", "master"):
            with self.subTest(output_name=output_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                shutil.copytree(source_root / "qualification", root / "qualification")
                shutil.copytree(source_root / "songs", root / "songs")
                shutil.copytree(source_root / "styles", root / "styles")
                output = root / "generated/circuit_bloom" / output_name
                output.mkdir(parents=True)
                sentinel = output / "human-recording.wav"
                sentinel.write_bytes(b"keep")
                with self.assertRaisesRegex(ValidationError, "output is unowned"):
                    build_qualification(root)
                self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_artifact_ledger_fails_on_missing_or_tampered_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "generated/song/master/listening.mp3"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"first")
            evidence = {
                artifact.relative_to(root).as_posix(): {
                    "bytes": 5,
                    "sha256": hashlib.sha256(b"first").hexdigest(),
                    "role": "listening_mp3",
                }
            }
            _check_artifacts(root, evidence)
            artifact.write_bytes(b"changed")
            with self.assertRaisesRegex(ValidationError, "size mismatch"):
                _check_artifacts(root, evidence)
            artifact.unlink()
            with self.assertRaisesRegex(ValidationError, "missing"):
                _check_artifacts(root, evidence)

    def test_stale_plan_hash_fails_before_evidence_is_trusted(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(source_root / "qualification", root / "qualification")
            shutil.copytree(source_root / "songs", root / "songs")
            shutil.copytree(source_root / "styles", root / "styles")
            ledger = root / "generated/qualification/ledger.json"
            ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({
                "schema": "songdna-qualification-ledger/v1",
                "status": "automated_pass",
                "plan": {"path": "qualification/plan.json", "sha256": "stale"},
                "songs": {},
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "plan hash is stale"):
                validate_qualification(root, automated_only=True)

    def test_deterministic_repeat_mismatch_fails(self) -> None:
        _check_deterministic_repeat({"a": "1"}, {"a": "1"})
        with self.assertRaisesRegex(ValidationError, "deterministic repeat mismatch"):
            _check_deterministic_repeat({"a": "1"}, {"a": "2"})

    def test_performance_guardrails_fail_closed(self) -> None:
        guardrails = {
            "compile_max_seconds": 1.0,
            "render_max_realtime_ratio": 1.0,
            "master_max_realtime_ratio": 1.0,
            "peak_rss_max_bytes": 100,
            "song_output_max_bytes": 100,
        }
        passing = {
            "compile_seconds": 0.1,
            "render_seconds": 1.0,
            "master_seconds": 1.0,
            "duration_seconds": 2.0,
            "peak_rss_bytes": 50,
            "output_bytes": 50,
        }
        _check_performance("fixture", passing, guardrails)
        failing = dict(passing, render_seconds=2.1)
        with self.assertRaisesRegex(ValidationError, "render performance guardrail"):
            _check_performance("fixture", failing, guardrails)

    def test_listening_review_is_pending_until_real_complete_signoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected: dict[str, dict[str, str]] = {}
            songs: dict[str, object] = {}
            for song_id in ("circuit_bloom", "neon_tides", "glass_transit"):
                relative = f"generated/{song_id}/master/listening.mp3"
                path = root / relative
                path.parent.mkdir(parents=True)
                path.write_bytes(song_id.encode())
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                expected[song_id] = {"path": relative, "sha256": digest}
                songs[song_id] = {
                    "listening_mp3": relative,
                    "listening_mp3_sha256": digest,
                    "checks": {
                        name: {"complete": False, "findings": ""}
                        for name in ("headphones", "speakers", "mono", "low_volume")
                    },
                    "musical_findings": {
                        name: "" for name in (
                            "arrangement", "distinctness", "tonal_balance_and_dynamics",
                            "artifacts_and_transitions", "translation",
                        )
                    },
                    "verdict": "pending",
                }
            review = {
                "schema": LISTENING_SCHEMA,
                "reviewer": "",
                "reviewed_at": "",
                "songs": songs,
                "signoff": {"complete": False, "statement": ""},
            }
            with self.assertRaisesRegex(ValidationError, "human listening review pending"):
                validate_listening_review(review, expected, require_complete=True)
            self.assertEqual(
                validate_listening_review(review, expected, require_complete=False),
                "pending",
            )
            review["reviewer"] = "Human Reviewer"
            review["reviewed_at"] = "2026-07-28"
            review["signoff"] = {"complete": True, "statement": "All three originals passed translation review."}
            for evidence in review["songs"].values():
                evidence["verdict"] = "pass"
                for check in evidence["checks"].values():
                    check.update(complete=True, findings="No blocking issue heard.")
                for key in evidence["musical_findings"]:
                    evidence["musical_findings"][key] = "Reviewed; no blocking issue heard."
            self.assertEqual(validate_listening_review(review, expected, require_complete=True), "pass")


if __name__ == "__main__":
    unittest.main()
