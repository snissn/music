"""End-to-end qualification ledger for the three original reference songs.

This module deliberately orchestrates the existing compiler, renderer,
masterer, and Ardour handoff.  It does not add another audio implementation.
"""
from __future__ import annotations

import gc
import hashlib
import json
import math
import platform
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, TypeVar
import wave

from .compiler import build_arrangement, compile_song, load_inputs
from .errors import SongDNAError, ValidationError
from .handoff import (
    HANDOFF_SCHEMA,
    SUPPORTED_ARDOUR_VERSION,
    create_handoff,
    validate_handoff_bundle,
)
from .mastering import (
    EXPECTED_FFMPEG_VERSION,
    EXPECTED_LAME_VERSION,
    _toolchain as _mastering_toolchain,
    master_pre_master,
)
from .renderer import render_arrangement

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on Windows
    resource = None  # type: ignore[assignment]


PLAN_SCHEMA = "songdna-qualification-plan/v1"
LEDGER_SCHEMA = "songdna-qualification-ledger/v1"
LISTENING_SCHEMA = "songdna-listening-review/v1"
QUALIFICATION_SONGS = {"circuit_bloom", "neon_tides", "glass_transit"}
LISTENING_CHECKS = {"headphones", "speakers", "mono", "low_volume"}
MUSICAL_FINDINGS = {
    "arrangement",
    "distinctness",
    "tonal_balance_and_dynamics",
    "artifacts_and_transitions",
    "translation",
}

T = TypeVar("T")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"unable to read qualification JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"qualification JSON must be an object: {path}")
    return value


