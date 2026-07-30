from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextlib import redirect_stderr
import io
from pathlib import Path
import threading
import time
import tempfile
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import DataTable, Input, Static, TextArea

import huroshiki
import huroshiki_core as core
import overlay_policy


PACK = core.ProjectInfo(
    kind="pack",
    project_id="demo",
    display_name="Demo",
    minecraft="1.21.1",
    loader="neoforge",
    loader_version="21.1.0",
    enabled=True,
)
TEMPLATE = core.ProjectInfo(
    kind="template",
    project_id="base",
    display_name="Base",
    minecraft="1.21.1",
    loader="neoforge",
    loader_version="21.1.0",
    enabled=True,
)
IDENTITY = core.PathIdentity(True, 1, 2, 0o755)
ENTRIES = (
    core.ContentEntry(
        "common", Path("config/demo.txt"), "file", 5, 0o644, False,
        "a" * 64, "utf8", "config", (),
    ),
    core.ContentEntry(
        "client", Path("resourcepacks/demo.zip"), "file", 12, 0o755, True,
        "b" * 64, "binary", "resourcepack", (),
    ),
    core.ContentEntry(
        "server", Path("empty"), "directory", 0, 0o755, True,
        None, "unknown", "other", (),
    ),
    core.ContentEntry(
        "common", Path("bad-link"), "invalid", 0, 0, False,
        None, "unknown", "other", ("Symlink is not allowed",),
    ),
)
SNAPSHOT_ENTRIES = tuple(
    core.ContentSnapshotEntry(
        entry.side,
        entry.relative_path,
        (entry.side, str(entry.relative_path).casefold()),
        entry.kind,
        entry.mode,
        entry.size,
        entry.digest,
        1,
        index + 10,
        entry.errors,
    )
    for index, entry in enumerate(ENTRIES)
)
SNAPSHOT = core.ContentSnapshot(
    "pack:demo", IDENTITY, IDENTITY, IDENTITY, SNAPSHOT_ENTRIES, "c" * 64
)
CONFLICTS = (
    core.ContentConflict(
        "common_client_overlap",
        "warning",
        "config/demo.txt",
        (("common", Path("config/demo.txt")), ("client", Path("config/demo.txt"))),
        "client overrides common",
    ),
    core.ContentConflict(
        "cross_side_type_conflict",
        "error",
        "empty",
        (("common", Path("empty")), ("server", Path("empty"))),
        "types differ",
    ),
)
BROWSE = core.ContentBrowseResult(ENTRIES, SNAPSHOT, CONFLICTS)
DOCUMENT = core.ContentTextDocument(
    "pack:demo",
    "common",
    Path("config/demo.txt"),
    SNAPSHOT,
    "a" * 64,
    0o644,
    "hello\n",
    "lf",
    6,
)
MIXED_DOCUMENT = core.ContentTextDocument(
    "pack:demo",
    "common",
    Path("config/demo.txt"),
    SNAPSHOT,
    "a" * 64,
    0o644,
    "one\ntwo\n",
    "mixed",
    10,
)


class _Discard:
    def __init__(self, plan, *, error: BaseException | None = None) -> None:
        self.plan = plan
        self.done = threading.Event()
        self.error = error
        self.started = False

    def start(self) -> None:
        self.started = True
        if self.error is None:
            self.plan.discard()
        self.done.set()

    def raise_for_error(self) -> None:
        if self.error is not None:
            raise self.error


class _Plan:
    def __init__(self, operations=(), *, conflicts=(), discard_error=None) -> None:
        self.operations = tuple(operations)
        self.changes = (
            core.ContentChange("updated", "common", Path("config/demo.txt"), before_digest="a", after_digest="b"),
        )
        self.conflicts = tuple(conflicts)
        self.transaction_root = Path("/tmp/content-plan")
        self.state = "ready" if not any(item.severity == "error" for item in conflicts) else "failed"
        self.cleanup_error = None
        self._project_lock = object()
        self.discard_error = discard_error
        self.discard_operation: _Discard | None = None

    def begin_discard(self, *, deadline=None):
        self.discard_operation = _Discard(self, error=self.discard_error)
        return self.discard_operation

    def discard(self, *, deadline=None) -> None:
        if self.discard_error is not None:
            raise self.discard_error
        self._project_lock = None
        self.state = "discarded"


