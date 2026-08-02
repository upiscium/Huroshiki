from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from dependency_equivalence import DependencyCandidate, EquivalenceContext
from provider_artifacts import ProviderArtifactError, materialize_provider_artifact
from process_runner import BoundedProcessResult


class ProviderArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.installer = self.workspace / "installer.jar"
        self.installer.write_bytes(b"installer")
        payload = self.workspace / "payload.jar"
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(
                "fabric.mod.json", json.dumps({"id": "demo", "version": "1"})
            )
        self.payload = payload.read_bytes()
        digest = hashlib.sha256(self.payload).hexdigest()
        metadata = (
            'name = "Demo"\nfilename = "demo.jar"\nside = "both"\n'
            '[download]\nhash-format = "sha256"\n'
            f'hash = "{digest}"\nurl = "https://example.invalid/demo.jar"\n'
            '[update.modrinth]\nmod-id = "demo"\nversion = "v1"\n'
        ).encode()
        self.candidate = DependencyCandidate(
            "modrinth:demo", "mods/demo.pw.toml", "demo.jar", metadata, "both"
        )
        self.context = EquivalenceContext(
            "1.21.1", "fabric", "0.16.0", "snapshot", "1"
        )
        self.cancel = threading.Event()
        self.deadline = 9_999_999_999.0
        self.env = mock.patch.dict(
            "os.environ", {"HUROSHIKI_PACKWIZ_INSTALLER_JAR": str(self.installer)}
        )
        self.env.start()
        self.download = mock.patch(
            "provider_artifacts.download_url_artifact",
            side_effect=self._download,
        )
        self.download.start()

    def _download(self, _url, _cancel, _log, _loader, _limit, **kwargs):
        kwargs["retained_path"].write_bytes(self.payload)
        return mock.Mock(sha256=hashlib.sha256(self.payload).hexdigest())

    def tearDown(self) -> None:
        self.download.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, output: bytes | None = None, process_result=None):
        calls = []

        def process(command, **kwargs):
            calls.append((list(command), kwargs))
            if command[0] == "java":
                destination = Path(kwargs["cwd"]).parent / "output" / "mods" / "demo.jar"
                destination.parent.mkdir(parents=True)
                destination.write_bytes(self.payload if output is None else output)
            result = process_result or BoundedProcessResult(0, "", "", False, False)
            callback = kwargs.get("result_callback")
            if callback is not None:
                callback(result)
            return result

        with mock.patch("provider_artifacts.run_bounded_process", side_effect=process):
            result = materialize_provider_artifact(
                self.candidate,
                self.context,
                workspace=self.workspace,
                cancel_event=self.cancel,
                deadline=self.deadline,
            )
        return result, calls

    def test_commands_share_workspace_lifecycle_and_deadline(self) -> None:
        result, calls = self._run()
        self.assertEqual(result.sha256, hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(calls[0][0], ["packwiz", "refresh"])
        self.assertEqual(calls[1][0][:7], [
            "java", "-jar", str(self.installer), "--no-gui", "--side", "client", "--pack-folder"
        ])
        self.assertEqual(calls[0][1]["deadline"], self.deadline)
        self.assertIs(calls[0][1]["cancel_event"], self.cancel)
        self.assertTrue(str(calls[0][1]["cwd"]).startswith(str(self.workspace)))

    def test_hash_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProviderArtifactError, "hash"):
            self._run(output=b"not-the-declared-jar")

    def test_exact_hash_remains_available_when_semantic_metadata_is_invalid(self) -> None:
        with mock.patch(
            "provider_artifacts.parse_semantic_jar", side_effect=ValueError("invalid")
        ):
            result, _calls = self._run()
        self.assertEqual(result.sha256, hashlib.sha256(self.payload).hexdigest())
        self.assertIsNone(result.semantic_identity)

    def test_symlink_is_rejected(self) -> None:
        def process(command, **kwargs):
            if command[0] == "java":
                output = Path(kwargs["cwd"]).parent / "output" / "mods" / "demo.jar"
                output.parent.mkdir(parents=True)
                output.symlink_to(self.workspace / "payload.jar")
            return BoundedProcessResult(0, "", "", False, False)

        with mock.patch("provider_artifacts.run_bounded_process", side_effect=process):
            with self.assertRaisesRegex(ProviderArtifactError, "ordinary file"):
                materialize_provider_artifact(
                    self.candidate, self.context, workspace=self.workspace
                )

    def test_cancelled_process_flags_fail_closed(self) -> None:
        result = BoundedProcessResult(0, "", "", True, False)
        with mock.patch("provider_artifacts.run_bounded_process", return_value=result):
            with self.assertRaisesRegex(ProviderArtifactError, "cancelled"):
                materialize_provider_artifact(
                    self.candidate, self.context, workspace=self.workspace
                )

    def test_filename_cannot_escape_materialization_workspace(self) -> None:
        candidate = DependencyCandidate(
            self.candidate.provider_identity,
            self.candidate.relative_metadata_path,
            "../demo.jar",
            self.candidate.contents,
            "both",
        )
        with self.assertRaises(ProviderArtifactError):
            materialize_provider_artifact(candidate, self.context, workspace=self.workspace)


if __name__ == "__main__":
    unittest.main()
