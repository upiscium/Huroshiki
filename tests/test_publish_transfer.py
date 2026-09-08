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
import time
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

    def _client_target(self) -> publish_target.PublishClientTarget:
        return publish_target.publish_client_target_from_remote_target(self._target())

    def test_client_target_uses_client_namespace_and_independent_generation_id(self) -> None:
        server_manifest = pack_publish.plan_pack_publish_manifest("demo", target_side="server")
        client_manifest = pack_publish.plan_pack_publish_manifest("demo", target_side="client")
        target = self._target()
        client = self._client_target()
        self.assertEqual(client.publication_root, target.publication_root / "client")
        self.assertNotEqual(
            transfer.compute_publish_generation_id(server_manifest, target),
            transfer.compute_publish_generation_id(client_manifest, client),
        )
        with self.assertRaises(transfer.PublishTransferPlanningError):
            transfer.prepare_publish_transfer("demo", server_manifest, client)
        with self.assertRaises(transfer.PublishTransferPlanningError):
            transfer.prepare_publish_transfer("demo", client_manifest, target)

    def test_client_transfer_commits_and_reuses_in_client_namespace(self) -> None:
        manifest = pack_publish.plan_pack_publish_manifest("demo", target_side="client")
        target = self._client_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        try:
            with patch.object(packctl, "deployment_settings", return_value=self._settings(self._target())), patch.object(
                transfer, "run_bounded_process", side_effect=self._fake_runner([])
            ):
                first = transfer.execute_publish_transfer(plan)
            self.assertFalse(first.reused)
            self.assertTrue(Path(first.generation_path).is_dir())
        finally:
            transfer.discard_publish_transfer_plan(plan)
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        try:
            with patch.object(packctl, "deployment_settings", return_value=self._settings(self._target())), patch.object(
                transfer, "run_bounded_process", side_effect=self._fake_runner([])
            ):
                second = transfer.execute_publish_transfer(plan)
            self.assertTrue(second.reused)
            self.assertEqual(second.target_side, "client")
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_client_reuse_rejects_corrupted_exact_tree(self) -> None:
        manifest = pack_publish.plan_pack_publish_manifest("demo", target_side="client")
        target = self._client_target()
        settings = self._settings(self._target())
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        try:
            with patch.object(
                packctl, "deployment_settings", return_value=settings
            ), patch.object(
                transfer, "run_bounded_process", side_effect=self._fake_runner([])
            ):
                staged = transfer.execute_publish_transfer(plan)
        finally:
            transfer.discard_publish_transfer_plan(plan)

        generation = Path(staged.generation_path)
        (generation / "unexpected.txt").write_text("not in the manifest\n")
        retry = transfer.prepare_publish_transfer("demo", manifest, target)
        with patch.object(
            packctl, "deployment_settings", return_value=settings
        ), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner([])
        ):
            try:
                with self.assertRaises(transfer.PublishTransferExecutionError):
                    transfer.execute_publish_transfer(retry)
            finally:
                transfer.discard_publish_transfer_plan(retry)

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

    def _nested_content_manifest_and_target(self):
        files = {
            "content/common/kubejs/server_scripts/common.js": b"common script\n",
            "content/server/kubejs/server_scripts/server.js": b"server script\n",
            "content/common/config/example.toml": b"[example]\nvalue = true\n",
        }
        for relative, contents in files.items():
            path = self.pack / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        return self._manifest_and_target()

    def _publish_nested_generation(self):
        manifest, target = self._nested_content_manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner([])
        ):
            result = transfer.execute_publish_transfer(plan)
        self.assertFalse(result.reused)
        transfer.discard_publish_transfer_plan(plan)
        return manifest, target, result

    def _assert_nested_reuse_rejected(self, mutate) -> None:
        manifest, target, first = self._publish_nested_generation()
        generation = Path(target.publication_root) / "generations" / first.generation_id
        mutate(generation)
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner([])
        ):
            try:
                with self.assertRaises(transfer.PublishTransferExecutionError):
                    transfer.execute_publish_transfer(plan)
            finally:
                transfer.discard_publish_transfer_plan(plan)

    def test_nested_content_manifest_workspace_header_spool_and_generation_are_identical(self) -> None:
        manifest, target = self._nested_content_manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        captured: dict[str, object] = {}

        def run(command, *, stdin_file, **kwargs):
            encoded = stdin_file.read()
            magic_end = len(transfer._FRAME_HEADER)
            self.assertEqual(encoded[:magic_end], transfer._FRAME_HEADER)
            version = struct.unpack("!I", encoded[magic_end:magic_end + 4])[0]
            self.assertEqual(version, 1)
            header_size = struct.unpack("!I", encoded[magic_end + 4:magic_end + 8])[0]
            header_start = magic_end + 8
            header = json.loads(encoded[header_start:header_start + header_size])
            captured["header"] = header
            offset = header_start + header_size
            frames: dict[str, bytes] = {}
            for item in header["files"]:
                size = struct.unpack("!Q", encoded[offset:offset + 8])[0]
                offset += 8
                frames[item["path"]] = encoded[offset:offset + size]
                offset += size
            self.assertEqual(offset, len(encoded))
            captured["frames"] = frames
            helper = transfer._REMOTE_HELPER_SCRIPT.replace(
                "            verify_tree(stage, expected)\n            os.fsync(stage)",
                "            verify_tree(stage, expected)\n"
                "            __import__('time').sleep(0.5)\n"
                "            os.fsync(stage)",
                1,
            )
            process = subprocess.Popen(
                [sys.executable, "-c", helper],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=kwargs["cwd"],
            )
            completed: dict[str, tuple[bytes, bytes]] = {}

            def communicate() -> None:
                completed["output"] = process.communicate(encoded)

            worker = threading.Thread(target=communicate)
            worker.start()
            stage = (
                Path(header["publication_root"])
                / "generations"
                / f".huroshiki-stage-{header['operation_id']}"
            )
            for _ in range(100):
                if stage.is_dir() and sum(
                    path.is_file() for path in stage.rglob("*")
                ) == len(header["files"]):
                    break
                time.sleep(0.01)
            self.assertTrue(stage.is_dir())
            captured["staging"] = {
                path.relative_to(stage).as_posix(): {
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "mode": path.stat().st_mode & 0o777,
                }
                for path in stage.rglob("*")
                if path.is_file()
            }
            worker.join(5)
            self.assertFalse(worker.is_alive())
            stdout, stderr = completed["output"]
            return BoundedProcessResult(
                process.returncode,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                False,
                False,
            )

        try:
            with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
                transfer, "run_bounded_process", side_effect=run
            ):
                result = transfer.execute_publish_transfer(plan)
            self.assertFalse(result.reused)
            header_files = {item["path"]: item for item in captured["header"]["files"]}
            manifest_files = {entry.relative_path.as_posix(): entry for entry in manifest.files}
            self.assertTrue(
                {
                    "kubejs/server_scripts/common.js",
                    "kubejs/server_scripts/server.js",
                    "config/example.toml",
                }.issubset(manifest_files)
            )
            self.assertEqual(set(header_files), set(manifest_files))
            self.assertEqual(set(captured["staging"]), set(manifest_files))
            generation = Path(target.publication_root) / "generations" / result.generation_id
            for relative, entry in manifest_files.items():
                detached = plan._payload_root / Path(*entry.relative_path.parts)
                remote = generation / Path(*entry.relative_path.parts)
                descriptor = header_files[relative]
                self.assertEqual(descriptor["path"], relative)
                self.assertEqual(descriptor["size"], entry.size)
                self.assertEqual(descriptor["sha256"], entry.sha256)
                self.assertEqual(descriptor["mode"], entry.mode)
                self.assertEqual(descriptor["source_kind"], entry.source_kind)
                self.assertEqual(
                    captured["staging"][relative],
                    {"size": entry.size, "sha256": entry.sha256, "mode": entry.mode},
                )
                self.assertEqual(captured["frames"][relative], detached.read_bytes())
                self.assertEqual(remote.read_bytes(), detached.read_bytes())
                self.assertEqual(remote.stat().st_size, entry.size)
                self.assertEqual(hashlib.sha256(remote.read_bytes()).hexdigest(), entry.sha256)
                self.assertEqual(remote.stat().st_mode & 0o777, entry.mode)
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_existing_generation_reuse_rejects_missing_nested_content_file(self) -> None:
        self._assert_nested_reuse_rejected(
            lambda generation: (generation / "kubejs/server_scripts/common.js").unlink()
        )

    def test_existing_generation_reuse_rejects_modified_nested_content_same_size(self) -> None:
        def mutate(generation: Path) -> None:
            path = generation / "kubejs/server_scripts/server.js"
            path.write_bytes(b"X" * len(b"server script\n"))
            self.assertEqual(path.stat().st_size, len(b"server script\n"))

        self._assert_nested_reuse_rejected(mutate)

    def test_existing_generation_reuse_rejects_modified_nested_content_different_size(self) -> None:
        self._assert_nested_reuse_rejected(
            lambda generation: (generation / "config/example.toml").write_bytes(b"different\n")
        )

    def test_existing_generation_reuse_rejects_nested_content_mode_drift(self) -> None:
        def mutate(generation: Path) -> None:
            path = generation / "kubejs/server_scripts/common.js"
            path.chmod(0o600 if path.stat().st_mode & 0o777 != 0o600 else 0o644)

        self._assert_nested_reuse_rejected(mutate)

    def test_existing_generation_reuse_rejects_unexpected_nested_content_file(self) -> None:
        self._assert_nested_reuse_rejected(
            lambda generation: (generation / "kubejs/server_scripts/unexpected.js").write_bytes(b"unexpected")
        )

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

    def test_prepare_cleanup_failure_retains_retryable_plan(self) -> None:
        manifest, target = self._manifest_and_target()
        primary = transfer.PublishTransferPlanningError("verification failed")
        with patch.object(
            transfer, "_verify_workspace", side_effect=primary
        ), patch.object(
            transfer, "_remove_tree", side_effect=OSError("cleanup failed")
        ):
            with self.assertRaises(transfer.PublishTransferCleanupError) as error:
                transfer.prepare_publish_transfer("demo", manifest, target)
        retained = error.exception.plan
        self.assertIsNotNone(retained)
        self.assertIs(error.exception.primary_error, primary)
        self.assertEqual(retained.state, "cleanup-pending")
        transfer.retry_discard_publish_transfer_plan(retained)
        self.assertEqual(retained.state, "discarded")

    def test_prepare_interrupt_cleans_workspace_and_preserves_interrupt(self) -> None:
        manifest, target = self._manifest_and_target()
        with patch.object(
            transfer, "_verify_workspace", side_effect=KeyboardInterrupt()
        ):
            with self.assertRaises(KeyboardInterrupt):
                transfer.prepare_publish_transfer("demo", manifest, target)

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

    def test_configured_publication_path_drift_is_rejected_before_remote_process(self) -> None:
        manifest = pack_publish.plan_pack_publish_manifest("demo")
        target = publish_target.publish_remote_target_from_legacy_settings(
            rsync_target=f"publisher@publish.example:{self.root / 'remote'}",
            ssh_host="minecraft@game.example",
            stack_dir="/srv/minecraft",
            service="minecraft",
        )
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        stale = packctl.DeploymentSettings(
            f"publisher@publish.example:{self.root / 'other'}",
            "minecraft@game.example",
            "/srv/minecraft",
            "minecraft",
        )
        try:
            with patch.object(packctl, "deployment_settings", return_value=stale), patch.object(
                transfer, "run_bounded_process"
            ) as run:
                with self.assertRaisesRegex(transfer.PublishTransferExecutionError, "target changed"):
                    transfer.execute_publish_transfer(plan)
            run.assert_not_called()
        finally:
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
            "target_side": plan.target_side,
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

    def test_status_recovery_failure_retains_uncertain_state(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        recovery = (
            Path(target.publication_root)
            / "generations"
            / f".huroshiki-stage-{plan.operation_id}"
        ).as_posix()
        responses = iter(
            [
                BoundedProcessResult(
                    1,
                    json.dumps(
                        {
                            "ok": False,
                            "status": "integrity_failure",
                            "error": "remote staging cleanup failed",
                            "recovery_path": recovery,
                        }
                    )
                    + "\n",
                    "",
                    False,
                    False,
                )
            ]
        )

        def run(*args, **kwargs):
            try:
                return next(responses)
            except StopIteration as error:
                raise OSError("status SSH launch failed") from error

        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=run
        ) as process:
            with self.assertRaisesRegex(
                transfer.PublishTransferUncertainError,
                "commit state is uncertain",
            ):
                transfer.execute_publish_transfer(plan)
        self.assertEqual(process.call_count, 2)
        self.assertEqual(plan.state, "uncertain")
        self.assertEqual(plan.recovery_path, Path(recovery))
        with patch.object(
            transfer,
            "run_bounded_process",
            return_value=BoundedProcessResult(
                0,
                json.dumps({
                    "ok": True, "status": "cleaned",
                    "target_side": plan.target_side,
                    "operation_id": plan.operation_id,
                    "manifest_digest": plan.manifest_digest,
                    "target_config_digest": plan.target_config_digest,
                    "generation_id": plan.generation_id,
                }) + "\n",
                "",
                False,
                False,
            ),
        ):
            transfer.retry_discard_publish_transfer_plan(plan)
        self.assertEqual(plan.state, "discarded")

    def test_status_failure_without_response_retains_deterministic_stage_for_discard(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        responses = iter([BoundedProcessResult(0, "", "", False, False)])

        def run(*args, **kwargs):
            try:
                return next(responses)
            except StopIteration as error:
                raise OSError("status SSH launch failed") from error

        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=run
        ) as process:
            with self.assertRaises(transfer.PublishTransferUncertainError):
                transfer.execute_publish_transfer(plan)
        self.assertEqual(process.call_count, 2)
        self.assertEqual(plan.state, "uncertain")
        self.assertEqual(plan.recovery_path, plan.staging_path)
        with patch.object(
            transfer,
            "run_bounded_process",
            return_value=BoundedProcessResult(
                0,
                json.dumps({
                    "ok": True, "status": "cleaned",
                    "target_side": plan.target_side,
                    "operation_id": plan.operation_id,
                    "manifest_digest": plan.manifest_digest,
                    "target_config_digest": plan.target_config_digest,
                    "generation_id": plan.generation_id,
                }) + "\n",
                "",
                False,
                False,
            ),
        ):
            transfer.retry_discard_publish_transfer_plan(plan)
        self.assertEqual(plan.state, "discarded")

    def test_committed_status_cleans_retained_remote_stage_before_success(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        recovery = (
            Path(target.publication_root)
            / "generations"
            / f".huroshiki-stage-{plan.operation_id}"
        ).as_posix()
        committed = {
            "ok": True,
            "status": "committed",
            "target_side": plan.target_side,
            "operation_id": plan.operation_id,
            "manifest_digest": plan.manifest_digest,
            "target_config_digest": plan.target_config_digest,
            "generation_id": plan.generation_id,
            "recovery_path": recovery,
        }
        responses = iter(
            [
                BoundedProcessResult(
                    1,
                    json.dumps(
                        {
                            "ok": False,
                            "status": "integrity_failure",
                            "error": "remote staging cleanup failed",
                            "recovery_path": recovery,
                        }
                    )
                    + "\n",
                    "",
                    False,
                    False,
                ),
                BoundedProcessResult(0, json.dumps(committed) + "\n", "", False, False),
                BoundedProcessResult(
                    0,
                    json.dumps({
                        "ok": True, "status": "cleaned",
                        "target_side": plan.target_side,
                        "operation_id": plan.operation_id,
                        "manifest_digest": plan.manifest_digest,
                        "target_config_digest": plan.target_config_digest,
                        "generation_id": plan.generation_id,
                    }) + "\n",
                    "",
                    False,
                    False,
                ),
            ]
        )
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=lambda *args, **kwargs: next(responses)
        ) as run:
            result = transfer.execute_publish_transfer(plan)
        self.assertFalse(result.reused)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(plan.state, "executed")
        self.assertIsNone(plan.recovery_path)
        transfer.discard_publish_transfer_plan(plan)

    def test_remote_integrity_failure_retains_cleanup_pending_recovery(self) -> None:
        manifest, target = self._manifest_and_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        recovery = (
            Path(target.publication_root)
            / "generations"
            / f".huroshiki-stage-{plan.operation_id}"
        ).as_posix()
        responses = iter(
            [
                BoundedProcessResult(
                    1,
                    json.dumps(
                        {
                            "ok": False,
                            "status": "integrity_failure",
                            "error": "remote staging cleanup failed",
                            "recovery_path": recovery,
                        }
                    )
                    + "\n",
                    "",
                    False,
                    False,
                ),
                BoundedProcessResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "status": "not_committed",
                            "target_side": plan.target_side,
                            "operation_id": plan.operation_id,
                            "manifest_digest": plan.manifest_digest,
                            "target_config_digest": plan.target_config_digest,
                            "generation_id": plan.generation_id,
                            "recovery_path": recovery,
                        }
                    )
                    + "\n",
                    "",
                    False,
                    False,
                ),
                BoundedProcessResult(
                    1,
                    json.dumps(
                        {
                            "ok": False,
                            "status": "integrity_failure",
                            "error": "refusing to remove symlink",
                        }
                    )
                    + "\n",
                    "",
                    False,
                    False,
                ),
            ]
        )
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=lambda *args, **kwargs: next(responses)
        ) as run:
            with self.assertRaises(transfer.PublishTransferCleanupError):
                transfer.execute_publish_transfer(plan)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(plan.state, "cleanup-pending")
        self.assertEqual(plan.recovery_path, Path(recovery))
        with patch.object(
            transfer,
            "run_bounded_process",
            return_value=BoundedProcessResult(
                0,
                '{"ok":true,"status":"unexpected"}\n',
                "",
                False,
                False,
            ),
        ) as retry:
            with self.assertRaises(transfer.PublishTransferCleanupError):
                transfer.retry_discard_publish_transfer_plan(plan)
        retry.assert_called_once()
        self.assertEqual(plan.state, "cleanup-pending")
        with patch.object(
            transfer,
            "run_bounded_process",
            return_value=BoundedProcessResult(
                0,
                json.dumps({
                    "ok": True, "status": "cleaned",
                    "target_side": plan.target_side,
                    "operation_id": plan.operation_id,
                    "manifest_digest": plan.manifest_digest,
                    "target_config_digest": plan.target_config_digest,
                    "generation_id": plan.generation_id,
                }) + "\n",
                "",
                False,
                False,
            ),
        ) as retry:
            transfer.retry_discard_publish_transfer_plan(plan)
        retry.assert_called_once()
        self.assertEqual(plan.state, "discarded")
        self.assertIsNone(plan.recovery_path)

    def test_lifecycle_failures_do_not_spawn_status_recovery(self) -> None:
        manifest, target = self._manifest_and_target()
        for field, message in (
            (
                "termination_incomplete",
                "Publish transfer process termination was incomplete",
            ),
            (
                "orphaned_descendants",
                "Publish transfer left background processes after completion",
            ),
        ):
            with self.subTest(field=field):
                plan = transfer.prepare_publish_transfer("demo", manifest, target)
                result = BoundedProcessResult(
                    0,
                    "",
                    "",
                    False,
                    False,
                    **{field: True},
                )
                with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
                    transfer, "run_bounded_process", return_value=result
                ) as run:
                    with self.assertRaisesRegex(transfer.PublishTransferUncertainError, message):
                        transfer.execute_publish_transfer(plan)
                run.assert_called_once()
                self.assertEqual(plan.state, "uncertain")
                with patch.object(
                    transfer,
                    "run_bounded_process",
                    return_value=BoundedProcessResult(
                        0,
                        json.dumps({
                            "ok": True, "status": "cleaned",
                            "target_side": plan.target_side,
                            "operation_id": plan.operation_id,
                            "manifest_digest": plan.manifest_digest,
                            "target_config_digest": plan.target_config_digest,
                            "generation_id": plan.generation_id,
                        }) + "\n",
                        "",
                        False,
                        False,
                    ),
                ):
                    transfer.discard_publish_transfer_plan(plan)

    def test_cancel_timeout_and_output_failures_preserve_their_cause(self) -> None:
        manifest, target = self._manifest_and_target()
        for result, message in (
            (
                BoundedProcessResult(-15, "", "", True, False),
                "Publish transfer was cancelled",
            ),
            (
                BoundedProcessResult(-15, "", "", False, True),
                "Publish transfer timed out",
            ),
            (
                BoundedProcessResult(0, "", "", False, False, output_limit_exceeded=True),
                "Publish transfer output exceeded the supported limit",
            ),
        ):
            with self.subTest(message=message):
                plan = transfer.prepare_publish_transfer("demo", manifest, target)
                responses = iter(
                    [
                        result,
                        BoundedProcessResult(
                            0,
                            json.dumps(
                                {
                                    "ok": True,
                                    "status": "not_committed",
                                    "target_side": plan.target_side,
                                    "operation_id": plan.operation_id,
                                    "manifest_digest": plan.manifest_digest,
                                    "target_config_digest": plan.target_config_digest,
                                    "generation_id": plan.generation_id,
                                }
                            )
                            + "\n",
                            "",
                            False,
                            False,
                        ),
                        BoundedProcessResult(
                            0,
                            json.dumps({
                                "ok": True, "status": "cleaned",
                                "target_side": plan.target_side,
                                "operation_id": plan.operation_id,
                                "manifest_digest": plan.manifest_digest,
                                "target_config_digest": plan.target_config_digest,
                                "generation_id": plan.generation_id,
                            }) + "\n",
                            "",
                            False,
                            False,
                        ),
                    ]
                )
                with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
                    transfer, "run_bounded_process", side_effect=lambda *args, **kwargs: next(responses)
                ) as run:
                    with self.assertRaisesRegex(transfer.PublishTransferExecutionError, message):
                        transfer.execute_publish_transfer(plan)
                self.assertEqual(run.call_count, 3)
                self.assertEqual(plan.state, "failed")
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
            "target_side": "server",
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

    @unittest.skipUnless(sys.platform == "linux", "generation no-clobber uses Linux renameat2")
    def test_generation_commit_is_atomic_no_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            header = self._header(directory)
            encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
            prefix = (
                transfer._FRAME_HEADER
                + struct.pack("!I", 1)
                + struct.pack("!I", len(encoded))
                + encoded
            )
            process = subprocess.Popen(
                [sys.executable, "-c", transfer._REMOTE_HELPER_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdin is not None
            process.stdin.write(prefix)
            process.stdin.flush()
            stage = Path(directory) / "generations" / (".huroshiki-stage-" + "a" * 32)
            for _ in range(200):
                if stage.is_dir():
                    break
                time.sleep(0.01)
            self.assertTrue(stage.is_dir())
            final = Path(directory) / "generations" / header["generation_id"]
            final.mkdir()
            marker = final / "external-marker"
            marker.write_bytes(b"must survive")
            process.stdin.write(struct.pack("!Q", len(b"payload")) + b"payload")
            process.stdin.close()
            stdout = process.stdout
            stderr = process.stderr
            output = stdout.read() if stdout is not None else b""
            if stderr is not None:
                stderr.read()
            code = process.wait()
            if stdout is not None:
                stdout.close()
            if stderr is not None:
                stderr.close()
            response = json.loads(output.decode("utf-8").splitlines()[-1])
            self.assertNotEqual(code, 0)
            self.assertEqual(response["status"], "integrity_failure")
            self.assertEqual(marker.read_bytes(), b"must survive")
            self.assertFalse(stage.exists())

    def test_status_reports_a_retained_stage_even_when_generation_is_committed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            header = self._header(directory)
            code, response = self._request(directory, header, (b"payload",))
            self.assertEqual(code, 0)
            self.assertEqual(response["status"], "committed")
            stage = Path(directory) / "generations" / (".huroshiki-stage-" + "a" * 32)
            stage.mkdir()
            status_header = dict(header, request="status")
            code, response = self._request(directory, status_header, ())
            self.assertEqual(code, 0)
            self.assertEqual(response["status"], "committed")
            self.assertEqual(
                response["recovery_path"],
                f"{directory}/generations/{stage.name}",
            )
            cleanup_header = dict(header, request="cleanup")
            code, response = self._request(directory, cleanup_header, ())
            self.assertEqual(code, 0)
            self.assertEqual(response["status"], "cleaned")


if __name__ == "__main__":
    unittest.main()
