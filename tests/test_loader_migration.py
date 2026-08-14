from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import tomlkit

import huroshiki_core as core
import packctl


PACK_TOML = """name = "Demo"
author = "Test"
pack-format = "packwiz:1.1.0"

[versions]
minecraft = "1.21.1"
neoforge = "21.1.1"
"""

URL_METADATA = """name = "Private MOD"
filename = "private.jar"
side = "both"

[download]
url = "https://mods.example/private.jar"
hash-format = "sha256"
hash = "00"
"""


class LoaderMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.pack = self.packs / "demo"
        self.source = self.pack / "source"
        self.state_root = self.root / ".huroshiki"
        self.log_root = self.state_root / "logs"
        self.source.mkdir(parents=True)
        self.templates.mkdir()
        (self.pack / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n",
            encoding="utf-8",
        )
        (self.source / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
        (self.source / "index.toml").write_text("hash-format = \"sha256\"\n", encoding="utf-8")
        self.patches = (
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.templates),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.templates),
            patch.object(packctl, "STATE_ROOT", self.state_root),
            patch.object(packctl, "LOG_ROOT", self.log_root),
        )
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def fake_packwiz(
        self,
        command: list[str],
        *,
        cwd: Path,
        cancel_event: threading.Event | None,
        deadline: float | None,
    ) -> core.ResolverProcessResult:
        if command[-2:] == ["migrate", "loader"]:
            raise AssertionError("missing loader version")
        if "migrate" in command:
            self.assertEqual(command[:4], ["packwiz", "--yes", "migrate", "loader"])
            requested = command[-1]
            version = {
                "latest": "21.1.3",
                "recommended": "21.1.2",
            }.get(requested, requested)
            document = tomlkit.parse((cwd / "pack.toml").read_text(encoding="utf-8"))
            document["versions"]["neoforge"] = version
            (cwd / "pack.toml").write_text(tomlkit.dumps(document), encoding="utf-8")
        elif command == ["packwiz", "refresh"]:
            (cwd / "index.toml").write_text(
                "hash-format = \"sha256\"\nhash = \"refreshed\"\n",
                encoding="utf-8",
            )
        else:
            raise AssertionError(command)
        return core.ResolverProcessResult(0, "", "", False, False)

    def prepare(self, version: str = "21.1.2") -> core.LoaderMigrationOperation:
        with patch.object(core, "run_resolver_process", side_effect=self.fake_packwiz):
            return core.prepare_loader_migration("pack:demo", version)

    def assert_unlocked(self) -> None:
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_explicit_latest_and_recommended_preview_without_real_changes(self) -> None:
        for requested, expected in (
            ("21.1.4", "21.1.4"),
            ("latest", "21.1.3"),
            ("recommended", "21.1.2"),
        ):
            with self.subTest(requested=requested):
                before = (self.source / "pack.toml").read_bytes()
                operation = self.prepare(requested)
                self.assertEqual(operation.preview.old_version, "21.1.1")
                self.assertEqual(operation.preview.new_version, expected)
                self.assertEqual(
                    tuple(change.relative_path for change in operation.preview.changes),
                    (Path("index.toml"), Path("pack.toml")),
                )
                self.assertEqual((self.source / "pack.toml").read_bytes(), before)
                operation.discard()
                self.assert_unlocked()

    def test_apply_publishes_migration_and_discard_does_not(self) -> None:
        discarded = self.prepare("21.1.2")
        discarded.discard()
        self.assertEqual(packctl.project_versions(self.source)[2], "21.1.1")

        applied = self.prepare("21.1.3")
        with patch.object(core.subprocess, "run") as unbounded_process:
            applied.apply()
        unbounded_process.assert_not_called()
        self.assertEqual(packctl.project_versions(self.source)[2], "21.1.3")
        self.assertIn("refreshed", (self.source / "index.toml").read_text())
        self.assert_unlocked()

    def test_migration_and_refresh_failures_discard_and_unlock(self) -> None:
        for failed_step in ("migrate", "refresh"):
            with self.subTest(failed_step=failed_step):
                before = core.tree_digest_snapshot(self.source)

                def fail(command: list[str], **kwargs: object) -> core.ResolverProcessResult:
                    if failed_step in command:
                        return core.ResolverProcessResult(7, "", "failed", False, False)
                    return self.fake_packwiz(command, **kwargs)

                with patch.object(core, "run_resolver_process", side_effect=fail):
                    with self.assertRaisesRegex(core.HuroshikiError, "failed with exit 7"):
                        core.prepare_loader_migration("pack:demo", "21.1.2")
                self.assertEqual(core.tree_digest_snapshot(self.source), before)
                self.assert_unlocked()

    def test_successful_diagnostics_reach_loader_migration_progress(self) -> None:
        def diagnostic(
            command: list[str], **kwargs: object
        ) -> core.ResolverProcessResult:
            result = self.fake_packwiz(command, **kwargs)
            return core.ResolverProcessResult(
                result.returncode,
                result.stdout,
                "Metadata disagreement: details\nexpected: foo\nactual: bar\n",
                result.cancelled,
                result.timed_out,
                result.orphaned_descendants,
                result.termination_incomplete,
            )

        with patch.object(core, "run_resolver_process", side_effect=diagnostic):
            operation = core.prepare_loader_migration("pack:demo", "21.1.2")
        progress = operation.drain_progress()
        self.assertTrue(
            any("completed with diagnostics" in message for message in progress)
        )
        self.assertTrue(any(".huroshiki/logs/demo/" in message for message in progress))
        self.assertEqual(len(list(self.log_root.rglob("*.log"))), 2)
        operation.discard()

    def test_timeout_and_incomplete_cleanup_fail_closed(self) -> None:
        results = (
            core.ResolverProcessResult(None, "", "", False, True),
            core.ResolverProcessResult(None, "", "", False, False, False, True),
            core.ResolverProcessResult(0, "", "", False, False, True, False),
        )
        patterns = ("timed out", "termination was incomplete", "background")
        for result, pattern in zip(results, patterns, strict=True):
            with self.subTest(pattern=pattern), patch.object(
                core, "run_resolver_process", return_value=result
            ):
                with self.assertRaisesRegex(core.HuroshikiError, pattern):
                    core.prepare_loader_migration("pack:demo", "21.1.2")
                self.assert_unlocked()

    def test_cancellation_waits_for_process_and_discards(self) -> None:
        entered = threading.Event()

        def blocked(
            command: list[str],
            *,
            cancel_event: threading.Event,
            **kwargs: object,
        ) -> core.ResolverProcessResult:
            entered.set()
            self.assertTrue(cancel_event.wait(2))
            return core.ResolverProcessResult(-15, "", "", True, False)

        operation = core.LoaderMigrationOperation("pack:demo", "21.1.2")
        with patch.object(core, "run_resolver_process", side_effect=blocked):
            worker = threading.Thread(target=operation.run)
            worker.start()
            self.assertTrue(entered.wait(2))
            operation.cancel()
            worker.join(3)

        self.assertFalse(worker.is_alive())
        self.assertTrue(operation.done.is_set())
        self.assertTrue(operation.cancelled)
        self.assertIsNone(operation.preview)
        self.assert_unlocked()

    def test_version_invariants_reject_minecraft_or_loader_changes(self) -> None:
        for changed, pattern in (("minecraft", "Minecraft"), ("loader", "loader type")):
            with self.subTest(changed=changed):
                def mutate(command: list[str], **kwargs: object) -> core.ResolverProcessResult:
                    result = self.fake_packwiz(command, **kwargs)
                    if "migrate" in command:
                        source = kwargs["cwd"]
                        document = tomlkit.parse(
                            (source / "pack.toml").read_text(encoding="utf-8")
                        )
                        if changed == "minecraft":
                            document["versions"]["minecraft"] = "1.21.2"
                        else:
                            del document["versions"]["neoforge"]
                            document["versions"]["fabric"] = "0.16.0"
                        (source / "pack.toml").write_text(
                            tomlkit.dumps(document), encoding="utf-8"
                        )
                    return result

                with patch.object(core, "run_resolver_process", side_effect=mutate):
                    with self.assertRaisesRegex(core.HuroshikiError, pattern):
                        core.prepare_loader_migration("pack:demo", "21.1.2")
                self.assertEqual(packctl.project_versions(self.source)[1:], ("neoforge", "21.1.1"))
                self.assert_unlocked()

    def test_external_source_or_pack_config_change_blocks_apply(self) -> None:
        for changed in ("source", "config"):
            with self.subTest(changed=changed):
                operation = self.prepare("21.1.2")
                if changed == "source":
                    (self.source / "external.txt").write_text("external", encoding="utf-8")
                else:
                    (self.pack / "pack.yaml").write_text(
                        "id: demo\ndisplay_name: External\nenabled: true\n",
                        encoding="utf-8",
                    )
                with self.assertRaisesRegex(core.HuroshikiError, "changed"):
                    operation.apply()
                if changed == "source":
                    self.assertEqual((self.source / "external.txt").read_text(), "external")
                    (self.source / "external.txt").unlink()
                else:
                    (self.pack / "pack.yaml").write_text(
                        "id: demo\ndisplay_name: Demo\nenabled: true\n",
                        encoding="utf-8",
                    )
                self.assert_unlocked()

    def test_url_metadata_adds_compatibility_warning(self) -> None:
        mods = self.source / "mods"
        mods.mkdir()
        (mods / "private.pw.toml").write_text(URL_METADATA, encoding="utf-8")
        operation = self.prepare("21.1.2")
        self.assertEqual(
            operation.preview.warnings,
            ("URL MOD compatibility cannot be verified",),
        )
        operation.discard()

    def test_input_and_project_kind_are_validated_before_transaction(self) -> None:
        for project, version in (
            ("template:demo", "21.1.2"),
            ("pack:demo", ""),
            ("pack:demo", " 21.1.2"),
            ("pack:demo", "21.1.2\nnext"),
        ):
            with self.subTest(project=project, version=version):
                with self.assertRaises(core.HuroshikiError):
                    core.LoaderMigrationOperation(project, version)


