from __future__ import annotations

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.test_pack_publish_manifest import PackPublishManifestTest

import packctl
import pack_publish
import publish_target
import publish_transfer as transfer
from process_runner import BoundedProcessResult


class PublishTransferTest(PackPublishManifestTest):
    def _target(self, *, remote_path: str | None = None) -> publish_target.PublishRemoteTarget:
        root = remote_path or str(self.root / "remote")
        return publish_target.publish_remote_target_from_legacy_settings(
            rsync_target=f"publisher@publish.example:{self.root / 'configured'}",
            ssh_host="minecraft@game.example",
            stack_dir="/srv/minecraft",
            service="minecraft",
            remote_path=root,
        )

    def _settings(self, target: publish_target.PublishRemoteTarget) -> packctl.DeploymentSettings:
        publication = target.publication_endpoint
        publication_host = f"[{publication.host}]" if ":" in publication.host else publication.host
        publication_prefix = f"{publication.user}@" if publication.user else ""
        restart = target.restart.endpoint
        restart_host = f"[{restart.host}]" if ":" in restart.host else restart.host
        restart_prefix = f"{restart.user}@" if restart.user else ""
        return packctl.DeploymentSettings(
            f"{publication_prefix}{publication_host}:{self.root / 'configured'}",
            f"{restart_prefix}{restart_host}",
            target.restart.stack_dir.as_posix(),
            target.restart.service,
        )

    def _manifest_and_target(self):
        manifest = self.plan_manifest = pack_publish.plan_pack_publish_manifest("demo")
        return manifest, self._target()

    def _fake_runner(self, commands: list[list[str]]):
        def run(command, *, stdin_file, **kwargs):
            commands.append(list(command))
            result = subprocess.run(
                [sys.executable, "-c", transfer._REMOTE_HELPER_SCRIPT],
                stdin=stdin_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=kwargs["cwd"],
                check=False,
            )
            return BoundedProcessResult(
                result.returncode,
                result.stdout.decode("utf-8", errors="replace"),
                result.stderr.decode("utf-8", errors="replace"),
                False,
                False,
            )

        return run

    def test_prepare_materializes_exact_manifest_files_and_modes(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        try:
            self.assertEqual(plan.state, "ready")
            self.assertEqual(plan.generation_id, transfer.compute_publish_generation_id(manifest, target))
            self.assertTrue(plan._workspace.is_dir())
            self.assertEqual(plan._workspace.stat().st_mode & 0o777, 0o700)
            for entry in manifest.files:
                path = plan._payload_root / Path(*entry.relative_path.parts)
                self.assertTrue(path.is_file())
                self.assertEqual(path.read_bytes(), entry.contents if entry.source_kind == "generated" else path.read_bytes())
                metadata = path.stat()
                self.assertEqual(metadata.st_size, entry.size)
                self.assertEqual(metadata.st_mode & 0o777, entry.mode)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry.sha256)
        finally:
            transfer.discard_publish_transfer_plan(plan)
        self.assertEqual(plan.state, "discarded")

    def test_generation_id_is_deterministic_and_target_bound(self) -> None:
        manifest, target = self._manifest_and_target()
        other = self._target(remote_path=str(self.root / "other-remote"))
        first = transfer.compute_publish_generation_id(manifest, target)
        self.assertEqual(first, transfer.compute_publish_generation_id(manifest, target))
        self.assertNotEqual(first, transfer.compute_publish_generation_id(manifest, other))

    def test_stale_manifest_and_wrong_plan_types_fail_closed(self) -> None:
        manifest, target = self._manifest_and_target()
        with self.assertRaises(transfer.PublishTransferPlanningError):
            transfer.prepare_publish_transfer("demo", object(), target)  # type: ignore[arg-type]
        with self.assertRaises(transfer.PublishTransferPlanningError):
            transfer.prepare_publish_transfer("other", manifest, target)

        (self.pack / "content" / "server" / "server.cfg").write_bytes(b"changed")
        with self.assertRaisesRegex(transfer.PublishTransferPlanningError, "stale"):
            transfer.prepare_publish_transfer("demo", manifest, target)

    def test_execute_uses_publication_endpoint_only_and_keeps_current_untouched(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        commands: list[list[str]] = []
        current = Path(target.publication_root) / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("untouched", encoding="utf-8")
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner(commands)
        ):
            result = transfer.execute_publish_transfer(plan)
        self.assertFalse(result.reused)
        self.assertTrue((Path(target.publication_root) / "generations" / result.generation_id).is_dir())
        self.assertEqual(current.read_text(encoding="utf-8"), "untouched")
        command_text = " ".join(commands[0])
        self.assertIn("publisher@publish.example", command_text)
        self.assertNotIn(target.publication_root.as_posix(), command_text)
        self.assertNotIn("minecraft@game.example", command_text)
        self.assertIn("BatchMode=yes", command_text)
        self.assertIn("ConnectTimeout=10", command_text)
        self.assertNotIn("StrictHostKeyChecking=no", command_text)
        transfer.discard_publish_transfer_plan(plan)

    def test_existing_generation_is_reused_and_mismatch_is_rejected(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        commands: list[list[str]] = []
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner(commands)
        ):
            first = transfer.execute_publish_transfer(plan)
        transfer.discard_publish_transfer_plan(plan)
        plan2 = transfer.prepare_publish_transfer("demo", manifest, target)
        commands.clear()
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner(commands)
        ):
            reused = transfer.execute_publish_transfer(plan2)
        self.assertTrue(reused.reused)
        generation_file = Path(target.publication_root) / "generations" / first.generation_id / "pack.toml"
        generation_file.write_bytes(b"tampered")
        transfer.discard_publish_transfer_plan(plan2)
        plan3 = transfer.prepare_publish_transfer("demo", manifest, target)
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner([])
        ):
            with self.assertRaises(transfer.PublishTransferExecutionError):
                transfer.execute_publish_transfer(plan3)
        transfer.discard_publish_transfer_plan(plan3)

    def test_remote_helper_handles_protocol_and_hostile_root_without_shell(self) -> None:
        target = self._target(remote_path=str(self.root / "remote;$(touch_p)"))
        manifest, _ = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        commands: list[list[str]] = []
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner(commands)
        ):
            result = transfer.execute_publish_transfer(plan)
        self.assertTrue(result.generation_path.is_absolute())
        self.assertFalse((self.root / "touch_p").exists())
        self.assertNotIn(target.publication_root.as_posix(), " ".join(commands[0]))
        transfer.discard_publish_transfer_plan(plan)

    def test_cancelled_execution_does_not_spawn_remote_process(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        event = threading.Event()
        event.set()
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process"
        ) as run:
            with self.assertRaises(transfer.PublishTransferError):
                transfer.execute_publish_transfer(plan, cancel_event=event)
        run.assert_not_called()
        transfer.discard_publish_transfer_plan(plan)

    def test_target_stale_and_double_execute_are_rejected_before_reuse(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        changed_target = publish_target.publish_remote_target_from_legacy_settings(
            rsync_target=f"other.publish.example:{self.root / 'configured'}",
            ssh_host="minecraft@game.example",
            stack_dir="/srv/minecraft",
            service="minecraft",
            remote_path=str(self.root / "remote"),
        )
        commands: list[list[str]] = []
        with patch.object(packctl, "deployment_settings", return_value=self._settings(changed_target)), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner(commands)
        ):
            with self.assertRaisesRegex(transfer.PublishTransferExecutionError, "target changed"):
                transfer.execute_publish_transfer(plan)
        self.assertEqual(commands, [])
        transfer.discard_publish_transfer_plan(plan)
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner(commands)
        ):
            result = transfer.execute_publish_transfer(plan)
        self.assertFalse(result.reused)
        with self.assertRaisesRegex(transfer.PublishTransferExecutionError, "not ready"):
            transfer.execute_publish_transfer(plan)
        transfer.discard_publish_transfer_plan(plan)

    def test_source_changed_after_ready_is_rejected_before_remote_process(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        (self.pack / "content" / "server" / "server.cfg").write_bytes(b"changed after ready")
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process"
        ) as run:
            with self.assertRaisesRegex(transfer.PublishTransferExecutionError, "changed after"):
                transfer.execute_publish_transfer(plan)
        run.assert_not_called()
        transfer.discard_publish_transfer_plan(plan)

    def test_malformed_transfer_response_runs_status_recovery(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        committed = json.dumps({
            "ok": True,
            "status": "committed",
            "operation_id": plan.operation_id,
            "manifest_digest": plan.manifest_digest,
            "target_config_digest": plan.target_config_digest,
            "generation_id": plan.generation_id,
        })
        responses = iter([
            BoundedProcessResult(0, "unexpected banner\nsecond line\n", "", False, False),
            BoundedProcessResult(0, committed + "\n", "", False, False),
        ])
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=lambda *args, **kwargs: next(responses)
        ) as run:
            result = transfer.execute_publish_transfer(plan)
        self.assertFalse(result.reused)
        self.assertEqual(run.call_count, 2)
        transfer.discard_publish_transfer_plan(plan)


