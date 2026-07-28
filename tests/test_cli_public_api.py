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

        for command in (
            "list-templates",
            "publish",
            "serve",
            "show-deployment",
            "set-deployment",
            "show-url-policy",
            "set-url-policy",
            "show-template-loader-version",
            "set-template-loader-version",
        ):
            self.assertIn(command, help_text)
        for command in ("migrate-template", "use", "current"):
            self.assertIsNone(re.search(rf"(?:[{{,]){command}(?:[,}}])", help_text))
            with self.subTest(command=command), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    packctl.parser().parse_args([command])

    def test_parser_routes_new_setting_commands(self) -> None:
        self.assertIs(
            packctl.parser().parse_args(["show-deployment", "demo"]).func,
            packctl.cmd_show_deployment,
        )
        self.assertIs(
            packctl.parser().parse_args(["set-deployment", "demo"]).func,
            packctl.cmd_set_deployment,
        )
        self.assertIs(
            packctl.parser().parse_args(["show-url-policy", "pack", "demo"]).func,
            packctl.cmd_show_url_policy,
        )
        self.assertIs(
            packctl.parser().parse_args(["show-template-loader-version", "demo"]).func,
            packctl.cmd_show_template_loader_version,
        )

    def test_parse_bool_flag_forms_for_url_policy(self) -> None:
        self.assertTrue(packctl._normalize_bool_flag("yes"))
        self.assertTrue(packctl._normalize_bool_flag("TRUE"))
        self.assertTrue(packctl._normalize_bool_flag("1"))
        self.assertTrue(packctl._normalize_bool_flag("on"))
        self.assertFalse(packctl._normalize_bool_flag("no"))
        self.assertFalse(packctl._normalize_bool_flag("FALSE"))
        self.assertFalse(packctl._normalize_bool_flag("0"))
        self.assertFalse(packctl._normalize_bool_flag("off"))
        self.assertIsNone(packctl.parser().parse_args(["set-url-policy", "pack", "demo"]).allow_private_networks)
        self.assertFalse(
            packctl.parser()
            .parse_args(["set-url-policy", "pack", "demo", "--allow-private-networks", "off"])
            .allow_private_networks
        )

    def test_parser_rejects_non_positive_url_policy_size(self) -> None:
        for value in ("0", "-1", "invalid"):
            with self.subTest(value=value), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    packctl.parser().parse_args(
                        ["set-url-policy", "pack", "demo", "--max-size", value]
                    )

    def test_set_and_show_deployment_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            pack_root = packs / "demo"
            pack_root.mkdir(parents=True)
            (pack_root / "pack.yaml").write_text(
                "id: demo\ndisplay_name: Demo\nenabled: true\n"
                "distribution:\n  rsync_target: origin:/demo\n"
                "minecraft_server:\n  ssh_host: old\n  stack_dir: /srv/old\n  service: old\n",
                encoding="utf-8",
            )

            patches = [
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for patch_item in patches:
                patch_item.start()

            try:
                self.assertIsNotNone(
                    packctl.ProjectLock
                )
                set_args = type(
                    "Args",
                    (),
                    {
                        "pack": "demo",
                        "rsync_target": "deploy@example:/srv/demo",
                        "ssh_host": "example",
                        "stack_dir": None,
                        "service": "minecraft",
                    },
                )()
                result = packctl.cmd_set_deployment(set_args)
                self.assertEqual(result, 0)

                out = StringIO()
                with redirect_stdout(out):
                    show_result = packctl.cmd_show_deployment(
                        type("Args", (), {"pack": "demo"})()
                    )
                text = out.getvalue()
                self.assertEqual(show_result, 0)
                self.assertIn("rsync_target: deploy@example:/srv/demo", text)
                self.assertIn("ssh_host: example", text)
                self.assertIn("service: minecraft", text)
                local = (pack_root / "pack.local.yaml").read_text(encoding="utf-8")
                self.assertIn("deploy@example:/srv/demo", local)
                self.assertIn("service: minecraft", local)

            finally:
                for patch_item in reversed(patches):
                    patch_item.stop()

    def test_set_deployment_requires_a_target_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            pack_root = packs / "demo"
            pack_root.mkdir(parents=True)
            (pack_root / "pack.yaml").write_text(
                "id: demo\ndisplay_name: Demo\nenabled: true\n"
                "distribution:\n  rsync_target: origin:/packs/demo\n"
                "minecraft_server:\n  ssh_host: old\n  stack_dir: /srv/old\n  service: old\n",
                encoding="utf-8",
            )

            patches = [
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for patch_item in patches:
                patch_item.start()

            try:
                args = type(
                    "Args",
                    (),
                    {
                        "pack": "demo",
                        "rsync_target": None,
                        "ssh_host": None,
                        "stack_dir": None,
                        "service": None,
                    },
                )()
                with self.assertRaisesRegex(
                    packctl.ConfigError,
                    "set-deployment requires at least one of --rsync-target",
                ):
                    packctl.cmd_set_deployment(args)
            finally:
                for patch_item in reversed(patches):
                    patch_item.stop()

    def test_set_deployment_normalizes_and_validates_rsync_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            pack_root = packs / "demo"
            pack_root.mkdir(parents=True)
            (pack_root / "pack.yaml").write_text(
                "id: demo\ndisplay_name: Demo\nenabled: true\n"
                "distribution:\n  rsync_target: origin:/packs/demo\n"
                "minecraft_server:\n  ssh_host: old\n  stack_dir: /srv/old\n  service: old\n",
                encoding="utf-8",
            )

            patches = [
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for patch_item in patches:
                patch_item.start()

            try:
                args = type(
                    "Args",
                    (),
                    {
                        "pack": "demo",
                        "rsync_target": " deploy@example:/srv/demo  ",
                        "ssh_host": None,
                        "stack_dir": None,
                        "service": None,
                    },
                )()
                self.assertEqual(packctl.cmd_set_deployment(args), 0)
                local = packctl.load_yaml(pack_root / "pack.local.yaml")
                self.assertEqual(
                    local["distribution"]["rsync_target"],
                    "deploy@example:/srv/demo",
                )

                with self.assertRaisesRegex(
                    packctl.ConfigError,
                    "rsync_target must be an explicit host:/absolute/path remote target",
                ):
                    packctl.cmd_set_deployment(
                        type(
                            "Args",
                            (),
                            {
                                "pack": "demo",
                                "rsync_target": "invalid_target",
                                "ssh_host": None,
                                "stack_dir": None,
                                "service": None,
                            },
                        )()
                    )
            finally:
                for patch_item in reversed(patches):
                    patch_item.stop()

    def test_set_deployment_validates_values_before_locking(self) -> None:
        args = type(
            "Args",
            (),
            {
                "pack": "demo",
                "rsync_target": None,
                "ssh_host": "  ",
                "stack_dir": None,
                "service": None,
            },
        )()

        with patch.object(packctl, "ProjectLock") as project_lock:
            with self.assertRaisesRegex(
                packctl.ConfigError,
                "SSH target must be a non-empty string",
            ):
                packctl.cmd_set_deployment(args)

        project_lock.assert_not_called()

    def test_deployment_target_validators_accept_safe_forms(self) -> None:
        for target in (
            "dockge",
            "dockge.example.internal",
            "admin@dockge",
            "192.0.2.10",
            "[2001:db8::10]",
            "admin@[2001:db8::10]",
        ):
            with self.subTest(target=target):
                self.assertEqual(packctl.validate_ssh_target(target), target)
        self.assertEqual(
            packctl.validate_remote_stack_dir("/srv/minecraft/demo"),
            "/srv/minecraft/demo",
        )
        self.assertEqual(packctl.validate_compose_service("minecraft-1"), "minecraft-1")

    def test_set_deployment_rejects_unsafe_values_before_locking(self) -> None:
        cases = (
            ("ssh_host", "-oProxyCommand=bad"),
            ("ssh_host", " dockge"),
            ("ssh_host", "host command"),
            ("ssh_host", "host..internal"),
            ("ssh_host", "admin@"),
            ("ssh_host", "host:/command"),
            ("ssh_host", "[2001:db8::10"),
            ("stack_dir", "/srv/../etc"),
            ("stack_dir", " /srv/demo"),
            ("stack_dir", "/"),
            ("stack_dir", "relative/path"),
            ("service", "--project-directory"),
            ("service", " minecraft"),
            ("service", "service name"),
        )
        for field, value in cases:
            values = {
                "pack": "demo",
                "rsync_target": None,
                "ssh_host": None,
                "stack_dir": None,
                "service": None,
            }
            values[field] = value
            with self.subTest(field=field, value=value), patch.object(
                packctl, "ProjectLock"
            ) as project_lock:
                with self.assertRaises(packctl.ConfigError):
                    packctl.cmd_set_deployment(type("Args", (), values)())
                project_lock.assert_not_called()

    def test_set_and_show_url_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            pack_root = packs / "demo"
            template_root = templates / "base"
            pack_root.mkdir(parents=True)
            template_root.mkdir(parents=True)
            (pack_root / "pack.yaml").write_text(
                "id: demo\ndisplay_name: Demo\nenabled: true\n"
                "distribution:\n  rsync_target: origin:/packs/demo\n"
                "minecraft_server:\n  ssh_host: old\n  stack_dir: /srv/old\n"
                "  service: old\n",
                encoding="utf-8",
            )
            (template_root / "template.yaml").write_text(
                "id: base\nenabled: true\ndisplay_name: Base\nminecraft: 1.21.1\n"
                "loader: neoforge\nreference_loader_version: 21.1.234\nmods: []\n",
                encoding="utf-8",
            )

            patches = [
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for patch_item in patches:
                patch_item.start()

            try:
                set_pack = type(
                    "Args",
                    (),
                    {
                        "kind": "pack",
                        "project": "demo",
                        "max_size": 1024,
                        "allow_private_networks": True,
                    },
                )()
                self.assertEqual(packctl.cmd_set_url_policy(set_pack), 0)
                set_template = type(
                    "Args",
                    (),
                    {
                        "kind": "template",
                        "project": "base",
                        "max_size": 2048,
                        "allow_private_networks": None,
                    },
                )()
                self.assertEqual(packctl.cmd_set_url_policy(set_template), 0)

                show_pack = StringIO()
                with redirect_stdout(show_pack):
                    self.assertEqual(
                        packctl.cmd_show_url_policy(
                            type("Args", (), {"kind": "pack", "project": "demo"})()
                        ),
                        0,
                    )
                output = show_pack.getvalue()
                self.assertIn("url_max_jar_size_bytes: 1024", output)
                self.assertIn("url_allow_private_networks: true", output)
                self.assertIn("url_max_jar_size_source: local", output)
                self.assertIn("url_allow_private_networks_source: local", output)

                show_template = StringIO()
                with redirect_stdout(show_template):
                    self.assertEqual(
                        packctl.cmd_show_url_policy(
                            type("Args", (), {"kind": "template", "project": "base"})()
                        ),
                        0,
                    )
                output = show_template.getvalue()
                self.assertIn("url_max_jar_size_bytes: 2048", output)

                local_text = (pack_root / "pack.local.yaml").read_text(encoding="utf-8")
                self.assertIn("url_allow_private_networks: true", local_text)
                self.assertIn("url_max_jar_size_bytes: 1024", local_text)
                self.assertEqual(
                    (
                        template_root / "template.local.yaml"
                    ).read_text(encoding="utf-8").strip(),
                    'url_max_jar_size_bytes: 2048',
                )
            finally:
                for patch_item in reversed(patches):
                    patch_item.stop()

    def test_show_url_policy_reports_effective_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            template = templates / "base"
            template.mkdir(parents=True)
            (template / "template.yaml").write_text(
                "id: base\ndisplay_name: Base\nenabled: true\nminecraft: 1.21.1\n"
                "loader: neoforge\nreference_loader_version: 21.1.234\nmods: []\n",
                encoding="utf-8",
            )
            output = StringIO()
            with patch.object(packctl, "ROOT", root), patch.object(
                packctl, "TEMPLATES", templates
            ), redirect_stdout(output):
                self.assertEqual(
                    packctl.cmd_show_url_policy(
                        type("Args", (), {"kind": "template", "project": "base"})()
                    ),
                    0,
                )

            self.assertEqual(
                output.getvalue().splitlines(),
                [
                    "url_max_jar_size_bytes: 268435456",
                    "url_max_jar_size_source: default",
                    "url_allow_private_networks: false",
                    "url_allow_private_networks_source: default",
                ],
            )

    def test_show_template_url_policy_rejects_legacy_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            template = templates / "base"
            (template / "source").mkdir(parents=True)
            (template / "template.yaml").write_text(
                "id: base\ndisplay_name: Base\nenabled: true\nminecraft: 1.21.1\n"
                "loader: neoforge\nreference_loader_version: 21.1.234\nmods: []\n",
                encoding="utf-8",
            )
            with patch.object(packctl, "ROOT", root), patch.object(
                packctl, "TEMPLATES", templates
            ):
                with self.assertRaisesRegex(packctl.ConfigError, "legacy template source"):
                    packctl.cmd_show_url_policy(
                        type("Args", (), {"kind": "template", "project": "base"})()
                    )

    def test_set_url_policy_requires_a_value_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            pack_root = packs / "demo"
            pack_root.mkdir(parents=True)
            (pack_root / "pack.yaml").write_text(
                "id: demo\n"
                "distribution:\n  rsync_target: origin:/packs/demo\n"
                "minecraft_server:\n  ssh_host: old\n  stack_dir: /srv/old\n  service: old\n",
                encoding="utf-8",
            )

            patches = [
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for patch_item in patches:
                patch_item.start()

            try:
                args = type(
                    "Args",
                    (),
                    {
                        "kind": "pack",
                        "project": "demo",
                        "max_size": None,
                        "allow_private_networks": None,
                    },
                )()
                with self.assertRaisesRegex(
                    packctl.ConfigError,
                    "set-url-policy requires --max-size",
                ):
                    packctl.cmd_set_url_policy(args)
            finally:
                for patch_item in reversed(patches):
                    patch_item.stop()

    def test_set_url_policy_rejects_non_positive_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            templates = root / "templates"
            pack_root = packs / "demo"
            pack_root.mkdir(parents=True)
            (pack_root / "pack.yaml").write_text("id: demo\n", encoding="utf-8")

            patches = [
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "PACKS", packs),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for patch_item in patches:
                patch_item.start()

            try:
                args = type(
                    "Args",
                    (),
                    {
                        "kind": "pack",
                        "project": "demo",
                        "max_size": 0,
                        "allow_private_networks": None,
                    },
                )()
                with self.assertRaisesRegex(
                    packctl.ConfigError,
                    "url_max_jar_size_bytes must be a positive integer",
                ):
                    packctl.cmd_set_url_policy(args)
            finally:
                for patch_item in reversed(patches):
                    patch_item.stop()

    def test_set_template_loader_version_updates_template_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            template_path = template_root / "template.yaml"
            template_path.write_text(
                "id: base\nenabled: true\ndisplay_name: Base\nminecraft: 1.21.1\n"
                "loader: neoforge\nreference_loader_version: 21.1.234\nmods: []\n",
                encoding="utf-8",
            )

            patches = [
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "TEMPLATES", templates),
            ]
            for patch_item in patches:
                patch_item.start()

            try:
                self.assertIs(
                    packctl.parser().parse_args(["show-template-loader-version", "base"]).func,
                    packctl.cmd_show_template_loader_version,
                )
                self.assertEqual(
                    packctl.cmd_set_template_loader_version(
                        type("Args", (), {"template": "base", "loader_version": "21.1.235"})()
                    ),
                    0,
                )
                self.assertEqual(
                    packctl.cmd_show_template_loader_version(
                        type("Args", (), {"template": "base"})()
                    ),
                    0,
                )
                out = StringIO()
                with redirect_stdout(out):
                    packctl.cmd_show_template_loader_version(
                        type("Args", (), {"template": "base"})()
                    )
                self.assertIn("21.1.235", out.getvalue())
                text = template_path.read_text(encoding="utf-8")
                self.assertIn("reference_loader_version: 21.1.235", text)
            finally:
                for patch_item in reversed(patches):
                    patch_item.stop()

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
        import huroshiki_core as core

        args = type(
            "Args",
            (),
            {"pack": "demo", "build": True, "allow_partial": False},
        )()
        events: list[str] = []

        def update(_: str, **__) -> core.UpdateRunReport:
            events.append("update")
            return core.UpdateRunReport((), (), (), False, False)

        def build(_: str) -> int:
            events.append("build")
            return 0

        with patch("huroshiki_core.update_all", side_effect=update), patch.object(
            packctl, "build_pack", side_effect=build
        ):
            self.assertEqual(packctl.cmd_update(args), 0)
        self.assertEqual(events, ["update", "build"])

        events.clear()
        failure = MagicMock(error_returncode=7)
        failed_report = core.UpdateRunReport((), (), (failure,), False, False)
        with patch(
            "huroshiki_core.update_all", return_value=failed_report
        ), patch.object(
            packctl, "build_pack"
        ) as build_mock:
            self.assertEqual(packctl.cmd_update(args), 7)
        build_mock.assert_not_called()

        partial_args = type(
            "Args",
            (),
            {"pack": "demo", "build": True, "allow_partial": True},
        )()
        partial_report = core.UpdateRunReport((), (), (failure,), True, True)
        stderr = StringIO()
        with patch(
            "huroshiki_core.update_all", return_value=partial_report
        ), patch.object(packctl, "build_pack") as build_mock, redirect_stderr(stderr):
            self.assertEqual(packctl.cmd_update(partial_args), 2)
        build_mock.assert_not_called()
        self.assertIn("Skipping build", stderr.getvalue())

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
            ["ssh", "--", "server", "cd /srv/demo && docker compose restart minecraft"]
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
                "--",
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