class _ContentApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, screen) -> None:
        super().__init__()
        self.initial_screen = screen
        self.content_workers: dict[str, object] = {}
        self.content_plans: dict[str, object] = {}
        self._content_discards: dict[str, object] = {}
        self._content_discard_timer = None

    def on_mount(self) -> None:
        self.push_screen(self.initial_screen)

    start_content_worker = huroshiki.HuroshikiApp.start_content_worker
    finish_content_worker = huroshiki.HuroshikiApp.finish_content_worker
    register_content_plan = huroshiki.HuroshikiApp.register_content_plan
    begin_content_discard = huroshiki.HuroshikiApp.begin_content_discard
    _poll_content_discards = huroshiki.HuroshikiApp._poll_content_discards
    open_content = huroshiki.HuroshikiApp.open_content

    def project_is_usable(self, project_key: str) -> bool:
        return huroshiki.core.project_info(project_key).error is None

    def open_project(self, project_key: str) -> bool:
        self.switch_screen(huroshiki.ProjectScreen(project_key))
        return True

    def go_main(self) -> None:
        self.switch_screen(huroshiki.MainMenuScreen())

    def on_unmount(self) -> None:
        for worker in tuple(self.content_workers.values()):
            worker.cancel()
            worker.wait(time.monotonic() + 1)
        for plan in tuple(self.content_plans.values()):
            try:
                plan.discard(deadline=time.monotonic() + 1)
            except BaseException:
                pass


