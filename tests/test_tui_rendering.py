from __future__ import annotations

from pathlib import Path
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rich.text import Text
from textual.app import App
from textual.widgets import DataTable

import huroshiki
import huroshiki_core as core
from template_import import (
    CandidateNameConflict,
    ImportSelectionOption,
    TemplateImportPlan,
    template_candidate,
)
from template_merge import MergedTemplateMod, TemplateConflict, TemplateComposition


WIDE_LABEL = "界"


def _wide_cell(text: str) -> Text:
    return Text(text)


def _assert_checkbox_cell(
    testcase: unittest.TestCase,
    marker: object,
    wide: str,
) -> None:
    testcase.assertIsInstance(marker, Text)
    marker_text = marker
    width = marker_text.cell_len + 1 + _wide_cell(wide).cell_len
    testcase.assertIn(marker_text.plain, ("[ ]", "[x]"))
    testcase.assertEqual(marker_text.cell_len, 3)
    testcase.assertEqual((marker_text + " " + _wide_cell(wide)).cell_len, width)


class _ScreenApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, screen) -> None:
        super().__init__()
        self._start_screen = screen
        self.transactions: dict[str, object] = {}
        self._transaction_discard_timer = None
        self._shutting_down = False
        self.update_apply_workers: dict[
            str, tuple[threading.Thread, threading.Event, threading.Event | None]
        ] = {}
        self.content_workers: dict[str, object] = {}

    def open_project(self, project_key: str) -> bool:
        return False

    def go_main(self) -> None:
        return None

    def on_mount(self) -> None:
        self.push_screen(self._start_screen)


class _FakeDiscardOperation:
    def __init__(self, transaction: object) -> None:
        self.transaction = transaction
        self.done = threading.Event()
        self.done.set()

    def start(self) -> None:
        self.done.set()

    def raise_for_error(self) -> None:
        return None


class _FakePackTransaction:
    def __init__(self) -> None:
        self.active = True

    def begin_discard(self) -> _FakeDiscardOperation:
        return _FakeDiscardOperation(self)

    def discard(self, *args, **kwargs) -> None:  # pragma: no cover - safety fallback
        self.active = False


class _FakeTemplateImportSession:
    def __init__(self, plan: TemplateImportPlan) -> None:
        self.plan = plan

    def discard(self) -> None:
        self.plan = None  # type: ignore[assignment]


class _FakeUpdatePreparationOperation:
    candidates: tuple[core.UpdateCandidate, ...] = ()

    def __init__(self, project_key: str, *, deadline: float | None = None) -> None:
        self.project_key = project_key
        self.deadline = deadline
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.cancelled = False
        self.candidates = self.__class__.candidates

    def run(self) -> None:
        self.done.set()

    def cancel(self, *, deadline=None) -> None:
        self.cancel_event.set()
        self.cancelled = True
        self.done.set()

    def claim_transaction(self) -> core.PackTransaction:
        return SimpleNamespace(begin_discard=_FakePackTransaction().begin_discard, active=True)

    def drain_progress(self) -> tuple[core.UpdateProgress, ...]:
        return ()


