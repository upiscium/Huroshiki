from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import packctl
from process_runner import BoundedProcessResult


class PackwizDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cwd = self.root / "source"
        self.cwd.mkdir()
        self.state_root = self.root / ".huroshiki"
        self.log_root = self.state_root / "logs"
        self.transaction_root = self.state_root / "transactions"
        self.trash_root = self.state_root / "trash"
        self.deploy_snapshot_root = self.state_root / "deploy-snapshots"
        self.patches = [
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "STATE_ROOT", self.state_root),
            patch.object(packctl, "LOG_ROOT", self.log_root),
            patch.object(packctl, "TRANSACTION_ROOT", self.transaction_root),
            patch.object(packctl, "TRASH_ROOT", self.trash_root),
            patch.object(packctl, "DEPLOY_SNAPSHOT_ROOT", self.deploy_snapshot_root),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _result(
        self,
        *,
        returncode: int | None = 0,
        stdout: str = "",
        stderr: str = "",
        cancelled: bool = False,
        timed_out: bool = False,
        orphaned_descendants: bool = False,
        termination_incomplete: bool = False,
        output_limit_exceeded: bool = False,
    ) -> BoundedProcessResult:
        return BoundedProcessResult(
            returncode,
            stdout,
            stderr,
            cancelled,
            timed_out,
            orphaned_descendants,
            termination_incomplete,
            output_limit_exceeded=output_limit_exceeded,
        )

    def _logs(self) -> list[Path]:
        return sorted(self.log_root.rglob("*.log")) if self.log_root.exists() else []

    def _run(
        self,
        result: BoundedProcessResult,
        *,
        project_id: str | None = "demo",
        operation: str = "refresh",
    ) -> tuple[BoundedProcessResult | None, str]:
        diagnostic = io.StringIO()
        returned: BoundedProcessResult | None = None
        with patch.object(packctl, "run_bounded_process", return_value=result):
            with redirect_stderr(diagnostic):
                returned = packctl.run_packwiz(
                    ["packwiz", "refresh"],
                    cwd=self.cwd,
                    project_id=project_id,
                    operation=operation,
                )
        return returned, diagnostic.getvalue()

    def test_success_without_output_does_not_create_log(self) -> None:
        result = self._result()
        with patch.object(
            packctl, "run_bounded_process", return_value=result
        ) as runner:
            diagnostic_output = io.StringIO()
            with redirect_stderr(diagnostic_output):
                returned = packctl.run_packwiz(
                    ["packwiz", "refresh"],
                    cwd=self.cwd,
                    project_id="demo",
                    operation="refresh",
                )
        self.assertIsNotNone(returned)
        self.assertEqual(
            runner.call_args.kwargs["max_output_bytes"],
            packctl.PACKWIZ_OUTPUT_MAX_BYTES,
        )
        self.assertEqual(self._logs(), [])
        self.assertNotIn("diagnostic", diagnostic_output.getvalue().lower())

    def test_success_with_each_output_shape_creates_log(self) -> None:
        for stdout, stderr in (
            ("stdout only\n", ""),
            ("", "stderr only\n"),
            ("stdout\n", "stderr\n"),
        ):
            with self.subTest(stdout=stdout, stderr=stderr):
                _, diagnostic = self._run(self._result(stdout=stdout, stderr=stderr))
                logs = self._logs()
                self.assertEqual(len(logs), 1)
                contents = logs[0].read_text(encoding="utf-8")
                self.assertIn(stdout.strip(), contents)
                self.assertIn(stderr.strip(), contents)
                self.assertIn("Packwiz completed with diagnostics", diagnostic)
                for log in logs:
                    log.unlink()

    def test_metadata_disagreement_multiline_stderr_is_preserved_when_return_is_ignored(self) -> None:
        stderr = "Metadata disagreement: project metadata differs\nexpected: foo\nactual: bar\n"
        with patch.object(packctl, "run_bounded_process", return_value=self._result(stderr=stderr)):
            packctl.run_packwiz(
                ["packwiz", "refresh"],
                cwd=self.cwd,
                project_id="demo",
                operation="refresh",
            )
        logs = self._logs()
        self.assertEqual(len(logs), 1)
        contents = logs[0].read_text(encoding="utf-8")
        for line in stderr.splitlines():
            self.assertIn(line, contents)

    def test_failure_with_multiline_stderr_preserves_primary_error_and_log_path(self) -> None:
        stderr = "first failure line\nsecond failure line\n"
        with self.assertRaises(packctl.ConfigError) as context:
            self._run(self._result(returncode=2, stderr=stderr))
        message = str(context.exception)
        self.assertIn("Packwiz failed", message)
        self.assertIn("Details:", message)
        self.assertEqual(len(self._logs()), 1)
        self.assertIn("second failure line", self._logs()[0].read_text(encoding="utf-8"))

    def test_failure_with_stdout_only_is_logged(self) -> None:
        with self.assertRaises(packctl.ConfigError):
            self._run(self._result(returncode=1, stdout="failure on stdout\n"))
        self.assertIn("failure on stdout", self._logs()[0].read_text(encoding="utf-8"))

    def test_failure_exception_redacts_sensitive_output(self) -> None:
        result = self._result(
            returncode=1,
            stderr="https://example.invalid/mod.jar?access%5Ftoken=secret-value\n",
        )
        with self.assertRaises(packctl.ConfigError) as context:
            self._run(result)
        self.assertNotIn("secret-value", str(context.exception))
        self.assertIn("<redacted>", str(context.exception))

    def test_process_lifecycle_failures_are_logged_with_metadata(self) -> None:
        cases = (
            {"cancelled": True},
            {"timed_out": True},
            {"termination_incomplete": True},
            {"orphaned_descendants": True},
            {"output_limit_exceeded": True, "stdout": "captured prefix"},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                with self.assertRaises(packctl.ConfigError):
                    self._run(self._result(**fields))
                contents = self._logs()[-1].read_text(encoding="utf-8")
                for key, value in fields.items():
                    if key == "stdout":
                        self.assertIn(value, contents)
                    else:
                        self.assertIn(f"{key}: true", contents)
                if fields.get("output_limit_exceeded"):
                    self.assertIn("output truncated / supported limit exceeded", contents)
                for log in self._logs():
                    log.unlink()

    def test_success_diagnostic_log_failure_is_explicit(self) -> None:
        with patch.object(
            packctl,
            "_write_packwiz_diagnostic_log",
            side_effect=OSError("read-only diagnostics"),
        ):
            _, diagnostic = self._run(self._result(stderr="warning\n"))
        self.assertIn("completed with diagnostics", diagnostic)
        self.assertIn("could not be written", diagnostic)
        self.assertEqual(self._logs(), [])

    def test_log_command_identity_redacts_secret_arguments(self) -> None:
        contents = packctl._packwiz_process_log_text(
            [
                "packwiz",
                "--token",
                "token-value",
                "--api-key=key-value",
                "https://user:password@example.invalid/mod.jar",
                "https://example.invalid/mod.jar?token=query-secret&name=demo",
            ],
            self._result(),
            operation="refresh",
            project="demo",
        )
        self.assertNotIn("token-value", contents)
        self.assertNotIn("key-value", contents)
        self.assertNotIn("password@example.invalid", contents)
        self.assertNotIn("query-secret", contents)
        self.assertIn("<redacted>", contents)

    def test_log_output_redacts_echoed_secrets_and_credential_urls(self) -> None:
        result = self._result(
            stdout="token-value https://user:password@example.invalid/mod.jar\n",
            stderr=(
                "key-value https://example.invalid/mod.jar?access_token=query-secret "
                "https://example.invalid/mod.jar?access%5Ftoken=encoded-secret\n"
            ),
        )
        contents = packctl._packwiz_process_log_text(
            [
                "packwiz",
                "--token",
                "token-value",
                "--api-key=key-value",
                "https://user:password@example.invalid/mod.jar",
                "https://example.invalid/mod.jar?access_token=query-secret",
            ],
            result,
            operation="refresh",
            project="demo",
        )
        self.assertNotIn("token-value", contents)
        self.assertNotIn("key-value", contents)
        self.assertNotIn("user:password@", contents)
        self.assertNotIn("query-secret", contents)
        self.assertNotIn("encoded-secret", contents)
        self.assertIn("<redacted>", contents)

    def test_run_packwiz_console_command_redacts_secret_arguments(self) -> None:
        console = io.StringIO()
        command = ["packwiz", "--token", "token-value", "refresh"]
        with patch.object(
            packctl, "run_bounded_process", return_value=self._result()
        ):
            with redirect_stdout(console):
                packctl.run_packwiz(command, cwd=self.cwd)
        self.assertNotIn("token-value", console.getvalue())
        self.assertIn("<redacted>", console.getvalue())

    def test_failure_diagnostic_log_failure_does_not_replace_primary_error(self) -> None:
        with patch.object(
            packctl,
            "_write_packwiz_diagnostic_log",
            side_effect=OSError("read-only diagnostics"),
        ):
            with self.assertRaises(packctl.ConfigError) as context:
                self._run(self._result(returncode=3, stderr="failed\n"))
        message = str(context.exception)
        self.assertIn("Packwiz failed", message)
        self.assertIn("diagnostic log could not be written", message)

    def test_unsafe_context_components_fall_back_inside_log_root(self) -> None:
        self._run(
            self._result(stderr="warning\n"),
            project_id="../escape",
            operation="../../refresh",
        )
        logs = self._logs()
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0].is_file())
        self.assertIn(self.log_root.resolve(), logs[0].resolve().parents)
        self.assertEqual(logs[0].parent.name, "global")
        self.assertIn("operation: packwiz", logs[0].read_text(encoding="utf-8"))

    def test_log_directories_and_files_have_restricted_modes(self) -> None:
        self._run(self._result(stderr="warning\n"))
        log = self._logs()[0]
        self.assertEqual(stat.S_IMODE(self.log_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_symlinked_log_root_is_rejected_without_success_being_silent(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.log_root.parent.mkdir(parents=True)
        self.log_root.symlink_to(outside, target_is_directory=True)
        _, diagnostic = self._run(self._result(stderr="warning\n"))
        self.assertIn("could not be written", diagnostic)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_project_log_directory_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.log_root.mkdir(parents=True)
        (self.log_root / "demo").symlink_to(outside, target_is_directory=True)
        _, diagnostic = self._run(self._result(stderr="warning\n"))
        self.assertIn("could not be written", diagnostic)
        self.assertEqual(list(outside.iterdir()), [])

    def test_logs_are_classified_as_logs_not_unexpected_active_state(self) -> None:
        self._run(self._result(stderr="warning\n"), project_id="demo")
        items = packctl.classify_state()
        matching = [item for item in items if item.path.suffix == ".log"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].category, "log")
        self.assertFalse(matching[0].active)


if __name__ == "__main__":
    unittest.main()
