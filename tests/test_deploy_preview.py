from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


class RsyncPreviewTest(unittest.TestCase):
    def test_command_construction_separates_dry_run_from_deploy(self) -> None:
        dist = Path("/tmp/demo dist")
        target = "deploy@example:/srv/demo"

        self.assertEqual(
            packctl.rsync_deploy_command(dist, target, dry_run=True),
            [
                "rsync", "-av", "--delete", "--dry-run", "--itemize-changes",
                "--", "/tmp/demo dist/", "deploy@example:/srv/demo/",
            ],
        )
        self.assertEqual(
            packctl.rsync_deploy_command(dist, target, dry_run=False),
            [
                "rsync", "-av", "--delete", "--", "/tmp/demo dist/",
                "deploy@example:/srv/demo/",
            ],
        )

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
                "id: demo\ndistribution:\n  rsync_target: host:/demo\n"
            )
            completed = subprocess.CompletedProcess(
                [], 0, stdout=">f+++++++++ client/new.jar\n", stderr=""
            )
            with patch.object(packctl, "PACKS", packs), patch.object(
                packctl.subprocess, "run", return_value=completed
            ) as run:
                preview = packctl.deploy_preview("demo")
                packctl.discard_deploy_snapshot(preview.snapshot)

        command = run.call_args.args[0]
        self.assertIn("--dry-run", command)
        self.assertIn("--itemize-changes", command)
        self.assertEqual(preview.target, "host:/demo")
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
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(packctl, "PACKS", packs), patch.object(
                packctl.subprocess, "run", side_effect=mutate_live
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
        ), patch.object(packctl, "PACKS", self.packs), patch.object(core.subprocess, "run") as run:
            with self.assertRaisesRegex(core.HuroshikiError, "changed after preview"):
                core.run_project_action("pack:demo", "deploy", self.preview())
        run.assert_not_called()

    def test_dist_change_aborts_without_execution(self) -> None:
        with patch.object(
            packctl, "distribution_target", return_value="host:/demo"
        ), patch.object(packctl, "PACKS", self.packs), patch.object(
            packctl, "distribution_digest", return_value="changed"
        ), patch.object(core.subprocess, "run") as run:
            with self.assertRaisesRegex(core.HuroshikiError, "changed after preview"):
                core.run_project_action("pack:demo", "deploy", self.preview())
        run.assert_not_called()

    def test_confirmed_preview_executes_guarded_deploy(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        with patch.object(
            packctl, "distribution_target", return_value="host:/demo"
        ), patch.object(packctl, "PACKS", self.packs), patch.object(
            core.subprocess, "run", return_value=completed
        ) as run:
            result = core.run_project_action("pack:demo", "deploy", self.preview())

        self.assertEqual(result, 0)
        self.assertNotIn("--dry-run", run.call_args.args[0])
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
        ), patch.object(core.subprocess, "run", side_effect=fake_run):
            core.run_project_action("pack:demo", "deploy", self.preview())

        self.assertEqual(seen, ["previewed"])

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

        def fake_run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(packctl, "PACKS", self.packs), patch.object(
            packctl, "distribution_target", return_value="host:/demo"
        ), patch.object(
            packctl,
            "minecraft_server_target",
            side_effect=[
                ("server", "/srv/demo", "minecraft"),
                ("changed", "/srv/demo", "minecraft"),
            ],
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
