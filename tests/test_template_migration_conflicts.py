from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import huroshiki_core as core
import template_migration as migration
import tests.test_template_migration as template_migration_tests
from template_migration_conflicts import (
    TemplateMigrationConflictResolutionError,
    TemplateMigrationRootResolution,
    TemplateMigrationResolutionRequest,
    create_template_migration_resolution_request,
    template_migration_request_digest,
    validate_template_migration_resolution_request,
)


class TemplateMigrationConflictApiTest(unittest.TestCase):
    target = "target"

    def setUp(self) -> None:
        self.roots = (
            migration.TemplateRootIntent(0, "First", "modrinth", "Old00001", "client"),
            migration.TemplateRootIntent(1, "Second", "curseforge", "42", "server"),
        )
        self.old_exact = migration.TemplateExactConstraint("modrinth", "Old00001", "OldFile1", "root")
        self.dependency_exact = migration.TemplateExactConstraint("modrinth", "dep", "dep-file", "dependency")
        self.unresolved = (
            migration.TemplateUnresolvedRoot(0, "Old00001", "modrinth:Old00001", "identity-collision", "collision"),
        )
        result = SimpleNamespace(
            status="resolution-required", resolution_attempt=3, digest="resolution-digest",
            unresolved=self.unresolved, version_intent_facts=("version-fact",),
            version_intent_issues=("version-issue",), collisions=("collision",),
            identity_collisions=(), path_collisions=(), filename_collisions=(),
        )
        self.state = SimpleNamespace(
            resolution=result, effective_roots=self.roots,
            effective_overrides=(self.old_exact, self.dependency_exact),
            consumed_resolution_requests=set(), attempt=3,
        )
        self.plan = SimpleNamespace(
            _state=self.state, source_snapshot_digest="source-digest", plan_digest="plan-digest",
            target=self.target, roots=self.roots,
        )

    def request(self, *choices: TemplateMigrationRootResolution) -> TemplateMigrationResolutionRequest:
        return create_template_migration_resolution_request(self.plan, tuple(choices))

    def test_request_binds_every_field_and_digest(self) -> None:
        request = self.request(TemplateMigrationRootResolution(0, "remove"))
        self.assertEqual(request.plan_identity, id(self.plan))
        self.assertEqual(request.source_snapshot_digest, "source-digest")
        self.assertEqual(request.plan_digest, "plan-digest")
        self.assertEqual(request.target, self.target)
        self.assertEqual(request.resolution_attempt, 3)
        self.assertEqual(request.resolution_digest, "resolution-digest")
        self.assertTrue(request.unresolved_digest)
        self.assertTrue(request.version_intent_digest)
        self.assertTrue(request.collision_digest)
        request_digest = template_migration_request_digest(request)
        self.assertEqual(len(request_digest), 64)
        self.assertEqual(validate_template_migration_resolution_request(self.plan, request).request_digest, request_digest)

    def test_stale_replay_duplicate_unknown_and_resolved_are_rejected(self) -> None:
        request = self.request(TemplateMigrationRootResolution(0, "remove"))
        cases = [
            (replace(request, plan_digest="stale"), "Stale plan digest"),
            (replace(request, resolutions=(TemplateMigrationRootResolution(0, "remove"), TemplateMigrationRootResolution(0, "remove"))), "Duplicate"),
            (replace(request, resolutions=(TemplateMigrationRootResolution(9, "remove"),)), "Unknown"),
            (replace(request, resolutions=()), "At least one"),
        ]
        for candidate, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(TemplateMigrationConflictResolutionError, message):
                validate_template_migration_resolution_request(self.plan, candidate)
        self.state.consumed_resolution_requests.add(template_migration_request_digest(request))
        with self.assertRaisesRegex(TemplateMigrationConflictResolutionError, "already been consumed"):
            validate_template_migration_resolution_request(self.plan, request)
        self.state.consumed_resolution_requests.clear()
        self.state.resolution = SimpleNamespace(**{**vars(self.state.resolution), "status": "resolved"})
        with self.assertRaisesRegex(TemplateMigrationConflictResolutionError, "not awaiting"):
            validate_template_migration_resolution_request(self.plan, request)

    def test_request_allows_nonempty_current_unresolved_subset(self) -> None:
        second = migration.TemplateUnresolvedRoot(
            1, "42", "curseforge:42", "identity-collision", "collision"
        )
        self.state.resolution = SimpleNamespace(**{
            **vars(self.state.resolution), "unresolved": self.unresolved + (second,)
        })
        request = self.request(TemplateMigrationRootResolution(0, "remove"))
        validated = validate_template_migration_resolution_request(self.plan, request)
        self.assertEqual([root.source_index for root in validated.effective_roots], [1])

    def test_remove_preserves_order_sides_and_only_abandons_root_exact(self) -> None:
        validated = validate_template_migration_resolution_request(self.plan, self.request(TemplateMigrationRootResolution(0, "remove")))
        self.assertEqual([(root.name, root.side) for root in validated.effective_roots], [("Second", "server")])
        self.assertEqual(validated.effective_overrides, (self.dependency_exact,))
        self.assertEqual(validated.removed_roots[0].abandoned_root_exact_constraints, (self.old_exact,))

    def test_replace_preserves_selector_position_and_side_before_resolution(self) -> None:
        self.state.effective_overrides = (self.dependency_exact,)
        choice = TemplateMigrationRootResolution(0, "replace", "modrinth", "New00001")
        validated = validate_template_migration_resolution_request(self.plan, self.request(choice))
        replacement = validated.effective_roots[0]
        self.assertEqual((replacement.source_index, replacement.side, replacement.provider, replacement.project_id), (0, "client", "modrinth", "New00001"))
        self.assertEqual(validated.effective_roots[1], self.roots[1])
        self.assertEqual(validated.replaced_roots[0].replacement_selector, "New00001")
        self.assertIsNone(validated.replaced_roots[0].new_identity)

    def test_replace_accepts_legal_modrinth_selectors_and_rejects_malformed(self) -> None:
        self.state.effective_overrides = (self.dependency_exact,)
        for selector in ("project-slug", "https://modrinth.com/mod/example", "New00001"):
            with self.subTest(selector=selector):
                validated = validate_template_migration_resolution_request(
                    self.plan,
                    self.request(TemplateMigrationRootResolution(
                        0, "replace", "modrinth", selector
                    )),
                )
                self.assertEqual(validated.replaced_roots[0].replacement_selector, selector)
        for selector in ("bad selector", "http://modrinth.com/mod/example"):
            with self.subTest(selector=selector), self.assertRaises(
                TemplateMigrationConflictResolutionError
            ):
                self.request(TemplateMigrationRootResolution(
                    0, "replace", "modrinth", selector
                ))

    def test_unsupported_version_blocked_and_old_exact_replacement_are_rejected(self) -> None:
        with self.assertRaisesRegex(TemplateMigrationConflictResolutionError, "Unsupported resolution action"):
            validate_template_migration_resolution_request(self.plan, self.request(TemplateMigrationRootResolution(0, "keep")))
        for unresolved in (
            replace(self.unresolved[0], replacement_supported=False),
            replace(self.unresolved[0], version_issue="blocked"),
        ):
            self.state.resolution = SimpleNamespace(**{**vars(self.state.resolution), "unresolved": (unresolved,)})
            with self.assertRaisesRegex(TemplateMigrationConflictResolutionError, "blocked"):
                self.validate_replace()
        self.state.resolution = SimpleNamespace(**{**vars(self.state.resolution), "unresolved": self.unresolved})
        self.state.effective_overrides = (self.dependency_exact,)
        same_selector = validate_template_migration_resolution_request(
            self.plan,
            self.request(TemplateMigrationRootResolution(
                0, "replace", "modrinth", "Old00001"
            )),
        )
        self.assertIsNone(same_selector.replaced_roots[0].new_identity)
        self.state.effective_overrides = (self.old_exact, self.dependency_exact)
        with self.assertRaisesRegex(TemplateMigrationConflictResolutionError, "cannot transfer"):
            validate_template_migration_resolution_request(self.plan, self.request(TemplateMigrationRootResolution(0, "replace", "curseforge", "99")))

    def validate_replace(self):
        return validate_template_migration_resolution_request(self.plan, self.request(TemplateMigrationRootResolution(0, "replace", "modrinth", "New00001")))


