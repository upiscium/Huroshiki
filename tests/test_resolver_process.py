from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import huroshiki_core as core


@unittest.skipUnless(os.name == "posix", "resolver process groups require POSIX")
class ResolverProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_python(
        self,
        source: str,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> core.ResolverProcessResult:
        return core.run_resolver_process(
            [sys.executable, "-c", source],
            cwd=self.cwd,
            cancel_event=cancel_event,
            deadline=deadline,
        )

    def test_normal_and_nonzero_results_preserve_output(self) -> None:
        success = self.run_python("print('success')")
        self.assertEqual(success.returncode, 0)
        self.assertEqual(success.stdout, "success\n")
        self.assertFalse(success.cancelled)
        self.assertFalse(success.timed_out)

        result = self.run_python(
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)"
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "out\n")
        self.assertEqual(result.stderr, "err\n")
        self.assertFalse(result.cancelled)
        self.assertFalse(result.timed_out)

    def test_prestart_cancel_does_not_spawn(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with patch.object(core.subprocess, "Popen") as popen:
            result = self.run_python("raise SystemExit(0)", cancel_event=cancel)
        popen.assert_not_called()
        self.assertTrue(result.cancelled)

    def test_running_cancel_terminates_process_group(self) -> None:
        cancel = threading.Event()
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            result = self.run_python("import time; time.sleep(60)", cancel_event=cancel)
        finally:
            timer.cancel()
        self.assertTrue(result.cancelled)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(result.returncode, -signal.SIGTERM)

    def test_sigterm_ignoring_process_is_killed_after_grace(self) -> None:
        cancel = threading.Event()
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        try:
            with patch.object(core, "RESOLVER_TERMINATE_GRACE_SECONDS", 0.15):
                result = self.run_python(
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "time.sleep(60)",
                    cancel_event=cancel,
                )
        finally:
            timer.cancel()
        self.assertTrue(result.cancelled)
        self.assertEqual(result.returncode, -signal.SIGKILL)

    def test_deadline_terminates_running_process(self) -> None:
        result = self.run_python(
            "import time; time.sleep(60)",
            deadline=time.monotonic() + 0.15,
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.returncode, -signal.SIGTERM)

    def test_keyboard_interrupt_stops_process_before_reraising(self) -> None:
        real_popen = subprocess.Popen
        real_sleep = time.sleep
        process_holder: list[subprocess.Popen[bytes]] = []
        sleep_calls = 0

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            process_holder.append(process)
            return process

        def interrupt_once(seconds: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                raise KeyboardInterrupt
            real_sleep(seconds)

        with patch.object(core.subprocess, "Popen", side_effect=launch), patch.object(
            core.time, "sleep", side_effect=interrupt_once
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.run_python("import time; time.sleep(60)")
        self.assertEqual(len(process_holder), 1)
        self.assertIsNotNone(process_holder[0].poll())

    def test_cancel_stops_child_process_in_same_group(self) -> None:
        child_pid_file = self.cwd / "child.pid"
        cancel = threading.Event()
        result_holder: list[core.ResolverProcessResult] = []
        source = (
            "import pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
            "time.sleep(60)"
        )
        worker = threading.Thread(
            target=lambda: result_holder.append(
                self.run_python(source, cancel_event=cancel)
            )
        )
        worker.start()
        deadline = time.monotonic() + 3
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_pid_file.exists())
        child_pid = int(child_pid_file.read_text())
        cancel.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result_holder[0].cancelled)

        state_path = Path(f"/proc/{child_pid}/stat")
        deadline = time.monotonic() + 2
        while state_path.exists() and time.monotonic() < deadline:
            fields = state_path.read_text().split()
            if len(fields) > 2 and fields[2] == "Z":
                break
            time.sleep(0.02)
        if state_path.exists():
            self.assertEqual(state_path.read_text().split()[2], "Z")


if __name__ == "__main__":
    unittest.main()
