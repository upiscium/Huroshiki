from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import queue
import threading
import unittest
from unittest.mock import patch

from textual.app import App
from textual.screen import Screen
from textual.widgets import DataTable, Static

import huroshiki
import huroshiki_core as core
from template_import import (
    ImportCandidateVerification,
    ModCandidate,
    TemplateCompatibility,
    build_template_import_plan,
    resolve_template_import_plan,
    template_candidate,
)


PACK = core.ProjectInfo(
    kind="pack",
    project_id="demo",
    display_name="Demo Pack",
    minecraft="1.21.1",
    loader="neoforge",
    loader_version="21.1.1",
    enabled=True,
)
TEMPLATE_PROJECT = core.ProjectInfo(
    kind="template",
    project_id="base",
    display_name="Base",
    minecraft="1.21.1",
    loader="neoforge",
    loader_version="21.1.999",
    enabled=True,
    mod_count=1,
)


def template_project(template_id: str, *, loader: str = "neoforge") -> core.ProjectInfo:
    return core.ProjectInfo(
        kind="template",
        project_id=template_id,
        display_name=template_id.title(),
        minecraft="1.21.1",
        loader=loader,
        loader_version=f"reference-{template_id}",
        enabled=True,
        mod_count=1,
    )


def pack_candidate(
    name: str,
    project_id: str,
    *,
    side: str = "both",
    provider: str = "modrinth",
    url: str | None = None,
) -> ModCandidate:
    return ModCandidate(
        "pack",
        "demo",
        name,
        provider,
        project_id,
        side,
        metadata_path=Path(f"mods/{project_id}.pw.toml"),
        filename=f"{project_id}.jar",
        url=url,
        actual_provider=provider,
        actual_project_id=project_id,
    )


def plan_for(
    pack: tuple[ModCandidate, ...],
    templates: tuple[ModCandidate, ...],
    *,
    failed: set[tuple[str, str, str | None]] | None = None,
):
    failed = failed or set()
    template_ids = tuple(dict.fromkeys(item.origin_id for item in templates))
    verifications = tuple(
        ImportCandidateVerification(
            candidate.selector_identity,
            None if candidate.selector_identity in failed else candidate.actual_identity,
            candidate.metadata_path,
            candidate.filename,
            "fingerprint" if candidate.provider == "url" else None,
            "HTTP 404" if candidate.selector_identity in failed else None,
        )
        for candidate in templates
    )
    return build_template_import_plan(
        pack_key="pack:demo",
        pack_minecraft="1.21.1",
        pack_loader="neoforge",
        template_ids=template_ids,
        compatibilities={
            template_id: TemplateCompatibility(template_id, "1.21.1", "neoforge")
            for template_id in template_ids
        },
        pack_candidates=pack,
        template_candidates=templates,
        verifications=verifications,
    )


def simple_plan():
    candidate = template_candidate(
        "base",
        name="Root",
        provider="modrinth",
        project_id="root",
        side="both",
        actual_provider="modrinth",
        actual_project_id="root",
    )
    return plan_for((), (candidate,))


class FakeSession:
    def __init__(self, plan=None, template_ids: tuple[str, ...] = ("base",)) -> None:
        self.plan = plan or simple_plan()
        self.template_ids = template_ids
        self.discard_calls = 0

    def discard(self) -> None:
        self.discard_calls += 1


PREVIEW = core.TemplateImportPreview(
    added_roots=(
        core.ImportedRootPreview(
            "template:modrinth:root",
            "modrinth:root",
            "Root",
            ("modrinth", "root"),
            ("modrinth", "root"),
            Path("mods/root.pw.toml"),
            "root.jar",
        ),
    ),
    added_dependencies=(
        core.ModInfo(
            Path("mods/dependency.pw.toml"),
            "dependency",
            "Dependency",
            "modrinth",
            "dependency",
            "dependency.jar",
            True,
            True,
        ),
    ),
    side_changes=((('modrinth', 'shared'), 'client', 'both'),),
    removed=(pack_candidate("Removed", "removed"),),
    unchanged=(pack_candidate("Unchanged", "unchanged"),),
    changes=(core.UpdateChange(Path("mods/root.pw.toml"), None, b"new"),),
    warnings=("duplicate MOD risk acknowledged",),
    version_constraints=(
        core.TemplateVersionConstraintPreview(
            "modrinth:root",
            "modrinth",
            "root",
            "artifact-root",
            "root",
            ("base", "Pack"),
            True,
            "maintain compatibility",
        ),
        core.TemplateVersionConstraintPreview(
            "modrinth:dependency",
            "modrinth",
            "dependency",
            "artifact-dependency",
            "dependency",
            ("base",),
        ),
    ),
)


