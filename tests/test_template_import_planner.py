from __future__ import annotations

from pathlib import Path
import unittest

from template_import import (
    ModCandidate,
    TemplateCompatibility,
    build_template_import_plan,
    candidate_from_template_entry,
    resolve_template_import_plan,
    template_candidate,
)
from template_merge import ConflictResolution, TemplateMergeError, TemplateModEntry


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
    )


def build(
    templates: list[str],
    pack: list[ModCandidate],
    candidates: list[ModCandidate],
):
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
        template_candidates=candidates,
    )


class TemplateImportPlannerTest(unittest.TestCase):
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
        self.assertEqual(candidate.identity, ("url", "private"))
        self.assertEqual(candidate.url, "https://mods.example/private.jar")

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
                conflict.key: ConflictResolution((incoming.candidate_key,))
            },
        )
        self.assertEqual(resolved.selected_new_roots, (incoming,))
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
        selected = (candidates[0].candidate_key, candidates[2].candidate_key)
        with self.assertRaisesRegex(TemplateMergeError, "acknowledging"):
            resolve_template_import_plan(
                plan,
                name_resolutions={conflict.key: ConflictResolution(selected)},
            )
        resolved = resolve_template_import_plan(
            plan,
            name_resolutions={conflict.key: ConflictResolution(selected, True)},
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
                name_resolutions={"stale": ConflictResolution(("curseforge:2",))},
            )


if __name__ == "__main__":
    unittest.main()
