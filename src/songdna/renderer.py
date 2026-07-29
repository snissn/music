"""Deterministic, dependency-free v1 audio renderer.

The canonical backend deliberately uses only the Python standard library.  It
is not intended as a production synth; its job is to provide a rights-clean,
headless, pinned baseline that exercises the renderer adapter contract.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import tempfile
from typing import Any
import wave

from .errors import ValidationError
from .model import Arrangement, Note
from .production import materialize_section_automation, process_production, resolve_graph
from .validation import validate_production


ADAPTER_VERSION = "songdna-renderer/v1"
BACKEND_ID = "builtin-deterministic-synth"
BACKEND_VERSION = "1.0.0"
PATCH_VERSION = "songdna-original-palette/v1"
CANONICAL_SAMPLE_RATE = 48_000
CHANNELS = 2
ROLE_PATCHES = {
    "kick": "kick_sine_drop", "clap": "clap_noise_burst", "closed_hat": "closed_hat_noise",
    "open_hat": "open_hat_noise", "percussion": "percussion_tone", "bass": "bass_square",
    "harmony": "harmony_sine", "lead": "lead_triangle", "fx_trigger": "fx_rise",
}


@dataclass(frozen=True)
class RenderResult:
    output_dir: Path
    manifest_path: Path
    preview_path: Path


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _frame_count(arrangement: Arrangement, sample_rate: int) -> int:
    seconds = arrangement.total_ticks / arrangement.ticks_per_beat * 60.0 / arrangement.tempo
    return round(seconds * sample_rate)


def _note_bounds(note: Note, arrangement: Arrangement, sample_rate: int, total_frames: int) -> tuple[int, int]:
    start = round(note.start / arrangement.ticks_per_beat * 60.0 / arrangement.tempo * sample_rate)
    end = round((note.start + note.duration) / arrangement.ticks_per_beat * 60.0 / arrangement.tempo * sample_rate)
    return max(0, min(total_frames, start)), max(0, min(total_frames, end))


def _noise(index: int, seed: int) -> float:
    # Integer-only hash noise: stable across supported hosts and Python versions.
    value = (index * 1103515245 + seed * 12345 + 12345) & 0x7FFFFFFF
    return value / 1073741824.0 - 1.0


def _voice(role: str, note: Note, position: int, frames: int, sample_rate: int) -> float:
    t = position / sample_rate
    length = max(frames / sample_rate, 1.0 / sample_rate)
    velocity = note.velocity / 127.0
    frequency = 440.0 * (2.0 ** ((note.pitch - 69) / 12.0))
    envelope = max(0.0, 1.0 - position / max(frames, 1))
    if role == "kick":
        phase = 2 * math.pi * (150.0 * t - 110.0 * t * t / length)
        return math.sin(phase) * envelope * velocity * 0.72
    if role in {"clap", "closed_hat", "open_hat"}:
        decay = 0.035 if role == "closed_hat" else (0.20 if role == "open_hat" else 0.09)
        return _noise(position, note.start) * math.exp(-t / decay) * velocity * (0.22 if role == "clap" else 0.14)
    if role == "percussion":
        return math.sin(2 * math.pi * frequency * t) * math.exp(-t / 0.12) * velocity * 0.25
    if role == "bass":
        phase = (frequency * t) % 1.0
        return (phase * 2.0 - 1.0) * min(1.0, t * 80.0) * envelope * velocity * 0.24
    if role == "harmony":
        return (math.sin(2 * math.pi * frequency * t) + 0.35 * math.sin(4 * math.pi * frequency * t)) * envelope * velocity * 0.11
    if role == "lead":
        phase = (frequency * t) % 1.0
        triangle = 1.0 - 4.0 * abs(phase - 0.5)
        return triangle * min(1.0, t * 120.0) * envelope * velocity * 0.18
    if role == "fx_trigger":
        sweep = 180.0 + (position / max(frames, 1)) * 2200.0
        return math.sin(2 * math.pi * sweep * t) * envelope * velocity * 0.12
    raise ValidationError(f"renderer has no patch for role {role}")


def _render_role(role: str, notes: list[Note], arrangement: Arrangement, sample_rate: int, frames: int) -> array:
    samples = array("f", [0.0]) * frames
    for note in notes:
        start, end = _note_bounds(note, arrangement, sample_rate, frames)
        for frame in range(start, end):
            samples[frame] += _voice(role, note, frame - start, end - start, sample_rate)
    return samples


def _write_wav(path: Path, samples: array, sample_rate: int, channels: int = CHANNELS) -> dict[str, Any]:
    peak = max((abs(value) for value in samples), default=0.0)
    if not all(math.isfinite(value) for value in samples):
        raise ValidationError(f"renderer produced NaN/Inf for {path.name}")
    if peak > 1.0:
        raise ValidationError(f"renderer produced clipping for {path.name}: peak {peak:.6f}")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(3)
        handle.setframerate(sample_rate)
        for start in range(0, len(samples), 8192):
            values = samples[start:start + 8192]
            if not any(values):
                handle.writeframesraw(b"\0" * (len(values) * channels * 3))
                continue
            payload = bytearray()
            for value in values:
                integer = max(-8_388_608, min(8_388_607, round(value * 8_388_607)))
                encoded = struct.pack("<i", integer)
                for _ in range(channels):
                    payload.extend(encoded[:3])
            handle.writeframesraw(payload)
    return {"frames": len(samples), "channels": channels, "sample_rate": sample_rate, "peak": round(peak, 8), "non_silent": peak > 0.0, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _write_stereo_wav(path: Path, left: Any, right: Any, sample_rate: int, gain: float = 1.0) -> dict[str, Any]:
    if len(left) != len(right):
        raise ValidationError("production stereo channels are not aligned")
    peak = max((abs(sample * gain) for channel in (left, right) for sample in channel), default=0.0)
    if peak > 1.0 or not all(math.isfinite(sample) for channel in (left, right) for sample in channel):
        raise ValidationError("production preview has invalid samples or clipping")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2); handle.setsampwidth(3); handle.setframerate(sample_rate)
        for start in range(0, len(left), 8192):
            payload = bytearray()
            for index in range(start, min(start + 8192, len(left))):
                payload.extend(struct.pack("<i", round(left[index] * gain * 8_388_607))[:3])
                payload.extend(struct.pack("<i", round(right[index] * gain * 8_388_607))[:3])
            handle.writeframesraw(payload)
    return {"frames": len(left), "channels": 2, "sample_rate": sample_rate, "peak": round(peak, 8), "non_silent": peak > 0.0, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _atomic_replace(stage: Path, target: Path) -> None:
    backup = target.with_name(target.name + ".previous")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.rename(backup)
    try:
        stage.rename(target)
    except Exception:
        if backup.exists():
            backup.rename(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def render_arrangement(arrangement: Arrangement, style: dict[str, Any], production: dict[str, Any], output_dir: Path | str, stems_only: bool = False) -> RenderResult:
    """Render aligned 24-bit WAV stems and, unless requested otherwise, a preview.

    Validation happens before a staging directory exists; all successful output
    becomes visible in one directory rename.
    """
    validate_production(production, {"song": {"id": arrangement.song_id}}, style)
    roles = set(style["roles"])
    if set(ROLE_PATCHES) != roles:
        raise ValidationError("renderer patch map does not cover style roles exactly")
    if any(production["role_map"][role]["origin"] != "original_synthesis" for role in roles):
        raise ValidationError("canonical renderer requires original_synthesis role mappings")
    sample_rate = int(production["session"]["sample_rate"])
    if sample_rate != CANONICAL_SAMPLE_RATE:
        raise ValidationError(f"canonical backend requires sample_rate {CANONICAL_SAMPLE_RATE}")
    target = Path(output_dir).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    try:
        frames = _frame_count(arrangement, sample_rate)
        raw_stems: dict[str, array] = {}
        stems: dict[str, dict[str, Any]] = {}
        stems_dir = stage / "stems"
        stems_dir.mkdir()
        for role in sorted(roles):
            samples = _render_role(role, arrangement.notes_by_role.get(role, []), arrangement, sample_rate, frames)
            stem = _write_wav(stems_dir / f"{role}.wav", samples, sample_rate, channels=1)
            if not stem["non_silent"]:
                raise ValidationError(f"renderer produced silent required role {role}")
            stems[role] = {**stem, "patch": ROLE_PATCHES[role], "patch_sha256": _digest({"version": PATCH_VERSION, "role": role, "patch": ROLE_PATCHES[role]}), "origin": production["role_map"][role]["origin"], "owner": production["role_map"][role]["owner"]}
            raw_stems[role] = samples
        preview_path = stage / "preview.wav"
        preview: dict[str, Any] | None = None
        diagnostics: dict[str, Any] | None = None
        if not stems_only:
            graph = resolve_graph(materialize_section_automation(production, arrangement), roles)
            produced = process_production(raw_stems, graph, sample_rate)
            # The production graph is pre-master: this fixed trim is only a
            # conservative preview boundary, not a loudness/mastering stage.
            preview = _write_stereo_wav(preview_path, produced.left, produced.right, sample_rate, gain=0.35)
            diagnostics = produced.diagnostics
            if diagnostics["clipping"]:
                raise ValidationError("production graph produced unreported clipping")
            (stage / "production-diagnostics.json").write_text(json.dumps(diagnostics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest = {
            "schema": "songdna-render-manifest/v1", "song_id": arrangement.song_id,
            "renderer": {"adapter": ADAPTER_VERSION, "backend": BACKEND_ID, "backend_version": BACKEND_VERSION, "patch_version": PATCH_VERSION, "backend_sha256": _digest({"backend": BACKEND_ID, "version": BACKEND_VERSION})},
            "frame_count": frames, "duration_seconds": frames / sample_rate, "sample_rate": sample_rate,
            "channel_layout": {"stems": 1, "preview": CHANNELS}, "bit_depth": 24, "stems": stems, "preview": preview,
            "provenance": {"audible_assets": "none", "license": "MIT renderer code; no third-party presets, plugins, codecs, or audio assets", "role_map": production["role_map"]},
            "production": {"schema": production["schema"], "graph_version": production["graph"]["version"], "diagnostics": "production-diagnostics.json" if diagnostics else None},
            "timing": {"timed_boundary": "arrangement-to-WAV staging render"},
        }
        manifest_path = stage / "render-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _atomic_replace(stage, target)
        return RenderResult(target, target / "render-manifest.json", target / "preview.wav")
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
