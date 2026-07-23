from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import Static

import huroshiki
import huroshiki_core as core
import packctl


PROJECT = core.ProjectInfo(
    kind="pack",
    project_id="demo",
    display_name="Demo Pack",
    minecraft="1.21.1",
    loader="neoforge",
    loader_version="21.1.0",
    enabled=True,
)


class _ProjectTestApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def on_mount(self) -> None:
        self.push_screen(huroshiki.ProjectScreen("pack:demo"))


class ProjectActionConfirmationTest(unittest.TestCase):
    def test_confirmation_uses_merged_remote_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packs = Path(directory) / "packs"
            pack = packs / "demo"
            pack.mkdir(parents=True)
            (pack / "pack.yaml").write_text(
                """id: demo
distribution:
  rsync_target: base:/packs/demo
minecraft_server:
  ssh_host: base-host
  stack_dir: /srv/base
  service: minecraft
""",
                encoding="utf-8",
            )
            (pack / "pack.local.yaml").write_text(
                """distribution:
  rsync_target: deploy@remote:/packs/demo
minecraft_server:
  ssh_host: ops@remote
  stack_dir: /srv/demo
""",
                encoding="utf-8",
            )

            with patch.object(packctl, "PACKS", packs):
                self.assertEqual(
                    core.project_action_confirmation("pack:demo", "deploy"),
                    (
                        "Pack: demo",
                        "Action: deploy",
                        "Rsync target: deploy@remote:/packs/demo",
                    ),
                )
                self.assertEqual(
                    core.project_action_confirmation("pack:demo", "restart"),
                    (
                        "Pack: demo",
                        "Action: restart",
                        "SSH target: ops@remote",
                        "Stack directory: /srv/demo",
                        "Compose service: minecraft",
                    ),
                )
                self.assertEqual(
                    core.project_action_confirmation("pack:demo", "publish"),
                    (
                        "Pack: demo",
                        "Action: publish",
                        "Rsync target: deploy@remote:/packs/demo",
                        "SSH target: ops@remote",
                        "Stack directory: /srv/demo",
                        "Compose service: minecraft",
                    ),
                )
                self.assertIsNone(
                    core.project_action_confirmation("pack:demo", "build")
                )

    def test_remote_action_aborts_if_config_changes_after_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packs = Path(directory) / "packs"
            pack = packs / "demo"
            pack.mkdir(parents=True)
            (pack / "pack.yaml").write_text(
                "id: demo\ndistribution:\n  rsync_target: old:/demo\n",
                encoding="utf-8",
            )

            with patch.object(packctl, "PACKS", packs), patch.object(
                core.subprocess, "run"
            ) as subprocess_run:
                confirmation = core.project_action_confirmation("pack:demo", "deploy")
                with self.assertRaisesRegex(
                    core.HuroshikiError, "changed after preview"
                ):
                    core.run_project_action("pack:demo", "deploy", confirmation)

            subprocess_run.assert_not_called()


class ProjectScreenActionTest(unittest.IsolatedAsyncioTestCase):
    async def test_publish_confirmation_contents_and_cancel(self) -> None:
        preview = core.ProjectDeployPreview(
            "pack:demo",
            "publish",
            "[deploy]@remote:/packs/demo",
            "digest",
            (packctl.RsyncChange("deleted", "old.jar", "*deleting   old.jar"),),
            ("*deleting   old.jar",),
            ("ops@remote", "/srv/demo", "minecraft"),
        )
        with (
            patch.object(huroshiki.core, "project_info", return_value=PROJECT),
            patch.object(
                huroshiki.core,
                "prepare_deploy_preview",
                return_value=preview,
            ),
            patch.object(huroshiki.core, "run_project_action") as run_action,
            patch.object(huroshiki.core, "discard_deploy_preview") as discard,
        ):
            app = _ProjectTestApp()
            with patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test() as pilot:
                    await pilot.press("j", "enter")
                    await pilot.pause()

                    modal = app.screen
                    self.assertIsInstance(modal, huroshiki.ConfirmModal)
                    self.assertEqual(modal.lines, list(preview.confirmation_lines))
                    message = modal.query_one("#modal-message", Static)
                    self.assertEqual(
                        message.content, "\n".join(preview.confirmation_lines)
                    )
                    self.assertIn("[deploy]", str(message.render()))
                    self.assertIn("1 deleted", str(message.render()))

                    await pilot.press("escape")
                    await pilot.pause()
                    run_action.assert_not_called()
                    discard.assert_called_once_with(preview)

    async def test_confirmed_deploy_runs_action(self) -> None:
        preview = core.ProjectDeployPreview(
            "pack:demo", "deploy", "host:/demo", "digest", (), ()
        )
        with (
            patch.object(huroshiki.core, "project_info", return_value=PROJECT),
            patch.object(
                huroshiki.core,
                "prepare_deploy_preview",
                return_value=preview,
            ),
            patch.object(
                huroshiki.core,
                "run_project_action",
                return_value=0,
            ) as run_action,
        ):
            app = _ProjectTestApp()
            with patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test() as pilot:
                    await pilot.press("j", "j", "enter")
                    await pilot.pause()
                    run_action.assert_not_called()

                    await pilot.press("enter")
                    await pilot.pause()
                    run_action.assert_called_once_with(
                        "pack:demo",
                        "deploy",
                        preview,
                    )

    async def test_build_runs_immediately(self) -> None:
        with (
            patch.object(huroshiki.core, "project_info", return_value=PROJECT),
            patch.object(
                huroshiki.core,
                "project_action_confirmation",
                return_value=None,
            ) as confirmation,
            patch.object(
                huroshiki.core,
                "run_project_action",
                return_value=0,
            ) as run_action,
        ):
            app = _ProjectTestApp()
            with patch.object(app, "suspend", return_value=nullcontext()):
                async with app.run_test() as pilot:
                    await pilot.press("enter")
                    await pilot.pause()

                    confirmation.assert_called_once_with("pack:demo", "build")
                    run_action.assert_called_once_with("pack:demo", "build", None)
                    self.assertIsInstance(app.screen, huroshiki.ProjectScreen)


if __name__ == "__main__":
    unittest.main()
