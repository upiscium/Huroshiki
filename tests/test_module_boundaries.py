import unittest

import deploy_support
import huroshiki_core
import packctl
import project_locks
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


if __name__ == "__main__":
    unittest.main()
