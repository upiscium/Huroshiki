from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from content_workers import ContentWorker


class ContentWorkerTest(unittest.TestCase):
    def test_named_non_daemon_worker_returns_result(self) -> None:
        observed: dict[str, object] = {}

        def target(cancel_event: threading.Event, deadline: float) -> str:
            thread = threading.current_thread()
            observed.update(
                name=thread.name,
                daemon=thread.daemon,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            return "result"

        worker = ContentWorker("huroshiki-content-test", target)
        worker.start()
        self.assertTrue(worker.done.wait(1))
        worker.raise_for_error()
        self.assertEqual(worker.result, "result")
        self.assertEqual(observed["name"], "huroshiki-content-test")
        self.assertFalse(observed["daemon"])
        self.assertIs(observed["cancel_event"], worker.cancel_event)
        self.assertEqual(observed["deadline"], worker.deadline)

    def test_cancel_deadline_error_and_bounded_wait(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def target(cancel_event: threading.Event, deadline: float) -> None:
            started.set()
            while not cancel_event.is_set() and time.monotonic() < deadline:
                release.wait(0.01)
            if cancel_event.is_set():
                raise RuntimeError("cancelled")

        deadline = time.monotonic() + 10
        worker = ContentWorker("huroshiki-content-cancel", target, deadline=deadline)
        worker.start()
        self.assertTrue(started.wait(1))
        self.assertFalse(worker.wait(time.monotonic()))
        worker.cancel()
        self.assertTrue(worker.wait(time.monotonic() + 1))
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            worker.raise_for_error()

    def test_duplicate_start_and_thread_start_failure_are_observable(self) -> None:
        worker = ContentWorker("huroshiki-content-once", lambda *_: None)
        worker.start()
        self.assertTrue(worker.done.wait(1))
        with self.assertRaisesRegex(RuntimeError, "already been started"):
            worker.start()

        failed = ContentWorker("huroshiki-content-failed", lambda *_: None)
        with patch.object(
            threading.Thread,
            "start",
            side_effect=RuntimeError("cannot start"),
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot start"):
                failed.start()
        self.assertTrue(failed.done.is_set())
        self.assertIsNone(failed.thread)
        with self.assertRaisesRegex(RuntimeError, "cannot start"):
            failed.raise_for_error()
