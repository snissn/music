from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from songdna.errors import ValidationError
from songdna.handoff import create_handoff, handoff_drift, validate_handoff_bundle


ROOT = Path(__file__).resolve().parents[1]


class ArdourHandoffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.fixture_root = cls._root_static(cls._temporary.name)
        cls.fixture = create_handoff(
            cls.fixture_root / "songs/glass_transit/song.toml", cls.fixture_root
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    @staticmethod
    def _root_static(temporary: str) -> Path:
        root = Path(temporary)
        shutil.copytree(ROOT / "styles", root / "styles")
        shutil.copytree(ROOT / "songs", root / "songs")
        return root

    def _root(self, temporary: str) -> Path:
        return self._root_static(temporary)

    def _bundle_copy(self, temporary: str) -> Path:
        target = Path(temporary) / "ardour-handoff"
        shutil.copytree(self.fixture.output_dir, target)
        return target

    def test_bundle_is_complete_relative_and_self_validating(self) -> None:
        manifest = validate_handoff_bundle(self.fixture.output_dir)

        self.assertEqual(manifest["schema"], "songdna-ardour-handoff/v1")
        self.assertEqual(manifest["song_id"], "glass_transit")
        self.assertEqual(manifest["supported_ardour"]["version"], "8.12.0~ds")
        self.assertEqual(manifest["ownership"]["mode"], "create-once/manual-update")
        self.assertEqual(
            {entry["role_id"] for entry in manifest["objects"]["tracks"]},
            set(manifest["production"]["role_ids"]),
        )
        self.assertTrue(manifest["objects"]["markers"])
        self.assertTrue(manifest["objects"]["buses"])
        self.assertTrue(manifest["objects"]["routes"])
        required_kinds = {"midi", "markers", "stem", "renderer", "production", "provenance", "bootstrap", "verification"}
        self.assertTrue(required_kinds.issubset({item["kind"] for item in manifest["artifacts"].values()}))
        for relative in manifest["artifacts"]:
            path = Path(relative)
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)

    def test_tampered_bundle_fails_before_it_can_be_bootstrapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._bundle_copy(temporary)
            (bundle / "metadata/production-manifest.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "(size|digest) mismatch"):
                validate_handoff_bundle(bundle)

    def test_rebuild_refuses_to_replace_unowned_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            target = root / "generated/circuit_bloom/ardour-handoff"
            target.mkdir(parents=True)
            sentinel = target / "human-notes.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "non-handoff"):
                create_handoff(root / "songs/circuit_bloom/song.toml", root)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_handoff_refuses_to_replace_unowned_render_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            render = root / "generated/circuit_bloom/render"
            render.mkdir(parents=True)
            sentinel = render / "human-recording.wav"
            sentinel.write_bytes(b"keep")
            with self.assertRaisesRegex(ValidationError, "non-render"):
                create_handoff(root / "songs/circuit_bloom/song.toml", root)
            self.assertEqual(sentinel.read_bytes(), b"keep")
            self.assertFalse((root / "generated/circuit_bloom/song.mid").exists())

    def test_validation_rejects_symlinked_ancestor_escaping_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._bundle_copy(temporary)
            external = Path(temporary) / "external-metadata"
            shutil.copytree(bundle / "metadata", external)
            shutil.rmtree(bundle / "metadata")
            (bundle / "metadata").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "escapes bundle root"):
                validate_handoff_bundle(bundle)

    def test_version_or_song_mismatch_is_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._bundle_copy(temporary)
            path = bundle / "handoff-manifest.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["supported_ardour"]["version"] = "9.0"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unsupported Ardour contract"):
                validate_handoff_bundle(bundle)

    def test_drift_check_distinguishes_current_and_changed_inputs(self) -> None:
        manifest = validate_handoff_bundle(self.fixture.output_dir)
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            lock = {
                "schema": "songdna-ardour-session-lock/v1",
                "song_id": "glass_transit",
                "source_fingerprint": manifest["source_fingerprint"],
            }
            lock_path = session / "songdna-handoff-lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            self.assertEqual(handoff_drift(self.fixture.output_dir, session)["status"], "generated-inputs-current")
            lock["source_fingerprint"] = "stale"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            self.assertEqual(
                handoff_drift(self.fixture.output_dir, session)["status"],
                "generated-inputs-changed/manual-update-required",
            )


if __name__ == "__main__":
    unittest.main()
