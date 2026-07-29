from __future__ import annotations

from pathlib import Path
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


def metadata(
    name: str,
    slug: str,
    version: str,
    *,
    pin: bool = False,
    side: str = "both",
) -> str:
    pinned = "pin = true\n" if pin else ""
    return f'''name = "{name}"
filename = "{slug}-{version}.jar"
side = "{side}"
{pinned}[download]
hash-format = "sha256"
hash = "00"
url = "https://example.invalid/{slug}.jar"
[update.modrinth]
mod-id = "{slug}"
version = "{version}"
'''


class TransactionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.source = self.packs / "demo" / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.source / "pack.toml").write_text('name = "Demo"\n', encoding="utf-8")
        (self.source / "index.toml").write_text(
            'hash-format = "sha256"\n', encoding="utf-8"
        )
        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "TEMPLATES", self.templates),
            patch.object(core, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                core,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(core, "LOG_ROOT", self.root / ".huroshiki" / "logs"),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "TEMPLATES", self.templates),
            patch.object(packctl, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                packctl,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(packctl, "LOG_ROOT", self.root / ".huroshiki" / "logs"),
            patch.object(packctl, "TRASH_ROOT", self.root / ".huroshiki" / "trash"),
            patch.object(
                packctl,
                "DEPLOY_SNAPSHOT_ROOT",
                self.root / ".huroshiki" / "deploy-snapshots",
            ),
            patch.object(
                core,
                "run_resolver_process",
                side_effect=self.run_fake_resolver,
            ),
        ]
        for item in self.patches:
            item.start()
        self.key = core.project_key("pack", "demo")

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def write_mod(self, slug: str, version: str = "v1", *, pin: bool = False) -> Path:
        path = self.source / "mods" / f"{slug}.pw.toml"
        path.write_text(metadata(slug.title(), slug, version, pin=pin), encoding="utf-8")
        return path

    @staticmethod
    def completed(command: list[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode)

    @staticmethod
    def run_fake_resolver(command, *, cwd, cancel_event, deadline):
        try:
            result = core.subprocess.run(command, cwd=cwd, check=False)
        except subprocess.TimeoutExpired:
            return core.ResolverProcessResult(-15, "", "", False, True)
        return core.ResolverProcessResult(
            result.returncode,
            result.stdout or "",
            result.stderr or "",
            False,
            False,
        )


class UpdateTransactionTest(TransactionTestCase):
    def test_transaction_copy_preserves_read_only_root_mode(self) -> None:
        self.source.chmod(0o555)
        transaction = None
        try:
            transaction = core.PackTransaction.create(self.key)
            self.assertEqual(stat.S_IMODE(transaction.source.stat().st_mode), 0o555)
            self.assertTrue((transaction.source / "pack.toml").is_file())
        finally:
            self.source.chmod(0o755)
            if transaction is not None:
                transaction.source.chmod(0o755)
                transaction.discard()

    def test_transaction_copy_preserves_nested_read_only_directory_mode(self) -> None:
        self.write_mod("first")
        self.source.joinpath("mods").chmod(0o555)
        transaction = None
        try:
            transaction = core.PackTransaction.create(self.key)
            staged_mods = transaction.source / "mods"
            self.assertEqual(stat.S_IMODE(staged_mods.stat().st_mode), 0o555)
            self.assertTrue((staged_mods / "first.pw.toml").is_file())
        finally:
            self.source.joinpath("mods").chmod(0o755)
            if transaction is not None:
                transaction.source.joinpath("mods").chmod(0o755)
                transaction.discard()

    def test_aba_source_change_during_copy_cannot_seed_staged_tree(self) -> None:
        target = self.write_mod("first")
        original = target.read_bytes()
        original_copy = core.copy_transaction_source

        def aba_copy(source, destination):
            original_copy(source, destination)
            target.write_text(metadata("First", "first", "temporary"), encoding="utf-8")
            (destination / "mods" / "first.pw.toml").write_text(
                metadata("First", "first", "temporary"), encoding="utf-8"
            )
            target.write_bytes(original)

        with patch.object(core, "copy_transaction_source", side_effect=aba_copy):
            with self.assertRaisesRegex(core.HuroshikiError, "while.*copy"):
                core.PackTransaction.create(self.key)
        self.assertEqual(target.read_bytes(), original)

    def test_source_change_during_transaction_copy_aborts_update_and_persists(self) -> None:
        target = self.write_mod("first")
        original_copy = core.copy_transaction_source

        def racing_copy(source, destination):
            result = original_copy(source, destination)
            target.write_text(metadata("First", "first", "external"), encoding="utf-8")
            return result

        with patch.object(core, "copy_transaction_source", side_effect=racing_copy):
            with self.assertRaisesRegex(core.HuroshikiError, "while.*copy"):
                core.update_all(self.key)
        self.assertIn("external", target.read_text(encoding="utf-8"))

    def test_source_directory_replacement_during_copy_is_not_followed(self) -> None:
        self.write_mod("first")
        mods = self.source / "mods"
        displaced = self.source / "displaced-mods"
        external = self.root / "external"
        external.mkdir()
        secret = external / "secret"
        secret.write_text("keep", encoding="utf-8")
        real_open = os.open
        replaced = False

        def replace_then_open(path, flags, *args, dir_fd=None, **kwargs):
            nonlocal replaced
            if path == "mods" and dir_fd is not None and not replaced:
                replaced = True
                mods.rename(displaced)
                mods.symlink_to(external, target_is_directory=True)
            return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

        with patch.object(
            packctl, "pack_source_fd_entry_issues", return_value=[]
        ), patch.object(core.os, "open", side_effect=replace_then_open):
            with self.assertRaisesRegex(core.HuroshikiError, "changed while opening"):
                core.PackTransaction.create(self.key)

        self.assertEqual(secret.read_text(encoding="utf-8"), "keep")
        self.assertFalse(packctl.project_lock_is_active(self.key))

    def test_no_candidates_reports_current_and_pinned(self) -> None:
        self.write_mod("current")
        self.write_mod("fixed", pin=True)
        (self.source / "mods" / "manual.pw.toml").write_text(
            '''name = "Manual"
filename = "manual.jar"
side = "both"
[download]
hash-format = "sha256"
hash = "00"
url = "https://example.invalid/manual.jar"
''',
            encoding="utf-8",
        )
        transaction = core.PackTransaction.create(self.key)
        with patch.object(
            core.subprocess,
            "run",
            side_effect=lambda command, **_: self.completed(command),
        ):
            candidates = transaction.prepare_updates()
        self.assertEqual(
            [(item.slug, item.status) for item in candidates],
            [
                ("current", "current"),
                ("fixed", "pinned"),
                ("manual", "unavailable"),
            ],
        )
        self.assertFalse(any(item.available for item in candidates))
        transaction.discard()

    def test_full_and_partial_selection_apply_only_selected_metadata(self) -> None:
        first = self.write_mod("first")
        second = self.write_mod("second")

        def run(command, *, cwd, **_):
            if command[:3] == ["packwiz", "--yes", "update"]:
                slug = command[3]
                (cwd / "mods" / f"{slug}.pw.toml").write_text(
                    metadata(slug.title(), slug, "v2"), encoding="utf-8"
                )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()
            self.assertEqual(
                [item.slug for item in candidates if item.available],
                ["first", "second"],
            )
            transaction.select_updates([Path("mods/first.pw.toml")])
            transaction.apply()

        self.assertIn('version = "v2"', first.read_text(encoding="utf-8"))
        self.assertIn('version = "v1"', second.read_text(encoding="utf-8"))

    def test_selected_new_dependency_is_retained_by_final_refresh(self) -> None:
        self.write_mod("first")

        def run(command, *, cwd, **_):
            if command == ["packwiz", "--yes", "update", "first"]:
                (cwd / "mods" / "first.pw.toml").write_text(
                    metadata("First", "first", "v2"), encoding="utf-8"
                )
                (cwd / "mods" / "dependency.pw.toml").write_text(
                    metadata("Dependency", "dependency", "v2"), encoding="utf-8"
                )
                (cwd / "index.toml").write_text("resolver index\n", encoding="utf-8")
                (cwd / "pack.toml").write_text("resolver pack\n", encoding="utf-8")
            elif command == ["packwiz", "refresh"] and cwd == transaction.source:
                self.assertTrue((cwd / "mods" / "dependency.pw.toml").is_file())
                (cwd / "index.toml").write_text(
                    "final index: dependency.pw.toml\n", encoding="utf-8"
                )
                (cwd / "pack.toml").write_text(
                    'name = "Demo"\nindex-hash = "final"\n', encoding="utf-8"
                )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidate = next(
                item for item in transaction.prepare_updates() if item.available
            )
            self.assertEqual(candidate.added_dependencies, 1)
            self.assertEqual(
                {change.relative_path for change in candidate.changes},
                {
                    Path("mods/first.pw.toml"),
                    Path("mods/dependency.pw.toml"),
                    Path("index.toml"),
                    Path("pack.toml"),
                },
            )
            transaction.select_updates([candidate.root])
            transaction.apply()

        self.assertTrue((self.source / "mods" / "dependency.pw.toml").is_file())
        self.assertIn(
            "dependency.pw.toml",
            (self.source / "index.toml").read_text(encoding="utf-8"),
        )
        self.assertIn(
            'index-hash = "final"',
            (self.source / "pack.toml").read_text(encoding="utf-8"),
        )

    def test_unselected_dependency_closure_is_absent(self) -> None:
        self.write_mod("first")
        self.write_mod("second")

        def run(command, *, cwd, **_):
            if command[:3] == ["packwiz", "--yes", "update"]:
                slug = command[3]
                (cwd / "mods" / f"{slug}.pw.toml").write_text(
                    metadata(slug.title(), slug, "v2"), encoding="utf-8"
                )
                (cwd / "mods" / f"{slug}-dep.pw.toml").write_text(
                    metadata(f"{slug.title()} Dep", f"{slug}-dep", "v2"),
                    encoding="utf-8",
                )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()
            self.assertFalse((transaction.source / "mods" / "first-dep.pw.toml").exists())
            self.assertFalse((transaction.source / "mods" / "second-dep.pw.toml").exists())
            first = next(item for item in candidates if item.slug == "first")
            transaction.select_updates([first.root])
            transaction.apply()

        self.assertTrue((self.source / "mods" / "first-dep.pw.toml").is_file())
        self.assertFalse((self.source / "mods" / "second-dep.pw.toml").exists())

    def test_shared_dependency_closures_merge_once(self) -> None:
        self.write_mod("first")
        self.write_mod("second")

        def run(command, *, cwd, **_):
            if command[:3] == ["packwiz", "--yes", "update"]:
                slug = command[3]
                (cwd / "mods" / f"{slug}.pw.toml").write_text(
                    metadata(slug.title(), slug, "v2"), encoding="utf-8"
                )
                (cwd / "mods" / "shared.pw.toml").write_text(
                    metadata("Shared", "shared", "v2"), encoding="utf-8"
                )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()
            transaction.select_updates(
                item.root for item in candidates if item.available
            )
            transaction.apply()

        shared = list(self.source.rglob("shared.pw.toml"))
        self.assertEqual(len(shared), 1)
        self.assertIn('version = "v2"', shared[0].read_text(encoding="utf-8"))

    def test_first_incoming_update_unions_existing_side(self) -> None:
        target = self.source / "mods/first.pw.toml"
        target.write_text(
            metadata("First", "first", "v1", side="client"), encoding="utf-8"
        )

        def run(command, *, cwd, **_):
            if command == ["packwiz", "--yes", "update", "first"]:
                (cwd / "mods/first.pw.toml").write_text(
                    metadata("First", "first", "v2", side="server"),
                    encoding="utf-8",
                )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidate = next(
                item for item in transaction.prepare_updates() if item.available
            )
            transaction.select_updates([candidate.root])
            transaction.apply()

        contents = target.read_text(encoding="utf-8")
        self.assertIn('version = "v2"', contents)
        self.assertIn('side = "both"', contents)

    def test_shared_incoming_updates_union_existing_side_once(self) -> None:
        self.write_mod("first")
        self.write_mod("second")
        shared = self.source / "mods/shared.pw.toml"
        shared.write_text(
            metadata("Shared", "shared", "v1", side="client"), encoding="utf-8"
        )

        def run(command, *, cwd, **_):
            if command[:3] == ["packwiz", "--yes", "update"]:
                slug = command[3]
                (cwd / "mods" / f"{slug}.pw.toml").write_text(
                    metadata(slug.title(), slug, "v2"), encoding="utf-8"
                )
                (cwd / "mods/shared.pw.toml").write_text(
                    metadata("Shared", "shared", "v2", side="server"),
                    encoding="utf-8",
                )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()
            transaction.select_updates(
                item.root for item in candidates if item.available
            )
            transaction.apply()

        contents = shared.read_text(encoding="utf-8")
        self.assertIn('version = "v2"', contents)
        self.assertIn('side = "both"', contents)

    def test_conflicting_dependency_versions_abort_before_real_source_apply(self) -> None:
        first = self.write_mod("first")
        second = self.write_mod("second")
        originals = (first.read_bytes(), second.read_bytes())

        def run(command, *, cwd, **_):
            if command[:3] == ["packwiz", "--yes", "update"]:
                slug = command[3]
                (cwd / "mods" / f"{slug}.pw.toml").write_text(
                    metadata(slug.title(), slug, "v2"), encoding="utf-8"
                )
                (cwd / "mods" / "shared.pw.toml").write_text(
                    metadata("Shared", "shared", f"from-{slug}"), encoding="utf-8"
                )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()
            with self.assertRaisesRegex(core.HuroshikiError, "metadata disagreement"):
                transaction.select_updates(
                    item.root for item in candidates if item.available
                )

        self.assertEqual(first.read_bytes(), originals[0])
        self.assertEqual(second.read_bytes(), originals[1])
        self.assertFalse((self.source / "mods" / "shared.pw.toml").exists())
        transaction.discard()

    def test_removed_dependency_is_grouped_with_updated_root(self) -> None:
        self.write_mod("first")
        dependency = self.write_mod("dependency")

        def run(command, *, cwd, **_):
            if command == ["packwiz", "--yes", "update", "first"]:
                (cwd / "mods" / "first.pw.toml").write_text(
                    metadata("First", "first", "v2"), encoding="utf-8"
                )
                (cwd / "mods" / "dependency.pw.toml").unlink()
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()
            first = next(item for item in candidates if item.slug == "first")
            removed = next(
                change
                for change in first.changes
                if change.relative_path == Path("mods/dependency.pw.toml")
            )
            self.assertIsNotNone(removed.before)
            self.assertIsNone(removed.after)
            transaction.select_updates([first.root])
            transaction.apply()

        self.assertFalse(dependency.exists())

    def test_update_all_applies_every_candidate(self) -> None:
        first = self.write_mod("first")
        second = self.write_mod("second")

        def run(command, *, cwd, **_):
            if command[:3] == ["packwiz", "--yes", "update"]:
                slug = command[3]
                (cwd / "mods" / f"{slug}.pw.toml").write_text(
                    metadata(slug.title(), slug, "v2"), encoding="utf-8"
                )
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run):
            report = core.update_all(self.key)
        self.assertTrue(report.applied)
        self.assertFalse(report.partial)
        self.assertEqual(len(report.selected), 2)
        self.assertIn('version = "v2"', first.read_text(encoding="utf-8"))
        self.assertIn('version = "v2"', second.read_text(encoding="utf-8"))

    def test_update_all_reports_no_available_updates_without_applying(self) -> None:
        target = self.write_mod("current")
        original = target.read_bytes()

        with patch.object(
            core.subprocess,
            "run",
            side_effect=lambda command, **_: self.completed(command),
        ):
            report = core.update_all(self.key)

        self.assertFalse(report.applied)
        self.assertEqual(report.selected, ())
        self.assertEqual(report.failures, ())
        self.assertEqual(target.read_bytes(), original)

    def test_update_failure_leaves_real_source_unchanged(self) -> None:
        target = self.write_mod("first")
        original = target.read_bytes()

        def run(command, *, cwd, **_):
            (cwd / "mods" / "first.pw.toml").write_text(
                metadata("First", "first", "broken"), encoding="utf-8"
            )
            return self.completed(command, 7)

        with patch.object(core.subprocess, "run", side_effect=run):
            report = core.update_all(self.key)
        self.assertFalse(report.applied)
        self.assertEqual(report.failures[0].error_returncode, 7)
        self.assertEqual(target.read_bytes(), original)

    def test_orphaned_update_resolver_is_unavailable_and_unlocks(self) -> None:
        target = self.write_mod("first")
        original = target.read_bytes()
        results = iter(
            (
                core.ResolverProcessResult(0, "", "", False, False),
                core.ResolverProcessResult(
                    0, "", "", False, False, orphaned_descendants=True
                ),
            )
        )
        with patch.object(
            core, "run_resolver_process", side_effect=lambda *_, **__: next(results)
        ):
            report = core.update_all(self.key)

        self.assertFalse(report.applied)
        self.assertEqual(len(report.failures), 1)
        self.assertIn("left background processes", report.failures[0].error or "")
        self.assertEqual(target.read_bytes(), original)
        with packctl.ProjectLock(self.key, "verify orphan cleanup"):
            pass

    def test_update_all_fails_closed_unless_partial_is_explicit(self) -> None:
        first = self.write_mod("first")
        second = self.write_mod("second")
        first_original = first.read_bytes()
        second_original = second.read_bytes()

        def run(command, *, cwd, **_):
            if command[:3] == ["packwiz", "--yes", "update"]:
                slug = command[3]
                if slug == "second":
                    return subprocess.CompletedProcess(command, 9, "", "network failed")
                (cwd / "mods" / "first.pw.toml").write_text(
                    metadata("First", "first", "v2"), encoding="utf-8"
                )
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run):
            report = core.update_all(self.key)
        self.assertFalse(report.applied)
        self.assertFalse(report.partial)
        self.assertEqual(report.selected, ())
        self.assertEqual(report.failures[0].error_returncode, 9)
        self.assertEqual(first.read_bytes(), first_original)
        self.assertEqual(second.read_bytes(), second_original)
        with packctl.ProjectLock(self.key, "verify release"):
            pass

        with patch.object(core.subprocess, "run", side_effect=run):
            report = core.update_all(self.key, allow_partial=True)
        self.assertTrue(report.applied)
        self.assertTrue(report.partial)
        self.assertEqual([candidate.slug for candidate in report.selected], ["first"])
        self.assertIn('version = "v2"', first.read_text(encoding="utf-8"))
        self.assertEqual(second.read_bytes(), second_original)

    def test_resolver_failure_is_unavailable_and_transaction_stays_clean(self) -> None:
        target = self.write_mod("first")
        original = target.read_bytes()

        def run(command, *, cwd, **_):
            if command[:3] == ["packwiz", "--yes", "update"]:
                (cwd / "mods" / "first.pw.toml").write_text(
                    metadata("First", "first", "broken"), encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 9, "", "network failed")
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].status, "unavailable")
        self.assertEqual(candidates[0].error, "network failed")
        self.assertEqual(
            (transaction.source / "mods" / "first.pw.toml").read_bytes(),
            original,
        )
        self.assertEqual(target.read_bytes(), original)
        transaction.discard()

    def test_duplicate_slugs_in_different_paths_are_all_unavailable(self) -> None:
        first = self.write_mod("shared")
        nested = self.source / "optional" / "shared.pw.toml"
        nested.parent.mkdir()
        nested.write_text(metadata("Other", "other", "v1"), encoding="utf-8")

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run") as run:
            candidates = transaction.prepare_updates()

        self.assertEqual([item.status for item in candidates], ["unavailable", "unavailable"])
        for candidate in candidates:
            self.assertIn("ambiguous Packwiz update slug 'shared'", candidate.error or "")
            self.assertIn(str(first.relative_to(self.source)), candidate.error or "")
            self.assertIn(str(nested.relative_to(self.source)), candidate.error or "")
        run.assert_not_called()
        transaction.discard()

    def test_disposable_normalization_indexes_untracked_metadata_before_update(self) -> None:
        self.write_mod("first")
        (self.source / "index.toml").write_text("stale index\n", encoding="utf-8")
        original_index = (self.source / "index.toml").read_bytes()
        normalization_sources: list[Path] = []

        def run(command, *, cwd, **_):
            if command == ["packwiz", "refresh"]:
                normalization_sources.append(cwd)
                (cwd / "index.toml").write_text(
                    "indexed mods/first.pw.toml\n", encoding="utf-8"
                )
            elif command == ["packwiz", "--yes", "update", "first"]:
                self.assertIn("mods/first.pw.toml", (cwd / "index.toml").read_text())
                (cwd / "mods/first.pw.toml").write_text(
                    metadata("First", "first", "v2"), encoding="utf-8"
                )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()

        self.assertTrue(candidates[0].available)
        self.assertEqual(len(normalization_sources), 1)
        self.assertNotEqual(normalization_sources[0], self.source)
        self.assertNotEqual(normalization_sources[0], transaction.source)
        self.assertEqual((self.source / "index.toml").read_bytes(), original_index)
        self.assertEqual((transaction.source / "index.toml").read_bytes(), original_index)
        transaction.discard()

    def test_normalization_failure_is_actionable_and_preserves_statuses_and_source(self) -> None:
        current = self.write_mod("current")
        self.write_mod("fixed", pin=True)
        original = current.read_bytes()

        def run(command, *, cwd, **_):
            self.assertEqual(command, ["packwiz", "refresh"])
            (cwd / "index.toml").write_text("partial\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 8, "", "bad index")

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()

        self.assertEqual(
            [(item.slug, item.status) for item in candidates],
            [("current", "unavailable"), ("fixed", "pinned")],
        )
        self.assertIn("disposable baseline normalization failed: bad index", candidates[0].error or "")
        self.assertEqual(candidates[0].error_returncode, 8)
        self.assertEqual(current.read_bytes(), original)
        self.assertEqual((transaction.source / "index.toml").read_text(), 'hash-format = "sha256"\n')
        transaction.discard()

    def test_discard_cancels_staged_update(self) -> None:
        target = self.write_mod("first")
        original = target.read_bytes()

        def run(command, *, cwd, **_):
            (cwd / "mods" / "first.pw.toml").write_text(
                metadata("First", "first", "v2"), encoding="utf-8"
            )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            transaction.prepare_updates()
        transaction.discard()
        self.assertEqual(target.read_bytes(), original)

    def test_external_source_change_aborts_apply(self) -> None:
        target = self.write_mod("first")

        def run(command, *, cwd, **_):
            if command == ["packwiz", "--yes", "update", "first"]:
                (cwd / "mods" / "first.pw.toml").write_text(
                    metadata("First", "first", "v2"), encoding="utf-8"
                )
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            candidates = transaction.prepare_updates()
            target.write_text(metadata("First", "first", "external"), encoding="utf-8")
            transaction.select_updates(
                item.relative_path for item in candidates if item.available
            )
            with self.assertRaisesRegex(core.HuroshikiError, "real Packwiz source changed"):
                transaction.apply()
        self.assertIn("external", target.read_text(encoding="utf-8"))
        transaction.discard()

    def test_external_write_immediately_before_source_rename_is_restored_exactly(self) -> None:
        target = self.write_mod("first")
        transaction = core.PackTransaction.create(self.key)
        staged = transaction.source / "mods" / "first.pw.toml"
        staged.write_text(metadata("First", "first", "v2"), encoding="utf-8")
        real_rename = Path.rename

        def write_then_rename(path: Path, destination: Path):
            if path == self.source:
                target.write_text(
                    metadata("First", "first", "external"), encoding="utf-8"
                )
            return real_rename(path, destination)

        with patch.object(core.subprocess, "run", side_effect=lambda command, **_: self.completed(command)), patch.object(
            Path, "rename", write_then_rename
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "real Packwiz source changed"):
                transaction.apply()

        self.assertIn("external", target.read_text(encoding="utf-8"))
        self.assertFalse(list(self.source.parent.glob(".source.huroshiki-backup-*")))
        transaction.discard()

    def test_interrupt_after_successful_initial_rename_restores_exact_source(self) -> None:
        target = self.write_mod("first")
        original = target.read_bytes()
        transaction = core.PackTransaction.create(self.key)
        (transaction.source / "mods/first.pw.toml").write_text(
            metadata("First", "first", "v2"), encoding="utf-8"
        )
        real_rename = Path.rename

        def rename_then_interrupt(path: Path, destination: Path):
            result = real_rename(path, destination)
            if path == self.source:
                raise KeyboardInterrupt
            return result

        with patch.object(
            core.subprocess,
            "run",
            side_effect=lambda command, **_: self.completed(command),
        ), patch.object(Path, "rename", rename_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                transaction.apply()

        self.assertEqual(target.read_bytes(), original)
        self.assertFalse((transaction.root / "replaced-source").exists())
        transaction.discard()

    def test_failure_after_successful_staged_rename_restores_exact_source(self) -> None:
        for failure in (KeyboardInterrupt(), RuntimeError("after rename")):
            with self.subTest(failure=type(failure).__name__):
                target = self.write_mod("first")
                original = {
                    path.relative_to(self.source): path.read_bytes()
                    for path in self.source.rglob("*")
                    if path.is_file()
                }
                transaction = core.PackTransaction.create(self.key)
                (transaction.source / "mods/first.pw.toml").write_text(
                    metadata("First", "first", "v2"), encoding="utf-8"
                )
                real_rename = Path.rename

                def rename_then_fail(path: Path, destination: Path):
                    result = real_rename(path, destination)
                    if path == transaction.source:
                        raise failure
                    return result

                try:
                    with patch.object(
                        core.subprocess,
                        "run",
                        side_effect=lambda command, **_: self.completed(command),
                    ), patch.object(Path, "rename", rename_then_fail):
                        with self.assertRaises(type(failure)):
                            transaction.apply()

                    restored = {
                        path.relative_to(self.source): path.read_bytes()
                        for path in self.source.rglob("*")
                        if path.is_file()
                    }
                    self.assertEqual(restored, original)
                    self.assertNotIn(
                        'version = "v2"', target.read_text(encoding="utf-8")
                    )
                    self.assertTrue(
                        (transaction.root / "failed-staged-source").is_dir()
                    )
                finally:
                    transaction.discard()
                self.assertFalse(packctl.project_lock_is_active(self.key))

    def test_external_recreation_after_staged_rename_is_preserved(self) -> None:
        self.write_mod("first")
        transaction = core.PackTransaction.create(self.key)
        original = {
            path.relative_to(self.source): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }
        real_rename = Path.rename

        def install_then_recreate(path: Path, destination: Path):
            result = real_rename(path, destination)
            if path == transaction.source:
                real_rename(destination, transaction.source)
                destination.mkdir()
                (destination / "external.txt").write_text("keep", encoding="utf-8")
                raise RuntimeError("external recreation")
            return result

        try:
            with patch.object(
                core.subprocess,
                "run",
                side_effect=lambda command, **_: self.completed(command),
            ), patch.object(Path, "rename", install_then_recreate):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "recreated externally"
                ):
                    transaction.apply()

            self.assertEqual(
                (self.source / "external.txt").read_text(encoding="utf-8"), "keep"
            )
            backup = transaction.root / "replaced-source"
            retained = {
                path.relative_to(backup): path.read_bytes()
                for path in backup.rglob("*")
                if path.is_file()
            }
            self.assertEqual(retained, original)
        finally:
            transaction.discard()
        self.assertFalse(packctl.project_lock_is_active(self.key))

    def test_rollback_restores_replacement_moved_after_inode_precheck(self) -> None:
        self.write_mod("first")
        transaction = core.PackTransaction.create(self.key)
        original = {
            path.relative_to(self.source): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }
        failed_staged = transaction.root / "failed-staged-source"
        displaced_staged = transaction.root / "displaced-installed-staged"
        real_rename = Path.rename
        installed = False
        replaced_during_rollback = False

        def replace_between_check_and_rename(path: Path, destination: Path):
            nonlocal installed, replaced_during_rollback
            if path == transaction.source and destination == self.source and not installed:
                installed = True
                real_rename(path, destination)
                raise RuntimeError("force publication rollback")
            if (
                path == self.source
                and destination == failed_staged
                and not replaced_during_rollback
            ):
                replaced_during_rollback = True
                real_rename(path, displaced_staged)
                path.mkdir()
                (path / "external.txt").write_text("keep", encoding="utf-8")
            return real_rename(path, destination)

        try:
            with patch.object(
                core.subprocess,
                "run",
                side_effect=lambda command, **_: self.completed(command),
            ), patch.object(Path, "rename", replace_between_check_and_rename):
                with self.assertRaisesRegex(
                    core.HuroshikiError, "replaced during rollback"
                ):
                    transaction.apply()

            self.assertEqual(
                (self.source / "external.txt").read_text(encoding="utf-8"), "keep"
            )
            backup = transaction.root / "replaced-source"
            retained = {
                path.relative_to(backup): path.read_bytes()
                for path in backup.rglob("*")
                if path.is_file()
            }
            self.assertEqual(retained, original)
            self.assertTrue(displaced_staged.is_dir())
            self.assertFalse(failed_staged.exists())
        finally:
            transaction.discard()
        self.assertFalse(packctl.project_lock_is_active(self.key))

    def test_late_old_inode_write_is_retained_until_completed_state_cleanup(self) -> None:
        target = self.write_mod("first")
        transaction = core.PackTransaction.create(self.key)
        staged = transaction.source / "mods" / "first.pw.toml"
        staged.write_text(metadata("First", "first", "v2"), encoding="utf-8")
        replaced = transaction.root / "replaced-source"
        held_fd = os.open(target, os.O_WRONLY | os.O_APPEND)
        original_snapshot = core.tree_digest_snapshot
        wrote_late = False

        def snapshot_then_write(path: Path):
            nonlocal wrote_late
            snapshot = original_snapshot(path)
            if path == replaced and not wrote_late:
                wrote_late = True
                os.write(held_fd, b"\nlate old-fd write\n")
            return snapshot

        try:
            with patch.object(
                core.subprocess,
                "run",
                side_effect=lambda command, **_: self.completed(command),
            ), patch.object(core, "tree_digest_snapshot", side_effect=snapshot_then_write):
                transaction.apply()
        finally:
            os.close(held_fd)

        self.assertFalse(transaction.active)
        self.assertTrue((transaction.root / ".completed").is_file())
        self.assertIn(
            "late old-fd write",
            (replaced / "mods" / "first.pw.toml").read_text(encoding="utf-8"),
        )
        self.assertIn('version = "v2"', target.read_text(encoding="utf-8"))
        self.assertFalse(packctl.project_lock_is_active(self.key))
        self.assertFalse(list(self.source.parent.glob(".source.huroshiki-backup-*")))

        now = transaction.root.stat().st_mtime + 1
        preview = packctl.clean_state(older_than_days=0, now=now)
        retained = next(item for item in preview.selected if item.path == transaction.root)
        self.assertEqual(retained.category, "completed_transaction")
        self.assertGreaterEqual(retained.bytes, len(b"late old-fd write"))

        report = packctl.clean_state(
            apply=True,
            older_than_days=0,
            now=now,
            expected=preview.selected,
        )
        self.assertEqual(report.removed_count, 1)
        self.assertGreaterEqual(report.removed_bytes, retained.bytes)
        self.assertFalse(transaction.root.exists())

    def test_recreated_source_is_preserved_when_staged_install_fails(self) -> None:
        self.write_mod("first")
        transaction = core.PackTransaction.create(self.key)
        real_rename = Path.rename

        def recreate_before_install(path: Path, destination: Path):
            if path == transaction.source:
                self.source.mkdir()
                (self.source / "external.txt").write_text("keep", encoding="utf-8")
                raise FileExistsError("recreated")
            return real_rename(path, destination)

        with patch.object(core.subprocess, "run", side_effect=lambda command, **_: self.completed(command)), patch.object(
            Path, "rename", recreate_before_install
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "recreated externally"):
                transaction.apply()

        self.assertEqual((self.source / "external.txt").read_text(encoding="utf-8"), "keep")
        replaced = transaction.root / "replaced-source"
        self.assertTrue(replaced.is_dir())
        self.assertFalse(list(self.source.parent.glob(".source.huroshiki-backup-*")))
        transaction.discard()
        self.assertTrue(replaced.is_dir())
        self.assertTrue((transaction.root / ".completed").is_file())


class RemoveTransactionTest(TransactionTestCase):
    def test_source_change_during_transaction_copy_aborts_remove_and_persists(self) -> None:
        target = self.write_mod("first")
        original_copy = core.copy_transaction_source

        def racing_copy(source, destination):
            result = original_copy(source, destination)
            target.write_text(metadata("First", "first", "external"), encoding="utf-8")
            return result

        with patch.object(core, "copy_transaction_source", side_effect=racing_copy):
            with self.assertRaisesRegex(core.HuroshikiError, "while.*copy"):
                core.remove_installed_mods(self.key, ["first"])
        self.assertIn("external", target.read_text(encoding="utf-8"))

    def test_batch_remove_failure_leaves_every_real_mod(self) -> None:
        first = self.write_mod("first")
        second = self.write_mod("second")

        def run(command, *, cwd, **_):
            if command[:2] == ["packwiz", "remove"]:
                slug = command[2]
                if slug == "second":
                    return self.completed(command, 9)
                (cwd / "mods" / f"{slug}.pw.toml").unlink()
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run):
            result = core.remove_installed_mods(self.key, ["first", "second"])
        self.assertEqual(result, 9)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_external_source_change_aborts_batch_remove(self) -> None:
        first = self.write_mod("first")
        second = self.write_mod("second")

        def run(command, *, cwd, **_):
            if command[:2] == ["packwiz", "remove"]:
                (cwd / "mods" / f"{command[2]}.pw.toml").unlink()
            return self.completed(command)

        transaction = core.PackTransaction.create(self.key)
        with patch.object(core.subprocess, "run", side_effect=run):
            self.assertEqual(transaction.remove_mods(["first", "second"]), 0)
            second.write_text(
                metadata("Second", "second", "external"), encoding="utf-8"
            )
            with self.assertRaisesRegex(core.HuroshikiError, "real Packwiz source changed"):
                transaction.apply()
        self.assertTrue(first.exists())
        self.assertIn("external", second.read_text(encoding="utf-8"))
        transaction.discard()

    def test_template_manifest_failure_is_atomic(self) -> None:
        template_root = self.templates / "base"
        template_root.mkdir(parents=True)
        manifest = template_root / "template.yaml"
        manifest.write_text(
            '''id: base
display_name: Base
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.0
mods:
  - name: First
    provider: modrinth
    project_id: first
    side: both
  - name: Second
    provider: curseforge
    project_id: second
    side: client
''',
            encoding="utf-8",
        )
        original = manifest.read_bytes()
        key = core.project_key("template", "base")
        with patch.object(
            packctl,
            "save_template_mods_raw",
            side_effect=packctl.ConfigError("write failed"),
        ):
            with self.assertRaisesRegex(packctl.ConfigError, "write failed"):
                core.remove_installed_mods(
                    key,
                    ["modrinth-first", "curseforge-second"],
                )
        self.assertEqual(manifest.read_bytes(), original)

    def test_template_config_change_during_resolver_setup_aborts_and_persists(self) -> None:
        template_root = self.templates / "base"
        template_root.mkdir(parents=True)
        manifest = template_root / "template.yaml"
        manifest.write_text(
            "id: base\ndisplay_name: Base\nenabled: true\n"
            "minecraft: 1.21.1\nloader: neoforge\n"
            "reference_loader_version: 21.1.0\nmods: []\n",
            encoding="utf-8",
        )
        local = template_root / "template.local.yaml"
        original_setup = core.create_resolver_source

        def racing_setup(*args, **kwargs):
            result = original_setup(*args, **kwargs)
            local.write_text("url_max_jar_size_bytes: 1024\n", encoding="utf-8")
            return result

        with patch.object(core, "create_resolver_source", side_effect=racing_setup):
            with self.assertRaisesRegex(core.HuroshikiError, "while.*resolver"):
                core.remove_installed_mods("template:base", ["anything"])
        self.assertEqual(local.read_text(encoding="utf-8"), "url_max_jar_size_bytes: 1024\n")


if __name__ == "__main__":
    unittest.main()
