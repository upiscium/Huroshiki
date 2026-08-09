from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from textual.app import App
from textual.screen import Screen
from textual.widgets import DataTable, Input, TextArea

import huroshiki
import huroshiki_core as core


PROJECT = core.ProjectInfo(
    kind="pack",
    project_id="demo",
    display_name="Demo Pack",
    minecraft="1.21.1",
    loader="neoforge",
    loader_version="21.1.0",
    enabled=True,
)


def binding_keys(owner: type) -> set[str]:
    return {binding.key for binding in getattr(owner, "BINDINGS", ())}


def plain(value: object) -> str:
    return getattr(value, "plain", str(value))


class _ProjectLabelApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self) -> None:
        super().__init__()
        self.opened: list[tuple[str, str]] = []

    def on_mount(self) -> None:
        self.push_screen(huroshiki.ProjectScreen("pack:demo"))

    def open_content(self, project_key: str) -> None:
        self.opened.append(("content", project_key))

    def open_template_import(self, project_key: str) -> None:
        self.opened.append(("template", project_key))


class _ModalApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, screen: Screen) -> None:
        super().__init__()
        self.initial_screen = screen
        self.result: object = "unset"

    def on_mount(self) -> None:
        self.push_screen(self.initial_screen, self._received)

    def _received(self, result: object) -> None:
        self.result = result


class TuiKeyConsistencyTest(unittest.IsolatedAsyncioTestCase):
    def test_filter_clear_uses_ctrl_l_not_q(self) -> None:
        self.assertNotIn("q", binding_keys(huroshiki.FilterInput))
        self.assertIn("ctrl+l", binding_keys(huroshiki.FilterListScreen))
        self.assertNotIn("q", binding_keys(huroshiki.FilterListScreen))

    def test_primary_navigation_help_uses_q(self) -> None:
        expected = {
            huroshiki.MainMenuScreen: "q: quit",
            huroshiki.StateScreen: "q: main",
            huroshiki.ProjectScreen: "q: main",
            huroshiki.SettingsScreen: "q: project",
            huroshiki.ClientDistributionScreen: "q: settings",
            huroshiki.ContentScreen: "q: project",
            huroshiki.ContentPlanPreviewScreen: "q: discard",
            huroshiki.TemplateScreen: "q: project",
            huroshiki.TemplateImportSelectionScreen: "q: project",
            huroshiki.TemplateImportConflictScreen: "q: discard",
            huroshiki.TemplateImportSideConflictScreen: "q: options",
            huroshiki.TemplateImportExecutionScreen: "q: discard",
            huroshiki.TemplateCandidateScreen: "q: main",
            huroshiki.TemplateConflictScreen: "q: templates",
            huroshiki.InstallScreen: "q: project",
            huroshiki.InstalledModsScreen: "q: project",
            huroshiki.UpdateScreen: "q: project",
        }
        for screen, marker in expected.items():
            with self.subTest(screen=screen.__name__):
                help_text = screen.help_text
                self.assertIn(marker, help_text)
                self.assertNotIn("q: clear", help_text)

        self.assertIn("Ctrl+L: clear filter", huroshiki.MainMenuScreen.help_text)
        self.assertIn("Ctrl+L: clear filter", huroshiki.ContentScreen.help_text)
        self.assertIn("Ctrl+L: clear filter", huroshiki.TemplateScreen.help_text)
        self.assertIn("Ctrl+L: clear filter", huroshiki.InstalledModsScreen.help_text)

    def test_text_editors_and_forms_do_not_bind_q(self) -> None:
        for screen in (
            huroshiki.DeploymentSettingsScreen,
            huroshiki.VersionsScreen,
            huroshiki.ContentEditorScreen,
            huroshiki.TemplateEditorScreen,
            huroshiki.PublicPackUrlEditModal,
            huroshiki.NewPackModal,
            huroshiki.CreateFromTemplateModal,
            huroshiki.NewTemplateModal,
            huroshiki.ContentCreateModal,
            huroshiki.ContentMoveModal,
            huroshiki.ContentImportModal,
        ):
            with self.subTest(screen=screen.__name__):
                self.assertNotIn("q", binding_keys(screen))

    def test_modal_q_actions_are_cancel_or_close(self) -> None:
        self.assertEqual(
            {binding.key: binding.action for binding in huroshiki.ConfirmModal.BINDINGS}["q"],
            "cancel",
        )
        self.assertEqual(
            {binding.key: binding.action for binding in huroshiki.MessageModal.BINDINGS}["q"],
            "close",
        )
        self.assertEqual(
            {binding.key: binding.action for binding in huroshiki.ContentPathInfoModal.BINDINGS}["q"],
            "close",
        )

    def test_project_action_presentation_keeps_internal_identifiers(self) -> None:
        self.assertEqual(huroshiki.project_action_label("Content"), "content")
        self.assertEqual(huroshiki.project_action_label("Apply Template"), "apply template")
        self.assertEqual(huroshiki.project_action_label("build"), "build")

    async def test_project_action_rows_are_lowercase_and_dispatch_internal_values(self) -> None:
        with (
            patch.object(huroshiki.core, "project_info", return_value=PROJECT),
            patch.object(
                huroshiki.core,
                "project_actions",
                return_value=("build", "publish", "deploy", "restart"),
            ),
        ):
            app = _ProjectLabelApp()
            async with app.run_test() as pilot:
                screen = app.screen
                table = screen.query_one("#project-actions", DataTable)
                rows = [table.get_row_at(index) for index in range(table.row_count)]
                self.assertEqual(
                    [plain(row[0]) for row in rows],
                    [
                        "build",
                        "publish",
                        "deploy",
                        "restart",
                        "content",
                        "apply template",
                        "settings",
                    ],
                )

                with patch.object(app, "open_content") as open_content:
                    table.move_cursor(row=4)
                    screen.run_selected()
                    open_content.assert_called_once_with("pack:demo")

                with patch.object(app, "open_template_import") as open_template:
                    table.move_cursor(row=5)
                    screen.run_selected()
                    open_template.assert_called_once_with("pack:demo")

                await pilot.pause()


class ModalKeyBehaviorTest(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_q_dismisses_false(self) -> None:
        app = _ModalApp(huroshiki.ConfirmModal("Confirm", ("body",)))
        async with app.run_test() as pilot:
            await pilot.press("q")
            await pilot.pause()
        self.assertIs(app.result, False)

    async def test_message_q_closes(self) -> None:
        app = _ModalApp(huroshiki.MessageModal("Message", ("body",)))
        async with app.run_test() as pilot:
            await pilot.press("q")
            await pilot.pause()
        self.assertIsNone(app.result)


class TextInputKeyBehaviorTest(unittest.IsolatedAsyncioTestCase):
    async def test_q_is_literal_in_input_and_textarea_without_screen_binding(self) -> None:
        class InputApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(InputScreen())

        class InputScreen(Screen[None]):
            def compose(self):
                yield Input(id="input")
                yield TextArea(id="editor")

            def on_mount(self) -> None:
                self.query_one("#input", Input).focus()

        app = InputApp()
        async with app.run_test() as pilot:
            await pilot.press("q")
            self.assertEqual(app.screen.query_one("#input", Input).value, "q")
            app.screen.query_one("#editor", TextArea).focus()
            await pilot.press("q")
            self.assertEqual(app.screen.query_one("#editor", TextArea).text, "q")


if __name__ == "__main__":
    unittest.main()
