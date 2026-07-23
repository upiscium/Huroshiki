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
                "/tmp/demo dist/", "deploy@example:/srv/demo/",
            ],
        )
        self.assertEqual(
            packctl.rsync_deploy_command(dist, target, dry_run=False),
            [
                "rsync", "-av", "--delete", "/tmp/demo dist/",
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

        command = run.call_args.args[0]
        self.assertIn("--dry-run", command)
        self.assertIn("--itemize-changes", command)
        self.assertEqual(preview.target, "host:/demo")
        self.assertEqual(preview.changes[0].category, "added")


class ConfirmedDeployTest(unittest.TestCase):
    def preview(self) -> core.ProjectDeployPreview:
        return core.ProjectDeployPreview(
            "pack:demo", "deploy", "host:/demo", "digest", (), ()
        )

    def test_target_change_aborts_without_execution(self) -> None:
        with patch.object(
            packctl, "distribution_target", return_value="other:/demo"
        ), patch.object(core.subprocess, "run") as run:
            with self.assertRaisesRegex(core.HuroshikiError, "changed after preview"):
                core.run_project_action("pack:demo", "deploy", self.preview())
        run.assert_not_called()

    def test_dist_change_aborts_without_execution(self) -> None:
        with patch.object(
            packctl, "distribution_target", return_value="host:/demo"
        ), patch.object(packctl, "distribution_root", return_value=Path("dist")), patch.object(
            packctl, "distribution_digest", return_value="changed"
        ), patch.object(core.subprocess, "run") as run:
            with self.assertRaisesRegex(core.HuroshikiError, "changed after preview"):
                core.run_project_action("pack:demo", "deploy", self.preview())
        run.assert_not_called()

    def test_confirmed_preview_executes_guarded_deploy(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        with patch.object(
            packctl, "distribution_target", return_value="host:/demo"
        ), patch.object(packctl, "distribution_root", return_value=Path("dist")), patch.object(
            packctl, "distribution_digest", return_value="digest"
        ), patch.object(core.subprocess, "run", return_value=completed) as run:
            result = core.run_project_action("pack:demo", "deploy", self.preview())

        self.assertEqual(result, 0)
        self.assertNotIn("--dry-run", run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][-1], "host:/demo/")


if __name__ == "__main__":
    unittest.main()
