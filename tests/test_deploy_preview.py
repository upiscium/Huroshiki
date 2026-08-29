from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


class RsyncPreviewTest(unittest.TestCase):
    def _assert_ssh_options(self, command: list[str]) -> None:
        if "-e" in command:
            wrapper = command[command.index("-e") + 1]
        else:
            wrappers = [part for part in command if part.startswith("-e=")]
            self.assertEqual(len(wrappers), 1)
            wrapper = wrappers[0][3:]
        self.assertIn("BatchMode=yes", wrapper)
        match = re.search(r"ConnectTimeout=(\d+)", wrapper)
        self.assertIsNotNone(match)
        assert match is not None
        timeout = int(match.group(1))
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 120)

    def test_command_construction_separates_dry_run_from_deploy(self) -> None:
        dist = Path("/tmp/demo dist")
        target = "deploy@example:/srv/demo"

        dry_run_command = packctl.rsync_deploy_command(dist, target, dry_run=True)
        deploy_command = packctl.rsync_deploy_command(dist, target, dry_run=False)

        self.assertEqual(dry_run_command[:3], ["rsync", "-av", "--delete"])
        self.assertEqual(deploy_command[:3], ["rsync", "-av", "--delete"])
        self.assertIn("--dry-run", dry_run_command)
        self.assertIn("--itemize-changes", dry_run_command)
        self.assertNotIn("--dry-run", deploy_command)
        self.assertNotIn("--itemize-changes", deploy_command)

        for command in (dry_run_command, deploy_command):
            self.assertIn("-e", command)
            self._assert_ssh_options(command)

        self.assertEqual(dry_run_command[-2:], ["/tmp/demo dist/", target.rstrip("/") + "/"])
        self.assertEqual(deploy_command[-2:], ["/tmp/demo dist/", target.rstrip("/") + "/"])
        self.assertIn("--", dry_run_command)
        self.assertIn("--", deploy_command)
        self.assertLess(dry_run_command.index("-e"), dry_run_command.index("--"))
        self.assertLess(deploy_command.index("-e"), deploy_command.index("--"))

    def test_parser_categorizes_added_updated_and_deleted_entries(self) -> None:
        output = """>f+++++++++ client/new.jar
>f.st...... server/server.properties
cd+++++++++ client/config/
*deleting   server/old.jar
sent 123 bytes  received 45 bytes
"""

        changes = packctl.parse_rsync_changes(output)

        self.assertEqual(
            [(item.category, item.path) for item in changes],
            [
                ("added", "client/new.jar"),
                ("updated", "server/server.properties"),
                ("added", "client/config/"),
                ("deleted", "server/old.jar"),
            ],
        )
        self.assertEqual(changes[0].raw, ">f+++++++++ client/new.jar")

    def test_preview_uses_only_rsync_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packs = Path(directory) / "packs"
            pack = packs / "demo"
            for side in ("client", "server"):
                side_root = pack / "dist" / side
                side_root.mkdir(parents=True)
                (side_root / "pack.toml").write_text("name = 'demo'\n")
            (pack / "pack.yaml").write_text(
                "id: demo\ndistribution:\n  rsync_target: user@host:/demo\n"
            )
            completed = packctl.BoundedProcessResult(
                0, ">f+++++++++ client/new.jar\n", "", False, False
            )
            with patch.object(packctl, "PACKS", packs), patch.object(
                packctl, "run_rsync_process", return_value=completed
            ) as run:
                preview = packctl.deploy_preview("demo")
                packctl.discard_deploy_snapshot(preview.snapshot)

        command = run.call_args.args[0]
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertIn("--dry-run", command)
        self.assertIn("--itemize-changes", command)
        self.assertIn("-e", command)
        self.assertIn("BatchMode=yes", command[command.index("-e") + 1])
        self.assertRegex(command[command.index("-e") + 1], r"ConnectTimeout=\d+")
        self.assertEqual(command[-1], "user@host:/demo/")
        self.assertEqual(preview.target, "user@host:/demo")
        self.assertEqual(preview.changes[0].category, "added")

    def test_preview_remains_bound_to_snapshot_if_live_dist_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packs = root / "packs"
            pack = packs / "demo"
            for side in ("client", "server"):
                side_root = pack / "dist" / side
                side_root.mkdir(parents=True)
                (side_root / "pack.toml").write_text("previewed", encoding="utf-8")
            (pack / "pack.yaml").write_text(
                "id: demo\ndistribution:\n  rsync_target: host:/demo\n",
                encoding="utf-8",
            )

            def mutate_live(command, **kwargs):
                (pack / "dist" / "client" / "pack.toml").write_text(
                    "changed", encoding="utf-8"
                )
                return packctl.BoundedProcessResult(0, "", "", False, False)

            with patch.object(packctl, "PACKS", packs), patch.object(
                packctl, "run_rsync_process", side_effect=mutate_live
            ):
                preview = packctl.deploy_preview("demo")
                try:
                    self.assertEqual(
                        (preview.snapshot / "client" / "pack.toml").read_text(
                            encoding="utf-8"
                        ),
                        "previewed",
                    )
                finally:
                    packctl.discard_deploy_snapshot(preview.snapshot)

    def test_preview_rejects_output_over_supported_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packs = Path(directory) / "packs"
            pack = packs / "demo"
            for side in ("client", "server"):
                side_root = pack / "dist" / side
                side_root.mkdir(parents=True)
                (side_root / "pack.toml").write_text("name = 'demo'\n")
            (pack / "pack.yaml").write_text(
                "id: demo\ndistribution:\n  rsync_target: host:/demo\n"
            )
            result = packctl.BoundedProcessResult(
                0, "x" * 33, "", False, False
            )
            with patch.object(packctl, "PACKS", packs), patch.object(
                packctl, "RSYNC_PREVIEW_OUTPUT_MAX_BYTES", 32
            ), patch.object(packctl, "run_rsync_process", return_value=result):
                with self.assertRaisesRegex(
                    packctl.ConfigError,
                    "Rsync preview output exceeded the supported limit",
                ):
                    packctl.deploy_preview("demo")

            snapshot_root = Path(directory) / ".huroshiki" / "deploy-snapshots"
            self.assertFalse(tuple(snapshot_root.iterdir()))

class RetiredCoreDeploymentTest(unittest.TestCase):
    def test_retired_pack_action_apis_fail_without_legacy_side_effects(self) -> None:
        for action in ("build", "deploy", "publish", "restart"):
            with self.subTest(action=action), patch.object(
                packctl, "_build_pack"
            ) as build, patch.object(packctl, "_deploy_pack") as deploy, patch.object(
                packctl, "run"
            ) as run:
                with self.assertRaisesRegex(core.HuroshikiError, "retired"):
                    core.run_project_action("pack:demo", action)
                with self.assertRaisesRegex(core.HuroshikiError, "retired"):
                    core.project_action_confirmation("pack:demo", action)
                with self.assertRaisesRegex(core.HuroshikiError, "retired"):
                    core.prepare_deploy_preview("pack:demo", action)
                build.assert_not_called()
                deploy.assert_not_called()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
