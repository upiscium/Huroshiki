from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


def metadata(name: str, project_id: str, side: str = "both") -> str:
    return f'''name = "{name}"
filename = "{project_id}.jar"
side = "{side}"
[download]
hash-format = "sha256"
hash = "00"
url = "https://example.invalid/{project_id}.jar"
[update.modrinth]
mod-id = "{project_id}"
version = "v1"
'''


class AddTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.source = self.packs / "demo" / "source"
        (self.source / "mods").mkdir(parents=True)
        (self.source / "pack.toml").write_text(
            'name = "Demo"\n[versions]\nminecraft = "1.21.1"\n'
            'neoforge = "21.1.234"\n',
            encoding="utf-8",
        )
        (self.source / "index.toml").write_bytes(b"original index\n")
        self.config = self.packs / "demo" / "pack.yaml"
        self.config.write_text(
            "id: demo\ndisplay_name: Demo\nenabled: true\n", encoding="utf-8"
        )
        self.patches = [
            patch.object(core, "ROOT", self.root),
            patch.object(core, "PACKS", self.packs),
            patch.object(core, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                core,
                "TRANSACTION_ROOT",
                self.root / ".huroshiki" / "transactions",
            ),
            patch.object(core, "LOG_ROOT", self.root / ".huroshiki" / "logs"),
            patch.object(packctl, "ROOT", self.root),
            patch.object(packctl, "PACKS", self.packs),
            patch.object(packctl, "STATE_ROOT", self.root / ".huroshiki"),
            patch.object(
                packctl,
                "resolve_modrinth_identity",
                side_effect=lambda selector: packctl.modrinth_project_reference(selector),
            ),
            patch.object(
                core,
                "_run_provider_lookup",
                side_effect=self.provider_lookup,
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

    def snapshot(self) -> dict[Path, bytes | str]:
        return self.snapshot_tree(self.source)

    @staticmethod
    def snapshot_tree(root: Path) -> dict[Path, bytes | str]:
        snapshot: dict[Path, bytes | str] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if path.is_symlink():
                snapshot[relative] = f"symlink:{path.readlink()}"
            elif path.is_file():
                snapshot[relative] = path.read_bytes()
            elif path.is_dir():
                snapshot[relative] = "directory"
        return snapshot

    @staticmethod
    def completed(command: list[str], returncode: int = 0):
        return subprocess.CompletedProcess(command, returncode)

    @staticmethod
    def run_fake_resolver(command, *, cwd, cancel_event, deadline):
        result = core.subprocess.run(command, cwd=cwd, check=False)
        return core.ResolverProcessResult(
            result.returncode,
            result.stdout or "",
            result.stderr or "",
            False,
            False,
        )

    @staticmethod
    def provider_lookup(arguments, **_):
        selector = arguments[2]
        reference = packctl.modrinth_project_reference(selector)
        return {
            "provider": "modrinth",
            "project_id": reference,
            "slug": reference,
            "title": reference,
        }

    def install_files(self, cwd: Path, root_id: str = "example") -> None:
        (cwd / "mods/root.pw.toml").write_text(
            metadata("Root", root_id), encoding="utf-8"
        )
        (cwd / "mods/dependency.pw.toml").write_text(
            metadata("Dependency", "dependency"), encoding="utf-8"
        )

    def assert_unlocked(self) -> None:
        self.assertFalse(packctl.project_lock_is_active(self.key))

    @staticmethod
    def closure(
        root_id: str,
        *dependencies: tuple[str, str],
    ) -> core.ResolvedModClosure:
        records = []
        for project_id, filename in ((root_id, f"{root_id}.jar"), *dependencies):
            identity = ("modrinth", project_id)
            records.append(
                core.ResolvedMetadata(
                    identity,
                    Path("mods") / f"{project_id}.pw.toml",
                    filename,
                    metadata(project_id, project_id).encode("utf-8"),
                    "modrinth",
                    project_id,
                )
            )
        return core.ResolvedModClosure(("modrinth", root_id), tuple(records))

    def enable_private_url_provider(self) -> None:
        (self.packs / "demo/pack.local.yaml").write_text(
            "url_allow_private_networks: true\n", encoding="utf-8"
        )

    @staticmethod
    def url_artifact() -> core.UrlArtifact:
        return core.UrlArtifact(
            name="Private Mod",
            mod_id="private_mod",
            version="1.0.0",
            filename="private-mod-1.0.0.jar",
            url="https://127.0.0.1/private-mod-1.0.0.jar",
            sha256="00",
            loaders=("neoforge",),
        )

    def test_success_applies_root_dependencies_and_packwiz_files_together(self) -> None:
        original = self.snapshot()
        command_directories: list[Path] = []

        def run(command, *, cwd, **_):
            self.assertEqual(self.snapshot(), original)
            self.assertNotEqual(cwd, self.source)
            command_directories.append(cwd)
            if "add" in command and command[-1] == "example":
                self.install_files(cwd)
            elif command == ["packwiz", "refresh"]:
                (cwd / "index.toml").write_bytes(b"refreshed index\n")
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run):
            result = core.add_mod_transactionally(
                self.key, "modrinth", "example", "client"
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(set(command_directories)), 2)
        self.assertIn('side = "client"', (self.source / "mods/root.pw.toml").read_text())
        self.assertIn(
            'side = "client"',
            (self.source / "mods/dependency.pw.toml").read_text(),
        )
        self.assertIn(
            'minecraft = "1.21.1"',
            (self.source / "pack.toml").read_text(encoding="utf-8"),
        )
        self.assertEqual((self.source / "index.toml").read_bytes(), b"refreshed index\n")
        self.assert_unlocked()

    def test_add_and_refresh_failures_leave_real_tree_unchanged(self) -> None:
        for failure in ("add", "refresh"):
            with self.subTest(failure=failure):
                original = self.snapshot()

                def run(command, *, cwd, **_):
                    if "add" in command:
                        self.install_files(cwd)
                        return self.completed(command, 7 if failure == "add" else 0)
                    if command == ["packwiz", "refresh"]:
                        (cwd / "index.toml").write_bytes(b"partial refresh\n")
                        return self.completed(command, 9)
                    raise AssertionError(command)

                with patch.object(core.subprocess, "run", side_effect=run):
                    if failure == "add":
                        with self.assertRaises(core.HuroshikiError):
                            core.add_mod_transactionally(
                                self.key, "modrinth", "example", "both"
                            )
                    else:
                        with self.assertRaises(core.HuroshikiError):
                            core.add_mod_transactionally(
                                self.key, "modrinth", "example", "both"
                            )
                self.assertEqual(self.snapshot(), original)
                self.assert_unlocked()

    def test_side_write_failure_leaves_real_tree_unchanged(self) -> None:
        original = self.snapshot()

        def run(command, *, cwd, **_):
            self.install_files(cwd)
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run), patch.object(
            core,
            "_metadata_contents_with_side",
            side_effect=OSError("side write failed"),
        ):
            with self.assertRaisesRegex(core.HuroshikiError, "side write failed"):
                core.add_mod_transactionally(
                    self.key, "modrinth", "example", "both"
                )
        self.assertEqual(self.snapshot(), original)
        self.assert_unlocked()

    def test_changed_existing_metadata_unions_baseline_side_by_path_and_identity(self) -> None:
        existing = self.source / "mods/existing.pw.toml"
        existing.write_text(metadata("Existing", "existing", "client"), encoding="utf-8")
        unchanged = self.source / "mods/unchanged.pw.toml"
        unchanged.write_text(metadata("Unchanged", "unchanged", "client"), encoding="utf-8")
        shared = self.source / "mods/shared.pw.toml"
        shared.write_text(metadata("Shared", "shared", "both"), encoding="utf-8")

        def run(command, *, cwd, **_):
            if "add" in command:
                moved = cwd / "dependencies/existing-renamed.pw.toml"
                moved.parent.mkdir()
                moved.write_text(metadata("Existing", "existing", "server"), encoding="utf-8")
                (cwd / "mods/root.pw.toml").write_text(
                    metadata("Root", "root", "client"), encoding="utf-8"
                )
                (cwd / "mods/shared.pw.toml").write_text(
                    metadata("Shared", "shared", "server"), encoding="utf-8"
                )
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run):
            self.assertEqual(
                core.add_mod_transactionally(self.key, "modrinth", "root", "server"),
                0,
            )

        self.assertIn('side = "both"', existing.read_text())
        self.assertIn('side = "server"', (self.source / "mods/root.pw.toml").read_text())
        self.assertIn('side = "client"', unchanged.read_text())
        self.assertIn('side = "both"', shared.read_text())
        self.assertFalse((self.source / "dependencies/existing-renamed.pw.toml").exists())

    def test_unchanged_shared_dependency_unions_sides_from_complete_closure(self) -> None:
        for first_side, requested_side in (("client", "server"), ("server", "client")):
            with self.subTest(first_side=first_side, requested_side=requested_side):
                shared = self.source / "mods/shared.pw.toml"
                shared.write_text(
                    metadata("shared", "shared", first_side), encoding="utf-8"
                )
                closure = self.closure("second-root", ("shared", "shared.jar"))

                with patch.object(
                    core, "resolve_mod_closure", return_value=closure
                ), patch.object(
                    core.subprocess,
                    "run",
                    side_effect=lambda command, **_: self.completed(command),
                ):
                    self.assertEqual(
                        core.add_mod_transactionally(
                            self.key, "modrinth", "second-root", requested_side
                        ),
                        0,
                    )

                self.assertEqual(packctl.read_toml(shared)["side"], "both")
                (self.source / "mods/second-root.pw.toml").unlink()
                shared.unlink()

    def test_resolved_add_operation_merges_complete_closure(self) -> None:
        shared = self.source / "mods/shared.pw.toml"
        shared.write_text(metadata("Shared", "shared", "client"), encoding="utf-8")
        transaction = core.PackTransaction.create(self.key)
        try:
            operation = transaction.begin_resolved_add(
                provider="modrinth",
                selector="second-root",
                canonical_project_id="second-root",
                side="server",
            )
            closure = self.closure("second-root", ("shared", "shared.jar"))
            with patch.object(
                core, "resolve_mod_closure", return_value=closure
            ) as resolve:
                result = operation.run()

            self.assertEqual(
                resolve.call_args.kwargs["canonical_project_id"], "second-root"
            )
            self.assertTrue(result.success, result.message)
            staged_shared = transaction.source / "mods/shared.pw.toml"
            self.assertEqual(packctl.read_toml(staged_shared)["side"], "both")
            self.assertFalse(operation.resolver_root.exists())
            self.assertEqual(packctl.read_toml(shared)["side"], "client")
        finally:
            transaction.discard()

    def test_closure_conflicts_abort_without_publishing(self) -> None:
        existing = self.source / "mods/existing.pw.toml"
        existing.write_text(metadata("Existing", "existing", "client"), encoding="utf-8")
        original = self.snapshot()
        divergent = self.closure("existing")
        record = divergent.metadata[0]
        divergent = core.ResolvedModClosure(
            divergent.root_identity,
            (
                core.ResolvedMetadata(
                    record.identity,
                    record.relative_path,
                    record.filename,
                    record.contents.replace(b'version = "v1"', b'version = "v2"'),
                    record.provider,
                    record.project_id,
                ),
            ),
        )
        with patch.object(core, "resolve_mod_closure", return_value=divergent):
            with self.assertRaisesRegex(core.HuroshikiError, "disagreement"):
                core.add_mod_transactionally(
                    self.key, "modrinth", "existing", "server"
                )
        self.assertEqual(self.snapshot(), original)
        self.assert_unlocked()

    def test_closure_path_and_filename_collisions_abort(self) -> None:
        existing = self.source / "mods/existing.pw.toml"
        existing.write_text(metadata("Existing", "existing"), encoding="utf-8")
        original = self.snapshot()
        base = self.closure("incoming").metadata[0]
        cases = (
            (
                core.ResolvedMetadata(
                    base.identity,
                    Path("mods/existing.pw.toml"),
                    base.filename,
                    base.contents,
                    base.provider,
                    base.project_id,
                ),
                "Metadata path collision",
            ),
            (
                core.ResolvedMetadata(
                    base.identity,
                    base.relative_path,
                    "existing.jar",
                    base.contents.replace(b'incoming.jar', b'existing.jar'),
                    base.provider,
                    base.project_id,
                ),
                "Filename collision",
            ),
        )
        for record, message in cases:
            with self.subTest(message=message), patch.object(
                core,
                "resolve_mod_closure",
                return_value=core.ResolvedModClosure(base.identity, (record,)),
            ):
                with self.assertRaisesRegex(core.HuroshikiError, message):
                    core.add_mod_transactionally(
                        self.key, "modrinth", "incoming", "both"
                    )
                self.assertEqual(self.snapshot(), original)
                self.assert_unlocked()

    def test_url_resolver_returns_single_root_closure(self) -> None:
        artifact = self.url_artifact()
        with patch.object(core, "download_url_artifact", return_value=artifact):
            closure = core.resolve_mod_closure(
                provider="url",
                selector="https://example.invalid/private-mod.jar",
                minecraft="1.21.1",
                loader="neoforge",
                loader_version="21.1.234",
            )
        self.assertEqual(closure.root_identity, ("url", "private_mod"))
        self.assertEqual(len(closure.metadata), 1)
        self.assertEqual(closure.metadata[0].identity, closure.root_identity)

    def test_modrinth_selectors_resolve_to_canonical_root_identity(self) -> None:
        canonical_id = "Canonical1"
        selectors = (
            canonical_id,
            "sodium-extra",
            "feature-with-hyphens",
            "https://modrinth.com/mod/sodium-extra",
            "https://www.modrinth.com/project/sodium-extra",
        )

        def run(command, *, cwd, **_):
            self.assertEqual(command[-2:], ["--project-id", canonical_id])
            (cwd / "mods/root.pw.toml").write_text(
                metadata("Completely Different Display", canonical_id), encoding="utf-8"
            )
            (cwd / "mods/dependency.pw.toml").write_text(
                metadata("Completely Different Display", "dependency"), encoding="utf-8"
            )
            return self.completed(command)

        for selector in selectors:
            lookup_result = {
                "provider": "modrinth",
                "project_id": canonical_id,
                "slug": "sodium-extra",
                "title": "Sodium Extra",
            }
            with self.subTest(selector=selector), patch.object(
                core, "_run_provider_lookup", return_value=lookup_result
            ) as resolve, patch.object(core.subprocess, "run", side_effect=run):
                closure = core.resolve_mod_closure(
                    provider="modrinth",
                    selector=selector,
                    minecraft="1.21.1",
                    loader="neoforge",
                    loader_version="21.1.234",
                )
            self.assertEqual(closure.root_identity, ("modrinth", canonical_id))
            resolve.assert_called_once()

    def test_modrinth_resolution_and_root_mismatch_fail_closed(self) -> None:
        for error in (
            core.HuroshikiError("API unavailable"),
            core.HuroshikiError("API timed out"),
        ):
            with self.subTest(error=str(error)), patch.object(
                core, "_run_provider_lookup", side_effect=error
            ), patch.object(core.subprocess, "run") as run:
                with self.assertRaisesRegex(core.HuroshikiError, str(error)):
                    core.resolve_mod_closure(
                        provider="modrinth",
                        selector="slug",
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.234",
                    )
                run.assert_not_called()

        def wrong_root(command, *, cwd, **_):
            (cwd / "mods/dependency.pw.toml").write_text(
                metadata("Requested Display", "dependency"), encoding="utf-8"
            )
            return self.completed(command)

        lookup_result = {
            "provider": "modrinth",
            "project_id": "expected",
            "slug": "expected",
            "title": "Expected",
        }
        with patch.object(
            core, "_run_provider_lookup", return_value=lookup_result
        ), patch.object(core.subprocess, "run", side_effect=wrong_root):
            with self.assertRaisesRegex(core.HuroshikiError, "resolved 0 times"):
                core.resolve_mod_closure(
                    provider="modrinth",
                    selector="requested-display",
                    minecraft="1.21.1",
                    loader="neoforge",
                    loader_version="21.1.234",
                )

    def test_curseforge_requires_canonical_numeric_identity(self) -> None:
        def run(command, *, cwd, **_):
            self.assertEqual(command[-2:], ["--addon-id", "12345"])
            (cwd / "mods/root.pw.toml").write_text(
                metadata("Root", "12345").replace("update.modrinth", "update.curseforge")
                .replace('mod-id = "12345"', "project-id = 12345"),
                encoding="utf-8",
            )
            (cwd / "mods/dependency.pw.toml").write_text(
                metadata("Dependency", "dependency"), encoding="utf-8"
            )
            return self.completed(command)

        for selector in ("12345", "cf:12345"):
            with self.subTest(selector=selector), patch.object(
                core.subprocess, "run", side_effect=run
            ):
                closure = core.resolve_mod_closure(
                    provider="curseforge",
                    selector=selector,
                    minecraft="1.21.1",
                    loader="neoforge",
                    loader_version="21.1.234",
                )
            self.assertEqual(closure.root_identity, ("curseforge", "12345"))

        for selector in (
            "search terms",
            "https://www.curseforge.com/minecraft/mc-mods/example",
        ):
            with self.subTest(selector=selector), patch.object(
                core.subprocess, "run"
            ) as run:
                with self.assertRaisesRegex(
                    core.HuroshikiError, "Canonical project ID is unavailable"
                ):
                    core.resolve_mod_closure(
                        provider="curseforge",
                        selector=selector,
                        minecraft="1.21.1",
                        loader="neoforge",
                        loader_version="21.1.234",
                    )
                run.assert_not_called()

    def test_resolved_add_failure_restores_existing_staged_changes(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        try:
            staged = transaction.source / "mods/staged.pw.toml"
            staged.write_text(metadata("Staged", "staged"), encoding="utf-8")
            before = staged.read_bytes()
            operation = transaction.begin_resolved_add(
                provider="curseforge",
                selector="12345",
                canonical_project_id="12345",
                side="both",
            )
            with patch.object(
                core, "resolve_mod_closure", side_effect=core.HuroshikiError("failed")
            ):
                result = operation.run()
            self.assertFalse(result.success)
            self.assertEqual(staged.read_bytes(), before)
            self.assertTrue(transaction.active)
        finally:
            transaction.discard()

    def test_resolved_add_cancel_preserves_existing_staged_changes(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        started = threading.Event()
        try:
            staged = transaction.source / "mods/staged.pw.toml"
            staged.write_text(metadata("Staged", "staged"), encoding="utf-8")
            before = staged.read_bytes()
            operation = transaction.begin_resolved_add(
                provider="modrinth",
                selector="Sodium Extra",
                canonical_project_id="canonical",
                side="client",
            )

            def resolve(*_, cancel_event, **__):
                started.set()
                cancel_event.wait(2)
                raise core.HuroshikiError("MOD resolution was cancelled")

            with patch.object(core, "resolve_mod_closure", side_effect=resolve):
                worker = threading.Thread(target=operation.run)
                worker.start()
                self.assertTrue(started.wait(1))
                operation.cancel()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            assert operation.result is not None
            self.assertTrue(operation.result.cancelled)
            self.assertEqual(staged.read_bytes(), before)
        finally:
            transaction.discard()

    def test_add_operation_constructors_do_not_prepare_filesystems(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        try:
            with patch.object(core, "copy_transaction_source") as copy, patch.object(
                core, "create_resolver_source"
            ) as create, patch.object(
                core.packctl, "project_versions"
            ) as versions, patch.object(
                core.packctl, "ensure_safe_state_path"
            ) as validate_log, patch.object(
                core, "PackwizPtySession"
            ) as session:
                before = time.monotonic()
                url_operation = transaction.begin_add(
                    "url",
                    "https://example.invalid/private.jar",
                    client=True,
                    server=True,
                )
                after = time.monotonic()
                self.assertGreaterEqual(
                    url_operation.deadline,
                    before + core.PACKWIZ_OPERATION_TIMEOUT_SECONDS,
                )
                self.assertLessEqual(
                    url_operation.deadline,
                    after + core.PACKWIZ_OPERATION_TIMEOUT_SECONDS,
                )
                copy.assert_not_called()
                create.assert_not_called()
                versions.assert_not_called()
                validate_log.assert_not_called()
                session.assert_not_called()
                self.assertFalse(url_operation.checkpoint.exists())
                self.assertFalse(url_operation.resolver_root.exists())
                self.assertIsNone(url_operation.session)
                self.assertTrue(
                    url_operation.abort_before_start(
                        core.HuroshikiError("test cleanup")
                    )
                )

                resolved_operation = transaction.begin_resolved_add(
                    provider="modrinth",
                    selector="root",
                    canonical_project_id="root",
                    side="both",
                )
                copy.assert_not_called()
                create.assert_not_called()
                versions.assert_not_called()
                self.assertFalse(resolved_operation.checkpoint.exists())
                self.assertFalse(resolved_operation.resolver_root.exists())
                resolved_operation.abort_before_start(
                    core.HuroshikiError("test cleanup")
                )
        finally:
            transaction.discard()

    def test_url_checkpoint_copy_runs_only_in_operation_worker(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        caller_thread = threading.get_ident()
        copy_threads: list[int] = []
        original_copy = core.copy_transaction_source
        try:
            operation = transaction.begin_add(
                "url",
                "https://example.invalid/private.jar",
                client=True,
                server=True,
            )
            self.assertEqual(copy_threads, [])

            def copy(*args, **kwargs):
                copy_threads.append(threading.get_ident())
                return original_copy(*args, **kwargs)

            with patch.object(core, "copy_transaction_source", side_effect=copy), patch.object(
                core, "download_url_artifact", return_value=self.url_artifact()
            ):
                worker = threading.Thread(target=operation.run)
                worker.start()
                worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(copy_threads), 1)
            self.assertNotEqual(copy_threads[0], caller_thread)
            assert operation.result is not None
            self.assertTrue(operation.result.success, operation.result.message)
            self.assertIs(operation.run(), operation.result)
        finally:
            transaction.discard()

    def test_checkpoint_copy_cancellation_removes_partial_state_and_rolls_back(self) -> None:
        for resolved in (False, True):
            with self.subTest(resolved=resolved):
                transaction = core.PackTransaction.create(self.key)
                started = threading.Event()
                staged = transaction.source / "mods/staged.pw.toml"
                staged.write_text(metadata("Staged", "staged"), encoding="utf-8")
                staged_before = self.snapshot_tree(transaction.source)
                real_before = self.snapshot()
                try:
                    operation = (
                        transaction.begin_resolved_add(
                            provider="modrinth",
                            selector="root",
                            canonical_project_id="root",
                            side="both",
                        )
                        if resolved
                        else transaction.begin_add(
                            "url",
                            "https://example.invalid/private.jar",
                            client=True,
                            server=True,
                        )
                    )

                    def copy(_, destination, *, checkpoint):
                        destination.mkdir(parents=True)
                        (destination / "partial").write_text("partial", encoding="utf-8")
                        started.set()
                        while True:
                            checkpoint()
                            time.sleep(0.005)

                    with patch.object(core, "copy_transaction_source", side_effect=copy):
                        worker = threading.Thread(target=operation.run)
                        worker.start()
                        self.assertTrue(started.wait(1))
                        operation.cancel(deadline=time.monotonic() + 1)
                        worker.join(2)

                    self.assertFalse(worker.is_alive())
                    assert operation.result is not None
                    self.assertTrue(operation.result.cancelled)
                    self.assertEqual(operation.result.returncode, 130)
                    self.assertFalse(operation.checkpoint.exists())
                    self.assertFalse(operation.resolver_root.exists())
                    self.assertEqual(
                        self.snapshot_tree(transaction.source), staged_before
                    )
                    self.assertEqual(self.snapshot(), real_before)
                    self.assertIsNone(transaction._operation)
                    self.assertTrue(operation.done.is_set())
                finally:
                    transaction.discard()

    def test_checkpoint_copy_deadline_is_distinct_from_cancellation(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        staged_before = self.snapshot_tree(transaction.source)
        real_before = self.snapshot()
        try:
            operation = transaction.begin_add(
                "url",
                "https://example.invalid/private.jar",
                client=True,
                server=True,
                deadline=time.monotonic() + 0.03,
            )

            def copy(_, destination, *, checkpoint):
                destination.mkdir(parents=True)
                (destination / "partial").touch()
                while True:
                    checkpoint()
                    time.sleep(0.005)

            with patch.object(core, "copy_transaction_source", side_effect=copy):
                result = operation.run()

            self.assertFalse(result.cancelled)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.message, "Install operation deadline exceeded")
            self.assertFalse(operation.checkpoint.exists())
            self.assertFalse(operation.resolver_root.exists())
            self.assertEqual(self.snapshot_tree(transaction.source), staged_before)
            self.assertEqual(self.snapshot(), real_before)
            self.assertIsNone(transaction._operation)
        finally:
            transaction.discard()

    def test_checkpoint_copy_rejects_conflicting_transaction_mutations(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        started = threading.Event()
        staged = transaction.source / "mods/staged.pw.toml"
        staged.write_text(metadata("Staged", "staged"), encoding="utf-8")
        try:
            operation = transaction.begin_add(
                "url",
                "https://example.invalid/private.jar",
                client=True,
                server=True,
            )

            def copy(_, destination, *, checkpoint):
                destination.mkdir(parents=True)
                started.set()
                while True:
                    checkpoint()
                    time.sleep(0.005)

            with patch.object(core, "copy_transaction_source", side_effect=copy):
                worker = threading.Thread(target=operation.run)
                worker.start()
                self.assertTrue(started.wait(1))
                with self.assertRaisesRegex(core.HuroshikiError, "Another Packwiz"):
                    transaction.begin_add(
                        "url",
                        "https://example.invalid/second.jar",
                        client=True,
                        server=True,
                    )
                with self.assertRaisesRegex(core.HuroshikiError, "active add operation"):
                    transaction.set_side(Path("mods/staged.pw.toml"), True, False)
                with self.assertRaisesRegex(core.HuroshikiError, "active add operation"):
                    transaction.unstage(Path("mods/staged.pw.toml"))
                with self.assertRaisesRegex(core.HuroshikiError, "active add operation"):
                    transaction.apply()
                operation.cancel(deadline=time.monotonic() + 1)
                worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertIsNone(transaction._operation)
        finally:
            transaction.discard()

    def test_resolved_add_propagates_operation_deadline(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        deadline = time.monotonic() + 30
        closure = self.closure("root")
        try:
            operation = transaction.begin_resolved_add(
                provider="modrinth",
                selector="root",
                canonical_project_id="root",
                side="both",
                deadline=deadline,
            )
            with patch.object(
                core, "resolve_mod_closure", return_value=closure
            ) as resolve:
                result = operation.run()
            self.assertTrue(result.success, result.message)
            self.assertEqual(resolve.call_args.kwargs["deadline"], deadline)
            self.assertIs(resolve.call_args.kwargs["cancel_event"], operation.cancel_event)
        finally:
            transaction.discard()

    def test_cancel_tightens_but_never_extends_operation_deadline(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        try:
            first = time.monotonic() + 30
            operation = transaction.begin_resolved_add(
                provider="modrinth",
                selector="root",
                canonical_project_id="root",
                side="both",
                deadline=first,
            )
            operation.cancel(deadline=first + 30)
            self.assertEqual(operation.deadline, first)
            self.assertTrue(operation.done.is_set())

            second = transaction.begin_resolved_add(
                provider="modrinth",
                selector="root",
                canonical_project_id="root",
                side="both",
                deadline=first,
            )
            tightened = first - 10
            second.cancel(deadline=tightened)
            self.assertEqual(second.deadline, tightened)
            self.assertTrue(second.done.is_set())
        finally:
            transaction.discard()

    def test_url_timeout_is_clipped_and_expired_deadline_skips_download(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        try:
            deadline = time.monotonic() + 5
            operation = transaction.begin_add(
                "url",
                "https://example.invalid/private.jar",
                client=True,
                server=True,
                deadline=deadline,
            )
            observed_timeouts: list[tuple[float, float]] = []

            def download(*_, total_timeout_seconds, **__):
                observed_timeouts.append(
                    (total_timeout_seconds, deadline - time.monotonic())
                )
                return self.url_artifact()

            with patch.object(
                core, "download_url_artifact", side_effect=download
            ):
                result = operation.run()
            self.assertTrue(result.success, result.message)
            timeout, remaining = observed_timeouts[0]
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, core.DEFAULT_URL_TOTAL_TIMEOUT_SECONDS)
            self.assertLessEqual(timeout, remaining + 0.01)

            expired = transaction.begin_add(
                "url",
                "https://example.invalid/expired.jar",
                client=True,
                server=True,
                deadline=time.monotonic() - 1,
            )
            with patch.object(core, "download_url_artifact") as skipped:
                expired_result = expired.run()
            skipped.assert_not_called()
            self.assertEqual(
                expired_result.message, "Install operation deadline exceeded"
            )
            self.assertFalse(expired_result.cancelled)
        finally:
            transaction.discard()

    def test_cancel_after_checkpoint_restores_staged_and_real_sources(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        entered = threading.Event()
        staged = transaction.source / "mods/staged.pw.toml"
        staged.write_text(metadata("Staged", "staged"), encoding="utf-8")
        staged_before = self.snapshot_tree(transaction.source)
        real_before = self.snapshot()
        try:
            operation = transaction.begin_add(
                "url",
                "https://example.invalid/private.jar",
                client=True,
                server=True,
            )

            def download(*_, **__):
                entered.set()
                operation.cancel_event.wait(2)
                raise core.HuroshikiError("download cancelled")

            with patch.object(core, "download_url_artifact", side_effect=download):
                worker = threading.Thread(target=operation.run)
                worker.start()
                self.assertTrue(entered.wait(1))
                self.assertTrue(operation._checkpoint_complete)
                operation.cancel(deadline=time.monotonic() + 1)
                worker.join(2)

            self.assertFalse(worker.is_alive())
            assert operation.result is not None
            self.assertTrue(operation.result.cancelled)
            self.assertEqual(self.snapshot_tree(transaction.source), staged_before)
            self.assertEqual(self.snapshot(), real_before)
            self.assertFalse(operation.checkpoint.exists())
            self.assertFalse(operation.resolver_root.exists())
            self.assertIsNone(transaction._operation)
        finally:
            transaction.discard()

    def test_worker_start_failure_releases_operation_ownership(self) -> None:
        transaction = core.PackTransaction.create(self.key)
        try:
            operation = transaction.begin_add(
                "url",
                "https://example.invalid/private.jar",
                client=True,
                server=True,
            )
            self.assertTrue(
                operation.abort_before_start(
                    core.HuroshikiError(
                        "Add operation worker could not start: start failed"
                    )
                )
            )
            self.assertTrue(operation.done.is_set())
            self.assertEqual(operation.state, "done")
            assert operation.result is not None
            self.assertIn("worker could not start", operation.result.message)
            self.assertIsNone(transaction._operation)
            self.assertTrue(transaction.active)

            retry = transaction.begin_add(
                "url",
                "https://example.invalid/retry.jar",
                client=True,
                server=True,
            )
            retry.abort_before_start(core.HuroshikiError("test cleanup"))
        finally:
            transaction.discard()

    def test_changed_invalid_baseline_side_is_not_silently_reclassified(self) -> None:
        existing = self.source / "mods/existing.pw.toml"
        existing.write_text(metadata("Existing", "existing", "invalid"), encoding="utf-8")
        original = self.snapshot()

        def run(command, *, cwd, **_):
            if "add" in command:
                (cwd / "mods/existing.pw.toml").write_text(
                    metadata("Existing", "existing", "server"), encoding="utf-8"
                )
            return self.completed(command)

        with patch.object(core.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(core.HuroshikiError, "invalid existing side"):
                core.add_mod_transactionally(
                    self.key, "modrinth", "existing", "server"
                )

        self.assertEqual(self.snapshot(), original)
        self.assert_unlocked()

    def test_provider_and_url_transactions_reject_source_symlinks_without_writes(self) -> None:
        external = self.root / "external"
        external.mkdir()
        secret = external / "secret.txt"
        secret.write_text("keep", encoding="utf-8")
        link = self.source / "mods/linked"
        link.symlink_to(external, target_is_directory=True)
        original = self.snapshot()

        for provider, selector in (
            ("modrinth", "example"),
            ("url", "https://example.invalid/private.jar"),
        ):
            with self.subTest(provider=provider), patch.object(core.subprocess, "run") as run, patch.object(
                core, "download_url_artifact"
            ) as download:
                with self.assertRaisesRegex(core.HuroshikiError, "symlink is not allowed"):
                    core.add_mod_transactionally(self.key, provider, selector, "both")
                run.assert_not_called()
                download.assert_not_called()
                self.assertEqual(self.snapshot(), original)
                self.assertEqual(secret.read_text(), "keep")
                self.assert_unlocked()

    def test_cli_url_add_succeeds_noninteractively_with_private_opt_in(self) -> None:
        self.enable_private_url_provider()
        args = type(
            "Args",
            (),
            {
                "pack": "demo",
                "query": "url:https://127.0.0.1/private-mod-1.0.0.jar",
                "side": "server",
            },
        )()

        def download(*_, **kwargs):
            self.assertTrue(kwargs["allow_private_networks"])
            return self.url_artifact()

        with patch.object(packctl, "choose_provider") as choose, patch.object(
            core, "download_url_artifact", side_effect=download
        ), patch.object(
            core.subprocess,
            "run",
            side_effect=lambda command, **_: self.completed(command),
        ):
            self.assertEqual(packctl.cmd_add(args), 0)

        choose.assert_not_called()
        installed = self.source / "mods/private_mod.pw.toml"
        self.assertIn('side = "server"', installed.read_text())
        self.assert_unlocked()

    def test_cli_url_failure_and_interrupt_preserve_source_and_unlock(self) -> None:
        self.enable_private_url_provider()
        args = type(
            "Args",
            (),
            {
                "pack": "demo",
                "query": "url:https://127.0.0.1/private-mod-1.0.0.jar",
                "side": "both",
            },
        )()
        original = self.snapshot()

        with patch.object(
            core,
            "download_url_artifact",
            side_effect=core.HuroshikiError("download failed"),
        ):
            self.assertEqual(packctl.cmd_add(args), 1)
        self.assertEqual(self.snapshot(), original)
        self.assert_unlocked()

        with patch.object(
            core, "download_url_artifact", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                packctl.cmd_add(args)
        self.assertEqual(self.snapshot(), original)
        self.assert_unlocked()

    def test_cli_url_external_change_is_preserved_and_unlocks(self) -> None:
        self.enable_private_url_provider()
        args = type(
            "Args",
            (),
            {
                "pack": "demo",
                "query": "url:https://127.0.0.1/private-mod-1.0.0.jar",
                "side": "both",
            },
        )()

        def download(*_, **kwargs):
            self.assertTrue(kwargs["allow_private_networks"])
            (self.source / "index.toml").write_bytes(b"external index\n")
            return self.url_artifact()

        with patch.object(
            core, "download_url_artifact", side_effect=download
        ), patch.object(
            core.subprocess,
            "run",
            side_effect=lambda command, **_: self.completed(command),
        ):
            with self.assertRaisesRegex(packctl.ConfigError, "changed"):
                packctl.cmd_add(args)

        self.assertEqual((self.source / "index.toml").read_bytes(), b"external index\n")
        self.assertFalse((self.source / "mods/private_mod.pw.toml").exists())
        self.assert_unlocked()

    def test_keyboard_interrupt_during_add_or_refresh_discards_and_unlocks(self) -> None:
        for interrupted_command in ("add", "refresh"):
            with self.subTest(command=interrupted_command):
                original = self.snapshot()

                def run(command, *, cwd, **_):
                    if "add" in command:
                        self.install_files(cwd)
                        if interrupted_command == "add":
                            raise KeyboardInterrupt
                    elif command == ["packwiz", "refresh"]:
                        (cwd / "index.toml").write_bytes(b"partial refresh\n")
                        raise KeyboardInterrupt
                    return self.completed(command)

                with patch.object(core.subprocess, "run", side_effect=run):
                    with self.assertRaises(KeyboardInterrupt):
                        core.add_mod_transactionally(
                            self.key, "modrinth", "example", "both"
                        )
            self.assertEqual(self.snapshot(), original)
            self.assert_unlocked()

    def test_cancelled_noninteractive_resolver_discards_and_unlocks(self) -> None:
        original = self.snapshot()
        cancelled = core.ResolverProcessResult(-15, "", "", True, False)
        with patch.object(core, "run_resolver_process", return_value=cancelled):
            with self.assertRaisesRegex(core.HuroshikiError, "resolution was cancelled"):
                core.add_mod_transactionally(
                    self.key, "modrinth", "example", "both"
                )
        self.assertEqual(self.snapshot(), original)
        self.assert_unlocked()
        transactions = self.root / ".huroshiki/transactions"
        retained = list(transactions.iterdir())
        self.assertEqual(len(retained), 1)
        self.assertTrue(retained[0].is_dir())

    def test_prestart_cancel_skips_selector_and_process_resolution(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with patch.object(core, "resolve_project_selector") as selector, patch.object(
            core, "run_resolver_process"
        ) as runner:
            with self.assertRaisesRegex(core.HuroshikiError, "resolution was cancelled"):
                core.resolve_mod_closure(
                    provider="modrinth",
                    selector="example",
                    minecraft="1.21.1",
                    loader="neoforge",
                    loader_version="21.1.234",
                    cancel_event=cancel,
                )
        selector.assert_not_called()
        runner.assert_not_called()

    def test_external_source_or_configuration_change_aborts_apply(self) -> None:
        for external_change in ("source", "config"):
            with self.subTest(change=external_change):
                (self.source / "index.toml").write_bytes(b"original index\n")
                self.config.write_text(
                    "id: demo\ndisplay_name: Demo\nenabled: true\n", encoding="utf-8"
                )

                def run(command, *, cwd, **_):
                    if "add" in command:
                        self.install_files(cwd)
                    elif command == ["packwiz", "refresh"]:
                        if external_change == "source":
                            (self.source / "index.toml").write_bytes(b"external index\n")
                        else:
                            self.config.write_text("external: true\n", encoding="utf-8")
                    return self.completed(command)

                with patch.object(core.subprocess, "run", side_effect=run):
                    with self.assertRaisesRegex(core.HuroshikiError, "changed"):
                        core.add_mod_transactionally(
                            self.key, "modrinth", "example", "server"
                        )
                self.assertFalse((self.source / "mods/root.pw.toml").exists())
                if external_change == "source":
                    self.assertEqual(
                        (self.source / "index.toml").read_bytes(), b"external index\n"
                    )
                else:
                    self.assertEqual(self.config.read_text(), "external: true\n")
                self.assert_unlocked()


if __name__ == "__main__":
    unittest.main()
