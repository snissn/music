from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compiler import build_arrangement, compile_song, load_inputs
from .errors import SongDNAError
from .renderer import render_arrangement


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="songdna")
    subparsers = parser.add_subparsers(dest="command", required=True)
    compile_parser = subparsers.add_parser("compile", help="compile song DNA into interchange artifacts")
    compile_parser.add_argument("song", type=Path, help="path to a song TOML file")
    compile_parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    validate_parser = subparsers.add_parser("validate", help="validate song, style, and production DNA")
    validate_parser.add_argument("song", type=Path, help="path to a song TOML file")
    validate_parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    inspect_parser = subparsers.add_parser("inspect", help="inspect resolved SongDNA without writing artifacts")
    inspect_parser.add_argument("song", type=Path, help="path to a song TOML file")
    inspect_parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    render_parser = subparsers.add_parser("render", help="render deterministic original WAV stems and preview")
    render_parser.add_argument("song", type=Path, help="path to a song TOML file")
    render_parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    render_parser.add_argument("--stems", action="store_true", help="render stems only; omit preview WAV")
    render_parser.add_argument("--output", type=Path, help="output directory under generated/")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "compile":
            result = compile_song(args.song, args.root)
            payload = {
                "song_id": result.song_id,
                "output_dir": str(result.output_dir),
                "total_bars": result.total_bars,
                "total_notes": result.total_notes,
            }
        elif args.command == "render":
            root = args.root.resolve()
            song, style, production = load_inputs(args.song, root)
            arrangement = build_arrangement(style, song)
            generated_root = (root / "generated").resolve()
            output = (root / args.output).resolve() if args.output else generated_root / arrangement.song_id / "render"
            if not output.is_relative_to(generated_root):
                raise SongDNAError("render output must stay beneath generated/")
            result = render_arrangement(
                arrangement, style, production, output, args.stems
            )
            payload = {"song_id": arrangement.song_id, "output_dir": str(result.output_dir), "manifest": str(result.manifest_path), "preview": None if args.stems else str(result.preview_path)}
        else:
            song, style, production = load_inputs(args.song, args.root)
            if args.command == "validate":
                # Validate composition semantics as well as file-level contracts.
                build_arrangement(style, song)
                payload = {"valid": True, "song_id": song["song"]["id"], "style": style["id"], "production": production["schema"]}
            else:
                arrangement = build_arrangement(style, song)
                payload = {
                    "song_id": arrangement.song_id,
                    "title": arrangement.title,
                    "style": style["id"],
                    "production": production["schema"],
                    "total_bars": arrangement.total_bars,
                    "total_notes": sum(len(notes) for notes in arrangement.notes_by_role.values()),
                    "roles": sorted(style["roles"]),
                }
    except SongDNAError as exc:
        print(f"songdna: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
