from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

from packwiz_parser import visible_menu_items
import packwiz_pty
from packwiz_pty import PackwizPtySession
import process_runner


@unittest.skipUnless(os.name == "posix", "PTY integration requires POSIX")
class PackwizPtySessionTest(unittest.TestCase):
    def make_script(self, root: Path, body: str) -> Path:
        binary = root / "packwiz"
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        binary.write_text(
            textwrap.dedent(body).replace("#!/usr/bin/env bash", f"#!{bash}"),
            encoding="utf-8",
        )
        binary.chmod(0o755)
        return binary

    def test_menu_selection_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "packwiz"
            bash = shutil.which("bash")
            self.assertIsNotNone(bash)
            binary.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    printf 'Searching Modrinth...\\r\\n'
                    printf '0) Cancel\\r\\n1) *Create\\r\\n2) Create Deco\\r\\n'
                    printf 'Choose a number:'
                    IFS= read -r selection
                    [[ "$selection" == 1 ]]
                    printf '\\r\\nWould you like to add them? (Y/n)'
                    IFS= read -r answer
                    [[ "$answer" =~ ^[Yy]?$ ]]
                    printf '\\r\\nProject successfully added!\\r\\n'
                    """
                ).replace("#!/usr/bin/env bash", f"#!{bash}"),
                encoding="utf-8",
            )
            binary.chmod(0o755)

            holder: dict[str, PackwizPtySession] = {}
            event_kinds: list[str] = []

            def callback(event) -> None:
                event_kinds.append(event.kind)
                if event.kind == "search_results":
                    item = visible_menu_items(event.items)[0]
                    holder["session"].send_line(str(item.index))
                elif event.kind == "confirmation":
                    holder["session"].send_line("y")

            session = PackwizPtySession(
                [str(binary)],
                cwd=root,
                log_dir=root / "logs",
                on_event=callback,
            )
            holder["session"] = session
            result = session.run()

            self.assertEqual(result.returncode, 0)
            self.assertEqual(event_kinds.count("search_results"), 1)
            self.assertEqual(event_kinds.count("confirmation"), 1)
            self.assertTrue(result.raw_log.is_file())
            self.assertTrue(result.event_log.is_file())
            self.assertIn("Project successfully added!", result.normalized_text)

    def test_prestart_cancel_does_not_spawn_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = PackwizPtySession(
                ["packwiz"],
                cwd=root,
                log_dir=root / "logs",
            )
            self.assertIsNone(session.cancel(deadline=time.monotonic() + 1))
            with patch.object(packwiz_pty.subprocess, "Popen") as popen:
                result = session.run()

            popen.assert_not_called()
            self.assertTrue(result.cancelled)
            self.assertEqual(result.returncode, -signal.SIGINT)
            self.assertTrue(result.raw_log.is_file())

    def test_cancel_escalates_and_drains_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = self.make_script(
                root,
                """\
                #!/usr/bin/env bash
                trap '' INT TERM
                sleep 30 &
                wait
                """,
            )
            session = PackwizPtySession(
                [str(binary)],
                cwd=root,
                log_dir=root / "logs",
            )
            holder: dict[str, object] = {}

            def run() -> None:
                holder["result"] = session.run()

            worker = threading.Thread(target=run, daemon=False)
            worker.start()
            deadline = time.monotonic() + 2
            while session.process is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(session.process)
            with (
                patch.object(packwiz_pty, "PTY_INTERRUPT_GRACE_SECONDS", 0.02),
                patch.object(process_runner, "PROCESS_TERMINATE_GRACE_SECONDS", 0.02),
                patch.object(process_runner, "PROCESS_KILL_GRACE_SECONDS", 0.2),
                patch.object(process_runner, "PROCESS_REAP_GRACE_SECONDS", 0.2),
            ):
                termination = session.cancel(deadline=time.monotonic() + 1)
            self.assertIsNotNone(termination)
            assert termination is not None
            self.assertTrue(termination.group_drained)
            self.assertTrue(termination.parent_reaped)
            self.assertTrue(termination.forced)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            result = holder["result"]
            self.assertTrue(result.cancelled)
            self.assertFalse(result.termination_incomplete)

    def test_cancel_reports_group_drain_and_parent_reap_failure(self) -> None:
        class _Process:
            pid = 12345

            @staticmethod
            def poll():
                return None

        session = PackwizPtySession(
            ["packwiz"],
            cwd=Path("/tmp"),
            log_dir=Path("/tmp/logs"),
        )
        session.process = _Process()
        incomplete = process_runner.ProcessTerminationResult(False, False, True)
        member = process_runner.ProcessGroupMember(12345, "S")
        with (
            patch.object(packwiz_pty.os, "killpg"),
            patch.object(packwiz_pty, "live_process_group_members", return_value=(member,)),
            patch.object(packwiz_pty, "stop_process_group", return_value=incomplete),
            patch.object(packwiz_pty, "PTY_INTERRUPT_GRACE_SECONDS", 0),
        ):
            result = session.cancel(deadline=time.monotonic() + 0.1)

        self.assertEqual(result, incomplete)


if __name__ == "__main__":
    unittest.main()
