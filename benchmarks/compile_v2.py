#!/usr/bin/env python3
"""Measure the stable SongDNA v2 compile fixtures and enforce broad guardrails."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from songdna.compiler import compile_song  # noqa: E402


SONGS = ("glass_transit", "neon_tides", "circuit_bloom")


def peak_rss_bytes() -> int:
    try:
        import resource
    except ImportError:
        return 0
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def measure(song: str) -> dict[str, int | float | str]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        shutil.copytree(ROOT / "styles", root / "styles")
        source = ROOT / "songs" / song
        shutil.copytree(source, root / "songs" / song)
        started = time.perf_counter()
        result = compile_song(f"songs/{song}/song.toml", root)
        elapsed = time.perf_counter() - started
        artifact_bytes = sum(path.stat().st_size for path in result.output_dir.iterdir() if path.is_file())
        return {
            "song": song,
            "bars": result.total_bars,
            "wall_seconds": round(elapsed, 6),
            "event_count": result.total_notes,
            "peak_rss_bytes": peak_rss_bytes(),
            "artifact_bytes": artifact_bytes,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--song", choices=SONGS)
    args = parser.parse_args()
    if args.song:
        print(json.dumps(measure(args.song), sort_keys=True))
        return 0
    rows = []
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    for song in SONGS:
        completed = subprocess.run(
            [sys.executable, __file__, "--song", song], check=True,
            capture_output=True, text=True, env=env,
        )
        rows.append(json.loads(completed.stdout))
    for row in rows:
        if row["wall_seconds"] > 5.0:
            raise SystemExit(f"compile guardrail exceeded 5 seconds: {row}")
        if row["event_count"] > row["bars"] * 64:
            raise SystemExit(f"event expansion guardrail exceeded 64 events/bar: {row}")
        if row["artifact_bytes"] > 1_000_000 + row["event_count"] * 1024:
            raise SystemExit(f"artifact scaling guardrail exceeded: {row}")
        if row["peak_rss_bytes"] and row["peak_rss_bytes"] > 256 * 1024 * 1024:
            raise SystemExit(f"peak RSS guardrail exceeded 256 MiB: {row}")
    print(json.dumps({"schema": "songdna-compile-benchmark/v1", "results": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
