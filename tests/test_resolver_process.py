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
from unittest.mock import Mock, patch

import huroshiki_core as core
import process_runner as runner


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

    @staticmethod
    def assert_not_live(pid: int) -> None:
        stat_path = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 2
        while stat_path.exists() and time.monotonic() < deadline:
            fields = stat_path.read_text().split()
            if len(fields) > 2 and fields[2] in {"Z", "X", "x"}:
                return
            time.sleep(0.02)
        if stat_path.exists():
            fields = stat_path.read_text().split()
            if len(fields) > 2:
                raise AssertionError(f"process {pid} remains live in state {fields[2]}")

    def test_parent_success_with_live_child_is_stopped_and_reported(self) -> None:
        child_pid_file = self.cwd / "post-exit-child.pid"
        result = self.run_python(
            "import pathlib,subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))"
        )
        child_pid = int(child_pid_file.read_text())
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.orphaned_descendants)
        self.assert_not_live(child_pid)

    def test_parent_and_child_normal_exit_has_no_orphan(self) -> None:
        result = self.run_python(
            "import subprocess,sys; "
            "subprocess.run([sys.executable,'-c','raise SystemExit(0)'], check=True)"
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.orphaned_descendants)

    def test_post_exit_child_ignoring_sigterm_is_killed(self) -> None:
        child_pid_file = self.cwd / "stubborn-child.pid"
        source = (
            "import pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(60)']); "
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
            "time.sleep(0.2)"
        )
        with patch.object(runner, "PROCESS_TERMINATE_GRACE_SECONDS", 0.15):
            result = self.run_python(source)
        child_pid = int(child_pid_file.read_text())
        self.assertTrue(result.orphaned_descendants)
        self.assert_not_live(child_pid)

    def test_zombie_only_group_member_is_not_live(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(0)"],
            start_new_session=True,
        )
        deadline = time.monotonic() + 2
        stat_path = Path(f"/proc/{process.pid}/stat")
        while time.monotonic() < deadline:
            if stat_path.exists() and stat_path.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        self.assertEqual(core.live_process_group_members(process.pid), ())
        process.wait()

    def test_parent_exit_races_have_single_consistent_outcome(self) -> None:
        source = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "time.sleep(0.1)"
        )
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        try:
            cancelled = self.run_python(source, cancel_event=cancel)
        finally:
            timer.cancel()
        self.assertNotEqual(cancelled.cancelled, cancelled.orphaned_descendants)

        deadline_result = self.run_python(
            source, deadline=time.monotonic() + 0.1
        )
        self.assertNotEqual(
            deadline_result.timed_out, deadline_result.orphaned_descendants
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
        with patch.object(runner.subprocess, "Popen") as popen:
            result = self.run_python("raise SystemExit(0)", cancel_event=cancel)
        popen.assert_not_called()
        self.assertTrue(result.cancelled)

    def test_prestart_expired_deadline_does_not_spawn(self) -> None:
        with patch.object(runner.subprocess, "Popen") as popen:
            result = self.run_python("raise SystemExit(0)", deadline=0.0)
        popen.assert_not_called()
        self.assertTrue(result.timed_out)

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
            with patch.object(runner, "PROCESS_TERMINATE_GRACE_SECONDS", 0.15):
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

    def test_incomplete_termination_is_reported_without_unbounded_wait(self) -> None:
        cancel = threading.Event()
        process = Mock(pid=12345, returncode=None)

        def poll() -> None:
            cancel.set()
            return None

        process.poll.side_effect = poll
        cleanup = core.ResolverTerminationResult(
            group_drained=False,
            parent_reaped=False,
            forced=True,
        )
        with patch.object(runner.subprocess, "Popen", return_value=process), patch.object(
            runner, "stop_process_group", return_value=cleanup
        ) as stop:
            result = self.run_python("raise SystemExit(0)", cancel_event=cancel)

        self.assertTrue(result.cancelled)
        self.assertTrue(result.termination_incomplete)
        self.assertIsNone(result.returncode)
        stop.assert_called_once()
        process.wait.assert_not_called()

    def test_stop_process_group_reports_unreaped_live_parent(self) -> None:
        parent = Mock()
        parent.poll.return_value = None
        parent.wait.side_effect = subprocess.TimeoutExpired("resolver", 0)
        with patch.object(
            runner, "live_process_group_members", return_value=(12345,)
        ), patch.object(runner.os, "killpg") as killpg, patch.object(
            runner, "PROCESS_TERMINATE_GRACE_SECONDS", 0.0
        ), patch.object(runner, "PROCESS_KILL_GRACE_SECONDS", 0.0):
            result = core.stop_resolver_process_group(
                12345,
                parent=parent,
                cleanup_deadline=time.monotonic(),
            )

        self.assertFalse(result.group_drained)
        self.assertFalse(result.parent_reaped)
        self.assertTrue(result.forced)
        self.assertEqual(killpg.call_count, 2)
        parent.wait.assert_called_once()

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

        with patch.object(runner.subprocess, "Popen", side_effect=launch), patch.object(
            runner.time, "sleep", side_effect=interrupt_once
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.run_python("import time; time.sleep(60)")
        self.assertEqual(len(process_holder), 1)
        self.assertIsNotNone(process_holder[0].poll())

    def test_exception_observer_receives_incomplete_cleanup_handles(self) -> None:
        process = Mock(pid=12345, returncode=None)
        process.poll.side_effect = KeyboardInterrupt
        cleanup = runner.ProcessTerminationResult(False, False, True)
        observed: list[runner.BoundedProcessResult] = []
        with patch.object(runner.subprocess, "Popen", return_value=process), patch.object(
            runner, "stop_process_group", return_value=cleanup
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.run_bounded_process(
                    [sys.executable, "-c", "pass"],
                    cwd=self.cwd,
                    result_callback=observed.append,
                )

        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].termination_incomplete)
        self.assertEqual(observed[0].process_group, 12345)
        self.assertIs(observed[0].parent_process, process)

    def test_exception_observer_runs_when_cleanup_itself_is_interrupted(self) -> None:
        process = Mock(pid=12346, returncode=None)
        process.poll.side_effect = RuntimeError("poll failed")
        observed: list[runner.BoundedProcessResult] = []
        with patch.object(runner.subprocess, "Popen", return_value=process), patch.object(
            runner, "stop_process_group", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.run_bounded_process(
                    [sys.executable, "-c", "pass"],
                    cwd=self.cwd,
                    result_callback=observed.append,
                )

        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].termination_incomplete)
        self.assertEqual(observed[0].process_group, 12346)
        self.assertIs(observed[0].parent_process, process)

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

    def test_legacy_core_api_reexports_shared_runner(self) -> None:
        self.assertIs(core.ResolverProcessResult, runner.BoundedProcessResult)
        self.assertIs(core.ResolverTerminationResult, runner.ProcessTerminationResult)
        self.assertIs(core.run_resolver_process, runner.run_bounded_process)
        self.assertIs(core.stop_resolver_process_group, runner.stop_process_group)

    def test_failure_message_prioritizes_cleanup_integrity_and_stderr(self) -> None:
        result = runner.BoundedProcessResult(
            7,
            "stdout detail\n",
            "stderr first\nstderr last\n",
            True,
            True,
            True,
            True,
        )
        self.assertEqual(
            runner.process_failure_message(result, label="Packwiz"),
            "Packwiz process termination was incomplete",
        )
        self.assertEqual(
            runner.process_failure_message(
                runner.BoundedProcessResult(
                    7,
                    "stdout detail\n",
                    "stderr first\nstderr last\n",
                    False,
                    False,
                ),
                label="Packwiz",
            ),
            "Packwiz failed: stderr last",
        )


if __name__ == "__main__":
    unittest.main()
