from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import Input, TextArea

import huroshiki
import huroshiki_core as core
import packctl


def committed_yaml(public_url: str | None = None) -> str:
    url_line = f"  public_pack_url: {public_url}\n" if public_url is not None else ""
    return (
        "id: demo\n"
        "display_name: Demo\n"
        "enabled: true\n"
        "distribution:\n"
        "  rsync_target: deploy:/packs/demo\n"
        f"{url_line}"
        "minecraft_server:\n"
        "  ssh_host: minecraft\n"
        "  stack_dir: /srv/demo\n"
        "  service: minecraft\n"
    )


class PublicPackUrlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.pack = self.packs / "demo"
        self.pack.mkdir(parents=True)
        self.templates.mkdir()
        self.committed = self.pack / "pack.yaml"
        self.local = self.pack / "pack.local.yaml"
        self.committed.write_text(committed_yaml(), encoding="utf-8")
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


class PublicPackUrlValidationTest(PublicPackUrlTestCase):
    def test_accepts_https_pack_toml_and_query(self) -> None:
        url = "https://packs.example/demo/pack.toml?channel=stable"
        self.assertEqual(packctl.validate_public_pack_url(url), url)

    def test_rejects_unsafe_or_wrong_urls(self) -> None:
        cases = (
            "http://packs.example/demo/pack.toml",
            "https://user@packs.example/demo/pack.toml",
            "https://user:secret@packs.example/demo/pack.toml",
            "https://packs.example/demo/pack.toml#latest",
            "https://packs.example/demo/index.toml",
            "https:///demo/pack.toml",
            "https://packs.example/demo/pack.toml\nignored",
        )
        for url in cases:
            with self.subTest(url=url), self.assertRaises(packctl.ConfigError):
                packctl.validate_public_pack_url(url)

    def test_committed_url_is_validated_even_when_local_overrides_it(self) -> None:
        self.committed.write_text(
            committed_yaml("http://invalid.example/pack.toml"),
            encoding="utf-8",
        )
        self.local.write_text(
            "distribution:\n"
            "  public_pack_url: https://valid.example/pack.toml\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(packctl.ConfigError, "must use https"):
            packctl.public_pack_url_info("demo")


class PublicPackUrlApiTest(PublicPackUrlTestCase):
    def test_reports_committed_local_and_unset_sources(self) -> None:
        self.assertEqual(
            packctl.public_pack_url_info("demo"),
            packctl.PublicPackUrlInfo(None, "unset", None),
        )
        committed_url = "https://packs.example/demo/pack.toml"
        self.committed.write_text(committed_yaml(committed_url), encoding="utf-8")
        committed = packctl.public_pack_url_info("demo")
        self.assertEqual((committed.value, committed.source), (committed_url, "committed"))
        self.assertEqual(
            committed.installer_command,
            f"java -jar packwiz-installer-bootstrap.jar {committed_url}",
        )

        local_url = "https://local.example/demo/pack.toml"
        self.local.write_text(
            "distribution:\n"
            f"  public_pack_url: {local_url}\n",
            encoding="utf-8",
        )
        local = packctl.public_pack_url_info("demo")
        self.assertEqual((local.value, local.source), (local_url, "local"))

    def test_set_preserves_local_siblings_and_noop_skips_write(self) -> None:
        self.local.write_text(
            "distribution:\n  rsync_target: local:/packs/demo\n"
            "url_max_jar_size_bytes: 12345\n",
            encoding="utf-8",
        )
        url = "https://packs.example/demo/pack.toml"
        info = packctl.set_public_pack_url("demo", url)
        self.assertEqual((info.value, info.source), (url, "local"))
        local = packctl.load_yaml(self.local)
        self.assertEqual(local["distribution"]["rsync_target"], "local:/packs/demo")
        self.assertEqual(local["url_max_jar_size_bytes"], 12345)

        with patch.object(packctl, "_write_yaml_atomic") as write:
            self.assertEqual(packctl.set_public_pack_url("demo", url), info)
        write.assert_not_called()

    def test_clear_removes_only_override_and_falls_back_to_committed(self) -> None:
        committed_url = "https://committed.example/demo/pack.toml"
        local_url = "https://local.example/demo/pack.toml"
        self.committed.write_text(committed_yaml(committed_url), encoding="utf-8")
        self.local.write_text(
            "distribution:\n"
            "  rsync_target: local:/packs/demo\n"
            f"  public_pack_url: {local_url}\n",
            encoding="utf-8",
        )

        info = packctl.clear_local_public_pack_url("demo")

        self.assertEqual((info.value, info.source), (committed_url, "committed"))
        self.assertEqual(
            packctl.load_yaml(self.local),
            {"distribution": {"rsync_target": "local:/packs/demo"}},
        )

    def test_clear_removes_empty_local_distribution(self) -> None:
        self.local.write_text(
            "distribution:\n"
            "  public_pack_url: https://local.example/demo/pack.toml\n"
            "url_allow_private_networks: false\n",
            encoding="utf-8",
        )
        packctl.clear_local_public_pack_url("demo")
        self.assertEqual(
            packctl.load_yaml(self.local),
            {"url_allow_private_networks": False},
        )

    def test_baseline_conflict_and_publication_failure_preserve_external_data(self) -> None:
        baseline = packctl.public_pack_url_baseline("demo")
        self.local.write_text("url_allow_private_networks: true\n", encoding="utf-8")
        external = self.local.read_bytes()
        with self.assertRaisesRegex(packctl.ConfigError, "changed after it was loaded"):
            packctl.set_public_pack_url(
                "demo",
                "https://packs.example/demo/pack.toml",
                expected_baseline=baseline,
            )
        self.assertEqual(self.local.read_bytes(), external)

        with patch.object(
            packctl,
            "_write_yaml_atomic",
            side_effect=packctl.ConfigError("publication failed"),
        ):
            with self.assertRaisesRegex(packctl.ConfigError, "publication failed"):
                packctl.set_public_pack_url(
                    "demo", "https://packs.example/demo/pack.toml"
                )
        self.assertEqual(self.local.read_bytes(), external)

    def test_cli_raw_output_and_unset_exit_contract(self) -> None:
        args = type("Args", (), {"pack": "demo", "raw": True})()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(packctl.cmd_show_pack_url(args), 1)
        self.assertEqual(output.getvalue(), "")

        url = "https://packs.example/demo/pack.toml"
        packctl.set_public_pack_url("demo", url)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(packctl.cmd_show_pack_url(args), 0)
        self.assertEqual(output.getvalue(), url + "\n")

    def test_parser_routes_pack_url_commands(self) -> None:
        parser = packctl.parser()
        self.assertIs(
            parser.parse_args(["show-pack-url", "demo"]).func,
            packctl.cmd_show_pack_url,
        )
        self.assertIs(
            parser.parse_args(["set-pack-url", "demo", "https://x/pack.toml"]).func,
            packctl.cmd_set_pack_url,
        )
        self.assertIs(
            parser.parse_args(["clear-pack-url", "demo"]).func,
            packctl.cmd_clear_pack_url,
        )


PROJECT = core.ProjectInfo(
    kind="pack",
    project_id="demo",
    display_name="Demo Pack",
    minecraft="1.21.1",
    loader="neoforge",
    loader_version="21.1.0",
    enabled=True,
)
LOCAL_INFO = core.PublicPackUrlInfo(
    "https://local.example/demo/pack.toml",
    "local",
    "java -jar packwiz-installer-bootstrap.jar https://local.example/demo/pack.toml",
)
BASELINE = type(
    "Baseline",
    (),
    {
        "info": LOCAL_INFO,
        "committed_value": "https://committed.example/demo/pack.toml",
    },
)()
NEW_INFO = core.PublicPackUrlInfo(
    "https://new.example/demo/pack.toml",
    "local",
    "java -jar packwiz-installer-bootstrap.jar https://new.example/demo/pack.toml",
)
NEW_BASELINE = type(
    "Baseline",
    (),
    {
        "info": NEW_INFO,
        "committed_value": "https://committed.example/demo/pack.toml",
    },
)()
COMMITTED_INFO = core.PublicPackUrlInfo(
    "https://committed.example/demo/pack.toml",
    "committed",
    "java -jar packwiz-installer-bootstrap.jar https://committed.example/demo/pack.toml",
)
COMMITTED_BASELINE = type(
    "Baseline",
    (),
    {
        "info": COMMITTED_INFO,
        "committed_value": COMMITTED_INFO.value,
    },
)()


class _PublicUrlTestApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def on_mount(self) -> None:
        self.push_screen(huroshiki.SettingsScreen("pack:demo"))

    def open_client_distribution_settings(self, project_key: str) -> None:
        self.switch_screen(huroshiki.ClientDistributionScreen(project_key))

    def open_deployment_settings(self, project_key: str) -> None:
        raise AssertionError("unexpected Deployment navigation")

    def open_settings(self, project_key: str) -> None:
        self.switch_screen(huroshiki.SettingsScreen(project_key))

    def open_project(self, project_key: str) -> bool:
        return True


class PublicPackUrlTuiTest(unittest.IsolatedAsyncioTestCase):
    async def test_settings_navigation_and_selectable_display(self) -> None:
        with (
            patch.object(core, "project_info", return_value=PROJECT),
            patch.object(core, "public_pack_url_baseline", return_value=BASELINE),
        ):
            app = _PublicUrlTestApp()
            async with app.run_test() as pilot:
                await pilot.press("j", "enter")
                await pilot.pause()

                self.assertIsInstance(app.screen, huroshiki.ClientDistributionScreen)
                self.assertEqual(
                    app.screen.query_one("#public-pack-url-display", TextArea).text,
                    LOCAL_INFO.value,
                )
                self.assertTrue(
                    app.screen.query_one("#public-pack-command-display", TextArea).read_only
                )

    async def test_edit_review_cancel_and_clear_cancel_do_not_mutate(self) -> None:
        with (
            patch.object(core, "project_info", return_value=PROJECT),
            patch.object(core, "public_pack_url_baseline", return_value=BASELINE),
            patch.object(core, "set_public_pack_url") as set_url,
            patch.object(core, "clear_local_public_pack_url") as clear_url,
        ):
            app = _PublicUrlTestApp()
            async with app.run_test() as pilot:
                await pilot.press("j", "enter", "e")
                await pilot.pause()
                modal = app.screen
                self.assertIsInstance(modal, huroshiki.PublicPackUrlEditModal)
                modal.query_one("#public-pack-url-input", Input).value = (
                    "https://new.example/demo/pack.toml"
                )

                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                await pilot.press("escape")
                await pilot.pause()
                set_url.assert_not_called()

                await pilot.press("c")
                await pilot.pause()
                self.assertIsInstance(app.screen, huroshiki.ConfirmModal)
                await pilot.press("escape")
                await pilot.pause()
                clear_url.assert_not_called()

    async def test_confirmed_edit_and_clear_reload_effective_info(self) -> None:
        with (
            patch.object(core, "project_info", return_value=PROJECT),
            patch.object(
                core,
                "public_pack_url_baseline",
                side_effect=(BASELINE, NEW_BASELINE, COMMITTED_BASELINE),
            ),
            patch.object(core, "set_public_pack_url", return_value=NEW_INFO) as set_url,
            patch.object(
                core,
                "clear_local_public_pack_url",
                return_value=COMMITTED_INFO,
            ) as clear_url,
        ):
            app = _PublicUrlTestApp()
            async with app.run_test() as pilot:
                await pilot.press("j", "enter", "e")
                await pilot.pause()
                app.screen.query_one("#public-pack-url-input", Input).value = NEW_INFO.value
                await pilot.press("enter", "enter")
                await pilot.pause()

                screen = app.screen
                set_url.assert_called_once_with(
                    "pack:demo",
                    NEW_INFO.value,
                    expected_baseline=BASELINE,
                )
                self.assertEqual(
                    screen.query_one("#public-pack-url-display", TextArea).text,
                    NEW_INFO.value,
                )

                await pilot.press("c", "enter")
                await pilot.pause()
                clear_url.assert_called_once_with(
                    "pack:demo",
                    expected_baseline=NEW_BASELINE,
                )
                self.assertEqual(
                    screen.query_one("#public-pack-url-display", TextArea).text,
                    COMMITTED_INFO.value,
                )


if __name__ == "__main__":
    unittest.main()
