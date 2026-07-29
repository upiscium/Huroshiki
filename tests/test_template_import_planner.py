from __future__ import annotations

from pathlib import Path
import unittest

from template_import import (
    ImportCandidateVerification,
    ImportConflictResolution,
    ModCandidate,
    TemplateCompatibility,
    build_template_import_plan,
    import_selection_options,
    candidate_from_template_entry,
    merge_template_import_candidates,
    resolve_template_import_plan,
    template_candidate,
)
from template_merge import TemplateMergeError, TemplateModEntry


def pack_candidate(
    name: str,
    project_id: str,
    side: str = "both",
    provider: str = "modrinth",
) -> ModCandidate:
    return ModCandidate(
        "pack",
        "demo",
        name,
        provider,
        project_id,
        side,
        Path(f"mods/{project_id}.pw.toml"),
        f"{project_id}.jar",
        actual_provider=provider,
        actual_project_id=project_id,
    )


def build(
    templates: list[str],
    pack: list[ModCandidate],
    candidates: list[ModCandidate],
):
    merged = merge_template_import_candidates(
        [
            candidate
            for template in templates
            for candidate in candidates
            if candidate.origin_id == template
        ]
    )
    verifications = tuple(
        ImportCandidateVerification(
            candidate.selector_identity,
            candidate.actual_identity or candidate.logical_identity,
            candidate.metadata_path,
            candidate.filename,
            "fingerprint" if candidate.provider == "url" else None,
            None,
        )
        for candidate in merged
    )
    verified_candidates = [
        candidate
        if candidate.actual_identity is not None
        else candidate.__class__(
            **{
                **candidate.__dict__,
                "actual_provider": candidate.provider,
                "actual_project_id": candidate.project_id,
            }
        )
        for candidate in candidates
    ]
    return build_template_import_plan(
        pack_key="pack:demo",
        pack_minecraft="1.21.1",
        pack_loader="neoforge",
        template_ids=templates,
        compatibilities={
            item: TemplateCompatibility(item, "1.21.1", "neoforge")
            for item in templates
        },
        pack_candidates=pack,
        template_candidates=verified_candidates,
        verifications=verifications,
    )


