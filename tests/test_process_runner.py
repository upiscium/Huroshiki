from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import process_runner as runner


class TrackingFile(io.BytesIO):
    def __init__(self, initial_bytes: bytes):
        super().__init__(initial_bytes)
        self.read_calls = 0
        self.seek_calls = 0

    def read(self, *args):  # type: ignore[override]
        self.read_calls += 1
        return super().read(*args)

    def seek(self, *args):  # type: ignore[override]
        self.seek_calls += 1
        return super().seek(*args)


class ProcessRunnerInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_popen_receives_stdin_file_directly(self) -> None:
        stdin = TrackingFile(b"json-payload")
        with patch.object(runner.subprocess, "Popen") as popen, patch.object(
            runner, "live_process_group_members", return_value=()
        ):
            process = Mock()
            process.pid = 123
            process.poll.return_value = 0
            process.returncode = 0
            popen.return_value = process
            result = runner.run_bounded_process(
                [sys.executable, "-c", "print('unused')"],
                cwd=self.cwd,
                stdin_file=stdin,
            )

        popen.assert_called_once()
        self.assertIs(popen.call_args.kwargs.get("stdin"), stdin)
        self.assertIsNot(popen.call_args.kwargs.get("stdin"), subprocess.PIPE)
        self.assertEqual(stdin.read_calls, 0)
        self.assertEqual(stdin.seek_calls, 0)
        self.assertTrue(result.returncode == 0)

    def test_prelaunch_cancel_does_not_consume_stdin_file(self) -> None:
        cancel = threading.Event()
        cancel.set()
        stdin = TrackingFile(b"json-payload")
        with patch.object(runner.subprocess, "Popen") as popen:
            result = runner.run_bounded_process(
                [sys.executable, "-c", "pass"],
                cwd=self.cwd,
                cancel_event=cancel,
                stdin_file=stdin,
            )

        popen.assert_not_called()
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)
        self.assertEqual(stdin.read_calls, 0)
        self.assertEqual(stdin.seek_calls, 0)

    @unittest.skipUnless(os.name == "posix", "resolver behavior relies on POSIX process checks")
    def test_prelaunch_expired_deadline_does_not_consume_stdin_file(self) -> None:
        stdin = TrackingFile(b"json-payload")
        with patch.object(runner.subprocess, "Popen") as popen:
            result = runner.run_bounded_process(
                [sys.executable, "-c", "pass"],
                cwd=self.cwd,
                deadline=time.monotonic(),
                stdin_file=stdin,
            )

        popen.assert_not_called()
        self.assertTrue(result.timed_out)
        self.assertEqual(stdin.read_calls, 0)
        self.assertEqual(stdin.seek_calls, 0)


if __name__ == "__main__":
    unittest.main()
