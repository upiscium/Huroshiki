from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
neoforge = "21.1.234"
'''

PACK_YAML = '''id: demo
display_name: Demo
enabled: true
distribution:
  rsync_target: deploy@example:/packs/demo
minecraft_server:
  ssh_host: ops@example
  stack_dir: /srv/demo
  service: demo
'''

TEMPLATE_YAML = '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Create
    provider: modrinth
    project_id: create
    side: both
  - name: Private
    provider: url
    project_id: private_mod
    url: https://mods.example/private.jar
    side: server
'''


class RepositoryValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.pack_root = self.packs / "demo"
        self.template_root = self.templates / "base"
        source = self.pack_root / "source"
        (source / "mods").mkdir(parents=True)
        self.template_root.mkdir(parents=True)
        (self.pack_root / "pack.yaml").write_text(PACK_YAML, encoding="utf-8")
        (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
        (source / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        (source / "mods" / "create.pw.toml").write_text(
            'name = "Create"\nfilename = "create.jar"\nside = "both"\n',
            encoding="utf-8",
        )
        (self.template_root / "template.yaml").write_text(
            TEMPLATE_YAML, encoding="utf-8"
        )
        (self.pack_root / "dist" / "client").mkdir(parents=True)
        (self.pack_root / "dist" / "client" / "sentinel").write_text(
            "unchanged", encoding="utf-8"
        )
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

    def snapshot(self) -> dict[Path, bytes]:
        return {
            path.relative_to(self.root): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def validate_all(self) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = packctl.cmd_validate(type("Args", (), {})())
        return result, stdout.getvalue(), stderr.getvalue()

    def test_validates_all_projects_successfully_without_side_effects(self) -> None:
        before = self.snapshot()
        with patch.object(packctl, "run") as run, patch.object(
            packctl, "urlopen"
        ) as urlopen:
            result, stdout, stderr = self.validate_all()

        self.assertEqual(result, 0)
        self.assertIn("Validated 1 pack(s) and 1 template(s)", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(self.snapshot(), before)
        run.assert_not_called()
        urlopen.assert_not_called()

    def test_accumulates_pack_and_template_errors_with_paths(self) -> None:
        (self.pack_root / "pack.yaml").write_text(
            '''id: wrong
display_name: ""
enabled: "yes"
distribution: []
minecraft_server:
  ssh_host: ""
''',
            encoding="utf-8",
        )
        (self.pack_root / "source" / "index.toml").unlink()
        (self.pack_root / "source" / "pack.toml").write_text(
            '''[versions]
minecraft = ""
forge = "47.0.0"
fabric = "0.16.0"
''',
            encoding="utf-8",
        )
        metadata = self.pack_root / "source" / "mods" / "create.pw.toml"
        metadata.write_text('name = "Create"\nside = "unknown"\n', encoding="utf-8")
        (self.template_root / "template.yaml").write_text(
            '''id: other
display_name: ""
enabled: enabled
minecraft: ""
loader: unknown
mods:
  - provider: url
    project_id: private
    url: file:///tmp/private.jar
    side: nowhere
  - provider: invalid
    project_id: create
  - provider: url
    project_id: ""
    url: https://mods.example/private.zip
''',
            encoding="utf-8",
        )

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 1)
        self.assertIn("Validation failed with", stderr)
        for expected in (
            "packs/demo/pack.yaml: id 'wrong' must match directory name 'demo'",
            "packs/demo/pack.yaml: enabled must be a boolean",
            "packs/demo/source/index.toml: missing required file",
            "must define exactly one supported loader",
            "packs/demo/source/mods/create.pw.toml: side must be client, server, or both",
            "templates/base/template.yaml: id 'other' must match directory name 'base'",
            "templates/base/template.yaml: loader must be one of",
            "reference_loader_version must be a non-empty string",
            "mods[0].url must be a public http(s) URL",
            "mods[1].provider must be modrinth, curseforge, or url",
            "mods[2].project_id must be a non-empty string",
            "mods[2].url must point to a .jar file",
        ):
            self.assertIn(expected, stderr)
        self.assertGreaterEqual(stderr.count("  - "), 15)

    def test_reports_yaml_shape_and_toml_parse_errors_together(self) -> None:
        (self.pack_root / "pack.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
        (self.pack_root / "pack.local.yaml").write_text(
            "- not\n- a mapping\n", encoding="utf-8"
        )
        (self.pack_root / "source" / "pack.toml").write_text(
            "[versions\n", encoding="utf-8"
        )
        (self.template_root / "template.yaml").write_text(
            "- not\n- a mapping\n", encoding="utf-8"
        )

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 1)
        self.assertIn("packs/demo/pack.yaml", stderr)
        self.assertIn("packs/demo/pack.local.yaml", stderr)
        self.assertIn("must contain a YAML mapping", stderr)
        self.assertIn("packs/demo/source/pack.toml", stderr)
        self.assertIn("templates/base/template.yaml", stderr)

    def test_requires_source_files_and_every_deployment_field(self) -> None:
        (self.pack_root / "source" / "pack.toml").unlink()
        (self.pack_root / "source" / "index.toml").unlink()
        (self.pack_root / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n"
            "distribution: {}\nminecraft_server: {}\n",
            encoding="utf-8",
        )

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 1)
        for expected in (
            "distribution.rsync_target must be a non-empty string",
            "minecraft_server.ssh_host must be a non-empty string",
            "minecraft_server.stack_dir must be a non-empty string",
            "minecraft_server.service must be a non-empty string",
            "source/pack.toml: missing required file",
            "source/index.toml: missing required file",
        ):
            self.assertIn(expected, stderr)

    def test_packwiz_versions_require_non_empty_strings(self) -> None:
        pack_toml = self.pack_root / "source" / "pack.toml"
        pack_toml.write_text(
            '[versions]\nminecraft = 121\nneoforge = "21.1.234"\n',
            encoding="utf-8",
        )

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 1)
        self.assertIn("versions.minecraft must be a non-empty string", stderr)

        pack_toml.write_text(
            '[versions]\nminecraft = "1.21.1"\nneoforge = 211234\n',
            encoding="utf-8",
        )
        result, _, stderr = self.validate_all()
        self.assertEqual(result, 1)
        self.assertIn("versions.neoforge must be a non-empty string", stderr)

    def test_local_deployment_overrides_are_validated_after_merge(self) -> None:
        (self.pack_root / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n",
            encoding="utf-8",
        )
        (self.pack_root / "pack.local.yaml").write_text(
            '''distribution:
  rsync_target: local:/demo
minecraft_server:
  ssh_host: local
  stack_dir: /srv/demo
  service: demo
''',
            encoding="utf-8",
        )

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")

    def test_disallowed_local_fields_are_rejected_by_schema(self) -> None:
        (self.pack_root / "pack.local.yaml").write_text(
            "id: other\ndisplay_name: ''\nenabled: 'yes'\n"
            "url_max_jar_size_bytes: 0\n",
            encoding="utf-8",
        )
        (self.template_root / "template.local.yaml").write_text(
            "id: other\nloader: unsupported\nmods: invalid\n"
            "url_max_jar_size_bytes: false\n",
            encoding="utf-8",
        )

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 1)
        for expected in (
            "pack.local.yaml: unsupported machine-local key 'id'",
            "template.local.yaml: id is committed semantic data",
        ):
            self.assertIn(expected, stderr)

    def test_reports_malformed_template_local_yaml(self) -> None:
        (self.template_root / "template.local.yaml").write_text(
            "invalid: [", encoding="utf-8"
        )

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 1)
        self.assertIn("templates/base/template.local.yaml", stderr)

    def test_template_local_yaml_cannot_supply_missing_committed_fields(self) -> None:
        (self.template_root / "template.yaml").write_text(
            "id: base\n", encoding="utf-8"
        )
        (self.template_root / "template.local.yaml").write_text(
            '''display_name: Local Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods: []
''',
            encoding="utf-8",
        )

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 1)
        for field in (
            "display_name",
            "enabled",
            "minecraft",
            "loader",
            "reference_loader_version",
            "mods",
        ):
            self.assertIn(field, stderr)

    def test_template_local_yaml_rejects_mods_even_when_committed_mods_exist(self) -> None:
        (self.template_root / "template.local.yaml").write_text(
            "mods: []\n", encoding="utf-8"
        )

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 1)
        self.assertIn("template.local.yaml: mods is committed semantic data", stderr)

    def test_pack_local_schema_allows_only_operational_fields(self) -> None:
        allowed_values = {
            "distribution": "distribution:\n  rsync_target: local:/demo\n",
            "minecraft_server": (
                "minecraft_server:\n"
                "  ssh_host: local\n"
                "  stack_dir: /srv/demo\n"
                "  service: demo\n"
            ),
            "url_max_jar_size_bytes": "url_max_jar_size_bytes: 1024\n",
        }
        local = self.pack_root / "pack.local.yaml"
        for key, text in allowed_values.items():
            with self.subTest(key=key):
                local.write_text(text, encoding="utf-8")
                self.assertEqual(self.validate_all()[0], 0)
                packctl.load_pack_config("demo")

        rejected = {
            "identity": "id: other\n",
            "packwiz semantic": "minecraft: 1.20.1\n",
            "unknown top-level": "future_setting: true\n",
            "unknown nested": "distribution:\n  future_target: local:/demo\n",
        }
        for label, text in rejected.items():
            with self.subTest(label=label):
                local.write_text(text, encoding="utf-8")
                result, _, stderr = self.validate_all()
                self.assertEqual(result, 1)
                self.assertIn("unsupported machine-local key", stderr)
                with self.assertRaisesRegex(
                    packctl.ConfigError, "unsupported machine-local key"
                ):
                    packctl.load_pack_config("demo")

    def test_template_validation_and_runtime_share_local_policy(self) -> None:
        local = self.template_root / "template.local.yaml"
        cases: dict[str, tuple[str, bool]] = {
            "allowed": ("url_max_jar_size_bytes: 1024\n", True),
            "unknown": ("future_setting: true\n", False),
            "invalid value": ("url_max_jar_size_bytes: false\n", False),
            "null value": ("url_max_jar_size_bytes: null\n", False),
        }
        cases.update(
            (f"semantic {key}", (f"{key}: local\n", False))
            for key in packctl.TEMPLATE_COMMITTED_KEYS
        )
        for label, (text, valid) in cases.items():
            with self.subTest(label=label):
                local.write_text(text, encoding="utf-8")
                result, _, _ = self.validate_all()
                if valid:
                    self.assertEqual(result, 0)
                    packctl.load_template_config("base")
                else:
                    self.assertEqual(result, 1)
                    with self.assertRaises(packctl.ConfigError):
                        packctl.load_template_config("base")

    def test_aggregate_validation_reports_legacy_source_before_bad_manifest(self) -> None:
        (self.template_root / "source").mkdir()
        (self.template_root / "template.yaml").write_text("invalid: [", encoding="utf-8")

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 1)
        self.assertIn("templates/base/source: legacy template source is not supported", stderr)
        self.assertIn("templates/base/template.yaml", stderr)

    def test_individual_validation_rejects_legacy_source_symlink(self) -> None:
        (self.template_root / "source").symlink_to(
            self.root / "missing-legacy-source", target_is_directory=True
        )
        args = type("Args", (), {"template": "base"})()
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = packctl.cmd_validate_template(args)

        self.assertEqual(result, 1)
        self.assertIn("templates/base/source: legacy template source is not supported", stderr.getvalue())

    def test_validate_for_ignores_templates_and_other_packs(self) -> None:
        (self.template_root / "template.yaml").write_text("invalid: [", encoding="utf-8")
        other = self.packs / "other"
        other.mkdir()
        args = type("Args", (), {"pack": "demo"})()
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = packctl.cmd_validate_for(args)

        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("Validated pack demo", stdout.getvalue())

    def test_full_validation_finds_directories_without_manifests(self) -> None:
        missing_pack = self.packs / "missing-pack"
        missing_template = self.templates / "missing-template"
        missing_pack.mkdir()
        missing_template.mkdir()

        result, _, stderr = self.validate_all()

        self.assertEqual(result, 1)
        self.assertIn("packs/missing-pack/pack.yaml: missing required file", stderr)
        self.assertIn(
            "templates/missing-template/template.yaml: missing required file", stderr
        )

    def test_cli_parser_exposes_both_validation_commands(self) -> None:
        validate_args = packctl.parser().parse_args(["validate"])
        focused_args = packctl.parser().parse_args(["validate-for", "demo"])

        self.assertIs(validate_args.func, packctl.cmd_validate)
        self.assertIs(focused_args.func, packctl.cmd_validate_for)
        self.assertEqual(focused_args.pack, "demo")
        template_args = packctl.parser().parse_args(["validate-template", "base"])
        self.assertIs(template_args.func, packctl.cmd_validate_template)


if __name__ == "__main__":
    unittest.main()
