from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

import huroshiki_core as core
from mod_version_overrides import (
    ModVersionOverride,
    ensure_mod_version_overrides_ignored,
    read_mod_version_overrides,
    write_mod_version_overrides,
)
import pack_migration
import pack_migration_resolution as resolution
import packctl
from pack_migration_roots import PackRootRecord, write_pack_root_manifest
from tests import test_pack_migration_core as migration_fixture
from tests.test_pack_migration_resolution import TARGET_PACK_TOML


ROOT_ID = "rootproj"
DEPENDENCY_ID = "depndncy"
SECOND_DEPENDENCY_ID = "depndcy2"
OTHER_ROOT_ID = "otheroot"
VERSION_1 = "ver00001"
VERSION_2 = "ver00002"


def metadata(version: str, *, dependency: bool = False) -> core.ResolvedMetadata:
    project = DEPENDENCY_ID if dependency else ROOT_ID
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


def dependency_metadata(project: str, version: str) -> core.ResolvedMetadata:
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


def curseforge_metadata(project: str, artifact: str) -> core.ResolvedMetadata:
    contents = f'''name = "curseforge-{project}"
filename = "curseforge-{project}-{artifact}.jar"
side = "both"
[download]
url = "https://mediafilez.forgecdn.net/{project}/{artifact}.jar"
[update.curseforge]
project-id = {project}
file-id = {artifact}
'''.encode()
    return core.ResolvedMetadata(
        ("curseforge", project),
        Path("mods") / f"curseforge-{project}.pw.toml",
        f"curseforge-{project}-{artifact}.jar",
        contents,
        "curseforge",
        project,
    )


class PackMigrationVersionIntentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = migration_fixture.PackMigrationCoreTest(methodName="runTest")
        self.base.setUp()
        for name in ("root", "packs", "templates", "state", "pack", "source"):
            setattr(self, name, getattr(self.base, name))
        (self.source / "mods" / "example.pw.toml").write_bytes(
            metadata(VERSION_1).contents
        )
        (self.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        write_pack_root_manifest(
            self.source,
            (PackRootRecord("modrinth", ROOT_ID, "both"),),
        )

    def tearDown(self) -> None:
        self.base.tearDown()

    @staticmethod
    def target() -> pack_migration.PackMigrationTarget:
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
        (source / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        (source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n", encoding="utf-8"
        )
        write_pack_root_manifest(source, ())

    @staticmethod
    def fake_selector(*_: object, **__: object) -> core.ResolvedSelector:
        return core.ResolvedSelector(
            "modrinth", ROOT_ID, ROOT_ID, "Root"
        )

    def set_overrides(self, *entries: ModVersionOverride) -> None:
        ensure_mod_version_overrides_ignored(self.source)
        write_mod_version_overrides(self.source, tuple(entries))

    def exact_root_conflict(
        self,
        *,
        locked: bool,
        original_is_transitive: bool,
        incompatible_on_retry: bool = False,
    ) -> tuple[
        pack_migration.PackMigrationPlan,
        object,
        object,
        object,
    ]:
        other = dependency_metadata(OTHER_ROOT_ID, VERSION_1)
        (self.source / other.relative_path).write_bytes(other.contents)
        write_pack_root_manifest(
            self.source,
            (
                PackRootRecord("modrinth", ROOT_ID, "both"),
                PackRootRecord("modrinth", OTHER_ROOT_ID, "both"),
            ),
        )
        self.set_overrides(
            ModVersionOverride(
                "modrinth", ROOT_ID, VERSION_1, locked, "Original exact choice"
            )
        )
        plan = self.plan()

        def selector(
            provider: str, project_id: str, **_: object
        ) -> core.ResolvedSelector:
            return core.ResolvedSelector(
                provider, project_id, project_id, project_id
            )

        def automatic(**kwargs: object) -> core.ResolvedModClosure:
            provider = str(kwargs["provider"])
            project_id = str(kwargs["canonical_project_id"])
            if provider == "curseforge":
                item = curseforge_metadata(project_id, "456")
                return core.ResolvedModClosure(item.identity, (item,))
            owner = dependency_metadata(project_id, VERSION_2)
            members = [owner]
            if project_id == OTHER_ROOT_ID and original_is_transitive:
                members.append(metadata(VERSION_2))
            return core.ResolvedModClosure(owner.identity, tuple(members))

        def exact(
            selection: core.ExactModArtifactSelection,
            **kwargs: object,
        ) -> core.ResolvedModClosure:
            preseeds = tuple(kwargs.get("preseed_selections", ()))
            if selection.identity_label == f"modrinth:{ROOT_ID}" and not preseeds:
                raise core.HuroshikiError("exact root requires conflict resolution")
            if (
                selection.identity_label == f"modrinth:{OTHER_ROOT_ID}"
                and incompatible_on_retry
                and plan._resolution_attempt > 0
            ):
                raise core.HuroshikiError(
                    "original exact dependency is incompatible with target"
                )
            owner = dependency_metadata(OTHER_ROOT_ID, VERSION_2)
            return core.ResolvedModClosure(
                owner.identity,
                (owner, metadata(VERSION_1)),
            )

        with patch.object(
            packctl, "init_packwiz_project", side_effect=self.fake_init
        ), patch.object(
            core, "resolve_mod_closure", side_effect=automatic
        ), patch.object(
            core, "resolve_exact_mod_closure", side_effect=exact
        ), patch.object(
            core, "resolve_project_selector", side_effect=selector
        ), patch.object(packctl, "run_packwiz"):
            initial = resolution.resolve_pack_migration_plan_at(
                plan, repository_root=self.root, state_root=self.state
            )
        self.assertEqual(initial.state, "resolution-required")
        self.assertEqual(
            {item.source_root.canonical_identity for item in initial.unresolved_roots},
            {f"modrinth:{ROOT_ID}"},
        )
        return plan, automatic, exact, selector

    def retry_exact_root_conflict(
        self,
        plan: pack_migration.PackMigrationPlan,
        automatic: object,
        exact: object,
        selector: object,
        *,
        action: str,
    ) -> core.PackMigrationConflictResolutionResult:
        choice = (
            core.PackMigrationRootResolution(f"modrinth:{ROOT_ID}", "remove")
            if action == "remove"
            else core.PackMigrationRootResolution(
                f"modrinth:{ROOT_ID}", "replace", "curseforge", "123"
            )
        )
        request = core.create_pack_migration_resolution_request(plan, (choice,))
        with patch.object(
            packctl, "init_packwiz_project", side_effect=self.fake_init
        ), patch.object(
            core, "resolve_mod_closure", side_effect=automatic
        ), patch.object(
            core, "resolve_exact_mod_closure", side_effect=exact
        ), patch.object(
            core, "resolve_project_selector", side_effect=selector
        ), patch.object(packctl, "run_packwiz"):
            return resolution.resolve_pack_migration_conflicts_at(
                plan,
                request,
                repository_root=self.root,
                state_root=self.state,
            )

    def test_malformed_source_override_fails_snapshot_validation(self) -> None:
        ensure_mod_version_overrides_ignored(self.source)
        (self.source / ".huroshiki-version-overrides.json").write_text(
            '{"schema": 1, "mods":', encoding="utf-8"
        )

        snapshot = pack_migration.snapshot_pack_migration_source_at(
            "pack:demo", self.pack, self.root
        )

        self.assertTrue(
            any("version override authority is invalid" in item.lower()
                for item in snapshot.validation_errors)
        )

    def test_stale_source_override_fails_snapshot_validation(self) -> None:
        self.set_overrides(
            ModVersionOverride("modrinth", ROOT_ID, "ver99999", True, "Keep")
        )

        snapshot = pack_migration.snapshot_pack_migration_source_at(
            "pack:demo", self.pack, self.root
        )

        self.assertTrue(
            any("does not match installed metadata" in item
                for item in snapshot.validation_errors)
        )

    def test_exact_root_intent_is_preserved_for_locked_and_unlocked(self) -> None:
        for locked in (True, False):
            with self.subTest(locked=locked):
                self.set_overrides(
                    ModVersionOverride(
                        "modrinth", ROOT_ID, VERSION_1, locked, "User choice"
                    )
                )
                plan = self.plan()

                def exact(
                    selection: core.ExactModArtifactSelection, **_: object
                ) -> core.ResolvedModClosure:
                    self.assertEqual(selection.identity_label, f"modrinth:{ROOT_ID}")
                    self.assertEqual(str(selection.artifact_id), VERSION_1)
                    return core.ResolvedModClosure(
                        selection.identity, (metadata(VERSION_1),)
                    )

                with patch.object(
                    packctl, "init_packwiz_project", side_effect=self.fake_init
                ), patch.object(
                    core, "resolve_exact_mod_closure", side_effect=exact
                ), patch.object(
                    core,
                    "resolve_mod_closure",
                    side_effect=AssertionError("automatic resolution used"),
                ), patch.object(
                    core, "resolve_project_selector", side_effect=self.fake_selector
                ), patch.object(packctl, "run_packwiz"):
                    result = resolution.resolve_pack_migration_plan_at(
                        plan, repository_root=self.root, state_root=self.state
                    )

                self.assertEqual(result.state, "resolved")
                self.assertFalse(result.version_intent_issues)
                self.assertEqual(
                    read_mod_version_overrides(plan.target_staging_root / "source"),
                    (
                        ModVersionOverride(
                            "modrinth",
                            ROOT_ID,
                            VERSION_1,
                            locked,
                            "User choice",
                        ),
                    ),
                )
                pack_migration.discard_pack_migration_plan(plan)

    def test_incompatible_exact_root_is_explicitly_blocked(self) -> None:
        self.set_overrides(
            ModVersionOverride("modrinth", ROOT_ID, VERSION_1, True, "Keep")
        )
        plan = self.plan()
        with patch.object(
            packctl, "init_packwiz_project", side_effect=self.fake_init
        ), patch.object(
            core,
            "resolve_exact_mod_closure",
            side_effect=core.HuroshikiError("artifact is incompatible with Fabric"),
        ), patch.object(
            core, "resolve_project_selector", side_effect=self.fake_selector
        ):
            result = resolution.resolve_pack_migration_plan_at(
                plan, repository_root=self.root, state_root=self.state
            )

        self.assertEqual(result.state, "resolution-required")
        self.assertEqual(result.unresolved_roots[0].reason_code, "version-intent-blocked")
        self.assertEqual(result.version_intent_issues[0].identity, f"modrinth:{ROOT_ID}")
        self.assertEqual(result.version_intent_issues[0].requested_artifact_id, VERSION_1)
        pack_migration.discard_pack_migration_plan(plan)

    def test_exact_intent_survives_atomic_target_publication(self) -> None:
        expected = ModVersionOverride(
            "modrinth", ROOT_ID, VERSION_1, True, "Published choice"
        )
        self.set_overrides(expected)
        source_before = pack_migration.scan_pack_migration_source(
            self.pack, checkpoint=lambda: None
        ).snapshot_digest
        plan = self.plan()

        def exact(
            selection: core.ExactModArtifactSelection, **_: object
        ) -> core.ResolvedModClosure:
            return core.ResolvedModClosure(
                selection.identity, (metadata(VERSION_1),)
            )

        with patch.object(
            packctl, "init_packwiz_project", side_effect=self.fake_init
        ), patch.object(
            core, "resolve_exact_mod_closure", side_effect=exact
        ), patch.object(
            core, "resolve_project_selector", side_effect=self.fake_selector
        ), patch.object(packctl, "run_packwiz"):
            result = resolution.resolve_pack_migration_plan_at(
                plan, repository_root=self.root, state_root=self.state
            )

        publication = pack_migration.prepare_pack_migration_publication(
            plan,
            result,
            acknowledged_warning_codes=tuple(
                warning.code
                for warning in plan.warnings
                if warning.acknowledgement_required
            ),
        )
        pack_migration.apply_pack_migration_publication(publication)

        published_source = self.packs / "next" / "source"
        self.assertEqual(read_mod_version_overrides(published_source), (expected,))
        self.assertIn(
            "/.huroshiki-version-overrides.json",
            (published_source / ".packwizignore").read_text(encoding="utf-8").splitlines(),
        )
        self.assertEqual(
            pack_migration.scan_pack_migration_source(
                self.pack, checkpoint=lambda: None
            ).snapshot_digest,
            source_before,
        )

    def test_exact_dependency_intent_rebuilds_owner_closure_and_is_preserved(self) -> None:
        (self.source / "mods" / "dependency.pw.toml").write_bytes(
            metadata(VERSION_1, dependency=True).contents
        )
        self.set_overrides(
            ModVersionOverride(
                "modrinth", DEPENDENCY_ID, VERSION_1, False, "Compatibility"
            )
        )
        plan = self.plan()
        exact_calls: list[tuple[str, tuple[str, ...]]] = []

        def automatic(**_: object) -> core.ResolvedModClosure:
            return core.ResolvedModClosure(
                ("modrinth", ROOT_ID),
                (metadata(VERSION_2), metadata(VERSION_2, dependency=True)),
            )

        def exact(
            selection: core.ExactModArtifactSelection,
            **kwargs: object,
        ) -> core.ResolvedModClosure:
            preseeds = tuple(kwargs.get("preseed_selections", ()))
            exact_calls.append(
                (selection.identity_label, tuple(item.identity_label for item in preseeds))
            )
            self.assertEqual(str(selection.artifact_id), VERSION_2)
            self.assertEqual(str(preseeds[0].artifact_id), VERSION_1)
            return core.ResolvedModClosure(
                selection.identity,
                (metadata(VERSION_2), metadata(VERSION_1, dependency=True)),
            )

        with patch.object(
            packctl, "init_packwiz_project", side_effect=self.fake_init
        ), patch.object(
            core, "resolve_mod_closure", side_effect=automatic
        ), patch.object(
            core, "resolve_exact_mod_closure", side_effect=exact
        ), patch.object(
            core, "resolve_project_selector", side_effect=self.fake_selector
        ), patch.object(packctl, "run_packwiz"):
            result = resolution.resolve_pack_migration_plan_at(
                plan, repository_root=self.root, state_root=self.state
            )

        self.assertEqual(result.state, "resolved")
        self.assertEqual(
            exact_calls,
            [(f"modrinth:{ROOT_ID}", (f"modrinth:{DEPENDENCY_ID}",))],
        )
        dependency = next(
            item
            for item in result.dependency_delta.unchanged + tuple(
                new for _, new in result.dependency_delta.updated
            )
            if item.canonical_identity == f"modrinth:{DEPENDENCY_ID}"
        )
        self.assertEqual(dependency.file_id, VERSION_1)
        self.assertEqual(
            read_mod_version_overrides(plan.target_staging_root / "source")[0].reason,
            "Compatibility",
        )
        pack_migration.discard_pack_migration_plan(plan)

    def test_incompatible_dependency_intent_blocks_without_root_replacement(self) -> None:
        (self.source / "mods" / "dependency.pw.toml").write_bytes(
            metadata(VERSION_1, dependency=True).contents
        )
        self.set_overrides(
            ModVersionOverride(
                "modrinth", DEPENDENCY_ID, VERSION_1, True, "Required"
            )
        )
        plan = self.plan()

        def automatic(**_: object) -> core.ResolvedModClosure:
            return core.ResolvedModClosure(
                ("modrinth", ROOT_ID),
                (metadata(VERSION_2), metadata(VERSION_2, dependency=True)),
            )

        with patch.object(
            packctl, "init_packwiz_project", side_effect=self.fake_init
        ), patch.object(
            core, "resolve_mod_closure", side_effect=automatic
        ), patch.object(
            core,
            "resolve_exact_mod_closure",
            side_effect=core.HuroshikiError("dependency artifact is incompatible"),
        ), patch.object(
            core, "resolve_project_selector", side_effect=self.fake_selector
        ):
            result = resolution.resolve_pack_migration_plan_at(
                plan, repository_root=self.root, state_root=self.state
            )

        self.assertEqual(result.state, "resolution-required")
        self.assertEqual(
            result.version_intent_issues[0].identity,
            f"modrinth:{DEPENDENCY_ID}",
        )
        self.assertFalse(result.unresolved_roots[0].replacement_supported)
        with self.assertRaisesRegex(
            core.PackMigrationConflictResolutionError,
            "Dependency version intent",
        ):
            core.create_pack_migration_resolution_request(
                plan,
                (
                    core.PackMigrationRootResolution(
                        result.unresolved_roots[0].source_root.canonical_identity,
                        "remove",
                    ),
                ),
            )
        pack_migration.discard_pack_migration_plan(plan)

    def test_removed_exact_root_intent_remains_authoritative_as_dependency(self) -> None:
        for locked in (True, False):
            with self.subTest(locked=locked):
                plan, automatic, exact, selector = self.exact_root_conflict(
                    locked=locked,
                    original_is_transitive=True,
                )
                result = self.retry_exact_root_conflict(
                    plan,
                    automatic,
                    exact,
                    selector,
                    action="remove",
                )

                self.assertEqual(result.state, "resolved")
                self.assertEqual(
                    {
                        item.source_root.canonical_identity
                        for item in result.resolution_plan.resolved_roots
                    },
                    {f"modrinth:{OTHER_ROOT_ID}"},
                )
                self.assertEqual(
                    read_mod_version_overrides(plan.target_staging_root / "source"),
                    (
                        ModVersionOverride(
                            "modrinth",
                            ROOT_ID,
                            VERSION_1,
                            locked,
                            "Original exact choice",
                        ),
                    ),
                )
                target_entries = {
                    item.canonical_identity: item
                    for item in result.resolution_plan.dependency_delta.added
                    + tuple(
                        new
                        for _, new in result.resolution_plan.dependency_delta.updated
                    )
                    + result.resolution_plan.dependency_delta.unchanged
                }
                self.assertEqual(
                    target_entries[f"modrinth:{ROOT_ID}"].file_id,
                    VERSION_1,
                )
                self.assertFalse(target_entries[f"modrinth:{ROOT_ID}"].root)
                pack_migration.discard_pack_migration_plan(plan)

    def test_replaced_exact_root_intent_is_not_transferred_and_remains_dependency(self) -> None:
        plan, automatic, exact, selector = self.exact_root_conflict(
            locked=True,
            original_is_transitive=True,
        )
        result = self.retry_exact_root_conflict(
            plan,
            automatic,
            exact,
            selector,
            action="replace",
        )

        self.assertEqual(result.state, "resolved")
        self.assertEqual(
            {
                item.source_root.canonical_identity
                for item in result.resolution_plan.resolved_roots
            },
            {f"modrinth:{OTHER_ROOT_ID}", "curseforge:123"},
        )
        overrides = read_mod_version_overrides(plan.target_staging_root / "source")
        self.assertEqual(
            overrides,
            (
                ModVersionOverride(
                    "modrinth",
                    ROOT_ID,
                    VERSION_1,
                    True,
                    "Original exact choice",
                ),
            ),
        )
        self.assertNotIn("curseforge:123", {item.canonical_identity for item in overrides})
        pack_migration.discard_pack_migration_plan(plan)

    def test_retired_exact_root_override_is_omitted_only_when_graph_absent(self) -> None:
        for action in ("remove", "replace"):
            with self.subTest(action=action):
                plan, automatic, exact, selector = self.exact_root_conflict(
                    locked=True,
                    original_is_transitive=False,
                )
                result = self.retry_exact_root_conflict(
                    plan,
                    automatic,
                    exact,
                    selector,
                    action=action,
                )

                self.assertEqual(result.state, "resolved")
                self.assertEqual(
                    read_mod_version_overrides(plan.target_staging_root / "source"),
                    (),
                )
                self.assertNotIn(
                    f"modrinth:{ROOT_ID}",
                    {
                        item.canonical_identity
                        for item in result.resolution_plan.dependency_delta.added
                        + tuple(
                            new
                            for _, new in result.resolution_plan.dependency_delta.updated
                        )
                        + result.resolution_plan.dependency_delta.unchanged
                    },
                )
                pack_migration.discard_pack_migration_plan(plan)

    def test_retired_exact_root_incompatible_as_dependency_cannot_be_bypassed(self) -> None:
        plan, automatic, exact, selector = self.exact_root_conflict(
            locked=True,
            original_is_transitive=True,
            incompatible_on_retry=True,
        )
        result = self.retry_exact_root_conflict(
            plan,
            automatic,
            exact,
            selector,
            action="remove",
        )

        self.assertEqual(result.state, "resolution-required")
        issue = result.resolution_plan.version_intent_issues[0]
        self.assertEqual(issue.identity, f"modrinth:{ROOT_ID}")
        self.assertEqual(issue.owner_identity, f"modrinth:{OTHER_ROOT_ID}")
        self.assertEqual(issue.reason_code, "version-intent-blocked")
        self.assertEqual(
            {
                item.source_root.canonical_identity
                for item in result.remaining_unresolved
            },
            {f"modrinth:{OTHER_ROOT_ID}"},
        )
        with self.assertRaisesRegex(
            core.PackMigrationConflictResolutionError,
            "Dependency version intent",
        ):
            core.create_pack_migration_resolution_request(
                plan,
                (
                    core.PackMigrationRootResolution(
                        f"modrinth:{OTHER_ROOT_ID}", "remove"
                    ),
                ),
            )
        pack_migration.discard_pack_migration_plan(plan)

    def test_dependency_constraints_expand_to_a_stable_complete_graph(self) -> None:
        for item in (
            metadata(VERSION_1, dependency=True),
            dependency_metadata(SECOND_DEPENDENCY_ID, VERSION_1),
        ):
            (self.source / item.relative_path).write_bytes(item.contents)
        self.set_overrides(
            ModVersionOverride(
                "modrinth", DEPENDENCY_ID, VERSION_1, True, "First constraint"
            ),
            ModVersionOverride(
                "modrinth",
                SECOND_DEPENDENCY_ID,
                VERSION_1,
                False,
                "Introduced constraint",
            ),
        )
        plan = self.plan()
        preseed_history: list[tuple[str, ...]] = []

        def automatic(**_: object) -> core.ResolvedModClosure:
            return core.ResolvedModClosure(
                ("modrinth", ROOT_ID),
                (metadata(VERSION_2), metadata(VERSION_2, dependency=True)),
            )

        def exact(
            selection: core.ExactModArtifactSelection,
            **kwargs: object,
        ) -> core.ResolvedModClosure:
            preseeds = tuple(kwargs.get("preseed_selections", ()))
            identities = tuple(item.identity_label for item in preseeds)
            preseed_history.append(identities)
            members = [metadata(VERSION_2), metadata(VERSION_1, dependency=True)]
            members.append(
                dependency_metadata(
                    SECOND_DEPENDENCY_ID,
                    VERSION_1 if len(preseeds) == 2 else VERSION_2,
                )
            )
            return core.ResolvedModClosure(selection.identity, tuple(members))

        with patch.object(
            packctl, "init_packwiz_project", side_effect=self.fake_init
        ), patch.object(
            core, "resolve_mod_closure", side_effect=automatic
        ), patch.object(
            core, "resolve_exact_mod_closure", side_effect=exact
        ), patch.object(
            core, "resolve_project_selector", side_effect=self.fake_selector
        ), patch.object(packctl, "run_packwiz"):
            result = resolution.resolve_pack_migration_plan_at(
                plan, repository_root=self.root, state_root=self.state
            )

        self.assertEqual(result.state, "resolved")
        self.assertEqual(
            preseed_history,
            [
                (f"modrinth:{DEPENDENCY_ID}",),
                (
                    f"modrinth:{SECOND_DEPENDENCY_ID}",
                    f"modrinth:{DEPENDENCY_ID}",
                ),
            ],
        )
        self.assertEqual(
            {item.canonical_identity for item in result.version_intent_facts.overrides},
            {
                f"modrinth:{DEPENDENCY_ID}",
                f"modrinth:{SECOND_DEPENDENCY_ID}",
            },
        )
        pack_migration.discard_pack_migration_plan(plan)

    def test_post_constraint_collisions_are_only_unresolved_with_diagnostics(self) -> None:
        (self.source / "mods" / "dependency.pw.toml").write_bytes(
            metadata(VERSION_1, dependency=True).contents
        )
        self.set_overrides(
            ModVersionOverride(
                "modrinth", DEPENDENCY_ID, VERSION_1, True, "Collision constraint"
            )
        )

        def automatic(**_: object) -> core.ResolvedModClosure:
            return core.ResolvedModClosure(
                ("modrinth", ROOT_ID),
                (metadata(VERSION_2), metadata(VERSION_2, dependency=True)),
            )

        def exact(
            selection: core.ExactModArtifactSelection,
            **_: object,
        ) -> core.ResolvedModClosure:
            return core.ResolvedModClosure(
                selection.identity,
                (metadata(VERSION_2), metadata(VERSION_1, dependency=True)),
            )

        cases = (
            ("path", "metadata path collision", "path_collisions"),
            ("filename", "JAR filename collision", "filename_collisions"),
            ("identity", "provider identity mismatch", "identity_changes"),
        )
        for label, message, diagnostic_field in cases:
            with self.subTest(collision=label):
                plan = self.plan()
                with patch.object(
                    packctl, "init_packwiz_project", side_effect=self.fake_init
                ), patch.object(
                    core, "resolve_mod_closure", side_effect=automatic
                ), patch.object(
                    core, "resolve_exact_mod_closure", side_effect=exact
                ), patch.object(
                    core, "resolve_project_selector", side_effect=self.fake_selector
                ), patch.object(
                    core,
                    "merge_metadata_closure",
                    side_effect=core.HuroshikiError(message),
                ):
                    result = resolution.resolve_pack_migration_plan_at(
                        plan, repository_root=self.root, state_root=self.state
                    )

                resolved_identities = {
                    item.source_root.canonical_identity
                    for item in result.resolved_roots
                }
                unresolved_identities = {
                    item.source_root.canonical_identity
                    for item in result.unresolved_roots
                }
                self.assertEqual(result.state, "resolution-required")
                self.assertEqual(resolved_identities, set())
                self.assertEqual(unresolved_identities, {f"modrinth:{ROOT_ID}"})
                self.assertFalse(resolved_identities & unresolved_identities)
                self.assertTrue(getattr(result, diagnostic_field))
                pack_migration.discard_pack_migration_plan(plan)


if __name__ == "__main__":
    unittest.main()
