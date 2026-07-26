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

    def test_template_side_edit_and_delete_update_yaml(self) -> None:
        key = core.project_key("template", "base")
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

    def test_local_mods_cannot_override_listing_or_composition_data(self) -> None:
        (self.template_root / "template.local.yaml").write_text(
            "display_name: Local Base\nmods: []\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(
            packctl.ConfigError, "template.local.yaml must not define mods"
        ):
            packctl.load_template_config("base")
        with self.assertRaisesRegex(
            packctl.ConfigError, "template.local.yaml must not define mods"
        ):
            packctl.template_mods("base")
        with self.assertRaisesRegex(
            packctl.ConfigError, "template.local.yaml must not define mods"
        ):
            core.list_mods("template:base")
        with self.assertRaisesRegex(
            packctl.ConfigError, "template.local.yaml must not define mods"
        ):
            core.prepare_template_composition(
                template_ids=["base"], minecraft="1.21.1", loader="neoforge"
            )

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
