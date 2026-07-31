from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compiler import build_arrangement, compile_song, load_inputs
from .errors import SongDNAError
from .handoff import create_handoff, handoff_drift, validate_handoff_bundle
from .renderer import render_arrangement
from .mastering import master_pre_master
from .qualification import qualify


def _render_output_path(root: Path, requested: Path | None, song_id: str) -> Path:
    generated_root = (root / "generated").resolve()
    output = (root / requested).resolve() if requested else generated_root / song_id / "render"
    if output == generated_root or not output.is_relative_to(generated_root):
        raise SongDNAError("render output must be strictly beneath generated/")
    if output.exists():
        manifest_path = output / "render-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SongDNAError("refusing to replace non-render output directory") from exc
        if (
            manifest.get("schema") != "songdna-render-manifest/v1"
            or manifest.get("song_id") != song_id
            or not isinstance(manifest.get("renderer"), dict)
            or not isinstance(manifest.get("stems"), dict)
        ):
            raise SongDNAError("refusing to replace non-render output directory")
    return output


def _master_output_path(root: Path, requested: Path | None, song_id: str) -> Path:
    generated_root = (root / "generated").resolve()
    output = (root / requested).resolve() if requested else generated_root / song_id / "master"
    if output == generated_root or not output.is_relative_to(generated_root):
        raise SongDNAError("master output must be strictly beneath generated/")
    if output.exists():
        try:
            manifest = json.loads((output / "delivery-manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SongDNAError("refusing to replace non-master output directory") from exc
        if manifest.get("schema") != "songdna-delivery-manifest/v1" or manifest.get("song_id") != song_id:
            raise SongDNAError("refusing to replace non-master output directory")
    return output


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
    render_parser.add_argument("--backend", choices=("builtin", "csound"), default="builtin", help="audio backend; Csound upgrades bass, harmony, and lead")
    master_parser = subparsers.add_parser("master", help="master a rendered pre-master into WAV, MP3, QA, and provenance")
    master_parser.add_argument("song", type=Path, help="path to a song TOML file")
    master_parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    master_parser.add_argument("--pre-master", type=Path, help="pre-master WAV, default generated/<song>/render/preview.wav")
    master_parser.add_argument("--output", type=Path, help="output directory under generated/")
    handoff_parser = subparsers.add_parser("handoff", help="build a self-validating Ardour handoff bundle")
    handoff_parser.add_argument("song", type=Path, help="path to a song TOML file")
    handoff_parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    handoff_validate_parser = subparsers.add_parser("handoff-validate", help="validate an Ardour handoff bundle")
    handoff_validate_parser.add_argument("bundle", type=Path, help="path to an Ardour handoff bundle")
    handoff_drift_parser = subparsers.add_parser("handoff-drift", help="compare a bundle with a bootstrapped Ardour session")
    handoff_drift_parser.add_argument("bundle", type=Path, help="path to an Ardour handoff bundle")
    handoff_drift_parser.add_argument("session", type=Path, help="path to the Ardour session directory")
    qualify_parser = subparsers.add_parser("qualify", help="build and validate the three-song qualification ledger")
    qualify_parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    qualify_parser.add_argument("--plan", default="qualification/plan.json", help="qualification plan path relative to root")
    qualify_parser.add_argument("--validate-only", action="store_true", help="validate existing artifacts and ledger without rebuilding")
    qualify_parser.add_argument("--automated-only", action="store_true", help="pass machine gates while reporting human listening as pending")
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
            output = _render_output_path(root, args.output, arrangement.song_id)
            result = render_arrangement(
                arrangement, style, production, output, args.stems, args.backend
            )
            payload = {"song_id": arrangement.song_id, "output_dir": str(result.output_dir), "manifest": str(result.manifest_path), "preview": None if args.stems else str(result.preview_path)}
        elif args.command == "master":
            root = args.root.resolve()
            song, style, production = load_inputs(args.song, root)
            pre_master = (root / args.pre_master).resolve() if args.pre_master else root / "generated" / song["song"]["id"] / "render" / "preview.wav"
            output = _master_output_path(root, args.output, song["song"]["id"])
            result = master_pre_master(pre_master, production, output)
            payload = {"song_id": song["song"]["id"], "output_dir": str(result.output_dir), "manifest": str(result.manifest_path), "master": str(result.master_path), "mp3": str(result.mp3_path)}
        elif args.command == "handoff":
            result = create_handoff(args.song, args.root)
            payload = {
                "song_id": result.song_id,
                "output_dir": str(result.output_dir),
                "manifest": str(result.manifest_path),
            }
        elif args.command == "handoff-validate":
            manifest = validate_handoff_bundle(args.bundle)
            payload = {
                "valid": True,
                "song_id": manifest["song_id"],
                "source_fingerprint": manifest["source_fingerprint"],
                "supported_ardour": manifest["supported_ardour"],
            }
        elif args.command == "handoff-drift":
            payload = handoff_drift(args.bundle, args.session)
        elif args.command == "qualify":
            payload = qualify(
                args.root,
                args.plan,
                validate_only=args.validate_only,
                automated_only=args.automated_only,
            )
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
