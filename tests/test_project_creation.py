from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


class ProjectCreationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.patches = [
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

    @staticmethod
    def args(kind: str, **overrides: str) -> argparse.Namespace:
        values = {
            "display_name": 'Create: My Pack #1 "航行" \'Delight\'',
            "minecraft": "1.21.1",
            "loader": "neoforge",
            "loader_version": "21.1.234",
            "pack" if kind == "pack" else "template": "generated",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_pack_special_characters_round_trip_with_ordered_fields(self) -> None:
        args = self.args("pack")

        def create_source(root: Path, **_: str) -> None:
            (root / "source").mkdir(parents=True)

        with patch.object(packctl, "init_packwiz_project", side_effect=create_source):
            packctl._new_pack(args)

        config = packctl.load_pack_config("generated")
        self.assertEqual(
            list(config),
            ["id", "display_name", "enabled", "distribution", "minecraft_server"],
        )
        self.assertEqual(config["id"], "generated")
        self.assertEqual(config["display_name"], args.display_name)
        self.assertIs(config["enabled"], True)
        self.assertEqual(
            config["distribution"],
            {"rsync_target": "dockge:/opt/stacks/packwiz-web/packs/generated"},
        )
        self.assertEqual(
            config["minecraft_server"],
            {
                "ssh_host": "minecraft",
                "stack_dir": "/opt/stacks/generated",
                "service": "generated",
            },
        )
        self.assertIn("航行", (self.packs / "generated" / "pack.yaml").read_text())

    def test_pack_and_template_preserve_the_same_special_display_name(self) -> None:
        pack_args = self.args("pack")
        template_args = self.args("template")

        def create_source(root: Path, **_: str) -> None:
            (root / "source").mkdir(parents=True)

        with patch.object(packctl, "init_packwiz_project", side_effect=create_source):
            packctl._new_pack(pack_args)
        packctl._new_template(template_args)

        pack = packctl.load_yaml(self.packs / "generated" / "pack.yaml")
        template = packctl.load_yaml(self.templates / "generated" / "template.yaml")
        self.assertEqual(pack["display_name"], pack_args.display_name)
        self.assertEqual(template["display_name"], pack_args.display_name)

    def test_pack_and_template_reject_newlines_and_controls_without_partial_root(self) -> None:
        cases = (
            ("display_name", "Line one\nLine two"),
            ("display_name", "Bell\x07Name"),
            ("display_name", "Line one\u2028Line two"),
            ("minecraft", "1.21\r1"),
            ("loader_version", "21.1\t234"),
        )
        for kind, creator, parent in (
            ("pack", packctl._new_pack, self.packs),
            ("template", packctl._new_template, self.templates),
        ):
            for field, value in cases:
                with self.subTest(kind=kind, field=field, value=repr(value)):
                    args = self.args(kind, **{field: value})
                    with self.assertRaisesRegex(
                        packctl.ConfigError,
                        "must not contain control characters or newlines",
                    ):
                        creator(args)
                    self.assertFalse((parent / "generated").exists())

    def test_cli_and_tui_validate_before_acquiring_project_lock(self) -> None:
        args = self.args("pack", display_name="Invalid\nName")
        with patch.object(packctl, "ProjectLock") as project_lock:
            with self.assertRaises(packctl.ConfigError):
                packctl.cmd_new(args)
        project_lock.assert_not_called()

        with patch.object(packctl, "ProjectLock") as project_lock:
            with self.assertRaises(core.HuroshikiError):
                core.create_project(
                    kind="pack",
                    project_id="generated",
                    display_name="Invalid\nName",
                    minecraft="1.21.1",
                    loader="neoforge",
                    loader_version="21.1.234",
                )
        project_lock.assert_not_called()
        self.assertFalse((self.packs / "generated").exists())


if __name__ == "__main__":
    unittest.main()
