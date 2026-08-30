from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

import huroshiki_core as core
import pack_migration
import pack_migration_conflicts as conflicts
import pack_migration_resolution as resolution
import packctl
from pack_migration_roots import PackRootRecord, write_pack_root_manifest
from tests import test_pack_migration_resolution as migration_fixture


class PackMigrationConflictTest(unittest.TestCase):
    """Focused API and transaction tests for migration conflict choices."""

    def setUp(self) -> None:
        self.base = migration_fixture.PackMigrationResolutionTest(methodName="runTest")
        self.base.setUp()
        for name in ("root", "packs", "templates", "state", "pack", "source"):
            setattr(self, name, getattr(self.base, name))

    def tearDown(self) -> None:
        self.base.tearDown()

    def _unresolved_plan(self) -> pack_migration.PackMigrationPlan:
        plan = self.base.plan()
        with patch.object(packctl, "init_packwiz_project", side_effect=self.base.fake_init), patch.object(
            core,
            "resolve_mod_closure",
            side_effect=core.HuroshikiError("no compatible file"),
        ), patch.object(core, "resolve_project_selector", side_effect=self.base.fake_selector):
            resolution.resolve_pack_migration_plan_at(
                plan, repository_root=self.root, state_root=self.state
            )
        self.assertEqual(plan.state, "resolution-required")
        return plan

    def _request(
        self,
        plan: pack_migration.PackMigrationPlan,
        choices: tuple[conflicts.PackMigrationRootResolution, ...],
        **overrides: object,
    ) -> conflicts.PackMigrationResolutionRequest:
        assert plan.resolution is not None
        values: dict[str, object] = {
            "plan_identity": id(plan),
            "source_snapshot_digest": plan.source_snapshot.snapshot_digest,
            "resolution_snapshot_digest": conflicts.resolution_snapshot_digest(
                plan.resolution, int(getattr(plan, "_resolution_attempt", 0))
            ),
            "resolutions": choices,
        }
        values.update(overrides)
        return conflicts.PackMigrationResolutionRequest(**values)

    def _remove_request(self, plan: pack_migration.PackMigrationPlan):
        identity = plan.resolution.unresolved_roots[0].source_root.canonical_identity
        return self._request(
            plan, (conflicts.PackMigrationRootResolution(identity, "remove"),)
        )

    def test_core_factory_binds_and_validates_current_conflict_request(self) -> None:
        plan = self._unresolved_plan()
        identity = plan.resolution.unresolved_roots[0].source_root.canonical_identity
        request = core.create_pack_migration_resolution_request(
            plan,
            (core.PackMigrationRootResolution(identity, "remove"),),
        )

        self.assertEqual(request.plan_identity, id(plan))
        self.assertEqual(
            request.source_snapshot_digest,
            plan.source_snapshot.snapshot_digest,
        )
        self.assertEqual(
            conflicts.validate_resolution_request(plan, request).effective_roots,
            (),
        )
        pack_migration.discard_pack_migration_plan(plan)

    def test_request_rejects_state_plan_identity_source_and_unresolved_digests(self) -> None:
        plan = self._unresolved_plan()
        request = self._remove_request(plan)
        cases = (
            ("state", lambda: setattr(plan, "_state", "staged"), "not awaiting"),
            ("plan", lambda: request.__class__(999, request.source_snapshot_digest, request.resolution_snapshot_digest, request.resolutions), "another plan"),
            ("source", lambda: replace(request, source_snapshot_digest="stale"), "Source snapshot"),
            ("unresolved", lambda: replace(request, resolution_snapshot_digest="stale"), "Unresolved root"),
        )
        for _, make, message in cases:
            with self.subTest(message=message):
                candidate = make()
                if candidate is None:
                    candidate = request
                with self.assertRaisesRegex(conflicts.PackMigrationConflictResolutionError, message):
                    conflicts.validate_resolution_request(plan, candidate)
                plan._state = "resolution-required"

    def test_request_rejects_duplicate_unknown_and_incomplete_coverage(self) -> None:
        plan = self._unresolved_plan()
        identity = plan.resolution.unresolved_roots[0].source_root.canonical_identity
        choices = conflicts.PackMigrationRootResolution(identity, "remove")
        for resolutions, message in (
            ((choices, choices), "Duplicate root resolution"),
            ((conflicts.PackMigrationRootResolution("modrinth:unknown", "remove"),), "Unknown unresolved"),
            ((), "Every unresolved root"),
        ):
            with self.subTest(message=message):
                request = self._request(plan, resolutions)
                with self.assertRaisesRegex(conflicts.PackMigrationConflictResolutionError, message):
                    conflicts.validate_resolution_request(plan, request)

    def test_request_validates_remove_and_replace_fields_and_actions(self) -> None:
        plan = self._unresolved_plan()
        identity = plan.resolution.unresolved_roots[0].source_root.canonical_identity
        invalid = (
            conflicts.PackMigrationRootResolution(identity, "remove", "curseforge", "123"),
            conflicts.PackMigrationRootResolution(identity, "replace", None, "123"),
            conflicts.PackMigrationRootResolution(identity, "replace", "curseforge", None),
            conflicts.PackMigrationRootResolution(identity, "replace", "modrinth", "root-project"),
            conflicts.PackMigrationRootResolution(identity, "keep"),  # type: ignore[arg-type]
        )
        messages = ("cannot include", "Replacement provider", "Replacement project", "different canonical", "Unsupported")
        for choice, message in zip(invalid, messages):
            with self.subTest(message=message):
                with self.assertRaisesRegex(conflicts.PackMigrationConflictResolutionError, message):
                    conflicts.validate_resolution_request(plan, self._request(plan, (choice,)))

    def test_provenance_is_required_before_conflict_choices(self) -> None:
        (self.source / ".huroshiki-roots.json").unlink()
        plan = self.base.plan()
        with patch.object(packctl, "init_packwiz_project", side_effect=self.base.fake_init), patch.object(
            core, "resolve_mod_closure", side_effect=core.HuroshikiError("unused")
        ), patch.object(core, "resolve_project_selector", side_effect=self.base.fake_selector):
            resolution.resolve_pack_migration_plan_at(plan, repository_root=self.root, state_root=self.state)
        identity = plan.resolution.unresolved_roots[0].source_root.canonical_identity
        request = self._request(plan, (conflicts.PackMigrationRootResolution(identity, "remove"),))
        with self.assertRaisesRegex(conflicts.PackMigrationConflictResolutionError, "provenance"):
            conflicts.validate_resolution_request(plan, request)

    def test_duplicate_unresolved_and_duplicate_replacement_target_are_rejected(self) -> None:
        (self.source / "mods" / "second.pw.toml").write_bytes(
            b'''name = "second"\nfilename = "second.jar"\nside = "both"\n[update.modrinth]\nmod-id = "second"\nversion = "v1"\n'''
        )
        write_pack_root_manifest(
            self.source,
            (PackRootRecord("modrinth", "root-project", "both"), PackRootRecord("modrinth", "second", "both")),
        )
        plan = self._unresolved_plan()
        duplicate = replace(plan.resolution, unresolved_roots=(plan.resolution.unresolved_roots[0], plan.resolution.unresolved_roots[0]))
        plan.resolution = duplicate
        with self.assertRaisesRegex(conflicts.PackMigrationConflictResolutionError, "Duplicate unresolved"):
            conflicts.validate_resolution_request(plan, self._request(plan, ()))
        plan.resolution = replace(plan.resolution, unresolved_roots=tuple({item.source_root.canonical_identity: item for item in plan.resolution.unresolved_roots}.values()))
        existing = replace(
            plan.resolution.roots[0],
            canonical_identity="curseforge:123",
            provider="curseforge",
            project_id="123",
        )
        plan.resolution = replace(plan.resolution, roots=plan.resolution.roots + (existing,))
        identities = [item.source_root.canonical_identity for item in plan.resolution.unresolved_roots]
        request = self._request(plan, tuple(conflicts.PackMigrationRootResolution(item, "replace", "curseforge", "123") for item in identities))
        with self.assertRaisesRegex(conflicts.PackMigrationConflictResolutionError, "collision"):
            conflicts.validate_resolution_request(plan, request)

    def test_curseforge_replacements_require_numeric_canonical_ids(self) -> None:
        plan = self._unresolved_plan()
        identity = plan.resolution.unresolved_roots[0].source_root.canonical_identity
        for project_id in ("slug", "-1", "0", "0123", "12.5"):
            with self.subTest(project_id=project_id):
                choice = conflicts.PackMigrationRootResolution(identity, "replace", "curseforge", project_id)
                with self.assertRaises(conflicts.PackMigrationConflictResolutionError):
                    conflicts.validate_resolution_request(plan, self._request(plan, (choice,)))

    def test_modrinth_replacement_rejects_noncanonical_selector_before_attempt(self) -> None:
        plan = self._unresolved_plan()
        identity = plan.resolution.unresolved_roots[0].source_root.canonical_identity
        request = self._request(
            plan,
            (
                conflicts.PackMigrationRootResolution(
                    identity, "replace", "modrinth", "display-slug"
                ),
            ),
        )
        canonical = core.ResolvedSelector(
            "modrinth", "display-slug", "canonical-id", "Replacement"
        )
        with patch.object(
            core, "resolve_project_selector", return_value=canonical
        ), patch.object(
            core, "resolve_mod_closure", side_effect=AssertionError("resolver called")
        ):
            with self.assertRaisesRegex(
                conflicts.PackMigrationConflictResolutionError,
                "canonical project ID",
            ):
                resolution.resolve_pack_migration_conflicts_at(
                    plan,
                    request,
                    repository_root=self.root,
                    state_root=self.state,
                )
        self.assertEqual(plan.state, "resolution-required")
        self.assertEqual(plan._resolution_attempt, 0)
        pack_migration.discard_pack_migration_plan(plan)

    def test_remove_all_unresolved_roots_uses_fresh_workspace_and_keeps_apply_prohibited(self) -> None:
        plan = self._unresolved_plan()
        before = plan._staging_snapshot_digest
        request = self._remove_request(plan)
        observed: list[str] = []
        real_exchange = resolution._exchange_target_source

        def exchange(*args: object, **kwargs: object):
            observed.append(plan._staging_snapshot_digest)
            return real_exchange(*args, **kwargs)

        with patch.object(packctl, "init_packwiz_project", side_effect=self.base.fake_init), patch.object(
            packctl, "run_packwiz"
        ), patch.object(resolution, "_exchange_target_source", side_effect=exchange):
            result = resolution.resolve_pack_migration_conflicts_at(
                plan, request, repository_root=self.root, state_root=self.state
            )
        self.assertEqual(observed, [before])
        self.assertEqual(result.state, "resolved")
        self.assertEqual(result.attempt_number, 1)
        self.assertEqual(result.removed_roots[0].source_root.canonical_identity, "modrinth:root-project")
        self.assertTrue((plan.transaction_root / "resolver-work-attempt-0001").is_dir())
        with self.assertRaisesRegex(pack_migration.PackMigrationError, "publication requires"):
            pack_migration.apply_pack_copy_migration_at(plan)
        pack_migration.discard_pack_migration_plan(plan)

    def test_replace_unresolved_modrinth_root_with_canonical_curseforge_root(self) -> None:
        plan = self._unresolved_plan()
        identity = plan.resolution.unresolved_roots[0].source_root.canonical_identity
        request = self._request(plan, (conflicts.PackMigrationRootResolution(identity, "replace", "curseforge", "123"),))
        contents = b'''name = "replacement"\nfilename = "replacement.jar"\nside = "both"\n[update.curseforge]\nproject-id = "123"\nfile-id = "456"\n'''
        closure = core.ResolvedModClosure(("curseforge", "123"), (core.ResolvedMetadata(("curseforge", "123"), Path("mods/replacement.pw.toml"), "replacement.jar", contents, "curseforge", "123"),))
        calls: list[dict[str, object]] = []

        def resolve_closure(**kwargs: object):
            calls.append(kwargs)
            return closure

        with patch.object(packctl, "init_packwiz_project", side_effect=self.base.fake_init), patch.object(
            core, "resolve_mod_closure", side_effect=resolve_closure
        ), patch.object(packctl, "run_packwiz"):
            result = resolution.resolve_pack_migration_conflicts_at(plan, request, repository_root=self.root, state_root=self.state)
        self.assertEqual(calls[0]["provider"], "curseforge")
        self.assertEqual(calls[0]["canonical_project_id"], "123")
        self.assertEqual(result.state, "resolved")
        self.assertIn(("modrinth:root-project", "curseforge:123"), result.resolution_plan.identity_changes)
        self.assertTrue(result.replaced_roots[0].provider_changed)
        self.assertEqual(result.attempt_number, 1)
        diagnostic = (plan.transaction_root / "plan.json").read_text()
        self.assertIn("curseforge:123", diagnostic)
        self.assertIn('"schema": 3', diagnostic)
        pack_migration.discard_pack_migration_plan(plan)

    def test_replace_does_not_auto_collapse_two_explicit_equivalent_roots(self) -> None:
        self.source.joinpath("pack.toml").write_text(
            '[versions]\nminecraft = "1.21.1"\nfabric = "0.16.0"\n', encoding="utf-8"
        )
        digest = "d" * 64
        existing = (
            'name = "123"\nfilename = "shared.jar"\nside = "server"\n'
            '[download]\nhash-format = "sha256"\n'
            f'hash = "{digest}"\nurl = "https://example.invalid/123.jar"\n'
            '[update.curseforge]\nproject-id = 123\nfile-id = 1\n'
        ).encode()
        incoming = (
            'name = "replacement"\nfilename = "shared.jar"\nside = "client"\n'
            '[download]\nhash-format = "sha256"\n'
            f'hash = "{digest}"\nurl = "https://example.invalid/replacement.jar"\n'
            '[update.modrinth]\nmod-id = "replacement"\nversion = "v2"\n'
        ).encode()
        (self.source / "mods/existing.pw.toml").write_bytes(existing)
        write_pack_root_manifest(
            self.source,
            (PackRootRecord("curseforge", "123", "server"),),
        )
        closure = core.ResolvedModClosure(
            ("modrinth", "replacement"),
            (core.ResolvedMetadata(
                ("modrinth", "replacement"), Path("mods/incoming.pw.toml"),
                "shared.jar", incoming, "modrinth", "replacement",
            ),),
        )
        with self.assertRaisesRegex(core.HuroshikiError, "two explicit roots cannot be merged"):
            core.merge_metadata_closure(
                self.source, closure, requested_side="client",
            )
        self.assertEqual(
            [item.canonical_identity for item in core.read_pack_root_manifest(self.source)],
            ["curseforge:123"],
        )

    def test_unresolved_replacement_keeps_staging_and_stale_request_never_reaches_resolver(self) -> None:
        plan = self._unresolved_plan()
        identity = plan.resolution.unresolved_roots[0].source_root.canonical_identity
        request = self._request(plan, (conflicts.PackMigrationRootResolution(identity, "replace", "curseforge", "123"),))
        before = plan._staging_snapshot_digest
        with patch.object(packctl, "init_packwiz_project", side_effect=self.base.fake_init), patch.object(
            core, "resolve_mod_closure", side_effect=core.HuroshikiError("still missing")
        ), patch.object(packctl, "run_packwiz"):
            result = resolution.resolve_pack_migration_conflicts_at(plan, request, repository_root=self.root, state_root=self.state)
        self.assertEqual(result.state, "resolution-required")
        self.assertEqual(plan._staging_snapshot_digest, before)
        with patch.object(core, "resolve_mod_closure", side_effect=AssertionError("resolver called")):
            with self.assertRaisesRegex(conflicts.PackMigrationConflictResolutionError, "stale"):
                resolution.resolve_pack_migration_conflicts_at(plan, request, repository_root=self.root, state_root=self.state)
        pack_migration.discard_pack_migration_plan(plan)


if __name__ == "__main__":
    unittest.main()
