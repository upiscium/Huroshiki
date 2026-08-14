from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, patch

import huroshiki
import huroshiki_core as core
from textual.app import App


def mod_info(project_id: str = "A1b2C3d4") -> core.ModInfo:
    return core.ModInfo(
        Path("mods/example.pw.toml"),
        "example",
        "Example MOD",
        "modrinth",
        project_id,
        "example.jar",
        True,
        True,
    )


def preview() -> core.ModVersionSelectionPreview:
    return core.ModVersionSelectionPreview(
        "modrinth:A1b2C3d4",
        Path("mods/example.pw.toml"),
        "Example MOD",
        "modrinth",
        "1.0",
        "OldVer01",
        "2.0",
        "E5f6G7h8",
        (core.UpdateChange(Path("mods/example.pw.toml"), b"old", b"new"),),
        1,
        1,
        ("modrinth:Added001",),
        ("modrinth:Removed1",),
        "modrinth:A1b2C3d4",
        "E5f6G7h8",
        False,
        ("Packwiz completed with diagnostics. Details: .huroshiki/logs/demo/x.log",),
    )


class _StagedVersionApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, transaction: MagicMock) -> None:
        super().__init__()
        self.transaction = transaction
        self.transactions = {"pack:demo": transaction}
        self.opened_install = threading.Event()

    def on_mount(self) -> None:
        target = core.StagedExactModTarget(
            mod_info(), "dependency", ("modrinth:Root0001",)
        )
        self.push_screen(
            huroshiki.StagedExactModVersionScreen("pack:demo", target)
        )

    def open_install(self, project_key: str) -> None:
        if project_key == "pack:demo":
            self.opened_install.set()


class _InstallReviewApp(App[None]):
    CSS_PATH = str(Path(huroshiki.__file__).with_name("huroshiki.tcss"))

    def __init__(self, transaction: MagicMock) -> None:
        super().__init__()
        self.transactions = {"pack:demo": transaction}

    def on_mount(self) -> None:
        self.push_screen(huroshiki.InstallScreen("pack:demo"))


class BorrowedStagedVersionTuiTest(unittest.IsolatedAsyncioTestCase):
    async def test_accept_keeps_borrowed_transaction_for_final_add_apply(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.operation_active = False
        transaction.exact_selection_prepared = True
        transaction.prepare_exact_mod_version.return_value = preview()
        worker_threads: list[int] = []

        def prepare(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            return preview()

        transaction.prepare_exact_mod_version.side_effect = prepare
        app = _StagedVersionApp(transaction)
        caller = threading.get_ident()
        with patch.object(core.PackTransaction, "create") as create:
            async with app.run_test() as pilot:
                artifact = app.screen.query_one(
                    "#staged-version-artifact", huroshiki.Input
                )
                artifact.value = "E5f6G7h8"
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertIn(
                    "Accept version change",
                    str(
                        app.screen.query_one(
                            "#staged-version-status", huroshiki.Static
                        ).render()
                    ),
                )
                self.assertIn(
                    ".huroshiki/logs/demo/x.log",
                    str(
                        app.screen.query_one(
                            "#staged-version-status", huroshiki.Static
                        ).render()
                    ),
                )
                app.screen.accept_version_change()
                self.assertTrue(app.opened_install.is_set())
        create.assert_not_called()
        transaction.apply.assert_not_called()
        transaction.discard.assert_not_called()
        transaction.rollback_exact_mod_version.assert_not_called()
        self.assertIs(app.transactions["pack:demo"], transaction)
        self.assertNotEqual(worker_threads[0], caller)

    async def test_cancel_preview_rolls_back_without_discarding_add(self) -> None:
        transaction = MagicMock()
        transaction.active = True
        transaction.operation_active = False
        transaction.exact_selection_prepared = True
        transaction.prepare_exact_mod_version.return_value = preview()
        app = _StagedVersionApp(transaction)
        async with app.run_test() as pilot:
            artifact = app.screen.query_one(
                "#staged-version-artifact", huroshiki.Input
            )
            artifact.value = "E5f6G7h8"
            await pilot.press("enter")
            await pilot.pause(0.2)
            app.screen.cancel_and_navigate()
            await pilot.pause(0.2)
            self.assertTrue(app.opened_install.is_set())
        transaction.rollback_exact_mod_version.assert_called_once_with()
        transaction.apply.assert_not_called()
        transaction.discard.assert_not_called()
        self.assertIs(app.transactions["pack:demo"], transaction)

    def test_target_exposes_dependency_reachability(self) -> None:
        target = core.StagedExactModTarget(
            mod_info(), "dependency", ("modrinth:Root0001", "modrinth:Root0002")
        )
        self.assertEqual(target.role, "dependency")
        self.assertEqual(
            target.required_by,
            ("modrinth:Root0001", "modrinth:Root0002"),
        )

    async def test_final_review_reads_current_changes_and_removed_baseline_mods(self) -> None:
        current = mod_info()
        removed = core.ModInfo(
            Path("mods/removed.pw.toml"),
            "removed",
            "Removed Dependency",
            "modrinth",
            "Remov001",
            "removed.jar",
            True,
            True,
        )
        transaction = MagicMock()
        transaction.active = True
        transaction.exact_selection_prepared = True
        transaction.staged_mods.return_value = [current]
        transaction.staged_removed_mods.return_value = [removed]
        transaction.staged_exact_mod_targets.return_value = [
            core.StagedExactModTarget(current, "root", ())
        ]
        with patch.object(core, "project_config", return_value={}):
            app = _InstallReviewApp(transaction)
            async with app.run_test() as pilot:
                install = app.screen
                install.review()
                await pilot.pause()
                modal = app.screen
                self.assertIsInstance(modal, huroshiki.ConfirmModal)
                self.assertEqual(modal.dialog_title, "Apply changes")
                self.assertTrue(any("Example MOD" in line for line in modal.lines))
                self.assertTrue(
                    any("Removed: Removed Dependency" in line for line in modal.lines)
                )


if __name__ == "__main__":
    unittest.main()
