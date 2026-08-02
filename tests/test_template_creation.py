from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


PACK_TOML = '''name = "Generated"
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


class TemplateCreationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner_patch = patch.object(
            core,
            "run_resolver_process",
            side_effect=self.run_fake_resolver,
        )
        self.packctl_runner_patch = patch.object(
            packctl,
            "run_bounded_process",
            side_effect=self.run_fake_resolver,
        )
        self.runner_patch.start()
        self.packctl_runner_patch.start()

    def tearDown(self) -> None:
        self.packctl_runner_patch.stop()
        self.runner_patch.stop()

    @staticmethod
    def run_fake_resolver(
        command, *, cwd, cancel_event, deadline, result_callback=None
    ):
        result = core.subprocess.run(command, cwd=cwd, check=False)
        resolved = core.ResolverProcessResult(
            result.returncode,
            result.stdout or "",
            result.stderr or "",
            False,
            False,
        )
        if result_callback is not None:
            result_callback(resolved)
        return resolved

    def test_report_optional_collections_default_to_empty(self) -> None:
        report = core.TemplateCreationReport("pack:generated", ("base",))
        self.assertEqual(report.installed, ())
        self.assertEqual(report.failed, ())

    def test_singular_api_delegates_to_plural_api(self) -> None:
        expected = object()
        with patch.object(core, "create_pack_from_templates", return_value=expected) as plural:
            report = core.create_pack_from_template(
                template_id="base",
                project_id="generated",
                display_name="Generated",
                minecraft="1.21.1",
                loader="neoforge",
                loader_version="21.1.999",
            )
        self.assertIs(report, expected)
        self.assertEqual(plural.call_args.kwargs["template_ids"], ["base"])

    def test_all_templates_and_conflicts_are_validated_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            for template_id, minecraft, provider, remote_id in (
                ("first", "1.21.1", "modrinth", "one"),
                ("second", "1.20.1", "curseforge", "2"),
            ):
                template = templates / template_id
                template.mkdir(parents=True)
                (template / "template.yaml").write_text(
                    f'''id: {template_id}
display_name: {template_id}
enabled: true
minecraft: {minecraft}
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Conflict
    provider: {provider}
    project_id: "{remote_id}"
    side: both
''',
                    encoding="utf-8",
                )
            with (
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project") as create,
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "second must use Minecraft"):
                    core.create_pack_from_templates(
                        template_ids=["first", "second"],
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
            create.assert_not_called()

    def test_unresolved_and_stale_conflicts_fail_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            for template_id, provider, remote_id in (
                ("first", "modrinth", "one"),
                ("second", "curseforge", "2"),
            ):
                template = templates / template_id
                template.mkdir(parents=True)
                (template / "template.yaml").write_text(
                    f'''id: {template_id}
display_name: {template_id}
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Conflict
    provider: {provider}
    project_id: "{remote_id}"
    side: both
