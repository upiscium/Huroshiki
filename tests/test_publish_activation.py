from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

import tomlkit

from tests.test_pack_publish_manifest import PackPublishManifestTest

import packctl
import pack_publish
import publish_activation as activation
import publish_target
import publish_transfer as transfer
from process_runner import BoundedProcessResult


class PublishSemanticVerificationTest(PackPublishManifestTest):
    def setUp(self) -> None:
        super().setUp()
        self.current_settings = self._settings(self._target())
        self.deployment_settings_patch = patch.object(
            packctl,
            "deployment_settings",
            return_value=self.current_settings,
        )
        self.deployment_settings_patch.start()

    def tearDown(self) -> None:
        self.deployment_settings_patch.stop()
        super().tearDown()

    def _target(self) -> publish_target.PublishRemoteTarget:
        return publish_target.publish_remote_target_from_legacy_settings(
            rsync_target=f"publisher@publish.example:{self.root / 'configured'}",
            ssh_host="minecraft@game.example",
            stack_dir="/srv/minecraft",
            service="minecraft",
            remote_path=str(self.root / "remote"),
        )

    def _client_target(self) -> publish_target.PublishClientTarget:
        return publish_target.publish_client_target_from_remote_target(self._target())

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

    def _verified_generation(self):
        manifest, target, plan, staged = self._staged_generation()
        with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
            verification = activation.verify_publish_generation(staged, manifest, target)
        return manifest, target, plan, staged, verification

    def _write_nested_content_fixtures(self) -> dict[str, bytes]:
        fixtures = {
            "common/config/common.toml": b"common config\n",
            "common/kubejs/client_scripts/common.js": b"common script\n",
            "client/kubejs/client_scripts/client.js": b"client script\n",
            "server/kubejs/server_scripts/server.js": b"server script\n",
        }
        for relative, contents in fixtures.items():
            path = self.pack / "content" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        return fixtures

    def _mutated_content_manifest(self, target_side: str, mutation: str):
        manifest = pack_publish.plan_pack_publish_manifest("demo", target_side=target_side)
        index_entry = next(entry for entry in manifest.files if entry.relative_path.as_posix() == "index.toml")
        pack_entry = next(entry for entry in manifest.files if entry.relative_path.as_posix() == "pack.toml")
        index = tomllib.loads((index_entry.contents or b"").decode("utf-8"))
        records = [dict(record) for record in index.get("files", [])]
        content_paths = [
            entry.relative_path.as_posix()
            for entry in manifest.files
            if entry.source_kind == "content"
        ]
        selected_path = content_paths[0]
        selected_index = next(
            index
            for index, record in enumerate(records)
            if record["file"] == selected_path
        )
        if mutation == "missing":
            records.pop(selected_index)
        elif mutation == "wrong-hash":
            records[selected_index] = {"file": selected_path, "hash": "0" * 64}
        elif mutation == "metafile":
            records[selected_index] = {
                "file": selected_path,
                "hash": records[selected_index]["hash"],
                "metafile": True,
            }
        elif mutation == "unexpected":
            records.append({"file": "unexpected/content.txt", "hash": "0" * 64})
        elif mutation == "wrong-side":
            opposite_path = (
                "kubejs/server_scripts/server.js"
                if target_side == "client"
                else "kubejs/client_scripts/client.js"
            )
            records.append({"file": opposite_path, "hash": "0" * 64})
        else:
            self.fail(f"unsupported test mutation: {mutation}")
        index_text = 'hash-format = "sha256"\n'
        for record in records:
            index_text += "\n[[files]]\n"
            index_text += f'file = {json.dumps(record["file"])}\n'
            index_text += f'hash = "{record["hash"]}"\n'
            if "metafile" in record:
                index_text += f'metafile = {str(record["metafile"]).lower()}\n'
        index_bytes = index_text.encode("utf-8")
        pack_document = tomlkit.parse((pack_entry.contents or b"").decode("utf-8"))
        pack_document["index"]["hash"] = hashlib.sha256(index_bytes).hexdigest()
        pack_bytes = tomlkit.dumps(pack_document).encode("utf-8")
        replacements = {
            "index.toml": replace(index_entry, size=len(index_bytes), sha256=hashlib.sha256(index_bytes).hexdigest(), contents=index_bytes),
            "pack.toml": replace(pack_entry, size=len(pack_bytes), sha256=hashlib.sha256(pack_bytes).hexdigest(), contents=pack_bytes),
        }
        files = tuple(
            replacements.get(entry.relative_path.as_posix(), entry)
            for entry in manifest.files
        )
        updated = replace(
            manifest,
            files=files,
            total_bytes=sum(entry.size for entry in files),
            manifest_digest="",
        )
        return replace(updated, manifest_digest=pack_publish.compute_publish_manifest_digest(updated))

    def _content_staged_generation(self, target_side: str):
        manifest = pack_publish.plan_pack_publish_manifest("demo", target_side=target_side)
        target = self._client_target() if target_side == "client" else self._target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
            staged = transfer.execute_publish_transfer(plan)
        return manifest, target, plan, staged

    def _assert_mutated_content_manifest_rejected(
        self, target_side: str, mutation: str
    ) -> None:
        manifest = self._mutated_content_manifest(target_side, mutation)
        target = self._client_target() if target_side == "client" else self._target()
        with patch.object(transfer, "plan_pack_publish_manifest", return_value=manifest):
            plan = transfer.prepare_publish_transfer("demo", manifest, target)
            try:
                with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                    staged = transfer.execute_publish_transfer(plan)
                    with self.assertRaises(activation.PublishSemanticVerificationError):
                        activation.verify_publish_generation(staged, manifest, target)
            finally:
                transfer.discard_publish_transfer_plan(plan)

    def _assert_content_mutation_rejected_for_both_sides(self, mutation: str) -> None:
        self._write_nested_content_fixtures()
        for target_side in ("client", "server"):
            with self.subTest(target_side=target_side):
                self._assert_mutated_content_manifest_rejected(
                    target_side, mutation
                )

    def test_issue_191_nested_content_records_are_verified_for_both_sides(self) -> None:
        self._write_nested_content_fixtures()
        for target_side in ("client", "server"):
            with self.subTest(target_side=target_side):
                manifest, target, plan, staged = self._content_staged_generation(target_side)
                try:
                    records = self.generated_index_records(manifest)
                    for entry in manifest.files:
                        if entry.source_kind == "content":
                            self.assertEqual(records[entry.relative_path.as_posix()].get("metafile", False), False)
                    for path, record in records.items():
                        if path.endswith(".pw.toml"):
                            self.assertTrue(record.get("metafile", False))
                    with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                        activation.verify_publish_generation(staged, manifest, target)
                finally:
                    transfer.discard_publish_transfer_plan(plan)

    def test_remote_semantics_rejects_missing_content_record(self) -> None:
        self._assert_content_mutation_rejected_for_both_sides("missing")

    def test_remote_semantics_rejects_wrong_content_hash(self) -> None:
        self._assert_content_mutation_rejected_for_both_sides("wrong-hash")

    def test_remote_semantics_rejects_content_marked_metafile(self) -> None:
        self._assert_content_mutation_rejected_for_both_sides("metafile")

    def test_remote_semantics_rejects_unexpected_index_record(self) -> None:
        self._assert_content_mutation_rejected_for_both_sides("unexpected")

    def test_remote_semantics_rejects_wrong_side_content_record(self) -> None:
        self._assert_content_mutation_rejected_for_both_sides("wrong-side")

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

    def test_verify_rejects_stale_effective_target_before_remote_request(self) -> None:
        manifest, target, plan, staged = self._staged_generation()
        stale_settings = replace(self.current_settings, ssh_host="other@game.example")
        try:
            with patch.object(packctl, "deployment_settings", return_value=stale_settings), patch.object(
                transfer, "run_bounded_process"
            ) as run:
                with self.assertRaises(activation.PublishSemanticVerificationError):
                    activation.verify_publish_generation(staged, manifest, target)
            run.assert_not_called()
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_configured_publication_path_drift_is_rejected_before_remote_request(self) -> None:
        manifest = pack_publish.plan_pack_publish_manifest("demo")
        target = publish_target.publish_remote_target_from_legacy_settings(
            rsync_target=f"publisher@publish.example:{self.root / 'remote'}",
            ssh_host="minecraft@game.example",
            stack_dir="/srv/minecraft",
            service="minecraft",
        )
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        current = packctl.DeploymentSettings(
            f"publisher@publish.example:{self.root / 'remote'}",
            "minecraft@game.example",
            "/srv/minecraft",
            "minecraft",
        )
        stale = replace(current, rsync_target=f"publisher@publish.example:{self.root / 'other'}")
        try:
            with patch.object(packctl, "deployment_settings", return_value=current), patch.object(
                transfer, "run_bounded_process", side_effect=self._fake_runner()
            ):
                staged = transfer.execute_publish_transfer(plan)
            with patch.object(packctl, "deployment_settings", return_value=stale), patch.object(
                transfer, "run_bounded_process"
            ) as run:
                with self.assertRaises(activation.PublishSemanticVerificationError):
                    activation.verify_publish_generation(staged, manifest, target)
            run.assert_not_called()
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_activate_rejects_stale_restart_target_before_remote_request(self) -> None:
        manifest, target, plan, staged, verification = self._verified_generation()
        stale_settings = replace(self.current_settings, stack_dir="/srv/other-minecraft")
        current = Path(target.publication_root) / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_bytes(b"unchanged")
        try:
            with patch.object(packctl, "deployment_settings", return_value=stale_settings), patch.object(
                transfer, "run_bounded_process"
            ) as run:
                with self.assertRaises(activation.PublishActivationError):
                    activation.activate_publish_generation(
                        staged,
                        verification,
                        target,
                        manifest=manifest,
                    )
            run.assert_not_called()
            self.assertEqual(current.read_bytes(), b"unchanged")
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_activation_from_absent_current_is_atomic_and_bound(self) -> None:
        manifest, target, plan, staged, verification = self._verified_generation()
        try:
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                activated = activation.activate_publish_generation(
                    staged,
                    verification,
                    target,
                    manifest=manifest,
                )
            self.assertFalse(activated.reused)
            self.assertIsNone(activated.previous_generation_id)
            self.assertEqual(activated.current_path, target.publication_root / "current")
            self.assertEqual(
                Path(target.publication_root, "current").readlink().as_posix(),
                f"generations/{staged.generation_id}",
            )
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_activated_nested_generation_drift_is_rejected_by_authoritative_verification(self) -> None:
        expected_content = {
            "kubejs/server_scripts/common.js": b"common script\n",
            "kubejs/server_scripts/server.js": b"server script\n",
            "config/example.toml": b"[example]\nvalue = true\n",
        }
        for source, contents in (
            ("content/common/kubejs/server_scripts/common.js", expected_content["kubejs/server_scripts/common.js"]),
            ("content/server/kubejs/server_scripts/server.js", expected_content["kubejs/server_scripts/server.js"]),
            ("content/common/config/example.toml", expected_content["config/example.toml"]),
        ):
            path = self.pack / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        manifest, target, plan, staged, verification = self._verified_generation()
        try:
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                activated = activation.activate_publish_generation(
                    staged, verification, target, manifest=manifest
                )
            for relative, contents in expected_content.items():
                self.assertEqual((Path(activated.current_path) / relative).read_bytes(), contents)
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                current_verification = activation.verify_activated_publish_generation(
                    activated, manifest, target
                )
            self.assertEqual(current_verification.generation_id, activated.generation_id)
            active_server_file = Path(activated.current_path) / "kubejs/server_scripts/server.js"
            active_server_file.write_bytes(b"drifted after activation")
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                with self.assertRaises(activation.PublishSemanticVerificationError):
                    activation.verify_activated_publish_generation(activated, manifest, target)
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_activation_is_idempotent_for_expected_current(self) -> None:
        manifest, target, plan, staged, verification = self._verified_generation()
        try:
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                first = activation.activate_publish_generation(staged, verification, target, manifest=manifest)
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                second = activation.activate_publish_generation(staged, verification, target, manifest=manifest)
            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertIsNone(second.previous_generation_id)
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_connection_loss_after_direct_reuse_recovers_reused_status(self) -> None:
        manifest, target, plan, staged, verification = self._verified_generation()
        current = Path(target.publication_root) / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(f"generations/{staged.generation_id}")
        calls = 0
        real_runner = self._fake_runner()

        def run(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = real_runner(*args, **kwargs)
            if calls == 1:
                return BoundedProcessResult(0, "malformed response\n", "", False, False)
            return result

        try:
            with patch.object(transfer, "run_bounded_process", side_effect=run):
                activated = activation.activate_publish_generation(
                    staged,
                    verification,
                    target,
                    manifest=manifest,
                )
            self.assertTrue(activated.reused)
            self.assertIsNone(activated.previous_generation_id)
            self.assertEqual(calls, 3)
            self.assertEqual(current.readlink().as_posix(), f"generations/{staged.generation_id}")
            self.assertEqual(list(Path(target.publication_root).glob(".huroshiki-activation-*.json")), [])
            self.assertEqual(list(Path(target.publication_root).glob(".huroshiki-current-*")), [])
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_activation_status_without_receipt_is_uncertain(self) -> None:
        manifest, target, plan, staged = self._staged_generation()
        operation_id = "c" * 32
        files = activation._validate_inputs(staged, manifest, target)
        header = activation._activation_header(
            staged,
            manifest,
            target,
            operation_id,
            files,
            "activation-status",
        )
        current = Path(target.publication_root) / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(f"generations/{staged.generation_id}")
        try:
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                result, response = transfer.run_publish_remote_control_request(
                    target,
                    header,
                    deadline=time.monotonic() + 30,
                    cancel_event=None,
                )
            self.assertTrue(result.succeeded)
            self.assertIsNotNone(response)
            self.assertEqual(response["status"], "uncertain")
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_activation_status_invalid_receipt_reuse_state_is_uncertain(self) -> None:
        manifest, target, plan, staged = self._staged_generation()
        operation_id = "d" * 32
        files = activation._validate_inputs(staged, manifest, target)
        header = activation._activation_header(
            staged,
            manifest,
            target,
            operation_id,
            files,
            "activation-status",
        )
        current = Path(target.publication_root) / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(f"generations/{staged.generation_id}")
        receipt = Path(target.publication_root) / f".huroshiki-activation-{operation_id}.json"
        receipt.write_text(
            json.dumps(
                {
                    "schema": "huroshiki-publish-activation-v1",
                    "operation_id": operation_id,
                    "manifest_digest": manifest.manifest_digest,
                    "target_config_digest": target.config_digest,
                    "generation_id": staged.generation_id,
                    "previous_generation_id": None,
                }
            ),
            encoding="utf-8",
        )
        try:
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                result, response = transfer.run_publish_remote_control_request(
                    target,
                    header,
                    deadline=time.monotonic() + 30,
                    cancel_event=None,
                )
            self.assertTrue(result.succeeded)
            self.assertIsNotNone(response)
            self.assertEqual(response["status"], "uncertain")
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_activation_records_previous_valid_generation(self) -> None:
        manifest, target, plan, staged, verification = self._verified_generation()
        previous = "v1-" + "a" * 64
        generations = Path(target.publication_root) / "generations"
        generations.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged.generation_path, generations / previous)
        (Path(target.publication_root) / "current").symlink_to(f"generations/{previous}")
        try:
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                activated = activation.activate_publish_generation(staged, verification, target, manifest=manifest)
            self.assertEqual(activated.previous_generation_id, previous)
            self.assertFalse(activated.reused)
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_invalid_current_is_uncertain_and_never_repaired(self) -> None:
        manifest, target, plan, staged, verification = self._verified_generation()
        current = Path(target.publication_root) / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_bytes(b"not a symlink")
        try:
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                with self.assertRaises(activation.PublishActivationUncertainError):
                    activation.activate_publish_generation(staged, verification, target, manifest=manifest)
            self.assertEqual(current.read_bytes(), b"not a symlink")
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_all_invalid_current_representations_fail_closed(self) -> None:
        for kind in ("regular", "directory", "fifo", "absolute", "traversal"):
            with self.subTest(kind=kind):
                manifest, target, plan, staged, verification = self._verified_generation()
                current = Path(target.publication_root) / "current"
                current.parent.mkdir(parents=True, exist_ok=True)
                if os.path.lexists(current):
                    if current.is_dir() and not current.is_symlink():
                        current.rmdir()
                    else:
                        current.unlink()
                if kind == "regular":
                    current.write_bytes(b"invalid")
                elif kind == "directory":
                    current.mkdir()
                elif kind == "fifo":
                    os.mkfifo(current)
                elif kind == "absolute":
                    current.symlink_to(staged.generation_path)
                else:
                    current.symlink_to("../outside")
                try:
                    with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                        with self.assertRaises(activation.PublishActivationUncertainError):
                            activation.activate_publish_generation(
                                staged,
                                verification,
                                target,
                                manifest=manifest,
                            )
                    self.assertTrue(os.path.lexists(current))
                finally:
                    transfer.discard_publish_transfer_plan(plan)
                    if os.path.lexists(current):
                        if current.is_dir() and not current.is_symlink():
                            current.rmdir()
                        else:
                            current.unlink()

    def test_lifecycle_failure_does_not_spawn_activation_status(self) -> None:
        for field in ("termination_incomplete", "orphaned_descendants"):
            with self.subTest(field=field):
                manifest, target, plan, staged, verification = self._verified_generation()
                try:
                    result = BoundedProcessResult(
                        0,
                        "",
                        "",
                        False,
                        False,
                        **{field: True},
                    )
                    with patch.object(transfer, "run_bounded_process", return_value=result) as run:
                        with self.assertRaises(activation.PublishActivationUncertainError):
                            activation.activate_publish_generation(staged, verification, target, manifest=manifest)
                    run.assert_called_once()
                finally:
                    transfer.discard_publish_transfer_plan(plan)

    def test_connection_loss_after_rename_recovers_activated_status(self) -> None:
        manifest, target, plan, staged, verification = self._verified_generation()
        previous = "v1-" + "b" * 64
        generations = Path(target.publication_root) / "generations"
        generations.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged.generation_path, generations / previous)
        (Path(target.publication_root) / "current").symlink_to(f"generations/{previous}")
        calls = 0
        real_runner = self._fake_runner()

        def run(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = real_runner(*args, **kwargs)
            if calls == 1:
                return BoundedProcessResult(0, "malformed response\n", "", False, False)
            return result

        try:
            with patch.object(transfer, "run_bounded_process", side_effect=run):
                activated = activation.activate_publish_generation(
                    staged,
                    verification,
                    target,
                    manifest=manifest,
                )
            self.assertFalse(activated.reused)
            self.assertEqual(activated.previous_generation_id, previous)
            self.assertEqual(calls, 3)
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_cleanup_response_binding_failure_is_not_success(self) -> None:
        manifest, target, plan, staged, verification = self._verified_generation()
        calls = 0
        real_runner = self._fake_runner()

        def run(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = real_runner(*args, **kwargs)
            if calls == 1:
                return BoundedProcessResult(0, "malformed response\n", "", False, False)
            if calls == 3:
                response = json.loads(result.stdout)
                response["operation_id"] = "0" * 32
                return BoundedProcessResult(0, json.dumps(response) + "\n", "", False, False)
            return result

        try:
            with patch.object(transfer, "run_bounded_process", side_effect=run):
                with self.assertRaises(activation.PublishActivationCleanupError) as context:
                    activation.activate_publish_generation(
                        staged,
                        verification,
                        target,
                        manifest=manifest,
                    )
                self.assertEqual(
                    context.exception.operation_id,
                    context.exception.recovery_path.name.removeprefix(".huroshiki-activation-").removesuffix(".json"),
                )
                self.assertIsNotNone(context.exception.activated)
                self.assertEqual(
                    context.exception.activated.generation_id,
                    staged.generation_id,
                )
                self.assertEqual(context.exception.expected_status, "activated")
                activation.retry_publish_activation_cleanup(
                    staged,
                    manifest,
                    target,
                    context.exception.operation_id,
                    finalize_receipt=True,
                    expected_status=context.exception.expected_status,
                )
            self.assertEqual(
                Path(target.publication_root, "current").readlink().as_posix(),
                f"generations/{staged.generation_id}",
            )
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_uncertain_status_retains_receipt_until_explicit_finalization(self) -> None:
        manifest, target, plan, staged, verification = self._verified_generation()
        calls = 0
        real_runner = self._fake_runner()

        def run(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                real_runner(*args, **kwargs)
                return BoundedProcessResult(0, "malformed response\n", "", False, False)
            if calls == 2:
                return BoundedProcessResult(1, "", "connection lost", False, False)
            return real_runner(*args, **kwargs)

        try:
            with patch.object(transfer, "run_bounded_process", side_effect=run):
                with self.assertRaises(activation.PublishActivationUncertainError) as context:
                    activation.activate_publish_generation(
                        staged,
                        verification,
                        target,
                        manifest=manifest,
                    )
                self.assertIsNotNone(context.exception.recovery_path)
                assert context.exception.recovery_path is not None
                recovery_path = Path(context.exception.recovery_path)
                self.assertTrue(recovery_path.exists())
                activation.retry_publish_activation_cleanup(
                    staged,
                    manifest,
                    target,
                    context.exception.operation_id,
                )
                self.assertTrue(recovery_path.exists())
                with self.assertRaises(activation.PublishActivationError):
                    activation.retry_publish_activation_cleanup(
                        staged,
                        manifest,
                        target,
                        context.exception.operation_id,
                        finalize_receipt=True,
                    )
                self.assertTrue(recovery_path.exists())
                activation.retry_publish_activation_cleanup(
                    staged,
                    manifest,
                    target,
                    context.exception.operation_id,
                    finalize_receipt=True,
                    expected_status="activated",
                )
                self.assertFalse(recovery_path.exists())
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_cancel_before_activation_does_not_spawn_remote_process(self) -> None:
        manifest, target, plan, staged, verification = self._verified_generation()
        event = threading.Event()
        event.set()
        try:
            with patch.object(transfer, "run_bounded_process") as run:
                with self.assertRaises(activation.PublishActivationError):
                    activation.activate_publish_generation(
                        staged,
                        verification,
                        target,
                        manifest=manifest,
                        cancel_event=event,
                    )
            run.assert_not_called()
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_client_activation_uses_client_namespace(self) -> None:
        manifest = pack_publish.plan_pack_publish_manifest("demo", target_side="client")
        target = self._client_target()
        plan = transfer.prepare_publish_transfer("demo", manifest, target)
        try:
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                staged = transfer.execute_publish_transfer(plan)
                verification = activation.verify_publish_generation(staged, manifest, target)
                activated = activation.activate_publish_generation(
                    staged, verification, target, manifest=manifest
                )
            self.assertEqual(staged.target_side, "client")
            self.assertEqual(verification.target_side, "client")
            self.assertEqual(activated.target_side, "client")
            self.assertEqual(activated.current_path, target.publication_root / "current")
            self.assertEqual(
                Path(target.publication_root, "current").readlink().as_posix(),
                f"generations/{staged.generation_id}",
            )
            self.assertFalse((Path(target.publication_root).parent / "current").exists())
        finally:
            transfer.discard_publish_transfer_plan(plan)

    def test_client_and_server_tokens_cannot_cross(self) -> None:
        server_manifest, server_target, server_plan, server_staged, server_verification = self._verified_generation()
        client_manifest = pack_publish.plan_pack_publish_manifest("demo", target_side="client")
        client_target = self._client_target()
        client_plan = transfer.prepare_publish_transfer("demo", client_manifest, client_target)
        try:
            with patch.object(transfer, "run_bounded_process", side_effect=self._fake_runner()):
                client_staged = transfer.execute_publish_transfer(client_plan)
            with self.assertRaises(activation.PublishActivationError):
                activation.activate_publish_generation(
                    client_staged, server_verification, client_target, manifest=client_manifest
                )
            with self.assertRaises(activation.PublishSemanticVerificationError):
                activation.verify_publish_generation(server_staged, client_manifest, client_target)
        finally:
            transfer.discard_publish_transfer_plan(server_plan)
            transfer.discard_publish_transfer_plan(client_plan)


if __name__ == "__main__":
    unittest.main()
