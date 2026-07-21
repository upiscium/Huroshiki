from __future__ import annotations

import os
from pathlib import Path
import tempfile
import textwrap
import unittest

from packwiz_parser import visible_menu_items
from packwiz_pty import PackwizPtySession


@unittest.skipUnless(os.name == "posix", "PTY integration requires POSIX")
class PackwizPtySessionTest(unittest.TestCase):
    def test_menu_selection_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "packwiz"
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
                ),
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


if __name__ == "__main__":
    unittest.main()