''',
                    encoding="utf-8",
                )
            arguments = dict(
                template_ids=["first", "second"],
                project_id="generated",
                display_name="Generated",
                minecraft="1.21.1",
                loader="neoforge",
                loader_version="21.1.999",
            )
            with (
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project") as create,
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "Unresolved"):
                    core.create_pack_from_templates(**arguments)
                with self.assertRaisesRegex(core.HuroshikiError, "Unknown or stale"):
                    core.create_pack_from_templates(
                        **arguments,
                        conflict_resolutions={
                            "stale": core.ConflictResolution(("modrinth:one",))
                        },
                    )
            create.assert_not_called()

    def test_third_candidate_added_after_preview_aborts_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            for template_id, provider, remote_id in (
                ("first", "modrinth", "one"),
                ("second", "curseforge", "2"),
            ):
                template_root = templates / template_id
                template_root.mkdir(parents=True)
                (template_root / "template.yaml").write_text(
                    f"""id: {template_id}
display_name: {template_id}
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Conflict
    provider: {provider}
    project_id: \"{remote_id}\"
    side: both
""",
                    encoding="utf-8",
                )
            patches = (
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            )
            for item in patches:
                item.start()
            try:
                preview = core.prepare_template_composition(
                    template_ids=["first", "second"],
                    minecraft="1.21.1",
                    loader="neoforge",
                )
                with (templates / "second" / "template.yaml").open(
                    "a", encoding="utf-8"
                ) as manifest:
                    manifest.write(
                        "  - name: Conflict\n"
                        "    provider: url\n"
                        "    project_id: three\n"
                        "    url: https://example.test/three.jar\n"
                        "    side: both\n"
                    )
                conflict = preview.conflicts[0]
                resolutions = {
                    conflict.key: core.ConflictResolution(
                        (conflict.candidates[0].candidate_key,)
                    )
                }
                with patch.object(core, "create_project") as create:
                    with self.assertRaisesRegex(
                        core.HuroshikiError, "changed after preview"
                    ):
                        core.create_pack_from_templates(
                            template_ids=["first", "second"],
                            project_id="generated",
                            display_name="Generated",
                            minecraft="1.21.1",
                            loader="neoforge",
                            loader_version="21.1.999",
                            conflict_resolutions=resolutions,
                            expected_composition=preview,
                        )
                create.assert_not_called()
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_creation_uses_already_held_lock_without_self_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template = templates / "base"
            template.mkdir(parents=True)
            (template / "template.yaml").write_text(
                "id: base\ndisplay_name: Base\nenabled: true\n"
                "minecraft: 1.21.1\nloader: neoforge\n"
                "reference_loader_version: 21.1.234\nmods: []\n",
                encoding="utf-8",
            )

            def fake_packwiz(command, *, cwd=None, **kwargs):
                source = Path(cwd)
                (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
                (source / "index.toml").write_text(
                    'hash-format = "sha256"\n', encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core.subprocess, "run", side_effect=fake_packwiz),
            ):
                report = core.create_pack_from_template(
                    template_id="base",
                    project_id="generated",
                    display_name="Generated",
                    minecraft="1.21.1",
                    loader="neoforge",
                    loader_version="21.1.999",
                )

            self.assertEqual(report.pack_key, "pack:generated")
            self.assertTrue((packs / "generated" / "pack.yaml").is_file())

    def test_invalid_url_is_rejected_before_pack_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Broken URL
    provider: url
    project_id: broken
    url: https://example.invalid/broken.zip
    side: both
''',
                encoding="utf-8",
            )
            with (
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project") as create,
            ):
                with self.assertRaisesRegex(packctl.ConfigError, r"\.jar file"):
                    core.create_pack_from_template(
                        template_id="base",
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
            create.assert_not_called()
            self.assertFalse((packs / "generated").exists())

    def test_malformed_manifest_is_validated_before_pack_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Broken
    provider: unsupported
    project_id: broken
    side: both
''',
                encoding="utf-8",
            )
            patches = [
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for item in patches:
                item.start()
            try:
                with patch.object(core, "create_project") as create:
                    with self.assertRaises(packctl.ConfigError):
                        core.create_pack_from_template(
                            template_id="base",
                            project_id="generated",
                            display_name="Generated",
                            minecraft="1.21.1",
                            loader="neoforge",
                            loader_version="21.1.999",
                        )
                create.assert_not_called()
                self.assertFalse((packs / "generated").exists())
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_fatal_error_after_creation_removes_destination_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Fatal
    provider: modrinth
    project_id: fatal
    side: both
''',
                encoding="utf-8",
            )
            patches = [
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for item in patches:
                item.start()

            def fake_create(*args, **kwargs):
                source = packs / "generated" / "source"
                (source / "mods").mkdir(parents=True)
                (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
                (source / "index.toml").write_text("original index\n", encoding="utf-8")
                return 0

            try:
                with patch.object(
                    core,
                    "_resolve_template_root",
                    side_effect=core.HuroshikiError("No compatible files"),
                ), patch.object(
                    core, "create_project", side_effect=fake_create
                ), patch.object(
                    core.subprocess, "run", side_effect=OSError("packwiz unavailable")
                ):
                    with self.assertRaisesRegex(OSError, "packwiz unavailable"):
                        core.create_pack_from_template(
                            template_id="base",
                            project_id="generated",
                            display_name="Generated",
                            minecraft="1.21.1",
                            loader="neoforge",
                            loader_version="21.1.999",
                        )
                self.assertFalse((packs / "generated").exists())
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_resolver_oserror_aborts_before_destination_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Fatal
    provider: modrinth
    project_id: fatal
    side: both
''',
                encoding="utf-8",
            )

            with (
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project") as create,
                patch.object(
                    core.subprocess,
                    "run",
                    side_effect=OSError("packwiz unavailable"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "packwiz unavailable"):
                    core.create_pack_from_template(
                        template_id="base",
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )

            create.assert_not_called()
            self.assertFalse((packs / "generated").exists())

    def test_final_refresh_bounded_failures_remove_owned_destination(self) -> None:
        failures = (
            (core.ResolverProcessResult(1, "", "refresh failed", False, False), "refresh failed"),
            (core.ResolverProcessResult(-15, "", "", False, True), "timed out"),
            (core.ResolverProcessResult(-15, "", "", True, False), "cancelled"),
            (
                core.ResolverProcessResult(0, "", "", False, False, True, False),
                "background processes",
            ),
            (
                core.ResolverProcessResult(0, "", "", False, False, False, True),
                "termination was incomplete",
            ),
        )
        for failure, message in failures:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                packs = root / "packs"
                templates = root / "templates"
                template = templates / "base"
                template.mkdir(parents=True)
                (template / "template.yaml").write_text(
                    "id: base\ndisplay_name: Base\n"
                    "minecraft: 1.21.1\nloader: neoforge\n"
                    "reference_loader_version: 21.1.234\nmods: []\n",
                    encoding="utf-8",
                )

                def fake_create(*args, **kwargs):
                    source = packs / "generated" / "source"
                    source.mkdir(parents=True)
                    (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
                    (source / "index.toml").write_text("index\n", encoding="utf-8")
                    return 0

                with (
                    patch.object(core, "ROOT", root),
                    patch.object(core, "PACKS", packs),
                    patch.object(core, "TEMPLATES", templates),
                    patch.object(packctl, "ROOT", root),
                    patch.object(packctl, "PACKS", packs),
                    patch.object(packctl, "TEMPLATES", templates),
                    patch.object(core, "create_project", side_effect=fake_create),
                    patch.object(core, "run_resolver_process", return_value=failure),
                ):
                    with self.assertRaisesRegex(core.HuroshikiError, message):
                        core.create_pack_from_template(
                            template_id="base",
                            project_id="generated",
                            display_name="Generated",
                            minecraft="1.21.1",
                            loader="neoforge",
                            loader_version="21.1.999",
                        )
                    self.assertFalse((packs / "generated").exists())
                    with packctl.ProjectLock("pack:generated", "test lock release"):
                        pass

    def test_packwiz_init_timeout_removes_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template = templates / "base"
            template.mkdir(parents=True)
            (template / "template.yaml").write_text(
                "id: base\ndisplay_name: Base\n"
                "minecraft: 1.21.1\nloader: neoforge\n"
                "reference_loader_version: 21.1.234\nmods: []\n",
                encoding="utf-8",
            )
            timeout = core.ResolverProcessResult(-15, "", "", False, True)
            with (
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(packctl, "run_bounded_process", return_value=timeout),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "timed out"):
                    core.create_pack_from_template(
                        template_id="base",
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
                self.assertFalse((packs / "generated").exists())
                with packctl.ProjectLock("pack:generated", "test lock release"):
                    pass

    def test_source_initialization_failure_removes_owned_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template = templates / "base"
            template.mkdir(parents=True)
            (template / "template.yaml").write_text(
                "id: base\ndisplay_name: Base\nminecraft: 1.21.1\n"
                "loader: neoforge\nreference_loader_version: 21.1.234\nmods: []\n",
                encoding="utf-8",
            )

            def fake_create(*args, **kwargs):
                (packs / "generated").mkdir(parents=True)
                return 0

            with (
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project", side_effect=fake_create),
                patch.object(core, "project_source", side_effect=OSError("source failed")),
            ):
                with self.assertRaisesRegex(OSError, "source failed"):
                    core.create_pack_from_template(
                        template_id="base",
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
            self.assertFalse((packs / "generated").exists())

    def test_fatal_error_reports_destination_rollback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Fatal
    provider: modrinth
    project_id: fatal
    side: both
''',
                encoding="utf-8",
            )

            def fake_create(*args, **kwargs):
                source = packs / "generated" / "source"
                (source / "mods").mkdir(parents=True)
                (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
                (source / "index.toml").write_text("original", encoding="utf-8")
                return 0

            real_rmtree = core.shutil.rmtree
            destination = packs / "generated"

            def failed_rollback(path, *args, **kwargs):
                if Path(path) == destination:
                    raise OSError("destination is busy")
                return real_rmtree(path, *args, **kwargs)

            with (
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(
                    core,
                    "_resolve_template_root",
                    side_effect=core.HuroshikiError("No compatible files"),
                ),
                patch.object(core, "create_project", side_effect=fake_create),
                patch.object(core.subprocess, "run", side_effect=OSError("packwiz unavailable")),
                patch.object(core.shutil, "rmtree", side_effect=failed_rollback),
            ):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "failed to roll back.*destination is busy"
                ) as raised:
                    core.create_pack_from_template(
                        template_id="base",
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertIn("packwiz unavailable", str(raised.exception.__cause__))

    def test_create_failure_does_not_claim_or_remove_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods: []
''',
                encoding="utf-8",
            )

            def failed_create(*args, **kwargs):
                destination = packs / "generated"
                destination.mkdir(parents=True)
                (destination / "diagnostic.txt").write_text("retained", encoding="utf-8")
                return 1

            with (
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project", side_effect=failed_create),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, "Failed to create"):
                    core.create_pack_from_template(
                        template_id="base",
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
            self.assertEqual(
                (packs / "generated" / "diagnostic.txt").read_text(), "retained"
            )

    def test_partial_install_keeps_successes_and_reports_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Works
    provider: modrinth
    project_id: works
    side: client
  - name: Wrong loader version
    provider: curseforge
    project_id: "404"
    side: server
''',
                encoding="utf-8",
            )
            addon_root = templates / "addon"
            addon_root.mkdir(parents=True)
            (addon_root / "template.yaml").write_text(
                '''id: addon
display_name: Addon
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Works from addon
    provider: modrinth
    project_id: works
    side: server
''',
                encoding="utf-8",
            )

            patches = [
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for item in patches:
                item.start()

            def fake_create(*args, **kwargs):
                pack_root = packs / "generated"
                (pack_root / "source" / "mods").mkdir(parents=True)
                (pack_root / "source" / "pack.toml").write_text(
                    PACK_TOML, encoding="utf-8"
                )
                (pack_root / "source" / "index.toml").write_text(
                    'hash-format = "sha256"\n', encoding="utf-8"
                )
                (pack_root / "pack.yaml").write_text(
                    "id: generated\ndisplay_name: Generated\nenabled: true\n",
                    encoding="utf-8",
                )
                return 0

            real_run = subprocess.run

            def fake_run(
                command,
                *,
                cwd=None,
                text=None,
                capture_output=False,
                check=False,
                timeout=None,
            ):
                if command[-1] == "works":
                    (Path(cwd) / "mods" / "works.pw.toml").write_text(
                        '''name = "Works"\nfilename = "works.jar"\nside = "both"\n[download]\nhash-format = "sha256"\nhash = "00"\nurl = "https://example.invalid"\n[update.modrinth]\nmod-id = "works"\nversion = "v"\n''',
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, "", "")
                if "--addon-id" in command:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        "",
                        "No compatible files for the selected loader version",
                    )
                if command[-1] == "refresh":
                    return subprocess.CompletedProcess(command, 0, "", "")
                return real_run(
                    command,
                    cwd=cwd,
                    text=text,
                    capture_output=capture_output,
                    check=check,
                )

            try:
                with patch.object(core, "create_project", side_effect=fake_create), patch.object(
                    core.subprocess, "run", side_effect=fake_run
                ):
                    report = core.create_pack_from_templates(
                        template_ids=["base", "addon"],
                        project_id="generated",
                        display_name="Generated",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.999",
                    )
                self.assertEqual(report.installed, ("Works",))
                self.assertEqual(report.template_ids, ("base", "addon"))
                self.assertEqual(len(report.failed), 1)
                self.assertIn("No compatible files", report.failed[0].reason)
                self.assertIn("Applied templates: base -> addon", report.warning_lines)
                self.assertTrue(
                    (packs / "generated" / "source" / "mods" / "works.pw.toml").exists()
                )
                self.assertEqual(
                    packctl.read_toml(
                        packs / "generated" / "source" / "mods" / "works.pw.toml"
                    )["side"],
                    "both",
                )
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_changed_shared_dependency_unions_client_and_server_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template = templates / "base"
            template.mkdir(parents=True)
            (template / "template.yaml").write_text(
                """id: base
display_name: Base
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Client Root
    provider: modrinth
    project_id: client-root
    side: client
  - name: Server Root
    provider: modrinth
    project_id: server-root
    side: server
""",
                encoding="utf-8",
            )

            def fake_create(*args, **kwargs):
                source = packs / "generated" / "source"
                (source / "mods").mkdir(parents=True)
                (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
                (source / "index.toml").write_text("index\n", encoding="utf-8")
                return 0

            def write_metadata(path: Path, mod_id: str, side: str) -> None:
                path.write_text(
                    f'name = "{mod_id}"\nfilename = "{mod_id}.jar"\n'
                    f'side = "{side}"\n[update.modrinth]\nmod-id = "{mod_id}"\n',
                    encoding="utf-8",
                )

            def fake_run(command, *, cwd=None, **kwargs):
                source = Path(cwd)
                if command[-1] == "refresh":
                    return subprocess.CompletedProcess(command, 0, "", "")
                root_id = command[-1]
                root_side = "client" if root_id == "client-root" else "server"
                write_metadata(source / "mods" / f"{root_id}.pw.toml", root_id, "both")
                write_metadata(source / "mods" / "shared.pw.toml", "shared", root_side)
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project", side_effect=fake_create),
                patch.object(core.subprocess, "run", side_effect=fake_run),
            ):
                core.create_pack_from_template(
                    template_id="base",
                    project_id="generated",
                    display_name="Generated",
                    minecraft="1.21.1",
                    loader="neoforge",
                    loader_version="21.1.999",
                )

            mods = packs / "generated" / "source" / "mods"
            self.assertEqual(packctl.read_toml(mods / "client-root.pw.toml")["side"], "client")
            self.assertEqual(packctl.read_toml(mods / "server-root.pw.toml")["side"], "server")
            self.assertEqual(packctl.read_toml(mods / "shared.pw.toml")["side"], "both")

    def test_new_client_dependency_stays_out_of_server_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            template = templates / "base"
            template.mkdir(parents=True)
            (template / "template.yaml").write_text(
                """id: base
display_name: Base
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Client Root
    provider: modrinth
    project_id: client-root
    side: client
""",
                encoding="utf-8",
            )

            def fake_create(*args, **kwargs):
                source = packs / "generated" / "source"
                (source / "mods").mkdir(parents=True)
                (source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
                (source / "index.toml").write_text("index\n", encoding="utf-8")
                return 0

            def write_metadata(path: Path, mod_id: str) -> None:
                path.write_text(
                    f'name = "{mod_id}"\nfilename = "{mod_id}.jar"\n'
                    f'side = "both"\n[update.modrinth]\nmod-id = "{mod_id}"\n',
                    encoding="utf-8",
                )

            def fake_run(command, *, cwd=None, **kwargs):
                if command[-1] != "refresh":
                    source = Path(cwd)
                    write_metadata(source / "mods" / "client-root.pw.toml", "client-root")
                    write_metadata(source / "mods" / "client-dependency.pw.toml", "client-dependency")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(core, "ROOT", root),
                patch.object(core, "PACKS", packs),
                patch.object(core, "TEMPLATES", templates),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
                patch.object(core, "create_project", side_effect=fake_create),
                patch.object(core.subprocess, "run", side_effect=fake_run),
            ):
                core.create_pack_from_template(
                    template_id="base",
                    project_id="generated",
                    display_name="Generated",
                    minecraft="1.21.1",
                    loader="neoforge",
                    loader_version="21.1.999",
                )

            pack_root = packs / "generated"
            dependency = pack_root / "source" / "mods" / "client-dependency.pw.toml"
            self.assertEqual(packctl.read_toml(dependency)["side"], "client")
            server = pack_root / "server-test"
            with patch.object(packctl, "run_packwiz"):
                self.assertEqual(packctl.build_target(pack_root, "server", server), [])
            self.assertFalse((server / "mods" / dependency.name).exists())


if __name__ == "__main__":
    unittest.main()
