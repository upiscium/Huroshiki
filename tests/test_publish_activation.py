from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.test_pack_publish_manifest import PackPublishManifestTest

import packctl
import pack_publish
import publish_activation as activation
import publish_target
import publish_transfer as transfer
from process_runner import BoundedProcessResult


class PublishSemanticVerificationTest(PackPublishManifestTest):
    def _target(self) -> publish_target.PublishRemoteTarget:
        return publish_target.publish_remote_target_from_legacy_settings(
            rsync_target=f"publisher@publish.example:{self.root / 'configured'}",
            ssh_host="minecraft@game.example",
            stack_dir="/srv/minecraft",
            service="minecraft",
            remote_path=str(self.root / "remote"),
        )

    def _settings(self, target: publish_target.PublishRemoteTarget) -> packctl.DeploymentSettings:
        publication = target.publication_endpoint
        restart = target.restart.endpoint
        return packctl.DeploymentSettings(
            f"{publication.user}@{publication.host}:{self.root / 'configured'}",
            f"{restart.user}@{restart.host}",
            target.restart.stack_dir.as_posix(),
            target.restart.service,
        )

    def _fake_runner(self):
        def run(command, *, stdin_file, **kwargs):
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

    def _staged_generation(self):
        manifest = pack_publish.plan_pack_publish_manifest("demo")
        target = self._target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        with patch.object(packctl, "deployment_settings", return_value=self._settings(target)), patch.object(
            transfer, "run_bounded_process", side_effect=self._fake_runner()
        ):
            staged = transfer.execute_publish_transfer(plan)
        return manifest, target, plan, staged

    def test_valid_generation_is_semantically_verified_without_touching_current(self) -> None:
        manifest, target, plan, staged = self._staged_generation()
        current = Path(target.publication_root) / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_bytes(b"untouched")
        try:
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                verification = activation.verify_publish_generation(staged, manifest, target)
            self.assertEqual(verification.manifest_digest, manifest.manifest_digest)
            self.assertEqual(verification.target_config_digest, target.config_digest)
            self.assertEqual(verification.generation_id, staged.generation_id)
            self.assertEqual(
                verification.pack_toml_sha256,
                next(entry.sha256 for entry in manifest.files if entry.relative_path.as_posix() == "pack.toml"),
            )
            self.assertEqual(
                verification.index_toml_sha256,
                next(entry.sha256 for entry in manifest.files if entry.relative_path.as_posix() == "index.toml"),
            )
            self.assertEqual(current.read_bytes(), b"untouched")
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_remote_generation_mutation_is_rejected_before_semantic_success(self) -> None:
        manifest, target, plan, staged = self._staged_generation()
        try:
            (Path(staged.generation_path) / "pack.toml").write_bytes(b"[versions\n")
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                with self.assertRaises(activation.PublishSemanticVerificationError):
                    activation.verify_publish_generation(staged, manifest, target)
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_staged_identity_mismatch_fails_before_remote_request(self) -> None:
        manifest, target, plan, staged = self._staged_generation()
        try:
            mismatched = replace(staged, generation_id="v1-" + "f" * 64)
            with patch.object(transfer, "run_bounded_process") as run:
                with self.assertRaises(activation.PublishSemanticVerificationError):
                    activation.verify_publish_generation(mismatched, manifest, target)
            run.assert_not_called()
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_response_binding_mismatch_is_uncertain(self) -> None:
        manifest, target, plan, staged = self._staged_generation()
        try:
            wrong = {
                "ok": True,
                "request": "verify",
                "status": "verified",
                "operation_id": "0" * 32,
                "manifest_digest": manifest.manifest_digest,
                "target_config_digest": target.config_digest,
                "generation_id": staged.generation_id,
                "pack_toml_sha256": next(
                    entry.sha256 for entry in manifest.files if entry.relative_path.as_posix() == "pack.toml"
                ),
                "index_toml_sha256": next(
                    entry.sha256 for entry in manifest.files if entry.relative_path.as_posix() == "index.toml"
                ),
            }
            result = BoundedProcessResult(0, json.dumps(wrong) + "\n", "", False, False)
            with patch.object(transfer, "run_bounded_process", return_value=result):
                with self.assertRaises(activation.PublishSemanticVerificationUncertainError):
                    activation.verify_publish_generation(staged, manifest, target)
        finally:
            transfer.discard_publish_transfer_plan(plan)


if __name__ == "__main__":
    unittest.main()