class TemplateMigrationConflictOperationTest(unittest.TestCase):
    def test_replacement_failure_remains_unresolved_and_ownerless_dependency_blocks(self) -> None:
        root = migration.TemplateRootIntent(0, "Root", "modrinth", "Old00001", "both")
        unresolved = migration.TemplateUnresolvedRoot(0, "Old00001", "modrinth:Old00001", "no-compatible-file", "failed")
        result = migration.TemplateResolutionResult(
            "resolution-required", "s", "t", (), (unresolved,), "1.21", "fabric", "1",
            (root,), (), (),
            (migration.TemplateVersionIntentIssue("modrinth", "dep", "missing", "dependency", "version-intent-blocked", "missing", ()),),
            (), (), (), (), (), (), (), (), 2, None, "d",
        )
        state = SimpleNamespace(
            resolution=result, effective_roots=(root,), effective_overrides=(), consumed_resolution_requests=set(),
            removed_roots=(), replaced_roots=(), pending_replacements=(), event=object(), deadline=10,
            result_digest=None, publication_token=None,
        )
        plan = SimpleNamespace(_state=state, source_snapshot_digest="s", plan_digest="p", target="t", roots=(root,))
        request = create_template_migration_resolution_request(plan, (TemplateMigrationRootResolution(0, "replace", "modrinth", "New00001"),))
        with patch.object(migration, "resolve_template_migration_plan_at", return_value=result):
            outcome = migration.resolve_template_migration_conflicts_at(plan, request, cancel_event=state.event, deadline=state.deadline)
        self.assertEqual(outcome.state, "resolution-required")
        self.assertEqual(outcome.resolution.unresolved, (unresolved,))
        self.assertEqual(outcome.resolution.version_intent_issues[0].owner_source_indices, ())
        self.assertEqual(state.effective_overrides, ())

    def test_operational_failure_preserves_previous_authority_and_request(self) -> None:
        root = migration.TemplateRootIntent(0, "Root", "modrinth", "Old00001", "both")
        unresolved = migration.TemplateUnresolvedRoot(
            0, "Old00001", "modrinth:Old00001", "no-compatible-file", "failed"
        )
        result = SimpleNamespace(
            status="resolution-required", resolution_attempt=1, digest="current",
            unresolved=(unresolved,), version_intent_facts=(), version_intent_issues=(),
            collisions=(), identity_collisions=(), path_collisions=(),
            filename_collisions=(), ordered_root_facts=(), ordered_roots=(root,),
        )
        event = object()
        state = SimpleNamespace(
            resolution=result, effective_roots=(root,), effective_overrides=(),
            consumed_resolution_requests=set(), removed_roots=(), replaced_roots=(), pending_replacements=(),
            event=event, deadline=10, result_digest=None, publication_token=None,
        )
        plan = SimpleNamespace(
            _state=state, source_snapshot_digest="s", plan_digest="p",
            target="t", roots=(root,),
        )
        request = create_template_migration_resolution_request(
            plan, (TemplateMigrationRootResolution(0, "remove"),)
        )
        with patch.object(
            migration, "resolve_template_migration_plan_at",
            side_effect=migration.TemplateMigrationOperationError("cancelled"),
        ):
            with self.assertRaises(migration.TemplateMigrationOperationError):
                migration.resolve_template_migration_conflicts_at(
                    plan, request, cancel_event=event, deadline=10
                )
        self.assertIs(state.resolution, result)
        self.assertEqual(state.effective_roots, (root,))
        self.assertEqual(state.removed_roots, ())
        self.assertEqual(state.consumed_resolution_requests, set())
        validate_template_migration_resolution_request(plan, request)


