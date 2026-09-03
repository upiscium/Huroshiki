from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
import re
import shlex
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import packctl


@dataclass(frozen=True)
class _PackctlMention:
    fragment: str
    fenced: bool
    before: str = ""
    after: str = ""


def _packctl_commands(fragment: str) -> tuple[str, ...]:
    """Return commands from shell-like packctl invocations."""
    try:
        lexer = shlex.shlex(fragment, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError as error:
        raise AssertionError(f"invalid shell-like guidance fragment: {fragment!r}: {error}") from error
    commands: list[str] = []
    for index, token in enumerate(tokens):
        if token != "packctl":
            continue
        skip = False
        for argument in tokens[index + 1 :]:
            if skip:
                skip = False
            elif argument == "--root":
                skip = True
            elif argument.startswith("--root=") or argument.startswith("-"):
                continue
            else:
                commands.append(argument)
                break
    return tuple(commands)


def classify_guidance_context(mention: _PackctlMention) -> str:
    """Classify a mention as descriptive only when its own context is explicit."""
    if mention.fenced:
        return "advertised"

    before = mention.before.rstrip()
    after = mention.after.lstrip()
    prohibited = re.search(
        r"\b(?:do not|don't|must not|never)\s+(?:run|use|invoke|execute)\s*$",
        before,
        re.IGNORECASE,
    )
    retired_suffix = re.match(
        r"(?:command\s+)?(?:"
        r"(?:is|was)\s+(?:retired|removed|no longer supported|not part of the public CLI)"
        r"|has been removed"
        r")\s*(?:[.!?]|$)",
        after,
        re.IGNORECASE,
    )
    if prohibited or retired_suffix:
        return "descriptive"
    return "advertised"


def extract_packctl_mentions(markdown: str) -> list[_PackctlMention]:
    """Extract inline and shell-like fenced packctl snippets from Markdown."""
    mentions: list[_PackctlMention] = []
    fenced_ranges: list[tuple[int, int]] = []
    fence_pattern = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)
    for match in fence_pattern.finditer(markdown):
        fenced_ranges.append(match.span())
        if match.group(1).strip().lower() not in {
            "",
            "bash",
            "console",
            "dash",
            "fish",
            "ksh",
            "sh",
            "shell",
            "shell-session",
            "zsh",
        }:
            continue
        for line in match.group(2).splitlines():
            if _packctl_commands(line):
                mentions.append(_PackctlMention(line, True))

    inline_pattern = re.compile(r"`([^`\n]+)`")
    for match in inline_pattern.finditer(markdown):
        if any(start <= match.start() < end for start, end in fenced_ranges):
            continue
        if not _packctl_commands(match.group(1)):
            continue
        line_start = markdown.rfind("\n", 0, match.start()) + 1
        line_end = markdown.find("\n", match.end())
        if line_end < 0:
            line_end = len(markdown)
        mentions.append(
            _PackctlMention(
                match.group(1),
                False,
                markdown[line_start : match.start()],
                markdown[match.end() : line_end],
            )
        )
    return mentions


def advertised_retired_commands(markdown: str) -> set[str]:
    """Return retired commands actually advertised by guidance."""
    advertised: set[str] = set()
    for mention in extract_packctl_mentions(markdown):
        if classify_guidance_context(mention) != "advertised":
            continue
        advertised.update(packctl._RETIRED_COMMANDS & set(_packctl_commands(mention.fragment)))
    return advertised


