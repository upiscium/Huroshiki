from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

import packctl


class PublicCliTest(unittest.TestCase):
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
        with tempfile.TemporaryDirectory() as directory, patch.object(
            packctl, "build_pack", return_value=0
        ) as build, patch.object(
            packctl, "get_pack_root", return_value=Path(directory) / "demo"
        ), patch.object(
            packctl, "ThreadingHTTPServer", return_value=server
        ) as server_type:
            self.assertEqual(packctl.cmd_serve(args), 0)

        build.assert_called_once_with("demo")
        self.assertEqual(server_type.call_args.args[0], ("", 9090))
        server.serve_forever.assert_called_once_with()

    def test_publish_restarts_only_after_successful_deploy(self) -> None:
        args = type("Args", (), {"pack": "demo"})()
        with patch.object(packctl, "deploy_pack", return_value=0) as deploy, patch.object(
            packctl, "cmd_restart", return_value=0
        ) as restart:
            self.assertEqual(packctl.cmd_publish(args), 0)
        self.assertEqual(deploy.call_args, call("demo", build=True))
        restart.assert_called_once_with(args)

        with patch.object(packctl, "deploy_pack", return_value=8), patch.object(
            packctl, "cmd_restart"
        ) as restart:
            self.assertEqual(packctl.cmd_publish(args), 8)
        restart.assert_not_called()

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
