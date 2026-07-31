"""Deterministic, dependency-free audio renderer.

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
import subprocess
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
PATCH_VERSION = "songdna-original-palette/v4"
CSOUND_PATCH_VERSION = "songdna-csound-tonal/v3"
CANONICAL_SAMPLE_RATE = 48_000
CHANNELS = 2
ROLE_PATCHES = {
    "kick": "kick_sub_click", "clap": "clap_layered_noise_body", "closed_hat": "closed_hat_metallic_noise",
    "open_hat": "open_hat_metallic_wash", "percussion": "percussion_metal_rim", "bass": "bass_subtractive_pluck",
    "harmony": "harmony_unison_stab", "lead": "lead_bandlimited_supersaw", "fx_trigger": "fx_noise_riser",
}
CSOUND_PATCHES = {
    "bass": "csound_saturated_sub_bass",
    "harmony": "csound_chorus_stab",
    "lead": "csound_chorus_supersaw",
}
CSOUND_INSTRUMENTS = {
    "bass": """
instr Tone
  iFreq = p4
  iAmp = p5
  iDecay = min(p3 * 0.40, 0.055)
  iHold = max(p3 - iDecay, 0.001)
  xtratim 0.075
  kAmp madsr 0.003, 0.040, 0.64, 0.070
  kCut expseg 6800, iDecay, 1050, iHold, 480
  aSub poscil 0.34, iFreq, giSine
  aSaw1 vco2 0.23, iFreq * 0.996
  aSaw2 vco2 0.23, iFreq * 1.004
  aOctave vco2 0.075, iFreq * 2.002
  aBody moogladder aSaw1 + aSaw2 + aOctave, kCut, 0.22
  aDrive = tanh(aBody * 1.85) * 0.46
  aTone butterlp aSub * 0.72 + aDrive, 9200
  aClean dcblock2 aTone
  out aClean * kAmp * iAmp * 1.22
endin
""",
    "harmony": """
instr Tone
  iFreq = p4
  iAmp = p5
  iSweep = min(p3 * 0.48, 0.070)
  iHold = max(p3 - iSweep, 0.001)
  xtratim 0.14
  kAmp madsr 0.005, 0.045, 0.44, 0.13
  kCut expseg 9200, iSweep, 2100, iHold, 1200
  kDrift1 poscil 0.0014, 0.23, giSine
  kDrift2 poscil 0.0011, 0.17, giSine
  aSaw1 vco2 0.18, iFreq * (0.995 + kDrift1)
  aSaw2 vco2 0.18, iFreq
  aSaw3 vco2 0.18, iFreq * (1.005 + kDrift2)
  aAir vco2 0.050, iFreq * 2.001
  aBody moogladder aSaw1 + aSaw2 + aSaw3 + aAir, kCut, 0.20
  aDrive = tanh(aBody * 1.45)
  aHighpass butterhp aDrive, 170
  kChorus poscil 1.6, 0.31, giSine
  aChorus vdelay3 aHighpass, 12 + kChorus, 20
  aClean = aHighpass * 0.76 + aChorus * 0.24
  out aClean * kAmp * iAmp * 0.31
endin
""",
    "lead": """
instr Tone
  iFreq = p4
  iAmp = p5
  iSweep = min(p3 * 0.45, 0.095)
  iHold = max(p3 - iSweep, 0.001)
  xtratim 0.16
  kAmp madsr 0.005, 0.060, 0.60, 0.15
  kVibrato poscil 0.0024, 5.1, giSine
  kDrift poscil 0.0012, 0.19, giSine
  kFreq = iFreq * (1 + kVibrato)
  kCut expseg 11800, iSweep, 4300, iHold, 2500
  aOne vco2 0.105, kFreq * (0.992 - kDrift)
  aTwo vco2 0.105, kFreq * 0.996
  aThree vco2 0.105, kFreq
  aFour vco2 0.105, kFreq * 1.004
  aFive vco2 0.105, kFreq * (1.008 + kDrift)
  aOctave vco2 0.042, kFreq * 2.001
  aCore poscil 0.055, kFreq, giSine
  aBody moogladder aOne + aTwo + aThree + aFour + aFive + aOctave, kCut, 0.18
  aDrive = tanh(aBody * 1.55) * 0.78 + aCore
  aHighpass butterhp aDrive, 190
  kChorus poscil 1.9, 0.27, giSine
  aChorus vdelay3 aHighpass, 13 + kChorus, 22
  aClean = aHighpass * 0.72 + aChorus * 0.28
  out aClean * kAmp * iAmp * 0.60
