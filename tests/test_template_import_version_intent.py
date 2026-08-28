"""Focused acceptance coverage for Template version intent (issue #157).

The broad Template-import and exact-version suites intentionally remain separate.
This module exercises the seams where their contracts meet, using the established
fixtures rather than manufacturing a second transaction implementation.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
from io import StringIO
import json
from pathlib import Path
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from mod_version_overrides import (
    ModVersionOverride,
    ensure_mod_version_overrides_ignored,
    parse_mod_version_overrides,
    read_mod_version_overrides,
    serialize_mod_version_overrides,
    set_mod_version_override,
)
from pack_migration_roots import PackRootRecord, read_pack_root_manifest
from template_import import (
    ImportCandidateVerification,
    ImportConflictResolution,
    TemplateCompatibility,
    TemplateVersionConstraint,
    build_template_import_plan,
    merge_template_import_candidates,
    resolve_template_import_plan,
    template_candidate,
)
from template_merge import TemplateMergeError

from tests import test_template_import_core as import_core
from tests import test_mod_version_selection as exact_selection


class TemplateImportVersionIntentTest(unittest.TestCase):
    """End-to-end invariants plus the small pure planning contracts."""

    def core_fixture(self) -> import_core.TemplateImportCoreTest:
        # Reuse the repository's transaction fixture without inheriting its test
        # methods (which would make unittest run the large core suite twice).
        fixture = import_core.TemplateImportCoreTest(
            "test_dry_run_classifies_root_dependency_and_preserves_real_pack"
        )
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def exact_fixture(self) -> exact_selection.ExactModVersionSelectionTest:
        fixture = exact_selection.ExactModVersionSelectionTest(
            "test_exact_modrinth_selection_previews_applies_and_unions_dependency_side"
        )
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        pack = fixture.packs / "demo"
        (pack / "pack.yaml").write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n",
            encoding="utf-8",
        )
        (pack / "content" / "common").mkdir(parents=True)
        template = fixture.root / "templates" / "base"
        template.mkdir(parents=True)
        return fixture

    def write_exact_template(
        self,
        fixture: exact_selection.ExactModVersionSelectionTest,
        *,
        provider: str,
        project_id: str,
        artifact_id: str | None,
        dependency: tuple[str, str, str] | None = None,
    ) -> None:
        lines = [
            "id: base",
            "display_name: Base",
            "enabled: true",
            "minecraft: 1.21.1",
            "loader: fabric",
            "reference_loader_version: 0.16.0",
            "mods:",
            "  - name: Root",
            f"    provider: {provider}",
            f'    project_id: "{project_id}"',
            "    side: both",
        ]
        overrides: list[tuple[str, str, str, str]] = []
        if artifact_id is not None:
            overrides.append((provider, project_id, artifact_id, "root"))
        if dependency is not None:
            overrides.append((*dependency, "dependency"))
        if overrides:
            lines.append("mod_version_overrides:")
            for item_provider, item_project, item_artifact, scope in overrides:
                lines.extend(
                    [
                        f"  - provider: {item_provider}",
                        f'    project_id: "{item_project}"',
                        f'    artifact_id: "{item_artifact}"',
                        f"    scope: {scope}",
                    ]
                )
        (fixture.root / "templates" / "base" / "template.yaml").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def run_exact_import(
        self, fixture: exact_selection.ExactModVersionSelectionTest
    ) -> core.TemplateImportOperation:
        session = core.TemplateImportSession.create(fixture.key, ["base"])
        resolved = resolve_template_import_plan(session.plan)
        operation = core.TemplateImportOperation(session, resolved)
        operation.run()
        return operation

    def run_same_identity_slug_pack_intent(
        self,
        *,
        locked: bool,
        reason: str | None,
    ) -> tuple[
        exact_selection.ExactModVersionSelectionTest,
        core.TemplateImportSession,
        core.TemplateImportOperation,
        str,
        str,
    ]:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        self.seed_installed_modrinth_root(
            fixture,
            artifact_id=artifact_id,
            locked=locked,
            reason=reason,
        )
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id="create",
            artifact_id=None,
        )
        automatic = core.ResolvedModClosure(
            ("modrinth", project_id),
            (fixture.resolved_metadata("root", "r2"),),
        )
        with patch.object(core, "resolve_mod_closure", return_value=automatic):
            session = core.TemplateImportSession.create(fixture.key, ["base"])
        verification = session.verifications[0]
        verified_root = next(
            item
            for item in verification.cached_closure.metadata
            if item.identity == ("modrinth", project_id)
        )
        self.assertEqual(
            core.parse_provider_metadata(
                verified_root.relative_path, verified_root.contents
            ).file_id,
            artifact_id,
        )
        incoming = session.plan.template_candidates[0]
        resolved = resolve_template_import_plan(
            session.plan,
            actual_identity_resolutions={
                f"modrinth:{project_id}": ImportConflictResolution(
                    (incoming.selection_key,)
                )
            },
        )
        operation = core.TemplateImportOperation(session, resolved)
        operation.run()
        self.assertIsNone(operation.error)
        return fixture, session, operation, project_id, artifact_id

    def seed_installed_modrinth_root(
        self,
        fixture: exact_selection.ExactModVersionSelectionTest,
        *,
        artifact_id: str,
        locked: bool = False,
        reason: str | None = None,
    ) -> tuple[str, str]:
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        root_path = fixture.source / "mods" / "root.pw.toml"
        root_path.write_text(
            fixture.metadata(
                "modrinth", project_id, artifact_id, side="both"
            ),
            encoding="utf-8",
        )
        (fixture.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n/.huroshiki-version-overrides.json\n",
            encoding="utf-8",
        )
        core.write_pack_root_manifest(
            fixture.source,
            (PackRootRecord("modrinth", project_id, "both"),),
        )
        core.write_mod_version_overrides(
            fixture.source,
            (
                ModVersionOverride(
                    "modrinth", project_id, artifact_id, locked, reason
                ),
            ),
        )
        return project_id, artifact_id

    def test_override_manifest_schema_and_duplicate_rejection(self) -> None:
        valid = serialize_mod_version_overrides(
            (ModVersionOverride("modrinth", "Abcd1234", "Efgh5678", True, "keep"),)
        )
        self.assertEqual(len(parse_mod_version_overrides(valid).entries), 1)
        cases = (
            b"{}",
            b'{"schema":1,"mods":[]}',
            b'{"schema":1,"mods":{"modrinth:Abcd1234":{"artifact_id":"bad"}}}',
            b'{"schema":1,"mods":{"curseforge:1":{"artifact_id":"2","selection":"user","locked":false},"curseforge:1":{"artifact_id":"3","selection":"user","locked":false}}}',
            b'{"schema":1,"mods":{"url:x":{"artifact_id":"y","selection":"user","locked":false}}}',
        )
        for contents in cases:
            with self.subTest(contents=contents), self.assertRaises(Exception):
                parse_mod_version_overrides(contents)

    def test_override_write_preserves_existing_edit_and_adds_ignore_contract(self) -> None:
        fixture = self.core_fixture()
        ignore = fixture.source / ".packwizignore"
        ignore.write_text("/custom-entry\n", encoding="utf-8")
        set_mod_version_override(
            fixture.source,
            ModVersionOverride("modrinth", "Abcd1234", "Efgh5678", False),
        )
        ensure_mod_version_overrides_ignored(fixture.source)
        self.assertIn("/custom-entry", ignore.read_text(encoding="utf-8"))
        self.assertIn("/.huroshiki-version-overrides.json", ignore.read_text(encoding="utf-8"))
        self.assertEqual(read_mod_version_overrides(fixture.source)[0].artifact_id, "Efgh5678")

    def test_packctl_template_override_schema_rejects_roles_and_noncanonical_roots(self) -> None:
        fixture = self.core_fixture()
        config = packctl.load_yaml(fixture.template / "template.yaml")
        config["mod_version_overrides"] = [
            {"provider": "url", "project_id": "x", "artifact_id": "y", "scope": "root"},
        ]
        with self.assertRaises(packctl.ConfigError):
            packctl.prospective_template_config("base", config, {})
        for provider, project, artifact in (("curseforge", "01", "2"), ("modrinth", "slug", "Efgh5678")):
            config["mod_version_overrides"] = [{"provider": provider, "project_id": project, "artifact_id": artifact, "scope": "root"}]
            with self.subTest(provider=provider), self.assertRaises(packctl.ConfigError):
                packctl.prospective_template_config("base", config, {})

    def test_packctl_accepts_canonical_cf_mr_roots_and_nonroot_dependency_intent(self) -> None:
        fixture = self.core_fixture()
        config = packctl.load_yaml(fixture.template / "template.yaml")
        config["mods"] = [
            {"name": "MR", "provider": "modrinth", "project_id": "Abcd1234", "side": "both"},
            {"name": "CF", "provider": "curseforge", "project_id": "123456", "side": "client"},
        ]
        config["mod_version_overrides"] = [
            {"provider": "modrinth", "project_id": "Abcd1234", "artifact_id": "Efgh5678", "scope": "root"},
            {"provider": "curseforge", "project_id": "123456", "artifact_id": "789012", "scope": "root"},
            {"provider": "modrinth", "project_id": "Qrst5678", "artifact_id": "Uvwx5678", "scope": "dependency"},
        ]
        prospective = packctl.prospective_template_config("base", config, {})
        self.assertEqual(len(prospective["mod_version_overrides"]), 3)

    def test_template_override_schema_rejects_null_unknown_duplicate_missing_root_and_role(self) -> None:
        fixture = self.core_fixture()
        base = packctl.load_yaml(fixture.template / "template.yaml")
        invalid_values = [
            None,
            {},
            [{"provider": "curseforge", "project_id": "1", "artifact_id": "2", "scope": "root", "pin": True}],
            [{"provider": "curseforge", "project_id": "1", "artifact_id": "2"}],
            [{"provider": "curseforge", "project_id": "1", "artifact_id": "2", "scope": "root"}],
        ]
        for value in invalid_values:
            config = dict(base)
            config["mod_version_overrides"] = value
            with self.subTest(value=value), self.assertRaises(packctl.ConfigError):
                packctl.prospective_template_config("base", config, {})
        config = dict(base)
        config["mods"] = [
            {"name": "Root", "provider": "curseforge", "project_id": "1", "side": "both"}
        ]
        config["mod_version_overrides"] = [
            {"provider": "curseforge", "project_id": "1", "artifact_id": "2", "scope": "root"},
            {"provider": "curseforge", "project_id": "1", "artifact_id": "2", "scope": "dependency"},
        ]
        with self.assertRaises(packctl.ConfigError):
            packctl.prospective_template_config("base", config, {})

    @staticmethod
    def verification(candidate, actual=None):
        return ImportCandidateVerification(
            candidate.selector_identity,
            actual if actual is not None else candidate.actual_identity,
            None,
            None,
            "closure",
            None,
        )

    def make_plan(self, templates, candidates, constraints=()):
        merged = merge_template_import_candidates(candidates)
        return build_template_import_plan(
            pack_key="pack:demo",
            pack_minecraft="1.21.1",
            pack_loader="neoforge",
            template_ids=templates,
            compatibilities={t: TemplateCompatibility(t, "1.21.1", "neoforge") for t in templates},
            pack_candidates=(),
            template_candidates=candidates,
            verifications=tuple(self.verification(c) for c in merged),
            constraints=constraints,
        )

    def test_plan_digest_binds_scope_origin_selector_and_constraints(self) -> None:
        candidate = template_candidate("base", name="Root", provider="modrinth", project_id="Abcd1234", side="client", actual_provider="modrinth", actual_project_id="Abcd1234")
        plain = self.make_plan(("base",), [candidate])
        constrained = self.make_plan(
            ("base",), [candidate],
            (TemplateVersionConstraint("base", "modrinth", "Abcd1234", "Efgh5678", "root"),),
        )
        self.assertNotEqual(plain.plan_digest, constrained.plan_digest)
        renamed = template_candidate("other", name="Root", provider="modrinth", project_id="Abcd1234", side="client", actual_provider="modrinth", actual_project_id="Abcd1234")
        self.assertNotEqual(plain.plan_digest, self.make_plan(("other",), [renamed]).plan_digest)
        dependency = self.make_plan(
            ("base",),
            [candidate],
            (
                TemplateVersionConstraint(
                    "base", "modrinth", "Qrst5678", "Uvwx5678", "dependency"
                ),
            ),
        )
        changed_dependency = self.make_plan(
            ("base",),
            [candidate],
            (
                TemplateVersionConstraint(
                    "base", "modrinth", "Qrst5678", "Efgh5678", "dependency"
                ),
            ),
        )
        self.assertNotEqual(dependency.plan_digest, changed_dependency.plan_digest)

    def test_matching_composition_is_order_independent_and_activation_is_identity_based(self) -> None:
        a = template_candidate("base", name="A", provider="modrinth", project_id="Abcd1234", side="client", actual_provider="modrinth", actual_project_id="Abcd1234")
        b = template_candidate("extra", name="B", provider="modrinth", project_id="Abcd1234", side="server", actual_provider="modrinth", actual_project_id="Abcd1234")
        first = self.make_plan(("base", "extra"), [a, b])
        reverse = self.make_plan(("extra", "base"), [b, a])
        self.assertEqual(first.template_candidates[0].side, "both")
        self.assertEqual(first.template_candidates[0].actual_identity, ("modrinth", "Abcd1234"))
        self.assertEqual(first.template_candidates[0].side, reverse.template_candidates[0].side)
        self.assertEqual(first.template_candidates[0].actual_identity, reverse.template_candidates[0].actual_identity)
        self.assertEqual(first.selection_options[0].option_key, reverse.selection_options[0].option_key)
        self.assertEqual(first.new_roots[0].actual_identity, ("modrinth", "Abcd1234"))

    def test_cross_template_matching_constraints_compose_and_conflicts_do_not_last_win(self) -> None:
        a = template_candidate(
            "base", name="Root", provider="modrinth", project_id="Abcd1234",
            side="client", actual_provider="modrinth", actual_project_id="Abcd1234",
        )
        b = template_candidate(
            "extra", name="Root", provider="modrinth", project_id="Abcd1234",
            side="server", actual_provider="modrinth", actual_project_id="Abcd1234",
        )
        matching = (
            TemplateVersionConstraint("base", "modrinth", "Abcd1234", "Efgh5678", "root"),
            TemplateVersionConstraint("extra", "modrinth", "Abcd1234", "Efgh5678", "root"),
        )
        plan = self.make_plan(("base", "extra"), [a, b], matching)
        resolved = resolve_template_import_plan(plan)
        self.assertEqual(
            {item.artifact_id for item in resolved.version_constraints},
            {"Efgh5678"},
        )
        conflicting = (
            matching[0],
            TemplateVersionConstraint("extra", "modrinth", "Abcd1234", "Ijkl9012", "root"),
        )
        conflict_plan = self.make_plan(("base", "extra"), [a, b], conflicting)
        with self.assertRaises(TemplateMergeError):
            resolve_template_import_plan(conflict_plan)

    def test_conflict_resolution_is_exactly_one_and_rejects_stale_or_contradictory_choices(self) -> None:
        left = template_candidate("base", name="Same", provider="modrinth", project_id="Abcd1234", side="both", actual_provider="modrinth", actual_project_id="Abcd1234")
        right = template_candidate("base", name="Same", provider="modrinth", project_id="Wxyz5678", side="both", actual_provider="modrinth", actual_project_id="Wxyz5678")
        plan = self.make_plan(("base",), [left, right])
        conflict = plan.name_conflicts[0]
        with self.assertRaises(TemplateMergeError):
            resolve_template_import_plan(plan, name_resolutions={"stale": ImportConflictResolution((conflict.options[0].option_key,))})
        with self.assertRaises(TemplateMergeError):
            resolve_template_import_plan(plan, name_resolutions={conflict.key: ImportConflictResolution(())})

    def test_rejected_root_candidate_deactivates_its_exact_constraint(self) -> None:
        left = template_candidate(
            "base", name="Same", provider="modrinth", project_id="Abcd1234",
            side="both", actual_provider="modrinth", actual_project_id="Abcd1234",
        )
        right = template_candidate(
            "extra", name="Same", provider="modrinth", project_id="Wxyz5678",
            side="both", actual_provider="modrinth", actual_project_id="Wxyz5678",
        )
        constraint = TemplateVersionConstraint(
            "base", "modrinth", "Abcd1234", "Efgh5678", "root"
        )
        plan = self.make_plan(("base", "extra"), [left, right], (constraint,))
        conflict = plan.name_conflicts[0]
        selected = next(
            option for option in conflict.options if right in option.candidates
        )
        resolved = resolve_template_import_plan(
            plan,
            name_resolutions={
                conflict.key: ImportConflictResolution((selected.option_key,))
            },
        )
        self.assertEqual(resolved.version_constraints, ())

    def test_operation_rejects_forged_resolution_that_drops_version_intent(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r2"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
        )
        session = core.TemplateImportSession.create(fixture.key, ["base"])
        resolved = resolve_template_import_plan(session.plan)
        forged = replace(resolved, version_constraints=())
        with self.assertRaisesRegex(core.HuroshikiError, "constraints are stale"):
            core.TemplateImportOperation(session, forged)
        session.discard()

    def test_constraint_roles_are_validated_and_dependency_constraints_remain_active(self) -> None:
        candidate = template_candidate("base", name="Root", provider="modrinth", project_id="Abcd1234", side="both", actual_provider="modrinth", actual_project_id="Abcd1234")
        dependency = TemplateVersionConstraint("base", "modrinth", "Qrst5678", "Uvwx5678", "dependency")
        plan = self.make_plan(("base",), [candidate], (dependency,))
        resolved = resolve_template_import_plan(plan)
        self.assertEqual(resolved.version_constraints, (dependency,))
        with self.assertRaises(TemplateMergeError):
            self.make_plan(("base",), [candidate], (dependency, TemplateVersionConstraint("base", "modrinth", "Qrst5678", "Efgh5678", "root")))

    def test_real_session_uses_requested_exact_artifact_and_persists_unlocked_override(self) -> None:
        fixture = self.core_fixture()
        fixture.template.joinpath("template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\nminecraft: 1.21.1\nloader: neoforge\nreference_loader_version: 21.1.0\n"
            "mods:\n  - name: Root\n    provider: modrinth\n    project_id: Abcd1234\n    side: client\n"
            "mod_version_overrides:\n  - provider: modrinth\n    project_id: Abcd1234\n    artifact_id: Efgh5678\n    scope: root\n",
            encoding="utf-8",
        )
        closure = core.ResolvedModClosure(
            ("modrinth", "Abcd1234"),
            (core.ResolvedMetadata(
                ("modrinth", "Abcd1234"), Path("mods/root.pw.toml"), "root.jar",
                import_core.metadata("Root", "Abcd1234", "Efgh5678"), "modrinth", "Abcd1234",
            ),),
        )
        verification = ImportCandidateVerification(
            ("modrinth", "Abcd1234", None), ("modrinth", "Abcd1234"),
            Path("mods/root.pw.toml"), "root.jar", "closure", None, closure,
        )
        with patch.object(core, "verify_import_candidates", return_value=(verification,)):
            session = core.TemplateImportSession.create("pack:demo", ["base"])
        self.assertTrue(session.plan.version_constraints)
        constraint = session.plan.version_constraints[0]
        self.assertEqual(constraint.artifact_id, "Efgh5678")
        self.assertEqual(constraint.scope, "root")
        session.discard()

    def test_exact_modrinth_root_runs_one_transaction_and_publishes_intent(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r2"]
        template = fixture.root / "templates" / "base" / "template.yaml"
        template.write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: fabric\n"
            "reference_loader_version: 0.16.0\nmods:\n"
            f"  - name: Root\n    provider: modrinth\n    project_id: {project_id}\n    side: both\n"
            "mod_version_overrides:\n"
            f"  - provider: modrinth\n    project_id: {project_id}\n"
            f"    artifact_id: {artifact_id}\n    scope: root\n",
            encoding="utf-8",
        )
        before = core.tree_digest_snapshot(fixture.source)
        session = core.TemplateImportSession.create(fixture.key, ["base"])
        resolved = resolve_template_import_plan(session.plan)
        operation = core.TemplateImportOperation(session, resolved)
        operation.run()
        self.assertIsNone(operation.error)
        self.assertIsNotNone(operation.preview)
        self.assertEqual(core.tree_digest_snapshot(fixture.source), before)
        self.assertEqual(
            operation.preview.version_constraints[0].artifact_id,
            artifact_id,
        )
        staged = read_mod_version_overrides(operation.transaction.source)
        self.assertEqual(
            [(item.artifact_id, item.locked) for item in staged],
            [(artifact_id, False)],
        )
        operation.apply()
        persisted = read_mod_version_overrides(fixture.source)
        self.assertEqual(
            [(item.artifact_id, item.locked) for item in persisted],
            [(artifact_id, False)],
        )

    def test_exact_curseforge_root_publishes_requested_file_id(self) -> None:
        fixture = self.exact_fixture()
        self.write_exact_template(
            fixture,
            provider="curseforge",
            project_id="101",
            artifact_id="7",
        )
        operation = self.run_exact_import(fixture)
        self.assertIsNone(operation.error)
        self.assertIn(
            "file-id = 7",
            operation.transaction.source.joinpath("mods/root.pw.toml").read_text(),
        )
        operation.apply()
        self.assertEqual(read_mod_version_overrides(fixture.source)[0].artifact_id, "7")

    def test_cli_json_exposes_structured_version_constraints(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r2"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
        )
        args = packctl.parser().parse_args(
            ["apply-template", "demo", "base", "--json"]
        )
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(packctl.cmd_apply_template(args), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            payload["version_constraints"],
            [
                {
                    "canonical_identity": f"modrinth:{project_id}",
                    "provider": "modrinth",
                    "project_id": project_id,
                    "artifact_id": artifact_id,
                    "scope": "root",
                    "origins": ["base"],
                    "locked": False,
                    "reason": None,
                }
            ],
        )

    def test_exact_dependency_is_owned_and_never_promoted_to_root(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
            dependency=("curseforge", "987654", "987656"),
        )
        operation = self.run_exact_import(fixture)
        self.assertIsNone(operation.error)
        roots = read_pack_root_manifest(operation.transaction.source)
        self.assertEqual(
            [(item.provider, item.project_id) for item in roots],
            [("modrinth", project_id)],
        )
        overrides = read_mod_version_overrides(operation.transaction.source)
        self.assertEqual(
            {(item.provider, item.project_id, item.artifact_id) for item in overrides},
            {
                ("modrinth", project_id, artifact_id),
                ("curseforge", "987654", "987656"),
            },
        )
        self.assertIn(
            "project-id = 987654",
            operation.transaction.source.joinpath(
                "mods/dependency.pw.toml"
            ).read_text(),
        )
        operation.discard()

    def test_orphaned_dependency_constraint_fails_closed(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
            dependency=("curseforge", "999999", "999998"),
        )
        operation = self.run_exact_import(fixture)
        self.assertIsNotNone(operation.error)
        self.assertIn("has no prospective owner", str(operation.error))

    def test_locked_pack_intent_matches_or_blocks_template_request(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        old_artifact = exact_selection.MR_VERSION_IDS["r1"]
        self.seed_installed_modrinth_root(
            fixture,
            artifact_id=old_artifact,
            locked=True,
            reason="compatibility",
        )
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=old_artifact,
        )
        matching = self.run_exact_import(fixture)
        self.assertIsNone(matching.error)
        staged = read_mod_version_overrides(matching.transaction.source)[0]
        self.assertTrue(staged.locked)
        self.assertEqual(staged.reason, "compatibility")
        matching.discard()

        new_artifact = exact_selection.MR_VERSION_IDS["r2"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=new_artifact,
        )
        blocked = self.run_exact_import(fixture)
        self.assertIsInstance(blocked.error, core.ProfileVersionIntentError)
        self.assertEqual(blocked.error.user_pin_reason, "compatibility")
        self.assertIn("pin reason: compatibility", str(blocked.error))

    def test_cli_json_reports_blocked_lock_without_synthetic_reason(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        old_artifact = exact_selection.MR_VERSION_IDS["r1"]
        new_artifact = exact_selection.MR_VERSION_IDS["r2"]
        self.seed_installed_modrinth_root(
            fixture, artifact_id=old_artifact, locked=True, reason=None
        )
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=new_artifact,
        )
        args = packctl.parser().parse_args(
            ["apply-template", "demo", "base", "--json"]
        )
        errors = StringIO()
        with redirect_stderr(errors):
            self.assertEqual(packctl.cmd_apply_template(args), 1)
        payload = json.loads(errors.getvalue())
        self.assertEqual(payload["version_block"]["identity"], f"modrinth:{project_id}")
        self.assertEqual(payload["version_block"]["pinned_artifact"], old_artifact)
        self.assertIsNone(payload["version_block"]["user_pin_reason"])
        self.assertNotIn("pin reason:", payload["version_block"]["technical_failure"])

    def test_unlocked_pack_intent_is_replaced_only_by_active_template_request(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        old_artifact = exact_selection.MR_VERSION_IDS["r1"]
        new_artifact = exact_selection.MR_VERSION_IDS["r2"]
        self.seed_installed_modrinth_root(
            fixture, artifact_id=old_artifact, locked=False
        )
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=new_artifact,
        )
        operation = self.run_exact_import(fixture)
        self.assertIsNone(operation.error)
        self.assertEqual(
            read_mod_version_overrides(operation.transaction.source)[0].artifact_id,
            new_artifact,
        )
        operation.discard()

    def test_automatic_template_root_obeys_existing_pack_intent(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        self.seed_installed_modrinth_root(
            fixture, artifact_id=artifact_id, locked=False
        )
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=None,
        )
        automatic = core.ResolvedModClosure(
            ("modrinth", project_id),
            (fixture.resolved_metadata("root", "r2"),),
        )
        with patch.object(core, "resolve_mod_closure", return_value=automatic):
            operation = self.run_exact_import(fixture)
        self.assertIsNone(operation.error)
        self.assertEqual(
            read_mod_version_overrides(operation.transaction.source)[0].artifact_id,
            artifact_id,
        )
        operation.discard()

    def test_slug_root_rebinds_to_unlocked_same_identity_pack_intent(self) -> None:
        fixture, _session, operation, project_id, artifact_id = (
            self.run_same_identity_slug_pack_intent(
                locked=False,
                reason=None,
            )
        )
        preview = next(
            item
            for item in operation.preview.version_constraints
            if item.canonical_identity == f"modrinth:{project_id}"
        )
        self.assertEqual(preview.artifact_id, artifact_id)
        self.assertEqual(preview.origins, ("Pack",))
        self.assertFalse(preview.locked)
        operation.apply()
        applied = core.parse_provider_metadata(
            Path("mods/root.pw.toml"),
            fixture.source.joinpath("mods/root.pw.toml").read_bytes(),
        )
        self.assertEqual(applied.file_id, artifact_id)
        self.assertEqual(
            read_mod_version_overrides(fixture.source)[0].artifact_id,
            artifact_id,
        )

    def test_slug_root_rebinds_to_locked_same_identity_pack_intent(self) -> None:
        fixture, _session, operation, project_id, artifact_id = (
            self.run_same_identity_slug_pack_intent(
                locked=True,
                reason="compatibility",
            )
        )
        preview = next(
            item
            for item in operation.preview.version_constraints
            if item.canonical_identity == f"modrinth:{project_id}"
        )
        self.assertEqual(preview.artifact_id, artifact_id)
        self.assertTrue(preview.locked)
        self.assertEqual(preview.reason, "compatibility")
        operation.apply()
        applied_override = read_mod_version_overrides(fixture.source)[0]
        self.assertEqual(applied_override.artifact_id, artifact_id)
        self.assertTrue(applied_override.locked)
        self.assertEqual(applied_override.reason, "compatibility")

    def test_slug_root_pack_intent_artifact_is_plan_fingerprint_bound(self) -> None:
        states: list[tuple[str, str, str]] = []
        for artifact_key in ("r1", "r2"):
            fixture = self.exact_fixture()
            project_id = exact_selection.MR_PROJECT_IDS["root"]
            artifact_id = exact_selection.MR_VERSION_IDS[artifact_key]
            self.seed_installed_modrinth_root(
                fixture,
                artifact_id=artifact_id,
            )
            self.write_exact_template(
                fixture,
                provider="modrinth",
                project_id="create",
                artifact_id=None,
            )
            automatic = core.ResolvedModClosure(
                ("modrinth", project_id),
                (fixture.resolved_metadata("root", "r2"),),
            )
            with patch.object(
                core, "resolve_mod_closure", return_value=automatic
            ):
                session = core.TemplateImportSession.create(
                    fixture.key, ["base"]
                )
            verification = session.verifications[0]
            root = next(
                item
                for item in verification.cached_closure.metadata
                if item.identity == ("modrinth", project_id)
            )
            states.append((
                session.plan.plan_digest,
                verification.closure_fingerprint,
                core.parse_provider_metadata(
                    root.relative_path, root.contents
                ).file_id,
            ))
            session.discard()
        self.assertEqual(
            [state[2] for state in states],
            [
                exact_selection.MR_VERSION_IDS["r1"],
                exact_selection.MR_VERSION_IDS["r2"],
            ],
        )
        self.assertNotEqual(states[0][0], states[1][0])
        self.assertNotEqual(states[0][1], states[1][1])

    def test_retained_unconstrained_pack_root_uses_fixed_baseline_artifact(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        (fixture.source / "mods" / "root.pw.toml").write_text(
            fixture.metadata(
                "modrinth", project_id, artifact_id, side="both"
            ),
            encoding="utf-8",
        )
        (fixture.source / ".packwizignore").write_text(
            "/.huroshiki-roots.json\n/.huroshiki-version-overrides.json\n",
            encoding="utf-8",
        )
        core.write_pack_root_manifest(
            fixture.source,
            (PackRootRecord("modrinth", project_id, "both"),),
        )
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=None,
            dependency=("curseforge", "987654", "987656"),
        )
        automatic = core.ResolvedModClosure(
            ("modrinth", project_id),
            (fixture.resolved_metadata("root", "r2"),),
        )
        with patch.object(core, "resolve_mod_closure", return_value=automatic):
            operation = self.run_exact_import(fixture)
        self.assertIsNone(operation.error)
        root_metadata = core.parse_provider_metadata(
            Path("mods/root.pw.toml"),
            operation.transaction.source.joinpath("mods/root.pw.toml").read_bytes(),
        )
        self.assertEqual(root_metadata.file_id, artifact_id)
        operation.discard()

    def test_cancel_after_preview_blocks_publication(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r2"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
        )
        before = core.tree_digest_snapshot(fixture.source)
        operation = self.run_exact_import(fixture)
        self.assertIsNone(operation.error)
        operation.cancel_event.set()
        with self.assertRaises(core.LoaderMigrationCancelled):
            operation.apply()
        self.assertEqual(core.tree_digest_snapshot(fixture.source), before)

    def test_non_mod_packwiz_metadata_is_preserved_and_never_materialized(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r2"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
        )
        pack_toml = (
            'name = "Custom Pack"\nauthor = "Pack Author"\n'
            '[versions]\nminecraft = "1.21.1"\nfabric = "0.16.0"\n'
        )
        fixture.source.joinpath("pack.toml").write_text(
            pack_toml, encoding="utf-8"
        )
        fixture.source.joinpath(".packwizignore").write_text(
            "/custom-cache/\n", encoding="utf-8"
        )
        unrelated = fixture.source / "notes" / "keep.bin"
        unrelated.parent.mkdir()
        unrelated.write_bytes(b"unrelated\x00bytes\n")
        resource = fixture.source / "resourcepacks" / "example.pw.toml"
        shader = fixture.source / "shaderpacks" / "example.pw.toml"
        resource.parent.mkdir()
        shader.parent.mkdir()
        resource.write_text(
            fixture.metadata(
                "modrinth", project_id, artifact_id, filename="example.zip"
            ), encoding="utf-8"
        )
        shader.write_text(
            fixture.metadata(
                "curseforge", "998", "3", filename="shader.zip"
            ), encoding="utf-8"
        )
        resource_before = resource.read_bytes()
        shader_before = shader.read_bytes()
        materialized: list[Path] = []

        def observe(candidate, *args, **kwargs):
            materialized.append(Path(candidate.relative_metadata_path))
            return fixture.materialize(candidate, *args, **kwargs)

        with patch.object(
            core, "materialize_provider_artifact", side_effect=observe
        ):
            operation = self.run_exact_import(fixture)
        self.assertIsNone(operation.error)
        self.assertEqual(
            operation.transaction.source.joinpath(
                "resourcepacks/example.pw.toml"
            ).read_bytes(),
            resource_before,
        )
        self.assertEqual(
            operation.transaction.source.joinpath(
                "shaderpacks/example.pw.toml"
            ).read_bytes(),
            shader_before,
        )
        self.assertTrue(materialized)
        self.assertTrue(all(path.parts[0] == "mods" for path in materialized))
        self.assertEqual(
            operation.transaction.source.joinpath("pack.toml").read_text(),
            pack_toml,
        )
        self.assertEqual(
            operation.transaction.source.joinpath("notes/keep.bin").read_bytes(),
            b"unrelated\x00bytes\n",
        )
        self.assertIn(
            "/custom-cache/",
            operation.transaction.source.joinpath(
                ".packwizignore"
            ).read_text().splitlines(),
        )
        operation.discard()

    def test_refresh_non_mod_change_fails_and_real_source_rolls_back(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r2"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
        )
        resource = fixture.source / "resourcepacks" / "example.pw.toml"
        resource.parent.mkdir()
        resource.write_text(
            fixture.metadata(
                "curseforge", "999", "4", filename="example.zip"
            ), encoding="utf-8"
        )
        before = core.tree_digest_snapshot(fixture.source)

        def mutate(command, *, cwd, **kwargs):
            result = fixture.run_fake_resolver(command, cwd=cwd, **kwargs)
            if command == ["packwiz", "refresh"] and cwd.name == "import-exact-preflight":
                (cwd / "resourcepacks" / "example.pw.toml").write_bytes(b"changed\n")
            return result

        with patch.object(core, "run_resolver_process", side_effect=mutate):
            operation = self.run_exact_import(fixture)
        self.assertIsNotNone(operation.error)
        self.assertIn("non-MOD Packwiz metadata", str(operation.error))
        self.assertEqual(core.tree_digest_snapshot(fixture.source), before)

    def test_exact_selection_rejects_incompatible_resolved_identity(self) -> None:
        fixture = self.core_fixture()
        selection = core.ExactModArtifactSelection(
            "modrinth",
            exact_selection.branded_project("root"),
            exact_selection.branded_version("r1"),
        )
        wrong = import_core.metadata("Wrong", "different-project", "r1")

        def resolver(command, *, cwd, **_kwargs):
            if command != ["packwiz", "refresh"]:
                (cwd / "mods").mkdir(exist_ok=True)
                (cwd / "mods" / "wrong.pw.toml").write_bytes(wrong)
            return fixture.refresh_ok(command)

        with patch.object(core, "run_resolver_process", side_effect=resolver):
            with self.assertRaises(Exception):
                core.resolve_exact_mod_closure(
                    selection,
                    source=fixture.source,
                    cancel_event=threading.Event(),
                    deadline=time.monotonic() + 5,
                    checkpoint=lambda: None,
                )

    def test_transaction_dry_run_preserves_source_and_apply_is_atomic(self) -> None:
        fixture = self.core_fixture()
        before = core.tree_digest_snapshot(fixture.source)
        with patch.object(
            core,
            "resolve_project_selector",
            return_value=core.ResolvedSelector(
                "modrinth", "root", "root", "Root"
            ),
        ):
            operation = fixture.operation()
        with patch.object(core, "resolve_mod_closure", return_value=fixture.closure()), patch.object(core, "run_resolver_process", side_effect=fixture.refresh_ok):
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(core.tree_digest_snapshot(fixture.source), before)
        operation.apply()
        self.assertTrue((fixture.source / "mods/root.pw.toml").exists())

    def test_refresh_failure_rolls_back_and_releases_transaction(self) -> None:
        fixture = self.core_fixture()
        operation = fixture.operation()
        failed = core.ResolverProcessResult(1, "", "refresh failed", False, False)
        with patch.object(core, "resolve_mod_closure", return_value=fixture.closure()), patch.object(core, "run_resolver_process", return_value=failed):
            operation.run()
        self.assertIsNotNone(operation.error)
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_realistic_exact_refresh_updates_index_binding_and_rejects_semantic_changes(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r2"]
        self.write_exact_template(fixture, provider="modrinth", project_id=project_id, artifact_id=artifact_id)
        pack_text = (
            'name = "Custom Pack"\nauthor = "Custom Author"\n'
            'version = "1.0.0"\n'
            'pack-format = "packwiz:1.1.0"\n[index]\nfile = "index.toml"\n'
            'hash-format = "sha256"\nhash = "stale"\n'
            '[versions]\nminecraft = "1.21.1"\nfabric = "0.16.0"\n'
        )
        fixture.source.joinpath("pack.toml").write_text(pack_text, encoding="utf-8")

        def refresh_with(pack_mutation=lambda contents: contents):
            def refresh(command, *, cwd, **kwargs):
                result = fixture.run_fake_resolver(command, cwd=cwd, **kwargs)
                if command == ["packwiz", "refresh"]:
                    index = 'hash-format = "sha256"\n\n[mods]\n'
                    (cwd / "index.toml").write_text(index, encoding="utf-8")
                    digest = hashlib.sha256(index.encode()).hexdigest()
                    refreshed = pack_text.replace(
                        'hash = "stale"', f'hash = "{digest}"'
                    )
                    (cwd / "pack.toml").write_text(
                        pack_mutation(refreshed), encoding="utf-8"
                    )
                return result
            return refresh

        before = core.tree_digest_snapshot(fixture.source)
        with patch.object(
            core, "run_resolver_process", side_effect=refresh_with()
        ):
            operation = self.run_exact_import(fixture)
        self.assertIsNone(operation.error)
        staged = tomllib.loads(operation.transaction.source.joinpath("pack.toml").read_text())
        self.assertEqual((staged["name"], staged["author"]), ("Custom Pack", "Custom Author"))
        self.assertEqual(staged["version"], "1.0.0")
        self.assertEqual(
            staged["versions"],
            {"minecraft": "1.21.1", "fabric": "0.16.0"},
        )
        self.assertEqual(staged["index"]["hash"], hashlib.sha256(operation.transaction.source.joinpath("index.toml").read_bytes()).hexdigest())
        operation.discard()
        self.assertEqual(core.tree_digest_snapshot(fixture.source), before)

        for label, old, new in (
            ("name", 'name = "Custom Pack"', 'name = "Changed"'),
            ("author", 'author = "Custom Author"', 'author = "Changed"'),
            ("version", 'version = "1.0.0"', 'version = "2.0.0"'),
            (
                "pack-format",
                'pack-format = "packwiz:1.1.0"',
                'pack-format = "packwiz:1.0.0"',
            ),
            ("minecraft", 'minecraft = "1.21.1"', 'minecraft = "1.21.2"'),
            ("loader-version", 'fabric = "0.16.0"', 'fabric = "0.17.0"'),
            ("loader", 'fabric = "0.16.0"', 'neoforge = "21.1.0"'),
        ):
            with self.subTest(label=label):
                fixture.source.joinpath("pack.toml").write_text(pack_text, encoding="utf-8")
                unchanged = core.tree_digest_snapshot(fixture.source)
                with patch.object(
                    core,
                    "run_resolver_process",
                    side_effect=refresh_with(lambda contents, old=old, new=new: contents.replace(old, new)),
                ):
                    failed = self.run_exact_import(fixture)
                self.assertIsNotNone(failed.error)
                self.assertEqual(core.tree_digest_snapshot(fixture.source), unchanged)

    def test_automatic_modrinth_slug_planning_binds_actual_identity_without_canonical_id(self) -> None:
        fixture = self.exact_fixture()
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id="root-slug",
            artifact_id=None,
            dependency=("curseforge", "987654", "987656"),
        )
        closure = core.ResolvedModClosure(
            ("modrinth", exact_selection.MR_PROJECT_IDS["root"]),
            (
                fixture.resolved_metadata("root", "r1"),
                core.ResolvedMetadata(
                    ("curseforge", "987654"),
                    Path("mods/dependency.pw.toml"),
                    "dependency.jar",
                    fixture.metadata(
                        "curseforge", "987654", "987656",
                        filename="dependency.jar",
                    ).encode(),
                    "curseforge",
                    "987654",
                ),
            ),
        )
        with patch.object(core, "resolve_mod_closure", return_value=closure) as resolver:
            session = core.TemplateImportSession.create(fixture.key, ["base"])
        self.assertIsNone(resolver.call_args.kwargs["canonical_project_id"])
        self.assertEqual(session.plan.template_candidates[0].actual_identity, closure.root_identity)
        operation = core.TemplateImportOperation(
            session, resolve_template_import_plan(session.plan)
        )
        operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(
            operation.preview.added_roots[0].actual_identity,
            closure.root_identity,
        )
        operation.discard()

    def test_automatic_modrinth_slug_import_succeeds_with_unrelated_pack_override(self) -> None:
        fixture = self.exact_fixture()
        self.seed_installed_modrinth_root(
            fixture,
            artifact_id=exact_selection.MR_VERSION_IDS["r1"],
        )
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id="create",
            artifact_id=None,
        )
        closure = core.ResolvedModClosure(
            ("modrinth", exact_selection.MR_PROJECT_IDS["root-a"]),
            (fixture.resolved_metadata("root-a", "r1"),),
        )
        with patch.object(
            core, "resolve_mod_closure", return_value=closure
        ) as resolver:
            operation = self.run_exact_import(fixture)
        self.assertIsNone(resolver.call_args.kwargs["canonical_project_id"])
        self.assertIsNone(operation.error)
        self.assertEqual(
            operation.preview.added_roots[0].actual_identity,
            closure.root_identity,
        )
        operation.discard()

    def test_url_actual_identity_replacement_is_not_duplicate_or_unchanged(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        fixture.root.joinpath("templates/base/template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: fabric\n"
            "reference_loader_version: 0.16.0\nmods:\n"
            "  - name: Requested\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/requested.jar\n",
            encoding="utf-8",
        )
        mods = fixture.source / "mods"
        (mods / "root.pw.toml").write_text(
            fixture.metadata("modrinth", project_id, artifact_id),
            encoding="utf-8",
        )
        installed_url = (
            import_core.url_metadata(
                "Installed", "actual.jar", "https://mods.example/installed.jar"
            )
            + b'\n[huroshiki]\nproject-id = "actual"\nloaders = ["fabric"]\n'
            + b'minecraft-versions = ["1.21.1"]\n'
        )
        (mods / "actual.pw.toml").write_bytes(installed_url)
        core.ensure_pack_root_manifest_ignored(fixture.source)
        core.ensure_mod_version_overrides_ignored(fixture.source)
        core.write_pack_root_manifest(
            fixture.source,
            (
                PackRootRecord("url", "actual", "both"),
                PackRootRecord("modrinth", project_id, "both"),
            ),
        )
        core.write_mod_version_overrides(
            fixture.source,
            (ModVersionOverride("modrinth", project_id, artifact_id),),
        )
        before = core.tree_digest_snapshot(fixture.source)
        incoming_contents = (
            import_core.url_metadata(
                "Requested", "actual.jar", "https://mods.example/requested.jar"
            )
            + b'\n[huroshiki]\nproject-id = "actual"\nloaders = ["fabric"]\n'
            + b'minecraft-versions = ["1.21.1"]\n'
        )
        incoming_closure = core.ResolvedModClosure(
            ("url", "actual"),
            (
                core.ResolvedMetadata(
                    ("url", "actual"),
                    Path("mods/actual.pw.toml"),
                    "actual.jar",
                    incoming_contents,
                    "url",
                    "actual",
                ),
            ),
        )
        with patch.object(
            core, "resolve_mod_closure", return_value=incoming_closure
        ):
            session = core.TemplateImportSession.create(fixture.key, ["base"])
            incoming = session.plan.template_candidates[0]
            resolved = resolve_template_import_plan(
                session.plan,
                actual_identity_resolutions={
                    "url:actual": ImportConflictResolution(
                        (incoming.selection_key,)
                    )
                },
            )
            operation = core.TemplateImportOperation(session, resolved)
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(len(operation.preview.added_roots), 1)
        self.assertEqual(len(operation.preview.removed), 1)
        self.assertEqual(len(operation.preview.unchanged), 0)
        final_url_entries = core._profile_mod_metadata_records(
            operation.transaction.source
        )[("url", "actual")]
        self.assertEqual(len(final_url_entries), 1)
        self.assertEqual(final_url_entries[0][0], Path("mods/actual.pw.toml"))
        self.assertEqual(core.tree_digest_snapshot(fixture.source), before)
        operation.discard()

    def test_url_identity_replacement_survives_unrelated_template_exact_root(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        fixture.root.joinpath("templates/base/template.yaml").write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: fabric\n"
            "reference_loader_version: 0.16.0\nmods:\n"
            "  - name: Requested\n    provider: url\n"
            "    project_id: logical\n    side: client\n"
            "    url: https://mods.example/requested.jar\n"
            "  - name: Provider Root\n    provider: modrinth\n"
            f'    project_id: "{project_id}"\n    side: both\n'
            "mod_version_overrides:\n"
            "  - provider: modrinth\n"
            f'    project_id: "{project_id}"\n'
            f'    artifact_id: "{artifact_id}"\n'
            "    scope: root\n",
            encoding="utf-8",
        )
        installed_contents = (
            import_core.url_metadata(
                "Installed", "actual.jar", "https://mods.example/installed.jar"
            )
            + b'\n[huroshiki]\nproject-id = "actual"\nloaders = ["fabric"]\n'
            + b'minecraft-versions = ["1.21.1"]\n'
        )
        fixture.source.joinpath("mods/actual.pw.toml").write_bytes(
            installed_contents
        )
        core.ensure_pack_root_manifest_ignored(fixture.source)
        core.write_pack_root_manifest(
            fixture.source, (PackRootRecord("url", "actual", "both"),)
        )
        incoming_contents = (
            import_core.url_metadata(
                "Requested", "actual.jar", "https://mods.example/requested.jar"
            )
            + b'\n[huroshiki]\nproject-id = "actual"\nloaders = ["fabric"]\n'
            + b'minecraft-versions = ["1.21.1"]\n'
        )
        incoming_closure = core.ResolvedModClosure(
            ("url", "actual"),
            (
                core.ResolvedMetadata(
                    ("url", "actual"),
                    Path("mods/actual.pw.toml"),
                    "actual.jar",
                    incoming_contents,
                    "url",
                    "actual",
                ),
            ),
        )
        with patch.object(
            core, "resolve_mod_closure", return_value=incoming_closure
        ):
            session = core.TemplateImportSession.create(fixture.key, ["base"])
            incoming = next(
                candidate
                for candidate in session.plan.template_candidates
                if candidate.provider == "url"
            )
            resolved = resolve_template_import_plan(
                session.plan,
                actual_identity_resolutions={
                    "url:actual": ImportConflictResolution(
                        (incoming.selection_key,)
                    )
                },
            )
            operation = core.TemplateImportOperation(session, resolved)
            operation.run()
        self.assertIsNone(operation.error)
        self.assertEqual(len(operation.preview.removed), 1)
        self.assertEqual(len(operation.preview.unchanged), 0)
        self.assertEqual(
            len(core._profile_mod_metadata_records(operation.transaction.source)[
                ("url", "actual")
            ]),
            1,
        )
        operation.discard()

    def test_provider_planning_lifecycle_results_fail_closed_and_preserve_source(self) -> None:
        fixture = self.exact_fixture()
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id="root-slug",
            artifact_id=None,
            dependency=("curseforge", "987654", "987656"),
        )
        before = core.tree_digest_snapshot(fixture.source)
        for result in (
            core.ResolverProcessResult(0, "", "", True, False),
            core.ResolverProcessResult(0, "", "", False, True),
            core.ResolverProcessResult(0, "", "", False, False, False, True),
            core.ResolverProcessResult(0, "", "", False, False, True, False),
        ):
            with self.subTest(result=result):
                def runner(*_args, result_callback=None, **_kwargs):
                    if result_callback is not None:
                        result_callback(result)
                    return result

                with patch.object(core, "run_resolver_process", side_effect=runner):
                    with self.assertRaises(Exception) as raised:
                        core.TemplateImportSession.create(fixture.key, ["base"])
                self.assertEqual(core.tree_digest_snapshot(fixture.source), before)
                retained = getattr(raised.exception, "transaction", None)
                if retained is not None:
                    self.assertTrue(retained.process_cleanup_pending)
                    retained._equivalence_process_results.clear()
                    retained.discard()

    def test_exact_root_planning_lifecycle_results_never_become_candidate_failures(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
        )
        before = core.tree_digest_snapshot(fixture.source)
        cases = (
            core.ResolverProcessResult(0, "", "", True, False),
            core.ResolverProcessResult(0, "", "", False, True),
            core.ResolverProcessResult(0, "", "", False, False, False, True),
            core.ResolverProcessResult(0, "", "", False, False, True, False),
        )
        for result in cases:
            with self.subTest(result=result):
                def resolver(*, process_result_callback, **_kwargs):
                    process_result_callback(result)
                    return core.ResolvedModClosure(
                        ("modrinth", project_id),
                        (fixture.resolved_metadata("root", "r1"),),
                    )

                with patch.object(
                    core, "resolve_exact_mod_closure", side_effect=resolver
                ):
                    with self.assertRaises(Exception) as raised:
                        core.TemplateImportSession.create(fixture.key, ["base"])
                self.assertEqual(core.tree_digest_snapshot(fixture.source), before)
                retained = getattr(raised.exception, "transaction", None)
                if retained is not None:
                    self.assertTrue(retained.process_cleanup_pending)
                    retained._equivalence_process_results.clear()
                    retained.discard()

    def test_exact_execution_rechecks_cached_closure_fingerprint(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
        )
        before = core.tree_digest_snapshot(fixture.source)
        session = core.TemplateImportSession.create(fixture.key, ["base"])
        verification = session.verifications[0]
        self.assertIsInstance(verification.cached_closure, core.ResolvedModClosure)
        cached = verification.cached_closure
        changed_metadata = replace(
            cached.metadata[0],
            contents=cached.metadata[0].contents + b"\n# changed after planning\n",
        )
        session.verifications = (
            replace(
                verification,
                cached_closure=core.ResolvedModClosure(
                    cached.root_identity,
                    (changed_metadata, *cached.metadata[1:]),
                ),
            ),
        )
        operation = core.TemplateImportOperation(
            session, resolve_template_import_plan(session.plan)
        )
        operation.run()
        self.assertIsNotNone(operation.error)
        self.assertIn("closure changed after planning", str(operation.error))
        self.assertEqual(core.tree_digest_snapshot(fixture.source), before)

    def test_execution_cleanup_failure_has_priority_and_remains_retryable(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
        )
        session = core.TemplateImportSession.create(fixture.key, ["base"])
        operation = core.TemplateImportOperation(
            session, resolve_template_import_plan(session.plan)
        )
        cleanup_error = core.TransactionDiscardIntegrityError(
            "process cleanup is incomplete"
        )
        with (
            patch.object(
                core,
                "_execute_exact_template_import",
                side_effect=core.HuroshikiError("candidate failed"),
            ),
            patch.object(session, "discard", side_effect=cleanup_error),
        ):
            operation.run()
        self.assertIs(operation.error, cleanup_error)
        self.assertFalse(operation._finished)
        self.assertFalse(session.finished)
        operation.discard()
        self.assertTrue(session.finished)

    def test_apply_cleanup_failure_retains_retryable_operation_ownership(self) -> None:
        fixture = self.exact_fixture()
        project_id = exact_selection.MR_PROJECT_IDS["root"]
        artifact_id = exact_selection.MR_VERSION_IDS["r1"]
        self.write_exact_template(
            fixture,
            provider="modrinth",
            project_id=project_id,
            artifact_id=artifact_id,
        )
        operation = self.run_exact_import(fixture)
        self.assertIsNone(operation.error)
        cleanup_error = core.TransactionDiscardIntegrityError(
            "apply cleanup incomplete"
        )
        with (
            patch.object(
                operation.transaction,
                "apply",
                side_effect=core.HuroshikiError("publication failed"),
            ),
            patch.object(operation.session, "discard", side_effect=cleanup_error),
        ):
            with self.assertRaises(core.TransactionDiscardIntegrityError):
                operation.apply()
        self.assertFalse(operation._finished)
        self.assertFalse(operation._applying)
        self.assertFalse(operation.session.finished)
        operation.discard()
        self.assertTrue(operation.session.finished)


if __name__ == "__main__":
    unittest.main()