class ContentTuiTest(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def patches(self):
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    huroshiki.core,
                    "project_info",
                    side_effect=lambda key: TEMPLATE if key.startswith("template:") else PACK,
                )
            )
            stack.enter_context(patch.object(huroshiki.core, "project_actions", return_value=()))
            stack.enter_context(patch.object(huroshiki.core, "load_content_browser", return_value=BROWSE))
            stack.enter_context(patch.object(huroshiki.core, "ContentChangePlan", _Plan))
            yield

    async def test_pack_project_content_route_and_template_warning(self) -> None:
        with self.patches():
            app = _ContentApp(huroshiki.ProjectScreen("pack:demo"))
            async with app.run_test() as pilot:
                await pilot.press("t")
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ContentScreen)

            template_app = _ContentApp(huroshiki.ProjectScreen("template:base"))
            async with template_app.run_test() as pilot:
                await pilot.press("t")
                await pilot.pause()
                self.assertIsInstance(template_app.screen, huroshiki.ProjectScreen)

    async def test_browser_columns_filters_status_detail_and_selection(self) -> None:
        with self.patches():
            screen = huroshiki.ContentScreen("pack:demo")
            app = _ContentApp(screen)
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                table = screen.query_one("#content-table", DataTable)
                self.assertEqual(table.row_count, 4)
                self.assertEqual(len(table.columns), 8)
                status = str(screen.query_one("#content-status", Static).content)
                self.assertIn("Warnings: 1", status)
                self.assertIn("Fatal conflicts: 1", status)
                table.move_cursor(row=1)
                screen.update_detail()
                self.assertIn("resourcepacks/demo.zip", str(screen.query_one("#content-detail", Static).content))
                await pilot.press("s")
                self.assertEqual(screen.side_filter, "common")
                self.assertEqual(table.row_count, 2)
                search = screen.query_one("#content-search", Input)
                search.value = "bad-link"
                await pilot.pause()
                self.assertEqual(table.row_count, 1)
                self.assertIn("Symlink", str(table.get_row_at(0)[7]))
                screen.selected_key = ("common", Path("bad-link"))
                search.value = ""
                screen.reload_rows()
                self.assertEqual(screen.current_entry().relative_path, Path("bad-link"))

    async def test_browser_load_cancel_defers_navigation_and_propagates_event(self) -> None:
        started = threading.Event()
        release = threading.Event()
        observed_cancel: threading.Event | None = None

        def blocking_load(_key, *, cancel_event, deadline):
            nonlocal observed_cancel
            observed_cancel = cancel_event
            started.set()
            release.wait(2)
            if cancel_event.is_set():
                raise core.ContentOperationCancelled("cancelled")
            return BROWSE

        with self.patches(), patch.object(
            huroshiki.core, "load_content_browser", side_effect=blocking_load
        ):
            screen = huroshiki.ContentScreen("pack:demo")
            app = _ContentApp(screen)
            async with app.run_test() as pilot:
                self.assertTrue(started.wait(1))
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, screen)
                self.assertTrue(observed_cancel.is_set())
                release.set()
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)

    async def test_editor_load_exact_text_and_save_builds_digest_snapshot_plan(self) -> None:
        captured: dict[str, object] = {}
        plan = _Plan()

        def plan_changes(project_key, operations, **kwargs):
            captured.update(project_key=project_key, operations=tuple(operations), **kwargs)
            plan.operations = tuple(operations)
            return plan

        with self.patches(), patch.object(
            huroshiki.core, "load_content_text_document", return_value=DOCUMENT
        ), patch.object(
            huroshiki.core, "plan_content_changes", side_effect=plan_changes
        ):
            screen = huroshiki.ContentEditorScreen("pack:demo", ENTRIES[0], SNAPSHOT)
            app = _ContentApp(screen)
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                editor = screen.query_one("#content-editor", TextArea)
                self.assertEqual(editor.text, "hello\n")
                editor.text = "changed\n"
                await pilot.press("ctrl+s")
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ContentPlanPreviewScreen)
                operation = captured["operations"][0]
                self.assertIsInstance(operation, core.ContentReplaceFile)
                self.assertEqual(operation.expected_digest, DOCUMENT.digest)
                self.assertIsNone(operation.mode)
                self.assertIs(captured["expected_snapshot"], SNAPSHOT)
                self.assertEqual(operation.contents, b"changed\n")

    async def test_editor_unsaved_confirmation_and_rejection_policies(self) -> None:
        with self.patches(), patch.object(
            huroshiki.core, "load_content_text_document", return_value=DOCUMENT
        ):
            screen = huroshiki.ContentEditorScreen("pack:demo", ENTRIES[0], SNAPSHOT)
            app = _ContentApp(screen)
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                screen.query_one("#content-editor", TextArea).text = "dirty"
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, screen)
                await pilot.press("escape")
                await pilot.press("enter")
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, huroshiki.ContentScreen)

            browser = huroshiki.ContentScreen("pack:demo")
            browser.result = BROWSE
            app = _ContentApp(browser)
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                table = browser.query_one("#content-table", DataTable)
                table.move_cursor(row=1)
                browser.edit_current()
                self.assertIs(app.screen, browser)
                table.move_cursor(row=2)
                browser.edit_current()
                self.assertIs(app.screen, browser)

    async def test_editor_preview_cancel_failure_retry_preserve_exact_draft(self) -> None:
        plans = [
            _Plan(conflicts=(CONFLICTS[1],)),
            _Plan(),
            _Plan(discard_error=RuntimeError("cleanup failed")),
            _Plan(),
        ]
        apply_should_fail = True

        def plan_changes(_key, operations, **_kwargs):
            plan = plans.pop(0)
            plan.operations = tuple(operations)
            return plan

        def apply(plan, **_kwargs):
            if apply_should_fail:
                raise core.ContentOperationError("apply failed")
            plan.state = "applied"
            plan._project_lock = None

        with self.patches(), patch.object(
            huroshiki.core,
            "load_content_text_document",
            return_value=MIXED_DOCUMENT,
        ), patch.object(
            huroshiki.core,
            "plan_content_changes",
            side_effect=plan_changes,
        ), patch.object(
            huroshiki.core,
            "apply_content_changes",
            side_effect=apply,
        ):
            editor_screen = huroshiki.ContentEditorScreen(
                "pack:demo", ENTRIES[0], SNAPSHOT
            )
            app = _ContentApp(editor_screen)
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                editor = editor_screen.query_one("#content-editor", TextArea)
                draft = "draft line\nsecond line\n"
                editor.text = draft
                editor.cursor_location = (1, 4)
                cursor = editor.cursor_location

                await pilot.press("ctrl+s")
                await pilot.pause(0.15)
                preview = app.screen
                self.assertIsInstance(preview, huroshiki.ContentPlanPreviewScreen)
                self.assertIn("Mixed newlines -> LF", preview.preview_text())
                self.assertIn(
                    "Mixed newlines -> LF",
                    str(preview.query_one("#content-plan-preview", Static).content),
                )
                self.assertTrue(preview.fatal)
                await pilot.press("escape")
                await pilot.pause(0.15)
                self.assertIs(app.screen, editor_screen)
                self.assertIs(editor_screen.query_one("#content-editor", TextArea), editor)
                self.assertEqual(editor.text, draft)
                self.assertEqual(editor.cursor_location, cursor)

                await pilot.press("ctrl+s")
                await pilot.pause(0.15)
                await pilot.press("enter")
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ContentPlanPreviewScreen)
                self.assertIn("apply failed", str(app.screen.query_one("#content-operation-status", Static).content))
                await pilot.press("escape")
                await pilot.pause(0.15)
                self.assertIs(app.screen, editor_screen)
                self.assertEqual(editor.text, draft)
                self.assertEqual(editor.cursor_location, cursor)

                await pilot.press("ctrl+s")
                await pilot.pause(0.15)
                cleanup_plan = app.screen.plan
                await pilot.press("escape")
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ContentPlanPreviewScreen)
                cleanup_plan.discard_error = None
                await pilot.press("r")
                await pilot.pause(0.15)
                self.assertIs(app.screen, editor_screen)
                self.assertEqual(editor.text, draft)
                self.assertEqual(editor.cursor_location, cursor)

                apply_should_fail = False
                await pilot.press("ctrl+s")
                await pilot.pause(0.15)
                await pilot.press("enter")
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ContentScreen)

    async def test_create_delete_move_use_immutable_operations_and_preview(self) -> None:
        plans: list[_Plan] = []

        def plan_changes(_key, operations, **_kwargs):
            plan = _Plan(tuple(operations))
            plans.append(plan)
            return plan

        with self.patches(), patch.object(
            huroshiki.core, "plan_content_changes", side_effect=plan_changes
        ):
            for operation_action in ("create", "delete", "move"):
                screen = huroshiki.ContentScreen("pack:demo")
                app = _ContentApp(screen)
                async with app.run_test() as pilot:
                    await pilot.pause(0.15)
                    if operation_action == "create":
                        screen.create_entry(
                            {"kind": "startup", "side": "common", "path": "boot", "mode": "0644", "text": "// hi\n"}
                        )
                    elif operation_action == "delete":
                        screen.delete_confirmed(ENTRIES[2], True)
                    else:
                        screen.move_confirmed(ENTRIES[0], {"side": "server", "path": "config/moved.txt"})
                    await pilot.pause(0.15)
                    self.assertIsInstance(app.screen, huroshiki.ContentPlanPreviewScreen)
                    await pilot.press("escape")
                    await pilot.pause(0.15)
                    self.assertIsInstance(app.screen, huroshiki.ContentScreen)
            self.assertIsInstance(plans[0].operations[0], core.ContentCreateFile)
            self.assertEqual(plans[0].operations[0].relative_path, Path("kubejs/startup_scripts/boot.js"))
            self.assertIsInstance(plans[1].operations[0], core.ContentDeleteDirectory)
            self.assertIsInstance(plans[2].operations[0], core.ContentMove)
            self.assertEqual(plans[2].operations[0].destination_side, "server")

    async def test_create_modal_applies_kubejs_defaults_before_preview(self) -> None:
        plan = _Plan()

        def plan_changes(_key, operations, **_kwargs):
            plan.operations = tuple(operations)
            return plan

        with self.patches(), patch.object(
            huroshiki.core,
            "plan_content_changes",
            side_effect=plan_changes,
        ):
            screen = huroshiki.ContentScreen("pack:demo")
            app = _ContentApp(screen)
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                await pilot.press("c")
                await pilot.pause()
                modal = app.screen
                self.assertIsInstance(modal, huroshiki.ContentCreateModal)
                modal.query_one("#content-create-kind", Input).value = "server"
                await pilot.pause()
                self.assertEqual(
                    modal.query_one("#content-create-side", Input).value,
                    "server",
                )
                modal.query_one("#content-create-path", Input).value = "tick"
                modal.action_submit()
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ContentPlanPreviewScreen)
                operation = plan.operations[0]
                self.assertIsInstance(operation, core.ContentCreateFile)
                self.assertEqual(operation.side, "server")
                self.assertEqual(
                    operation.relative_path,
                    Path("kubejs/server_scripts/tick.js"),
                )

    async def test_fatal_preview_disables_apply_and_cleanup_failure_retains_plan(self) -> None:
        plan = _Plan(conflicts=(CONFLICTS[1],), discard_error=RuntimeError("cleanup failed"))
        with self.patches():
            app = _ContentApp(huroshiki.ContentPlanPreviewScreen("pack:demo", plan))
            app.content_plans["pack:demo"] = plan
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ContentPlanPreviewScreen)
                self.assertIsNone(app.screen.worker)
                await pilot.press("escape")
                await pilot.pause(0.15)
                self.assertIs(app.content_plans["pack:demo"], plan)
                self.assertIsInstance(app.screen, huroshiki.ContentPlanPreviewScreen)
                self.assertIn("cleanup failed", str(app.screen.query_one("#content-operation-status", Static).content))

    async def test_worker_start_failure_leaves_browser_retryable_and_plan_owned(self) -> None:
        with self.patches(), patch.object(
            threading.Thread, "start", side_effect=RuntimeError("start failed")
        ):
            screen = huroshiki.ContentScreen("pack:demo")
            app = _ContentApp(screen)
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertFalse(app.content_workers)
                self.assertIn("start failed", str(screen.query_one("#content-status", Static).content))

        plan = _Plan()
        with self.patches(), patch.object(
            threading.Thread, "start", side_effect=RuntimeError("apply start failed")
        ):
            screen = huroshiki.ContentPlanPreviewScreen("pack:demo", plan)
            app = _ContentApp(screen)
            app.content_plans["pack:demo"] = plan
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                self.assertIs(app.content_plans["pack:demo"], plan)
                self.assertIsNone(screen.worker)

    async def test_apply_failure_retains_plan_and_success_reloads_browser(self) -> None:
        failed_plan = _Plan()
        with self.patches(), patch.object(
            huroshiki.core,
            "apply_content_changes",
            side_effect=core.ContentOperationError("apply failed"),
        ):
            screen = huroshiki.ContentPlanPreviewScreen("pack:demo", failed_plan)
            app = _ContentApp(screen)
            app.content_plans["pack:demo"] = failed_plan
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause(0.15)
                self.assertIs(app.content_plans["pack:demo"], failed_plan)
                self.assertIn("apply failed", str(screen.query_one("#content-operation-status", Static).content))
                await pilot.press("escape")
                await pilot.pause(0.15)
                self.assertNotIn("pack:demo", app.content_plans)
                self.assertIsInstance(app.screen, huroshiki.ContentScreen)

        applied_plan = _Plan()

        def apply(plan, **_kwargs):
            plan.state = "applied"
            plan._project_lock = None

        with self.patches(), patch.object(
            huroshiki.core,
            "apply_content_changes",
            side_effect=apply,
        ):
            screen = huroshiki.ContentPlanPreviewScreen("pack:demo", applied_plan)
            app = _ContentApp(screen)
            app.content_plans["pack:demo"] = applied_plan
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause(0.15)
                self.assertNotIn("pack:demo", app.content_plans)
                self.assertIsInstance(app.screen, huroshiki.ContentScreen)

    async def test_plan_and_apply_cancel_wait_for_completion_and_discard(self) -> None:
        plan_started = threading.Event()
        plan_release = threading.Event()
        planned = _Plan()

        def blocking_plan(_key, _operations, *, cancel_event, **_kwargs):
            plan_started.set()
            plan_release.wait(2)
            return planned

        with self.patches(), patch.object(
            huroshiki.core,
            "plan_content_changes",
            side_effect=blocking_plan,
        ):
            screen = huroshiki.ContentScreen("pack:demo")
            app = _ContentApp(screen)
            async with app.run_test() as pilot:
                await pilot.pause(0.15)
                screen.start_plan((core.ContentDeleteFile("common", Path("config/demo.txt")),))
                await pilot.pause()
                self.assertTrue(plan_started.is_set())
                await pilot.press("escape")
                self.assertIs(app.screen, screen)
                plan_release.set()
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)
                self.assertNotIn("pack:demo", app.content_plans)

        apply_started = threading.Event()
        apply_cancelled = threading.Event()
        apply_release = threading.Event()

        def blocking_apply(_plan, *, cancel_event, **_kwargs):
            apply_started.set()
            cancel_event.wait(2)
            apply_cancelled.set()
            apply_release.wait(2)
            raise core.ContentOperationCancelled("apply cancelled")

        applying = _Plan()
        with self.patches(), patch.object(
            huroshiki.core,
            "apply_content_changes",
            side_effect=blocking_apply,
        ):
            screen = huroshiki.ContentPlanPreviewScreen("pack:demo", applying)
            app = _ContentApp(screen)
            app.content_plans["pack:demo"] = applying
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(apply_started.is_set())
                await pilot.press("escape")
                self.assertTrue(apply_cancelled.wait(1))
                self.assertIs(app.screen, screen)
                apply_release.set()
                await pilot.pause(0.15)
                self.assertIsInstance(app.screen, huroshiki.ContentScreen)
                self.assertNotIn("pack:demo", app.content_plans)

    async def test_discard_start_failure_keeps_plan_and_allows_retry(self) -> None:
        plan = _Plan()
        original_begin = plan.begin_discard
        plan.begin_discard = lambda **_: (_ for _ in ()).throw(RuntimeError("discard start failed"))
        with self.patches():
            screen = huroshiki.ContentPlanPreviewScreen("pack:demo", plan)
            app = _ContentApp(screen)
            app.content_plans["pack:demo"] = plan
            async with app.run_test() as pilot:
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.content_plans["pack:demo"], plan)
                self.assertIs(app.screen, screen)
                self.assertIn("discard start failed", str(screen.query_one("#content-operation-status", Static).content))
                plan.begin_discard = original_begin
                await pilot.press("r")
                await pilot.pause(0.15)
                self.assertNotIn("pack:demo", app.content_plans)
                self.assertIsInstance(app.screen, huroshiki.ContentScreen)