endin
""",
}


@dataclass(frozen=True)
class RenderResult:
    output_dir: Path
    manifest_path: Path
    preview_path: Path


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _frame_count(arrangement: Arrangement, sample_rate: int) -> int:
    return round(arrangement.tick_to_seconds(arrangement.total_ticks) * sample_rate)


def _note_bounds(note: Note, arrangement: Arrangement, sample_rate: int, total_frames: int) -> tuple[int, int]:
    start = round(arrangement.tick_to_seconds(note.start) * sample_rate)
    end = round(arrangement.tick_to_seconds(note.start + note.duration) * sample_rate)
    return max(0, min(total_frames, start)), max(0, min(total_frames, end))


def _noise(index: int, seed: int) -> float:
    # Hash each sample independently. A linear congruential step over adjacent
    # indexes repeats as a pitched saw; this mixer stays deterministic without
    # introducing that audible period.
    value = (index + seed * 0x9E3779B9) & 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    value = (value * 0x846CA68B) & 0xFFFFFFFF
    value ^= value >> 16
    return value / 2147483648.0 - 1.0


def _bright_noise(index: int, seed: int) -> float:
    return (_noise(index, seed) - 0.7 * _noise(index - 1, seed)) / 1.7


def _triangle(phase: float) -> float:
    return 1.0 - 4.0 * abs((phase % 1.0) - 0.5)


def _bandlimited_saw(phase: float, step: float) -> float:
    position = phase % 1.0
    value = 2.0 * position - 1.0
    if position < step:
        edge = position / step
        value -= edge + edge - edge * edge - 1.0
    elif position > 1.0 - step:
        edge = (position - 1.0) / step
        value -= edge * edge + edge + edge + 1.0
    return value


def _attack_release(t: float, length: float, attack: float, release: float) -> float:
    return min(1.0, t / attack) * min(1.0, max(0.0, length - t) / release)


def _csound_document(role: str, notes: list[Note], arrangement: Arrangement) -> str:
    score = []
    for note in notes:
        start = arrangement.tick_to_seconds(note.start)
        end = arrangement.tick_to_seconds(note.start + note.duration)
        frequency = 440.0 * (2.0 ** ((note.pitch - 69) / 12.0))
        score.append(f'i "Tone" {start:.9f} {end - start:.9f} {frequency:.9f} {note.velocity / 127.0:.9f}')
    duration = arrangement.tick_to_seconds(arrangement.total_ticks) + 0.25
    score_text = "\n".join(score)
    return f"""<CsoundSynthesizer>
