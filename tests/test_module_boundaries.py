import unittest

import deploy_support
import huroshiki_core
import pack_migration
import pack_migration_conflicts
import pack_migration_resolution
import pack_migration_roots
import pack_migration_version_intent
import packctl
import publish_activation
import publish_orchestration
import publish_restart
import publish_transfer
import project_locks
import publish_target
import url_artifacts


class ModuleBoundaryTest(unittest.TestCase):
    def test_packctl_reexports_lock_types_and_error(self) -> None:
        self.assertIs(packctl.ProjectLockMetadata, project_locks.ProjectLockMetadata)
        self.assertTrue(issubclass(packctl.ProjectLock, project_locks.ProjectLock))

    def test_packctl_reexports_deploy_support_api(self) -> None:
        self.assertIs(packctl.RsyncChange, deploy_support.RsyncChange)
        self.assertIs(packctl.DeployPreview, deploy_support.DeployPreview)
        self.assertIs(packctl.parse_rsync_changes, deploy_support.parse_rsync_changes)

    def test_core_reexports_url_artifact_api(self) -> None:
        self.assertIs(huroshiki_core.HuroshikiError, url_artifacts.HuroshikiError)
        self.assertIs(huroshiki_core.UrlArtifact, url_artifacts.UrlArtifact)
        self.assertIs(
            huroshiki_core.download_url_artifact,
            url_artifacts.download_url_artifact,
        )

    def test_core_reexports_pack_migration_api(self) -> None:
        self.assertTrue(callable(huroshiki_core.PackCopyMigrationSession))
        self.assertTrue(callable(huroshiki_core.format_pack_copy_migration_preview))
        self.assertTrue(callable(huroshiki_core.format_pack_copy_migration_requirements))
        self.assertIs(huroshiki_core.PackMigrationPlan, pack_migration.PackMigrationPlan)
        self.assertIs(
            huroshiki_core.PackMigrationPublicationPlan,
            pack_migration.PackMigrationPublicationPlan,
        )
        self.assertIs(
            huroshiki_core.PackMigrationResolutionPlan,
            pack_migration_resolution.PackMigrationResolutionPlan,
        )
        self.assertIs(
            huroshiki_core.PackMigrationUnresolvedRoot,
            pack_migration_resolution.PackMigrationUnresolvedRoot,
        )
        self.assertIs(
            huroshiki_core.PackMigrationResolutionRequest,
            pack_migration_conflicts.PackMigrationResolutionRequest,
        )
        self.assertIs(
            huroshiki_core.PackMigrationRootResolution,
            pack_migration_conflicts.PackMigrationRootResolution,
        )
        self.assertIs(
            huroshiki_core.PackMigrationRootSelection,
            pack_migration_roots.PackMigrationRootSelection,
        )
        self.assertIs(
            huroshiki_core.PackMigrationVersionIntentFacts,
            pack_migration_version_intent.PackMigrationVersionIntentFacts,
        )
        self.assertIs(
            huroshiki_core.PackMigrationVersionIntentIssue,
            pack_migration_version_intent.PackMigrationVersionIntentIssue,
        )
        self.assertIs(
            huroshiki_core.PackMigrationConflictResolutionError,
            pack_migration_conflicts.PackMigrationConflictResolutionError,
        )
        self.assertIs(
            huroshiki_core.PackMigrationVersionIntentError,
            pack_migration_version_intent.PackMigrationVersionIntentError,
        )
        for name in (
            "snapshot_pack_migration_source",
            "plan_pack_copy_migration",
            "resolve_pack_migration_plan",
            "commit_pack_migration_root_selection",
            "create_pack_migration_resolution_request",
            "resolve_pack_migration_conflicts",
            "prepare_pack_migration_publication",
            "apply_pack_migration_publication",
            "retry_pack_migration_cleanup",
            "discard_pack_migration_plan",
            "exact_mod_artifact_selection",
            "verify_exact_mod_metadata",
            "run_noninteractive_packwiz",
        ):
            self.assertTrue(callable(getattr(huroshiki_core, name)))

    def test_core_reexports_publish_target_api(self) -> None:
        self.assertIs(
            huroshiki_core.PublishRemoteTarget,
            publish_target.PublishRemoteTarget,
        )
        self.assertIs(
            huroshiki_core.PublishRestartTarget,
            publish_target.PublishRestartTarget,
        )
        self.assertIs(
            huroshiki_core.PublishSshEndpoint,
            publish_target.PublishSshEndpoint,
        )
        self.assertIs(
            huroshiki_core.parse_publish_ssh_endpoint,
            publish_target.parse_publish_ssh_endpoint,
        )
        self.assertIs(
            huroshiki_core.compute_publish_remote_target_digest,
            publish_target.compute_publish_remote_target_digest,
        )

    def test_core_reexports_publish_transfer_api(self) -> None:
        self.assertIs(
            huroshiki_core.PublishTransferPlan,
            publish_transfer.PublishTransferPlan,
        )
        self.assertIs(
            huroshiki_core.PublishStagedGeneration,
            publish_transfer.PublishStagedGeneration,
        )
        self.assertIs(
            huroshiki_core.prepare_publish_transfer,
            publish_transfer.prepare_publish_transfer,
        )
        self.assertIs(
            huroshiki_core.execute_publish_transfer,
            publish_transfer.execute_publish_transfer,
        )

    def test_core_reexports_publish_semantic_verification_api(self) -> None:
        self.assertIs(
            huroshiki_core.PublishSemanticVerification,
            publish_activation.PublishSemanticVerification,
        )
        self.assertIs(
            huroshiki_core.verify_publish_generation,
            publish_activation.verify_publish_generation,
        )
        self.assertIs(
            huroshiki_core.PublishActivatedGeneration,
            publish_activation.PublishActivatedGeneration,
        )
        self.assertIs(
            huroshiki_core.activate_publish_generation,
            publish_activation.activate_publish_generation,
        )
        self.assertIs(
            huroshiki_core.retry_publish_activation_cleanup,
            publish_activation.retry_publish_activation_cleanup,
        )

    def test_core_reexports_publish_restart_api(self) -> None:
        self.assertIs(
            huroshiki_core.PublishRestartResult,
            publish_restart.PublishRestartResult,
        )
        self.assertIs(
            huroshiki_core.PublishRestartIntegrityError,
            publish_restart.PublishRestartIntegrityError,
        )
        self.assertIs(
            huroshiki_core.restart_activated_publish,
            publish_restart.restart_activated_publish,
        )

    def test_core_reexports_publish_orchestration_api(self) -> None:
        self.assertIs(
            huroshiki_core.PackPublishPlan,
            publish_orchestration.PackPublishPlan,
        )
        self.assertIs(
            huroshiki_core.PackPublishResult,
            publish_orchestration.PackPublishResult,
        )
        self.assertIs(
            huroshiki_core.PackPublishProgress,
            publish_orchestration.PackPublishProgress,
        )
        self.assertIs(
            huroshiki_core.PackPublishExecutionError,
            publish_orchestration.PackPublishExecutionError,
        )
        self.assertIs(
            huroshiki_core.PackPublishCancelled,
            publish_orchestration.PackPublishCancelled,
        )
        self.assertIs(
            huroshiki_core.PackPublishDeadlineExceeded,
            publish_orchestration.PackPublishDeadlineExceeded,
        )
        self.assertIs(
            huroshiki_core.PackPublishCleanupError,
            publish_orchestration.PackPublishCleanupError,
        )
        self.assertIs(
            huroshiki_core.PackPublishRestartError,
            publish_orchestration.PackPublishRestartError,
        )
        self.assertIs(
            huroshiki_core.PackPublishRestartUncertainError,
            publish_orchestration.PackPublishRestartUncertainError,
        )
        self.assertIs(
            huroshiki_core.plan_pack_publish,
            publish_orchestration.plan_pack_publish,
        )
        self.assertIs(
            huroshiki_core.execute_pack_publish,
            publish_orchestration.execute_pack_publish,
        )
        self.assertIs(
            huroshiki_core.retry_pack_publish_cleanup,
            publish_orchestration.retry_pack_publish_cleanup,
        )


if __name__ == "__main__":
    unittest.main()
