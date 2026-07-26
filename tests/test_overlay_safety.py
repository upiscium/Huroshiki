from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import DataTable

import huroshiki
import huroshiki_core as core
from overlay_policy import scan_content_overlays
import packctl


class OverlayPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.content = self.root / "content"
        for target in ("common", "client", "server"):
            (self.content / target).mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rejects_root_and_nested_packwiz_owned_paths(self) -> None:
        reserved = (
            Path("common/pack.toml"),
            Path("client/nested/index.toml"),
            Path("server/deep/mod.PW.pw.toml"),
        )
        for relative in reserved:
            path = self.content / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("reserved", encoding="utf-8")
        (self.content / "common" / "PACK.TOML").write_text("ordinary", encoding="utf-8")

        issues = scan_content_overlays(self.content).issues

        self.assertEqual(
            {issue.relative_path for issue in issues},
            set(reserved),
        )

    def test_reports_file_directory_dangling_internal_and_external_links(self) -> None:
        external = self.root / "external"
        external.mkdir()
        external_file = external / "secret.txt"
        external_file.write_text("secret", encoding="utf-8")
        internal_file = self.content / "common" / "ordinary.txt"
        internal_file.write_text("ordinary", encoding="utf-8")
        internal_dir = self.content / "common" / "directory"
        internal_dir.mkdir()
        links = {
            Path("common/file-link"): external_file,
            Path("common/directory-link"): external,
            Path("client/dangling-link"): self.root / "missing",
            Path("client/internal-file-link"): internal_file,
            Path("server/internal-directory-link"): internal_dir,
        }
        for relative, target in links.items():
            (self.content / relative).symlink_to(target, target_is_directory=target.is_dir())

        scan = scan_content_overlays(self.content)

        symlinks = {entry.relative_path: entry for entry in scan.entries if entry.kind == "symlink"}
        self.assertEqual(set(symlinks), set(links))
        for relative, target in links.items():
            self.assertEqual(symlinks[relative].link_target, str(target))
            self.assertTrue(
                any(
                    issue.relative_path == relative and f"-> {target}" in issue.message
                    for issue in scan.issues
                )
            )


class OverlayCoreSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.pack = self.packs / "demo"
        for target in ("common", "client", "server"):
            (self.pack / "content" / target).mkdir(parents=True)
        self.patch = patch.object(packctl, "PACKS", self.packs)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp.cleanup()

    def test_listing_exposes_symlink_without_reading_target(self) -> None:
        secret = self.root / "secret.bin"
        secret.write_bytes(b"\xffprivate")
        link = self.pack / "content" / "common" / "secret-link"
        link.symlink_to(secret)

        files = core.list_templates("pack:demo")

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].relative_path, Path("secret-link"))
        self.assertEqual(files[0].size, 0)
        self.assertEqual(files[0].error, f"symlink is not allowed -> {secret}")

    def test_listing_exposes_symlink_overlay_target(self) -> None:
        external = self.root / "external"
        external.mkdir()
        (self.pack / "content" / "client").rmdir()
        (self.pack / "content" / "client").symlink_to(
            external, target_is_directory=True
        )

        files = core.list_templates("pack:demo")

        invalid = next(item for item in files if item.target == "client")
        self.assertEqual(invalid.relative_path, Path("."))
        self.assertEqual(
            invalid.error,
            f"symlink is not allowed -> {external}",
        )

    def test_listing_exposes_symlink_content_root(self) -> None:
        external = self.root / "external"
        external.mkdir()
        for target in ("common", "client", "server"):
            (self.pack / "content" / target).rmdir()
        (self.pack / "content").rmdir()
        (self.pack / "content").symlink_to(external, target_is_directory=True)

        files = core.list_templates("pack:demo")

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].target, "content")
        self.assertEqual(files[0].relative_path, Path("."))
        self.assertEqual(files[0].error, f"symlink is not allowed -> {external}")

    def test_create_and_editor_reject_symlink_ancestor_without_external_write(self) -> None:
        external = self.root / "external"
        external.mkdir()
        secret = external / "secret.txt"
        secret.write_text("unchanged", encoding="utf-8")
        ancestor = self.pack / "content" / "common" / "linked"
        ancestor.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(core.HuroshikiError, "Symlink.*linked"):
            core.create_template("pack:demo", "common", "linked/new.txt")
        with self.assertRaisesRegex(core.HuroshikiError, "Symlink.*linked"):
            core.read_template_text("pack:demo", "common", "linked/secret.txt")
        with self.assertRaisesRegex(core.HuroshikiError, "Symlink.*linked"):
            core.write_template_text(
                "pack:demo", "common", "linked/secret.txt", "exfiltrated"
            )

        self.assertEqual(secret.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse((external / "new.txt").exists())

    def test_create_rejects_packwiz_owned_name_before_mutation(self) -> None:
        with self.assertRaisesRegex(core.HuroshikiError, "Packwiz-owned"):
            core.create_template("pack:demo", "common", "nested/index.toml")

        self.assertFalse((self.pack / "content" / "common" / "nested").exists())


class _OverlayListApp(App[None]):
    def on_mount(self) -> None:
        self.push_screen(huroshiki.TemplateScreen("pack:demo"))


class OverlayTuiTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_symlink_has_explicit_status_and_cannot_open_editor(self) -> None:
        project = core.ProjectInfo(
            "pack", "demo", "Demo", "1.21.1", "neoforge", "21.1", True
        )
        invalid = core.TemplateInfo(
            "common",
            Path("secret-link"),
            Path("/content/common/secret-link"),
            0,
            "symlink is not allowed -> /private/secret",
        )
        with (
            patch.object(huroshiki.core, "project_info", return_value=project),
            patch.object(huroshiki.core, "list_templates", return_value=[invalid]),
            patch.object(huroshiki.core, "read_template_text") as read_text,
        ):
            app = _OverlayListApp()
            async with app.run_test() as pilot:
                table = app.screen.query_one("#template-table", DataTable)
                self.assertEqual(table.get_row_at(0)[3], invalid.error)
                await pilot.press("enter")
                await pilot.pause()

                self.assertIsInstance(app.screen, huroshiki.TemplateScreen)
                read_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
