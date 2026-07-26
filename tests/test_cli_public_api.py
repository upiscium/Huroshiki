from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import packctl


class PublicCliTest(unittest.TestCase):
    def test_add_direct_query_forms_preserve_provider_and_selector(self) -> None:
        cases = (
            ("mr:example", ("modrinth", "example")),
            ("cf:123", ("curseforge", "123")),
            (
                "https://modrinth.com/mod/example",
                ("modrinth", "https://modrinth.com/mod/example"),
            ),
            (
                "https://www.curseforge.com/minecraft/mc-mods/example",
                (
                    "curseforge",
                    "https://www.curseforge.com/minecraft/mc-mods/example",
                ),
            ),
            (
                "url:https://mods.example/private.jar",
                ("url", "https://mods.example/private.jar"),
            ),
            (
                "https://mods.example/private.jar",
                ("url", "https://mods.example/private.jar"),
            ),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                self.assertEqual(packctl.direct_project_selector(query), expected)

    def test_add_uses_lazy_transaction_api_without_an_outer_lock(self) -> None:
        args = type(
            "Args",
            (),
            {"pack": "demo", "query": "mr:example", "side": "client"},
        )()
        with patch(
            "huroshiki_core.add_mod_transactionally", return_value=7
        ) as add, patch.object(packctl, "ProjectLock") as project_lock:
            self.assertEqual(packctl.cmd_add(args), 7)

        add.assert_called_once_with("pack:demo", "modrinth", "example", "client")
        project_lock.assert_not_called()

    def test_add_preserves_tty_provider_selection_and_reports_core_errors(self) -> None:
        import huroshiki_core

        args = type(
            "Args",
            (),
            {"pack": "demo", "query": "search words", "side": "both"},
        )()
        with patch.object(packctl, "choose_provider", return_value="curseforge"), patch(
            "huroshiki_core.add_mod_transactionally",
            side_effect=huroshiki_core.HuroshikiError("refresh failed"),
        ) as add:
            with self.assertRaisesRegex(packctl.ConfigError, "refresh failed"):
                packctl.cmd_add(args)

        add.assert_called_once_with(
            "pack:demo", "curseforge", "search words", "both"
        )

    def test_help_exposes_public_commands_and_omits_removed_commands(self) -> None:
        help_text = packctl.parser().format_help()

        for command in ("list-templates", "publish", "serve"):
            self.assertIn(command, help_text)
        for command in ("migrate-template", "use", "current"):
            self.assertIsNone(re.search(rf"(?:[{{,]){command}(?:[,}}])", help_text))
            with self.subTest(command=command), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    packctl.parser().parse_args([command])

    def test_list_templates_is_a_public_human_readable_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            templates = Path(directory) / "templates"
            template = templates / "base"
            template.mkdir(parents=True)
            (template / "template.yaml").write_text(
                "id: base\ndisplay_name: Base\nenabled: true\n",
                encoding="utf-8",
            )
            stdout = StringIO()
            with patch.object(packctl, "TEMPLATES", templates), redirect_stdout(stdout):
                result = packctl.cmd_list_templates(type("Args", (), {})())

        self.assertEqual(result, 0)
        self.assertIn("TEMPLATE", stdout.getvalue())
        self.assertIn("base", stdout.getvalue())
        self.assertIn("Base", stdout.getvalue())

    def test_update_builds_only_after_a_successful_update(self) -> None:
        args = type("Args", (), {"pack": "demo", "build": True})()
        events: list[str] = []

        def update(_: str) -> int:
            events.append("update")
            return 0

        def build(_: str) -> int:
            events.append("build")
            return 0

        with patch("huroshiki_core.update_all", side_effect=update), patch.object(
            packctl, "build_pack", side_effect=build
        ):
            self.assertEqual(packctl.cmd_update(args), 0)
        self.assertEqual(events, ["update", "build"])

        events.clear()
        with patch("huroshiki_core.update_all", return_value=7), patch.object(
            packctl, "build_pack"
        ) as build_mock:
            self.assertEqual(packctl.cmd_update(args), 7)
        build_mock.assert_not_called()

    def test_serve_builds_then_runs_server_for_pack_dist(self) -> None:
        args = type("Args", (), {"pack": "demo", "port": 9090})()
        server = MagicMock()
        server.__enter__.return_value = server
        stdout = StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            packctl, "build_pack", return_value=0
        ) as build, patch.object(
            packctl, "get_pack_root", return_value=Path(directory) / "demo"
        ), patch.object(
            packctl, "ThreadingHTTPServer", return_value=server
        ) as server_type, redirect_stdout(stdout):
            self.assertEqual(packctl.cmd_serve(args), 0)

        build.assert_called_once_with("demo")
        self.assertEqual(server_type.call_args.args[0], ("127.0.0.1", 9090))
        self.assertIn("http://127.0.0.1:9090/", stdout.getvalue())
        server.serve_forever.assert_called_once_with()

    def test_publish_restarts_only_after_successful_deploy(self) -> None:
        args = type("Args", (), {"pack": "demo"})()
        snapshot = Path("/safe/snapshot")
        config = {
            "distribution": {"rsync_target": "host:/demo"},
            "minecraft_server": {
                "ssh_host": "server",
                "stack_dir": "/srv/demo",
                "service": "minecraft",
            },
        }
        patches = (
            patch.object(packctl, "ProjectLock", MagicMock()),
            patch.object(packctl, "_build_pack", return_value=0),
            patch.object(packctl, "load_pack_config", return_value=config),
            patch.object(packctl, "distribution_root", return_value=Path("/dist")),
            patch.object(packctl, "_make_deploy_snapshot", return_value=snapshot),
            patch.object(packctl, "distribution_digest", return_value="digest"),
            patch.object(packctl, "discard_deploy_snapshot"),
        )
        with patches[0], patches[1] as build, patches[2], patches[3], patches[4], patches[5], patches[6], patch.object(
            packctl, "_deploy_pack", return_value=0
        ) as deploy, patch.object(packctl, "run") as run:
            self.assertEqual(packctl.cmd_publish(args), 0)
        build.assert_called_once_with("demo")
        self.assertEqual(deploy.call_args.kwargs["confirmed_target"], "host:/demo")
        run.assert_called_once_with(
            ["ssh", "server", "cd /srv/demo && docker compose restart minecraft"]
        )

        with patches[0], patch.object(packctl, "_build_pack", return_value=8), patch.object(
            packctl, "load_pack_config"
        ) as load_config, patch.object(packctl, "run") as run:
            self.assertEqual(packctl.cmd_publish(args), 1)
        load_config.assert_not_called()
        run.assert_not_called()

    def test_publish_keeps_one_locked_configuration_across_deploy_and_restart(self) -> None:
        args = type("Args", (), {"pack": "demo"})()
        config = {
            "distribution": {"rsync_target": "first:/demo"},
            "minecraft_server": {
                "ssh_host": "first-server",
                "stack_dir": "/srv/first",
                "service": "first-service",
            },
        }
        lock = MagicMock()
        lock.__enter__.return_value = lock

        def mutate_configuration(*args, **kwargs):
            self.assertTrue(lock.__enter__.called)
            self.assertFalse(lock.__exit__.called)
            config["distribution"]["rsync_target"] = "second:/demo"
            config["minecraft_server"] = {
                "ssh_host": "second-server",
                "stack_dir": "/srv/second",
                "service": "second-service",
            }
            return 0

        with patch.object(packctl, "ProjectLock", return_value=lock) as lock_type, patch.object(
            packctl, "_build_pack", return_value=0
        ), patch.object(
            packctl, "load_pack_config", return_value=config
        ) as load_config, patch.object(
            packctl, "distribution_root", return_value=Path("/dist")
        ), patch.object(
            packctl, "_make_deploy_snapshot", return_value=Path("/snapshot")
        ), patch.object(
            packctl, "distribution_digest", return_value="digest"
        ), patch.object(
            packctl, "_deploy_pack", side_effect=mutate_configuration
        ) as deploy, patch.object(
            packctl, "discard_deploy_snapshot"
        ), patch.object(packctl, "run") as run:
            self.assertEqual(packctl.cmd_publish(args), 0)

        lock_type.assert_called_once_with("pack:demo", "publish")
        load_config.assert_called_once_with("demo")
        self.assertEqual(deploy.call_args.kwargs["confirmed_target"], "first:/demo")
        run.assert_called_once_with(
            [
                "ssh",
                "first-server",
                "cd /srv/first && docker compose restart first-service",
            ]
        )

    def test_justfile_contains_only_development_tasks(self) -> None:
        justfile = (Path(__file__).resolve().parents[1] / "Justfile").read_text(
            encoding="utf-8"
        )
        recipes = [
            line.removesuffix(":")
            for line in justfile.splitlines()
            if line and not line.startswith((" ", "#", "set ")) and line.endswith(":")
        ]

        self.assertEqual(recipes, ["default", "test-huroshiki", "check"])
        self.assertNotIn("MODPACK", justfile)


if __name__ == "__main__":
    unittest.main()
