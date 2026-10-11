from __future__ import annotations

import errno
import os
import select
import signal
import sys
import time
import unittest
from collections.abc import Callable


# This is a terminal-semantics test, not an interpreter cold-start benchmark.
# Give exec/import/mount a separate bounded startup budget; retain the original
# five-second interaction budget from RAW readiness until actual process exit.
STARTUP_TIMEOUT_SECONDS = 30.0
INTERACTION_TIMEOUT_SECONDS = 5.0


def run_pty_script(
    script: str,
    *,
    startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
    interaction_timeout: float = INTERACTION_TIMEOUT_SECONDS,
    on_spawn: Callable[[int], None] | None = None,
) -> tuple[bytes, int]:
    environment = os.environ.copy()
    environment.pop("TEXTUAL_ALLOW_SIGNALS", None)
    pid, master = os.forkpty()
    if pid == 0:
        try:
            os.execve(sys.executable, [sys.executable, "-c", script], environment)
        except BaseException:
            os.write(2, b"PTY child exec failed\n")
            os._exit(127)
    output = bytearray()
    status: int | None = None
    eof = False

    def pump(deadline: float) -> None:
        nonlocal status, eof
        ready, _, _ = select.select(
            [master] if not eof else [], [], [],
            min(0.05, max(0.0, deadline - time.monotonic())),
        )
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b""  # Linux PTY EOF.
            if not data:
                eof = True
            output.extend(data)
            if len(output) > 1024 * 1024:
                raise AssertionError("PTY output exceeded the test capture bound")
        if status is None:
            waited, candidate = os.waitpid(pid, os.WNOHANG)
            # (0, 0) means STILL RUNNING, not a successful exit status.
            if waited == pid:
                status = candidate

    def diagnostic(phase: str) -> str:
        return (
            f"PTY {phase}; reaped={status is not None}, eof={eof}; "
            f"output={output.decode(errors='replace')}"
        )

    try:
        if on_spawn is not None:
            on_spawn(pid)
        startup_deadline = time.monotonic() + startup_timeout
        while b"RAW:1" not in output:
            if time.monotonic() >= startup_deadline:
                raise TimeoutError(diagnostic("startup readiness timed out"))
            pump(startup_deadline)
            if status is not None and eof and b"RAW:1" not in output:
                raise AssertionError(diagnostic("child exited before RAW readiness"))
        if status is not None:
            raise AssertionError(diagnostic("child exited before control input"))
        os.write(master, b"\x13\x03")
        interaction_deadline = time.monotonic() + interaction_timeout
        # Drain the final markers AND reap the child. Marker text alone is not
        # proof of clean termination, and waitpid can race buffered PTY output.
        while status is None or not eof:
            if time.monotonic() >= interaction_deadline:
                raise TimeoutError(diagnostic("interaction/exit timed out"))
            pump(interaction_deadline)
        return bytes(output), os.waitstatus_to_exitcode(status)
    finally:
        if status is None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(master)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "forkpty"), "requires a POSIX PTY")
class TextualTermiosTest(unittest.TestCase):
    def test_raw_ctrl_keys_and_termios_restoration(self) -> None:
        script = r'''
import os
import sys
import termios
os.write(1, b"STARTED\n")
from textual.app import App
from textual.binding import Binding
os.write(1, b"IMPORTED\n")

configured = termios.tcgetattr(0)
configured[3] |= termios.ISIG
configured[0] |= termios.IXON | termios.IXOFF
termios.tcsetattr(0, termios.TCSANOW, configured)
expected_iflag = configured[0]
expected_lflag = configured[3]

class ControlApp(App[None]):
    BINDINGS = [
        Binding("ctrl+c", "control_c", priority=True),
        Binding("ctrl+s", "control_s", priority=True),
    ]

    def on_mount(self):
        active = termios.tcgetattr(0)
        cleared = not (active[0] & (termios.IXON | termios.IXOFF)) and not (active[3] & termios.ISIG)
        os.write(1, f"RAW:{int(cleared)}\n".encode())
        self.received = []

    def action_control_c(self):
        self.received.append("c")
        self.finish_if_ready()

    def action_control_s(self):
        self.received.append("s")
        self.finish_if_ready()

    def finish_if_ready(self):
        if len(self.received) == 2:
            os.write(1, ("KEYS:" + "".join(self.received) + "\n").encode())
            self.exit()

try:
    ControlApp().run()
finally:
    restored = termios.tcgetattr(0)
    ok = restored[0] == expected_iflag and restored[3] == expected_lflag
    os.write(1, f"RESTORED:{int(ok)}\n".encode())
'''

        try:
            output, exit_code = run_pty_script(script)
        except OSError as error:
            if error.errno in {errno.ENOSYS, errno.ENODEV, errno.ENOENT}:
                self.skipTest(f"controlling PTY unavailable: {error}")
            raise
        text = output.decode(errors="replace")
        self.assertIn("RAW:1", text)
        self.assertIn("KEYS:sc", text)
        self.assertIn("RESTORED:1", text)
        self.assertEqual(exit_code, 0, text)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "forkpty"), "requires a POSIX PTY")
class PtyHarnessLifecycleTest(unittest.TestCase):
    def assert_reaped(self, pid: int) -> None:
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_success_markers_do_not_replace_process_exit(self) -> None:
        created: list[int] = []
        script = (
            r"import os,tty,signal,time; tty.setraw(0); "
            r"signal.signal(signal.SIGHUP,signal.SIG_IGN); "
            r"os.write(1,b'RAW:1\nKEYS:sc\nRESTORED:1\n'); time.sleep(60)"
        )
        with self.assertRaisesRegex(TimeoutError, "interaction/exit"):
            run_pty_script(script, interaction_timeout=0.1, on_spawn=created.append)
        self.assertEqual(len(created), 1)
        self.assert_reaped(created[0])

    def test_startup_timeout_always_reaps_child(self) -> None:
        created: list[int] = []
        script = r"import os,time; os.write(1,b'STARTED\n'); time.sleep(60)"
        with self.assertRaisesRegex(TimeoutError, "startup readiness"):
            run_pty_script(script, startup_timeout=0.1, on_spawn=created.append)
        self.assertEqual(len(created), 1)
        self.assert_reaped(created[0])

    def test_early_child_exit_reports_startup_failure(self) -> None:
        created: list[int] = []
        with self.assertRaisesRegex(AssertionError, "exited before RAW readiness"):
            run_pty_script("import os; os._exit(23)", on_spawn=created.append)
        self.assertEqual(len(created), 1)
        self.assert_reaped(created[0])

    def test_nonzero_exit_is_not_coerced_to_success(self) -> None:
        created: list[int] = []
        script = (
            r"import os,tty; tty.setraw(0); os.write(1,b'RAW:1\n'); "
            r"os.read(0,2); os.write(1,b'KEYS:sc\nRESTORED:1\n'); os._exit(7)"
        )
        output, exit_code = run_pty_script(script, on_spawn=created.append)
        self.assertIn(b"RESTORED:1", output)
        self.assertEqual(exit_code, 7)
        self.assert_reaped(created[0])


if __name__ == "__main__":
    unittest.main()
