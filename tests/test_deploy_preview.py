from __future__ import annotations

from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
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

    def test_core_preview_threads_cancel_and_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packs = Path(directory) / "packs"
            (packs / "demo").mkdir(parents=True)
            (packs / "demo" / "pack.yaml").write_text("id: demo\n")
            cancel = threading.Event()
            deadline = time.monotonic() + 10
            preview = packctl.DeployPreview(
                "host:/demo", "digest", (), (), Path(directory) / "snapshot"
            )
            with patch.object(packctl, "PACKS", packs), patch.object(
                packctl, "_build_pack", return_value=0
            ) as build, patch.object(
                packctl, "_deploy_preview", return_value=preview
            ) as deploy:
                result = core.prepare_deploy_preview(
                    "pack:demo",
                    "deploy",
                    cancel_event=cancel,
                    deadline=deadline,
                )

        self.assertEqual(result.target, "host:/demo")
        self.assertIs(build.call_args.kwargs["cancel_event"], cancel)
        self.assertEqual(build.call_args.kwargs["deadline"], deadline)
        self.assertIs(deploy.call_args.kwargs["cancel_event"], cancel)
        self.assertEqual(deploy.call_args.kwargs["deadline"], deadline)


class ConfirmedDeployTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.snapshot = self.root / ".huroshiki" / "deploy-snapshots" / "snapshot"
        self.snapshot.mkdir(parents=True)
        (self.snapshot / "payload").write_text("previewed", encoding="utf-8")
        self.digest = packctl.distribution_digest(self.snapshot)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def preview(self) -> core.ProjectDeployPreview:
        return core.ProjectDeployPreview(
            "pack:demo", "deploy", "host:/demo", self.digest, (), (), None,
            self.snapshot,
        )

    def test_target_change_aborts_without_execution(self) -> None:
        with patch.object(
            packctl, "distribution_target", return_value="other:/demo"
        ), patch.object(packctl, "PACKS", self.packs), patch.object(
            packctl, "run_rsync_process"
        ) as run:
            with self.assertRaisesRegex(core.HuroshikiError, "changed after preview"):
                core.run_project_action("pack:demo", "deploy", self.preview())
        run.assert_not_called()

    def test_dist_change_aborts_without_execution(self) -> None:
        with patch.object(
            packctl, "distribution_target", return_value="host:/demo"
        ), patch.object(packctl, "PACKS", self.packs), patch.object(
            packctl, "distribution_digest", return_value="changed"
        ), patch.object(packctl, "run_rsync_process") as run:
            with self.assertRaisesRegex(core.HuroshikiError, "changed after preview"):
                core.run_project_action("pack:demo", "deploy", self.preview())
        run.assert_not_called()

    def test_confirmed_preview_executes_guarded_deploy(self) -> None:
        completed = packctl.BoundedProcessResult(0, "", "", False, False)
        with patch.object(
            packctl, "distribution_target", return_value="host:/demo"
        ), patch.object(packctl, "PACKS", self.packs), patch.object(
            packctl, "run_rsync_process", return_value=completed
        ) as run:
            result = core.run_project_action("pack:demo", "deploy", self.preview())

        self.assertEqual(result, 0)
        self.assertNotIn("--dry-run", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs.get("shell", False), False)
        self.assertIn("-e", run.call_args.args[0])
        self.assertIn("BatchMode=yes", run.call_args.args[0][run.call_args.args[0].index("-e") + 1])
        self.assertRegex(run.call_args.args[0][run.call_args.args[0].index("-e") + 1], r"ConnectTimeout=\d+")
        self.assertEqual(run.call_args.args[0][-1], "host:/demo/")
        self.assertFalse(self.snapshot.exists())

    def test_remote_target_validation_accepts_supported_forms(self) -> None:
        for target in (
            "alias:/srv/demo",
            "user@example.com:/srv/demo",
            "[2001:db8::1]:/srv/demo",
            "user@[2001:db8::1]:/srv/demo",
        ):
            self.assertEqual(packctl.validate_rsync_target(target), target)

    def test_remote_target_validation_rejects_local_and_unsafe_forms(self) -> None:
        for target in (
            "/tmp/demo",
            "relative/path",
            "-e sh:/tmp",
            "host:relative",
            "host:/tmp demo",
            "host:/tmp\n--delete",
            "[not-ipv6]:/tmp",
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                packctl.validate_rsync_target(target)

    def test_deploy_reads_confirmed_snapshot_when_live_dist_mutates(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        live = self.root / "live"
        live.mkdir()
        (live / "payload").write_text("changed", encoding="utf-8")
        seen: list[str] = []

        def fake_run(command, **kwargs):
            seen.append((Path(command[-2]) / "payload").read_text(encoding="utf-8"))
            return completed

        with patch.object(packctl, "PACKS", self.packs), patch.object(
            packctl, "distribution_target", return_value="host:/demo"
        ), patch.object(packctl, "run_rsync_process", side_effect=fake_run):
            core.run_project_action("pack:demo", "deploy", self.preview())

        self.assertEqual(seen, ["previewed"])

    def test_core_deploy_threads_cancel_and_deadline(self) -> None:
        cancel = threading.Event()
        deadline = time.monotonic() + 10
        with patch.object(
            packctl, "distribution_target", return_value="host:/demo"
        ), patch.object(packctl, "PACKS", self.packs), patch.object(
            packctl, "_deploy_pack", return_value=0
        ) as deploy:
            result = core.run_project_action(
                "pack:demo",
                "deploy",
                self.preview(),
                cancel_event=cancel,
                deadline=deadline,
            )

        self.assertEqual(result, 0)
        self.assertIs(deploy.call_args.kwargs["cancel_event"], cancel)
        self.assertEqual(deploy.call_args.kwargs["deadline"], deadline)

    def test_publish_revalidates_restart_target_after_rsync(self) -> None:
        preview = core.ProjectDeployPreview(
            "pack:demo",
            "publish",
            "host:/demo",
            self.digest,
            (),
            (),
            ("server", "/srv/demo", "minecraft"),
            self.snapshot,
        )
        commands: list[list[str]] = []

        def fake_rsync(command, **kwargs):
            commands.append(command)
            return packctl.BoundedProcessResult(0, "", "", False, False)

        def fake_run(command, **kwargs):
            commands.append(command)
            return None

        with patch.object(packctl, "PACKS", self.packs), patch.object(
            packctl, "distribution_target", return_value="host:/demo"
        ), patch.object(
            packctl,
            "minecraft_server_target",
            side_effect=[
                ("server", "/srv/demo", "minecraft"),
                ("changed", "/srv/demo", "minecraft"),
            ],
        ), patch.object(
            packctl, "run_rsync_process", side_effect=fake_rsync
        ), patch.object(packctl, "run", side_effect=fake_run):
            with self.assertRaisesRegex(core.HuroshikiError, "changed during deployment"):
                core.run_project_action("pack:demo", "publish", preview)

        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], "rsync")

    def test_restart_reads_configuration_once_and_executes_confirmed_tuple(self) -> None:
        target = ("server", "/srv/demo", "minecraft")
        confirmation = core._restart_confirmation("demo", "restart", target)
        with patch.object(packctl, "PACKS", self.packs), patch.object(
            packctl, "minecraft_server_target", return_value=target
        ) as read_target, patch.object(packctl, "run") as run:
            result = core.run_project_action("pack:demo", "restart", confirmation)

        self.assertEqual(result, 0)
        read_target.assert_called_once_with("demo")
        run.assert_called_once_with(
            ["ssh", "--", "server", "cd /srv/demo && docker compose restart minecraft"]
        )


if __name__ == "__main__":
    unittest.main()