class CheckboxMarkerTest(unittest.TestCase):
    def test_marker_returns_fresh_text_and_expected_plain(self) -> None:
        checked = huroshiki.checkbox_marker(True)
        unchecked = huroshiki.checkbox_marker(False)
        self.assertIsInstance(checked, Text)
        self.assertIsInstance(unchecked, Text)
        self.assertIsNot(checked, unchecked)
        self.assertIsNot(huroshiki.checkbox_marker(True), checked)
        self.assertIsNot(huroshiki.checkbox_marker(False), unchecked)
        self.assertEqual(checked.plain, "[x]")
        self.assertEqual(unchecked.plain, "[ ]")

    def test_marker_preserves_text_width(self) -> None:
        for state, expected in ((True, "[x]"), (False, "[ ]")):
            marker = huroshiki.checkbox_marker(state)
            self.assertIsInstance(marker, Text)
            self.assertEqual(marker.plain, expected)
            self.assertEqual(marker.cell_len, 3)

    def test_marker_text_is_not_markup_parsed(self) -> None:
        checked = huroshiki.checkbox_marker(True)
        unchecked = huroshiki.checkbox_marker(False)
        self.assertEqual(checked.plain, "[x]")
        self.assertEqual(unchecked.plain, "[ ]")
        self.assertEqual(Text.from_markup(checked.plain).plain, "")
        self.assertEqual(Text.from_markup(unchecked.plain).plain, "[ ]")

    def test_wide_label_alignment_formula_with_marker(self) -> None:
        wide_label = f"{WIDE_LABEL}ide"
        for state, expected in ((True, "[x]"), (False, "[ ]")):
            marker = huroshiki.checkbox_marker(state)
            self.assertIsInstance(marker, Text)
            combined_width = (marker + " " + _wide_cell(wide_label)).cell_len
            expected_width = marker.cell_len + 1 + _wide_cell(wide_label).cell_len
            self.assertEqual(combined_width, expected_width)


