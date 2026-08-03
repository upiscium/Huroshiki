from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib
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

    def _run(
        self,
        output: bytes | None = None,
        process_result=None,
        candidate: DependencyCandidate | None = None,
        process_callable=None,
    ):
        candidate = self.candidate if candidate is None else candidate
        calls = []

        if process_callable is None:

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
        else:

            def process(command, **kwargs):
                calls.append((list(command), kwargs))
                return process_callable(command, **kwargs)

        with mock.patch("provider_artifacts.run_bounded_process", side_effect=process):
            result = materialize_provider_artifact(
                candidate,
                self.context,
                workspace=self.workspace,
                cancel_event=self.cancel,
                deadline=self.deadline,
            )
        return result, calls

    def test_mode_metadata_curseforge_is_supported_without_download_url(self) -> None:
        candidate = DependencyCandidate(
            "curseforge:12345",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "server"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
                'mode = "metadata:curseforge"\n'
                '[update.curseforge]\n'
                'project-id = 12345\n'
                'file-id = 54321\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )

        with mock.patch("provider_artifacts.download_url_artifact") as download:
            with mock.patch("provider_artifacts.run_bounded_process") as run_process:
                calls = []

                def process(command, **kwargs):
                    calls.append((list(command), kwargs))
                    if command[0] == "java":
                        destination = Path(kwargs["cwd"]).parent / "output" / "mods" / "demo.jar"
                        destination.parent.mkdir(parents=True)
                        destination.write_bytes(self.payload)
                    return BoundedProcessResult(0, "", "", False, False)

                run_process.side_effect = process
                result = materialize_provider_artifact(
                    candidate,
                    self.context,
                    workspace=self.workspace,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )

        self.assertEqual(result.sha256, hashlib.sha256(self.payload).hexdigest())
        download.assert_not_called()
        self.assertEqual(calls[0][0], ["packwiz", "refresh"])
        self.assertEqual(calls[1][0][:7], [
            "java",
            "-jar",
            str(self.installer),
            "--no-gui",
            "--side",
            "client",
            "--pack-folder",
        ])

    def test_metadata_contents_are_preserved_when_materializing_metadata_mode(self) -> None:
        candidate = DependencyCandidate(
            "curseforge:12345",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "server"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
                'mode = "metadata:curseforge"\n'
                '[update.curseforge]\n'
                'project-id = 12345\n'
                'file-id = 54321\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )
        observed: Path | None = None

        def process(command, **kwargs):
            nonlocal observed
            if command[0] == "packwiz":
                observed = Path(kwargs["cwd"]) / candidate.relative_metadata_path
            if command[0] == "java":
                destination = Path(kwargs["cwd"]).parent / "output" / "mods" / "demo.jar"
                destination.parent.mkdir(parents=True)
                destination.write_bytes(self.payload)
            return BoundedProcessResult(0, "", "", False, False)

        with mock.patch("provider_artifacts.download_url_artifact"):
            with mock.patch("provider_artifacts.run_bounded_process", side_effect=process):
                materialize_provider_artifact(
                    candidate,
                    self.context,
                    workspace=self.workspace,
                    cancel_event=self.cancel,
                    deadline=self.deadline,
                )

        self.assertIsNotNone(observed)
        materialized = tomllib.loads(observed.read_text("utf-8"))
        download = materialized["download"]
        self.assertEqual(download["mode"], "metadata:curseforge")
        self.assertNotIn("url", download)
        self.assertEqual(download["hash-format"], "sha256")
        self.assertEqual(download["hash"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(materialized["side"], "both")

    def test_metadata_curseforge_with_download_url_is_rejected(self) -> None:
        candidate = DependencyCandidate(
            "curseforge:12345",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "both"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
                'mode = "metadata:curseforge"\n'
                'url = "https://example.invalid/demo.jar"\n'
                '[update.curseforge]\n'
                'project-id = 12345\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )
        with self.assertRaisesRegex(
            ProviderArtifactError,
            "metadata:curseforge mode must not include download.url",
        ):
            self._run(candidate=candidate)

    def test_http_server_is_closed_if_thread_start_fails(self) -> None:
        candidate = DependencyCandidate(
            self.candidate.provider_identity,
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            self.candidate.contents,
            "both",
        )

        server_instances: list[object] = []

        class FakeServer:
            def __init__(self, _address, _handler):
                self.server_port = 9
                self.closed = False
                server_instances.append(self)

            def server_close(self) -> None:
                self.closed = True

            def shutdown(self) -> None:
                raise RuntimeError("shutdown should not be called")

        class FakeThread:
            def __init__(self, *args, **kwargs) -> None:
                self.started = False

            def start(self) -> None:
                self.started = True
                raise RuntimeError("thread start failed")

            def is_alive(self) -> bool:
                return self.started

            def join(self, timeout: float | None = None) -> None:
                return None

        fake_server = FakeServer
        fake_thread = FakeThread
        with mock.patch("provider_artifacts.HTTPServer", fake_server):
            with mock.patch("provider_artifacts.threading.Thread", fake_thread):
                with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                    self._run()
                self.assertEqual(len(server_instances), 1)
                self.assertTrue(server_instances[0].closed)

    def test_missing_curseforge_url_is_invalid_for_url_mode(self) -> None:
        candidate = DependencyCandidate(
            "curseforge:123",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "both"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )
        with self.assertRaisesRegex(
            ProviderArtifactError,
            r"provider artifact URL mode requires an HTTP\(S\) URL",
        ):
            self._run(candidate=candidate)

    def test_curseforge_manual_download_is_detected_from_installer_output(self) -> None:
        candidate = DependencyCandidate(
            "curseforge:12345",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "both"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
                'mode = "metadata:curseforge"\n'
                '[update.curseforge]\n'
                'project-id = 12345\n'
                'file-id = 54321\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )

        def process(command, **kwargs):
            if command[0] == "packwiz":
                return BoundedProcessResult(0, "", "", False, False)
            if command[0] == "java":
                return BoundedProcessResult(
                    1,
                    "Packwiz installer output:\nThis project requires manual download",
                    "",
                    False,
                    False,
                )
            raise AssertionError(command)

        with self.assertRaisesRegex(
            ProviderArtifactError,
            "CurseForge artifact requires manual download and cannot be automatically verified",
        ):
            self._run(candidate=candidate, process_callable=process)

    def test_zero_project_id_is_rejected_in_metadata_curseforge_mode(self) -> None:
        candidate = DependencyCandidate(
            "curseforge:123",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "both"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
                'mode = "metadata:curseforge"\n'
                '[update.curseforge]\n'
                'project-id = 0\n'
                'file-id = 0\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )
        with self.assertRaisesRegex(
            ProviderArtifactError,
            "provider metadata has no positive numeric CurseForge project ID",
        ):
            self._run(candidate=candidate)

    def test_missing_file_id_is_rejected_in_metadata_curseforge_mode(self) -> None:
        candidate = DependencyCandidate(
            "curseforge:123",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "both"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
                'mode = "metadata:curseforge"\n'
                '[update.curseforge]\n'
                'project-id = 123\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )
        with self.assertRaisesRegex(
            ProviderArtifactError,
            "provider metadata has no positive numeric CurseForge file ID",
        ):
            self._run(candidate=candidate)

    def test_project_id_mismatch_between_identity_and_metadata_is_rejected(self) -> None:
        candidate = DependencyCandidate(
            "curseforge:111",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "both"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
                'mode = "metadata:curseforge"\n'
                '[update.curseforge]\n'
                'project-id = 999\n'
                'file-id = 123\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )
        with self.assertRaisesRegex(
            ProviderArtifactError,
            "provider identity project ID does not match metadata project-id",
        ):
            self._run(candidate=candidate)

    def test_non_positive_file_id_is_rejected_in_metadata_curseforge_mode(self) -> None:
        candidate = DependencyCandidate(
            "curseforge:123",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "both"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
                'mode = "metadata:curseforge"\n'
                '[update.curseforge]\n'
                'project-id = 123\n'
                'file-id = 0\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )
        with self.assertRaisesRegex(
            ProviderArtifactError,
            "provider metadata has no positive numeric CurseForge file ID",
        ):
            self._run(candidate=candidate)

    def test_non_curseforge_metadata_curseforge_mode_is_rejected(self) -> None:
        candidate = DependencyCandidate(
            "modrinth:some-slug",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "both"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
                'mode = "metadata:curseforge"\n'
                '[update.curseforge]\n'
                'project-id = 123\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )
        with self.assertRaisesRegex(
            ProviderArtifactError,
            "metadata:curseforge mode is unsupported for non-CurseForge metadata",
        ):
            self._run(candidate=candidate)

    def test_unknown_curseforge_mode_is_rejected(self) -> None:
        candidate = DependencyCandidate(
            "curseforge:123",
            self.candidate.relative_metadata_path,
            self.candidate.filename,
            (
                'name = "Demo"\n'
                'filename = "demo.jar"\n'
                'side = "both"\n'
                '[download]\n'
                'hash-format = "sha256"\n'
                f'hash = "{hashlib.sha256(self.payload).hexdigest()}"\n'
                'mode = "mystery"\n'
                '[update.curseforge]\n'
                'project-id = 123\n'
            ).encode(
                "utf-8"
            ),
            "both",
        )
        with self.assertRaisesRegex(
            ProviderArtifactError,
            "provider artifact mode 'mystery' is unsupported",
        ):
            self._run(candidate=candidate)

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
