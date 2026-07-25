from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import DataTable, Static

import huroshiki
import huroshiki_core as core
from template_merge import TemplateModEntry, compose_templates


VALUES = {
    "project_id": "generated",
    "display_name": "Generated",
    "minecraft": "1.21.1",
    "loader": "neoforge",
    "loader_version": "21.1.999",
}


def template(template_id: str) -> core.ProjectInfo:
    return core.ProjectInfo(
        kind="template",
        project_id=template_id,
        display_name=template_id.title(),
        minecraft="1.21.1",
        loader="neoforge",
        loader_version="21.1.234",
        enabled=True,
        mod_count=1,
    )


class _CandidateApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self) -> None:
        super().__init__()
        self.opened: str | None = None
        self.went_main = False

    def on_mount(self) -> None:
        self.push_screen(huroshiki.TemplateCandidateScreen(dict(VALUES)))

    def open_project(self, project_key: str) -> None:
        self.opened = project_key

    def go_main(self) -> None:
        self.went_main = True


class TemplateCompositionInteractionTest(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_selection_count_clear_and_stable_order(self) -> None:
        candidates = [template("alpha"), template("beta"), template("gamma")]
        composition = compose_templates(["gamma", "beta"], [])
        with (
            patch.object(huroshiki.core, "compatible_templates", return_value=candidates),
            patch.object(huroshiki.core, "prepare_template_composition", return_value=composition),
            patch.object(huroshiki.TemplateCandidateScreen, "finish_creation") as finish,
        ):
            app = _CandidateApp()
            async with app.run_test() as pilot:
                screen = app.screen
                table = screen.query_one("#template-candidate-table", DataTable)
                self.assertEqual(table.row_count, 3)
                self.assertTrue(all(row[0] == "[ ]" for row in (table.get_row_at(i) for i in range(3))))

                await pilot.press("space", "j", "space")
                await pilot.pause()
                self.assertEqual(screen.selected_template_ids, ["alpha", "beta"])
                self.assertIn("2", str(screen.query_one("#template-selected-count", Static).content))

                await pilot.press("q")
                await pilot.pause()
                self.assertEqual(screen.selected_template_ids, [])

                await pilot.press("j", "space", "k", "space", "enter")
                await pilot.pause()
                self.assertEqual(screen.selected_template_ids, ["gamma", "beta"])
                self.assertEqual(
                    finish.call_args.args[0]["template_ids"],
                    ["gamma", "beta"],
                )

    async def test_conflict_screen_blocks_creation_and_acknowledges_general_subset(self) -> None:
        candidates = [template("one"), template("two"), template("three")]
        entries = [
            TemplateModEntry("one", "Moonlight Lib", "curseforge", "499980", "both"),
            TemplateModEntry("two", " moonlight lib ", "modrinth", "twkfQtEc", "both"),
            TemplateModEntry(
                "three",
                "MOONLIGHT LIB",
                "url",
                "moonlight",
                "both",
                "https://example.test/moonlight.jar",
            ),
        ]
        composition = compose_templates(["one", "two", "three"], entries)
        report = core.TemplateCreationReport(
            "pack:generated",
            ("one", "two", "three"),
            (),
            (),
            ("Moonlight Lib",),
            (),
        )
        with (
            patch.object(huroshiki.core, "compatible_templates", return_value=candidates),
            patch.object(huroshiki.core, "prepare_template_composition", return_value=composition),
            patch.object(huroshiki.core, "create_pack_from_templates", return_value=report) as create,
        ):
            app = _CandidateApp()
            with patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test() as pilot:
                    await pilot.press("space", "j", "space", "j", "space", "enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, huroshiki.TemplateConflictScreen)
                    create.assert_not_called()
                    screen = app.screen

                    await pilot.press("space")
                    await pilot.pause()
                    self.assertEqual(len(screen.selected["moonlight lib"]), 1)

                    await pilot.press("j", "space", "j", "space")
                    await pilot.pause()
                    self.assertEqual(len(screen.selected["moonlight lib"]), 3)
                    self.assertIn(
                        "WARNING",
                        str(screen.query_one("#conflict-warning", Static).content),
                    )

                    await pilot.press("k", "space", "enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                    create.assert_not_called()

                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, huroshiki.TemplateConflictScreen)
                    create.assert_not_called()

                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, huroshiki.ConfirmModal)

                    await pilot.press("enter")
                    await pilot.pause()
                    create.assert_called_once()
                    resolution = create.call_args.kwargs["conflict_resolutions"]["moonlight lib"]
                    self.assertEqual(len(resolution.candidate_keys), 2)
                    self.assertTrue(resolution.acknowledge_duplicate_risk)


if __name__ == "__main__":
    unittest.main()
