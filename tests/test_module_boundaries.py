import unittest

import deploy_support
import huroshiki_core
import packctl
import publish_activation
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


if __name__ == "__main__":
    unittest.main()
