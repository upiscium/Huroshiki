from __future__ import annotations

import threading
import time
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


class ContentWorker(Generic[T]):
    def __init__(
        self,
        name: str,
        target: Callable[[threading.Event, float], T],
        *,
        timeout_seconds: float = 600.0,
        deadline: float | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Content worker timeout must be positive")
        self.name = name
        self.target = target
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + timeout_seconds
        )
        self.result: T | None = None
        self.error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._started = False

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                raise RuntimeError("Content worker has already been started")
            self._started = True
            thread = threading.Thread(
                target=self._run,
                name=self.name,
                daemon=False,
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException as error:
                self._thread = None
                self.error = error
                self.done.set()
                raise

    def _run(self) -> None:
        try:
            self.result = self.target(self.cancel_event, self.deadline)
        except BaseException as error:
            self.error = error
        finally:
            self.done.set()

    def cancel(self) -> None:
        self.cancel_event.set()

    def wait(self, deadline: float) -> bool:
        thread = self._thread
        if thread is None:
            return self.done.is_set()
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(remaining)
        return self.done.is_set() and not thread.is_alive()

    def raise_for_error(self) -> None:
        if self.error is not None:
            raise self.error