def _relative_file(root: Path, relative: str, context: str) -> Path:
    root = root.resolve()
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ValidationError(f"{context} path is not safe and relative: {relative}")
    path = root.joinpath(*pure.parts)
    if path.is_symlink():
        raise ValidationError(f"{context} must not be a symlink: {relative}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValidationError(f"{context} escapes the repository root: {relative}")
    return resolved


def _timed(call: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter()
    result = call()
    return result, time.perf_counter() - started


def _qualification_timed(song_id: str, stage: str, call: Callable[[], T]) -> tuple[T, float]:
    try:
        return _timed(call)
    except SongDNAError as exc:
        raise ValidationError(f"qualification {song_id} {stage} failed: {exc}") from exc


def _source_revision(root: Path) -> dict[str, str]:
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValidationError("qualification requires a readable Git checkout") from exc
    if Path(top).resolve() != root.resolve() or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValidationError("qualification root must be the exact Git worktree root")
    if status.strip():
        raise ValidationError("qualification requires a clean tracked Git worktree")
    return {"git_commit": commit, "tracked_worktree": "clean"}


def _canonical_build_environment(plan: dict[str, Any]) -> dict[str, Any]:
    supported = plan["supported_environment"]
    macos_version = platform.mac_ver()[0]
    try:
        macos_major = int(macos_version.split(".", 1)[0])
    except (ValueError, IndexError):
        macos_major = -1
    if (
        sys.platform != "darwin"
        or sys.version_info[:2] != (3, 11)
        or macos_major != supported["macos_major"]
    ):
        raise ValidationError("qualification builds require the canonical macOS 14/Python 3.11 environment")
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "macos_version": macos_version,
        "macos_major": macos_major,
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def _check_recorded_environment(plan: dict[str, Any], environment: Any) -> None:
    supported = plan["supported_environment"]
    if not isinstance(environment, dict):
        raise ValidationError("qualification build environment evidence is missing")
    python_version = environment.get("python")
    macos_version = environment.get("macos_version")
    tools = environment.get("mastering_toolchain")
    if (
        not isinstance(python_version, str)
        or python_version.split(".")[:2] != supported["python"].split(".")
        or environment.get("implementation") != "CPython"
        or environment.get("system") != "Darwin"
        or environment.get("macos_major") != supported["macos_major"]
        or not isinstance(macos_version, str)
        or macos_version.split(".", 1)[0] != str(supported["macos_major"])
        or tools != {"ffmpeg": EXPECTED_FFMPEG_VERSION, "lame": EXPECTED_LAME_VERSION}
    ):
        raise ValidationError("qualification build environment evidence is stale or unsupported")


def _peak_rss_bytes() -> int | None:
    if resource is None:
        return None
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _load_plan(root: Path, relative: str = "qualification/plan.json") -> tuple[Path, dict[str, Any]]:
    path = _relative_file(root, relative, "qualification plan")
    plan = _json(path)
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValidationError(f"unsupported qualification plan schema: {plan.get('schema')}")
    songs = plan.get("songs")
    if not isinstance(songs, list) or len(songs) != 3 or not all(isinstance(item, dict) for item in songs) or {item.get("id") for item in songs} != QUALIFICATION_SONGS:
        raise ValidationError("qualification plan must contain exactly the three reference songs")
    if sum(bool(item.get("ardour_handoff")) for item in songs) != 1 or not next(
        item for item in songs if item.get("id") == "circuit_bloom"
    ).get("ardour_handoff"):
        raise ValidationError("qualification plan must assign the Ardour handoff to Circuit Bloom")
    supported = plan.get("supported_environment")
    if (
        not isinstance(supported, dict)
        or supported.get("ci_host") != "macos-14"
        or supported.get("macos_major") != 14
        or supported.get("python") != "3.11"
        or supported.get("ffmpeg") != EXPECTED_FFMPEG_VERSION
        or supported.get("lame") != EXPECTED_LAME_VERSION
    ):
        raise ValidationError("qualification plan mastering toolchain is stale")
    guardrails = plan.get("performance_guardrails")
    required = {
        "compile_max_seconds", "render_max_realtime_ratio", "master_max_realtime_ratio",
        "peak_rss_max_bytes", "song_output_max_bytes",
    }
    if not isinstance(guardrails, dict) or set(guardrails) != required or any(
        isinstance(guardrails[key], bool) or not isinstance(guardrails[key], (int, float)) or guardrails[key] <= 0
        for key in required
    ):
        raise ValidationError("qualification performance guardrails are missing or invalid")
    for item in songs:
        if set(item) != {"id", "song", "style", "ardour_handoff"}:
            raise ValidationError("qualification song declarations have unsupported or missing fields")
        _relative_file(root, str(item["song"]), f"qualification song {item['id']}")
    for field in ("artifact_root", "ledger", "listening_review", "listening_template"):
        if not isinstance(plan.get(field), str):
            raise ValidationError(f"qualification plan {field} is missing")
        _relative_file(root, plan[field], f"qualification {field}")
    if plan["artifact_root"] != "generated":
        raise ValidationError("qualification v1 artifact root must be generated")
    return path, plan


def _artifact_entry(root: Path, path: Path, role: str) -> tuple[str, dict[str, Any]]:
    relative = path.resolve().relative_to(root).as_posix()
    return relative, {"role": role, "bytes": path.stat().st_size, "sha256": _sha256(path)}


def _check_artifacts(root: Path, evidence: dict[str, Any]) -> None:
    if not isinstance(evidence, dict) or not evidence:
        raise ValidationError("qualification artifact ledger is empty")
    for relative, metadata in evidence.items():
        path = _relative_file(root, relative, "qualification artifact")
        if not path.is_file():
            raise ValidationError(f"qualification artifact is missing: {relative}")
        if not isinstance(metadata, dict) or metadata.get("bytes") != path.stat().st_size:
            raise ValidationError(f"qualification artifact size mismatch: {relative}")
        if metadata.get("sha256") != _sha256(path):
            raise ValidationError(f"qualification artifact digest mismatch: {relative}")
        if not isinstance(metadata.get("role"), str) or not metadata["role"]:
            raise ValidationError(f"qualification artifact role is missing: {relative}")


def _check_deterministic_repeat(first: dict[str, str], second: dict[str, str]) -> None:
    if first != second:
        changed = sorted(set(first) | set(second))
        changed = [key for key in changed if first.get(key) != second.get(key)]
        raise ValidationError("deterministic repeat mismatch: " + ", ".join(changed))


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"qualification {context} must be a finite number")
    return float(value)


def _check_performance(song_id: str, performance: dict[str, Any], guardrails: dict[str, Any]) -> None:
    if not isinstance(performance, dict):
        raise ValidationError(f"qualification {song_id} performance evidence is missing")
    compile_seconds = _number(performance.get("compile_seconds"), f"{song_id} compile time")
    render_seconds = _number(performance.get("render_seconds"), f"{song_id} render time")
    master_seconds = _number(performance.get("master_seconds"), f"{song_id} master time")
    duration = _number(performance.get("duration_seconds"), f"{song_id} duration")
    output_bytes = _number(performance.get("output_bytes"), f"{song_id} output size")
    peak_rss = performance.get("peak_rss_bytes")
    if duration <= 0 or min(compile_seconds, render_seconds, master_seconds, output_bytes) < 0:
        raise ValidationError(f"qualification {song_id} performance measurements are invalid")
    if compile_seconds > float(guardrails["compile_max_seconds"]):
        raise ValidationError(f"{song_id} compile performance guardrail exceeded")
    if render_seconds / duration > float(guardrails["render_max_realtime_ratio"]):
        raise ValidationError(f"{song_id} render performance guardrail exceeded")
    if master_seconds / duration > float(guardrails["master_max_realtime_ratio"]):
        raise ValidationError(f"{song_id} master performance guardrail exceeded")
    if output_bytes > float(guardrails["song_output_max_bytes"]):
        raise ValidationError(f"{song_id} output-size guardrail exceeded")
    if peak_rss is not None and _number(peak_rss, f"{song_id} peak RSS") > float(guardrails["peak_rss_max_bytes"]):
        raise ValidationError(f"{song_id} peak-RSS guardrail exceeded")


def _wav_contract(path: Path, *, channels: int, frames: int, sample_rate: int = 48_000) -> None:
    try:
        with wave.open(str(path), "rb") as handle:
            actual = (handle.getnchannels(), handle.getsampwidth() * 8, handle.getframerate(), handle.getnframes())
    except (OSError, wave.Error) as exc:
        raise ValidationError(f"qualification WAV is unreadable: {path}") from exc
    expected = (channels, 24, sample_rate, frames)
    if actual != expected:
        raise ValidationError(f"qualification WAV contract mismatch for {path}: expected {expected}, found {actual}")


def _input_hashes(root: Path, song_relative: str, lineage: list[str]) -> dict[str, str]:
    paths = [song_relative, str(PurePosixPath(song_relative).with_name("production.toml"))]
    paths.extend(f"styles/{style_id}/style.toml" for style_id in lineage)
    result: dict[str, str] = {}
    for relative in paths:
        path = _relative_file(root, relative, "qualification input")
        if not path.is_file():
            raise ValidationError(f"qualification input is missing: {relative}")
        result[relative] = _sha256(path)
    return result


def _contract_hashes(root: Path, plan: dict[str, Any]) -> dict[str, str]:
    paths = ("src/songdna/qualification.py", plan["listening_template"])
    return {relative: _sha256(_relative_file(root, relative, "qualification contract")) for relative in paths}


def _manifest_artifacts(root: Path, song_id: str, manifest: dict[str, Any]) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "arrangement.json", "markers.csv", "resolved.json", "rights.json", "song.mid"
    }:
        raise ValidationError(f"{song_id} compile manifest artifact set is incomplete")
    base = root / "generated" / song_id
    for name, evidence in artifacts.items():
        path = base / name
        if not isinstance(evidence, dict) or not path.is_file() or evidence.get("bytes") != path.stat().st_size or evidence.get("sha256") != _sha256(path):
            raise ValidationError(f"{song_id} compile manifest mismatch: {name}")