class TemplateImportPlannerTest(unittest.TestCase):
    def test_selection_options_group_only_verified_equivalent_sources(self) -> None:
        installed = pack_candidate("Installed", "shared")
        equivalent = template_candidate(
            "base",
            name="Equivalent",
            provider="modrinth",
            project_id="shared",
            side="both",
            actual_provider="modrinth",
            actual_project_id="shared",
        )
        changed = template_candidate(
            "base",
            name="Changed",
            provider="modrinth",
            project_id="shared",
            side="both",
            actual_provider="modrinth",
            actual_project_id="changed",
        )
        failed = template_candidate(
            "base",
            name="Failed",
            provider="url",
            project_id="failed",
            side="both",
            url="https://mods.example/failed.jar",
        )
        options = import_selection_options((installed, equivalent, changed, failed))
        self.assertEqual(len(options), 3)
        grouped = next(option for option in options if len(option.candidates) == 2)
        self.assertTrue(grouped.option_key.startswith("group:"))
        self.assertEqual(grouped.candidates, (installed, equivalent))
        self.assertEqual(grouped.selector_identity, installed.selector_identity)
        self.assertEqual(grouped.actual_identity, installed.actual_identity)
        singleton_keys = {
            option.option_key for option in options if len(option.candidates) == 1
        }
        self.assertEqual(
            singleton_keys,
            {changed.selection_key, failed.selection_key},
        )

    def logical_divergence(self, *, same_name: bool = False):
        installed = pack_candidate("Installed", "logical", provider="url")
        installed = installed.__class__(
            **{
                **installed.__dict__,
                "url": "https://mods.example/old.jar",
            }
        )
        incoming = template_candidate(
            "base",
            name="Installed" if same_name else "Replacement",
            provider="url",
            project_id="logical",
            side="both",
            url="https://mods.example/new.jar",
            actual_provider="url",
            actual_project_id="actual-new",
        )
        return installed, incoming, build(["base"], [installed], [incoming])

    def test_template_entry_adapter_preserves_candidate_identity(self) -> None:
        candidate = candidate_from_template_entry(
            TemplateModEntry(
                "base",
                "Private",
                "url",
                "private",
                "client",
                "https://mods.example/private.jar",
            )
        )
        self.assertEqual(candidate.origin_kind, "template")
        self.assertEqual(candidate.logical_identity, ("url", "private"))
        self.assertEqual(
            candidate.selector_identity,
            ("url", "private", "https://mods.example/private.jar"),
        )
        self.assertEqual(candidate.url, "https://mods.example/private.jar")

    def test_pack_and_template_same_candidate_key_have_unique_selection_keys(self) -> None:
        url = "https://mods.example/mod.jar"
        installed = pack_candidate("Installed", "logical", provider="url")
        installed = installed.__class__(**{**installed.__dict__, "url": url})
        incoming = template_candidate(
            "base",
            name="Replacement",
            provider="url",
            project_id="logical",
            side="both",
            url=url,
            actual_provider="url",
            actual_project_id="new-id",
        )
        plan = build(["base"], [installed], [incoming])
        pack = plan.pack_candidates[0]
        template = plan.template_candidates[0]
        self.assertEqual(pack.candidate_key, template.candidate_key)
        self.assertNotEqual(pack.selection_key, template.selection_key)
        conflict = plan.logical_identity_conflicts[0]
        keep = resolve_template_import_plan(
            plan,
            logical_identity_resolutions={
                conflict.key: ImportConflictResolution((pack.selection_key,))
            },
        )
        self.assertEqual(keep.removed_pack_candidates, ())
        self.assertEqual(keep.selected_new_roots, ())
        replace_plan = resolve_template_import_plan(
            plan,
            logical_identity_resolutions={
                conflict.key: ImportConflictResolution((template.selection_key,))
            },
        )
        self.assertEqual(replace_plan.removed_pack_candidates, (pack,))
        self.assertEqual(replace_plan.selected_new_roots, (template,))

    def test_single_and_multiple_templates_preserve_selection_order(self) -> None:
        candidates = [
            template_candidate(
                "second", name="Second", provider="modrinth", project_id="second", side="both"
            ),
            template_candidate(
                "first", name="First", provider="modrinth", project_id="first", side="both"
            ),
        ]
        single = build(["first"], [], [candidates[1]])
        multiple = build(["first", "second"], [], candidates)
        self.assertEqual([item.project_id for item in single.new_roots], ["first"])
        self.assertEqual(
            [item.project_id for item in multiple.new_roots],
            ["first", "second"],
        )
        self.assertEqual(multiple.template_ids, ("first", "second"))

    def test_incompatible_minecraft_or_loader_is_rejected(self) -> None:
        for compatibility in (
            TemplateCompatibility("base", "1.20.1", "neoforge"),
            TemplateCompatibility("base", "1.21.1", "fabric"),
        ):
            with self.subTest(compatibility=compatibility), self.assertRaisesRegex(
                TemplateMergeError, "incompatible"
            ):
                build_template_import_plan(
                    pack_key="pack:demo",
                    pack_minecraft="1.21.1",
                    pack_loader="neoforge",
                    template_ids=["base"],
                    compatibilities={"base": compatibility},
                    pack_candidates=[],
                    template_candidates=[],
                    verifications=[],
                )

    def test_same_identity_same_side_is_unchanged(self) -> None:
        installed = pack_candidate("Shared", "shared", "client")
        plan = build(
            ["base"],
            [installed],
            [
                template_candidate(
                    "base",
                    name="Renamed Shared",
                    provider="modrinth",
                    project_id="shared",
                    side="client",
                )
            ],
        )
        self.assertEqual(plan.new_roots, ())
        self.assertEqual(plan.existing_identities, (installed,))
        self.assertEqual(plan.side_conflicts, ())

    def test_side_conflict_supports_all_three_decisions(self) -> None:
        installed = pack_candidate("Shared", "shared", "client")
        incoming = template_candidate(
            "base",
            name="Shared",
            provider="modrinth",
            project_id="shared",
            side="server",
        )
        plan = build(["base"], [installed], [incoming])
        identity = ("modrinth", "shared")
        expected = {
            "keep_pack": (),
            "use_template": ((identity, "client", "server"),),
            "union": ((identity, "client", "both"),),
        }
        for decision, changes in expected.items():
            with self.subTest(decision=decision):
                resolved = resolve_template_import_plan(
                    plan,
                    name_resolutions={},
                    side_decisions={identity: decision},
                )
                self.assertEqual(resolved.side_changes, changes)

    def test_pack_candidate_participates_in_name_conflict(self) -> None:
        installed = pack_candidate("Architectury API", "old")
        incoming = template_candidate(
            "base",
            name="architectury api",
            provider="curseforge",
            project_id="419699",
            side="both",
        )
        plan = build(["base"], [installed], [incoming])
        conflict = plan.name_conflicts[0]
        self.assertEqual(
            [item.origin_kind for item in conflict.candidates],
            ["pack", "template"],
        )
        resolved = resolve_template_import_plan(
            plan,
            name_resolutions={
                conflict.key: ImportConflictResolution((incoming.selection_key,))
            },
        )
        self.assertEqual(
            [item.candidate_key for item in resolved.selected_new_roots],
            [incoming.candidate_key],
        )
        self.assertEqual(resolved.removed_pack_candidates, (installed,))

    def test_three_candidates_require_duplicate_acknowledgement(self) -> None:
        candidates = [
            template_candidate(
                template,
                name="Moonlight Lib",
                provider=provider,
                project_id=project_id,
                side="both",
            )
            for template, provider, project_id in (
                ("a", "modrinth", "a"),
                ("b", "curseforge", "2"),
                ("c", "modrinth", "c"),
            )
        ]
        plan = build(["a", "b", "c"], [], candidates)
        conflict = plan.name_conflicts[0]
        selected = (candidates[0].selection_key, candidates[2].selection_key)
        with self.assertRaisesRegex(TemplateMergeError, "acknowledging"):
            resolve_template_import_plan(
                plan,
                name_resolutions={conflict.key: ImportConflictResolution(selected)},
            )
        resolved = resolve_template_import_plan(
            plan,
            name_resolutions={conflict.key: ImportConflictResolution(selected, True)},
        )
        self.assertEqual(
            [item.project_id for item in resolved.selected_new_roots], ["a", "c"]
        )
        self.assertEqual(len(resolved.warnings), 1)

    def test_nfc_nfd_names_share_stable_conflict_key(self) -> None:
        installed = pack_candidate("Caf\N{LATIN SMALL LETTER E WITH ACUTE}", "installed")
        incoming = template_candidate(
            "base",
            name="Cafe\N{COMBINING ACUTE ACCENT}",
            provider="curseforge",
            project_id="2",
            side="both",
        )
        first = build(["base"], [installed], [incoming])
        second = build(["base"], [installed], [incoming])
        self.assertEqual(first.name_conflicts[0].key, "caf\N{LATIN SMALL LETTER E WITH ACUTE}")
        self.assertEqual(first.name_conflicts, second.name_conflicts)
        self.assertEqual(first.plan_digest, second.plan_digest)

    def test_different_unicode_names_do_not_conflict(self) -> None:
        plan = build(
            ["base"],
            [pack_candidate("Sm\N{LATIN SMALL LETTER O WITH STROKE}r", "installed")],
            [
                template_candidate(
                    "base",
                    name="Smor",
                    provider="curseforge",
                    project_id="2",
                    side="both",
                )
            ],
        )
        self.assertEqual(plan.name_conflicts, ())

    def test_template_shared_identity_is_merged_in_order_with_side_union(self) -> None:
        first = template_candidate(
            "a", name="Shared", provider="modrinth", project_id="shared", side="client"
        )
        second = template_candidate(
            "b", name="Shared", provider="modrinth", project_id="shared", side="server"
        )
        plan = build(["a", "b"], [], [second, first])
        self.assertEqual(len(plan.new_roots), 1)
        self.assertEqual(plan.new_roots[0].origin_id, "a")
        self.assertEqual(plan.new_roots[0].side, "both")

    def test_same_url_selector_merges_with_conservative_policy(self) -> None:
        first = template_candidate(
            "a",
            name="Private",
            provider="url",
            project_id="private",
            side="client",
            url="https://mods.example/private.jar",
            url_max_jar_size_bytes=20,
            url_allow_private_networks=True,
        )
        second = template_candidate(
            "b",
            name="Private",
            provider="url",
            project_id="private",
            side="server",
            url="https://mods.example/private.jar",
            url_max_jar_size_bytes=10,
            url_allow_private_networks=False,
        )
        plan = build(["a", "b"], [], [first, second])
        self.assertEqual(len(plan.new_roots), 1)
        self.assertEqual(plan.new_roots[0].side, "both")
        self.assertEqual(plan.new_roots[0].url_max_jar_size_bytes, 10)
        self.assertFalse(plan.new_roots[0].url_allow_private_networks)
        self.assertEqual(plan.url_selector_conflicts, ())

    def test_same_url_logical_id_with_different_urls_requires_resolution(self) -> None:
        candidates = [
            template_candidate(
                template,
                name=name,
                provider="url",
                project_id="private",
                side="both",
                url=url,
                actual_provider="url",
                actual_project_id=f"actual-{template}",
            )
            for template, name, url in (
                ("a", "First", "https://a.example/private.jar"),
                ("b", "Second", "https://b.example/private.jar"),
            )
        ]
        plan = build(["a", "b"], [], candidates)
        self.assertEqual(len(plan.new_roots), 2)
        self.assertEqual(plan.url_selector_conflicts[0].key, "url:private")
        with self.assertRaisesRegex(TemplateMergeError, "URL selector"):
            resolve_template_import_plan(plan)
        with self.assertRaisesRegex(TemplateMergeError, "acknowledging"):
            resolve_template_import_plan(
                plan,
                url_selector_resolutions={
                    "url:private": ImportConflictResolution(
                        tuple(candidate.selection_key for candidate in candidates)
                    )
                },
            )
        resolved = resolve_template_import_plan(
            plan,
            url_selector_resolutions={
                "url:private": ImportConflictResolution((candidates[0].selection_key,))
            },
        )
        self.assertEqual(
            [item.candidate_key for item in resolved.selected_new_roots],
            [candidates[0].candidate_key],
        )

    def test_url_selector_order_and_policy_change_plan_digest(self) -> None:
        first = template_candidate(
            "a",
            name="Private",
            provider="url",
            project_id="private",
            side="both",
            url="https://a.example/private.jar",
            url_max_jar_size_bytes=10,
        )
        second = template_candidate(
            "b",
            name="Private B",
            provider="url",
            project_id="private",
            side="both",
            url="https://b.example/private.jar",
        )
        base = build(["a", "b"], [], [first, second])
        reordered = build(["b", "a"], [], [first, second])
        changed = build(
            ["a", "b"],
            [],
            [first.__class__(**{**first.__dict__, "url_max_jar_size_bytes": 11}), second],
        )
        self.assertNotEqual(base.plan_digest, reordered.plan_digest)
        self.assertNotEqual(base.plan_digest, changed.plan_digest)

    def test_actual_identity_conflict_requires_exactly_one_candidate(self) -> None:
        installed = pack_candidate("Installed", "actual", provider="url")
        installed = installed.__class__(
            **{
                **installed.__dict__,
                "url": "https://mods.example/installed.jar",
                "actual_provider": "url",
                "actual_project_id": "actual",
            }
        )
        incoming = template_candidate(
            "base",
            name="Requested",
            provider="url",
            project_id="logical",
            side="both",
            url="https://mods.example/requested.jar",
            actual_provider="url",
            actual_project_id="actual",
        )
        plan = build(["base"], [installed], [incoming])
        self.assertEqual(plan.new_roots, ())
        self.assertEqual(plan.actual_identity_conflicts[0].key, "url:actual")
        with self.assertRaisesRegex(TemplateMergeError, "actual identity"):
            resolve_template_import_plan(plan)
        resolved = resolve_template_import_plan(
            plan,
            actual_identity_resolutions={
                "url:actual": ImportConflictResolution((incoming.selection_key,))
            },
        )
        self.assertEqual(resolved.selected_new_roots, (incoming,))
        self.assertEqual(resolved.removed_pack_candidates, (installed,))
        self.assertEqual(
            [item.candidate_key for item in resolved.selected_new_roots],
            [incoming.candidate_key],
        )

    def test_logical_divergence_requires_explicit_replacement(self) -> None:
        installed, incoming, plan = self.logical_divergence()
        self.assertEqual(plan.logical_identity_conflicts[0].key, "url:logical")
        self.assertEqual(
            [item.candidate_key for item in plan.new_roots],
            [incoming.candidate_key],
        )
        keep = resolve_template_import_plan(
            plan,
            logical_identity_resolutions={
                "url:logical": ImportConflictResolution((installed.selection_key,))
            },
        )
        self.assertEqual(keep.selected_new_roots, ())
        self.assertEqual(keep.removed_pack_candidates, ())
        replace_plan = resolve_template_import_plan(
            plan,
            logical_identity_resolutions={
                "url:logical": ImportConflictResolution((incoming.selection_key,))
            },
        )
        self.assertEqual(
            [item.candidate_key for item in replace_plan.selected_new_roots],
            [incoming.candidate_key],
        )
        self.assertEqual(replace_plan.removed_pack_candidates, (installed,))

    def test_different_names_still_create_logical_conflict(self) -> None:
        _installed, _incoming, plan = self.logical_divergence(same_name=False)
        self.assertEqual(len(plan.logical_identity_conflicts), 1)
        self.assertEqual(plan.name_conflicts, ())

    def test_name_and_actual_resolution_disagreement_is_rejected(self) -> None:
        installed = pack_candidate("Same", "shared", provider="url")
        installed = installed.__class__(
            **{**installed.__dict__, "url": "https://mods.example/old.jar"}
        )
        incoming = template_candidate(
            "base",
            name="same",
            provider="url",
            project_id="incoming",
            side="both",
            url="https://mods.example/new.jar",
            actual_provider="url",
            actual_project_id="shared",
        )
        plan = build(["base"], [installed], [incoming])
        with self.assertRaisesRegex(TemplateMergeError, "selected by name conflict"):
            resolve_template_import_plan(
                plan,
                name_resolutions={
                    "same": ImportConflictResolution((installed.selection_key,))
                },
                actual_identity_resolutions={
                    "url:shared": ImportConflictResolution((incoming.selection_key,))
                },
            )
        resolved = resolve_template_import_plan(
            plan,
            name_resolutions={
                "same": ImportConflictResolution((incoming.selection_key,))
            },
            actual_identity_resolutions={
                "url:shared": ImportConflictResolution((incoming.selection_key,))
            },
        )
        self.assertEqual(resolved.removed_pack_candidates, (installed,))

    def test_name_and_url_resolution_disagreement_is_rejected(self) -> None:
        candidates = [
            template_candidate(
                template,
                name="Same",
                provider="url",
                project_id="logical",
                side="both",
                url=url,
                actual_provider="url",
                actual_project_id=f"actual-{template}",
            )
            for template, url in (
                ("a", "https://mods.example/a.jar"),
                ("b", "https://mods.example/b.jar"),
            )
        ]
        plan = build(["a", "b"], [], candidates)
        with self.assertRaisesRegex(TemplateMergeError, "selected by name conflict"):
            resolve_template_import_plan(
                plan,
                name_resolutions={
                    "same": ImportConflictResolution((candidates[0].selection_key,))
                },
                url_selector_resolutions={
                    "url:logical": ImportConflictResolution((candidates[1].selection_key,))
                },
            )

    def test_logical_and_actual_resolution_disagreement_is_rejected(self) -> None:
        logical_pack = pack_candidate("Logical", "logical", provider="url")
        logical_pack = logical_pack.__class__(
            **{**logical_pack.__dict__, "url": "https://mods.example/logical.jar"}
        )
        actual_pack = pack_candidate("Actual", "shared", provider="url")
        actual_pack = actual_pack.__class__(
            **{**actual_pack.__dict__, "url": "https://mods.example/shared.jar"}
        )
        incoming = template_candidate(
            "base",
            name="Incoming",
            provider="url",
            project_id="logical",
            side="both",
            url="https://mods.example/incoming.jar",
            actual_provider="url",
            actual_project_id="shared",
        )
        plan = build(["base"], [logical_pack, actual_pack], [incoming])
        with self.assertRaisesRegex(TemplateMergeError, "logical identity conflict"):
            resolve_template_import_plan(
                plan,
                logical_identity_resolutions={
                    "url:logical": ImportConflictResolution((incoming.selection_key,))
                },
                actual_identity_resolutions={
                    "url:shared": ImportConflictResolution((actual_pack.selection_key,))
                },
            )

    def test_pack_candidate_cannot_be_removed_without_template_replacement(self) -> None:
        first = pack_candidate("Same", "first")
        second = pack_candidate("same", "second")
        plan = build(["base"], [first, second], [])
        with self.assertRaisesRegex(TemplateMergeError, "no selected replacement"):
            resolve_template_import_plan(
                plan,
                name_resolutions={
                    "same": ImportConflictResolution((first.selection_key,))
                },
            )

    def test_selected_existing_template_identity_cannot_justify_removal(self) -> None:
        removed = pack_candidate("Same", "removed")
        retained = pack_candidate("same", "retained")
        incoming = template_candidate(
            "base",
            name="Same",
            provider="modrinth",
            project_id="retained",
            side="both",
        )
        plan = build(["base"], [removed, retained], [incoming])
        with self.assertRaisesRegex(TemplateMergeError, "no selected replacement"):
            resolve_template_import_plan(
                plan,
                name_resolutions={
                    "same": ImportConflictResolution((retained.selection_key,))
                },
            )

    def test_removed_identity_does_not_receive_side_change(self) -> None:
        installed = pack_candidate("Same", "shared", side="client")
        same_identity = template_candidate(
            "base",
            name="same",
            provider="modrinth",
            project_id="shared",
            side="server",
        )
        replacement = template_candidate(
            "base",
            name="Same",
            provider="curseforge",
            project_id="2",
            side="both",
        )
        plan = build(["base"], [installed], [same_identity, replacement])
        resolved = resolve_template_import_plan(
            plan,
            name_resolutions={
                "same": ImportConflictResolution((replacement.selection_key,))
            },
            side_decisions={("modrinth", "shared"): "use_template"},
        )
        self.assertEqual(resolved.side_changes, ())
        self.assertEqual(resolved.removed_pack_candidates, (installed,))

    def test_actual_identity_changes_plan_digest(self) -> None:
        candidate = template_candidate(
            "base",
            name="Requested",
            provider="url",
            project_id="logical",
            side="both",
            url="https://mods.example/requested.jar",
            actual_provider="url",
            actual_project_id="first",
        )
        first = build(["base"], [], [candidate])
        second = build(
            ["base"],
            [],
            [
                candidate.__class__(
                    **{**candidate.__dict__, "actual_project_id": "second"}
                )
            ],
        )
        self.assertNotEqual(first.plan_digest, second.plan_digest)

    def test_two_urls_resolving_to_same_actual_identity_fail_closed(self) -> None:
        candidates = [
            template_candidate(
                template,
                name=name,
                provider="url",
                project_id=logical_id,
                side="both",
                url=url,
                actual_provider="url",
                actual_project_id="shared_actual",
            )
            for template, name, logical_id, url in (
                ("a", "First", "first", "https://a.example/first.jar"),
                ("b", "Second", "second", "https://b.example/second.jar"),
            )
        ]
        plan = build(["a", "b"], [], candidates)
        self.assertEqual(plan.actual_identity_conflicts[0].key, "url:shared_actual")
        with self.assertRaisesRegex(TemplateMergeError, "actual identity"):
            resolve_template_import_plan(plan)
        with self.assertRaisesRegex(TemplateMergeError, "exactly one"):
            resolve_template_import_plan(
                plan,
                actual_identity_resolutions={
                    "url:shared_actual": ImportConflictResolution(
                        tuple(candidate.selection_key for candidate in candidates),
                        True,
                    )
                },
            )

    def test_plan_digest_changes_with_template_order_or_candidate_data(self) -> None:
        a = template_candidate(
            "a", name="A", provider="modrinth", project_id="a", side="both"
        )
        b = template_candidate(
            "b", name="B", provider="modrinth", project_id="b", side="both"
        )
        first = build(["a", "b"], [], [a, b])
        reordered = build(["b", "a"], [], [a, b])
        changed = build(["a", "b"], [], [a, b.__class__(**{**b.__dict__, "side": "client"})])
        self.assertNotEqual(first.plan_digest, reordered.plan_digest)
        self.assertNotEqual(first.plan_digest, changed.plan_digest)

    def test_unresolved_and_stale_resolutions_fail_closed(self) -> None:
        plan = build(
            ["base"],
            [pack_candidate("Same", "installed")],
            [
                template_candidate(
                    "base",
                    name="same",
                    provider="curseforge",
                    project_id="2",
                    side="both",
                )
            ],
        )
        with self.assertRaisesRegex(TemplateMergeError, "Unresolved"):
            resolve_template_import_plan(plan)
        with self.assertRaisesRegex(TemplateMergeError, "stale"):
            resolve_template_import_plan(
                plan,
                name_resolutions={
                    "stale": ImportConflictResolution(("template:curseforge:2",))
                },
            )


if __name__ == "__main__":
    unittest.main()
