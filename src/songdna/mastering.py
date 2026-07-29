"""Fail-closed canonical mastering and delivery pipeline.

This is intentionally a small orchestration layer, not a plugin host.  FFmpeg
does the declared loudness/true-peak stage and measurement; the separately
pinned LAME binary makes the listening MP3.  Both are required so a missing or
different tool cannot quietly produce a differently-mastered release.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from typing import Any

from .errors import SongDNAError, ValidationError

try:  # `resource` is unavailable on Windows; keep every CLI importable there.
    import resource
except ImportError:  # pragma: no cover - exercised on Windows
    resource = None  # type: ignore[assignment]


MASTERING_ADAPTER_VERSION = "songdna-mastering-adapter/v1"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
LAME = "lame"
EXPECTED_FFMPEG_VERSION = "8.1.2"
EXPECTED_LAME_VERSION = "3.100"
PROCESSING_TRUE_PEAK_MARGIN_DB = 0.2
MP3_DURATION_TOLERANCE_SECONDS = 0.10


@dataclass(frozen=True)
class MasterResult:
    output_dir: Path
    manifest_path: Path
    master_path: Path
    mp3_path: Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _peak_rss() -> tuple[int | None, str]:
    """Return child peak RSS in bytes when the platform supports it."""
    if resource is None:
        return None, "unavailable: platform has no resource module"
    value = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # macOS reports bytes; Linux reports KiB.
    return (value if sys.platform == "darwin" else value * 1024), "maximum child-process RSS observed by this fresh mastering CLI process"


def _run(command: list[str], context: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise SongDNAError(f"mastering requires {context}, but it is unavailable") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        raise SongDNAError(f"mastering {context} failed: {detail[-1] if detail else 'unknown tool error'}") from exc


def _tool_path(name: str) -> str:
    # Environment overrides make the supported CI image explicit while the
    # default remains ordinary PATH lookup for a documented local install.
    configured = __import__("os").environ.get(f"SONGDNA_{name.upper()}")
    candidate = configured or shutil.which(name.lower())
    if not candidate:
        raise SongDNAError(f"mastering requires {name}, but it is unavailable")
    return candidate


def _toolchain() -> dict[str, Any]:
    ffmpeg_path = _tool_path("ffmpeg")
    ffprobe_path = _tool_path("ffprobe")
    lame_path = _tool_path("lame")
    ffmpeg = _run([ffmpeg_path, "-version"], "FFmpeg")
    ffprobe = _run([ffprobe_path, "-version"], "FFprobe")
    lame = _run([lame_path, "--version"], "LAME")
    ffmpeg_match = re.search(r"ffmpeg version ([^\s]+)", ffmpeg.stdout)
    lame_match = re.search(r"LAME\s+(?:64bits\s+)?version\s+([^\s]+)", lame.stdout)
    if not ffmpeg_match or ffmpeg_match.group(1) != EXPECTED_FFMPEG_VERSION:
        actual = ffmpeg_match.group(1) if ffmpeg_match else "unrecognised"
        raise SongDNAError(f"mastering requires FFmpeg {EXPECTED_FFMPEG_VERSION}, found {actual}")
    ffprobe_match = re.search(r"ffprobe version ([^\s]+)", ffprobe.stdout)
    if not ffprobe_match or ffprobe_match.group(1) != EXPECTED_FFMPEG_VERSION:
        actual = ffprobe_match.group(1) if ffprobe_match else "unrecognised"
        raise SongDNAError(f"mastering requires FFprobe {EXPECTED_FFMPEG_VERSION}, found {actual}")
    if not lame_match or lame_match.group(1) != EXPECTED_LAME_VERSION:
        actual = lame_match.group(1) if lame_match else "unrecognised"
        raise SongDNAError(f"mastering requires LAME {EXPECTED_LAME_VERSION}, found {actual}")
    if "--enable-libmp3lame" not in ffmpeg.stdout:
        raise SongDNAError("mastering FFmpeg lacks the declared libmp3lame build support")
    return {
        "ffmpeg": {"path": ffmpeg_path, "version": EXPECTED_FFMPEG_VERSION, "license": "GPL-2.0-or-later (configured --enable-gpl)", "sha256_boundary": "system binary; version-pinned, not byte-pinned"},
        "ffprobe": {"path": ffprobe_path, "version": EXPECTED_FFMPEG_VERSION, "license": "GPL-2.0-or-later (configured --enable-gpl)"},
        "lame": {"path": lame_path, "version": EXPECTED_LAME_VERSION, "license": "LGPL-2.0-or-later", "sha256_boundary": "system binary; version-pinned, not byte-pinned"},
    }


def _wav_info(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as handle:
            info = {"channels": handle.getnchannels(), "sample_rate": handle.getframerate(), "bit_depth": handle.getsampwidth() * 8, "frames": handle.getnframes(), "compression": handle.getcomptype()}
    except (wave.Error, OSError) as exc:
        raise ValidationError(f"pre-master is not a readable WAV: {path}") from exc
    if info["compression"] != "NONE":
        raise ValidationError("pre-master WAV must be uncompressed PCM")
    return info


def _wrap_s24le_as_wav(raw_path: Path, wav_path: Path, *, sample_rate: int, channels: int, expected_frames: int) -> None:
    """Write a classic PCM WAV that Python 3.11's `wave` reader can inspect."""
    frame_bytes = channels * 3
    raw_size = raw_path.stat().st_size
    if raw_size % frame_bytes:
        raise ValidationError("mastering PCM output is not aligned to complete 24-bit frames")
    frames = raw_size // frame_bytes
    if frames != expected_frames:
        raise ValidationError(f"mastering PCM frame count changed: expected {expected_frames}, found {frames}")
    with raw_path.open("rb") as source, wave.open(str(wav_path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(3)
        target.setframerate(sample_rate)
        while payload := source.read(65_536):
            target.writeframesraw(payload)


def _assert_pre_master(path: Path, policy: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        raise SongDNAError(f"pre-master WAV is missing: {path}; run songdna render first")
    info = _wav_info(path)
    expected = {"channels": int(policy["channels"]), "sample_rate": int(policy["sample_rate"]), "bit_depth": int(policy["bit_depth"])}
    for key, value in expected.items():
        if info[key] != value:
            raise ValidationError(f"pre-master {key} must be {value}, found {info[key]}")
    if info["frames"] < int(policy["fade_frames"]) * 2:
        raise ValidationError("pre-master is shorter than the declared fades")
    return info


def _loudness(path: Path) -> dict[str, float]:
    # loudnorm in analysis mode supplies integrated loudness, LRA and true peak
    # from the same open-source meter used by the mastering stage.
    result = _run([_tool_path("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path), "-af", "loudnorm=I=-14:LRA=11:TP=-1:print_format=json", "-f", "null", "-"], "FFmpeg loudness analysis")
    matches = re.findall(r"\{\s*\"input_i\".*?\}", result.stderr, re.DOTALL)
    if not matches:
        raise SongDNAError("FFmpeg loudness analysis produced no machine-readable measurements")
    try:
        raw = json.loads(matches[-1])
        return {"integrated_lufs": float(raw["input_i"]), "loudness_range_lu": float(raw["input_lra"]), "true_peak_dbtp": float(raw["input_tp"]), "threshold_lufs": float(raw["input_thresh"])}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SongDNAError("FFmpeg loudness analysis returned invalid measurements") from exc


def _sample_qa(path: Path) -> dict[str, Any]:
    info = _wav_info(path)
    invalid = 0
    max_abs = 0.0
    total = 0.0
    near_silent = 0
    with wave.open(str(path), "rb") as handle:
        while data := handle.readframes(8192):
            for offset in range(0, len(data), 3):
                raw = data[offset:offset + 3]
                value = int.from_bytes(raw + (b"\xff" if raw[2] & 0x80 else b"\0"), "little", signed=True) / 8_388_608.0
                if not math.isfinite(value):
                    invalid += 1
                max_abs = max(max_abs, abs(value)); total += value
                near_silent += int(abs(value) < 1e-7)
    samples = info["frames"] * info["channels"]
    return {"sample_peak_dbfs": 20 * math.log10(max(max_abs, 1e-12)), "dc_offset": total / max(samples, 1), "invalid_samples": invalid, "silent": near_silent == samples, "near_silent_fraction": near_silent / max(samples, 1)}


def _mp3_stream_info(path: Path) -> dict[str, Any]:
    """Observe the encoded stream without coercing it through a WAV writer."""
    result = _run([_tool_path("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels,sample_rate,duration", "-of", "json", str(path)], "FFprobe MP3 inspection")
    try:
        stream = json.loads(result.stdout)["streams"][0]
        return {"channels": int(stream["channels"]), "sample_rate": int(stream["sample_rate"]), "duration_seconds": float(stream["duration"])}
    except (KeyError, TypeError, ValueError, IndexError, json.JSONDecodeError) as exc:
        raise SongDNAError("FFprobe MP3 inspection returned invalid stream metadata") from exc


def _qa(path: Path, policy: dict[str, Any]) -> dict[str, Any]:
    structural = _wav_info(path)
    samples = _sample_qa(path)
    meter = _loudness(path)
    failures: list[str] = []
    if structural["channels"] != int(policy["channels"]) or structural["sample_rate"] != int(policy["sample_rate"]) or structural["bit_depth"] != int(policy["bit_depth"]):
        failures.append("wrong_format")
    if samples["invalid_samples"]: failures.append("invalid_samples")
    if samples["silent"]: failures.append("silence")
    if abs(samples["dc_offset"]) > 0.01: failures.append("dc_offset")
    if meter["true_peak_dbtp"] > float(policy["true_peak_dbtp"]): failures.append("true_peak_ceiling")
    if abs(meter["integrated_lufs"] - float(policy["target_lufs"])) > float(policy["lufs_tolerance"]): failures.append("integrated_loudness")
    return {"schema": "songdna-audio-qa/v1", "releaseable": not failures, "failures": failures, "structural": structural, "samples": samples, "loudness": meter, "observations": ["Automated loudness compliance does not establish musical quality or translation."]}


def _atomic_replace(stage: Path, target: Path) -> None:
    backup = target.with_name(target.name + ".previous")
    if backup.exists(): shutil.rmtree(backup)
    if target.exists(): target.rename(backup)
    try: stage.rename(target)
    except Exception:
        if backup.exists(): backup.rename(target)
        raise
    if backup.exists(): shutil.rmtree(backup)


def master_pre_master(pre_master: Path, production: dict[str, Any], output_dir: Path) -> MasterResult:
    """Make a complete delivery atomically; failed QA publishes nothing."""
    policy = production["mastering"]
    pre_info = _assert_pre_master(pre_master, policy)
    pre_samples = _sample_qa(pre_master)
    if pre_samples["invalid_samples"] or pre_samples["silent"] or pre_samples["sample_peak_dbfs"] >= -0.01:
        raise ValidationError("pre-master QA failed: " + ", ".join(name for name, bad in (("invalid_samples", bool(pre_samples["invalid_samples"])), ("silence", pre_samples["silent"]), ("clipping", pre_samples["sample_peak_dbfs"] >= -0.01)) if bad))
    tools = _toolchain()
    target = output_dir.resolve(); target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    started = time.monotonic()
    try:
        retained = stage / "pre-master.wav"; shutil.copyfile(pre_master, retained)
        master = stage / "master.wav"
        raw_master = stage / "master.s24le"
        duration = pre_info["frames"] / pre_info["sample_rate"]
        fade = int(policy["fade_frames"]) / int(policy["sample_rate"])
        processing_ceiling = float(policy["true_peak_dbtp"]) - PROCESSING_TRUE_PEAK_MARGIN_DB
        filtergraph = f"afade=t=in:st=0:d={fade:.9f},afade=t=out:st={max(0.0, duration - fade):.9f}:d={fade:.9f},loudnorm=I={float(policy['target_lufs']):.2f}:LRA=11:TP={processing_ceiling:.2f}:linear=false"
        ffmpeg = _tool_path("ffmpeg"); lame = _tool_path("lame")
        _run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(retained), "-af", filtergraph, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", "-f", "s24le", str(raw_master)], "FFmpeg mastering stage")
        _wrap_s24le_as_wav(raw_master, master, sample_rate=48_000, channels=2, expected_frames=pre_info["frames"])
        raw_master.unlink()
        qa = _qa(master, policy)
        if not qa["releaseable"]:
            raise ValidationError("master QA failed: " + ", ".join(qa["failures"]))
        mp3 = stage / "listening.mp3"
        _run([lame, "--silent", "--noreplaygain", "--cbr", "-b", str(int(policy["mp3_bitrate_kbps"])), "--resample", "48", str(master), str(mp3)], "LAME MP3 encoding")
        # Observe encoded stream properties before a full decode; neither
        # operation coerces rate or channel layout.
        decoded_info = _mp3_stream_info(mp3)
        duration_delta = abs(decoded_info["duration_seconds"] - duration)
        if decoded_info["channels"] != 2 or decoded_info["sample_rate"] != 48_000 or duration_delta > MP3_DURATION_TOLERANCE_SECONDS:
            raise ValidationError("decoded MP3 does not meet channel, sample-rate, or duration contract")
        _run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(mp3), "-f", "null", "-"], "FFmpeg MP3 decode verification")
        elapsed = time.monotonic() - started
        decoded_report = {**decoded_info, "duration_delta_seconds": duration_delta, "duration_tolerance_seconds": MP3_DURATION_TOLERANCE_SECONDS}
        peak_rss_bytes, peak_rss_scope = _peak_rss()
        manifest = {"schema": "songdna-delivery-manifest/v1", "song_id": production["song"], "mastering": {"adapter": MASTERING_ADAPTER_VERSION, "policy": policy, "processing_true_peak_dbtp": processing_ceiling, "dither_application": "none: canonical PCM remains 24-bit end-to-end", "same_environment_audio_hash_boundary": "WAV and MP3 bytes are reproducible only with the recorded host tool binaries and versions."}, "toolchain": tools, "pre_master": {**pre_info, "sample_qa": pre_samples, "path": "pre-master.wav", "sha256": _sha256(retained)}, "master": {"path": "master.wav", "sha256": _sha256(master), "qa": "qa.json"}, "listening_mp3": {"path": "listening.mp3", "sha256": _sha256(mp3), "source_master_sha256": _sha256(master), "codec": policy["codec"], "bitrate_kbps": int(policy["mp3_bitrate_kbps"]), "decoded_with": f"FFmpeg {EXPECTED_FFMPEG_VERSION}", "decoded": decoded_report}, "performance": {"elapsed_seconds": round(elapsed, 4), "export_factor_realtime": round(duration / max(elapsed, 1e-9), 4), "peak_rss_bytes": peak_rss_bytes, "peak_rss_scope": peak_rss_scope}}
        (stage / "qa.json").write_text(json.dumps(qa, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (stage / "qa.md").write_text("# Audio QA\n\nStatus: **PASS**\n\n" + "\n".join(f"- {key}: `{value}`" for key, value in sorted(qa["loudness"].items())) + "\n", encoding="utf-8")
        (stage / "delivery-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _atomic_replace(stage, target)
        return MasterResult(target, target / "delivery-manifest.json", target / "master.wav", target / "listening.mp3")
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