def _preflight_song_outputs(root: Path, song_id: str) -> None:
    """Refuse unowned render/master directories before any song is mutated."""
    contracts = (
        (root / "generated" / song_id / "render", "render-manifest.json", "songdna-render-manifest/v1", ("renderer", "stems"), "render"),
        (root / "generated" / song_id / "master", "delivery-manifest.json", "songdna-delivery-manifest/v1", (), "master"),
    )
    for output, manifest_name, schema, required_objects, label in contracts:
        if not output.exists() and not output.is_symlink():
            continue
        if output.is_symlink() or not output.is_dir():
            raise ValidationError(f"refusing qualification: {song_id} {label} output is not an owned directory")
        try:
            manifest = _json(output / manifest_name)
        except ValidationError as exc:
            raise ValidationError(f"refusing qualification: {song_id} {label} output is unowned") from exc
        if manifest.get("schema") != schema or manifest.get("song_id") != song_id or any(
            not isinstance(manifest.get(field), dict) for field in required_objects
        ):
            raise ValidationError(f"refusing qualification: {song_id} {label} output is unowned")


def _collect_song_evidence(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    song_id = str(spec["id"])
    song, style, production = load_inputs(str(spec["song"]), root)
    if song["song"]["id"] != song_id or style["id"] != spec["style"]:
        raise ValidationError(f"qualification input mismatch for {song_id}")
    base = root / "generated" / song_id
    compile_manifest = _json(base / "manifest.json")
    arrangement = _json(base / "arrangement.json")
    rights = _json(base / "rights.json")
    resolved = _json(base / "resolved.json")
    if any(item.get("song_id") != song_id for item in (compile_manifest, arrangement, rights)):
        raise ValidationError(f"{song_id} compile artifacts disagree on song id")
    _manifest_artifacts(root, song_id, compile_manifest)
    if resolved != {"song": song, "style": style, "production": production}:
        raise ValidationError(f"{song_id} resolved inputs are stale or mismatched")
    if rights.get("schema") != "songdna-rights/v1" or rights.get("policy") != "original_only" or rights.get("external_audio") != []:
        raise ValidationError(f"{song_id} provenance is incomplete or permits undeclared audio")
    if any(entry.get("origin") not in {"original_composition", "original_midi", "original_synthesis", "self_recorded"} for entry in rights.get("entries", [])):
        raise ValidationError(f"{song_id} provenance contains an unsupported origin")

    render_dir = base / "render"
    render = _json(render_dir / "render-manifest.json")
    diagnostics = _json(render_dir / "production-diagnostics.json")
    roles = set(style["roles"])
    nodes = [str(node["id"]) for node in production["graph"]["nodes"]]
    if render.get("schema") != "songdna-render-manifest/v1" or render.get("song_id") != song_id:
        raise ValidationError(f"{song_id} render manifest is missing or mismatched")
    if set(render.get("stems", {})) != roles or set(production["role_map"]) != roles:
        raise ValidationError(f"{song_id} renderer/provenance role coverage mismatch")
    frames = render.get("frame_count")
    if isinstance(frames, bool) or not isinstance(frames, int) or frames < 1:
        raise ValidationError(f"{song_id} render frame count is invalid")
    _wav_contract(render_dir / "preview.wav", channels=2, frames=frames)
    preview = render.get("preview")
    if not isinstance(preview, dict) or preview.get("sha256") != _sha256(render_dir / "preview.wav") or not preview.get("non_silent"):
        raise ValidationError(f"{song_id} preview evidence is stale or silent")
    for role in roles:
        stem_path = render_dir / "stems" / f"{role}.wav"
        stem = render["stems"][role]
        _wav_contract(stem_path, channels=1, frames=frames)
        if not isinstance(stem, dict) or stem.get("sha256") != _sha256(stem_path) or stem.get("origin") != "original_synthesis" or not stem.get("non_silent"):
            raise ValidationError(f"{song_id} stem evidence mismatch: {role}")
    if diagnostics.get("schema") != "songdna-production-diagnostics/v1" or diagnostics.get("exercised_nodes") != nodes:
        raise ValidationError(f"{song_id} production nodes were not all exercised in declaration order")
    if diagnostics.get("clipping") or diagnostics.get("invalid_samples") != 0:
        raise ValidationError(f"{song_id} production diagnostics report invalid or clipped audio")
    role_energy = diagnostics.get("role_energy")
    if not isinstance(role_energy, dict) or set(role_energy) != roles or any(not isinstance(value, (int, float)) or value <= 0 for value in role_energy.values()):
        raise ValidationError(f"{song_id} production diagnostics do not cover audible roles")

    master_dir = base / "master"
    delivery = _json(master_dir / "delivery-manifest.json")
    qa = _json(master_dir / "qa.json")
    if delivery.get("schema") != "songdna-delivery-manifest/v1" or delivery.get("song_id") != song_id:
        raise ValidationError(f"{song_id} delivery manifest is missing or mismatched")
    toolchain = delivery.get("toolchain")
    if not isinstance(toolchain, dict):
        raise ValidationError(f"{song_id} delivery toolchain evidence is missing")
    for tool, version in (("ffmpeg", EXPECTED_FFMPEG_VERSION), ("ffprobe", EXPECTED_FFMPEG_VERSION), ("lame", EXPECTED_LAME_VERSION)):
        declaration = toolchain.get(tool)
        if not isinstance(declaration, dict) or declaration.get("version") != version:
            raise ValidationError(f"{song_id} delivery toolchain is stale: {tool}")
    retained = master_dir / "pre-master.wav"
    master = master_dir / "master.wav"
    listening = master_dir / "listening.mp3"
    for path in (retained, master, listening, master_dir / "qa.md"):
        if not path.is_file():
            raise ValidationError(f"{song_id} delivery artifact is missing: {path.name}")
    if delivery.get("pre_master", {}).get("sha256") != _sha256(retained) or _sha256(retained) != _sha256(render_dir / "preview.wav"):
        raise ValidationError(f"{song_id} retained pre-master does not match the renderer preview")
    if delivery.get("master", {}).get("sha256") != _sha256(master):
        raise ValidationError(f"{song_id} master WAV digest mismatch")
    listening_record = delivery.get("listening_mp3", {})
    if not isinstance(listening_record, dict) or listening_record.get("sha256") != _sha256(listening) or listening_record.get("source_master_sha256") != _sha256(master):
        raise ValidationError(f"{song_id} listening MP3 lineage mismatch")
    decoded = listening_record.get("decoded", {})
    if not isinstance(decoded, dict) or decoded.get("channels") != 2 or decoded.get("sample_rate") != 48_000 or decoded.get("duration_delta_seconds", 1) > decoded.get("duration_tolerance_seconds", 0):
        raise ValidationError(f"{song_id} MP3 decode evidence is missing or invalid")
    if qa.get("schema") != "songdna-audio-qa/v1" or not qa.get("releaseable") or qa.get("failures"):
        raise ValidationError(f"{song_id} mastering QA is not releaseable")
    samples = qa.get("samples")
    loudness = qa.get("loudness")
    if not isinstance(samples, dict) or not isinstance(loudness, dict) or samples.get("invalid_samples") != 0 or loudness.get("true_peak_dbtp", 0) > float(production["mastering"]["true_peak_dbtp"]):
        raise ValidationError(f"{song_id} mastering QA violates sample or true-peak policy")
    _wav_contract(master, channels=2, frames=frames)

    artifact_evidence: dict[str, Any] = {}
    for path in sorted(item for item in base.rglob("*") if item.is_file()):
        relative = path.relative_to(base).as_posix()
        if relative.endswith("listening.mp3"):
            role = "listening_mp3"
        elif relative.startswith("master/"):
            role = "mastering"
        elif relative.startswith("render/"):
            role = "render"
        elif relative.startswith("ardour-handoff/"):
            role = "ardour_handoff"
        else:
            role = "compile"
        key, value = _artifact_entry(root, path, role)
        artifact_evidence[key] = value

    handoff_summary: dict[str, Any] | None = None
    if spec["ardour_handoff"]:
        handoff = validate_handoff_bundle(base / "ardour-handoff")
        if handoff.get("schema") != HANDOFF_SCHEMA or handoff.get("song_id") != song_id:
            raise ValidationError("Circuit Bloom Ardour handoff is missing or mismatched")
        ownership = handoff.get("ownership", {})
        if ownership.get("mode") != "create-once/manual-update" or ownership.get("refresh") != "unsupported; bootstrap refuses any session containing non-master routes before mutation":
            raise ValidationError("Circuit Bloom Ardour ownership/drift contract is stale")
        handoff_summary = {
            "bundle_valid": True,
            "source_fingerprint": handoff["source_fingerprint"],
            "supported_ardour": handoff["supported_ardour"]["version"],
            "ownership": ownership["mode"],
            "save_reopen_smoke": "required separate ardour-handoff-smoke CI job",
        }

    return {
        "input_hashes": _input_hashes(root, str(spec["song"]), list(style["lineage"])),
        "semantic": {
            "style": style["id"],
            "roles": sorted(roles),
            "production_nodes": nodes,
            "production_nodes_exercised": diagnostics["exercised_nodes"],
            "provenance_status": rights["status"],
            "mastering_qa": "pass",
            "mp3_decode": "pass",
            "duration_seconds": render["duration_seconds"],
        },
        "artifacts": artifact_evidence,
        "listening_mp3": {
            "path": (master_dir / "listening.mp3").relative_to(root).as_posix(),
            "sha256": _sha256(listening),
        },
        "ardour_handoff": handoff_summary,
    }


def _deterministic_hashes(root: Path, song_id: str) -> dict[str, str]:
    base = root / "generated" / song_id
    paths = [
        base / name for name in (
            "song.mid", "markers.csv", "arrangement.json", "rights.json", "resolved.json", "manifest.json"
        )
    ]
    paths.extend(sorted(item for item in (base / "render").rglob("*") if item.is_file()))
    # Delivery timing and absolute binary paths are observations, not stable
    # payloads.  The PCM, MP3, and QA payloads are the claimed repeat boundary.
    paths.extend(base / "master" / name for name in (
        "pre-master.wav", "master.wav", "listening.mp3", "qa.json", "qa.md"
    ))
    result: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            raise ValidationError(f"deterministic payload is missing: {path.relative_to(root)}")
        result[path.relative_to(root).as_posix()] = _sha256(path)
    return result


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _prepare_listening_review(root: Path, plan: dict[str, Any], expected: dict[str, dict[str, str]]) -> Path:
    target = _relative_file(root, plan["listening_review"], "listening review")
    if target.exists():
        if not target.is_file():
            raise ValidationError("listening review path exists but is not a file")
        return target
    template = _json(_relative_file(root, plan["listening_template"], "listening template"))
    template_songs = template.get("songs")
    if template.get("schema") != LISTENING_SCHEMA or not isinstance(template_songs, dict) or set(template_songs) != QUALIFICATION_SONGS:
        raise ValidationError("listening review template is stale")
    for song_id, audio in expected.items():
        template["songs"][song_id]["listening_mp3"] = audio["path"]
        template["songs"][song_id]["listening_mp3_sha256"] = audio["sha256"]
    _write_json_atomic(target, template)
    return target


def validate_listening_review(review: dict[str, Any], expected: dict[str, dict[str, str]], *, require_complete: bool) -> str:
    songs = review.get("songs")
    if review.get("schema") != LISTENING_SCHEMA or not isinstance(songs, dict) or set(songs) != QUALIFICATION_SONGS:
        raise ValidationError("listening review schema or song set is invalid")
    complete = bool(review.get("reviewer")) and bool(review.get("reviewed_at"))
    for song_id in sorted(QUALIFICATION_SONGS):
        evidence = songs[song_id]
        if not isinstance(evidence, dict) or evidence.get("listening_mp3") != expected[song_id]["path"] or evidence.get("listening_mp3_sha256") != expected[song_id]["sha256"]:
            raise ValidationError(f"{song_id} listening evidence is stale or mismatched")
        checks = evidence.get("checks")
        findings = evidence.get("musical_findings")
        if not isinstance(checks, dict) or set(checks) != LISTENING_CHECKS:
            raise ValidationError(f"{song_id} listening playback checks are incomplete")
        if not isinstance(findings, dict) or set(findings) != MUSICAL_FINDINGS:
            raise ValidationError(f"{song_id} musical listening fields are incomplete")
        complete = complete and evidence.get("verdict") == "pass"
        complete = complete and all(
            isinstance(item, dict) and item.get("complete") is True and bool(str(item.get("findings", "")).strip())
            for item in checks.values()
        )
        complete = complete and all(bool(str(value).strip()) for value in findings.values())
    signoff = review.get("signoff")
    complete = complete and isinstance(signoff, dict) and signoff.get("complete") is True and bool(str(signoff.get("statement", "")).strip())
    if not complete and require_complete:
        raise ValidationError("human listening review pending: complete headphones, speakers, mono, low-volume, findings, and signoff")
    return "pass" if complete else "pending"


def build_qualification(root: Path | str = ".", plan_relative: str = "qualification/plan.json") -> Path:
    root_path = Path(root).resolve()
    plan_path, plan = _load_plan(root_path, plan_relative)
    first_hashes: dict[str, dict[str, str]] = {}
    second_hashes: dict[str, dict[str, str]] = {}
    performance: dict[str, dict[str, Any]] = {}

    # This is a set-wide transaction boundary for ownership: discover every
    # unsafe target before compile/render/master can change the first song.
    for spec in plan["songs"]:
        _preflight_song_outputs(root_path, str(spec["id"]))
    source_revision = _source_revision(root_path)
    build_environment = _canonical_build_environment(plan)
    _mastering_toolchain()  # fail before a long render if the pinned tools are unavailable

    for spec in plan["songs"]:
        song_id = spec["id"]
        song_path = str(spec["song"])
        song, style, production = load_inputs(song_path, root_path)
        arrangement = build_arrangement(style, song)
        compiled, compile_seconds = _qualification_timed(song_id, "compile", lambda: compile_song(song_path, root_path))
        render, render_seconds = _qualification_timed(song_id, "render", lambda: render_arrangement(
            arrangement, style, production, root_path / "generated" / song_id / "render"
        ))
        _master, master_seconds = _qualification_timed(song_id, "master", lambda: master_pre_master(
            render.preview_path, production, root_path / "generated" / song_id / "master"
        ))
        first_hashes[song_id] = _deterministic_hashes(root_path, song_id)
        duration = json.loads(render.manifest_path.read_text(encoding="utf-8"))["duration_seconds"]
        performance[song_id] = {
            "compile_seconds": round(compile_seconds, 6),
            "render_seconds": round(render_seconds, 6),
            "master_seconds": round(master_seconds, 6),
            "duration_seconds": duration,
            "render_factor_realtime": round(duration / max(render_seconds, 1e-9), 6),
            "export_factor_realtime": round(duration / max(master_seconds, 1e-9), 6),
            "peak_rss_bytes": _peak_rss_bytes(),
            "peak_rss_scope": "maximum SongDNA qualification process RSS observed after this song",
            "output_bytes": 0,
        }
        del compiled, render, arrangement
        gc.collect()

    for spec in plan["songs"]:
        song_id = spec["id"]
        song_path = str(spec["song"])
        song, style, production = load_inputs(song_path, root_path)
        arrangement = build_arrangement(style, song)
        _compiled, repeat_compile = _qualification_timed(song_id, "repeat compile", lambda: compile_song(song_path, root_path))
        render, repeat_render = _qualification_timed(song_id, "repeat render", lambda: render_arrangement(
            arrangement, style, production, root_path / "generated" / song_id / "render"
        ))
        _master, repeat_master = _qualification_timed(song_id, "repeat master", lambda: master_pre_master(
            render.preview_path, production, root_path / "generated" / song_id / "master"
        ))
        second_hashes[song_id] = _deterministic_hashes(root_path, song_id)
        _check_deterministic_repeat(first_hashes[song_id], second_hashes[song_id])
        performance[song_id]["repeat_seconds"] = {
            "compile": round(repeat_compile, 6),
            "render": round(repeat_render, 6),
            "master": round(repeat_master, 6),
        }
        del render, arrangement
        gc.collect()

    handoff_spec = next(item for item in plan["songs"] if item["ardour_handoff"])
    handoff = create_handoff(str(handoff_spec["song"]), root_path)
    validate_handoff_bundle(handoff.output_dir)
    del handoff
    gc.collect()

    evidence: dict[str, dict[str, Any]] = {}
    expected_listening: dict[str, dict[str, str]] = {}
    for spec in plan["songs"]:
        song_id = spec["id"]
        current = _collect_song_evidence(root_path, spec)
        performance[song_id]["output_bytes"] = sum(item["bytes"] for item in current["artifacts"].values())
        _check_performance(song_id, performance[song_id], plan["performance_guardrails"])
        current["performance"] = performance[song_id]
        current["deterministic_repeat"] = {
            "status": "pass",
            "first": first_hashes[song_id],
            "second": second_hashes[song_id],
        }
        evidence[song_id] = current
        expected_listening[song_id] = current["listening_mp3"]

    review_path = _prepare_listening_review(root_path, plan, expected_listening)
    review_status = validate_listening_review(_json(review_path), expected_listening, require_complete=False)
    ledger = {
        "schema": LEDGER_SCHEMA,
        "status": "automated_pass",
        "human_listening": review_status,
        "source_revision": source_revision,
        "plan": {"path": plan_path.relative_to(root_path).as_posix(), "sha256": _sha256(plan_path)},
        "environment": {
            **build_environment,
            "mastering_toolchain": {"ffmpeg": EXPECTED_FFMPEG_VERSION, "lame": EXPECTED_LAME_VERSION},
            "ardour": SUPPORTED_ARDOUR_VERSION,
        },
        "repeat_policy": plan["repeat_policy"],
        "contracts": _contract_hashes(root_path, plan),
        "songs": evidence,
        "listening_review": review_path.relative_to(root_path).as_posix(),
    }
    ledger_path = _relative_file(root_path, plan["ledger"], "qualification ledger")
    _write_json_atomic(ledger_path, ledger)
    return ledger_path


def validate_qualification(
    root: Path | str = ".",
    plan_relative: str = "qualification/plan.json",
    *,
    automated_only: bool = False,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    plan_path, plan = _load_plan(root_path, plan_relative)
    ledger_path = _relative_file(root_path, plan["ledger"], "qualification ledger")
    ledger = _json(ledger_path)
    if ledger.get("schema") != LEDGER_SCHEMA or ledger.get("status") != "automated_pass":
        raise ValidationError("qualification ledger schema or automated status is invalid")
    if ledger.get("source_revision") != _source_revision(root_path):
        raise ValidationError("qualification ledger Git revision is stale or mismatched")
    if ledger.get("plan") != {"path": plan_path.relative_to(root_path).as_posix(), "sha256": _sha256(plan_path)}:
        raise ValidationError("qualification ledger plan hash is stale or mismatched")
    if ledger.get("contracts") != _contract_hashes(root_path, plan):
        raise ValidationError("qualification ledger evidence contracts are stale or mismatched")
    _check_recorded_environment(plan, ledger.get("environment"))
    songs = ledger.get("songs")
    if not isinstance(songs, dict) or set(songs) != QUALIFICATION_SONGS:
        raise ValidationError("qualification ledger song set is incomplete")
    expected_listening: dict[str, dict[str, str]] = {}
    for spec in plan["songs"]:
        song_id = spec["id"]
        record = songs[song_id]
        if not isinstance(record, dict):
            raise ValidationError(f"qualification ledger record is invalid: {song_id}")
        repeat = record.get("deterministic_repeat")
        if not isinstance(repeat, dict) or repeat.get("status") != "pass" or not isinstance(repeat.get("first"), dict) or not isinstance(repeat.get("second"), dict):
            raise ValidationError(f"{song_id} deterministic repeat evidence is missing")
        _check_deterministic_repeat(repeat["first"], repeat["second"])
        _check_artifacts(root_path, record.get("artifacts"))
        current = _collect_song_evidence(root_path, spec)
        for field in ("input_hashes", "semantic", "artifacts", "listening_mp3", "ardour_handoff"):
            if record.get(field) != current[field]:
                raise ValidationError(f"{song_id} qualification ledger {field} is stale or mismatched")
        _check_performance(song_id, record.get("performance", {}), plan["performance_guardrails"])
        expected_listening[song_id] = current["listening_mp3"]
    review_relative = ledger.get("listening_review")
    if review_relative != plan["listening_review"]:
        raise ValidationError("qualification listening review path is mismatched")
    review = _json(_relative_file(root_path, review_relative, "listening review"))
    listening_status = validate_listening_review(review, expected_listening, require_complete=not automated_only)
    return {
        "valid": True,
        "status": "qualified" if listening_status == "pass" else "automated_pass_human_pending",
        "ledger": ledger_path.relative_to(root_path).as_posix(),
        "listening_review": review_relative,
        "listening_status": listening_status,
        "songs": sorted(QUALIFICATION_SONGS),
    }


def qualify(
    root: Path | str = ".",
    plan_relative: str = "qualification/plan.json",
    *,
    validate_only: bool = False,
    automated_only: bool = False,
) -> dict[str, Any]:
    if not validate_only:
        build_qualification(root, plan_relative)
    return validate_qualification(root, plan_relative, automated_only=automated_only)
