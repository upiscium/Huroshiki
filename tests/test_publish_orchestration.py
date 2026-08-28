from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import threading
import time
import unittest
from unittest.mock import Mock, patch

import pack_publish
import packctl
import publish_activation
import publish_orchestration as publish
import publish_restart
import publish_target
import publish_transfer
from tests import test_pack_publish_manifest as manifest_tests


def make_target() -> publish_target.PublishRemoteTarget:
    return publish_target.publish_remote_target_from_legacy_settings(
        rsync_target="publisher@example.org:/srv/packs/demo",
        ssh_host="restart.example.org",
        stack_dir="/srv/restart/demo",
        service="minecraft",
        server_id=publish_target.LEGACY_SERVER_ID,
    )


class PublishOrchestrationTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture = manifest_tests.PackPublishManifestTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.fixture = fixture
        self.manifest = pack_publish.plan_pack_publish_manifest("demo")
        self.target = make_target()
        self.settings = packctl.DeploymentSettings(
            "publisher@example.org:/srv/packs/demo",
            "restart.example.org",
            "/srv/restart/demo",
            "minecraft",
        )
        self.cancel = threading.Event()
        self.deadline = time.monotonic() + 600

    def plan(self, *, progress=None) -> publish.PackPublishPlan:
        with patch.object(
            publish.packctl, "deployment_settings", return_value=self.settings
        ):
            return publish.plan_pack_publish(
                "demo",
                cancel_event=self.cancel,
                deadline=self.deadline,
                progress=progress,
            )

    def tokens(self, plan: publish.PackPublishPlan):
        owner = Mock(spec=publish_transfer.PublishTransferPlan, name="transfer-owner")
        staged = publish_transfer.PublishStagedGeneration(
            plan.manifest_digest,
            plan.target_config_digest,
            plan.generation_id,
            plan.target.publication_root / "generations" / plan.generation_id,
            tuple(
                publish_transfer.PublishStagedFile(
                    entry.relative_path, entry.size, entry.sha256, entry.mode
                )
                for entry in plan.manifest.files
            ),
            plan.manifest.total_bytes,
            False,
        )
        verified = publish_activation.PublishSemanticVerification(
            plan.manifest_digest,
            plan.target_config_digest,
            plan.generation_id,
            "a" * 64,
            "b" * 64,
            len(plan.manifest.files),
        )
        activated = publish_activation.PublishActivatedGeneration(
            plan.manifest_digest,
            plan.target_config_digest,
            plan.generation_id,
            staged.generation_path,
            plan.target.publication_root / "current",
            None,
            False,
        )
        restart = publish_restart.PublishRestartResult(
            plan.manifest_digest,
            plan.target_config_digest,
            plan.generation_id,
            True,
            True,
            "succeeded",
            0,
        )
        return owner, staged, verified, activated, restart

    def phase_mocks(self, plan: publish.PackPublishPlan, *, restart=None, discard=None):
        owner, staged, verified, activated, successful_restart = self.tokens(plan)
        return {
            "prepare_publish_transfer": Mock(return_value=owner),
            "execute_publish_transfer": Mock(return_value=staged),
            "verify_publish_generation": Mock(return_value=verified),
            "activate_publish_generation": Mock(return_value=activated),
            "restart_activated_publish": Mock(
                return_value=successful_restart if restart is None else restart
            ),
            "discard_publish_transfer_plan": Mock() if discard is None else discard,
        }, (owner, staged, verified, activated, successful_restart)

    def patch_phases(self, mocks):
        return patch.multiple(publish, **{name: mock for name, mock in mocks.items()})

    def test_plan_is_deterministic_network_free_and_binds_authority(self) -> None:
        progress = Mock()
        with patch.object(publish, "plan_pack_publish_manifest", return_value=self.manifest) as manifest, patch.object(
            publish, "publish_remote_target_from_legacy_settings", return_value=self.target
        ) as target, patch.object(publish, "prepare_publish_transfer") as prepare, patch.object(
            publish, "restart_activated_publish"
        ) as restart:
            first = self.plan(progress=progress)
            second = self.plan()
        self.assertEqual(first.manifest_digest, second.manifest_digest)
        self.assertEqual(first.target_config_digest, second.target_config_digest)
        self.assertEqual(first.generation_id, second.generation_id)
        self.assertEqual(first.target, self.target)
        self.assertEqual(first.target_config_digest, self.target.config_digest)
        manifest.assert_called()
        target.assert_called()
        prepare.assert_not_called()
        restart.assert_not_called()
        self.assertTrue(
            all(call.args[0].phase in {"planning", "validated"} for call in progress.call_args_list)
        )

    def test_plan_rejects_invalid_pack_target_cancel_and_deadline(self) -> None:
        with self.assertRaises(publish.PackPublishExecutionError):
            self.plan_pack_publish_bad_pack()
        with patch.object(
            publish, "publish_remote_target_from_legacy_settings", side_effect=publish_target.PublishTargetError("bad target")
        ):
            with self.assertRaises(publish.PackPublishExecutionError):
                self.plan()
        self.cancel.set()
        with self.assertRaises(publish.PackPublishCancelled):
            self.plan()
        self.cancel.clear()
        with self.assertRaises(publish.PackPublishDeadlineExceeded):
            publish.plan_pack_publish("demo", deadline=0)

    def plan_pack_publish_bad_pack(self):
        with patch.object(publish.packctl, "deployment_settings", return_value=self.settings):
            return publish.plan_pack_publish("../invalid", deadline=self.deadline)

    def test_phases_chain_exact_tokens_and_cleanup_last(self) -> None:
        plan = self.plan()
        mocks, (owner, staged, verified, activated, restart) = self.phase_mocks(plan)
        calls: list[tuple[str, object]] = []
        values = {
            "prepare_publish_transfer": owner,
            "execute_publish_transfer": staged,
            "verify_publish_generation": verified,
            "activate_publish_generation": activated,
            "restart_activated_publish": restart,
        }
        for name, value in values.items():
            mocks[name].side_effect = lambda *args, _name=name, _value=value, **kwargs: (
                calls.append((_name, args)), _value
            )[1]
        mocks["discard_publish_transfer_plan"].side_effect = lambda *args, **kwargs: calls.append(("cleanup", args))
        with self.patch_phases(mocks):
            result = publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
        self.assertEqual([name for name, _ in calls], [
            "prepare_publish_transfer", "execute_publish_transfer", "verify_publish_generation",
            "activate_publish_generation", "restart_activated_publish", "cleanup",
        ])
        self.assertIs(calls[1][1][0], owner)
        self.assertIs(calls[2][1][0], staged)
        self.assertIs(calls[3][1][0], staged)
        self.assertIs(calls[4][1][0], activated)
        self.assertEqual(result.final_status, "published")

    def test_same_controls_reach_all_phases_and_replacements_fail(self) -> None:
        plan = self.plan()
        mocks, _ = self.phase_mocks(plan)
        with self.patch_phases(mocks):
            publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
        for name in (
            "prepare_publish_transfer", "execute_publish_transfer", "verify_publish_generation",
            "activate_publish_generation", "restart_activated_publish",
        ):
            call = mocks[name].call_args
            self.assertIs(call.kwargs["cancel_event"], self.cancel)
            self.assertIs(call.kwargs["deadline"], self.deadline)
        prepare_deadline = mocks["discard_publish_transfer_plan"].call_args.kwargs[
            "deadline"
        ]
        self.assertNotEqual(prepare_deadline, self.deadline)
        with self.assertRaises(ValueError):
            publish.execute_pack_publish(plan, cancel_event=threading.Event(), deadline=self.deadline)

    def test_success_cleanup_precedes_terminal_progress_and_callback_errors_are_ignored(self) -> None:
        plan = self.plan()
        mocks, _ = self.phase_mocks(plan)
        events = []
        mocks["discard_publish_transfer_plan"].side_effect = lambda *a, **k: events.append("cleanup")
        callback = Mock(side_effect=lambda event: (events.append(event.phase), 1 / 0)[1])
        with self.patch_phases(mocks):
            result = publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline, progress=callback)
        self.assertEqual(result.final_status, "published")
        self.assertTrue(result.publication_succeeded)
        self.assertEqual(events[-2:], ["cleanup", "published"])
        self.assertIsInstance(result, publish.PackPublishResult)
        mocks["discard_publish_transfer_plan"].assert_called_once()

    def test_prepare_and_each_later_failure_stop_following_phases(self) -> None:
        phase_names = [
            "prepare_publish_transfer",
            "execute_publish_transfer",
            "verify_publish_generation",
            "activate_publish_generation",
            "restart_activated_publish",
        ]
        for failed in range(5):
            with self.subTest(failed=failed):
                plan = self.plan()
                mocks, _ = self.phase_mocks(plan)
                mocks[phase_names[failed]].side_effect = RuntimeError("phase failure")
                with self.patch_phases(mocks):
                    with self.assertRaises(publish.PackPublishExecutionError):
                        publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
                if failed == 0:
                    mocks["discard_publish_transfer_plan"].assert_not_called()
                else:
                    mocks["discard_publish_transfer_plan"].assert_called_once()
                self.assertTrue(
                    all(
                        not mocks[name].called
                        for name in phase_names[failed + 1 :]
                    )
                )

    def test_stale_lower_errors_are_surfaced_without_replanning_or_retargeting(self) -> None:
        for name, error in (
            ("prepare_publish_transfer", publish_transfer.PublishTransferPlanningError("stale source")),
            ("verify_publish_generation", publish_activation.PublishSemanticVerificationError("stale target")),
        ):
            with self.subTest(phase=name):
                plan = self.plan()
                mocks, _ = self.phase_mocks(plan)
                mocks[name].side_effect = error
                with patch.object(publish, "plan_pack_publish_manifest") as replan, patch.object(
                    publish, "publish_remote_target_from_legacy_settings"
                ) as retarget, self.patch_phases(mocks):
                    with self.assertRaises(publish.PackPublishExecutionError) as raised:
                        publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
                self.assertIs(raised.exception.primary_error, error)
                replan.assert_not_called()
                retarget.assert_not_called()

    def test_known_restart_result_fails_without_retry_and_preserves_publication(self) -> None:
        plan = self.plan()
        failed_restart = publish_restart.PublishRestartResult(
            plan.manifest_digest, plan.target_config_digest, plan.generation_id,
            True, False, "failed", 1,
        )
        mocks, _ = self.phase_mocks(plan, restart=failed_restart)
        with self.patch_phases(mocks):
            with self.assertRaises(publish.PackPublishRestartError) as raised:
                publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
        self.assertTrue(raised.exception.publication_succeeded)
        self.assertEqual(raised.exception.result.final_status, "restart_failed")
        self.assertEqual(mocks["discard_publish_transfer_plan"].call_count, 1)
        mocks["restart_activated_publish"].assert_called_once()

    def test_prelaunch_and_unexpected_restart_errors_preserve_exact_authority(self) -> None:
        for lower in (
            publish_restart.PublishRestartError("stale restart target"),
            publish_restart.PublishRestartError("local SSH launch failed"),
        ):
            with self.subTest(lower=str(lower)):
                plan = self.plan()
                mocks, _ = self.phase_mocks(plan)
                mocks["restart_activated_publish"].side_effect = lower
                with self.patch_phases(mocks):
                    with self.assertRaises(publish.PackPublishRestartError) as raised:
                        publish.execute_pack_publish(
                            plan,
                            cancel_event=self.cancel,
                            deadline=self.deadline,
                        )
                result = raised.exception.result
                self.assertTrue(result.publication_succeeded)
                self.assertTrue(result.remote_verified)
                self.assertTrue(result.activated)
                self.assertFalse(result.restart_attempted)
                self.assertFalse(result.restart_succeeded)
                self.assertEqual(result.restart_status, "not_started")
                self.assertEqual(result.final_status, "restart_not_started")
                mocks["restart_activated_publish"].assert_called_once()

        plan = self.plan()
        mocks, _ = self.phase_mocks(plan)
        generic = RuntimeError("unexpected restart boundary failure")
        mocks["restart_activated_publish"].side_effect = generic
        with self.patch_phases(mocks):
            with self.assertRaises(
                publish.PackPublishRestartUncertainError
            ) as raised:
                publish.execute_pack_publish(
                    plan,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )
        result = raised.exception.result
        self.assertTrue(result.publication_succeeded)
        self.assertTrue(result.restart_attempted)
        self.assertFalse(result.restart_succeeded)
        self.assertEqual(result.restart_status, "uncertain")
        self.assertEqual(result.final_status, "restart_uncertain")
        self.assertIs(raised.exception.primary_error, generic)
        mocks["restart_activated_publish"].assert_called_once()

    def test_uncertain_restart_result_and_prelaunch_controls_preserve_publication(self) -> None:
        plan = self.plan()
        uncertain = publish_restart.PublishRestartResult(
            plan.manifest_digest, plan.target_config_digest, plan.generation_id,
            True, False, "uncertain", None,
        )
        mocks, _ = self.phase_mocks(plan)
        mocks["restart_activated_publish"].side_effect = (
            publish_restart.PublishRestartIntegrityError("uncertain", uncertain)
        )
        with self.patch_phases(mocks):
            with self.assertRaises(publish.PackPublishRestartUncertainError) as raised:
                publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
        self.assertTrue(raised.exception.publication_succeeded)
        self.assertEqual(raised.exception.result.restart_status, "uncertain")
        self.assertTrue(raised.exception.result.restart_attempted)

        for lower, expected in (
            (publish_restart.PublishRestartCancelled("cancelled"), publish.PackPublishCancelled),
            (publish_restart.PublishRestartDeadlineExceeded("deadline"), publish.PackPublishDeadlineExceeded),
        ):
            plan = self.plan()
            mocks, _ = self.phase_mocks(plan)
            mocks["restart_activated_publish"].side_effect = lower
            with self.patch_phases(mocks):
                with self.assertRaises(expected) as raised:
                    publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
            self.assertTrue(raised.exception.publication_succeeded)

    def test_orchestration_checkpoint_before_restart_is_not_an_attempt(self) -> None:
        plan = self.plan()
        mocks, (_, _, _, activated, _) = self.phase_mocks(plan)

        def activate_then_cancel(*args, **kwargs):
            self.cancel.set()
            return activated

        mocks["activate_publish_generation"].side_effect = activate_then_cancel
        with self.patch_phases(mocks):
            with self.assertRaises(publish.PackPublishCancelled) as raised:
                publish.execute_pack_publish(
                    plan,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )
        self.cancel.clear()
        result = raised.exception.result
        self.assertTrue(result.publication_succeeded)
        self.assertFalse(result.restart_attempted)
        self.assertEqual(result.restart_status, "not_started")
        self.assertEqual(result.final_status, "cancelled")
        mocks["restart_activated_publish"].assert_not_called()

        plan = self.plan()
        mocks, _ = self.phase_mocks(plan)
        real_checkpoint = publish._checkpoint
        checkpoints = 0

        def expire_before_restart(cancel_event, deadline):
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == 5:
                raise publish.PackPublishDeadlineExceeded(
                    "deadline",
                    result=None,
                    phase="checkpoint",
                    primary_error=None,
                )
            real_checkpoint(cancel_event, deadline)

        with self.patch_phases(mocks), patch.object(
            publish, "_checkpoint", side_effect=expire_before_restart
        ):
            with self.assertRaises(publish.PackPublishDeadlineExceeded) as raised:
                publish.execute_pack_publish(
                    plan,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )
        result = raised.exception.result
        self.assertTrue(result.publication_succeeded)
        self.assertFalse(result.restart_attempted)
        self.assertEqual(result.restart_status, "not_started")
        self.assertEqual(result.final_status, "cancelled")
        mocks["restart_activated_publish"].assert_not_called()

    def test_phase_cancellation_and_deadline_keep_partial_authority(self) -> None:
        for phase, trigger, expected in (
            ("execute_publish_transfer", "cancel", publish.PackPublishCancelled),
            (
                "verify_publish_generation",
                "deadline",
                publish.PackPublishDeadlineExceeded,
            ),
            ("activate_publish_generation", "cancel", publish.PackPublishCancelled),
        ):
            with self.subTest(phase=phase, trigger=trigger):
                plan = self.plan()
                mocks, _ = self.phase_mocks(plan)

                def fail(*args, **kwargs):
                    if trigger == "cancel":
                        self.cancel.set()
                    else:
                        plan._deadline = time.monotonic() - 1
                    raise RuntimeError(trigger)

                mocks[phase].side_effect = fail
                with self.patch_phases(mocks):
                    with self.assertRaises(expected) as raised:
                        publish.execute_pack_publish(
                            plan,
                            cancel_event=self.cancel,
                            deadline=self.deadline,
                        )
                self.assertFalse(raised.exception.result.publication_succeeded)
                mocks["restart_activated_publish"].assert_not_called()
                mocks["discard_publish_transfer_plan"].assert_called_once()
                self.cancel.clear()

    def test_cleanup_pending_retains_both_errors_and_retry_runs_cleanup_only(self) -> None:
        for primary_failure in (None, RuntimeError("primary")):
            with self.subTest(primary_failure=primary_failure):
                plan = self.plan()
                mocks, _ = self.phase_mocks(plan)
                if primary_failure is not None:
                    mocks["verify_publish_generation"].side_effect = primary_failure
                cleanup_failure = RuntimeError("cleanup")
                mocks["discard_publish_transfer_plan"].side_effect = cleanup_failure
                with self.patch_phases(mocks):
                    with self.assertRaises(publish.PackPublishCleanupError) as raised:
                        publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
                self.assertIs(raised.exception.plan, plan)
                self.assertIs(raised.exception.cleanup_error, cleanup_failure)
                if primary_failure is not None:
                    self.assertIs(raised.exception.primary_error, primary_failure)
                self.assertEqual(plan.state, "cleanup-pending")
                before = {name: mock.call_count for name, mock in mocks.items() if name != "discard_publish_transfer_plan"}
                retry = Mock()
                with patch.object(publish, "retry_discard_publish_transfer_plan", new=retry):
                    publish.retry_pack_publish_cleanup(plan)
                retry.assert_called_once()
                self.assertEqual(before, {name: mock.call_count for name, mock in mocks.items() if name != "discard_publish_transfer_plan"})
                self.assertEqual(plan.state, "completed" if primary_failure is None else "failed")

    def test_cleanup_retry_failure_stays_pending_and_guards_are_one_shot(self) -> None:
        plan = self.plan()
        mocks, _ = self.phase_mocks(plan)
        mocks["discard_publish_transfer_plan"].side_effect = RuntimeError("cleanup")
        with self.patch_phases(mocks):
            with self.assertRaises(publish.PackPublishCleanupError):
                publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
        with patch.object(publish, "retry_discard_publish_transfer_plan", side_effect=RuntimeError("still pending")):
            with self.assertRaises(publish.PackPublishCleanupError):
                publish.retry_pack_publish_cleanup(plan)
        self.assertEqual(plan.state, "cleanup-pending")
        with self.assertRaises(publish.PackPublishExecutionError):
            publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
        with self.assertRaises(publish.PackPublishCleanupError):
            publish.retry_pack_publish_cleanup(self.plan())

    def test_activation_cleanup_retains_publication_and_retries_only_cleanup(self) -> None:
        plan = self.plan()
        mocks, (_, staged, _, activated, _) = self.phase_mocks(plan)
        cleanup = publish_activation.PublishActivationCleanupError(
            "activation cleanup pending",
            plan.target.publication_root
            / (".huroshiki-activation-" + "a" * 32 + ".json"),
            "a" * 32,
            activated=activated,
            expected_status="activated",
        )
        mocks["activate_publish_generation"].side_effect = cleanup
        with self.patch_phases(mocks):
            with self.assertRaises(publish.PackPublishCleanupError) as raised:
                publish.execute_pack_publish(
                    plan,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )
        self.assertTrue(raised.exception.result.publication_succeeded)
        self.assertTrue(raised.exception.result.activated)
        self.assertFalse(raised.exception.result.restart_attempted)
        mocks["restart_activated_publish"].assert_not_called()
        mocks["discard_publish_transfer_plan"].assert_called_once()
        with patch.object(publish, "retry_publish_activation_cleanup") as retry:
            publish.retry_pack_publish_cleanup(plan)
        retry.assert_called_once_with(
            staged,
            plan.manifest,
            plan.target,
            "a" * 32,
            deadline=retry.call_args.kwargs["deadline"],
            finalize_receipt=True,
            expected_status="activated",
        )
        self.assertEqual(plan.state, "failed")
        self.assertTrue(plan.result.publication_succeeded)
        self.assertEqual(plan.result.final_status, "restart_not_started")

    def test_activation_uncertainty_cleanup_success_restores_terminal_failure(self) -> None:
        plan = self.plan()
        mocks, (_, staged, _, _, _) = self.phase_mocks(plan)
        uncertain = publish_activation.PublishActivationUncertainError(
            "activation uncertain",
            plan.target.publication_root
            / (".huroshiki-activation-" + "c" * 32 + ".json"),
            "c" * 32,
        )
        mocks["activate_publish_generation"].side_effect = uncertain
        with self.patch_phases(mocks):
            with self.assertRaises(publish.PackPublishCleanupError):
                publish.execute_pack_publish(
                    plan,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )
        phase_counts = {
            name: mock.call_count
            for name, mock in mocks.items()
            if name != "discard_publish_transfer_plan"
        }
        with patch.object(publish, "retry_publish_activation_cleanup") as retry:
            publish.retry_pack_publish_cleanup(plan)
        retry.assert_called_once()
        self.assertIs(retry.call_args.args[0], staged)
        self.assertFalse(retry.call_args.kwargs["finalize_receipt"])
        self.assertIsNone(retry.call_args.kwargs["expected_status"])
        self.assertEqual(plan.state, "failed")
        self.assertEqual(plan.result.final_status, "publication_failed")
        self.assertFalse(plan.result.publication_succeeded)
        self.assertFalse(plan.result.activated)
        self.assertIs(plan._primary_error, uncertain)
        self.assertIsNone(plan._activation_cleanup_error)
        self.assertIsNone(plan._activation_staged)
        self.assertIsNone(plan._cleanup_error)
        self.assertEqual(
            phase_counts,
            {
                name: mock.call_count
                for name, mock in mocks.items()
                if name != "discard_publish_transfer_plan"
            },
        )
        with self.assertRaises(publish.PackPublishCleanupError):
            publish.retry_pack_publish_cleanup(plan)

    def test_activation_uncertainty_cleanup_failure_then_success(self) -> None:
        plan = self.plan()
        mocks, (owner, staged, _, _, _) = self.phase_mocks(plan)
        uncertain = publish_activation.PublishActivationUncertainError(
            "activation uncertain",
            plan.target.publication_root / (".huroshiki-activation-" + "b" * 32 + ".json"),
            "b" * 32,
        )
        mocks["activate_publish_generation"].side_effect = uncertain
        with self.patch_phases(mocks):
            with self.assertRaises(publish.PackPublishCleanupError) as raised:
                publish.execute_pack_publish(
                    plan,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )
        self.assertFalse(raised.exception.result.publication_succeeded)
        phase_counts = {
            name: mock.call_count
            for name, mock in mocks.items()
            if name != "discard_publish_transfer_plan"
        }
        retry_failure = RuntimeError("activation temp cleanup failed")
        retry = Mock(side_effect=[retry_failure, None])
        with patch.object(
            publish, "retry_publish_activation_cleanup", new=retry
        ):
            with self.assertRaises(publish.PackPublishCleanupError):
                publish.retry_pack_publish_cleanup(plan)
            self.assertEqual(plan.state, "cleanup-pending")
            publish.retry_pack_publish_cleanup(plan)
        self.assertEqual(retry.call_count, 2)
        self.assertIs(retry.call_args.args[0], staged)
        self.assertFalse(retry.call_args.kwargs["finalize_receipt"])
        self.assertIsNone(retry.call_args.kwargs["expected_status"])
        self.assertEqual(plan.state, "failed")
        self.assertEqual(plan.result.final_status, "publication_failed")
        self.assertFalse(plan.result.publication_succeeded)
        self.assertFalse(plan.result.activated)
        self.assertIs(plan._primary_error, uncertain)
        self.assertIsNone(plan._activation_cleanup_error)
        self.assertIsNone(plan._activation_staged)
        self.assertIsNone(plan._cleanup_error)
        self.assertEqual(
            phase_counts,
            {
                name: mock.call_count
                for name, mock in mocks.items()
                if name != "discard_publish_transfer_plan"
            },
        )
        with self.assertRaises(publish.PackPublishCleanupError):
            publish.retry_pack_publish_cleanup(plan)
        self.assertEqual(owner.method_calls, [])

    def test_prepare_cleanup_owner_and_base_exception_are_not_abandoned(self) -> None:
        plan = self.plan()
        mocks, (owner, _, _, _, _) = self.phase_mocks(plan)
        primary = publish_transfer.PublishTransferPlanningError("prepare failed")
        retained = publish_transfer.PublishTransferCleanupError(
            "cleanup pending", plan=owner, primary_error=primary
        )
        mocks["prepare_publish_transfer"].side_effect = retained
        mocks["discard_publish_transfer_plan"].side_effect = RuntimeError(
            "still pending"
        )
        with self.patch_phases(mocks):
            with self.assertRaises(publish.PackPublishCleanupError) as raised:
                publish.execute_pack_publish(
                    plan,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )
        self.assertIs(raised.exception.primary_error, primary)
        self.assertEqual(plan.state, "cleanup-pending")

        retained_interrupt_plan = self.plan()
        retained_interrupt_mocks, (interrupt_owner, _, _, _, _) = self.phase_mocks(
            retained_interrupt_plan
        )
        retained_interrupt_mocks["prepare_publish_transfer"].side_effect = (
            publish_transfer.PublishTransferCleanupError(
                "cleanup pending",
                plan=interrupt_owner,
                primary_error=KeyboardInterrupt(),
            )
        )
        with self.patch_phases(retained_interrupt_mocks):
            with self.assertRaises(KeyboardInterrupt):
                publish.execute_pack_publish(
                    retained_interrupt_plan,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )
        retained_interrupt_mocks["discard_publish_transfer_plan"].assert_called_once()
        self.assertEqual(retained_interrupt_plan.state, "failed")

        interrupted_plan = self.plan()
        interrupted, _ = self.phase_mocks(interrupted_plan)
        interrupted["execute_publish_transfer"].side_effect = KeyboardInterrupt()
        with self.patch_phases(interrupted):
            with self.assertRaises(KeyboardInterrupt):
                publish.execute_pack_publish(
                    interrupted_plan,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )
        interrupted["discard_publish_transfer_plan"].assert_called_once()
        self.assertEqual(interrupted_plan.state, "failed")

    def test_cleanup_is_ordered_after_active_phase_and_models_are_immutable(self) -> None:
        plan = self.plan()
        mocks, _ = self.phase_mocks(plan)
        order = []
        for name, mock in mocks.items():
            value = mock.return_value
            mock.side_effect = lambda *a, _name=name, _value=value, **k: order.append(_name) or _value
        mocks["discard_publish_transfer_plan"].side_effect = lambda *a, **k: order.append("cleanup")
        with self.patch_phases(mocks):
            publish.execute_pack_publish(plan, cancel_event=self.cancel, deadline=self.deadline)
        self.assertEqual(order[-1], "cleanup")
        self.assertEqual(order[:5], [
            "prepare_publish_transfer", "execute_publish_transfer", "verify_publish_generation",
            "activate_publish_generation", "restart_activated_publish",
        ])
        progress = publish.PackPublishProgress("published")
        self.assertRaises(FrozenInstanceError, setattr, progress, "phase", "secret")
        self.assertNotIn("publisher@example.org", repr(plan))
        self.assertNotIn("secret", repr(progress))

    def test_result_rejects_contradictory_restart_terminal_states(self) -> None:
        plan = self.plan()
        valid_failed = publish.PackPublishResult(
            plan.pack_id,
            plan.target_side,
            plan.manifest_digest,
            plan.target_config_digest,
            plan.generation_id,
            True,
            True,
            True,
            True,
            False,
            "failed",
            "restart_failed",
        )
        for changes in (
            {
                "restart_attempted": False,
                "restart_status": "not_started",
            },
            {
                "restart_attempted": False,
                "restart_status": "not_started",
                "final_status": "restart_uncertain",
            },
            {
                "restart_attempted": True,
                "restart_status": "failed",
                "final_status": "restart_not_started",
            },
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(valid_failed, **changes)
