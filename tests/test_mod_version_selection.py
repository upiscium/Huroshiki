from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from dependency_equivalence import MaterializedArtifact


class ExactModVersionSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.source = self.packs / "demo" / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.source / "pack.toml").write_text(
            '[versions]\nminecraft = "1.21.1"\nfabric = "0.16.0"\n',
            encoding="utf-8",
        )
        (self.source / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.root / "templates"),
            patch.object(core, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                core,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(core, "LOG_ROOT", self.root / ".huroshiki" / "logs"),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.root / "templates"),
            patch.object(packctl, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                packctl,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(packctl, "LOG_ROOT", self.root / ".huroshiki" / "logs"),
            patch.object(
                packctl,
                "TRASH_ROOT",
                self.root / ".huroshiki" / "trash",
            ),
            patch.object(
                core,
                "run_resolver_process",
                side_effect=self.run_fake_resolver,
            ),
        ]
        for item in self.patches:
            item.start()
        self.commands: list[tuple[str, ...]] = []
        self.key = core.project_key("pack", "demo")

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def metadata(
        provider: str,
        project_id: str,
        artifact_id: str,
        *,
        side: str = "both",
        filename: str | None = None,
    ) -> str:
        filename = filename or f"{project_id}.jar"
        digest = "a" * 64
        if provider == "modrinth":
            update = (
                "[update.modrinth]\n"
                f'mod-id = "{project_id}"\n'
                f'version = "{artifact_id}"\n'
            )
        else:
            update = (
                "[update.curseforge]\n"
                f"project-id = {project_id}\n"
                f"file-id = {artifact_id}\n"
            )
        return (
            f'name = "{project_id}"\n'
            f'filename = "{filename}"\n'
            f'side = "{side}"\n'
            '[download]\n'
            'hash-format = "sha256"\n'
            f'hash = "{digest}"\n'
            f'url = "https://example.invalid/{project_id}.jar"\n'
            f"{update}"
        )

    def write_installed_mods(self, provider: str, project_id: str) -> None:
        dependency_provider = (
            "modrinth" if provider == "curseforge" else "curseforge"
        )
        dependency_project = "987654" if dependency_provider == "curseforge" else "dependency"
        (self.source / "mods" / "root.pw.toml").write_text(
            self.metadata(
                provider,
                project_id,
                "1" if provider == "curseforge" else "old-artifact",
                side="server",
            ),
            encoding="utf-8",
        )
        (self.source / "mods" / "dependency.pw.toml").write_text(
            self.metadata(
                dependency_provider,
                dependency_project,
                "dependency-artifact" if dependency_provider == "modrinth" else "987655",
                side="client",
                filename="dependency.jar",
            ),
            encoding="utf-8",
        )

    def run_fake_resolver(
        self,
        command: list[str],
        *,
        cwd: Path,
        cancel_event: threading.Event,
        deadline: float,
        result_callback=None,
    ) -> core.ResolverProcessResult:
        del cancel_event, deadline
        self.commands.append(tuple(command))
        if command == ["packwiz", "refresh"]:
            (cwd / "index.toml").write_text(
                'hash-format = "sha256"\nrefreshed = true\n',
                encoding="utf-8",
            )
        elif "--version-id" in command:
            project_id = command[command.index("--project-id") + 1]
            artifact_id = command[command.index("--version-id") + 1]
            (cwd / "mods" / "root.pw.toml").write_text(
                self.metadata("modrinth", project_id, artifact_id, side="both"),
                encoding="utf-8",
            )
            (cwd / "mods" / "dependency.pw.toml").write_text(
                self.metadata(
                    "curseforge",
                    "987654",
                    "987656",
                    side="client",
                    filename="dependency.jar",
                ),
                encoding="utf-8",
            )
        elif "--file-id" in command:
            project_id = command[command.index("--addon-id") + 1]
            artifact_id = command[command.index("--file-id") + 1]
            (cwd / "mods" / "root.pw.toml").write_text(
                self.metadata("curseforge", project_id, artifact_id, side="both"),
                encoding="utf-8",
            )
            (cwd / "mods" / "dependency.pw.toml").write_text(
                self.metadata(
                    "modrinth",
                    "dependency",
                    "dependency-new",
                    side="client",
                    filename="dependency.jar",
                ),
                encoding="utf-8",
            )
        else:
            raise AssertionError(f"Unexpected Packwiz command: {command}")
        result = core.ResolverProcessResult(0, "", "", False, False)
        if result_callback is not None:
            result_callback(result)
        return result

    def materialize(self, candidate, *_args, **_kwargs):
        del candidate
        return MaterializedArtifact("b" * 64)

    def make_transaction(self, provider: str, project_id: str) -> core.PackTransaction:
        self.write_installed_mods(provider, project_id)
        core.write_pack_root_manifest(
            self.source,
            (
                core.PackRootRecord(
                    provider,
                    project_id,
                    "server",
                ),
            ),
        )
        return core.PackTransaction.create(self.key)

    def test_selection_validation_and_commands(self) -> None:
        self.assertEqual(
            core.build_exact_artifact_command(
                core.ExactModArtifactSelection("modrinth", "sodium", "v1")
            ),
            [
                "packwiz",
                "--yes",
                "modrinth",
                "add",
                "--project-id",
                "sodium",
                "--version-id",
                "v1",
            ],
        )
        self.assertEqual(
            core.build_exact_artifact_command(
                core.ExactModArtifactSelection("curseforge", "123", "456")
            ),
            [
                "packwiz",
                "--yes",
                "curseforge",
                "add",
                "--addon-id",
                "123",
                "--file-id",
                "456",
            ],
        )
        for provider, project_id, artifact_id in (
            ("curseforge", "0", "1"),
            ("curseforge", "01", "1"),
            ("curseforge", "1", "01"),
            ("modrinth", " sodium", "v1"),
            ("modrinth", "sodium", "v 1"),
        ):
            with self.subTest(provider=provider, project_id=project_id):
                with self.assertRaises(core.HuroshikiError):
                    core.ExactModArtifactSelection(provider, project_id, artifact_id)

    def test_exact_modrinth_selection_previews_applies_and_unions_dependency_side(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        try:
            with patch.object(
                core, "materialize_provider_artifact", side_effect=self.materialize
            ) as materialize:
                preview = transaction.prepare_exact_mod_version(
                    core.ExactModArtifactSelection("modrinth", "root", "v2")
                )
            self.assertEqual(preview.identity, "modrinth:root")
            self.assertEqual(preview.old_artifact_id, "old-artifact")
            self.assertEqual(preview.new_artifact_id, "v2")
            self.assertEqual(preview.added_dependencies, 0)
            self.assertEqual(preview.removed_dependencies, 0)
            self.assertEqual(materialize.call_count, 2)
            self.assertEqual(
                self.commands,
                [
                    (
                        "packwiz",
                        "--yes",
                        "modrinth",
                        "add",
                        "--project-id",
                        "root",
                        "--version-id",
                        "v2",
                    ),
                    ("packwiz", "refresh"),
                    ("packwiz", "refresh"),
                ],
            )
            self.assertIn(
                'side = "both"',
                transaction.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
            self.assertIn(
                'version = "old-artifact"',
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
            transaction.apply()
            self.assertIn(
                'version = "v2"',
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
            self.assertIn(
                'side = "both"',
                self.source.joinpath("mods/dependency.pw.toml").read_text(),
            )
            self.assertEqual(
                core.read_pack_root_manifest(self.source),
                (core.PackRootRecord("modrinth", "root", "server"),),
            )
        finally:
            transaction.discard()

    def test_exact_curseforge_artifact_mismatch_restores_transaction_source(self) -> None:
        transaction = self.make_transaction("curseforge", "123")
        original = core.tree_digest_snapshot(transaction.source)
        self.commands.clear()

        def wrong_resolver(*args, **kwargs):
            result = self.run_fake_resolver(*args, **kwargs)
            if "--file-id" in args[0]:
                path = kwargs["cwd"] / "mods/root.pw.toml"
                path.write_text(
                    self.metadata("curseforge", "123", "999"),
                    encoding="utf-8",
                )
            return result

        try:
            with patch.object(core, "run_resolver_process", side_effect=wrong_resolver):
                with self.assertRaisesRegex(core.HuroshikiError, "expected"):
                    transaction.prepare_exact_mod_version(
                        core.ExactModArtifactSelection("curseforge", "123", "456")
                    )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), original)
            self.assertIn(
                'file-id = 1',
                transaction.source.joinpath("mods/root.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_exact_curseforge_selection_previews_and_applies(self) -> None:
        transaction = self.make_transaction("curseforge", "123")
        try:
            with patch.object(
                core, "materialize_provider_artifact", side_effect=self.materialize
            ):
                preview = transaction.prepare_exact_mod_version(
                    core.ExactModArtifactSelection("curseforge", "123", "456")
                )
            self.assertEqual(preview.identity, "curseforge:123")
            self.assertEqual(preview.old_artifact_id, "1")
            self.assertEqual(preview.new_artifact_id, "456")
            transaction.apply()
            self.assertIn(
                "file-id = 456",
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
            self.assertEqual(
                self.commands[0],
                (
                    "packwiz",
                    "--yes",
                    "curseforge",
                    "add",
                    "--addon-id",
                    "123",
                    "--file-id",
                    "456",
                ),
            )
        finally:
            transaction.discard()

    def test_staged_exact_selection_cannot_be_changed_and_stale_apply_rolls_back(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        try:
            with patch.object(
                core, "materialize_provider_artifact", side_effect=self.materialize
            ):
                transaction.prepare_exact_mod_version(
                    core.ExactModArtifactSelection("modrinth", "root", "v2")
                )
            with self.assertRaisesRegex(core.HuroshikiError, "apply or discard"):
                transaction.set_side(Path("mods/root.pw.toml"), True, True)
            transaction.source.joinpath("mods/root.pw.toml").write_text(
                self.metadata("modrinth", "root", "tampered"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(core.HuroshikiError, "changed after preview"):
                transaction.apply()
            self.assertIn(
                'version = "old-artifact"',
                transaction.source.joinpath("mods/root.pw.toml").read_text(),
            )
            self.assertIn(
                'version = "old-artifact"',
                self.source.joinpath("mods/root.pw.toml").read_text(),
            )
        finally:
            transaction.discard()

    def test_cancellation_before_checkpoint_does_not_modify_transaction(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        original = core.tree_digest_snapshot(transaction.source)
        cancel_event = threading.Event()
        cancel_event.set()
        try:
            with self.assertRaises(core.ExactModVersionCancelled):
                transaction.prepare_exact_mod_version(
                    core.ExactModArtifactSelection("modrinth", "root", "v2"),
                    cancel_event=cancel_event,
                )
            self.assertEqual(core.tree_digest_snapshot(transaction.source), original)
        finally:
            transaction.discard()

    def test_discard_owns_and_cancels_running_exact_selection(self) -> None:
        transaction = self.make_transaction("modrinth", "root")
        started = threading.Event()
        worker_error: list[BaseException] = []

        def blocking_resolver(
            command: list[str],
            *,
            cwd: Path,
            cancel_event: threading.Event,
            deadline: float,
            result_callback=None,
        ) -> core.ResolverProcessResult:
            del cwd, deadline
            self.commands.append(tuple(command))
            if "--version-id" in command:
                started.set()
                while not cancel_event.is_set():
                    time.sleep(0.005)
                return core.ResolverProcessResult(-15, "", "", True, False)
            raise AssertionError(command)

        def prepare() -> None:
            try:
                with patch.object(
                    core,
                    "run_resolver_process",
                    side_effect=blocking_resolver,
                ):
                    transaction.prepare_exact_mod_version(
                        core.ExactModArtifactSelection("modrinth", "root", "v2")
                    )
            except BaseException as error:
                worker_error.append(error)

        worker = threading.Thread(target=prepare, name="exact-selection-test-worker")
        worker.start()
        self.assertTrue(started.wait(1))
        transaction.discard()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(worker_error), 1)
        self.assertIsInstance(worker_error[0], core.HuroshikiError)
        self.assertIn(
            'version = "old-artifact"',
            transaction.source.joinpath("mods/root.pw.toml").read_text(),
        )
        self.assertFalse(packctl.project_lock_is_active(self.key))


if __name__ == "__main__":
    unittest.main()
