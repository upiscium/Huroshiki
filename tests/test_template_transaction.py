from __future__ import annotations

import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl
from packwiz_parser import visible_menu_items


@unittest.skipUnless(os.name == "posix", "PTY integration requires POSIX")
class TemplateTransactionTest(unittest.TestCase):
    def test_template_packwiz_transaction_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            template_root = templates / "base"
            template_root.mkdir(parents=True)
            (template_root / "template.yaml").write_text(
                '''id: base
display_name: Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods: []
''',
                encoding="utf-8",
            )

            binary_dir = root / "bin"
            binary_dir.mkdir()
            binary = binary_dir / "packwiz"
            binary.write_text(
                textwrap.dedent(
                    r"""
                    #!/usr/bin/env bash
                    set -euo pipefail
                    printf 'Searching Modrinth...\r\n'
                    printf '0) Cancel\r\n1) *Example MOD\r\n'
                    printf 'Choose a number:'
                    IFS= read -r selection
                    [[ "$selection" == 1 ]]
                    mkdir -p mods
                    cat > mods/example.pw.toml <<'EOF'
                    name = "Example MOD"
                    filename = "example.jar"
                    side = "both"
                    [download]
                    hash-format = "sha256"
                    hash = "00"
                    url = "https://example.invalid/example.jar"
                    [update.modrinth]
                    mod-id = "example-id"
                    version = "version-id"
                    EOF
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            binary.chmod(0o755)

            environment = os.environ.copy()
            environment["PATH"] = f"{binary_dir}:{environment['PATH']}"
            key = core.project_key("template", "base")
            patches = [
                patch.object(core, "ROOT", root),
                patch.object(core, "TEMPLATES", templates),
                patch.object(core, "STATE_ROOT", root / ".huroshiki"),
                patch.object(
                    core,
                    "TRANSACTION_ROOT",
                    root / ".huroshiki" / "transactions",
                ),
                patch.object(core, "LOG_ROOT", root / ".huroshiki" / "logs"),
                patch.object(packctl, "ROOT", root),
                patch.object(packctl, "TEMPLATES", templates),
                patch.dict(os.environ, environment, clear=True),
            ]
            for item in patches:
                item.start()
            try:
                transaction = core.PackTransaction.create(key)
                holder = {}

                def callback(event) -> None:
                    if event.kind == "search_results":
                        result = visible_menu_items(event.items)[0]
                        holder["operation"].send_selection(result.index)

                operation = transaction.begin_add(
                    "modrinth",
                    "example",
                    client=True,
                    server=False,
                    on_event=callback,
                )
                holder["operation"] = operation
                result = operation.run()
                self.assertTrue(result.success)
                transaction.apply()
                mods = core.list_mods(key)
                self.assertEqual(len(mods), 1)
                self.assertEqual(mods[0].project_id, "example-id")
                self.assertTrue(mods[0].client)
                self.assertFalse(mods[0].server)
                self.assertFalse((template_root / "source").exists())
            finally:
                for item in reversed(patches):
                    item.stop()


if __name__ == "__main__":
    unittest.main()
