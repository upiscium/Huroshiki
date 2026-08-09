from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

import packctl


class RsyncProcessTest(unittest.TestCase):
    def result(self, **overrides) -> packctl.BoundedProcessResult:
        values = dict(
            returncode=0,
            stdout="",
            stderr="",
            cancelled=False,
            timed_out=False,
            orphaned_descendants=False,
            termination_incomplete=False,
        )
        values.update(overrides)
        return packctl.BoundedProcessResult(**values)

    def test_runner_passes_cancel_and_clipped_absolute_deadline(self) -> None:
        cancel = threading.Event()
        requested_deadline = time.monotonic() + packctl.RSYNC_PROCESS_TIMEOUT_SECONDS * 2
        with patch.object(
            packctl,
            "run_bounded_process",
            return_value=self.result(),
        ) as runner:
            result = packctl.run_rsync_process(
                ["rsync", "--version"],
                cwd=Path.cwd(),
                cancel_event=cancel,
                deadline=requested_deadline,
                phase="rsync-preview",
            )

        self.assertTrue(result.succeeded)
        self.assertIs(runner.call_args.kwargs["cancel_event"], cancel)
        effective_deadline = runner.call_args.kwargs["deadline"]
        self.assertLess(effective_deadline, requested_deadline)
        self.assertGreater(effective_deadline, time.monotonic() - 1)

    def test_lifecycle_errors_have_fixed_priority(self) -> None:
        cases = (
            (
                dict(
                    returncode=1,
                    orphaned_descendants=True,
                    termination_incomplete=True,
                ),
                "Rsync process cleanup did not complete",
            ),
            (
                dict(returncode=1, orphaned_descendants=True),
                "Rsync left descendant processes running",
            ),
            (dict(returncode=1, cancelled=True), "Rsync operation was cancelled"),
            (
                dict(returncode=1, timed_out=True),
                "Rsync operation exceeded its deadline",
            ),
            (
                dict(output_limit_exceeded=True),
                "Rsync transfer output exceeded the supported limit",
            ),
            (dict(returncode=17), "Rsync exited with status 17"),
        )
        for result_values, message in cases:
            with self.subTest(message=message), patch.object(
                packctl,
                "run_bounded_process",
                return_value=self.result(**result_values),
            ):
                with self.assertRaisesRegex(packctl.ConfigError, message):
                    packctl.run_rsync_process(
                        ["rsync", "--version"],
                        cwd=Path.cwd(),
                        phase="rsync-transfer",
                    )

    def test_diagnostic_is_bounded_and_marks_truncation(self) -> None:
        diagnostic = packctl.bounded_diagnostic("x" * 100, limit=16)

        self.assertLessEqual(len(diagnostic), 100 + 60)
        self.assertTrue(diagnostic.startswith("x" * 16))
        self.assertIn("diagnostic truncated", diagnostic)

    def test_stderr_heavy_process_completes_without_pipe_deadlock(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('x' * 200000)",
        ]

        result = packctl.run_rsync_process(
            command,
            cwd=Path.cwd(),
            phase="rsync-transfer",
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(len(result.stderr), 200000)

    def test_output_limit_stops_capture_and_fails_closed(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 200000)",
        ]

        with patch.object(packctl, "RSYNC_OUTPUT_MAX_BYTES", 32):
            with self.assertRaisesRegex(
                packctl.ConfigError,
                "Rsync transfer output exceeded the supported limit",
            ):
                packctl.run_rsync_process(
                    command,
                    cwd=Path.cwd(),
                    phase="rsync-transfer",
                )

    def test_deadline_terminates_a_hung_process(self) -> None:
        command = [sys.executable, "-c", "import time; time.sleep(30)"]

        started = time.monotonic()
        with self.assertRaisesRegex(
            packctl.ConfigError, "Rsync operation exceeded its deadline"
        ):
            packctl.run_rsync_process(
                command,
                cwd=Path.cwd(),
                deadline=time.monotonic() + 0.15,
                phase="rsync-transfer",
            )

        self.assertLess(time.monotonic() - started, 5)

    def test_cancellation_terminates_a_running_process(self) -> None:
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        cancel = threading.Event()
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                packctl.ConfigError, "Rsync operation was cancelled"
            ):
                packctl.run_rsync_process(
                    command,
                    cwd=Path.cwd(),
                    cancel_event=cancel,
                    phase="rsync-transfer",
                )
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 5)

    def test_descendant_after_parent_exit_is_not_generic_success(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"
            ),
        ]

        with self.assertRaisesRegex(
            packctl.ConfigError, "Rsync left descendant processes running"
        ):
            packctl.run_rsync_process(
                command,
                cwd=Path.cwd(),
                phase="rsync-transfer",
            )


if __name__ == "__main__":
    unittest.main()
