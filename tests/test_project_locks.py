from __future__ import annotations

from contextlib import ExitStack
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


def _configure_roots(root: Path) -> None:
    packs = root / "packs"
    templates = root / "templates"
    state = root / ".huroshiki"
    packctl.ROOT = root
    packctl.PACKS = packs
    packctl.TEMPLATES = templates
    packctl.STATE_ROOT = state
    packctl.TRANSACTION_ROOT = state / "transactions"
    packctl.LOG_ROOT = state / "logs"
    packctl.TRASH_ROOT = state / "trash"
    core.ROOT = root
    core.PACKS = packs
    core.TEMPLATES = templates
    core.STATE_ROOT = state
    core.TRANSACTION_ROOT = state / "transactions"
    core.LOG_ROOT = state / "logs"
    core.TRASH_ROOT = state / "trash"


def _hold_lock(
    root: Path,
    project_key: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    _configure_roots(root)
    try:
        with packctl.ProjectLock(project_key, "worker hold"):
            results.put(("acquired", os.getpid()))
            ready.set()
            release.wait(10)
        results.put(("released", os.getpid()))
    except BaseException as error:
        results.put(("error", str(error)))
        ready.set()


def _try_lock(
    root: Path,
    project_key: str,
    results: multiprocessing.queues.Queue,
) -> None:
    _configure_roots(root)
    try:
        with packctl.ProjectLock(project_key, "worker try"):
            results.put(("acquired", os.getpid()))
    except BaseException as error:
        results.put(("blocked", str(error)))


class ProjectLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.state = self.root / ".huroshiki"
        self.templates.mkdir()
        for project_id in ("demo", "other"):
            source = self.packs / project_id / "source"
            source.mkdir(parents=True)
            (source / "pack.toml").write_text(
                'name = "Demo"\n[versions]\nminecraft = "1.21.1"\n'
                'neoforge = "21.1.234"\n',
                encoding="utf-8",
            )
            (source / "index.toml").write_text(
                'hash-format = "sha256"\n', encoding="utf-8"
            )
            (source.parent / "pack.yaml").write_text(
                f"id: {project_id}\n", encoding="utf-8"
            )
        self.stack = ExitStack()
        for module in (packctl, core):
            for name, value in (
                ("ROOT", self.root),
                ("PACKS", self.packs),
                ("TEMPLATES", self.templates),
                ("STATE_ROOT", self.state),
                ("TRANSACTION_ROOT", self.state / "transactions"),
                ("LOG_ROOT", self.state / "logs"),
                ("TRASH_ROOT", self.state / "trash"),
            ):
                self.stack.enter_context(patch.object(module, name, value))
        self.context = multiprocessing.get_context("fork")

    def tearDown(self) -> None:
        self.stack.close()
        self.temp.cleanup()

    def start_holder(self, project_key: str = "pack:demo"):
        ready = self.context.Event()
        release = self.context.Event()
        results = self.context.Queue()
        process = self.context.Process(
            target=_hold_lock,
            args=(self.root, project_key, ready, release, results),
        )
        process.start()
        self.assertTrue(ready.wait(5))
        self.assertEqual(results.get(timeout=5)[0], "acquired")
        return process, release, results

    def stop_holder(self, process, release, results) -> None:
        release.set()
        self.assertEqual(results.get(timeout=5)[0], "released")
        process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_same_project_contention_reports_owner_metadata(self) -> None:
        holder, release, holder_results = self.start_holder()
        try:
            metadata = packctl.active_project_lock("pack:demo")
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(metadata.operation, "worker hold")
            self.assertEqual(metadata.project_key, "pack:demo")
            self.assertEqual(metadata.process_start, packctl.process_start_identity(metadata.pid))
            self.assertTrue(metadata.acquired_at)

            results = self.context.Queue()
            contender = self.context.Process(
                target=_try_lock,
                args=(self.root, "pack:demo", results),
            )
            contender.start()
            status, message = results.get(timeout=5)
            contender.join(5)
            self.assertEqual(status, "blocked")
            self.assertIn(f"PID {metadata.pid}", message)
            self.assertIn("worker hold", message)
            self.assertEqual(contender.exitcode, 0)
        finally:
            self.stop_holder(holder, release, holder_results)

    def test_different_projects_remain_parallel(self) -> None:
        holder, release, holder_results = self.start_holder("pack:demo")
        try:
            results = self.context.Queue()
            other = self.context.Process(
                target=_try_lock,
                args=(self.root, "pack:other", results),
            )
            other.start()
            self.assertEqual(results.get(timeout=5)[0], "acquired")
            other.join(5)
            self.assertEqual(other.exitcode, 0)
            self.assertTrue(holder.is_alive())
        finally:
            self.stop_holder(holder, release, holder_results)

    def test_stale_metadata_without_kernel_lock_does_not_block(self) -> None:
        path = packctl.project_lock_path("pack:demo")
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "process_start": "reused-pid-start",
                    "operation": "stale",
                    "project_key": "pack:demo",
                    "acquired_at": "2000-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        with packctl.ProjectLock("pack:demo", "replacement") as lock:
            self.assertEqual(lock.metadata.operation, "replacement")
            self.assertTrue(packctl.project_lock_is_active("pack:demo"))
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_exception_releases_lock(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with packctl.ProjectLock("pack:demo", "failing operation"):
                raise RuntimeError("boom")

        results = self.context.Queue()
        process = self.context.Process(
            target=_try_lock,
            args=(self.root, "pack:demo", results),
        )
        process.start()
        self.assertEqual(results.get(timeout=5)[0], "acquired")
        process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_transaction_holds_project_lock_for_full_lifetime(self) -> None:
        transaction = core.PackTransaction.create("pack:demo")
        try:
            self.assertFalse((transaction.root / ".lock").exists())
            results = self.context.Queue()
            process = self.context.Process(
                target=_try_lock,
                args=(self.root, "pack:demo", results),
            )
            process.start()
            self.assertEqual(results.get(timeout=5)[0], "blocked")
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        finally:
            transaction.discard()

        results = self.context.Queue()
        process = self.context.Process(
            target=_try_lock,
            args=(self.root, "pack:demo", results),
        )
        process.start()
        self.assertEqual(results.get(timeout=5)[0], "acquired")
        process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_discard_keeps_lock_and_tree_until_blocked_url_worker_finishes(self) -> None:
        transaction = core.PackTransaction.create("pack:demo")
        operation = transaction.begin_add(
            "url",
            "https://example.invalid/private.jar",
            client=True,
            server=True,
        )
        entered = threading.Event()
        release = threading.Event()
        cleaned = threading.Event()

        def blocked_download(*args, **kwargs):
            entered.set()
            release.wait()
            return core.UrlArtifact(
                "Private", "private", "1.0", "private.jar",
                "https://example.invalid/private.jar", "00", ("neoforge",),
            )

        original_finish = transaction._finish_discard_once

        def finish_discard(*, deadline: float) -> None:
            original_finish(deadline=deadline)
            cleaned.set()

        transaction._finish_discard_once = finish_discard
        with patch.object(core, "download_url_artifact", side_effect=blocked_download):
            worker = threading.Thread(target=operation.run)
            worker.start()
            self.assertTrue(entered.wait(2))
            with self.assertRaises(core.TransactionDiscardTimeout):
                transaction.discard(deadline=time.monotonic() + 0.02)

            self.assertTrue(transaction.root.is_dir())
            self.assertTrue(packctl.project_lock_is_active("pack:demo"))
            with self.assertRaisesRegex(core.HuroshikiError, "locked"):
                core.PackTransaction.create("pack:demo")

            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            retry = transaction.retry_discard(deadline=time.monotonic() + 1)
            self.assertTrue(retry.wait(1))
            retry.raise_for_error()
            self.assertTrue(cleaned.wait(2))

        self.assertTrue(transaction.root.is_dir())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))
        self.assertFalse((transaction.root / "source" / "mods" / "private.pw.toml").exists())


if __name__ == "__main__":
    unittest.main()
