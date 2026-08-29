from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import huroshiki
import huroshiki_core as core


PROJECT = core.ProjectInfo(
    kind="pack", project_id="demo", display_name="Demo Pack",
    minecraft="1.21.1", loader="neoforge", loader_version="21.1.0", enabled=True,
)


class ProjectActionTest(unittest.TestCase):
    def test_project_actions_only_exposes_publish_for_packs(self) -> None:
        self.assertEqual(core.project_actions("pack:demo"), ("publish",))
        self.assertEqual(core.project_actions("template:base"), ("create MODPACK", "validate"))


class ProjectScreenPublishTest(unittest.IsolatedAsyncioTestCase):
    async def test_selecting_publish_opens_publish_without_legacy_deploy_calls(self) -> None:
        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "prepare_deploy_preview", side_effect=AssertionError
        ), patch.object(huroshiki.core, "run_project_action", side_effect=AssertionError), patch.object(
            huroshiki.HuroshikiApp, "open_publish"
        ) as open_publish:
            app = huroshiki.HuroshikiApp("pack:demo")
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)
                app.screen.run_selected()
                await pilot.pause()
            open_publish.assert_called_once_with("pack:demo", "Demo Pack")

    async def test_publish_worker_is_named_non_daemon_and_confirmation_uses_exact_plan(self) -> None:
        plan = object()
        owner = huroshiki.PackPublishOwner("pack:demo", threading.Event(), 456.0)
        app = huroshiki.HuroshikiApp()
        seen = {}

        def target() -> None:
            seen["thread"] = threading.current_thread()
            owner.plan = plan
            owner.done.set()

        app.start_publish_worker(owner, target)
        owner.thread.join(2)
        self.assertFalse(owner.thread.daemon)
        self.assertTrue(owner.thread.name.startswith("huroshiki-publish-pack-demo"))
        self.assertIs(seen["thread"], owner.thread)
        self.assertTrue(app.publish_worker_finished(owner))

    def test_confirmation_execution_passes_the_same_plan_controls(self) -> None:
        plan = object()
        owner = huroshiki.PackPublishOwner("pack:demo", threading.Event(), 456.0, plan=plan)
        screen = huroshiki.PublishScreen("pack:demo")
        screen.owner = owner
        with patch.object(huroshiki.core, "execute_pack_publish", return_value="result") as execute:
            screen._execute()
        execute.assert_called_once_with(
            plan, cancel_event=owner.cancel_event, deadline=owner.deadline,
            progress=screen._set_progress,
        )

    async def test_planning_failure_releases_owner(self) -> None:
        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "plan_pack_publish", side_effect=RuntimeError("planning failed")
        ):
            app = huroshiki.HuroshikiApp()
            async with app.run_test() as pilot:
                app.open_publish("pack:demo", "Demo Pack")
                await pilot.pause(0.2)
                screen = app.screen
                self.assertIsInstance(screen, huroshiki.PublishScreen)
                self.assertNotIn("pack:demo", app.publish_owners)

    async def test_cancelled_planning_completion_does_not_open_confirmation(self) -> None:
        started = threading.Event()
        release = threading.Event()
        plan = object()

        def planner(*_args, **_kwargs):
            started.set()
            release.wait(2)
            return plan

        with patch.object(huroshiki.core, "project_info", return_value=PROJECT), patch.object(
            huroshiki.core, "plan_pack_publish", side_effect=planner
        ), patch.object(huroshiki.core, "execute_pack_publish") as execute:
            app = huroshiki.HuroshikiApp("pack:demo")
            async with app.run_test() as pilot:
                await pilot.pause()
                app.open_publish("pack:demo", "Demo Pack")
                await pilot.pause()
                self.assertTrue(started.wait(1))
                await pilot.press("escape")
                release.set()
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, huroshiki.ProjectScreen)
                self.assertNotIn("pack:demo", app.publish_owners)
                execute.assert_not_called()

    def test_cancel_requests_event_without_releasing_active_owner(self) -> None:
        app = huroshiki.HuroshikiApp()
        owner = huroshiki.PackPublishOwner("pack:demo", threading.Event(), 456.0)
        app.publish_owners[owner.project_key] = owner
        owner.navigation_pending = True
        owner.cancel_event.set()
        self.assertTrue(owner.cancel_event.is_set())
        self.assertIn(owner.project_key, app.publish_owners)