class ContentCreateModelTest(unittest.TestCase):
    def test_modes_presets_and_invalid_inputs(self) -> None:
        for kind, side, prefix in (
            ("startup", "common", "kubejs/startup_scripts"),
            ("server", "server", "kubejs/server_scripts"),
            ("client", "client", "kubejs/client_scripts"),
        ):
            operation, key = huroshiki.content_create_operation(
                {"kind": kind, "side": side, "path": "demo", "mode": "0644", "text": ""}
            )
            self.assertIsInstance(operation, core.ContentCreateFile)
            self.assertEqual(key, (side, Path(f"{prefix}/demo.js")))
            self.assertEqual(operation.mode, 0o644)
        directory, _ = huroshiki.content_create_operation(
            {"kind": "directory", "side": "common", "path": "config/empty", "mode": "0755", "text": ""}
        )
        self.assertIsInstance(directory, core.ContentCreateDirectory)
        self.assertEqual(directory.mode, 0o755)
        with self.assertRaises(core.ContentOperationError):
            huroshiki.content_create_operation(
                {"kind": "file", "side": "common", "path": "x", "mode": "9999", "text": ""}
            )


class ContentAppShutdownTest(unittest.TestCase):
    def test_shutdown_cancels_recursive_scan_worker_without_live_thread(self) -> None:
        entered = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            content_root = Path(directory) / "content"

            def target(cancel_event: threading.Event, _deadline: float):
                def checkpoint() -> None:
                    entered.set()
                    cancel_event.wait(2)
                    if cancel_event.is_set():
                        raise core.ContentOperationCancelled("scan cancelled")

                return overlay_policy.scan_content_overlays(
                    content_root,
                    checkpoint=checkpoint,
                )

            worker = huroshiki.ContentWorker(
                "huroshiki-content-browser-pack-demo",
                target,
            )
            worker.start()
            self.assertTrue(entered.wait(1))
            app = huroshiki.HuroshikiApp()
            app.content_workers["pack:demo"] = worker
            app.on_unmount()
        self.assertTrue(worker.done.is_set())
        self.assertIsNotNone(worker.thread)
        self.assertFalse(worker.thread.is_alive())
        self.assertFalse(app.content_workers)

    def test_worker_instance_identity_rejects_stale_completion(self) -> None:
        app = huroshiki.HuroshikiApp()
        stale = huroshiki.ContentWorker(
            "huroshiki-content-stale-pack-demo",
            lambda *_: "stale",
        )
        current = huroshiki.ContentWorker(
            "huroshiki-content-current-pack-demo",
            lambda *_: "current",
        )
        stale.start()
        current.start()
        self.assertTrue(stale.done.wait(1))
        self.assertTrue(current.done.wait(1))
        app.content_workers["pack:demo"] = current
        with self.assertRaisesRegex(core.ContentOperationError, "ownership changed"):
            app.finish_content_worker("pack:demo", stale)
        self.assertIs(app.content_workers["pack:demo"], current)
        self.assertEqual(app.finish_content_worker("pack:demo", current), "current")

    def test_shutdown_cancels_workers_collects_plans_and_discards(self) -> None:
        plan = _Plan()
        started = threading.Event()

        def target(cancel_event: threading.Event, _deadline: float):
            started.set()
            cancel_event.wait(1)
            return plan

        app = huroshiki.HuroshikiApp()
        with patch.object(huroshiki.core, "ContentChangePlan", _Plan):
            worker = huroshiki.ContentWorker(
                "huroshiki-content-shutdown-pack-demo",
                target,
            )
            worker.start()
            self.assertTrue(started.wait(1))
            app.content_workers["pack:demo"] = worker
            app.on_unmount()
        self.assertTrue(worker.cancel_event.is_set())
        self.assertTrue(worker.done.is_set())
        self.assertFalse(app.content_workers)
        self.assertFalse(app.content_plans)
        self.assertEqual(plan.state, "discarded")

    def test_shutdown_failure_reports_transaction_and_retains_plan(self) -> None:
        plan = _Plan(discard_error=RuntimeError("cleanup failed"))
        app = huroshiki.HuroshikiApp()
        app.content_plans["pack:demo"] = plan
        output = io.StringIO()
        with redirect_stderr(output):
            app.on_unmount()
        self.assertIs(app.content_plans["pack:demo"], plan)
        self.assertIn(str(plan.transaction_root), output.getvalue())
        self.assertIn("cleanup failed", output.getvalue())
