from __future__ import annotations

import subprocess
import sys
import threading
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import ANY, Mock, patch

import pack_publish
import publish_activation
import publish_orchestration as publish
import publish_restart
import publish_target
import publish_transfer
from process_runner import BoundedProcessResult
from tests import test_pack_publish_manifest as manifest_tests


def target():
    return publish_target.publish_remote_target_from_legacy_settings(
        rsync_target="publisher@example.org:/srv/packs/demo",
        ssh_host="restart.example.org", stack_dir="/srv/restart/demo",
        service="minecraft", server_id=publish_target.LEGACY_SERVER_ID)


class PublishOrchestrationTest(unittest.TestCase):
    def setUp(self):
        fixture = manifest_tests.PackPublishManifestTest(); fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.bundle = pack_publish.plan_pack_publish_manifest_bundle("demo")
        self.server = target()
        self.client = publish_target.publish_client_target_from_remote_target(self.server)
        self.event = threading.Event(); self.deadline = time.monotonic() + 600

    def plan(self):
        with patch.object(publish.packctl, "deployment_settings", return_value=fixture_settings()):
            with patch.object(publish, "plan_pack_publish_manifest_bundle", return_value=self.bundle):
                with patch.object(publish, "_resolve_target", return_value=self.server):
                    return publish.plan_pack_publish("demo", cancel_event=self.event, deadline=self.deadline)

    def phase_mocks(self, plan):
        calls = []
        owners = {s: Mock(name=f"{s}-owner") for s in ("client", "server")}
        staged = {s: Mock(target_side=s, name=f"{s}-staged") for s in owners}
        verified = {s: Mock(target_side=s, name=f"{s}-verified") for s in owners}
        activated = {s: Mock(target_side=s, name=f"{s}-activated") for s in owners}
        for side, owner in owners.items(): owner.target_side = side
        restart = publish_restart.PublishRestartResult(
            plan.manifest_digest, plan.target_config_digest, plan.generation_id,
            True, True, "succeeded", 0)
        def phase(name, values):
            def call(*args, **kwargs):
                value = args[1] if name == "prepare" else args[0]
                side = value.target_side
                calls.append((name, side))
                return values[side]
            return call
        mocks = {
            "prepare_publish_transfer": Mock(side_effect=phase("prepare", owners)),
            "execute_publish_transfer": Mock(side_effect=phase("transfer", staged)),
            "verify_publish_generation": Mock(side_effect=phase("verify", verified)),
            "activate_publish_generation": Mock(side_effect=phase("activate", activated)),
            "verify_activated_publish_generation": Mock(side_effect=phase("active", verified)),
            "restart_activated_publish": Mock(side_effect=lambda *a, **k: calls.append(("restart", "server")) or restart),
            "discard_publish_transfer_plan": Mock(side_effect=lambda owner, **k: calls.append(("cleanup", owner))),
        }
        return mocks, calls

    def test_plan_binds_one_bundle_and_derives_client_namespace(self):
        plan = self.plan()
        self.assertIs(plan.bundle, self.bundle)
        self.assertEqual(plan.client_target.publication_root, plan.server_target.publication_root / "client")
        self.assertEqual(plan.client_generation_id, publish_transfer.compute_publish_generation_id(self.bundle.client, self.client))
        self.assertIs(plan.cancel_event, self.event)

    def test_barriers_and_server_only_restart(self):
        plan = self.plan(); mocks, calls = self.phase_mocks(plan)
        with patch.multiple(publish, **mocks):
            result = publish.execute_pack_publish(plan)
        self.assertEqual([x[0] for x in calls], ["prepare", "prepare", "transfer", "transfer",
            "verify", "verify", "activate", "active", "activate", "active", "restart",
            "cleanup", "cleanup"])
        self.assertEqual(calls[10][1], "server")
        self.assertTrue(result.client.active_verified and result.server.active_verified)

    def test_real_helpers_publish_both_namespaces_before_server_restart(self):
        remote_root = Path(publish.packctl.PACKS).parent / "published"
        server = publish_target.publish_remote_target_from_legacy_settings(
            rsync_target="publisher@example.org:/configured",
            ssh_host="restart.example.org",
            stack_dir="/srv/restart/demo",
            service="minecraft",
            server_id=publish_target.LEGACY_SERVER_ID,
            remote_path=str(remote_root),
        )
        settings = type(
            "Settings",
            (),
            {
                "rsync_target": "publisher@example.org:/configured",
                "ssh_host": "restart.example.org",
                "stack_dir": "/srv/restart/demo",
                "service": "minecraft",
            },
        )()

        def run_helper(command, *, stdin_file, **kwargs):
            result = subprocess.run(
                [sys.executable, "-c", publish_transfer._REMOTE_HELPER_SCRIPT],
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

        with patch.object(
            publish,
            "plan_pack_publish_manifest_bundle",
            return_value=self.bundle,
        ), patch.object(publish, "_resolve_target", return_value=server), patch.object(
            publish.packctl, "deployment_settings", return_value=settings
        ):
            plan = publish.plan_pack_publish(
                "demo", cancel_event=self.event, deadline=self.deadline
            )

            def restart(activated, manifest, target, **kwargs):
                self.assertEqual(activated.target_side, "server")
                self.assertTrue((remote_root / "client" / "current").is_symlink())
                self.assertTrue((remote_root / "current").is_symlink())
                return publish_restart.PublishRestartResult(
                    manifest.manifest_digest,
                    target.config_digest,
                    activated.generation_id,
                    True,
                    True,
                    "succeeded",
                    0,
                )

            with patch.object(
                publish_transfer,
                "run_bounded_process",
                side_effect=run_helper,
            ), patch.object(
                publish, "restart_activated_publish", side_effect=restart
            ) as restart_mock:
                result = publish.execute_pack_publish(plan)

        self.assertEqual(restart_mock.call_count, 1)
        self.assertTrue(result.client.active_verified)
        self.assertTrue(result.server.active_verified)
        self.assertEqual(
            (remote_root / "client" / "current").readlink(),
            Path("generations") / plan.client_generation_id,
        )
        self.assertEqual(
            (remote_root / "current").readlink(),
            Path("generations") / plan.server_generation_id,
        )

    def test_client_failure_blocks_server_activation_and_restart(self):
        plan = self.plan(); mocks, calls = self.phase_mocks(plan)
        mocks["activate_publish_generation"].side_effect = RuntimeError("client failed")
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishExecutionError) as raised:
                publish.execute_pack_publish(plan)
        self.assertFalse(raised.exception.result.server.activated)
        self.assertFalse(raised.exception.result.restart_attempted)
        self.assertEqual([x[0] for x in calls], ["prepare", "prepare", "transfer", "transfer",
            "verify", "verify", "cleanup", "cleanup"])

    def test_server_failure_preserves_client_facts(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        def fail(value, *args, **kwargs):
            if value.target_side == "server": raise RuntimeError("server failed")
            return value
        mocks["activate_publish_generation"].side_effect = fail
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishExecutionError) as raised:
                publish.execute_pack_publish(plan)
        self.assertTrue(raised.exception.result.client.activated)
        self.assertFalse(raised.exception.result.server.activated)

    def test_cleanup_is_independent_and_retry_does_not_repeat_phases(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        failures = [RuntimeError("client cleanup"), RuntimeError("server cleanup")]
        mocks["discard_publish_transfer_plan"].side_effect = failures
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishCleanupError): publish.execute_pack_publish(plan)
            before = sum(m.call_count for n, m in mocks.items() if n != "discard_publish_transfer_plan")
            with patch.object(publish, "retry_discard_publish_transfer_plan") as retry:
                publish.retry_pack_publish_cleanup(plan)
            self.assertEqual(before, sum(m.call_count for n, m in mocks.items() if n != "discard_publish_transfer_plan"))
            self.assertEqual(retry.call_count, 2)

    def test_controls_are_shared_and_one_shot(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        with patch.multiple(publish, **mocks): publish.execute_pack_publish(plan)
        with self.assertRaises(publish.PackPublishExecutionError): publish.execute_pack_publish(plan)
        with self.assertRaises(ValueError): publish.execute_pack_publish(plan, cancel_event=threading.Event())

    # The following regression tests retain the pre-client orchestration
    # coverage.  They deliberately use the dual-side fixture above: a failure
    # in either side must not cause the other side to be silently re-planned.
    def test_plan_is_deterministic_network_free_and_binds_authority(self):
        first = self.plan(); second = self.plan()
        self.assertEqual(first.bundle, second.bundle)
        self.assertEqual(first.manifest_digest, second.manifest_digest)
        self.assertEqual(first.target_config_digest, second.target_config_digest)
        self.assertEqual(first.generation_id, second.generation_id)
        self.assertEqual(first.target_config_digest, first.target.config_digest)

    def test_plan_rejects_invalid_pack_target_cancel_and_deadline(self):
        with self.assertRaises(publish.PackPublishExecutionError):
            publish.plan_pack_publish("../invalid", cancel_event=self.event, deadline=self.deadline)
        with self.assertRaisesRegex(ValueError, "dual client/server"):
            publish.plan_pack_publish(
                "demo", target_side="client", cancel_event=self.event,
                deadline=self.deadline,
            )
        self.event.set()
        with self.assertRaises(publish.PackPublishCancelled): self.plan()
        self.event.clear()
        with self.assertRaises(publish.PackPublishDeadlineExceeded):
            publish.plan_pack_publish("demo", deadline=0)

    def test_phases_chain_exact_tokens_and_cleanup_last(self):
        plan = self.plan(); mocks, calls = self.phase_mocks(plan)
        with patch.multiple(publish, **mocks): result = publish.execute_pack_publish(plan)
        self.assertEqual([name for name, _ in calls], [
            "prepare", "prepare", "transfer", "transfer", "verify", "verify",
            "activate", "active", "activate", "active", "restart", "cleanup", "cleanup",
        ])
        self.assertEqual(result.final_status, "published")

    def test_active_generation_verification_failure_prevents_restart_and_preserves_publication_state(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        failure = publish_activation.PublishSemanticVerificationError("active drift")
        mocks["verify_activated_publish_generation"].side_effect = failure
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishExecutionError) as raised:
                publish.execute_pack_publish(plan)
        self.assertIs(raised.exception.primary_error, failure)
        self.assertFalse(raised.exception.result.restart_attempted)
        self.assertEqual(raised.exception.result.final_status, "publication_failed")
        mocks["restart_activated_publish"].assert_not_called()

    def test_same_controls_reach_all_phases_and_replacements_fail(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        with patch.multiple(publish, **mocks): publish.execute_pack_publish(plan)
        for name in ("prepare_publish_transfer", "execute_publish_transfer",
                     "verify_publish_generation", "activate_publish_generation",
                     "verify_activated_publish_generation", "restart_activated_publish"):
            self.assertIs(mocks[name].call_args.kwargs["cancel_event"], self.event)
            self.assertIs(mocks[name].call_args.kwargs["deadline"], self.deadline)
        with self.assertRaises(ValueError):
            publish.execute_pack_publish(plan, cancel_event=threading.Event())

    def test_success_cleanup_precedes_terminal_progress_and_callback_errors_are_ignored(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan); events = []
        mocks["discard_publish_transfer_plan"].side_effect = lambda *a, **k: events.append("cleanup")
        callback = Mock(side_effect=lambda value: (events.append(value.phase), 1 / 0)[1])
        with patch.multiple(publish, **mocks):
            result = publish.execute_pack_publish(plan, progress=callback)
        self.assertEqual(events[-2:], ["cleanup", "published"])
        self.assertTrue(result.publication_succeeded)

    def test_prepare_and_each_later_failure_stop_following_phases(self):
        names = ["prepare_publish_transfer", "execute_publish_transfer", "verify_publish_generation",
                 "activate_publish_generation", "verify_activated_publish_generation",
                 "restart_activated_publish"]
        for failed in range(len(names)):
            with self.subTest(phase=names[failed]):
                plan = self.plan(); mocks, _ = self.phase_mocks(plan)
                mocks[names[failed]].side_effect = RuntimeError("phase failure")
                with patch.multiple(publish, **mocks):
                    with self.assertRaises(publish.PackPublishExecutionError): publish.execute_pack_publish(plan)
                self.assertTrue(all(not mocks[name].called for name in names[failed + 1:]))

    def test_stale_lower_errors_are_surfaced_without_replanning_or_retargeting(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        stale = publish_transfer.PublishTransferPlanningError("stale source")
        mocks["prepare_publish_transfer"].side_effect = stale
        with patch.object(publish, "plan_pack_publish_manifest_bundle") as replan, patch.object(
            publish, "_resolve_target") as retarget, patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishExecutionError) as raised: publish.execute_pack_publish(plan)
        self.assertIs(raised.exception.primary_error, stale); replan.assert_not_called(); retarget.assert_not_called()

    def test_known_restart_result_fails_without_retry_and_preserves_publication(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        failed = publish_restart.PublishRestartResult(plan.manifest_digest, plan.target_config_digest,
            plan.generation_id, True, False, "failed", 1)
        mocks["restart_activated_publish"].side_effect = None
        mocks["restart_activated_publish"].return_value = failed
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishRestartError) as raised: publish.execute_pack_publish(plan)
        self.assertTrue(raised.exception.publication_succeeded)
        self.assertEqual(raised.exception.result.final_status, "restart_failed")

    def test_prelaunch_and_unexpected_restart_errors_preserve_exact_authority(self):
        for failure, expected in ((publish_restart.PublishRestartError("launch"), "restart_not_started"),
                                  (RuntimeError("boundary"), "restart_uncertain")):
            plan = self.plan(); mocks, _ = self.phase_mocks(plan)
            mocks["restart_activated_publish"].side_effect = failure
            with patch.multiple(publish, **mocks):
                error_type = publish.PackPublishRestartError if expected == "restart_not_started" else publish.PackPublishRestartUncertainError
                with self.assertRaises(error_type) as raised: publish.execute_pack_publish(plan)
            self.assertTrue(raised.exception.publication_succeeded)
            self.assertEqual(raised.exception.result.final_status, expected)

    def test_uncertain_restart_result_and_prelaunch_controls_preserve_publication(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        uncertain = publish_restart.PublishRestartResult(plan.manifest_digest, plan.target_config_digest,
            plan.generation_id, True, False, "uncertain", None)
        mocks["restart_activated_publish"].side_effect = None
        mocks["restart_activated_publish"].return_value = uncertain
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishRestartUncertainError) as raised: publish.execute_pack_publish(plan)
        self.assertTrue(raised.exception.publication_succeeded)
        self.assertTrue(raised.exception.result.restart_attempted)

    def test_orchestration_checkpoint_before_restart_is_not_an_attempt(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        def cancel_after_active(*args, **kwargs): self.event.set(); return mocks["verify_activated_publish_generation"].return_value
        mocks["verify_activated_publish_generation"].side_effect = cancel_after_active
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishCancelled) as raised: publish.execute_pack_publish(plan)
        self.event.clear(); self.assertFalse(raised.exception.result.restart_attempted)
        mocks["restart_activated_publish"].assert_not_called()

    def test_phase_cancellation_and_deadline_keep_partial_authority(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        def fail(*args, **kwargs): self.event.set(); raise RuntimeError("cancelled")
        mocks["execute_publish_transfer"].side_effect = fail
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishCancelled) as raised: publish.execute_pack_publish(plan)
        self.event.clear(); self.assertFalse(raised.exception.result.publication_succeeded)

    def test_cleanup_pending_retains_both_errors_and_retry_runs_cleanup_only(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        cleanup = RuntimeError("cleanup"); mocks["discard_publish_transfer_plan"].side_effect = cleanup
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishCleanupError) as raised: publish.execute_pack_publish(plan)
        self.assertIs(raised.exception.cleanup_error, cleanup); self.assertEqual(plan.state, "cleanup-pending")
        before = sum(m.call_count for n, m in mocks.items() if n != "discard_publish_transfer_plan")
        with patch.object(publish, "retry_discard_publish_transfer_plan") as retry: publish.retry_pack_publish_cleanup(plan)
        self.assertEqual(retry.call_count, 2)
        self.assertEqual(before, sum(m.call_count for n, m in mocks.items() if n != "discard_publish_transfer_plan"))

    def test_cleanup_retry_failure_stays_pending_and_guards_are_one_shot(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        mocks["discard_publish_transfer_plan"].side_effect = RuntimeError("cleanup")
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishCleanupError): publish.execute_pack_publish(plan)
        with patch.object(publish, "retry_discard_publish_transfer_plan", side_effect=RuntimeError("still pending")):
            with self.assertRaises(publish.PackPublishCleanupError): publish.retry_pack_publish_cleanup(plan)
        self.assertEqual(plan.state, "cleanup-pending")
        with self.assertRaises(publish.PackPublishExecutionError): publish.execute_pack_publish(plan)

    def test_prepare_cleanup_owner_and_base_exception_are_not_abandoned(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        owner = mocks["prepare_publish_transfer"].return_value
        primary = KeyboardInterrupt()
        mocks["prepare_publish_transfer"].side_effect = publish_transfer.PublishTransferCleanupError(
            "cleanup pending", plan=owner, primary_error=primary)
        with patch.multiple(publish, **mocks):
            with self.assertRaises(KeyboardInterrupt): publish.execute_pack_publish(plan)
        mocks["discard_publish_transfer_plan"].assert_called_once()

    def test_activation_cleanup_retains_publication_and_retries_only_cleanup(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        activated = mocks["activate_publish_generation"].return_value
        pending = publish_activation.PublishActivationCleanupError(
            "activation cleanup pending", plan.target.publication_root / ".huroshiki-activation-a.json",
            "a" * 32, activated=activated, expected_status="activated")
        mocks["activate_publish_generation"].side_effect = pending
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishCleanupError) as raised:
                publish.execute_pack_publish(plan)
        self.assertTrue(raised.exception.result.client.activated or raised.exception.result.server.activated)
        mocks["restart_activated_publish"].assert_not_called()
        with patch.object(publish, "retry_publish_activation_cleanup") as retry:
            publish.retry_pack_publish_cleanup(plan)
        retry.assert_called_once(); self.assertEqual(plan.state, "failed")

    def test_activation_uncertainty_cleanup_success_restores_terminal_failure(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        uncertain = publish_activation.PublishActivationUncertainError(
            "activation uncertain", plan.target.publication_root / ".huroshiki-activation-c.json", "c" * 32)
        mocks["activate_publish_generation"].side_effect = uncertain
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishCleanupError): publish.execute_pack_publish(plan)
        with patch.object(publish, "retry_publish_activation_cleanup") as retry:
            publish.retry_pack_publish_cleanup(plan)
        retry.assert_called_once(); self.assertEqual(plan.state, "failed")
        self.assertFalse(plan.result.publication_succeeded)

    def test_activation_uncertainty_cleanup_failure_then_success(self):
        plan = self.plan(); mocks, _ = self.phase_mocks(plan)
        uncertain = publish_activation.PublishActivationUncertainError(
            "activation uncertain", plan.target.publication_root / ".huroshiki-activation-b.json", "b" * 32)
        mocks["activate_publish_generation"].side_effect = uncertain
        with patch.multiple(publish, **mocks):
            with self.assertRaises(publish.PackPublishCleanupError): publish.execute_pack_publish(plan)
        retry = Mock(side_effect=[RuntimeError("temporary"), None])
        with patch.object(publish, "retry_publish_activation_cleanup", new=retry):
            with self.assertRaises(publish.PackPublishCleanupError): publish.retry_pack_publish_cleanup(plan)
            publish.retry_pack_publish_cleanup(plan)
        self.assertEqual(retry.call_count, 2); self.assertEqual(plan.state, "failed")

    def test_cleanup_is_ordered_after_active_phase_and_models_are_immutable(self):
        plan = self.plan(); mocks, calls = self.phase_mocks(plan)
        with patch.multiple(publish, **mocks): publish.execute_pack_publish(plan)
        self.assertEqual(calls[-1][0], "cleanup")
        progress = publish.PackPublishProgress("published")
        with self.assertRaises(FrozenInstanceError): progress.phase = "secret"
        self.assertNotIn("publisher@example.org", repr(plan))

    def test_result_rejects_contradictory_restart_terminal_states(self):
        plan = self.plan()
        valid = publish.PackPublishResult(plan.pack_id, plan.target_side, plan.manifest_digest,
            plan.target_config_digest, plan.generation_id, True, True, True, True, False,
            "failed", "restart_failed")
        with self.assertRaises(ValueError): replace(valid, restart_attempted=False, restart_status="not_started")
        with self.assertRaises(ValueError): replace(valid, restart_attempted=False, restart_status="not_started", final_status="restart_uncertain")
        with self.assertRaises(ValueError): replace(valid, final_status="restart_not_started")

    def test_result_rejects_contradictory_variant_facts(self):
        with self.assertRaises(ValueError):
            publish.PublishVariantFacts("client", remote_verified=True)
        with self.assertRaises(ValueError):
            publish.PublishVariantFacts("client", transferred=True, activated=True)
        with self.assertRaises(ValueError):
            publish.PublishVariantFacts("server", transferred=True, remote_verified=True,
                active_verified=True)

        plan = self.plan()
        complete_client = publish.PublishVariantFacts(
            "client", True, True, True, True
        )
        incomplete_server = publish.PublishVariantFacts("server")
        with self.assertRaises(ValueError):
            publish.PackPublishResult(
                plan.pack_id, plan.target_side, plan.manifest_digest,
                plan.target_config_digest, plan.generation_id,
                True, True, True, False, False, "not_started",
                "restart_not_started", complete_client, incomplete_server,
            )


def fixture_settings():
    return type("Settings", (), dict(
        rsync_target="publisher@example.org:/srv/packs/demo", ssh_host="restart.example.org",
        stack_dir="/srv/restart/demo", service="minecraft"))()


if __name__ == "__main__":
    unittest.main()
