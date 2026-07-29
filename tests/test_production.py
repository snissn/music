from __future__ import annotations

import unittest

from songdna.errors import ValidationError
from songdna.production import process_production, resolve_graph


class ProductionContractTest(unittest.TestCase):
    def test_kick_ducks_bass_and_section_filter_is_time_local(self) -> None:
        production = {
            "schema": "songdna-production/v2", "song": "fixture",
            "session": {"daw": "headless", "sample_rate": 48_000, "bit_depth": 24},
            "role_map": {"kick": {"origin": "original_synthesis", "owner": "test", "description": "kick"}, "bass": {"origin": "original_synthesis", "owner": "test", "description": "bass"}},
            "graph": {"version": "production-graph/v1", "master_bus": "music", "buses": [{"id": "music"}], "nodes": [
                {"id": "duck", "type": "sidechain_duck", "version": "v1", "source": "role:bass", "key": "role:kick", "destination": "bus:music", "amount": 0.5, "release_frames": 4},
                {"id": "filter", "type": "one_pole_lowpass", "version": "v1", "source": "bus:music", "destination": "bus:music", "cutoff": 0.05, "automation": [{"start_frame": 8, "end_frame": 16, "parameter": "cutoff", "start": 0.05, "end": 0.9}]},
            ]},
        }
        stems = {"kick": [1.0] + [0.0] * 15, "bass": [1.0] * 16}
        graph = resolve_graph(production, {"kick", "bass"})
        result = process_production(stems, graph, 48_000)
        self.assertEqual(len(result.samples), 16)
        self.assertLess(result.role_samples["bass"][0], 0.55)
        self.assertGreater(result.role_samples["bass"][7], result.role_samples["bass"][0])
        self.assertLess(result.samples[8], result.samples[15])
        self.assertEqual(result.diagnostics["exercised_nodes"], ["duck", "filter"])
        self.assertEqual(result.diagnostics["frame_count"], 16)

    def test_unknown_parameter_and_cycle_fail_closed(self) -> None:
        base = {"schema": "songdna-production/v2", "song": "fixture", "session": {"daw": "headless", "sample_rate": 48000, "bit_depth": 24}, "role_map": {"bass": {"origin": "original_synthesis", "owner": "test", "description": "bass"}}, "graph": {"version": "production-graph/v1", "master_bus": "a", "buses": [{"id": "a"}, {"id": "b"}], "nodes": [{"id": "a_to_b", "type": "gain_pan", "version": "v1", "source": "bus:a", "destination": "bus:b", "gain": 1.0, "pan": 0.0}, {"id": "b_to_a", "type": "gain_pan", "version": "v1", "source": "bus:b", "destination": "bus:a", "gain": 1.0, "pan": 0.0}]}}
        with self.assertRaisesRegex(ValidationError, "cycle"):
            resolve_graph(base, {"bass"})
        base["graph"]["nodes"] = [{"id": "unknown", "type": "gain_pan", "version": "v1", "source": "role:bass", "destination": "bus:a", "gain": 1.0, "pan": 0.0, "plugin": "nope"}]
        with self.assertRaisesRegex(ValidationError, "unsupported parameters"):
            resolve_graph(base, {"bass"})
        base["graph"]["nodes"][0].pop("plugin")
        base["graph"]["nodes"][0]["automation"] = [{"start_frame": 0, "end_frame": 1, "parameter": "gain", "start": 0.0, "end": 5.0}]
        with self.assertRaisesRegex(ValidationError, "automation end is out of range"):
            resolve_graph(base, {"bass"})
        base["role_map"] = {}
        base["graph"]["nodes"][0].pop("automation")
        with self.assertRaisesRegex(ValidationError, "missing role_map ownership"):
            resolve_graph(base, {"bass"})

    def test_gain_pan_is_a_stereo_contract(self) -> None:
        production = {"schema": "songdna-production/v2", "song": "fixture", "session": {"daw": "headless", "sample_rate": 48000, "bit_depth": 24}, "role_map": {"bass": {"origin": "original_synthesis", "owner": "test", "description": "bass"}}, "graph": {"version": "production-graph/v1", "master_bus": "music", "buses": [{"id": "music"}], "nodes": [{"id": "left", "type": "gain_pan", "version": "v1", "source": "role:bass", "destination": "bus:music", "gain": 1.0, "pan": -1.0}]}}
        result = process_production({"bass": [1.0, 1.0]}, resolve_graph(production, {"bass"}), 48_000)
        self.assertGreater(result.left[0], 0.99)
        self.assertLess(result.right[0], 0.01)

    def test_filter_insert_replaces_instead_of_parallel_summing_and_stereo_clips(self) -> None:
        production = {"schema": "songdna-production/v2", "song": "fixture", "session": {"daw": "headless", "sample_rate": 48000, "bit_depth": 24}, "role_map": {"bass": {"origin": "original_synthesis", "owner": "test", "description": "bass"}}, "graph": {"version": "production-graph/v1", "master_bus": "music", "buses": [{"id": "music"}], "nodes": [{"id": "route", "type": "gain_pan", "version": "v1", "source": "role:bass", "destination": "bus:music", "gain": 2.0, "pan": -1.0}, {"id": "insert", "type": "one_pole_lowpass", "version": "v1", "source": "bus:music", "destination": "bus:music", "cutoff": 0.5}]}}
        result = process_production({"bass": [1.0, 1.0]}, resolve_graph(production, {"bass"}), 48_000)
        self.assertAlmostEqual(result.left[0], 1.0)  # filter result, not dry + wet (3.0)
        self.assertTrue(result.diagnostics["clipping"])
        self.assertLess(result.diagnostics["headroom_db"], 0.0)
