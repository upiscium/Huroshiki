from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


PACK_TOML = '''name = "Demo"
author = "tester"
version = "0.1.0"
pack-format = "packwiz:1.1.0"

[index]
file = "index.toml"
hash-format = "sha256"
hash = "placeholder"

[versions]
minecraft = "1.21.1"
neoforge = "21.1.999"
'''


def template_yaml(loader_version: str = "21.1.234") -> str:
    return f'''id: base
display_name: Base Template
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: {loader_version}
mods:
  - name: Create
    provider: modrinth
    project_id: create-id
    side: both
  - name: JEI
    provider: curseforge
    project_id: "238222"
    side: client
 '''


def override_yaml() -> str:
    return '''
mod_version_overrides:
  - provider: modrinth
    project_id: Abcd1234
    artifact_id: Efgh5678
    scope: root
  - provider: curseforge
    project_id: "238222"
    artifact_id: "123456"
    scope: root
'''


class TemplateManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.pack_root = self.packs / "demo"
        self.template_root = self.templates / "base"
        (self.pack_root / "source" / "mods").mkdir(parents=True)
        self.template_root.mkdir(parents=True)
        (self.pack_root / "source" / "pack.toml").write_text(
            PACK_TOML, encoding="utf-8"
        )
        (self.pack_root / "source" / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        (self.pack_root / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n",
            encoding="utf-8",
        )
        (self.template_root / "template.yaml").write_text(
            template_yaml(), encoding="utf-8"
        )

        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.templates),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.templates),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_template_stores_mods_as_manifest_entries(self) -> None:
        key = core.project_key("template", "base")
        info = core.project_info(key)
        self.assertEqual(info.minecraft, "1.21.1")
        self.assertEqual(info.loader, "neoforge")
        mods = core.list_mods(key)
        self.assertEqual([mod.name for mod in mods], ["Create", "JEI"])
        self.assertTrue(mods[0].client and mods[0].server)
        self.assertTrue(mods[1].client and not mods[1].server)

    def test_template_version_overrides_are_normalized_and_preserved(self) -> None:
        (self.template_root / "template.yaml").write_text(
            template_yaml().replace("project_id: create-id", "project_id: Abcd1234")
            + override_yaml(),
            encoding="utf-8",
        )
        expected = [
            {
                "provider": "modrinth",
                "project_id": "Abcd1234",
                "artifact_id": "Efgh5678",
                "scope": "root",
            },
            {
                "provider": "curseforge",
                "project_id": "238222",
                "artifact_id": "123456",
                "scope": "root",
            },
        ]
        self.assertEqual(packctl.template_mod_version_overrides("base"), expected)
        packctl.save_template_mods("base", packctl.template_mods("base"))
        saved = packctl.load_yaml(self.template_root / "template.yaml")
        self.assertEqual(saved["mod_version_overrides"], expected)

    def test_template_version_override_schema_rejects_unresolved_or_locked_intent(self) -> None:
        cases = [
            {"provider": "url", "project_id": "x", "artifact_id": "y", "scope": "root"},
            {"provider": "curseforge", "project_id": "01", "artifact_id": "2", "scope": "root"},
            {"provider": "modrinth", "project_id": "slug", "artifact_id": "Efgh5678", "scope": "root"},
            {"provider": "modrinth", "project_id": "Abcd1234", "artifact_id": "Efgh5678", "scope": "root", "locked": True},
        ]
        for override in cases:
            with self.subTest(override=override):
                config = packctl.load_yaml(self.template_root / "template.yaml")
                config["mod_version_overrides"] = [override]
                with self.assertRaises(packctl.ConfigError):
                    packctl.prospective_template_config("base", config, {})

    def test_template_side_edit_and_delete_update_yaml(self) -> None:
        key = core.project_key("template", "base")
        local = self.template_root / "template.local.yaml"
        local.write_text("url_max_jar_size_bytes: 1024\n", encoding="utf-8")
        local_before = local.read_bytes()
        mods = core.list_mods(key)
        core.set_installed_mod_side(
            key, mods[0].relative_path, client=False, server=True
        )
        changed = core.list_mods(key)
        self.assertFalse(changed[0].client)
        self.assertTrue(changed[0].server)

        result = core.remove_installed_mods(key, [changed[1].slug])
        self.assertEqual(result, 0)
        self.assertEqual([mod.name for mod in core.list_mods(key)], ["Create"])
        self.assertEqual(local.read_bytes(), local_before)

    def test_local_mods_cannot_override_listing_or_composition_data(self) -> None:
        (self.template_root / "template.local.yaml").write_text(
            "mods: []\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(
            packctl.ConfigError, "mods is committed semantic data"
        ):
            packctl.load_template_config("base")
        with self.assertRaisesRegex(
            packctl.ConfigError, "mods is committed semantic data"
        ):
            packctl.template_mods("base")
        with self.assertRaisesRegex(
            packctl.ConfigError, "mods is committed semantic data"
        ):
            core.list_mods("template:base")
        with self.assertRaisesRegex(
            packctl.ConfigError, "mods is committed semantic data"
        ):
            core.prepare_template_composition(
                template_ids=["base"], minecraft="1.21.1", loader="neoforge"
            )

    def test_template_local_schema_allows_only_url_policy(self) -> None:
        local = self.template_root / "template.local.yaml"
        local.write_text("url_max_jar_size_bytes: 1024\n", encoding="utf-8")
        self.assertEqual(
            packctl.load_template_config("base")["url_max_jar_size_bytes"],
            1024,
        )
        local.write_text("url_allow_private_networks: true\n", encoding="utf-8")
        self.assertTrue(
            packctl.load_template_config("base")["url_allow_private_networks"]
        )

        for key in sorted(packctl.TEMPLATE_COMMITTED_KEYS):
            with self.subTest(key=key):
                local.write_text(f"{key}: local\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    packctl.ConfigError,
                    rf"{key} is committed semantic data; edit template.yaml instead",
                ):
                    packctl.load_template_config("base")

        local.write_text("future_setting: true\n", encoding="utf-8")
        with self.assertRaisesRegex(
            packctl.ConfigError,
            "unsupported machine-local key 'future_setting'.*allowed keys: "
            "url_allow_private_networks, url_max_jar_size_bytes",
        ):
            packctl.load_template_config("base")

    def test_disallowed_template_local_config_becomes_project_error(self) -> None:
        (self.template_root / "template.local.yaml").write_text(
            "display_name: Local Base\n", encoding="utf-8"
        )

        info = core.project_info("template:base")

        self.assertIsNotNone(info.error)
        self.assertIn("display_name is committed semantic data", info.error or "")

    def test_template_transaction_conflicts_when_local_limit_changes(self) -> None:
        local = self.template_root / "template.local.yaml"
        local.write_text("url_max_jar_size_bytes: 1024\n", encoding="utf-8")
        transaction = core.PackTransaction.create("template:base")
        try:
            local.write_text("url_max_jar_size_bytes: 2048\n", encoding="utf-8")
            with self.assertRaisesRegex(
                core.HuroshikiError,
                "template configuration changed while this transaction was open",
            ):
                transaction.apply()
        finally:
            transaction.discard()

    def test_candidate_matching_ignores_loader_version(self) -> None:
        candidates = core.compatible_templates("1.21.1", "neoforge")
        self.assertEqual([item.project_id for item in candidates], ["base"])
        self.assertEqual(candidates[0].loader_version, "21.1.234")
        self.assertEqual(
            core.compatible_templates("1.21.1", "fabric"), []
        )
        self.assertEqual(
            core.compatible_templates("1.20.1", "neoforge"), []
        )

    def test_legacy_packwiz_template_is_not_loaded_as_manifest_data(self) -> None:
        legacy = self.templates / "legacy"
        (legacy / "source" / "mods").mkdir(parents=True)
        (legacy / "template.yaml").write_text(
            "id: legacy\ndisplay_name: Legacy\nenabled: true\n",
            encoding="utf-8",
        )
        (legacy / "source" / "pack.toml").write_text(
            PACK_TOML.replace('name = "Demo"', 'name = "Legacy"'),
            encoding="utf-8",
        )
        legacy_metadata = (
            'name = "Legacy MOD"\n'
            'filename = "legacy.jar"\n'
            'side = "server"\n'
            '[download]\n'
            'hash-format = "sha256"\n'
            'hash = "00"\n'
            'url = "https://example.invalid"\n'
            '[update.modrinth]\n'
            'mod-id = "legacy-id"\n'
            'version = "v"\n'
        )
        (legacy / "source" / "mods" / "legacy.pw.toml").write_text(
            legacy_metadata,
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            packctl.ConfigError, "legacy template source is not supported"
        ):
            packctl.load_template_config("legacy")
        self.assertFalse(hasattr(packctl, "legacy_template_mods"))
        self.assertFalse(hasattr(packctl, "derive_legacy_template_config"))

    def test_invalid_templates_do_not_hide_valid_candidates(self) -> None:
        invalid = self.templates / "invalid"
        invalid.mkdir()
        (invalid / "template.yaml").write_text(
            "id: invalid\ndisplay_name: Invalid\nmods: []\n",
            encoding="utf-8",
        )
        candidates = packctl.compatible_template_ids("1.21.1", "neoforge")
        self.assertEqual(candidates, ["base"])

    def test_build_uses_pack_content_only(self) -> None:
        metadata = self.pack_root / "source" / "mods" / "demo.pw.toml"
        metadata.write_text(
            '''name = "Demo MOD"\nfilename = "demo.jar"\nside = "both"\n''',
            encoding="utf-8",
        )
        with patch.object(packctl, "run"):
            errors = packctl.build_target(self.pack_root, "client")
        self.assertEqual(errors, [])
        self.assertTrue(
            (self.pack_root / "dist" / "client" / "mods" / "demo.pw.toml").exists()
        )


if __name__ == "__main__":
    unittest.main()