class FakeImportOperation:
    def __init__(
        self,
        session: FakeSession,
        *,
        delayed: bool = False,
        preview: core.TemplateImportPreview | None = PREVIEW,
        error: BaseException | None = None,
    ) -> None:
        self.session = session
        self.delayed = delayed
        self.result_preview = preview
        self.preview: core.TemplateImportPreview | None = None
        self.done = threading.Event()
        self.started = threading.Event()
        self.release = threading.Event()
        self.error = error
        self.cancelled = False
        self.progress: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.run_thread_id: int | None = None
        self.cancel_calls = 0
        self.discard_calls = 0
        self.apply_calls = 0

    def run(self) -> None:
        self.run_thread_id = threading.get_ident()
        self.started.set()
        self.progress.put("Resolving 1/1: Root")
        if self.delayed:
            self.release.wait(3)
        if self.error is not None:
            pass
        elif self.cancel_calls:
            self.cancelled = True
            self.session.discard()
        else:
            self.preview = self.result_preview
        self.done.set()

    def cancel(self) -> None:
        self.cancel_calls += 1

    def discard(self) -> None:
        self.discard_calls += 1
        self.session.discard()

    def apply(self) -> None:
        self.apply_calls += 1

    def drain_progress(self) -> tuple[str, ...]:
        values: list[str] = []
        while True:
            try:
                values.append(self.progress.get_nowait())
            except queue.Empty:
                return tuple(values)


class _ScreenApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, screen: Screen[None]) -> None:
        super().__init__()
        self.initial_screen = screen
        self.opened_projects: list[str] = []
        self.went_main = False

    def on_mount(self) -> None:
        self.push_screen(self.initial_screen)

    def open_project(self, project_key: str) -> bool:
        self.opened_projects.append(project_key)
        return True

    def go_main(self) -> None:
        self.went_main = True


class _ProjectApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, project_key: str) -> None:
        super().__init__()
        self.project_key = project_key
        self.import_opens: list[str] = []

    def on_mount(self) -> None:
        self.push_screen(huroshiki.ProjectScreen(self.project_key))

    def open_template_import(self, project_key: str) -> None:
        self.import_opens.append(project_key)


