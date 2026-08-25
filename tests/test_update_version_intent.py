from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import huroshiki_core as core
from mod_version_overrides import ModVersionOverride, read_mod_version_overrides, write_mod_version_overrides


class UpdateVersionIntentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.source / ".packwizignore").write_text(
            "/.huroshiki-version-overrides.json\n", encoding="utf-8"
        )
        self.mod = self.source / "mods" / "demo.pw.toml"
        self.mod.write_text(
            'name = "Demo"\nfilename = "demo.jar"\nside = "both"\n'
            '[update.curseforge]\nproject-id = 1\nfile-id = 2\n',
            encoding="utf-8",
        )
        self.tx = core.PackTransaction(
            "pack:demo", self.source.parent, self.source,
            core.metadata_digest_snapshot(self.source),
            core.metadata_content_snapshot(self.source),
            real_source_baseline=core.tree_digest_snapshot(self.source),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def refresh_transaction(self) -> None:
        self.tx = core.PackTransaction(
            "pack:demo", self.source.parent, self.source,
            core.metadata_digest_snapshot(self.source),
            core.metadata_content_snapshot(self.source),
            real_source_baseline=core.tree_digest_snapshot(self.source),
        )

    def test_locked_root_is_structured_and_does_not_start_resolver(self) -> None:
        write_mod_version_overrides(self.source, (ModVersionOverride("curseforge", "1", "2", True, "compat"),))
        self.refresh_transaction()
        with patch.object(core, "run_resolver_process") as resolver:
            candidates = self.tx.prepare_updates()
        resolver.assert_not_called()
        self.assertEqual(candidates[0].status, "version-locked")
        self.assertIsNone(candidates[0].error)

    def test_malformed_or_drifted_intent_blocks_before_normalization(self) -> None:
        write_mod_version_overrides(self.source, (ModVersionOverride("curseforge", "1", "3"),))
        self.refresh_transaction()
        with patch.object(core, "run_resolver_process") as resolver:
            with self.assertRaisesRegex(core.HuroshikiError, "drifted"):
                self.tx.prepare_updates()
        resolver.assert_not_called()

    def test_packwiz_pin_remains_independent(self) -> None:
        self.mod.write_text(self.mod.read_text().replace('[update.curseforge]', 'pin = true\n[update.curseforge]'), encoding="utf-8")
        self.refresh_transaction()
        candidate = self.tx.prepare_updates()[0]
        self.assertEqual(candidate.status, "pinned")

    def test_unlocked_selected_update_follows_artifact_and_reselection_restores(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2", False, "user"),)
        )
        self.refresh_transaction()
        before = self.mod.read_bytes()
        after = before.replace(b"file-id = 2", b"file-id = 3")
        candidate = core.UpdateCandidate(
            key="curseforge:1", root=Path("mods/demo.pw.toml"), slug="demo",
            name="Demo", provider="curseforge", current_version="2",
            new_version="3", status="update",
            changes=(core.UpdateChange(Path("mods/demo.pw.toml"), before, after),),
        )
        self.tx.update_candidates = (candidate,)
        self.tx.select_updates([candidate.root])
        self.assertEqual(read_mod_version_overrides(self.source)[0].artifact_id, "3")
        self.tx.select_updates([])
        self.assertEqual(self.mod.read_bytes(), before)
        self.assertEqual(read_mod_version_overrides(self.source)[0].artifact_id, "2")

    def test_update_selection_failure_restores_source_and_intent_bytes(self) -> None:
        write_mod_version_overrides(
            self.source, (ModVersionOverride("curseforge", "1", "2", False, "user"),)
        )
        self.refresh_transaction()
        source_before = core._file_content_snapshot(self.source)
        candidate = core.UpdateCandidate(
            key="curseforge:1", root=Path("mods/demo.pw.toml"), slug="demo",
            name="Demo", provider="curseforge", current_version="2",
            new_version="3", status="update",
            changes=(core.UpdateChange(
                Path("mods/demo.pw.toml"),
                self.mod.read_bytes(),
                self.mod.read_bytes().replace(b"file-id = 2", b"file-id = 3"),
            ),),
        )
        self.tx.update_candidates = (candidate,)
        with patch.object(
            core,
            "set_mod_version_override",
            side_effect=core.HuroshikiError("manifest write failed"),
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "manifest write failed"):
                self.tx.select_updates([candidate.root])
        self.assertEqual(core._file_content_snapshot(self.source), source_before)


class LockedDependencyUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.transaction_root = self.root / "transaction"
        (self.source / "mods").mkdir(parents=True)
        self.transaction_root.mkdir()
        (self.source / "pack.toml").write_text(
            '[versions]\nminecraft = "1.21.1"\nfabric = "0.16.0"\n',
            encoding="utf-8",
        )
        (self.source / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        (self.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n/.huroshiki-version-overrides.json\n",
            encoding="utf-8",
        )
        self.write_mod("root", "1", "10", side="server")
        self.write_mod("other", "3", "30")
        self.write_mod("dependency", "2", "20")
        core.write_pack_root_manifest(
            self.source,
            (
                core.PackRootRecord("curseforge", "1", "server"),
                core.PackRootRecord("curseforge", "3", "both"),
            ),
        )
        write_mod_version_overrides(
            self.source,
            (ModVersionOverride("curseforge", "2", "20", True, "compat"),),
        )
        self.calls: list[tuple[tuple[str, ...], str]] = []
        self.other_depends_on_root = False

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def metadata(name: str, project_id: str, file_id: str, *, side: str = "both") -> str:
        return (
            f'name = "{name}"\n'
            f'filename = "{name}.jar"\n'
            f'side = "{side}"\n'
            '[download]\n'
            'hash-format = "sha256"\n'
            f'hash = "{"a" * 64}"\n'
            f'url = "https://example.invalid/{name}.jar"\n'
            '[update.curseforge]\n'
            f'project-id = {project_id}\n'
            f'file-id = {file_id}\n'
        )

    def write_mod(
        self,
        name: str,
        project_id: str,
        file_id: str,
        *,
        side: str = "both",
        source: Path | None = None,
    ) -> None:
        target = source or self.source
        (target / "mods").mkdir(parents=True, exist_ok=True)
        (target / "mods" / f"{name}.pw.toml").write_text(
            self.metadata(name, project_id, file_id, side=side), encoding="utf-8"
        )

    def fake_resolver(self, command, *, cwd, result_callback=None, **_kwargs):
        command_tuple = tuple(command)
        self.calls.append((command_tuple, cwd.name))
        if command_tuple[:3] == ("packwiz", "--yes", "update"):
            slug = command_tuple[3]
            if slug == "root":
                self.write_mod("root", "1", "11", side="server", source=cwd)
                self.write_mod("dependency", "2", "21", source=cwd)
            elif slug == "other":
                self.write_mod("other", "3", "31", source=cwd)
                if self.other_depends_on_root:
                    self.write_mod("root", "1", "12", side="server", source=cwd)
            else:
                raise AssertionError(command)
        elif "--file-id" in command_tuple:
            project_id = command_tuple[command_tuple.index("--addon-id") + 1]
            file_id = command_tuple[command_tuple.index("--file-id") + 1]
            if project_id == "1":
                self.write_mod(
                    "root",
                    "1",
                    file_id,
                    side="server",
                    source=cwd,
                )
                if cwd.name.startswith("update-owner-root"):
                    self.write_mod("dependency", "2", "20", source=cwd)
            elif project_id == "2":
                self.write_mod("dependency", "2", file_id, source=cwd)
            elif project_id == "3":
                self.write_mod("other", "3", file_id, source=cwd)
                if (
                    self.other_depends_on_root
                    and cwd.name.startswith("update-owner-root")
                ):
                    self.write_mod("root", "1", "10", side="server", source=cwd)
            else:
                raise AssertionError(command)
        elif command_tuple != ("packwiz", "refresh"):
            raise AssertionError(command)
        result = core.ResolverProcessResult(0, "", "", False, False)
        if result_callback is not None:
            result_callback(result)
        return result

    def prepare(self, *, verification_error: str | None = None):
        verification = (
            patch.object(
                core,
                "_verify_exact_closure_artifacts",
                side_effect=core.HuroshikiError(verification_error),
            )
            if verification_error is not None
            else patch.object(core, "_verify_exact_closure_artifacts", return_value=())
        )
        with patch.object(core, "run_resolver_process", side_effect=self.fake_resolver), verification:
            return core._prepare_update_candidates(
                self.source,
                self.transaction_root,
                core.metadata_content_snapshot(self.source),
                cancel_event=threading.Event(),
                version_overrides=read_mod_version_overrides(self.source),
            )

    def test_locked_dependency_is_preseeded_only_into_its_owner(self) -> None:
        candidates = self.prepare()
        by_key = {candidate.key: candidate for candidate in candidates}
        self.assertEqual(by_key["curseforge:2"].status, "version-locked")
        self.assertTrue(by_key["curseforge:1"].available)
        self.assertTrue(by_key["curseforge:3"].available)
        constrained_calls = [
            command
            for command, directory in self.calls
            if directory == "exact-source" and "--file-id" in command
        ]
        self.assertEqual(
            [command[command.index("--addon-id") + 1] for command in constrained_calls],
            ["2", "1"],
        )
        self.assertFalse(
            any(
                change.relative_path == Path("mods/dependency.pw.toml")
                for change in by_key["curseforge:1"].changes
            )
        )
        self.assertFalse(
            any(b"file-id = 21" in (change.after or b"") for change in by_key["curseforge:1"].changes)
        )

    def test_incompatible_locked_dependency_blocks_owner_candidate(self) -> None:
        with self.assertRaisesRegex(
            core.UpdateVersionIntentError, "dependency range conflict"
        ):
            self.prepare(verification_error="dependency range conflict")

    def test_locked_root_is_constrained_when_another_root_requires_it(self) -> None:
        self.other_depends_on_root = True
        write_mod_version_overrides(
            self.source,
            (
                ModVersionOverride("curseforge", "1", "10", True, "root pin"),
                ModVersionOverride("curseforge", "2", "20", True, "dependency pin"),
            ),
        )
        candidates = self.prepare()
        by_key = {candidate.key: candidate for candidate in candidates}
        self.assertEqual(by_key["curseforge:1"].status, "version-locked")
        self.assertTrue(by_key["curseforge:3"].available)
        constrained_calls = [
            command
            for command, directory in self.calls
            if directory == "exact-source" and "--file-id" in command
        ]
        self.assertEqual(
            [command[command.index("--addon-id") + 1] for command in constrained_calls],
            ["1", "3"],
        )
        self.assertFalse(
            any(b"file-id = 12" in (change.after or b"") for change in by_key["curseforge:3"].changes)
        )


class UpdateVersionIntentCliTest(unittest.TestCase):
    def test_update_all_reports_version_locked_candidate(self) -> None:
        candidate = core.UpdateCandidate(
            key="curseforge:1",
            root=Path("mods/demo.pw.toml"),
            slug="demo",
            name="Demo",
            provider="curseforge",
            current_version="1.2.3",
            current_file_id="456",
            new_version="-",
            status="version-locked",
        )

        class FakeTransaction:
            def prepare_updates(self, **_kwargs):
                return [candidate]

            def discard(self):
                return None

        output = io.StringIO()
        with (
            patch.object(core.PackTransaction, "create", return_value=FakeTransaction()),
            redirect_stdout(output),
        ):
            report = core.update_all("pack:demo", allow_partial=True)
        self.assertFalse(report.applied)
        self.assertIn(
            "Version locked: Demo [curseforge] at 1.2.3 (456); skipped",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
