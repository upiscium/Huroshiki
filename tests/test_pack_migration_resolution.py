from __future__ import annotations

from pathlib import Path
import json
import os
import stat
import unittest
from unittest.mock import patch

import huroshiki_core as core
import pack_migration
import pack_migration_resolution as resolution
import packctl
from pack_migration_roots import PackRootRecord, write_pack_root_manifest
from pack_migration_roots import (
    PackMigrationRootSelection,
    read_pack_root_manifest,
)
from tests import test_pack_migration_core as migration_fixture


TARGET_PACK_TOML = '''name = "Next"
author = "Test"
pack-format = "packwiz:1.1.0"
[index]
file = "index.toml"
hash-format = "sha256"
hash = "target"
[versions]
minecraft = "1.21.4"
fabric = "0.16.0"
'''


def metadata(version: str, *, dependency: bool = False) -> core.ResolvedMetadata:
    project = "dependency" if dependency else "root-project"
    path = Path("mods") / f"{project}.pw.toml"
    contents = f'''name = "{project}"
filename = "{project}-{version}.jar"
side = "both"
[download]
url = "https://cdn.modrinth.com/{project}-{version}.jar"
[update.modrinth]
mod-id = "{project}"
version = "{version}"
'''.encode()
    return core.ResolvedMetadata(
        ("modrinth", project),
        path,
        f"{project}-{version}.jar",
        contents,
        "modrinth",
        project,
    )


def filesystem_snapshot(root: Path) -> tuple[tuple[str, str, int, bytes | None], ...]:
    """Capture the contract-visible tree without following filesystem links."""
    captured: list[tuple[str, str, int, bytes | None]] = []

    def visit(directory: Path, prefix: Path = Path(".")) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = directory / entry.name
                relative = (prefix / entry.name).as_posix()
                info = entry.stat(follow_symlinks=False)
                mode = stat.S_IMODE(info.st_mode)
                if stat.S_ISDIR(info.st_mode):
                    kind, contents = "directory", None
                    captured.append((relative, kind, mode, contents))
                    visit(path, prefix / entry.name)
                elif stat.S_ISREG(info.st_mode):
                    captured.append((relative, "file", mode, path.read_bytes()))
                elif stat.S_ISLNK(info.st_mode):
                    captured.append((relative, "symlink", mode, None))
                else:
                    captured.append((relative, "special", mode, None))

    visit(root)
    return tuple(captured)


class PackMigrationResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = migration_fixture.PackMigrationCoreTest(methodName="runTest")
        self.base.setUp()
        for name in ("root", "packs", "templates", "state", "pack", "source"):
            setattr(self, name, getattr(self.base, name))
        (self.source / "mods" / "example.pw.toml").write_bytes(metadata("v1").contents)
        (self.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        write_pack_root_manifest(
            self.source,
            (PackRootRecord("modrinth", "root-project", "both"),),
        )

    def tearDown(self) -> None:
        self.base.tearDown()

    def target(self) -> pack_migration.PackMigrationTarget:
        return pack_migration.PackMigrationTarget(
            "next", "Next", "1.21.4", "fabric", "0.16.0"
        )

    @staticmethod
    def provider_metadata(provider: str, project: str, *, filename: str, side: str, digest: str) -> core.ResolvedMetadata:
        update = (
            f'[update.modrinth]\nmod-id = "{project}"\nversion = "v2"\n'
            if provider == "modrinth"
            else f"[update.curseforge]\nproject-id = {project}\nfile-id = 1\n"
        )
        contents = (
            f'name = "{project}"\nfilename = "{filename}"\nside = "{side}"\n'
            f'[download]\nhash-format = "sha256"\nhash = "{digest}"\n'
            f'url = "https://example.invalid/{project}.jar"\n{update}'
        ).encode()
        return core.ResolvedMetadata(
            (provider, project), Path("mods") / f"{project}.pw.toml", filename,
            contents, provider, project,
        )

    def _two_provider_roots(self) -> None:
        (self.source / "mods/example.pw.toml").unlink(missing_ok=True)
        first = self.provider_metadata("modrinth", "root-project", filename="root.jar", side="both", digest="1" * 64)
        second = self.provider_metadata("curseforge", "123", filename="other.jar", side="both", digest="2" * 64)
        (self.source / first.relative_path).write_bytes(first.contents)
        (self.source / second.relative_path).write_bytes(second.contents)
        write_pack_root_manifest(
            self.source,
            (PackRootRecord("modrinth", "root-project", "both"), PackRootRecord("curseforge", "123", "both")),
        )

    def _two_provider_closure(self, *, equivalent: bool) -> core.ResolvedModClosure:
        provider = "modrinth" if self._current_root == "root-project" else "curseforge"
        project = self._current_root
        root = self.provider_metadata(
            provider, project, filename="root.jar" if provider == "modrinth" else "other.jar",
            side="both", digest="1" * 64 if provider == "modrinth" else "2" * 64,
        )
        shared = self.provider_metadata(
            "modrinth" if provider == "modrinth" else "curseforge",
            "shared" if provider == "modrinth" else "456",
            filename="shared.jar", side="client" if provider == "modrinth" else "server",
            digest="a" * 64 if equivalent else ("b" if provider == "modrinth" else "c") * 64,
        )
        return core.ResolvedModClosure((provider, project), (root, shared))

    def plan(self) -> pack_migration.PackMigrationPlan:
        snapshot = pack_migration.snapshot_pack_migration_source_at(
            "pack:demo", self.pack, self.root
        )
        return pack_migration.plan_pack_copy_migration_at(
            "pack:demo",
            self.pack,
            self.packs / "next",
            self.state / "transactions",
            self.target(),
            expected_snapshot=snapshot,
            repository_root=self.root,
            state_root=self.state,
        )

    @staticmethod
    def fake_init(root: Path, **_: object) -> None:
        source = root / "source"
        (source / "mods").mkdir(parents=True)
        (source / "pack.toml").write_text(TARGET_PACK_TOML, encoding="utf-8")
        (source / "index.toml").write_text('hash-format = "sha256"\n', encoding="utf-8")
        (source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        write_pack_root_manifest(source, ())

    @staticmethod
    def fake_closure(**_: object) -> core.ResolvedModClosure:
        return core.ResolvedModClosure(
            ("modrinth", "root-project"),
            (metadata("v2"), metadata("v1", dependency=True)),
        )

    @staticmethod
    def fake_selector(*_: object, **__: object) -> core.ResolvedSelector:
        return core.ResolvedSelector(
            "modrinth", "root-project", "root-project", "Root"
        )

    def test_staged_resolves_new_target_source_and_never_becomes_ready(self) -> None:
        plan = self.plan()
        progress: list[object] = []
        with patch.object(packctl, "init_packwiz_project", side_effect=self.fake_init), patch.object(
            core, "resolve_mod_closure", side_effect=self.fake_closure
        ), patch.object(core, "resolve_project_selector", side_effect=self.fake_selector), patch.object(packctl, "run_packwiz"):
            result = resolution.resolve_pack_migration_plan_at(
                plan,
                repository_root=self.root,
                state_root=self.state,
                progress=progress.append,
            )
        self.assertEqual(result.state, "resolved")
        self.assertEqual(plan.state, "resolved")
        self.assertTrue((plan.target_staging_root / "source" / "mods" / "root-project.pw.toml").is_file())
        self.assertTrue((plan._resolver_work_root / "original-source" / "pack.toml").is_file())
        self.assertEqual(result.dependency_delta.added[0].canonical_identity, "modrinth:dependency")
        diagnostic = json.loads((plan.transaction_root / "plan.json").read_text())
        self.assertEqual(diagnostic["schema"], 2)
        self.assertEqual(diagnostic["state"], "resolved")
        self.assertNotIn(str(self.root), json.dumps(diagnostic))
        with self.assertRaisesRegex(pack_migration.PackMigrationError, "publication requires"):
            pack_migration.apply_pack_copy_migration_at(plan)
        pack_migration.discard_pack_migration_plan(plan)

    def test_verified_cross_provider_dependencies_collapse_with_one_destination_and_roots_preserved(self) -> None:
        self._two_provider_roots()
        plan = self.plan()
        equivalent = True

        def closure(**kwargs: object) -> core.ResolvedModClosure:
            self._current_root = str(kwargs["canonical_project_id"])
            return self._two_provider_closure(equivalent=equivalent)

        with patch.object(packctl, "init_packwiz_project", side_effect=self.fake_init), patch.object(
            core, "resolve_mod_closure", side_effect=closure
        ), patch.object(core, "resolve_project_selector", side_effect=self.fake_selector), patch.object(packctl, "run_packwiz"):
            result = resolution.resolve_pack_migration_plan_at(plan, repository_root=self.root, state_root=self.state)

        target = plan.target_staging_root / "source"
        self.assertEqual(result.state, "resolved")
        shared = [core.read_mod(target, path) for path in target.rglob("*.pw.toml") if path.is_file() and 'filename = "shared.jar"' in path.read_text()]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0].side, "both")
        self.assertEqual(
            [item.canonical_identity for item in read_pack_root_manifest(target)],
            ["curseforge:123", "modrinth:root-project"],
        )
        pack_migration.discard_pack_migration_plan(plan)

    def test_final_merged_root_side_drives_preview_and_target_provenance(self) -> None:
        (self.source / "mods/example.pw.toml").unlink(missing_ok=True)
        modrinth_root = self.provider_metadata(
            "modrinth", "root-project", filename="root.jar", side="client",
            digest="1" * 64,
        )
        curseforge_root = self.provider_metadata(
            "curseforge", "123", filename="other.jar", side="server",
            digest="2" * 64,
        )
        (self.source / modrinth_root.relative_path).write_bytes(modrinth_root.contents)
        (self.source / curseforge_root.relative_path).write_bytes(curseforge_root.contents)
        write_pack_root_manifest(
            self.source,
            (
                PackRootRecord("modrinth", "root-project", "client"),
                PackRootRecord("curseforge", "123", "server"),
            ),
        )
        plan = self.plan()

        def closure(**kwargs: object) -> core.ResolvedModClosure:
            project = str(kwargs["canonical_project_id"])
            if project == "root-project":
                return core.ResolvedModClosure(
                    ("modrinth", project), (modrinth_root,)
                )
            dependency_view = self.provider_metadata(
                "modrinth", "root-project", filename="root.jar", side="server",
                digest="1" * 64,
            )
            return core.ResolvedModClosure(
                ("curseforge", project), (curseforge_root, dependency_view)
            )

        with patch.object(packctl, "init_packwiz_project", side_effect=self.fake_init), patch.object(
            core, "resolve_mod_closure", side_effect=closure
        ), patch.object(core, "resolve_project_selector", side_effect=self.fake_selector), patch.object(
            packctl, "run_packwiz"
        ):
            result = resolution.resolve_pack_migration_plan_at(
                plan, repository_root=self.root, state_root=self.state
            )
        finalized = {
            item.target_identity: item for item in result.resolved_roots
        }
        self.assertEqual(finalized["modrinth:root-project"].target_side, "both")
        self.assertEqual(
            finalized["modrinth:root-project"].classification, "updated"
        )
        target_roots = {
            item.canonical_identity: item.side
            for item in read_pack_root_manifest(plan.target_staging_root / "source")
        }
        self.assertEqual(
            target_roots,
            {"curseforge:123": "server", "modrinth:root-project": "both"},
        )
        pack_migration.discard_pack_migration_plan(plan)

    def test_non_equivalent_cross_provider_dependency_collision_fails_closed(self) -> None:
        self._two_provider_roots()
        plan = self.plan()

        def closure(**kwargs: object) -> core.ResolvedModClosure:
            self._current_root = str(kwargs["canonical_project_id"])
            return self._two_provider_closure(equivalent=False)

        def materialize(candidate: object, *_: object, **__: object):
            from dependency_equivalence import MaterializedArtifact
            return MaterializedArtifact("e" * 64 if "modrinth" in getattr(candidate, "provider_identity", "") else "f" * 64)

        with patch.object(packctl, "init_packwiz_project", side_effect=self.fake_init), patch.object(
            core, "resolve_mod_closure", side_effect=closure
        ), patch.object(core, "resolve_project_selector", side_effect=self.fake_selector), patch.object(
            core, "materialize_provider_artifact", side_effect=materialize
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "could not be verified as equivalent"):
                resolution.resolve_pack_migration_plan_at(plan, repository_root=self.root, state_root=self.state)
        self.assertEqual(plan.state, "failed")
        pack_migration.discard_pack_migration_plan(plan)

    def test_failed_handoff_rolls_staging_source_back(self) -> None:
        plan = self.plan()
        before = pack_migration.scan_pack_migration_source(
            plan.target_staging_root / "source", checkpoint=lambda: None
        ).content_digest
        real_renameat2 = packctl.renameat2

        def fail_original_source(
            old_dir_fd: int,
            old_path: str,
            new_dir_fd: int,
            new_path: str,
            flags: int,
        ) -> None:
            if old_path == "source" and new_path == "original-source":
                raise OSError("injected handoff failure")
            real_renameat2(old_dir_fd, old_path, new_dir_fd, new_path, flags)

        with patch.object(packctl, "init_packwiz_project", side_effect=self.fake_init), patch.object(
            core, "resolve_mod_closure", side_effect=self.fake_closure
        ), patch.object(core, "resolve_project_selector", side_effect=self.fake_selector), patch.object(
            packctl, "run_packwiz"
        ), patch.object(packctl, "renameat2", side_effect=fail_original_source):
            with self.assertRaisesRegex(OSError, "injected handoff failure"):
                resolution.resolve_pack_migration_plan_at(
                    plan,
                    repository_root=self.root,
                    state_root=self.state,
                )
        after = pack_migration.scan_pack_migration_source(
            plan.target_staging_root / "source", checkpoint=lambda: None
        ).content_digest
        self.assertEqual(after, before)
        self.assertEqual(plan.state, "failed")
        pack_migration.discard_pack_migration_plan(plan)

    def test_unresolved_retains_original_staging_source(self) -> None:
        plan = self.plan()
        before = (plan.target_staging_root / "source" / "pack.toml").read_bytes()
        with patch.object(packctl, "init_packwiz_project", side_effect=self.fake_init), patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.HuroshikiError("no compatible file"),
        ), patch.object(core, "resolve_project_selector", side_effect=self.fake_selector):
            result = resolution.resolve_pack_migration_plan_at(
                plan,
                repository_root=self.root,
                state_root=self.state,
            )
        self.assertEqual(result.state, "resolution-required")
        self.assertEqual(plan.state, "resolution-required")
        self.assertEqual(
            (plan.target_staging_root / "source" / "pack.toml").read_bytes(), before
        )
        self.assertEqual(result.unresolved_roots[0].reason_code, "no-compatible-file")
        with self.assertRaisesRegex(pack_migration.PackMigrationError, "publication requires"):
            pack_migration.apply_pack_copy_migration_at(plan)
        pack_migration.discard_pack_migration_plan(plan)

    def test_cancellation_is_operation_failure_not_unresolved(self) -> None:
        plan = self.plan()
        with patch.object(packctl, "init_packwiz_project", side_effect=self.fake_init), patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.HuroshikiError("MOD resolution was cancelled"),
        ), patch.object(core, "resolve_project_selector", side_effect=self.fake_selector):
            with self.assertRaises(resolution.PackMigrationResolutionError):
                resolution.resolve_pack_migration_plan_at(
                    plan,
                    repository_root=self.root,
                    state_root=self.state,
                )
        self.assertEqual(plan.state, "failed")
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        pack_migration.discard_pack_migration_plan(plan)

    def test_url_compatibility_never_promotes_unknown(self) -> None:
        compatible = resolution._url_compatibility(
            b'[huroshiki]\nloaders = ["fabric"]\nminecraft-versions = ["1.21.4"]\n',
            self.target(),
        )
        unknown = resolution._url_compatibility(
            b'[huroshiki]\nloaders = ["fabric"]\nminecraft-versions = [">=1.21"]\n',
            self.target(),
        )
        incompatible = resolution._url_compatibility(
            b'[huroshiki]\nloaders = ["neoforge"]\nminecraft-versions = ["1.21.4"]\n',
            self.target(),
        )
        self.assertEqual(compatible.status, "compatible")
        self.assertEqual(unknown.status, "unknown")
        self.assertEqual(incompatible.status, "incompatible")

    def test_existing_pack_bootstraps_explicit_roots_before_resolution(self) -> None:
        (self.source / ".huroshiki-roots.json").unlink()
        (self.source / ".packwizignore").write_text("*.log\n", encoding="utf-8")
        (self.source / "mods" / "dependency.pw.toml").write_bytes(
            metadata("v1", dependency=True).contents
        )
        first_plan = self.plan()
        original_staging = pack_migration.scan_pack_migration_source(
            first_plan.target_staging_root / "source", checkpoint=lambda: None
        ).content_digest

        first_result = resolution.resolve_pack_migration_plan_at(
            first_plan,
            repository_root=self.root,
            state_root=self.state,
        )

        self.assertEqual(first_result.state, "resolution-required")
        self.assertTrue(first_result.provenance_required)
        self.assertEqual(first_result.roots, ())
        self.assertEqual(len(first_result.root_candidates), 2)
        self.assertTrue(
            all(
                unresolved.reason_code == "root-provenance-required"
                for unresolved in first_result.unresolved_roots
            )
        )
        self.assertFalse((first_plan.transaction_root / "resolver-work").exists())
        diagnostic = json.loads((first_plan.transaction_root / "plan.json").read_text())
        self.assertTrue(diagnostic["resolution"]["provenance_required"])
        self.assertEqual(diagnostic["resolution"]["root_candidates"], 2)
        self.assertEqual(
            pack_migration.scan_pack_migration_source(
                first_plan.target_staging_root / "source", checkpoint=lambda: None
            ).content_digest,
            original_staging,
        )

        source_before_selection = filesystem_snapshot(self.source)
        with patch.object(packctl, "init_packwiz_project", side_effect=self.fake_init), patch.object(
            core, "resolve_mod_closure", side_effect=self.fake_closure
        ), patch.object(core, "resolve_project_selector", side_effect=self.fake_selector), patch.object(
            packctl, "run_packwiz"
        ):
            selected_result = resolution.select_pack_migration_roots_at(
                first_plan,
            (
                PackMigrationRootSelection(
                    Path("mods/example.pw.toml"),
                    "modrinth",
                    "root-project",
                ),
            ),
            repository_root=self.root,
            state_root=self.state,
        )
        self.assertEqual(selected_result.state, "resolved")
        self.assertEqual(filesystem_snapshot(self.source), source_before_selection)
        self.assertEqual(
            [record.canonical_identity for record in read_pack_root_manifest(
                first_plan.target_staging_root / "source"
            )],
            ["modrinth:root-project"],
        )
        self.assertEqual(
            (first_plan.target_staging_root / "source" / ".packwizignore").read_text(
                encoding="utf-8"
            ),
            "/.huroshiki-roots.json\n",
        )
        self.assertFalse((self.source / ".huroshiki-roots.json").exists())
        pack_migration.discard_pack_migration_plan(first_plan)

    def test_provenance_exchange_exception_rolls_live_source_back(self) -> None:
        (self.source / ".huroshiki-roots.json").unlink()
        plan = self.plan()
        result = resolution.resolve_pack_migration_plan_at(
            plan,
            repository_root=self.root,
            state_root=self.state,
        )
        self.assertTrue(result.provenance_required)
        real_renameat2 = packctl.renameat2
        injected = False

        def fail_after_exchange(
            old_dir_fd: int,
            old_path: str,
            new_dir_fd: int,
            new_path: str,
            flags: int,
        ) -> None:
            nonlocal injected
            real_renameat2(old_dir_fd, old_path, new_dir_fd, new_path, flags)
            if (
                not injected
                and old_path == "source"
                and new_path == "provenance-staging"
                and flags == packctl.RENAME_EXCHANGE
            ):
                injected = True
                raise OSError("injected provenance exchange uncertainty")

        with patch.object(packctl, "renameat2", side_effect=fail_after_exchange):
            with self.assertRaisesRegex(OSError, "exchange uncertainty"):
                resolution.commit_pack_migration_root_selection_at(
                    plan,
                    (
                        PackMigrationRootSelection(
                            Path("mods/example.pw.toml"),
                            "modrinth",
                            "root-project",
                        ),
                    ),
                    repository_root=self.root,
                )
        self.assertFalse((self.source / ".huroshiki-roots.json").exists())
        self.assertEqual(plan.state, "failed")
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        pack_migration.discard_pack_migration_plan(plan)

    def test_legacy_url_root_selection_is_local_and_target_gets_identity(self) -> None:
        (self.source / ".huroshiki-roots.json").unlink()
        (self.source / "mods" / "example.pw.toml").write_text(
            '''name = "Legacy URL"
filename = "legacy.jar"
side = "both"
[download]
url = "https://example.invalid/legacy.jar"
[huroshiki]
loaders = ["fabric"]
minecraft-versions = ["1.21.4"]
''',
            encoding="utf-8",
        )
        plan = self.plan()
        result = resolution.resolve_pack_migration_plan_at(
            plan,
            repository_root=self.root,
            state_root=self.state,
        )
        self.assertIsNone(result.root_candidates[0].canonical_identity)
        source_before = filesystem_snapshot(self.source)
        target_url_metadata = b'''name = "Legacy URL"
filename = "legacy.jar"
side = "both"
[download]
url = "https://example.invalid/legacy.jar"
[huroshiki]
project-id = "legacy-url-id"
loaders = ["fabric"]
minecraft-versions = ["1.21.4"]
'''
        legacy_closure = core.ResolvedModClosure(
            ("url", "legacy-url-id"),
            (core.ResolvedMetadata(
                ("url", "legacy-url-id"), Path("mods/example.pw.toml"), "legacy.jar",
                target_url_metadata,
                "url", "legacy-url-id"
            ),),
        )
        with patch.object(packctl, "init_packwiz_project", side_effect=self.fake_init), patch.object(
            core, "resolve_mod_closure", return_value=legacy_closure
        ), patch.object(packctl, "run_packwiz") as refresh:
            selected = resolution.select_pack_migration_roots_at(
                plan,
                (
                    PackMigrationRootSelection(
                        Path("mods/example.pw.toml"),
                        "url",
                        "legacy-url-id",
                    ),
                ),
            repository_root=self.root,
            state_root=self.state,
        )
        refresh.assert_called_once()
        self.assertEqual(selected.state, "resolved")
        self.assertEqual(filesystem_snapshot(self.source), source_before)
        self.assertIn(
            'project-id = "legacy-url-id"',
            (plan.target_staging_root / "source" / "mods" / "example.pw.toml").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            read_pack_root_manifest(plan.target_staging_root / "source")[0].canonical_identity,
            "url:legacy-url-id",
        )
        pack_migration.discard_pack_migration_plan(plan)

    def test_legacy_url_refresh_cannot_change_selected_metadata(self) -> None:
        (self.source / ".huroshiki-roots.json").unlink()
        (self.source / "mods" / "example.pw.toml").write_text(
            '''name = "Legacy URL"
filename = "legacy.jar"
side = "both"
[download]
url = "https://example.invalid/legacy.jar"
''',
            encoding="utf-8",
        )
        plan = self.plan()
        required = resolution.resolve_pack_migration_plan_at(
            plan, repository_root=self.root, state_root=self.state
        )
        self.assertTrue(required.provenance_required)
        source_before = filesystem_snapshot(self.source)

        def mutate_metadata(*_args: object, **kwargs: object) -> None:
            path = Path(kwargs["cwd"]) / "mods/example.pw.toml"
            path.write_bytes(path.read_bytes() + b"\n# changed by refresh\n")

        with patch.object(packctl, "run_packwiz", side_effect=mutate_metadata):
            with self.assertRaisesRegex(
                resolution.PackMigrationResolutionError,
                "refresh changed Packwiz metadata",
            ):
                resolution.select_pack_migration_roots_at(
                    plan,
                    (
                        PackMigrationRootSelection(
                            Path("mods/example.pw.toml"),
                            "url",
                            "legacy-url-id",
                        ),
                    ),
                    repository_root=self.root,
                    state_root=self.state,
                )
        self.assertEqual(filesystem_snapshot(self.source), source_before)
        self.assertFalse((self.packs / "next").exists())
        pack_migration.discard_pack_migration_plan(plan)


if __name__ == "__main__":
    unittest.main()