<CsOptions>
</CsOptions>
<CsInstruments>
sr = {CANONICAL_SAMPLE_RATE}
ksmps = 32
nchnls = 1
0dbfs = 1
giSine ftgen 1, 0, 65536, 10, 1
{CSOUND_INSTRUMENTS[role]}
</CsInstruments>
<CsScore>
{score_text}
f 0 {duration:.9f}
e
</CsScore>
</CsoundSynthesizer>
"""


def _csound_engine() -> tuple[str, str, str]:
    executable = shutil.which("csound")
    if not executable:
        raise ValidationError("Csound backend requires the csound executable")
    probe = subprocess.run([executable, "--version"], capture_output=True, text=True, check=False)
    version_line = next((line.strip() for line in (probe.stdout + probe.stderr).splitlines() if "Csound version" in line), "")
    if probe.returncode or "Csound version 6.18" not in version_line:
        raise ValidationError(f"Csound backend requires version 6.18.x, observed {version_line or 'unknown'}")
    binary_hash = hashlib.sha256(Path(executable).read_bytes()).hexdigest()
    return executable, version_line.replace("\x1b[m", ""), binary_hash


def _render_csound_role(
    executable: str,
    role: str,
    notes: list[Note],
    arrangement: Arrangement,
    frames: int,
    work_dir: Path,
) -> array:
    csd = work_dir / f".{role}.csd"
    raw = work_dir / f".{role}.f32"
    csd.write_text(_csound_document(role, notes, arrangement), encoding="utf-8")
    result = subprocess.run(
        [executable, "-d", "-m0", "--format=float", "-h", "-o", str(raw), str(csd)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode or not raw.is_file():
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ValidationError(f"Csound failed rendering {role}: {detail[-1] if detail else 'no output'}")
    samples = array("f")
    with raw.open("rb") as handle:
        samples.fromfile(handle, raw.stat().st_size // samples.itemsize)
    if len(samples) < frames:
        samples.extend(array("f", [0.0]) * (frames - len(samples)))
    elif len(samples) > frames:
        del samples[frames:]
    csd.unlink()
    raw.unlink()
    return samples


def _voice(role: str, note: Note, position: int, frames: int, sample_rate: int) -> float:
    t = position / sample_rate
    length = max(frames / sample_rate, 1.0 / sample_rate)
    velocity = note.velocity / 127.0
    frequency = 440.0 * (2.0 ** ((note.pitch - 69) / 12.0))
    envelope = max(0.0, 1.0 - position / max(frames, 1))
    if role == "kick":
        phase = 2 * math.pi * (48.0 * t + 112.0 * 0.024 * (1.0 - math.exp(-t / 0.024)))
        body = math.sin(phase)
        click = _bright_noise(position, note.start) * math.exp(-t / 0.004)
        return (0.84 * body + 0.16 * click) * math.exp(-t / 0.18) * envelope ** 0.35 * velocity * 0.78
    if role == "clap":
        bursts = sum(
            weight * math.exp(-(t - onset) / 0.006)
            for onset, weight in ((0.0, 1.0), (0.012, 0.82), (0.024, 0.64))
            if t >= onset
        )
        tail = 0.32 * math.exp(-t / 0.075)
        texture = 0.78 * _bright_noise(position, note.start) + 0.22 * _noise(position, note.start + 17)
        body = math.sin(2 * math.pi * 720.0 * t) * math.exp(-t / 0.022)
        return (texture * min(1.28, bursts + tail) * 0.24 + body * 0.035) * envelope ** 0.3 * velocity
    if role in {"closed_hat", "open_hat"}:
        offset = (note.start % 29) * 5.0
        metal = sum(
            1.0 if math.sin(2 * math.pi * frequency * t) >= 0.0 else -1.0
            for frequency in (5423.0 + offset, 7987.0 - offset, 10937.0 + offset * 0.5)
        ) / 3.0
        texture = 0.84 * _bright_noise(position, note.start) + 0.16 * metal
        attack = min(1.0, t * 6000.0)
        decay = math.exp(-t / 0.032) if role == "closed_hat" else 0.74 * math.exp(-t / 0.075) + 0.26 * math.exp(-t / 0.24)
        return texture * attack * decay * envelope ** 0.2 * velocity * (0.18 if role == "closed_hat" else 0.21)
    if role == "percussion":
        ring_frequency = 980.0 + (note.start % 11) * 37.0
        ring = math.sin(2 * math.pi * ring_frequency * t + 0.45 * math.sin(2 * math.pi * 233.0 * t))
        texture = 0.58 * _bright_noise(position, note.start) + 0.42 * ring
        return texture * min(1.0, t * 5000.0) * math.exp(-t / 0.052) * envelope ** 0.25 * velocity * 0.24
    if role == "bass":
        phase = frequency * t
        step = frequency / sample_rate
        brightness = 0.18 + 0.82 * math.exp(-t / 0.065)
        edge = 0.5 * (
            _bandlimited_saw(phase * 0.997, step * 0.997)
            + _bandlimited_saw(phase * 1.003, step * 1.003)
        )
        body = 0.68 * math.sin(2 * math.pi * phase) + 0.32 * brightness * edge
        saturated = math.tanh(1.55 * body) / math.tanh(1.55)
        shape = _attack_release(t, length, 0.004, 0.035) * (0.82 + 0.18 * math.exp(-t / 0.11))
        return saturated * shape * velocity * 0.30
    if role == "harmony":
        phase_offset = ((note.start * 31 + note.pitch * 17) % 257) / 257.0
        phase = frequency * t + phase_offset
        step = frequency / sample_rate
        brightness = 0.22 + 0.78 * math.exp(-t / 0.055)
        unison = 0.5 * (
            _bandlimited_saw(phase * 0.998, step * 0.998)
            + _bandlimited_saw(phase * 1.002, step * 1.002)
        )
        body = brightness * unison + (1.0 - brightness) * _triangle(phase)
        shape = _attack_release(t, length, 0.006, 0.045) * math.exp(-t / 0.24)
        return math.tanh(1.1 * body) * shape * velocity * 0.14
    if role == "lead":
        phase_offset = ((note.start * 19 + note.pitch * 23) % 251) / 251.0
        vibrato = 0.028 * (1.0 - math.exp(-t / 0.08)) * math.sin(2 * math.pi * 5.2 * t)
        phase = frequency * t + phase_offset + vibrato
        step = frequency / sample_rate
        unison = (
            _bandlimited_saw(phase * 0.996, step * 0.996)
            + 0.8 * _bandlimited_saw(phase, step)
            + _bandlimited_saw(phase * 1.004, step * 1.004)
        ) / 2.8
        brightness = 0.46 + 0.54 * math.exp(-t / 0.11)
        body = brightness * unison + (1.0 - brightness) * _triangle(phase)
        shape = _attack_release(t, length, 0.007, 0.055) * (0.88 + 0.12 * math.exp(-t / 0.18))
        return math.tanh(1.25 * body) * shape * velocity * 0.21
    if role == "fx_trigger":
        progress = position / max(frames - 1, 1)
        sweep_phase = 180.0 * t + (2600.0 - 180.0) * t * t / (2.0 * length)
        sweep = math.sin(2 * math.pi * sweep_phase)
        texture = 0.72 * _bright_noise(position, note.start) + 0.28 * sweep
        shape = math.sin(math.pi * progress) ** 0.7
        return texture * shape * velocity * 0.18
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


def render_arrangement(
    arrangement: Arrangement,
    style: dict[str, Any],
    production: dict[str, Any],
    output_dir: Path | str,
    stems_only: bool = False,
    backend: str = "builtin",
) -> RenderResult:
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
    if backend not in {"builtin", "csound"}:
        raise ValidationError(f"renderer has no backend {backend}")
    csound = _csound_engine() if backend == "csound" else None
    patches = {**ROLE_PATCHES, **(CSOUND_PATCHES if csound else {})}
    patch_version = CSOUND_PATCH_VERSION if csound else PATCH_VERSION
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
            notes = arrangement.notes_by_role.get(role, [])
            samples = (
                _render_csound_role(csound[0], role, notes, arrangement, frames, stage)
                if csound and role in CSOUND_PATCHES
                else _render_role(role, notes, arrangement, sample_rate, frames)
            )
            stem = _write_wav(stems_dir / f"{role}.wav", samples, sample_rate, channels=1)
            if not stem["non_silent"]:
                raise ValidationError(f"renderer produced silent required role {role}")
            stems[role] = {**stem, "patch": patches[role], "patch_sha256": _digest({"version": patch_version, "role": role, "patch": patches[role]}), "origin": production["role_map"][role]["origin"], "owner": production["role_map"][role]["owner"]}
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
            "renderer": {
                "adapter": ADAPTER_VERSION,
                "backend": "csound-tonal-hybrid" if csound else BACKEND_ID,
                "backend_version": csound[1] if csound else BACKEND_VERSION,
                "patch_version": patch_version,
                "backend_sha256": csound[2] if csound else _digest({"backend": BACKEND_ID, "version": BACKEND_VERSION}),
            },
            "frame_count": frames, "duration_seconds": frames / sample_rate, "sample_rate": sample_rate,
            "channel_layout": {"stems": 1, "preview": CHANNELS}, "bit_depth": 24, "stems": stems, "preview": preview,
            "provenance": {
                "audible_assets": "none",
                "license": "MIT renderer code; Csound LGPL-2.1-or-later when selected; no presets, plugins, or audio assets",
                "role_map": production["role_map"],
            },
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
