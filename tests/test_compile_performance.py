from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CompilePerformanceGuardrailTest(unittest.TestCase):
    def test_representative_short_and_long_songs_stay_within_scaling_guardrails(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "benchmarks/compile_v2.py")],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            check=True, capture_output=True, text=True,
        )
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], "songdna-compile-benchmark/v1")
        self.assertEqual([row["song"] for row in report["results"]], ["glass_transit", "neon_tides", "circuit_bloom"])
        self.assertGreater(report["results"][-1]["event_count"], report["results"][0]["event_count"] * 5)
        self.assertGreater(report["results"][-1]["artifact_bytes"], report["results"][0]["artifact_bytes"])


if __name__ == "__main__":
    unittest.main()