class TemplateMigrationConflictIntegrationTest(unittest.TestCase):
    setUp = template_migration_tests.TemplateMigrationCoreTest.setUp
    tearDown = template_migration_tests.TemplateMigrationCoreTest.tearDown
    _write_template = template_migration_tests.TemplateMigrationCoreTest._write_template
    target = template_migration_tests.TemplateMigrationCoreTest.target
    plan = template_migration_tests.TemplateMigrationCoreTest.plan
    metadata = staticmethod(template_migration_tests.TemplateMigrationCoreTest.metadata)
    resolver_patches = template_migration_tests.TemplateMigrationCoreTest.resolver_patches

    def _closure(self, project: str, artifact: str, path: str):
        return SimpleNamespace(
            root_identity=("modrinth", project),
            metadata=(self.metadata("modrinth", project, artifact, path),),
        )

    def _resolve_partial_collision(self, choice: TemplateMigrationRootResolution):
        self._write_template(self.source, mods=[
            {"name": "A", "provider": "modrinth", "project_id": "RootA001", "side": "client"},
            {"name": "B", "provider": "modrinth", "project_id": "RootB001", "side": "server"},
            {"name": "C", "provider": "modrinth", "project_id": "RootC001", "side": "both"},
        ])
        left = self.metadata("modrinth", "DepA0001", "DepAV001", "left")
        right = self.metadata("curseforge", "999", "9", "right")
        closures = {
            "RootA001": SimpleNamespace(root_identity=("modrinth", "RootA001"), metadata=(
                self.metadata("modrinth", "RootA001", "FileA001", "a"), left,
            )),
            "RootB001": self._closure("RootB001", "FileB001", "b"),
            "RootC001": SimpleNamespace(root_identity=("modrinth", "RootC001"), metadata=(
                self.metadata("modrinth", "RootC001", "FileC001", "c"), right,
            )),
            "NewA0001": self._closure("NewA0001", "NewFile1", "new-a"),
        }
        left_fact = migration._metadata_identity(left)
        right_fact = migration._metadata_identity(right)
        evidence = core.MetadataCollisionEvidence(
            "identity-collision", left.identity, right.identity,
            left.relative_path, right.relative_path,
            left_fact.filename, right_fact.filename,
        )
        context, _ = self.resolver_patches(closure=closures["RootA001"])
        merge_calls = 0
        def merge(*_args, **_kwargs):
            nonlocal merge_calls
            merge_calls += 1
            if merge_calls == 3:
                raise core.MetadataClosureCollisionError("A and C collide", evidence)
        with context, \
             patch("huroshiki_core.resolve_mod_closure", side_effect=lambda **kwargs: closures[kwargs["canonical_project_id"]]), \
             patch("huroshiki_core.merge_metadata_closure", side_effect=merge):
            plan = self.plan(); initial = migration.resolve_template_migration_plan_at(plan)
            request = create_template_migration_resolution_request(plan, (choice,))
            outcome = migration.resolve_template_migration_conflicts_at(
                plan, request, cancel_event=plan.cancel_event, deadline=plan.deadline
            )
        self.assertEqual([item.source_index for item in initial.unresolved], [0, 2])
        return plan, outcome

    def test_remove_rebuilds_ordered_root_only_manifest_on_same_plan(self) -> None:
        self._write_template(self.source, mods=[
            {"name": "Old", "provider": "modrinth", "project_id": "Old00001", "side": "client"},
            {"name": "Keep", "provider": "modrinth", "project_id": "Keep0001", "side": "server"},
        ])
        old = self._closure("Old00001", "OldFile1", "old")
        keep = self._closure("Keep0001", "KeepFile", "keep")
        context, _ = self.resolver_patches(closure=old)
        def resolve(**kwargs):
            project = kwargs["canonical_project_id"]
            if project == "Old00001" and kwargs["minecraft"] == "1.21.4":
                raise RuntimeError("no compatible target")
            return old if project == "Old00001" else keep
        with context, patch("huroshiki_core.resolve_mod_closure", side_effect=resolve):
            plan = self.plan(); initial = migration.resolve_template_migration_plan_at(plan)
            request = create_template_migration_resolution_request(
                plan, (TemplateMigrationRootResolution(0, "remove"),)
            )
            outcome = migration.resolve_template_migration_conflicts_at(
                plan, request, cancel_event=plan.cancel_event, deadline=plan.deadline
            )
        self.assertEqual(initial.status, "resolution-required")
        self.assertEqual(outcome.state, "resolved")
        self.assertEqual([(root.source_index, root.side) for root in outcome.resolution.resolved], [(1, "server")])
        manifest = (plan._state.staging / "template.yaml").read_text()
        self.assertNotIn("Old00001", manifest)
        self.assertIn("Keep0001", manifest)
        self.assertEqual(outcome.resolution.removed_roots[0].source_root.source_index, 0)
        migration.discard_template_migration_plan(plan, deadline=time.monotonic() + 30)

    def test_replace_canonical_root_preserves_position_side_and_is_updated(self) -> None:
        self._write_template(self.source, mods=[
            {"name": "Old", "provider": "modrinth", "project_id": "Old00001", "side": "client"},
        ])
        old = self._closure("Old00001", "OldFile1", "old")
        new = self._closure("New00001", "NewFile1", "new")
        context, _ = self.resolver_patches(closure=old)
        def resolve(**kwargs):
            project = kwargs["canonical_project_id"]
            if project == "Old00001" and kwargs["minecraft"] == "1.21.4":
                raise RuntimeError("no compatible target")
            return old if project == "Old00001" else new
        with context, patch("huroshiki_core.resolve_mod_closure", side_effect=resolve):
            plan = self.plan(); migration.resolve_template_migration_plan_at(plan)
            request = create_template_migration_resolution_request(plan, (
                TemplateMigrationRootResolution(0, "replace", "modrinth", "New00001"),
            ))
            outcome = migration.resolve_template_migration_conflicts_at(
                plan, request, cancel_event=plan.cancel_event, deadline=plan.deadline
            )
        root = outcome.resolution.ordered_roots[0]
        self.assertEqual((root.source_index, root.project_id, root.side), (0, "New00001", "client"))
        self.assertEqual(outcome.resolution.resolved[0].classification, "updated")
        self.assertEqual(outcome.resolution.replaced_roots[0].new_identity, "modrinth:New00001")
        manifest = (plan._state.staging / "template.yaml").read_text()
        self.assertIn("project_id: New00001", manifest)
        self.assertNotIn("project_id: Old00001", manifest)
        migration.discard_template_migration_plan(plan, deadline=time.monotonic() + 30)

    def test_replacement_uses_old_source_baseline_and_target_only_replacement(self) -> None:
        self._write_template(self.source, mods=[
            {"name": "Old", "provider": "modrinth", "project_id": "Old00001", "side": "client"},
        ])
        old = self._closure("Old00001", "OldFile1", "old")
        new = self._closure("New00001", "NewFile1", "new")
        context, _ = self.resolver_patches(closure=old)
        def canonical(_provider, selector, **_kwargs):
            project = "New00001" if selector == "new-project" else "Old00001"
            return SimpleNamespace(provider="modrinth", canonical_project_id=project)
        resolver_calls = []
        def resolve(**kwargs):
            resolver_calls.append((kwargs["canonical_project_id"], kwargs["minecraft"], kwargs["loader"]))
            project = kwargs["canonical_project_id"]
            if project == "New00001" and kwargs["minecraft"] == "1.21.1":
                raise AssertionError("replacement must not resolve against source configuration")
            if project == "Old00001" and kwargs["minecraft"] == "1.21.4":
                raise RuntimeError("no compatible target")
            return old if project == "Old00001" else new
        with context, \
             patch("huroshiki_core.resolve_project_selector", side_effect=canonical) as lookup, \
             patch("huroshiki_core.resolve_mod_closure", side_effect=resolve):
            plan = self.plan(self.target(loader="fabric", reference="0.16.0")); migration.resolve_template_migration_plan_at(plan)
            request = create_template_migration_resolution_request(plan, (
                TemplateMigrationRootResolution(0, "replace", "modrinth", "new-project"),
            ))
            outcome = migration.resolve_template_migration_conflicts_at(
                plan, request, cancel_event=plan.cancel_event, deadline=plan.deadline
            )
        fact = outcome.resolution.replaced_roots[0]
        self.assertEqual(fact.old_identity, "modrinth:Old00001")
        self.assertEqual(fact.replacement_selector, "new-project")
        self.assertEqual(fact.new_identity, "modrinth:New00001")
        self.assertEqual((fact.replacement_root.source_index, fact.replacement_root.side), (0, "client"))
        self.assertEqual(outcome.resolution.ordered_roots[0].project_id, "New00001")
        root_fact = outcome.resolution.ordered_root_facts[0]
        self.assertEqual(root_fact.source_canonical_identity, "modrinth:Old00001")
        self.assertEqual(root_fact.target_canonical_identity, "modrinth:New00001")
        self.assertEqual(root_fact.classification, "updated")
        self.assertNotIn(("New00001", "1.21.1", "neoforge"), resolver_calls)
        self.assertIn(("Old00001", "1.21.1", "neoforge"), resolver_calls)
        self.assertIn(("New00001", "1.21.4", "fabric"), resolver_calls)
        manifest = (plan._state.staging / "template.yaml").read_text()
        self.assertIn("project_id: New00001", manifest)
        slug_call = next(call for call in lookup.call_args_list if call.args[1] == "new-project")
        self.assertIs(slug_call.kwargs["cancel_event"], plan.cancel_event)
        self.assertEqual(slug_call.kwargs["deadline"], plan.deadline)
        migration.discard_template_migration_plan(plan, deadline=time.monotonic() + 30)

    def test_modrinth_slug_canonicalizing_to_old_identity_is_rejected(self) -> None:
        self._write_template(self.source, mods=[
            {"name": "Old", "provider": "modrinth", "project_id": "Old00001", "side": "both"},
        ])
        old = self._closure("Old00001", "OldFile1", "old")
        context, _ = self.resolver_patches(closure=old)
        def resolve(**kwargs):
            if kwargs["minecraft"] == "1.21.4":
                raise RuntimeError("no compatible target")
            return old
        with context, \
             patch("huroshiki_core.resolve_project_selector", return_value=SimpleNamespace(
                 provider="modrinth", canonical_project_id="Old00001"
             )), \
             patch("huroshiki_core.resolve_mod_closure", side_effect=resolve):
            plan = self.plan(); previous = migration.resolve_template_migration_plan_at(plan)
            request = create_template_migration_resolution_request(plan, (
                TemplateMigrationRootResolution(0, "replace", "modrinth", "same-project"),
            ))
            with self.assertRaisesRegex(
                TemplateMigrationConflictResolutionError, "original canonical identity"
            ):
                migration.resolve_template_migration_conflicts_at(
                    plan, request, cancel_event=plan.cancel_event, deadline=plan.deadline
                )
        self.assertIs(plan.resolution, previous)
        self.assertFalse((plan._state.staging / "template.yaml").exists())
        migration.discard_template_migration_plan(plan, deadline=time.monotonic() + 30)

    def test_modrinth_slug_canonicalizing_to_retained_root_is_typed_conflict(self) -> None:
        self._write_template(self.source, mods=[
            {"name": "Old", "provider": "modrinth", "project_id": "Old00001", "side": "client"},
            {"name": "Keep", "provider": "modrinth", "project_id": "Keep0001", "side": "server"},
        ])
        old = self._closure("Old00001", "OldFile1", "old")
        keep = self._closure("Keep0001", "KeepFile", "keep")
        context, _ = self.resolver_patches(closure=old)
        def canonical(_provider, selector, **_kwargs):
            project = "Keep0001" if selector in {"kept-project", "Keep0001"} else "Old00001"
            return SimpleNamespace(provider="modrinth", canonical_project_id=project)
        def resolve(**kwargs):
            project = kwargs["canonical_project_id"]
            if project == "Old00001" and kwargs["minecraft"] == "1.21.4":
                raise RuntimeError("no compatible target")
            return old if project == "Old00001" else keep
        with context, \
             patch("huroshiki_core.resolve_project_selector", side_effect=canonical), \
             patch("huroshiki_core.resolve_mod_closure", side_effect=resolve):
            plan = self.plan(); migration.resolve_template_migration_plan_at(plan)
            request = create_template_migration_resolution_request(plan, (
                TemplateMigrationRootResolution(0, "replace", "modrinth", "kept-project"),
            ))
            outcome = migration.resolve_template_migration_conflicts_at(
                plan, request, cancel_event=plan.cancel_event, deadline=plan.deadline
            )
        self.assertEqual(outcome.state, "resolution-required")
        self.assertEqual([item.source_index for item in outcome.resolution.unresolved], [0, 1])
        self.assertEqual(outcome.resolution.identity_collisions[0].canonical_identities, ("modrinth:Keep0001",))
        self.assertEqual(outcome.resolution.replaced_roots[0].new_identity, "modrinth:Keep0001")
        self.assertFalse((plan._state.staging / "template.yaml").exists())
        migration.discard_template_migration_plan(plan, deadline=time.monotonic() + 30)

    def test_partial_remove_recomputes_collision_participant_as_resolved(self) -> None:
        plan, outcome = self._resolve_partial_collision(
            TemplateMigrationRootResolution(0, "remove")
        )
        self.assertEqual(outcome.state, "resolved")
        self.assertEqual(
            [(item.source_index, item.side) for item in outcome.resolution.resolved],
            [(1, "server"), (2, "both")],
        )
        self.assertEqual(outcome.resolution.unresolved, ())
        migration.discard_template_migration_plan(plan, deadline=time.monotonic() + 30)

    def test_partial_replace_recomputes_collision_participant_as_resolved(self) -> None:
        plan, outcome = self._resolve_partial_collision(
            TemplateMigrationRootResolution(
                0, "replace", "modrinth", "NewA0001"
            )
        )
        self.assertEqual(outcome.state, "resolved")
        self.assertEqual(
            [item.source_index for item in outcome.resolution.resolved], [0, 1, 2]
        )
        self.assertEqual(outcome.resolution.replaced_roots[0].new_identity, "modrinth:NewA0001")
        migration.discard_template_migration_plan(plan, deadline=time.monotonic() + 30)

    def test_partial_remove_leaves_independent_unresolved_root(self) -> None:
        self._write_template(self.source, mods=[
            {"name": "A", "provider": "modrinth", "project_id": "RootA001", "side": "client"},
            {"name": "B", "provider": "modrinth", "project_id": "RootB001", "side": "server"},
        ])
        closures = {
            project: self._closure(project, artifact, project.lower())
            for project, artifact in (("RootA001", "FileA001"), ("RootB001", "FileB001"))
        }
        context, _ = self.resolver_patches(closure=closures["RootA001"])
        def resolve(**kwargs):
            if kwargs["minecraft"] == "1.21.4":
                raise RuntimeError(f"{kwargs['canonical_project_id']} unavailable")
            return closures[kwargs["canonical_project_id"]]
        with context, patch("huroshiki_core.resolve_mod_closure", side_effect=resolve):
            plan = self.plan(); initial = migration.resolve_template_migration_plan_at(plan)
            request = create_template_migration_resolution_request(
                plan, (TemplateMigrationRootResolution(0, "remove"),)
            )
            outcome = migration.resolve_template_migration_conflicts_at(
                plan, request, cancel_event=plan.cancel_event, deadline=plan.deadline
            )
        self.assertEqual([item.source_index for item in initial.unresolved], [0, 1])
        self.assertEqual(outcome.state, "resolution-required")
        self.assertEqual([item.source_index for item in outcome.resolution.unresolved], [1])
        self.assertEqual(outcome.resolution.ordered_roots[0].source_index, 1)
        migration.discard_template_migration_plan(plan, deadline=time.monotonic() + 30)


if __name__ == "__main__":
    unittest.main()