class TemplateImportTuiTest(unittest.IsolatedAsyncioTestCase):
    async def test_project_action_is_pack_only_and_uses_dedicated_navigation(self) -> None:
        def info(project_key: str):
            return PACK if project_key.startswith("pack:") else TEMPLATE_PROJECT

        with (
            patch.object(core, "project_info", side_effect=info),
            patch.object(core, "project_actions", return_value=()),
        ):
            pack_app = _ProjectApp("pack:demo")
            async with pack_app.run_test() as pilot:
                screen = pack_app.screen
                self.assertIn("Apply Template", screen.actions)
                table = screen.query_one("#project-actions", DataTable)
                table.move_cursor(row=screen.actions.index("Apply Template"))
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(pack_app.import_opens, ["pack:demo"])

            template_app = _ProjectApp("template:base")
            async with template_app.run_test():
                self.assertNotIn("Apply Template", template_app.screen.actions)

    async def test_selection_filters_by_pack_identity_and_preserves_toggle_order(self) -> None:
        candidates = [template_project("alpha"), template_project("beta")]
        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(core, "compatible_templates", return_value=candidates) as compatible,
            patch.object(core.TemplateImportSession, "create") as create,
        ):
            app = _ScreenApp(huroshiki.TemplateImportSelectionScreen("pack:demo"))
            async with app.run_test() as pilot:
                screen = app.screen
                compatible.assert_called_once_with("1.21.1", "neoforge")
                self.assertEqual(
                    [screen.templates[index].loader_version for index in range(2)],
                    ["reference-alpha", "reference-beta"],
                )
                await pilot.press("enter")
                await pilot.pause()
                create.assert_not_called()
                await pilot.press("space", "j", "space", "k", "space", "j", "k", "space")
                await pilot.pause()
                self.assertEqual(screen.selected_template_ids, ["beta", "alpha"])
                self.assertIn(
                    "beta -> alpha",
                    str(screen.query_one("#template-import-order", Static).content),
                )

    async def test_planning_runs_off_loop_and_passes_exact_template_order(self) -> None:
        session = FakeSession(template_ids=("beta", "alpha"))
        worker_thread_id: list[int] = []

        def create(_project_key: str, template_ids: tuple[str, ...], **_kwargs: object):
            worker_thread_id.append(threading.get_ident())
            self.assertEqual(template_ids, ("beta", "alpha"))
            return session

        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(
                core,
                "compatible_templates",
                return_value=[template_project("alpha"), template_project("beta")],
            ),
            patch.object(core.TemplateImportSession, "create", side_effect=create),
        ):
            app = _ScreenApp(huroshiki.TemplateImportSelectionScreen("pack:demo"))
            async with app.run_test() as pilot:
                main_thread = threading.get_ident()
                await pilot.press("j", "space", "k", "space", "enter")
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.TemplateImportConflictScreen)
                self.assertNotEqual(worker_thread_id, [main_thread])
                self.assertFalse(app.screen.session.finished if hasattr(app.screen.session, "finished") else False)

    async def test_planning_escape_waits_for_cleanup_completion(self) -> None:
        started = threading.Event()
        cleanup_release = threading.Event()

        def create(_project_key: str, _template_ids: tuple[str, ...], **kwargs: object):
            started.set()
            cancel_event = kwargs["cancel_event"]
            cancel_event.wait(2)
            cleanup_release.wait(2)
            raise core.LoaderMigrationCancelled("cancelled")

        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(core, "compatible_templates", return_value=[template_project("base")]),
            patch.object(core.TemplateImportSession, "create", side_effect=create),
        ):
            app = _ScreenApp(huroshiki.TemplateImportSelectionScreen("pack:demo"))
            async with app.run_test() as pilot:
                await pilot.press("space", "enter")
                self.assertTrue(started.wait(1))
                screen = app.screen
                self.assertFalse(screen.worker_thread.daemon)
                await pilot.press("escape")
                await pilot.pause(0.1)
                self.assertEqual(app.opened_projects, [])
                self.assertIs(app.screen, screen)
                cleanup_release.set()
                await pilot.pause(0.15)
                self.assertEqual(app.opened_projects, ["pack:demo"])

    async def test_planning_cancel_retains_failed_session_cleanup_for_retry(self) -> None:
        started = threading.Event()

        class RetryingSession(FakeSession):
            def discard(self) -> None:
                self.discard_calls += 1
                if self.discard_calls < 3:
                    raise core.TransactionDiscardIntegrityError(
                        "cleanup incomplete"
                    )

        session = RetryingSession()

        def create(_project_key: str, _template_ids: tuple[str, ...], **kwargs: object):
            started.set()
            kwargs["cancel_event"].wait(2)
            return session

        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(core, "compatible_templates", return_value=[template_project("base")]),
            patch.object(core.TemplateImportSession, "create", side_effect=create),
        ):
            app = _ScreenApp(huroshiki.TemplateImportSelectionScreen("pack:demo"))
            async with app.run_test() as pilot:
                await pilot.press("space", "enter")
                self.assertTrue(started.wait(1))
                screen = app.screen
                await pilot.press("escape")
                await pilot.pause(0.2)
                self.assertIs(app.screen, screen)
                self.assertEqual(app.opened_projects, [])
                self.assertEqual(session.discard_calls, 2)
                self.assertIs(screen.session, session)
                await pilot.press("escape")
                await pilot.pause(0.1)
                self.assertEqual(session.discard_calls, 3)
                self.assertEqual(app.opened_projects, ["pack:demo"])

    async def test_planning_failure_stays_on_selection_without_navigation(self) -> None:
        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(core, "compatible_templates", return_value=[template_project("base")]),
            patch.object(
                core.TemplateImportSession,
                "create",
                side_effect=core.HuroshikiError("planning failed"),
            ),
        ):
            app = _ScreenApp(huroshiki.TemplateImportSelectionScreen("pack:demo"))
            async with app.run_test() as pilot:
                await pilot.press("space", "enter")
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.TemplateImportSelectionScreen)
                self.assertIsNone(app.screen.worker_thread)
                self.assertEqual(app.opened_projects, [])
                self.assertIn(
                    "planning failed",
                    str(app.screen.query_one("#template-import-status", Static).content),
                )

    async def test_no_conflict_plan_still_calls_core_resolver_with_empty_mappings(self) -> None:
        session = FakeSession()
        resolved = resolve_template_import_plan(session.plan)
        operation = FakeImportOperation(session, preview=None)
        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(
                core,
                "resolve_template_import_plan",
                return_value=resolved,
            ) as resolve,
            patch.object(core, "TemplateImportOperation", return_value=operation),
        ):
            app = _ScreenApp(
                huroshiki.TemplateImportConflictScreen("pack:demo", session)
            )
            async with app.run_test() as pilot:
                self.assertEqual(app.screen.rows, [])
                await pilot.press("enter")
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.TemplateImportExecutionScreen)
                resolve.assert_called_once_with(
                    session.plan,
                    name_resolutions={},
                    url_selector_resolutions={},
                    logical_identity_resolutions={},
                    actual_identity_resolutions={},
                    side_decisions={},
                )

    async def test_conflicts_use_option_keys_acknowledge_multiple_and_keep_errors(self) -> None:
        first = template_candidate(
            "base",
            name="Same",
            provider="modrinth",
            project_id="first",
            side="both",
            actual_provider="modrinth",
            actual_project_id="first",
        )
        second = template_candidate(
            "base",
            name="Same",
            provider="curseforge",
            project_id="2",
            side="both",
            actual_provider="curseforge",
            actual_project_id="2",
        )
        session = FakeSession(plan_for((), (first, second)))
        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(
                core,
                "resolve_template_import_plan",
                side_effect=core.TemplateMergeError("overlapping conflict contradiction"),
            ),
        ):
            app = _ScreenApp(huroshiki.TemplateImportConflictScreen("pack:demo", session))
            async with app.run_test() as pilot:
                screen = app.screen
                self.assertTrue(all(row[4].option_key in str(screen.query_one("#template-import-conflicts", DataTable).get_row_at(i)) for i, row in enumerate(screen.rows)))
                await pilot.press("space", "j", "space", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                await pilot.press("enter")
                await pilot.pause()
                self.assertIs(app.screen, screen)
                self.assertIn(
                    "overlapping conflict contradiction",
                    str(screen.query_one("#template-import-conflict-error", Static).content),
                )
                selected = next(iter(screen.state.selected.values()))
                self.assertEqual(selected, [first.selection_key, second.selection_key])
                self.assertTrue(next(iter(screen.state.duplicate_acknowledged)))

    async def test_exactly_one_and_failed_option_details_are_visible(self) -> None:
        installed = pack_candidate(
            "Installed",
            "logical",
            provider="url",
            url="https://mods.example/installed.jar",
        )
        failed = template_candidate(
            "base",
            name="Failed replacement",
            provider="url",
            project_id="logical",
            side="both",
            url="https://mods.example/full-and-unshortened.jar",
        )
        plan = plan_for(
            (installed,),
            (failed,),
            failed={failed.selector_identity},
        )
        session = FakeSession(plan)
        with patch.object(core, "project_info", return_value=PACK):
            app = _ScreenApp(huroshiki.TemplateImportConflictScreen("pack:demo", session))
            async with app.run_test() as pilot:
                screen = app.screen
                table = screen.query_one("#template-import-conflicts", DataTable)
                self.assertIn("HTTP 404", " ".join(str(value) for value in table.get_row_at(1)))
                self.assertIn(
                    "https://mods.example/full-and-unshortened.jar",
                    " ".join(str(value) for value in table.get_row_at(1)),
                )
                await pilot.press("space", "j", "space")
                await pilot.pause()
                selected = next(iter(screen.state.selected.values()))
                self.assertEqual(selected, [failed.selection_key])
                await pilot.press("d")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.MessageModal)
                self.assertIn("Verification error: HTTP 404", "\n".join(app.screen.lines))

    async def test_grouped_origins_and_actual_identity_exactly_one_are_visible(self) -> None:
        installed = pack_candidate("Same", "shared")
        equivalent = template_candidate(
            "base",
            name="Same",
            provider="modrinth",
            project_id="shared",
            side="both",
            actual_provider="modrinth",
            actual_project_id="shared",
        )
        alternate = template_candidate(
            "base",
            name="Same",
            provider="curseforge",
            project_id="2",
            side="both",
            actual_provider="curseforge",
            actual_project_id="2",
        )
        grouped_session = FakeSession(plan_for((installed,), (equivalent, alternate)))
        with patch.object(core, "project_info", return_value=PACK):
            app = _ScreenApp(
                huroshiki.TemplateImportConflictScreen("pack:demo", grouped_session)
            )
            async with app.run_test():
                table = app.screen.query_one("#template-import-conflicts", DataTable)
                grouped_row = next(
                    table.get_row_at(index)
                    for index, row in enumerate(app.screen.rows)
                    if len(row[4].candidates) == 2
                )
                self.assertIn("pack:demo", grouped_row[4])
                self.assertIn("template:base", grouped_row[4])

        actual_pack = pack_candidate("Installed", "installed")
        incoming = template_candidate(
            "base",
            name="Incoming",
            provider="url",
            project_id="logical",
            side="both",
            url="https://mods.example/incoming.jar",
            actual_provider="modrinth",
            actual_project_id="installed",
        )
        actual_session = FakeSession(plan_for((actual_pack,), (incoming,)))
        with patch.object(core, "project_info", return_value=PACK):
            app = _ScreenApp(
                huroshiki.TemplateImportConflictScreen("pack:demo", actual_session)
            )
            async with app.run_test() as pilot:
                screen = app.screen
                self.assertTrue(all(row[0] == "actual" for row in screen.rows))
                await pilot.press("space", "j", "space")
                selected = screen.state.selected[("actual", "modrinth:installed")]
                self.assertEqual(selected, [incoming.selection_key])

    async def test_side_conflict_cycles_all_decisions_and_result_sides(self) -> None:
        installed = pack_candidate("Shared", "shared", side="client")
        incoming = template_candidate(
            "base",
            name="Shared",
            provider="modrinth",
            project_id="shared",
            side="server",
            actual_provider="modrinth",
            actual_project_id="shared",
        )
        session = FakeSession(plan_for((installed,), (incoming,)))
        state = huroshiki.TemplateImportResolutionState()
        with patch.object(core, "project_info", return_value=PACK):
            app = _ScreenApp(
                huroshiki.TemplateImportSideConflictScreen("pack:demo", session, state)
            )
            async with app.run_test() as pilot:
                table = app.screen.query_one("#template-import-side-conflicts", DataTable)
                self.assertEqual(table.get_row_at(0)[3:], ["keep_pack", "client"])
                await pilot.press("space")
                self.assertEqual(table.get_row_at(0)[3:], ["use_template", "server"])
                await pilot.press("space")
                self.assertEqual(table.get_row_at(0)[3:], ["union", "both"])
                await pilot.press("space")
                self.assertEqual(table.get_row_at(0)[3:], ["keep_pack", "client"])

    async def test_execution_runs_off_loop_renders_preview_and_applies_only_after_confirm(self) -> None:
        session = FakeSession()
        resolved = resolve_template_import_plan(session.plan)
        operation = FakeImportOperation(session)
        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(core, "TemplateImportOperation", return_value=operation),
        ):
            app = _ScreenApp(
                huroshiki.TemplateImportExecutionScreen("pack:demo", session, resolved)
            )
            with patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test() as pilot:
                    main_thread = threading.get_ident()
                    await pilot.pause(0.15)
                    screen = app.screen
                    self.assertNotEqual(operation.run_thread_id, main_thread)
                    rendered = str(screen.query_one("#template-import-preview", Static).content)
                    for expected in (
                        "Version constraints",
                        "modrinth:root | artifact ID: artifact-root | role: root | origins: base, Pack | lock state: locked | pin reason: maintain compatibility",
                        "modrinth:dependency | artifact ID: artifact-dependency | role: dependency | origins: base | lock state: unlocked",
                        "Explicit roots",
                        "Dependencies",
                        "Side changes",
                        "REMOVED Pack roots",
                        "Metadata file changes",
                        "duplicate MOD risk",
                        "No persistent Template association",
                    ):
                        self.assertIn(expected, rendered)
                    self.assertNotIn("artifact-dependency | role: dependency | origins: base | lock state: unlocked | pin reason:", rendered)
                    self.assertEqual(operation.apply_calls, 0)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(operation.apply_calls, 1)
                    self.assertEqual(operation.discard_calls, 0)
                    self.assertEqual(app.opened_projects, ["pack:demo"])

    async def test_execution_rejection_discards_and_escape_waits_for_cleanup(self) -> None:
        session = FakeSession()
        resolved = resolve_template_import_plan(session.plan)
        operation = FakeImportOperation(session, delayed=True)
        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(core, "TemplateImportOperation", return_value=operation),
        ):
            app = _ScreenApp(
                huroshiki.TemplateImportExecutionScreen("pack:demo", session, resolved)
            )
            async with app.run_test() as pilot:
                self.assertTrue(operation.started.wait(1))
                screen = app.screen
                self.assertFalse(screen.worker_thread.daemon)
                await pilot.pause(0.1)
                self.assertIn(
                    "Resolving 1/1: Root",
                    str(
                        screen.query_one(
                            "#template-import-execution-status", Static
                        ).content
                    ),
                )
                await pilot.press("escape")
                await pilot.pause(0.1)
                self.assertEqual(operation.cancel_calls, 1)
                self.assertEqual(app.opened_projects, [])
                operation.release.set()
                await pilot.pause(0.15)
                self.assertEqual(app.opened_projects, ["pack:demo"])
                self.assertEqual(session.discard_calls, 1)

    async def test_preview_confirmation_rejection_discards_without_apply(self) -> None:
        session = FakeSession()
        resolved = resolve_template_import_plan(session.plan)
        operation = FakeImportOperation(session)
        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(core, "TemplateImportOperation", return_value=operation),
        ):
            app = _ScreenApp(
                huroshiki.TemplateImportExecutionScreen("pack:demo", session, resolved)
            )
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(operation.apply_calls, 0)
                self.assertEqual(operation.discard_calls, 1)
                self.assertEqual(app.opened_projects, ["pack:demo"])

    async def test_failed_apply_cleanup_retains_screen_until_discard_retry(self) -> None:
        class RetainedSession(FakeSession):
            def __init__(self) -> None:
                super().__init__()
                self.finished = False

            def discard(self) -> None:
                self.discard_calls += 1
                self.finished = True

        class FailedApplyOperation(FakeImportOperation):
            def apply(self) -> None:
                self.apply_calls += 1
                raise core.TransactionDiscardIntegrityError(
                    "apply cleanup incomplete"
                )

        session = RetainedSession()
        resolved = resolve_template_import_plan(session.plan)
        operation = FailedApplyOperation(session)
        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(core, "TemplateImportOperation", return_value=operation),
        ):
            app = _ScreenApp(
                huroshiki.TemplateImportExecutionScreen(
                    "pack:demo", session, resolved
                )
            )
            with patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test() as pilot:
                    await pilot.pause(0.15)
                    screen = app.screen
                    await pilot.press("enter")
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIs(app.screen, screen)
                    self.assertFalse(screen.ownership_finished)
                    self.assertEqual(app.opened_projects, [])
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertEqual(operation.discard_calls, 1)
                    self.assertEqual(app.opened_projects, ["pack:demo"])

    async def test_execution_error_renders_technical_text_without_pin_reason(self) -> None:
        session = FakeSession()
        resolved = resolve_template_import_plan(session.plan)
        error = core.ProfileVersionIntentError(
            "Locked Template Import intent conflict for modrinth:root: "
            "artifact old; requested new",
            identity="modrinth:root",
            artifact_id="old",
        )
        operation = FakeImportOperation(session, error=error)
        with (
            patch.object(core, "project_info", return_value=PACK),
            patch.object(core, "TemplateImportOperation", return_value=operation),
        ):
            app = _ScreenApp(
                huroshiki.TemplateImportExecutionScreen("pack:demo", session, resolved)
            )
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                rendered = str(
                    app.screen.query_one("#template-import-execution-status", Static).content
                )
                self.assertIn(f"Technical failure: {error}", rendered)
                self.assertIn("Blocked identity: modrinth:root", rendered)
                self.assertIn("Pinned artifact: old", rendered)
                self.assertNotIn("Pin reason:", rendered)


if __name__ == "__main__":
    unittest.main()
