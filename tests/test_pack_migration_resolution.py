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


if __name__ == "__main__":
    unittest.main()