def parse_completion_commands(completion: str) -> set[str]:
    """Parse the command descriptions from the zsh completion command block."""
    commands_block = re.search(
        r"^\s*commands=\(\n(.*?)^\s*\)", completion, re.DOTALL | re.MULTILINE
    )
    if commands_block is None:
        raise AssertionError("completion command block is missing")
    commands: set[str] = set()
    for line in commands_block.group(1).splitlines():
        try:
            tokens = shlex.split(line)
        except ValueError as error:
            raise AssertionError(f"invalid completion entry: {line!r}: {error}") from error
        if tokens and ":" in tokens[0]:
            commands.add(tokens[0].split(":", 1)[0])
    return commands


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
            "loader-version",
            "version",
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
        self.assertIs(
            packctl.parser()
            .parse_args(
                ["version", "demo", "modrinth:A1b2C3d4", "--version-id", "E5f6G7h8"]
            )
            .func,
            packctl.cmd_version,
        )

    def test_template_migrate_nested_parser_and_selector_colons(self) -> None:
        args = packctl.parser().parse_args([
            "template", "migrate", "base", "--copy-to", "new", "--display-name", "New",
            "--minecraft", "1.21.1", "--loader", "fabric", "--loader-version", "0.16",
            "--remove", "2", "--replace", "3=modrinth:https://example.invalid/a:b",
        ])
        self.assertIs(args.func, packctl.cmd_template_migrate)
        self.assertEqual(args.source_template, "base")
        class Choice:
            def __init__(self, source_index, action, **kwargs):
                self.source_index = source_index
                self.action = action
                self.__dict__.update(kwargs)
        choices = packctl._template_migration_choices(
            args, type("Core", (), {"TemplateMigrationRootResolution": Choice})
        )
        self.assertEqual(choices[1].replacement_project_id, "https://example.invalid/a:b")

    def test_exact_version_preview_discards_and_apply_publishes(self) -> None:
        import huroshiki_core as core

        preview = core.ModVersionSelectionPreview(
            identity="modrinth:A1b2C3d4",
            relative_path=Path("mods/example.pw.toml"),
            name="Example",
            provider="modrinth",
            old_version="1.0",
            old_artifact_id="OldVer01",
            new_version="2.0",
            new_artifact_id="E5f6G7h8",
            changes=(core.UpdateChange(Path("mods/example.pw.toml"), b"old", b"new"),),
            added_dependencies=1,
            removed_dependencies=1,
            added_dependency_identities=("modrinth:Added001",),
            removed_dependency_identities=("modrinth:Remov001",),
        )
        for apply in (False, True):
            with self.subTest(apply=apply):
                transaction = MagicMock()
                transaction.active = True
                transaction.prepare_exact_mod_version.return_value = preview
                transaction.apply.side_effect = lambda: setattr(transaction, "active", False)
                transaction.discard.side_effect = lambda: setattr(transaction, "active", False)
                args = type(
                    "Args",
                    (),
                    {
                        "pack": "demo",
                        "identity": "modrinth:A1b2C3d4",
                        "artifact_id": None,
                        "file_id": None,
                        "version_id": "E5f6G7h8",
                        "apply": apply,
                    },
                )()
                output = StringIO()
                with patch.object(
                    core.PackTransaction, "create", return_value=transaction
                ), redirect_stdout(output):
                    self.assertEqual(packctl.cmd_version(args), 0)
                selection = transaction.prepare_exact_mod_version.call_args.args[0]
                self.assertEqual(selection.identity, ("modrinth", "A1b2C3d4"))
                self.assertEqual(selection.artifact_id, "E5f6G7h8")
                if apply:
                    transaction.apply.assert_called_once_with()
                else:
                    transaction.apply.assert_not_called()
                    transaction.discard.assert_called_once_with()
                text = output.getvalue()
                self.assertIn("Identity: modrinth:A1b2C3d4", text)
                self.assertIn("Artifact ID: OldVer01 -> E5f6G7h8", text)
                self.assertIn("Added dependencies: 1", text)
                self.assertIn("modrinth:Added001", text)
                self.assertIn("mods/example.pw.toml", text)

    def test_exact_version_rejects_invalid_identity_and_aliases(self) -> None:
        cases = (
            ("modrinth:sodium", None, None, "E5f6G7h8"),
            ("modrinth:A1b2C3d4", None, "123", None),
            ("curseforge:309927", None, None, "E5f6G7h8"),
            ("curseforge:0", "1", None, None),
        )
        for identity, artifact, file_id, version_id in cases:
            with self.subTest(identity=identity):
                args = type(
                    "Args",
                    (),
                    {
                        "pack": "demo",
                        "identity": identity,
                        "artifact_id": artifact,
                        "file_id": file_id,
                        "version_id": version_id,
                        "apply": False,
                    },
                )()
                with self.assertRaises(packctl.ConfigError):
                    packctl.cmd_version(args)

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

    def test_update_keyboard_interrupt_returns_130_without_build(self) -> None:
        args = type(
            "Args",
            (),
            {"pack": "demo", "build": True, "allow_partial": False},
        )()
        stderr = StringIO()
        with patch(
            "huroshiki_core.update_all", side_effect=KeyboardInterrupt
        ), patch.object(packctl, "build_pack") as build, redirect_stderr(stderr):
            self.assertEqual(packctl.cmd_update(args), 130)
        build.assert_not_called()
        self.assertIn("cancelled", stderr.getvalue())

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

    def test_publish_parser_and_main_retire_legacy_commands(self) -> None:
        parser = packctl.parser()
        publish = parser.parse_args(["publish", "demo", "--yes"])
        self.assertIs(publish.func, packctl.cmd_publish)
        self.assertTrue(publish.yes)
        self.assertFalse(publish.preview)
        help_text = parser.format_help()
        for command in ("build", "build-all", "deploy", "deploy-dry-run", "deploy-all", "restart"):
            self.assertIsNone(
                re.search(rf"(?:[{{,]){re.escape(command)}(?:[,}}])", help_text)
            )

        for command in ("build", "build-all", "deploy", "deploy-dry-run", "deploy-all", "restart"):
            with self.subTest(command=command), patch.object(sys, "argv", ["packctl", command, "demo"]), patch.object(
                packctl, "_build_pack", side_effect=AssertionError
            ), patch.object(packctl, "_deploy_pack", side_effect=AssertionError), patch.object(
                packctl, "cmd_restart", side_effect=AssertionError
            ), patch.object(packctl, "ProjectLock", side_effect=AssertionError):
                error = StringIO()
                with redirect_stderr(error):
                    self.assertEqual(packctl.main(), 2)
                self.assertEqual(
                    error.getvalue().strip(),
                    f"packctl {command} has been removed. Use packctl publish <pack>.",
                )

    def test_guidance_helpers_classify_inline_retired_command_examples(self) -> None:
        descriptive = (
            "`packctl build` is retired.",
            "Do not run `packctl deploy`; use `packctl publish` instead.",
            "The former `packctl restart` command has been removed.",
            "`packctl build` is not part of the public CLI.",
        )
        for example in descriptive:
            with self.subTest(example=example):
                mentions = extract_packctl_mentions(example)
                retired_mentions = [
                    mention
                    for mention in mentions
                    if packctl._RETIRED_COMMANDS
                    & set(_packctl_commands(mention.fragment))
                ]
                self.assertEqual(len(retired_mentions), 1)
                self.assertEqual(
                    classify_guidance_context(retired_mentions[0]), "descriptive"
                )
                self.assertEqual(advertised_retired_commands(example), set())

        advertised = (
            "Run `packctl build demo` before publishing.",
            "Use `packctl deploy demo` to update the server.",
            "`packctl restart demo` recreates the service.",
        )
        for example in advertised:
            with self.subTest(example=example):
                mentions = extract_packctl_mentions(example)
                self.assertEqual(len(mentions), 1)
                self.assertEqual(classify_guidance_context(mentions[0]), "advertised")
                self.assertEqual(
                    advertised_retired_commands(example),
                    set(_packctl_commands(mentions[0].fragment)),
                )

    def test_fenced_shell_examples_are_advertised_without_context_inference(self) -> None:
        guidance = "Negative prose: do not treat this as a command.\n\n```bash\npackctl build demo\n```"
        self.assertEqual(advertised_retired_commands(guidance), {"build"})

    def test_guidance_helpers_fail_closed_and_parse_shell_boundaries(self) -> None:
        contradictory = "The retired command `packctl build` is still supported."
        self.assertEqual(advertised_retired_commands(contradictory), {"build"})
        self.assertEqual(
            advertised_retired_commands("Run `packctl build;` before publishing."),
            {"build"},
        )
        self.assertEqual(
            advertised_retired_commands("```fish\npackctl deploy;\n```"),
            {"deploy"},
        )
        self.assertEqual(
            advertised_retired_commands("```bash\n# packctl restart demo\n```"),
            set(),
        )

    def test_retired_commands_are_absent_from_guidance(self) -> None:
        root = Path(__file__).resolve().parents[1]
        guidance = (root / "AGENTS.md").read_text(encoding="utf-8")
        documented_commands = advertised_retired_commands(guidance)

        self.assertFalse(
            packctl._RETIRED_COMMANDS & documented_commands,
            f"retired commands documented as packctl invocations: "
            f"{sorted(packctl._RETIRED_COMMANDS & documented_commands)}",
        )

    def test_completion_commands_are_disjoint_from_retired_commands(self) -> None:
        root = Path(__file__).resolve().parents[1]
        completion = (root / "shared/completions/zsh/_packctl").read_text(encoding="utf-8")
        completion_commands = parse_completion_commands(completion)
        self.assertFalse(packctl._RETIRED_COMMANDS & completion_commands)
        self.assertIn("serve", completion_commands)
        self.assertIn("publish", completion_commands)

    def _publish_plan(self):
        plan = MagicMock()
        plan.cancel_event = threading.Event()
        plan.deadline = 123.0
        plan.pack_id = "demo"
        return plan

    def test_publish_plans_then_executes_exact_plan_controls_and_no_legacy_pipeline(self) -> None:
        import huroshiki_core as orchestration
        plan = self._publish_plan()
        result = MagicMock(final_status="published", pack_id="demo", generation_id="g1")
        args = type("Args", (), {"pack": "demo", "yes": True, "preview": False})()
        with patch.object(orchestration, "plan_pack_publish", return_value=plan) as planner, patch.object(
            orchestration, "execute_pack_publish", return_value=result
        ) as execute, patch.object(packctl, "_build_pack") as build, patch.object(
            packctl, "_deploy_pack"
        ) as deploy, patch.object(packctl, "cmd_restart") as restart, patch.object(
            packctl, "ProjectLock"
        ) as lock, patch.object(packctl, "_print_pack_publish_preview"), patch.object(
            packctl, "_print_pack_publish_result"
        ):
            self.assertEqual(packctl.cmd_publish(args), 0)
        planner.assert_called_once_with("demo", cancel_event=unittest.mock.ANY)
        execute.assert_called_once_with(plan, cancel_event=plan.cancel_event, deadline=plan.deadline)
        build.assert_not_called(); deploy.assert_not_called(); restart.assert_not_called(); lock.assert_not_called()

    def test_publish_preview_and_decline_do_not_execute(self) -> None:
        import huroshiki_core as orchestration
        for preview, stdin_tty, answer, expected in ((True, False, "", 0), (False, True, "n", 0), (False, False, "", 2)):
            with self.subTest(preview=preview, stdin_tty=stdin_tty):
                plan = self._publish_plan()
                args = type("Args", (), {"pack": "demo", "yes": False, "preview": preview})()
                with patch.object(orchestration, "plan_pack_publish", return_value=plan), patch.object(
                    orchestration, "execute_pack_publish"
                ) as execute, patch.object(packctl, "_print_pack_publish_preview"), patch.object(
                    sys.stdin, "isatty", return_value=stdin_tty
                ), patch("builtins.input", return_value=answer):
                    self.assertEqual(packctl.cmd_publish(args), expected)
                execute.assert_not_called()

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
