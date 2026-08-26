from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import huroshiki_core as core
from dependency_equivalence import LoaderDependencyRequirement, SemanticJarIdentity
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

    def test_second_selection_restores_and_recomputes_locked_graph(self) -> None:
        dependency = self.source / "mods" / "dependency.pw.toml"
        dependency.write_text(
            'name = "Dependency"\nfilename = "dependency.jar"\nside = "both"\n'
            '[update.curseforge]\nproject-id = 2\nfile-id = 20\n',
            encoding="utf-8",
        )
        write_mod_version_overrides(
            self.source,
            (ModVersionOverride("curseforge", "2", "20", True, "compat"),),
        )
        self.refresh_transaction()
        before = self.mod.read_bytes()
        after = before.replace(b"file-id = 2", b"file-id = 3")
        candidate = core.UpdateCandidate(
            key="curseforge:1",
            root=Path("mods/demo.pw.toml"),
            slug="demo",
            name="Demo",
            provider="curseforge",
            current_version="2",
            new_version="3",
            status="update",
            target_identity=("curseforge", "1"),
            target_artifact_id="3",
        )
        self.tx.update_candidates = (candidate,)
        manifest_before = (
            self.source / ".huroshiki-version-overrides.json"
        ).read_bytes()

        def reconstruct(_source, _roots, selected, *_args, **_kwargs):
            if selected:
                return (
                    core.UpdateChange(Path("mods/demo.pw.toml"), before, after),
                )
            return ()

        with (
            patch.object(
                core,
                "scan_pack_migration_source",
                return_value=SimpleNamespace(root_identity="root", snapshot_digest="digest"),
            ),
            patch.object(core, "extract_pack_migration_roots", return_value=()),
            patch.object(
                core,
                "_reconstruct_locked_update_selection",
                side_effect=reconstruct,
            ) as reconstruction,
        ):
            self.tx.select_updates([candidate.root])
            self.assertEqual(self.mod.read_bytes(), after)
            self.tx.select_updates([])
        self.assertEqual(self.mod.read_bytes(), before)
        self.assertEqual(
            (self.source / ".huroshiki-version-overrides.json").read_bytes(),
            manifest_before,
        )
        self.assertEqual(reconstruction.call_count, 2)
        self.assertEqual(reconstruction.call_args_list[0].args[2], {("curseforge", "1"): "3"})
        self.assertEqual(reconstruction.call_args_list[1].args[2], {})


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
        self.root_update_drops_dependency = False
        self.other_baseline_requires_dependency = False
        self.other_update_requires_dependency = False
        self.last_graph_owners = {("curseforge", "1")}

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
                if self.root_update_drops_dependency:
                    (cwd / "mods" / "dependency.pw.toml").unlink(missing_ok=True)
                else:
                    self.write_mod("dependency", "2", "21", source=cwd)
            elif slug == "other":
                self.write_mod("other", "3", "31", source=cwd)
                if self.other_update_requires_dependency:
                    self.write_mod("dependency", "2", "21", source=cwd)
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
                requires_dependency = file_id == "10" or (
                    file_id == "11" and not self.root_update_drops_dependency
                )
                if requires_dependency and not (
                    cwd / "mods" / "dependency.pw.toml"
                ).exists():
                    self.write_mod(
                        "dependency",
                        "2",
                        "21" if file_id == "11" else "20",
                        source=cwd,
                    )
            elif project_id == "2":
                self.write_mod("dependency", "2", file_id, source=cwd)
            elif project_id == "3":
                self.write_mod("other", "3", file_id, source=cwd)
                requires_dependency = (
                    self.other_baseline_requires_dependency
                    if file_id == "30"
                    else self.other_update_requires_dependency
                )
                if requires_dependency and not (
                    cwd / "mods" / "dependency.pw.toml"
                ).exists():
                    self.write_mod(
                        "dependency",
                        "2",
                        "21" if file_id == "31" else "20",
                        source=cwd,
                    )
                if self.other_depends_on_root:
                    self.write_mod("root", "1", "10", side="server", source=cwd)
            else:
                raise AssertionError(command)
        elif command_tuple != ("packwiz", "refresh"):
            raise AssertionError(command)
        result = core.ResolverProcessResult(0, "", "", False, False)
        if result_callback is not None:
            result_callback(result)
        return result

    def prepare(
        self,
        *,
        verification_error: str | None = None,
        incompatible_project_id: str = "1",
    ):
        def verify(_baseline, desired, *_args, **_kwargs):
            if verification_error is not None:
                root = desired.get(("curseforge", incompatible_project_id), ())
                if root and b"file-id = 11" in root[0][1]:
                    raise core.HuroshikiError(verification_error)
                if root and b"file-id = 31" in root[0][1]:
                    raise core.HuroshikiError(verification_error)
            owners = set()
            root = desired.get(("curseforge", "1"), ())
            if root:
                root_contents = root[0][1]
                if b"file-id = 10" in root_contents or (
                    b"file-id = 11" in root_contents
                    and not self.root_update_drops_dependency
                ):
                    owners.add(("curseforge", "1"))
            other = desired.get(("curseforge", "3"), ())
            if other:
                other_contents = other[0][1]
                if (
                    b"file-id = 30" in other_contents
                    and self.other_baseline_requires_dependency
                ) or (
                    b"file-id = 31" in other_contents
                    and self.other_update_requires_dependency
                ):
                    owners.add(("curseforge", "3"))
            self.last_graph_owners = owners
            return ()

        class Graph:
            @staticmethod
            def reachable_roots(identity):
                if identity == ("curseforge", "2"):
                    return set(self.last_graph_owners)
                return set()

        with (
            patch.object(core, "run_resolver_process", side_effect=self.fake_resolver),
            patch.object(core, "_verify_exact_closure_artifacts", side_effect=verify),
            patch.object(core, "_build_exact_dependency_graph", return_value=Graph()),
            patch.object(core, "_assert_exact_selected_dependency_reachability"),
        ):
            return core._prepare_update_candidates(
                self.source,
                self.transaction_root,
                core.metadata_content_snapshot(self.source),
                cancel_event=threading.Event(),
                version_overrides=read_mod_version_overrides(self.source),
            )

    def reconstruct(self, selected_artifacts, *, real_graph: bool = False, omit_edges: bool = False):
        def verify(_baseline, desired, *_args, **_kwargs):
            owners = set()
            root = desired.get(("curseforge", "1"), ())
            if root:
                root_contents = root[0][1]
                if b"file-id = 10" in root_contents or (
                    b"file-id = 11" in root_contents
                    and not self.root_update_drops_dependency
                ):
                    owners.add(("curseforge", "1"))
            other = desired.get(("curseforge", "3"), ())
            if other:
                other_contents = other[0][1]
                if (
                    b"file-id = 30" in other_contents
                    and self.other_baseline_requires_dependency
                ) or (
                    b"file-id = 31" in other_contents
                    and self.other_update_requires_dependency
                ):
                    owners.add(("curseforge", "3"))
            self.last_graph_owners = owners
            if real_graph:
                verifications = []
                for identity, entries in sorted(desired.items()):
                    if identity[0] not in {"curseforge", "modrinth"}:
                        continue
                    artifact_id = core.parse_provider_metadata(
                        entries[0][0], entries[0][1]
                    ).file_id
                    mod_id = {
                        ("curseforge", "1"): "root",
                        ("curseforge", "2"): "dependency",
                        ("curseforge", "3"): "other",
                    }[identity]
                    requirements = ()
                    if not omit_edges and identity in owners:
                        requirements = (
                            LoaderDependencyRequirement("dependency", ">=1"),
                        )
                    verifications.append(
                        core.ExactArtifactVerification(
                            identity,
                            artifact_id or "",
                            "a" * 64,
                            SemanticJarIdentity(((mod_id, "1"),), "fabric"),
                            requirements,
                        )
                    )
                return tuple(verifications)
            return ()

        class Graph:
            def reachable_roots(_graph, identity):
                if identity == ("curseforge", "2"):
                    return set(self.last_graph_owners)
                return set()

        scan = core.scan_pack_migration_source(self.source, checkpoint=lambda: None)
        roots = core.extract_pack_migration_roots(
            self.source,
            expected_identity=scan.root_identity,
            expected_snapshot_digest=scan.snapshot_digest,
            checkpoint=lambda: None,
        )
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    core, "run_resolver_process", side_effect=self.fake_resolver
                )
            )
            stack.enter_context(
                patch.object(
                    core, "_verify_exact_closure_artifacts", side_effect=verify
                )
            )
            if not real_graph:
                stack.enter_context(
                    patch.object(
                        core, "_build_exact_dependency_graph", return_value=Graph()
                    )
                )
                stack.enter_context(
                    patch.object(
                        core, "_assert_exact_selected_dependency_reachability"
                    )
                )
            return core._reconstruct_locked_update_selection(
                self.source,
                roots,
                selected_artifacts,
                read_mod_version_overrides(self.source),
                workspace=self.transaction_root / f"reconstruct-{len(self.calls)}",
                cancel_event=threading.Event(),
                deadline=core.time.monotonic() + 60,
                process_result_callback=None,
                diagnostic_project_id="demo",
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
            if "-constrained-" in directory and "--file-id" in command
        ]
        constrained_projects = [
            command[command.index("--addon-id") + 1]
            for command in constrained_calls
        ]
        self.assertIn(["2", "1"], [
            constrained_projects[index : index + 2]
            for index in range(len(constrained_projects) - 1)
        ])
        self.assertNotIn(["2", "3"], [
            constrained_projects[index : index + 2]
            for index in range(len(constrained_projects) - 1)
        ])
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
        candidates = self.prepare(verification_error="dependency range conflict")
        by_key = {candidate.key: candidate for candidate in candidates}
        self.assertEqual(by_key["curseforge:1"].status, "version-blocked")
        self.assertEqual(by_key["curseforge:1"].error_kind, "version-intent")
        self.assertIn(
            "dependency range conflict", by_key["curseforge:1"].error or ""
        )
        self.assertTrue(by_key["curseforge:3"].available)

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
            if "-constrained-" in directory and "--file-id" in command
        ]
        constrained_projects = [
            command[command.index("--addon-id") + 1]
            for command in constrained_calls
        ]
        self.assertIn(["1", "3"], [
            constrained_projects[index : index + 2]
            for index in range(len(constrained_projects) - 1)
        ])
        self.assertFalse(
            any(b"file-id = 12" in (change.after or b"") for change in by_key["curseforge:3"].changes)
        )

    def test_new_prospective_owner_uses_compatible_locked_artifact(self) -> None:
        self.other_update_requires_dependency = True
        manifest_before = (
            self.source / ".huroshiki-version-overrides.json"
        ).read_bytes()
        candidates = self.prepare()
        candidate = next(item for item in candidates if item.key == "curseforge:3")
        self.assertTrue(candidate.available)
        self.assertFalse(
            any(b"file-id = 21" in (change.after or b"") for change in candidate.changes)
        )
        constrained_projects = [
            command[command.index("--addon-id") + 1]
            for command, directory in self.calls
            if "-constrained-" in directory and "--file-id" in command
        ]
        self.assertIn(["2", "3"], [
            constrained_projects[index : index + 2]
            for index in range(len(constrained_projects) - 1)
        ])
        self.assertEqual(
            (self.source / ".huroshiki-version-overrides.json").read_bytes(),
            manifest_before,
        )

    def test_new_prospective_owner_incompatible_lock_is_candidate_local(self) -> None:
        self.other_update_requires_dependency = True
        candidates = self.prepare(
            verification_error="new owner dependency conflict",
            incompatible_project_id="3",
        )
        by_key = {candidate.key: candidate for candidate in candidates}
        self.assertEqual(by_key["curseforge:3"].status, "version-blocked")
        self.assertEqual(by_key["curseforge:3"].blocked_identity, "curseforge:2")
        self.assertEqual(by_key["curseforge:3"].blocked_artifact_id, "20")
        self.assertTrue(by_key["curseforge:1"].available)

    def test_one_of_two_owners_can_drop_locked_dependency(self) -> None:
        self.root_update_drops_dependency = True
        self.other_baseline_requires_dependency = True
        candidates = self.prepare()
        candidate = next(item for item in candidates if item.key == "curseforge:1")
        self.assertTrue(candidate.available)
        self.assertFalse(
            any(
                change.relative_path == Path("mods/dependency.pw.toml")
                and change.after is None
                for change in candidate.changes
            )
        )

    def test_final_owner_drop_is_version_blocked(self) -> None:
        self.root_update_drops_dependency = True
        candidates = self.prepare()
        candidate = next(item for item in candidates if item.key == "curseforge:1")
        self.assertEqual(candidate.status, "version-blocked")
        self.assertEqual(candidate.error_kind, "version-intent")
        self.assertIn("no longer required", candidate.error or "")

    def test_selected_owner_transfer_reconstructs_final_graph(self) -> None:
        self.root_update_drops_dependency = True
        self.other_update_requires_dependency = True
        changes = self.reconstruct(
            {
                ("curseforge", "1"): "11",
                ("curseforge", "3"): "31",
            },
            real_graph=True,
        )
        self.assertTrue(
            any(
                change.relative_path == Path("mods/root.pw.toml")
                and b"file-id = 11" in (change.after or b"")
                for change in changes
            )
        )
        self.assertTrue(
            any(
                change.relative_path == Path("mods/other.pw.toml")
                and b"file-id = 31" in (change.after or b"")
                for change in changes
            )
        )
        self.assertFalse(
            any(b"file-id = 21" in (change.after or b"") for change in changes)
        )

    def test_selected_set_cannot_remove_final_locked_owner(self) -> None:
        self.root_update_drops_dependency = True
        with self.assertRaisesRegex(
            core.UpdateVersionIntentError, "no longer required"
        ):
            self.reconstruct({("curseforge", "1"): "11"})

    def test_preseed_presence_without_required_edge_is_rejected(self) -> None:
        self.other_update_requires_dependency = True
        with self.assertRaisesRegex(
            core.UpdateVersionIntentError, "required-edge owner"
        ):
            self.reconstruct(
                {("curseforge", "3"): "31"},
                real_graph=True,
                omit_edges=True,
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

    def test_partial_update_applies_only_independent_safe_candidate(self) -> None:
        blocked = core.UpdateCandidate(
            key="curseforge:1",
            root=Path("mods/blocked.pw.toml"),
            slug="blocked",
            name="Blocked",
            provider="curseforge",
            current_version="1",
            new_version="-",
            status="version-blocked",
            error="locked dependency conflict",
            error_kind="version-intent",
            blocked_identity="curseforge:2",
            blocked_artifact_id="20",
            blocked_reason="dependency range conflict",
            version_intent_reason="compatibility",
        )
        safe = core.UpdateCandidate(
            key="curseforge:3",
            root=Path("mods/safe.pw.toml"),
            slug="safe",
            name="Safe",
            provider="curseforge",
            current_version="1",
            new_version="2",
            status="update",
        )

        class FakeTransaction:
            def __init__(self):
                self.selected = ()
                self.applied = False

            def prepare_updates(self, **_kwargs):
                return [blocked, safe]

            def select_updates(self, paths):
                self.selected = tuple(paths)

            def apply(self, **_kwargs):
                self.applied = True

            def discard(self):
                return None

        normal = FakeTransaction()
        with (
            patch.object(core.PackTransaction, "create", return_value=normal),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            normal_report = core.update_all("pack:demo")
        self.assertFalse(normal_report.applied)
        self.assertFalse(normal.applied)

        partial = FakeTransaction()
        stderr = io.StringIO()
        with (
            patch.object(core.PackTransaction, "create", return_value=partial),
            redirect_stdout(io.StringIO()),
            redirect_stderr(stderr),
        ):
            partial_report = core.update_all("pack:demo", allow_partial=True)
        self.assertTrue(partial_report.applied)
        self.assertEqual(partial.selected, (safe.root,))
        self.assertTrue(partial.applied)
        self.assertIn("pin reason: compatibility", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
