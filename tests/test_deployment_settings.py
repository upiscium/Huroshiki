from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import Input, Static

import deploy_support
import huroshiki
import huroshiki_core as core
import packctl


class RsyncTargetPartsTest(unittest.TestCase):
    def test_split_and_join_supported_targets(self) -> None:
        cases = (
            ("host:/packs/demo", "host", "/packs/demo"),
            ("deploy@host:/packs/demo", "deploy@host", "/packs/demo"),
            ("[2001:db8::1]:/packs/demo", "[2001:db8::1]", "/packs/demo"),
            (
                "deploy@[2001:db8::1]:/packs/demo",
                "deploy@[2001:db8::1]",
                "/packs/demo",
            ),
        )
        for target, host, path in cases:
            with self.subTest(target=target):
                parts = deploy_support.split_rsync_target(target)
                self.assertEqual((parts.host, parts.path), (host, path))
                self.assertEqual(deploy_support.join_rsync_target(host, path), target)

    def test_host_only_change_preserves_path(self) -> None:
        parts = deploy_support.split_rsync_target("old:/srv/packs/demo")
        self.assertEqual(
            deploy_support.join_rsync_target("new", parts.path),
            "new:/srv/packs/demo",
        )


class DeploymentSettingsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.pack = self.packs / "demo"
        self.pack.mkdir(parents=True)
        self.templates.mkdir()
        (self.pack / "pack.yaml").write_text(
            "id: demo\n"
            "display_name: Demo\n"
            "enabled: true\n"
            "distribution:\n  rsync_target: committed:/packs/demo\n"
            "minecraft_server:\n"
            "  ssh_host: committed-host\n"
            "  stack_dir: /srv/committed\n"
            "  service: minecraft\n",
            encoding="utf-8",
        )
        (self.pack / "pack.local.yaml").write_text(
            "distribution:\n  rsync_target: local:/packs/demo\n"
            "minecraft_server:\n  ssh_host: local-host\n"
            "url_max_jar_size_bytes: 12345\n",
            encoding="utf-8",
        )
        self.patches = (
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.templates),
        )
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def test_reads_effective_values_and_sources(self) -> None:
        self.assertEqual(
            packctl.deployment_settings("demo"),
            packctl.DeploymentSettings(
                "local:/packs/demo",
                "local-host",
                "/srv/committed",
                "minecraft",
            ),
        )
        self.assertEqual(
            packctl.deployment_settings_sources("demo"),
            packctl.DeploymentSettingsSources(
                "local", "local", "committed", "committed"
            ),
        )

    def test_partial_update_preserves_sibling_local_keys(self) -> None:
        result = packctl.update_deployment_settings("demo", ssh_host="new-host")

        self.assertEqual(result.ssh_host, "new-host")
        local = packctl.load_yaml(self.pack / "pack.local.yaml")
        self.assertEqual(local["distribution"]["rsync_target"], "local:/packs/demo")
        self.assertEqual(local["url_max_jar_size_bytes"], 12345)
        self.assertEqual(local["minecraft_server"], {"ssh_host": "new-host"})

    def test_noop_does_not_publish_local_file(self) -> None:
        before = (self.pack / "pack.local.yaml").read_bytes()
        with patch.object(packctl, "_write_yaml_atomic") as write:
            result = packctl.update_deployment_settings(
                "demo",
                ssh_host="local-host",
            )

        self.assertEqual(result.ssh_host, "local-host")
        write.assert_not_called()
        self.assertEqual((self.pack / "pack.local.yaml").read_bytes(), before)

    def test_invalid_values_are_rejected_without_writes(self) -> None:
        cases = (
            {"ssh_host": "bad host"},
            {"stack_dir": "relative"},
            {"service": "bad service"},
            {"rsync_target": "host:relative"},
        )
        before = (self.pack / "pack.local.yaml").read_bytes()
        for values in cases:
            with self.subTest(values=values), self.assertRaises(packctl.ConfigError):
                packctl.update_deployment_settings("demo", **values)
            self.assertEqual((self.pack / "pack.local.yaml").read_bytes(), before)

    def test_external_change_is_not_overwritten(self) -> None:
        original_write = packctl._write_yaml_atomic

        def race(*args: object, **kwargs: object) -> None:
            (self.pack / "pack.local.yaml").write_text(
                "distribution:\n  rsync_target: external:/packs/demo\n",
                encoding="utf-8",
            )
            original_write(*args, **kwargs)

        with patch.object(packctl, "_write_yaml_atomic", side_effect=race):
            with self.assertRaisesRegex(packctl.ConfigError, "changed while applying"):
                packctl.update_deployment_settings("demo", ssh_host="new-host")

        self.assertEqual(
            packctl.load_yaml(self.pack / "pack.local.yaml"),
            {"distribution": {"rsync_target": "external:/packs/demo"}},
        )

    def test_change_after_read_is_rejected_by_baseline(self) -> None:
        baseline = packctl.deployment_settings_baseline("demo")
        local_path = self.pack / "pack.local.yaml"
        local_path.write_text(
            local_path.read_text(encoding="utf-8") + "url_allow_private_networks: true\n",
            encoding="utf-8",
        )
        external = local_path.read_bytes()

        with self.assertRaisesRegex(packctl.ConfigError, "changed after it was loaded"):
            packctl.update_deployment_settings(
                "demo",
                service="server",
                expected_baseline=baseline.snapshot,
            )

        self.assertEqual(local_path.read_bytes(), external)

    def test_publication_failure_leaves_existing_local_file(self) -> None:
        before = (self.pack / "pack.local.yaml").read_bytes()
        with patch.object(
            packctl,
            "_write_yaml_atomic",
            side_effect=packctl.ConfigError("publication failed"),
        ):
            with self.assertRaisesRegex(packctl.ConfigError, "publication failed"):
                packctl.update_deployment_settings("demo", service="server")

        self.assertEqual((self.pack / "pack.local.yaml").read_bytes(), before)


