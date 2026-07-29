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
        self.assertEqual(result.role_samples["bass"][0], 1.0)
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
        base["role_map"] = {"bass": {"origin": "original_synthesis", "owner": "test", "description": "bass"}}
        base["graph"]["unexpected"] = True
        with self.assertRaisesRegex(ValidationError, "graph has unsupported fields"):
            resolve_graph(base, {"bass"})
        base["graph"].pop("unexpected")
        base["graph"]["nodes"][0]["automation"] = "not-a-list"
        with self.assertRaisesRegex(ValidationError, "automation must be a list"):
            resolve_graph(base, {"bass"})
        base["graph"]["nodes"][0].pop("automation")
        base["graph"]["nodes"][0]["type"] = []
        with self.assertRaisesRegex(ValidationError, "unsupported type"):
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

    def test_pan_and_wet_automation_change_samples(self) -> None:
        production = {"schema": "songdna-production/v2", "song": "fixture", "session": {"daw": "headless", "sample_rate": 48000, "bit_depth": 24}, "role_map": {"bass": {"origin": "original_synthesis", "owner": "test", "description": "bass"}}, "graph": {"version": "production-graph/v1", "master_bus": "music", "buses": [{"id": "music"}], "nodes": [{"id": "pan", "type": "gain_pan", "version": "v1", "source": "role:bass", "destination": "bus:music", "gain": 1.0, "pan": -1.0, "automation": [{"start_frame": 0, "end_frame": 3, "parameter": "pan", "start": -1.0, "end": 1.0}]}]}}
        result = process_production({"bass": [1.0, 1.0, 1.0]}, resolve_graph(production, {"bass"}), 48_000)
        self.assertGreater(result.left[0], result.right[0])
        self.assertGreater(result.right[2], result.left[2])
        production["graph"]["nodes"] = [{"id": "delay", "type": "delay_send", "version": "v1", "source": "role:bass", "destination": "bus:music", "wet": 0.0, "delay_frames": 1, "automation": [{"start_frame": 0, "end_frame": 3, "parameter": "wet", "start": 0.0, "end": 1.0}]}]
        wet = process_production({"bass": [1.0, 0.0, 0.0]}, resolve_graph(production, {"bass"}), 48_000)
        self.assertGreater(wet.left[2], 0.1)

    def test_unpopulated_sidechain_key_and_master_fail_closed(self) -> None:
        production = {"schema": "songdna-production/v2", "song": "fixture", "session": {"daw": "headless", "sample_rate": 48000, "bit_depth": 24}, "role_map": {"bass": {"origin": "original_synthesis", "owner": "test", "description": "bass"}}, "graph": {"version": "production-graph/v1", "master_bus": "music", "buses": [{"id": "music"}, {"id": "key"}], "nodes": [{"id": "duck", "type": "sidechain_duck", "version": "v1", "source": "role:bass", "key": "bus:key", "destination": "bus:music", "amount": 0.5, "release_frames": 1}]}}
        graph = resolve_graph(production, {"bass"})
        with self.assertRaisesRegex(ValidationError, "unpopulated sidechain key"):
            process_production({"bass": [1.0]}, graph, 48_000)
        production["graph"]["master_bus"] = "key"
        production["graph"]["nodes"][0]["key"] = "role:bass"
        with self.assertRaisesRegex(ValidationError, "master bus key was not populated"):
            process_production({"bass": [1.0]}, resolve_graph(production, {"bass"}), 48_000)

    def test_sidechain_does_not_mutate_role_source_across_fanout(self) -> None:
        production = {"schema": "songdna-production/v2", "song": "fixture", "session": {"daw": "headless", "sample_rate": 48000, "bit_depth": 24}, "role_map": {"kick": {"origin": "original_synthesis", "owner": "test", "description": "kick"}, "bass": {"origin": "original_synthesis", "owner": "test", "description": "bass"}}, "graph": {"version": "production-graph/v1", "master_bus": "dry", "buses": [{"id": "ducked"}, {"id": "dry"}], "nodes": [{"id": "duck", "type": "sidechain_duck", "version": "v1", "source": "role:bass", "key": "role:kick", "destination": "bus:ducked", "amount": 1.0, "release_frames": 1}, {"id": "dry_route", "type": "gain_pan", "version": "v1", "source": "role:bass", "destination": "bus:dry", "gain": 1.0, "pan": -1.0}]}}
        result = process_production({"kick": [1.0], "bass": [1.0]}, resolve_graph(production, {"kick", "bass"}), 48_000)
        self.assertGreater(result.left[0], 0.99)
        self.assertEqual(result.role_samples["bass"][0], 1.0)
