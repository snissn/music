from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compiler import compile_song
from .errors import SongDNAError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="songdna")
    subparsers = parser.add_subparsers(dest="command", required=True)
    compile_parser = subparsers.add_parser("compile", help="compile song DNA into interchange artifacts")
    compile_parser.add_argument("song", type=Path, help="path to a song TOML file")
    compile_parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = compile_song(args.song, args.root)
    except SongDNAError as exc:
        print(f"songdna: error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "song_id": result.song_id,
                "output_dir": str(result.output_dir),
                "total_bars": result.total_bars,
                "total_notes": result.total_notes,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