class DeploymentSettingsCoreTest(unittest.TestCase):
    def test_tui_wrapper_only_writes_changed_fields(self) -> None:
        current = packctl.DeploymentSettings(
            "old:/packs/demo", "ssh", "/srv/demo", "minecraft"
        )
        proposed = packctl.DeploymentSettings(
            "new:/packs/demo", "ssh", "/srv/demo", "minecraft"
        )
        baseline = type(
            "Baseline",
            (),
            {"settings": current, "snapshot": object()},
        )()
        with patch.object(
            core.packctl,
            "update_deployment_settings",
            return_value=proposed,
        ) as update:
            self.assertEqual(
                core.update_deployment_settings(
                    "pack:demo",
                    proposed,
                    expected_baseline=baseline,
                ),
                proposed,
            )

        kwargs = update.call_args.kwargs
        self.assertEqual(kwargs["rsync_target"], "new:/packs/demo")
        self.assertIs(kwargs["ssh_host"], packctl.UNSET)
        self.assertIs(kwargs["stack_dir"], packctl.UNSET)
        self.assertIs(kwargs["service"], packctl.UNSET)
        self.assertIs(kwargs["expected_baseline"], baseline.snapshot)


PROJECT = core.ProjectInfo(
    kind="pack",
    project_id="demo",
    display_name="Demo Pack",
    minecraft="1.21.1",
    loader="neoforge",
    loader_version="21.1.0",
    enabled=True,
)
CURRENT = core.DeploymentSettings(
    "deploy@host:/packs/demo",
    "minecraft",
    "/srv/demo",
    "minecraft",
)
BASELINE = type("Baseline", (), {"settings": CURRENT})()
UPDATED = core.DeploymentSettings(
    "new-host:/packs/demo",
    "minecraft",
    "/srv/demo",
    "minecraft",
)
UPDATED_BASELINE = type("Baseline", (), {"settings": UPDATED})()


class _SettingsTestApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def on_mount(self) -> None:
        self.push_screen(huroshiki.ProjectScreen("pack:demo"))

    def open_settings(self, project_key: str) -> None:
        self.switch_screen(huroshiki.SettingsScreen(project_key))

    def open_deployment_settings(self, project_key: str) -> None:
        self.switch_screen(huroshiki.DeploymentSettingsScreen(project_key))

    def open_project(self, project_key: str) -> bool:
        self.switch_screen(huroshiki.ProjectScreen(project_key))
        return True


class DeploymentSettingsTuiTest(unittest.IsolatedAsyncioTestCase):
    async def test_project_settings_deployment_navigation(self) -> None:
        with (
            patch.object(core, "project_info", return_value=PROJECT),
            patch.object(core, "deployment_settings_baseline", return_value=BASELINE),
        ):
            app = _SettingsTestApp()
            async with app.run_test() as pilot:
                await pilot.press("s")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.SettingsScreen)

                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.DeploymentSettingsScreen)
                self.assertEqual(
                    app.screen.query_one("#deployment-rsync-host", Input).value,
                    "deploy@host",
                )

    async def test_review_modal_and_cancel_leave_settings_unchanged(self) -> None:
        with (
            patch.object(core, "project_info", return_value=PROJECT),
            patch.object(core, "deployment_settings_baseline", return_value=BASELINE),
            patch.object(core, "update_deployment_settings") as update,
        ):
            app = _SettingsTestApp()
            async with app.run_test() as pilot:
                await pilot.press("s", "enter")
                await pilot.pause()
                screen = app.screen
                screen.query_one("#deployment-rsync-host", Input).value = "new-host"

                await pilot.press("ctrl+s")
                await pilot.pause()
                modal = app.screen
                self.assertIsInstance(modal, huroshiki.ConfirmModal)
                message = modal.query_one("#modal-message", Static)
                self.assertIn("Save to: pack.local.yaml", str(message.content))
                self.assertIn(
                    "deploy@host:/packs/demo -> new-host:/packs/demo",
                    str(message.content),
                )

                await pilot.press("escape")
                await pilot.pause()
                update.assert_not_called()
                self.assertIsInstance(app.screen, huroshiki.DeploymentSettingsScreen)

    async def test_confirmed_save_reloads_effective_values(self) -> None:
        with (
            patch.object(core, "project_info", return_value=PROJECT),
            patch.object(
                core,
                "deployment_settings_baseline",
                side_effect=(BASELINE, UPDATED_BASELINE),
            ),
            patch.object(
                core,
                "update_deployment_settings",
                return_value=UPDATED,
            ) as update,
        ):
            app = _SettingsTestApp()
            async with app.run_test() as pilot:
                await pilot.press("s", "enter")
                await pilot.pause()
                screen = app.screen
                screen.query_one("#deployment-rsync-host", Input).value = "new-host"

                await pilot.press("ctrl+s")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()

                update.assert_called_once_with(
                    "pack:demo",
                    UPDATED,
                    expected_baseline=BASELINE,
                )
                self.assertEqual(
                    screen.query_one("#deployment-rsync-host", Input).value,
                    "new-host",
                )


if __name__ == "__main__":
    unittest.main()