class PublishTransferProtocolTest(unittest.TestCase):
    def _request(self, root: str, header: dict[str, object], frames: tuple[bytes, ...]) -> tuple[int, dict[str, object]]:
        encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload = BytesIO()
        payload.write(transfer._FRAME_HEADER)
        payload.write(struct.pack("!I", 1))
        payload.write(struct.pack("!I", len(encoded)))
        payload.write(encoded)
        for frame in frames:
            payload.write(struct.pack("!Q", len(frame)))
            payload.write(frame)
        result = subprocess.run(
            [sys.executable, "-c", transfer._REMOTE_HELPER_SCRIPT],
            input=payload.getvalue(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        response = json.loads(result.stdout.decode("utf-8").splitlines()[-1])
        return result.returncode, response

    def _header(self, root: str, *, mode: int = 0o644, digest: str | None = None) -> dict[str, object]:
        data = b"payload"
        return {
            "schema": "huroshiki-publish-transfer-v1",
            "version": 1,
            "request": "transfer",
            "operation_id": "a" * 32,
            "manifest_digest": "b" * 64,
            "source_snapshot_digest": "c" * 64,
            "target_config_digest": "d" * 64,
            "generation_id": "v1-" + "e" * 64,
            "publication_root": root,
            "files": [{
                "path": "nested/file.txt",
                "size": len(data),
                "sha256": digest or hashlib.sha256(data).hexdigest(),
                "mode": mode,
            }],
            "total_bytes": len(data),
        }

    def test_generation_id_input_is_not_remote_command_data(self) -> None:
        target = publish_target.publish_remote_target_from_legacy_settings(
            rsync_target="publisher@publish.example:/tmp/target",
            ssh_host="minecraft@game.example",
            stack_dir="/srv/minecraft",
            service="minecraft",
            remote_path="/tmp/hostile;$(touch_p)",
        )
        command = transfer._ssh_command(target)
        self.assertNotIn(target.publication_root.as_posix(), command)
        self.assertIn("python3", command[-1])
        self.assertIn("-c", command[-1])

    def test_helper_rejects_size_digest_mode_extra_and_symlink_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            header = self._header(directory)
            code, response = self._request(directory, header, (b"bad",))
            self.assertNotEqual(code, 0)
            self.assertEqual(response["status"], "integrity_failure")
            stage = Path(directory) / "generations" / (".huroshiki-stage-" + "a" * 32)
            self.assertFalse(stage.exists())

            code, response = self._request(directory, header, (b"payload",))
            self.assertEqual(code, 0)
            self.assertEqual(response["status"], "committed")
            final = Path(directory) / "generations" / header["generation_id"]
            (final / "extra").write_bytes(b"unexpected")
            code, response = self._request(directory, header, (b"payload",))
            self.assertNotEqual(code, 0)
            self.assertEqual(response["status"], "integrity_failure")

            (final / "extra").unlink()
            mode_header = self._header(directory, mode=0o600)
            code, response = self._request(directory, mode_header, (b"payload",))
            self.assertNotEqual(code, 0)
            self.assertEqual(response["status"], "integrity_failure")

        with tempfile.TemporaryDirectory() as directory:
            generations = Path(directory) / "generations"
            generations.mkdir()
            os.symlink("missing", generations / ("v1-" + "e" * 64))
            code, response = self._request(directory, self._header(directory), (b"payload",))
            self.assertNotEqual(code, 0)
            self.assertEqual(response["status"], "integrity_failure")


if __name__ == "__main__":
    unittest.main()
