from __future__ import annotations

from pathlib import Path
import json
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
        with self.assertRaisesRegex(pack_migration.PackMigrationError, "cannot be applied"):
            pack_migration.apply_pack_copy_migration_at(plan)
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
        with self.assertRaisesRegex(pack_migration.PackMigrationError, "cannot be applied"):
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

        selected = resolution.commit_pack_migration_root_selection_at(
            first_plan,
            (
                PackMigrationRootSelection(
                    Path("mods/example.pw.toml"),
                    "modrinth",
                    "root-project",
                ),
            ),
            repository_root=self.root,
        )
        self.assertEqual(first_plan.state, "discarded")
        self.assertEqual(
            [record.canonical_identity for record in selected],
            ["modrinth:root-project"],
        )
        self.assertEqual(
            [record.canonical_identity for record in read_pack_root_manifest(self.source)],
            ["modrinth:root-project"],
        )
        self.assertIn(
            "/.huroshiki-roots.json",
            (self.source / ".packwizignore").read_text(encoding="utf-8").splitlines(),
        )

        second_plan = self.plan()
        with patch.object(packctl, "init_packwiz_project", side_effect=self.fake_init), patch.object(
            core, "resolve_mod_closure", side_effect=self.fake_closure
        ), patch.object(core, "resolve_project_selector", side_effect=self.fake_selector), patch.object(
            packctl, "run_packwiz"
        ):
            second_result = resolution.resolve_pack_migration_plan_at(
                second_plan,
                repository_root=self.root,
                state_root=self.state,
            )
        self.assertEqual(second_result.state, "resolved")
        self.assertFalse(second_result.provenance_required)
        pack_migration.discard_pack_migration_plan(second_plan)

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

    def test_legacy_url_root_selection_persists_identity_and_refreshes(self) -> None:
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
        result = resolution.resolve_pack_migration_plan_at(
            plan,
            repository_root=self.root,
            state_root=self.state,
        )
        self.assertIsNone(result.root_candidates[0].canonical_identity)
        with patch.object(packctl, "run_packwiz") as refresh:
            resolution.commit_pack_migration_root_selection_at(
                plan,
                (
                    PackMigrationRootSelection(
                        Path("mods/example.pw.toml"),
                        "url",
                        "legacy-url-id",
                    ),
                ),
                repository_root=self.root,
            )
        refresh.assert_called_once()
        self.assertIn(
            'project-id = "legacy-url-id"',
            (self.source / "mods" / "example.pw.toml").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            read_pack_root_manifest(self.source)[0].canonical_identity,
            "url:legacy-url-id",
        )


if __name__ == "__main__":
    unittest.main()