class CheckboxRenderingScreensTest(unittest.IsolatedAsyncioTestCase):
    async def test_template_candidate_table_uses_checkbox_text_cells(self) -> None:
        values = {
            "minecraft": "1.21.1",
            "loader": "neoforge",
            "loader_version": "21.1.0",
        }

        templates = [
            core.ProjectInfo(
                kind="template",
                project_id="one",
                display_name=f"{WIDE_LABEL} Template",
                minecraft="1.21.1",
                loader="neoforge",
                loader_version="21.1.0",
                enabled=True,
            )
        ]

        with patch.object(core, "compatible_templates", return_value=templates):
            app = _ScreenApp(huroshiki.TemplateCandidateScreen(dict(values)))
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.screen.query_one("#template-candidate-table", DataTable)
                row = table.get_row_at(0)
                _assert_checkbox_cell(self, row[0], row[1])
                app.screen.toggle_selected()
                self.assertEqual(table.get_row_at(0)[0].plain, "[x]")

    async def test_template_conflict_table_uses_checkbox_text_cells(self) -> None:
        candidate = MergedTemplateMod(
            candidate_key="modrinth:example",
            name=f"{WIDE_LABEL} Conflict",
            provider="modrinth",
            project_id="example",
            side="both",
            template_ids=("template",),
        )
        conflict = TemplateConflict(
            key="modrinth:example",
            name="name conflict",
            candidates=(candidate,),
        )
        composition = TemplateComposition(
            template_ids=("template",),
            mods=(candidate,),
            conflicts=(conflict,),
        )

        app = _ScreenApp(huroshiki.TemplateConflictScreen({"dummy": "1"}, composition))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#template-conflict-table", DataTable)
            row = table.get_row_at(0)
            _assert_checkbox_cell(self, row[0], row[1])
            app.screen.toggle_selected()
            self.assertEqual(table.get_row_at(0)[0].plain, "[x]")

    async def test_template_import_conflict_table_uses_checkbox_text_cells(self) -> None:
        candidate = template_candidate(
            "template",
            name=f"{WIDE_LABEL} Import",
            provider="modrinth",
            project_id="example",
            side="both",
        )
        option = ImportSelectionOption(option_key="import:template", candidates=(candidate,))
        conflict = CandidateNameConflict(
            key="modrinth:example",
            name="name conflict",
            options=(option,),
        )
        plan = TemplateImportPlan(
            pack_key="pack:demo",
            template_ids=("template",),
            template_candidates=(candidate,),
            pack_candidates=(),
            selection_options=(option,),
            new_roots=(candidate,),
            existing_identities=(candidate,),
            side_conflicts=(),
            name_conflicts=(conflict,),
            url_selector_conflicts=(),
            logical_identity_conflicts=(),
            actual_identity_conflicts=(),
            verifications=(),
            plan_digest="digest",
        )
        session = _FakeTemplateImportSession(plan)

        with patch.object(
            core,
            "project_info",
            return_value=core.ProjectInfo(
                kind="pack",
                project_id="demo",
                display_name="Demo",
                minecraft="1.21.1",
                loader="neoforge",
                loader_version="21.1.0",
                enabled=True,
            ),
        ):
            app = _ScreenApp(huroshiki.TemplateImportConflictScreen("pack:demo", session))
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.screen.query_one("#template-import-conflicts", DataTable)
                row = table.get_row_at(0)
                _assert_checkbox_cell(self, row[0], row[5])
                app.screen.toggle_option()
                self.assertEqual(table.get_row_at(0)[0].plain, "[x]")

    async def test_installed_mods_table_uses_checkbox_text_cells(self) -> None:
        with (
            patch.object(
                core,
                "project_config",
                return_value={"display_name": "Demo"},
            ),
            patch.object(
                core,
                "list_mods",
                return_value=(
                    core.ModInfo(
                        relative_path=Path("mods/example.pw.toml"),
                        slug="example",
                        name=f"{WIDE_LABEL} Mod",
                        provider="modrinth",
                        project_id="example",
                        filename="example.pw.toml",
                        client=True,
                        server=True,
                    ),
                ),
            ),
            patch.object(core, "filter_mods", lambda mods, _: mods),
        ):
            app = _ScreenApp(huroshiki.InstalledModsScreen("pack:demo"))
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.screen.query_one("#installed-table", DataTable)
                row = table.get_row_at(0)
                _assert_checkbox_cell(self, row[0], row[1])
                app.screen.toggle_selected()
                self.assertEqual(table.get_row_at(0)[0].plain, "[x]")

    async def test_update_screen_uses_checkbox_text_cells(self) -> None:
        _FakeUpdatePreparationOperation.candidates = (
            core.UpdateCandidate(
                key="mods/example.pw.toml",
                root=Path("mods/example.pw.toml"),
                slug="example",
                name=f"{WIDE_LABEL} Update",
                provider="modrinth",
                current_version="1.0",
                new_version="1.1",
                status="update",
            ),
        )
        with (
            patch.object(core, "project_config", return_value={"display_name": "Demo"}),
            patch.object(
                huroshiki.core,
                "UpdatePreparationOperation",
                _FakeUpdatePreparationOperation,
            ),
        ):
            app = _ScreenApp(huroshiki.UpdateScreen("pack:demo"))
            async with app.run_test() as pilot:
                for _ in range(20):
                    await pilot.pause(0.05)
                    if app.screen.query_one("#update-options", DataTable).row_count:
                        break
                table = app.screen.query_one("#update-options", DataTable)
                row = table.get_row_at(0)
                _assert_checkbox_cell(self, row[0], row[1])
                app.screen.toggle_candidate()
                self.assertEqual(table.get_row_at(0)[0].plain, "[ ]")

    async def test_update_screen_renders_version_locked_as_non_toggleable(self) -> None:
        _FakeUpdatePreparationOperation.candidates = (
            core.UpdateCandidate(
                key="mods/locked.pw.toml",
                root=Path("mods/locked.pw.toml"),
                slug="locked",
                name="Locked Mod",
                provider="curseforge",
                current_version="1.2.3",
                current_file_id="456",
                new_version="1.2.4",
                status="version-locked",
            ),
        )
        with (
            patch.object(core, "project_config", return_value={"display_name": "Demo"}),
            patch.object(
                huroshiki.core,
                "UpdatePreparationOperation",
                _FakeUpdatePreparationOperation,
            ),
        ):
            app = _ScreenApp(huroshiki.UpdateScreen("pack:demo"))
            async with app.run_test() as pilot:
                for _ in range(20):
                    await pilot.pause(0.05)
                    if app.screen.query_one("#update-options", DataTable).row_count:
                        break
                table = app.screen.query_one("#update-options", DataTable)
                row = table.get_row_at(0)
                self.assertEqual(row[0].plain, "[ ]")
                self.assertEqual(str(row[3]), "1.2.3 (456)")
                self.assertEqual(str(row[6]), "version locked")
                with patch.object(app, "notify") as notify:
                    app.screen.toggle_candidate()
                self.assertEqual(table.get_row_at(0)[0].plain, "[ ]")
                notify.assert_called_once_with(
                    "Locked Mod is version locked and cannot be selected; "
                    "update or remove its version pin first",
                    severity="warning",
                )

    async def test_update_screen_renders_version_blocked_details_as_non_toggleable(self) -> None:
        _FakeUpdatePreparationOperation.candidates = (
            core.UpdateCandidate(
                key="mods/blocked.pw.toml",
                root=Path("mods/blocked.pw.toml"),
                slug="blocked",
                name="Blocked Mod",
                provider="modrinth",
                current_version="1.2.3",
                current_file_id="456",
                new_version="-",
                status="version-blocked",
                error="requested artifact is unavailable",
                blocked_identity="modrinth:blocked-project",
                blocked_artifact_id="789",
                blocked_reason="requested artifact is unavailable",
                version_intent_reason="user pin requires exact artifact",
            ),
        )
        with (
            patch.object(core, "project_config", return_value={"display_name": "Demo"}),
            patch.object(
                huroshiki.core,
                "UpdatePreparationOperation",
                _FakeUpdatePreparationOperation,
            ),
        ):
            app = _ScreenApp(huroshiki.UpdateScreen("pack:demo"))
            async with app.run_test() as pilot:
                for _ in range(20):
                    await pilot.pause(0.05)
                    if app.screen.query_one("#update-options", DataTable).row_count:
                        break
                table = app.screen.query_one("#update-options", DataTable)
                row = table.get_row_at(0)
                self.assertEqual(row[0].plain, "[ ]")
                self.assertIn(
                    "version blocked: requires modrinth:blocked-project artifact 789: "
                    "requested artifact is unavailable; pin reason: user pin requires exact artifact",
                    str(row[6]),
                )
                with patch.object(app, "notify") as notify:
                    app.screen.toggle_candidate()
                self.assertEqual(table.get_row_at(0)[0].plain, "[ ]")
                notify.assert_called_once_with(
                    "Blocked Mod is version blocked: requires modrinth:blocked-project "
                    "artifact 789: requested artifact is unavailable; pin reason: user pin "
                    "requires exact artifact and cannot be selected; change its version pin "
                    "or choose a different artifact",
                    severity="warning",
                )

    async def test_update_screen_renders_version_locked_reason(self) -> None:
        _FakeUpdatePreparationOperation.candidates = (
            core.UpdateCandidate(
                key="mods/locked-reason.pw.toml",
                root=Path("mods/locked-reason.pw.toml"),
                slug="locked-reason",
                name="Reasoned Lock",
                provider="curseforge",
                current_version="1.2.3",
                current_file_id="456",
                new_version="-",
                status="version-locked",
                blocked_reason="direct MOD version is locked",
                version_intent_reason="maintain server compatibility",
            ),
        )
        with (
            patch.object(core, "project_config", return_value={"display_name": "Demo"}),
            patch.object(
                huroshiki.core,
                "UpdatePreparationOperation",
                _FakeUpdatePreparationOperation,
            ),
        ):
            app = _ScreenApp(huroshiki.UpdateScreen("pack:demo"))
            async with app.run_test() as pilot:
                for _ in range(20):
                    await pilot.pause(0.05)
                    if app.screen.query_one("#update-options", DataTable).row_count:
                        break
                row = app.screen.query_one("#update-options", DataTable).get_row_at(0)
                self.assertEqual(
                    str(row[6]),
                    "version locked: direct MOD version is locked; "
                    "pin reason: maintain server compatibility",
                )
