import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import packctl


class ProfileSchemaTest(unittest.TestCase):
    def valid(self, **values):
        entry = {"source": "curseforge", "project": 123}
        entry.update(values)
        return packctl.normalize_profile_entry(entry)

    def test_legacy_and_default_scope(self):
        entry = self.valid(side="client")
        self.assertEqual(entry.scope, "root")
        self.assertEqual(entry.project, 123)
        legacy_modrinth = packctl.normalize_profile_entry(
            {"source": "modrinth", "project": "create", "side": "both"}
        )
        self.assertEqual(legacy_modrinth.project, "create")

    def test_exact_curseforge_root(self):
        entry = self.valid(project="123", artifact_id="456", side="both")
        self.assertEqual((entry.project, entry.artifact_id), ("123", "456"))

    def test_exact_modrinth_root(self):
        entry = packctl.normalize_profile_entry({
            "source": "modrinth", "project": "AbCd1234", "artifact_id": "EfGh5678",
            "side": "server",
        })
        self.assertEqual(entry.scope, "root")

    def test_dependency_form(self):
        entry = self.valid(artifact_id="456", scope="dependency")
        self.assertEqual(entry.side, None)

    def test_rejects_unknown_and_ambiguous_fields(self):
        with self.assertRaises(packctl.ConfigError):
            self.valid(side="client", unexpected=True)
        with self.assertRaises(packctl.ConfigError):
            self.valid(artifact_id=456, scope="dependency", side="client")

    def test_dependency_requires_artifact(self):
        with self.assertRaises(packctl.ConfigError):
            self.valid(scope="dependency")

    def test_rejects_invalid_ids_and_types(self):
        cases = [
            {"project": True}, {"project": 0}, {"project": "01"},
            {"artifact_id": 0}, {"artifact_id": "01"},
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(packctl.ConfigError):
                self.valid(side="client", **change)
        with self.assertRaises(packctl.ConfigError):
            self.valid(side=1)
        with self.assertRaises(packctl.ConfigError):
            self.valid(source="other", side="client")
        with self.assertRaises(packctl.ConfigError):
            self.valid(artifact_id=456, side="client")
        with self.assertRaises(packctl.ConfigError):
            packctl.normalize_profile_entry({"source": "modrinth", "project": "short",
                                              "artifact_id": "EfGh5678", "side": "client"})

    def test_load_profiles_contextual_validation_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            shared = root / "shared"
            local = root / "profiles.yaml"
            package.mkdir()
            shared.mkdir()
            (shared / "profiles.yaml").write_text("profiles:\n  first:\n    - {source: curseforge, project: 1, side: client}\n", encoding="utf-8")
            local.write_text("profiles:\n  second:\n    - {source: curseforge, project: 2, side: server}\n", encoding="utf-8")
            with patch.object(packctl, "PACKAGE_DATA", package), patch.object(packctl, "SHARED", shared):
                result = packctl.load_profiles(root)
            self.assertEqual(list(result), ["first", "second"])
            self.assertEqual(result["first"][0]["scope"], "root")

            local.write_text('profiles:\n  broken:\n    - {source: curseforge, project: "01", side: client}\n', encoding="utf-8")
            with patch.object(packctl, "PACKAGE_DATA", package), patch.object(packctl, "SHARED", shared), self.assertRaisesRegex(packctl.ConfigError, "Profile 'broken' entry 1"):
                packctl.load_profiles(root)


if __name__ == "__main__":
    unittest.main()
