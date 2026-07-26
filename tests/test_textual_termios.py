from __future__ import annotations

import errno
import os
from pathlib import Path
import select
import signal
import sys
import termios
import time
import unittest


@unittest.skipUnless(os.name == "posix" and hasattr(os, "forkpty"), "requires a POSIX PTY")
class TextualTermiosTest(unittest.TestCase):
    def test_raw_ctrl_keys_and_termios_restoration(self) -> None:
        script = r'''
import os
import sys
import termios
from textual.app import App
from textual.binding import Binding

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
            pid, master = os.forkpty()
        except OSError as error:
            self.skipTest(f"controlling PTY unavailable: {error}")
        if pid == 0:
            environment = os.environ.copy()
            environment.pop("TEXTUAL_ALLOW_SIGNALS", None)
            os.execve(sys.executable, [sys.executable, "-c", script], environment)

        output = bytearray()
        status: int | None = None
        deadline = time.monotonic() + 5
        try:
            while b"RAW:1" not in output and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError as error:
                        if error.errno != errno.EIO:  # Linux reports PTY EOF as EIO.
                            raise
                        break
            if b"RAW:1" not in output:
                self.fail(
                    "Textual did not enter expected raw mode on the test PTY: "
                    + output.decode(errors="replace")
                )

            os.write(master, b"\x13\x03")
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    break
            self.assertIsNotNone(status, output.decode(errors="replace"))
        finally:
            if status is None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
            os.close(master)

        text = output.decode(errors="replace")
        self.assertIn("KEYS:sc", text)
        self.assertIn("RESTORED:1", text)
        self.assertTrue(os.waitstatus_to_exitcode(status or 0) == 0, text)


if __name__ == "__main__":
    unittest.main()
