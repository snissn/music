"""Small, deterministic pre-master production graph for SongDNA.

The graph intentionally exposes a short whitelist instead of accepting DAW or
plugin identifiers.  It is a portable production intent layer, not a plugin
host.  All nodes operate on aligned mono floating-point buffers; the renderer
owns final stereo encoding.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import math
from typing import Any, Sequence

from .errors import ValidationError


NODE_TYPES = {"gain_pan", "one_pole_lowpass", "sidechain_duck", "delay_send", "reverb_send"}
NODE_VERSION = "v1"


@dataclass(frozen=True)
class ResolvedGraph:
    master_bus: str
    buses: tuple[str, ...]
    nodes: tuple[dict[str, Any], ...]
    source_owners: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ProductionResult:
    samples: list[float]
    left: list[float]
    right: list[float]
    role_samples: dict[str, list[float]]
    diagnostics: dict[str, Any]


def _fail(message: str) -> None:
    raise ValidationError(f"production graph: {message}")


def _source(value: Any, roles: set[str], buses: set[str], field: str) -> tuple[str, str]:
    if not isinstance(value, str) or ":" not in value:
        _fail(f"{field} must be role:<name> or bus:<name>")
    kind, name = value.split(":", 1)
    if kind == "role" and name in roles:
        return kind, name
    if kind == "bus" and name in buses:
        return kind, name
    _fail(f"{field} references missing {value}")


def _number(node: dict[str, Any], name: str, minimum: float | None = None, maximum: float | None = None) -> float:
    value = node.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(f"node {node.get('id', '?')} parameter {name} must be finite")
    value = float(value)
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        _fail(f"node {node.get('id', '?')} parameter {name} is out of range")
    return value


def resolve_graph(production: dict[str, Any], roles: set[str]) -> ResolvedGraph:
    if production.get("schema") != "songdna-production/v2":
        _fail("requires songdna-production/v2")
    graph = production.get("graph")
    if not isinstance(graph, dict) or graph.get("version") != "production-graph/v1":
        _fail("requires graph version production-graph/v1")
    buses_raw = graph.get("buses")
    if not isinstance(buses_raw, list) or not buses_raw:
        _fail("requires non-empty graph.buses")
    buses: list[str] = []
    for item in buses_raw:
        if not isinstance(item, dict) or set(item) != {"id"} or not isinstance(item["id"], str) or not item["id"]:
            _fail("bus declarations must contain only a non-empty id")
        buses.append(item["id"])
    if len(set(buses)) != len(buses):
        _fail("bus ids must be unique")
    bus_set = set(buses)
    master = graph.get("master_bus")
    if master not in bus_set:
        _fail("master_bus must name a declared bus")
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        _fail("requires non-empty graph.nodes")
    ids: set[str] = set()
    edges: dict[str, set[str]] = {bus: set() for bus in buses}
    checked: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            _fail("node must be an object")
        required = {"id", "type", "version", "source", "destination"}
        if not required <= set(node):
            _fail("node is missing required fields")
        ident = node["id"]
        if not isinstance(ident, str) or not ident or ident in ids:
            _fail("node ids must be unique and non-empty")
        ids.add(ident)
        if node["type"] not in NODE_TYPES or node["version"] != NODE_VERSION:
            _fail(f"node {ident} has unsupported type or version")
        source_kind, source_name = _source(node["source"], roles, bus_set, f"node {ident}.source")
        destination_kind, destination_name = _source(node["destination"], roles, bus_set, f"node {ident}.destination")
        if destination_kind != "bus":
            _fail(f"node {ident}.destination must be a bus")
        if source_kind == "bus" and source_name != destination_name:
            edges[source_name].add(destination_name)
        if node["type"] == "sidechain_duck":
            _source(node.get("key"), roles, bus_set, f"node {ident}.key")
            _number(node, "amount", 0.0, 1.0)
            release = node.get("release_frames")
            if isinstance(release, bool) or not isinstance(release, int) or release < 1:
                _fail(f"node {ident} parameter release_frames must be a positive integer")
        elif node["type"] == "gain_pan":
            _number(node, "gain", 0.0, 4.0)
            _number(node, "pan", -1.0, 1.0)
        elif node["type"] == "one_pole_lowpass":
            _number(node, "cutoff", 0.001, 1.0)
        else:
            _number(node, "wet", 0.0, 1.0)
            delay = node.get("delay_frames")
            if isinstance(delay, bool) or not isinstance(delay, int) or delay < 1:
                _fail(f"node {ident} parameter delay_frames must be a positive integer")
        for lane in node.get("automation", []):
            absolute = {"start_frame", "end_frame", "parameter", "start", "end"}
            relative = {"section", "start_ratio", "end_ratio", "parameter", "start", "end"}
            if not isinstance(lane, dict) or (set(lane) != absolute and set(lane) != relative):
                _fail(f"node {ident} has unsupported automation lane")
            if lane["parameter"] not in {"gain", "cutoff", "wet"} or lane["parameter"] not in node:
                _fail(f"node {ident} automates unsupported parameter")
            if set(lane) == absolute:
                if not all(isinstance(lane[x], int) and not isinstance(lane[x], bool) for x in ("start_frame", "end_frame")) or lane["start_frame"] < 0 or lane["end_frame"] <= lane["start_frame"]:
                    _fail(f"node {ident} has invalid automation range")
            elif not isinstance(lane["section"], int) or isinstance(lane["section"], bool) or lane["section"] < 1 or any(not isinstance(lane[x], (int, float)) or isinstance(lane[x], bool) or not 0 <= lane[x] <= 1 for x in ("start_ratio", "end_ratio")) or lane["end_ratio"] <= lane["start_ratio"]:
                _fail(f"node {ident} has invalid section-relative automation range")
            for x in ("start", "end"):
                if isinstance(lane[x], bool) or not isinstance(lane[x], (int, float)) or not math.isfinite(lane[x]):
                    _fail(f"node {ident} automation {x} must be finite")
        checked.append(dict(node))
    # A bus-to-itself node is a deliberate sequential insert; cycles between
    # distinct buses have no deterministic ordering and are rejected.
    visiting: set[str] = set(); visited: set[str] = set()
    def visit(bus: str) -> None:
        if bus in visiting: _fail("contains a bus dependency cycle")
        if bus in visited: return
        visiting.add(bus)
        for child in edges[bus]: visit(child)
        visiting.remove(bus); visited.add(bus)
    for bus in buses: visit(bus)
    return ResolvedGraph(master, tuple(buses), tuple(checked), {role: production["role_map"][role] for role in roles})


def materialize_section_automation(production: dict[str, Any], arrangement: Any) -> dict[str, Any]:
    """Resolve section-relative lanes into absolute sample positions for a render."""
    resolved = copy.deepcopy(production)
    sections = arrangement.sections
    frames_per_beat = 60.0 / arrangement.tempo * int(production["session"]["sample_rate"])
    beats_per_bar = arrangement.meter_numerator * (4 / arrangement.meter_denominator)
    for node in resolved["graph"]["nodes"]:
        lanes = []
        for lane in node.get("automation", []):
            if "section" not in lane:
                lanes.append(lane); continue
            index = lane["section"] - 1
            if index >= len(sections): _fail(f"node {node['id']} references missing section {lane['section']}")
            section = sections[index]
            start = (int(section["start_bar"]) - 1) * beats_per_bar * frames_per_beat
            length = int(section["bars"]) * beats_per_bar * frames_per_beat
            lanes.append({"parameter": lane["parameter"], "start": lane["start"], "end": lane["end"], "start_frame": round(start + length * lane["start_ratio"]), "end_frame": round(start + length * lane["end_ratio"])})
        node["automation"] = lanes
    return resolved


def _curve(node: dict[str, Any], parameter: str, frame: int) -> float:
    value = float(node[parameter])
    for lane in node.get("automation", []):
        if lane["parameter"] == parameter and lane["start_frame"] <= frame < lane["end_frame"]:
            ratio = (frame - lane["start_frame"]) / (lane["end_frame"] - lane["start_frame"])
            return float(lane["start"]) + (float(lane["end"]) - float(lane["start"])) * ratio
    return value


def process_production(stems: dict[str, Sequence[float]], graph: ResolvedGraph, sample_rate: int) -> ProductionResult:
    if not stems: _fail("requires source stems")
    frames = len(next(iter(stems.values())))
    if frames < 1 or any(len(samples) != frames for samples in stems.values()): _fail("source stems must be non-empty and aligned")
    if set(stems) != set(graph.source_owners): _fail("source stems do not match graph ownership")
    roles = {name: [float(value) for value in values] for name, values in stems.items()}
    buses = {bus: ([0.0] * frames, [0.0] * frames) for bus in graph.buses}
    populated_buses: set[str] = set()
    exercised: list[str] = []
    for node in graph.nodes:
        kind, name = node["source"].split(":", 1)
        if kind == "bus" and name not in populated_buses:
            _fail(f"node {node['id']} reads unpopulated bus {name}")
        if kind == "role":
            source_left = roles[name]; source_right = roles[name]
        else:
            source_left, source_right = buses[name]
        output_left = [0.0] * frames; output_right = [0.0] * frames
        if node["type"] == "gain_pan":
            # Constant-power pan is observable in the stereo preview while a
            # centred source remains transparent in the mono diagnostics.
            pan = float(node["pan"])
            left_gain = math.cos((pan + 1.0) * math.pi / 4.0)
            right_gain = math.sin((pan + 1.0) * math.pi / 4.0)
            for i in range(frames):
                gain = _curve(node, "gain", i)
                mono = (source_left[i] + source_right[i]) * 0.5 * gain
                output_left[i] = mono * left_gain; output_right[i] = mono * right_gain
        elif node["type"] == "one_pole_lowpass":
            previous_left = previous_right = 0.0
            for i in range(frames):
                cutoff = _curve(node, "cutoff", i)
                previous_left += cutoff * (source_left[i] - previous_left); previous_right += cutoff * (source_right[i] - previous_right)
                output_left[i] = previous_left; output_right[i] = previous_right
        elif node["type"] == "sidechain_duck":
            key_kind, key_name = node["key"].split(":", 1)
            key = roles[key_name] if key_kind == "role" else [(buses[key_name][0][i] + buses[key_name][1][i]) * 0.5 for i in range(frames)]
            envelope = 0.0
            for i in range(frames):
                envelope = max(abs(key[i]), envelope * (1.0 - 1.0 / int(node["release_frames"])))
                duck = 1.0 - float(node["amount"]) * min(1.0, envelope)
                output_left[i] = source_left[i] * duck; output_right[i] = source_right[i] * duck
            if kind == "role": roles[name] = [(output_left[i] + output_right[i]) * 0.5 for i in range(frames)]
        else:
            delay = int(node["delay_frames"]); wet = float(node["wet"])
            feedback = 0.45 if node["type"] == "reverb_send" else 0.7
            for i in range(frames):
                output_left[i] = source_left[i] + (output_left[i - delay] if i >= delay else 0.0) * wet * feedback
                output_right[i] = source_right[i] + (output_right[i - delay] if i >= delay else 0.0) * wet * feedback
        destination_name = node["destination"].split(":", 1)[1]
        destination_left, destination_right = buses[destination_name]
        for i in range(frames):
            destination_left[i] += output_left[i]; destination_right[i] += output_right[i]
        populated_buses.add(destination_name)
        exercised.append(node["id"])
    left, right = buses[graph.master_bus]
    mix = [(left[i] + right[i]) * 0.5 for i in range(frames)]
    if not all(math.isfinite(value) for value in mix): _fail("produced NaN/Inf")
    peak = max((abs(value) for value in mix), default=0.0)
    energy = lambda values: round(sum(value * value for value in values) / len(values), 10)
    bus_energy = {bus: energy([(pair[0][i] + pair[1][i]) * 0.5 for i in range(frames)]) for bus, pair in sorted(buses.items())}
    return ProductionResult(mix, left, right, roles, {"schema": "songdna-production-diagnostics/v1", "frame_count": frames, "sample_rate": sample_rate, "exercised_nodes": exercised, "nodes": [{"id": node["id"], "type": node["type"], "version": node["version"]} for node in graph.nodes], "headroom_dbfs": round(20 * math.log10(max(peak, 1e-12)), 5), "clipping": peak > 1.0, "invalid_samples": 0, "role_energy": {role: energy(values) for role, values in sorted(roles.items())}, "bus_energy": bus_energy})