class LoaderMigrationCliTest(unittest.TestCase):
    def test_cli_preview_and_apply_contract(self) -> None:
        preview = core.LoaderMigrationPreview(
            "pack:demo",
            "1.21.1",
            "neoforge",
            "21.1.1",
            "21.1.2",
            (core.UpdateChange(Path("pack.toml"), b"old", b"new"),),
            ("warning",),
        )
        for apply in (False, True):
            with self.subTest(apply=apply):
                operation = MagicMock(preview=preview)
                with patch.object(
                    core,
                    "prepare_loader_migration",
                    return_value=operation,
                ):
                    output = StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(
                            packctl.cmd_loader_version(
                                type(
                                    "Args",
                                    (),
                                    {"pack": "demo", "version": "latest", "apply": apply},
                                )()
                            ),
                            0,
                        )
                self.assertIn("Loader version: 21.1.1 -> 21.1.2", output.getvalue())
                self.assertIn("pack.toml", output.getvalue())
                if apply:
                    operation.apply.assert_called_once_with()
                else:
                    operation.apply.assert_not_called()
                    self.assertIn("Dry run only", output.getvalue())

    def test_parser_routes_loader_version(self) -> None:
        args = packctl.parser().parse_args(
            ["loader-version", "demo", "recommended", "--apply"]
        )
        self.assertIs(args.func, packctl.cmd_loader_version)
        self.assertTrue(args.apply)


if __name__ == "__main__":
    unittest.main()
