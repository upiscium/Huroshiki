from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import huroshiki_core as core
import packctl


class _DelayedOperation:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.deadline: float | None = None

    def cancel(self, *, deadline=None) -> None:
        self.deadline = deadline
        self.cancelled.set()

    def wait(self, timeout=None) -> bool:
        return self.done.wait(timeout)


class TransactionDiscardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packs = self.root / "packs"
        self.templates = self.root / "templates"
        self.state = self.root / ".huroshiki"
        self.templates.mkdir()
        source = self.packs / "demo" / "source"
        source.mkdir(parents=True)
        (source / "pack.toml").write_text(
            'name = "Demo"\n[versions]\nminecraft = "1.21.1"\n'
            'neoforge = "21.1.234"\n',
            encoding="utf-8",
        )
        (source / "index.toml").write_text(
            'hash-format = "sha256"\n',
            encoding="utf-8",
        )
        (source.parent / "pack.yaml").write_text("id: demo\n", encoding="utf-8")
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

    def tearDown(self) -> None:
        self.stack.close()
        self.temp.cleanup()

    def transaction(self) -> core.PackTransaction:
        return core.PackTransaction.create("pack:demo")

    def test_immediate_discard_is_bounded_idempotent_and_exactly_once(self) -> None:
        transaction = self.transaction()
        lock = transaction._project_lock
        self.assertIsNotNone(lock)
        assert lock is not None
        with (
            patch.object(lock, "release", wraps=lock.release) as release,
            patch.object(
                transaction,
                "_finish_discard_once",
                wraps=transaction._finish_discard_once,
            ) as finish,
        ):
            transaction.discard()
            transaction.discard()

        self.assertFalse(transaction.root.exists())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))
        self.assertEqual(release.call_count, 1)
        self.assertEqual(finish.call_count, 1)
        self.assertIsNone(transaction.discard_error)

    def test_concurrent_begin_discard_uses_one_non_daemon_worker(self) -> None:
        transaction = self.transaction()
        active = _DelayedOperation()
        transaction._operation = active

        first = transaction.begin_discard(deadline=time.monotonic() + 1)
        second = transaction.begin_discard(deadline=time.monotonic() + 1)
        self.assertIs(first, second)
        first.start()
        second.start()
        self.assertIsNotNone(first._thread)
        assert first._thread is not None
        self.assertFalse(first._thread.daemon)
        self.assertTrue(active.cancelled.wait(1))
        self.assertTrue(transaction.root.is_dir())
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))

        active.done.set()
        self.assertTrue(first.wait(1))
        first.raise_for_error()
        self.assertFalse(transaction.root.exists())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_concurrent_synchronous_discard_finalizes_once(self) -> None:
        transaction = self.transaction()
        active = _DelayedOperation()
        transaction._operation = active
        lock = transaction._project_lock
        self.assertIsNotNone(lock)
        assert lock is not None
        errors: list[BaseException] = []
        deadline = time.monotonic() + 1

        def discard() -> None:
            try:
                transaction.discard(deadline=deadline)
            except BaseException as error:
                errors.append(error)

        with (
            patch.object(lock, "release", wraps=lock.release) as release,
            patch.object(
                transaction,
                "_finish_discard_once",
                wraps=transaction._finish_discard_once,
            ) as finish,
        ):
            workers = [threading.Thread(target=discard) for _ in range(2)]
            for worker in workers:
                worker.start()
            self.assertTrue(active.cancelled.wait(1))
            active.done.set()
            for worker in workers:
                worker.join(1)

        self.assertEqual(errors, [])
        self.assertEqual(finish.call_count, 1)
        self.assertEqual(release.call_count, 1)

    def test_timeout_retains_root_and_lock_then_retry_succeeds(self) -> None:
        transaction = self.transaction()
        active = _DelayedOperation()
        transaction._operation = active

        operation = transaction.begin_discard(deadline=time.monotonic() + 0.02)
        operation.run()
        self.assertTrue(operation.done.is_set())
        with self.assertRaises(core.TransactionDiscardTimeout):
            operation.raise_for_error()
        self.assertIs(transaction.discard_error, operation.error)
        self.assertTrue(transaction.root.is_dir())
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))

        active.done.set()
        retry = transaction.retry_discard(deadline=time.monotonic() + 1)
        self.assertIsNot(retry, operation)
        self.assertTrue(retry.wait(1))
        retry.raise_for_error()
        self.assertFalse(transaction.root.exists())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_rmtree_failure_is_observable_and_retryable(self) -> None:
        transaction = self.transaction()
        original_rmtree = shutil.rmtree
        with patch.object(core.shutil, "rmtree", side_effect=OSError("disk busy")):
            operation = transaction.begin_discard()
            operation.run()

        with self.assertRaisesRegex(core.TransactionDiscardIntegrityError, "disk busy"):
            operation.raise_for_error()
        self.assertTrue(transaction.root.is_dir())
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))

        with patch.object(core.shutil, "rmtree", side_effect=original_rmtree):
            retry = transaction.retry_discard(deadline=time.monotonic() + 1)
            self.assertTrue(retry.wait(1))
            retry.raise_for_error()
        self.assertFalse(transaction.root.exists())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_incomplete_pty_cleanup_fails_closed(self) -> None:
        transaction = self.transaction()
        incomplete = core.ProcessTerminationResult(False, False, True)
        complete = core.ProcessTerminationResult(True, True, True)

        class _Session:
            def __init__(self) -> None:
                self.results = iter((incomplete, complete))

            def cancel(self, *, deadline=None):
                return next(self.results)

        operation = object.__new__(core.PackwizAddOperation)
        operation.done = threading.Event()
        operation.done.set()
        operation.cancelled = False
        operation.cancel_event = threading.Event()
        operation.session = _Session()
        operation.termination_result = incomplete
        operation.cleanup_error = None
        operation.termination_incomplete = True
        transaction._operation = operation

        discard = transaction.begin_discard()
        discard.run()
        with self.assertRaisesRegex(
            core.TransactionDiscardIntegrityError,
            "process-group cleanup was incomplete",
        ):
            discard.raise_for_error()
        self.assertTrue(transaction.root.is_dir())
        self.assertTrue(packctl.project_lock_is_active("pack:demo"))

        retry = transaction.retry_discard(deadline=time.monotonic() + 1)
        self.assertTrue(retry.wait(1))
        retry.raise_for_error()

    def test_replaced_source_is_retained_after_successful_discard(self) -> None:
        transaction = self.transaction()
        replaced = transaction.root / "replaced-source"
        replaced.mkdir()
        (replaced / "pack.toml").write_text("backup", encoding="utf-8")

        transaction.discard()

        self.assertTrue(transaction.root.is_dir())
        self.assertTrue((transaction.root / ".completed").is_file())
        self.assertTrue((replaced / "pack.toml").is_file())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))

    def test_discard_after_successful_apply_is_noop_and_retains_backup(self) -> None:
        transaction = self.transaction()
        transaction.apply(refresh=False)
        replaced = transaction.root / "replaced-source"

        transaction.discard()

        self.assertTrue(replaced.is_dir())
        self.assertTrue((transaction.root / ".completed").is_file())
        self.assertFalse(packctl.project_lock_is_active("pack:demo"))


if __name__ == "__main__":
    unittest.main()
