from __future__ import annotations

import unittest

from template_merge import (
    ConflictResolution,
    TemplateMergeError,
    TemplateModEntry,
    compose_templates,
    resolve_composition,
)


def entry(
    template_id: str,
    name: str,
    provider: str,
    project_id: str,
    side: str = "both",
    url: str | None = None,
) -> TemplateModEntry:
    return TemplateModEntry(template_id, name, provider, project_id, side, url)


class TemplateMergeTest(unittest.TestCase):
    def test_exact_identity_preserves_first_order_and_unions_sides(self) -> None:
        composition = compose_templates(
            ["base", "addon"],
            [
                entry("base", "First", "modrinth", "first", "client"),
                entry("base", "Shared", "modrinth", "shared", "client"),
                entry("addon", "Last", "curseforge", "3", "both"),
                entry("addon", "Shared renamed", "modrinth", "shared", "server"),
            ],
        )

        self.assertEqual(
            [mod.project_id for mod in composition.mods],
            ["first", "shared", "3"],
        )
        self.assertEqual(composition.mods[1].name, "Shared")
        self.assertEqual(composition.mods[1].side, "both")
        self.assertEqual(composition.mods[1].template_ids, ("base", "addon"))
        self.assertEqual(composition.conflicts, ())

    def test_architectury_moonlight_and_three_candidate_conflicts(self) -> None:
        composition = compose_templates(
            ["curse", "modrinth", "mirror"],
            [
                entry("curse", " Architectury API ", "curseforge", "419699"),
                entry("modrinth", "architectury api", "modrinth", "lhGA9TYQ"),
                entry("curse", "Moonlight Lib", "curseforge", "499980"),
                entry("modrinth", "MOONLIGHT LIB", "modrinth", "twkfQtEc"),
                entry(
                    "mirror",
                    "moonlight lib ",
                    "url",
                    "moonlight",
                    url="https://example.test/moonlight.jar",
                ),
            ],
        )

        self.assertEqual(
            [item.key for item in composition.conflicts],
            ["architectury api", "moonlight lib"],
        )
        self.assertEqual(len(composition.conflicts[1].candidates), 3)
        moonlight = composition.conflicts[1]
        selected = (
            moonlight.candidates[0].candidate_key,
            moonlight.candidates[2].candidate_key,
        )
        resolved = resolve_composition(
            composition,
            {
                "architectury api": ConflictResolution(
                    (composition.conflicts[0].candidates[1].candidate_key,)
                ),
                "moonlight lib": ConflictResolution(selected, True),
            },
        )

        self.assertEqual(
            [mod.project_id for mod in resolved.mods],
            ["lhGA9TYQ", "499980", "moonlight"],
        )
        self.assertEqual(len(resolved.conflict_selections), 2)
        self.assertEqual(len(resolved.warnings), 1)
        self.assertIn("duplicate MOD", resolved.warnings[0])

    def test_url_identity_merges_only_when_selector_matches(self) -> None:
        same = compose_templates(
            ["a", "b"],
            [
                entry(
                    "a", "Private", "url", "private", "client",
                    "https://a.test/private.jar",
                ),
                entry(
                    "b", "Private", "url", "private", "server",
                    "https://a.test/private.jar",
                ),
            ],
        )
        self.assertEqual(len(same.mods), 1)
        self.assertEqual(same.mods[0].side, "both")
        self.assertEqual(same.conflicts, ())

        changed = compose_templates(
            ["a", "b"],
            [
                entry("a", "Private", "url", "private", url="https://a.test/private.jar"),
                entry("b", "Renamed Private MOD", "url", "private", url="https://b.test/private.jar"),
            ],
        )
        self.assertEqual(len(changed.conflicts), 1)
        self.assertNotEqual(
            changed.conflicts[0].candidates[0].candidate_key,
            changed.conflicts[0].candidates[1].candidate_key,
        )

    def test_resolution_rejects_empty_unresolved_stale_and_unacknowledged_multiple(self) -> None:
        composition = compose_templates(
            ["a", "b"],
            [
                entry("a", "Same", "modrinth", "a"),
                entry("b", "same", "curseforge", "2"),
            ],
        )
        conflict = composition.conflicts[0]
        cases = [
            (None, "Unresolved"),
            ({"same": ConflictResolution(())}, "at least one"),
            ({"stale": ConflictResolution(("modrinth:a",))}, "Unknown or stale"),
            ({"same": ConflictResolution(("missing",))}, "unknown or stale candidate"),
            (
                {
                    "same": ConflictResolution(
                        tuple(item.candidate_key for item in conflict.candidates)
                    )
                },
                "without acknowledging",
            ),
        ]
        for resolutions, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                TemplateMergeError, message
            ):
                resolve_composition(composition, resolutions)

    def test_empty_and_duplicate_template_selections_are_rejected(self) -> None:
        with self.assertRaisesRegex(TemplateMergeError, "At least one"):
            compose_templates([], [])
        with self.assertRaisesRegex(TemplateMergeError, "duplicate"):
            compose_templates(["a", "a"], [])


if __name__ == "__main__":
    unittest.main()
