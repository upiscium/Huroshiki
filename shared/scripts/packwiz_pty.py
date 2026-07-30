#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
import base64
import errno
import fcntl
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from typing import Callable

from packwiz_parser import PackwizOutputParser, ParserEvent
from process_runner import (
    PROCESS_KILL_GRACE_SECONDS,
    PROCESS_POLL_SECONDS,
    PROCESS_REAP_GRACE_SECONDS,
    PROCESS_TERMINATE_GRACE_SECONDS,
    ProcessTerminationResult,
    live_process_group_members,
    stop_process_group,
)


EventCallback = Callable[[ParserEvent], None]
PTY_INTERRUPT_GRACE_SECONDS = 1.5


@dataclass(frozen=True)
class PtyResult:
    returncode: int
    raw_log: Path
    event_log: Path
    text_log: Path
    normalized_text: str
    termination_result: ProcessTerminationResult | None = None
    cancelled: bool = False
    timed_out: bool = False
    orphaned_descendants: bool = False
    termination_incomplete: bool = False


class PackwizPtySession:
    """Run one interactive Packwiz command behind a PTY proxy.

    The caller owns the user interface. This class owns the PTY master from
    process creation, parses output, accepts selections through ``send_line``
    and records output-only diagnostics. Success must still be decided from the
    exit status and Packwiz metadata changes by the transaction layer.
    """

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        log_dir: Path,
        on_event: EventCallback | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.log_dir = log_dir
        self.on_event = on_event
        self.parser = PackwizOutputParser()
        self.process: subprocess.Popen[bytes] | None = None
        self.master_fd: int | None = None
        self._write_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._termination_lock = threading.Lock()
        self._termination_result: ProcessTerminationResult | None = None
        self._cancel_deadline: float | None = None

    def run(self, *, deadline: float | None = None) -> PtyResult:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        raw_log = self.log_dir / "session.raw"
        event_log = self.log_dir / "session.jsonl"
        text_log = self.log_dir / "session.txt"

        if self._cancelled.is_set():
            raw_log.touch()
            event_log.touch()
            text_log.write_text("", encoding="utf-8")
            return PtyResult(
                -signal.SIGINT,
                raw_log,
                event_log,
                text_log,
                "",
                cancelled=True,
            )
        if deadline is not None and time.monotonic() >= deadline:
            raw_log.touch()
            event_log.touch()
            text_log.write_text("", encoding="utf-8")
            return PtyResult(
                -signal.SIGTERM,
                raw_log,
                event_log,
                text_log,
                "",
                timed_out=True,
            )

        master_fd, slave_fd = os.openpty()
        self.master_fd = master_fd
        self._set_window_size(slave_fd)

        try:
            try:
                self.process = subprocess.Popen(
                    self.command,
                    cwd=self.cwd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    close_fds=True,
                    start_new_session=True,
                )
            finally:
                os.close(slave_fd)
        except Exception:
            os.close(master_fd)
            self.master_fd = None
            raise

        os.set_blocking(master_fd, False)
        selector = selectors.DefaultSelector()
        selector.register(master_fd, selectors.EVENT_READ)
        orphaned_descendants = False
        termination_incomplete = False
        timed_out = False
        try:
            with (
                raw_log.open("wb") as raw_handle,
                event_log.open("w", encoding="utf-8") as event_handle,
            ):
                while True:
                    if self._cancelled.is_set():
                        deadline = self._cancel_deadline
                        if deadline is None:
                            deadline = self._default_cleanup_deadline()
                        termination = self.cancel(deadline=deadline)
                        if termination is not None:
                            termination_incomplete = not (
                                termination.group_drained and termination.parent_reaped
                            )
                            if termination_incomplete:
                                break
                    elif deadline is not None and time.monotonic() >= deadline:
                        timed_out = True
                        self._termination_result = stop_process_group(
                            self.process.pid,
                            parent=self.process,
                            cleanup_deadline=deadline,
                        )
                        termination_incomplete = not (
                            self._termination_result.group_drained
                            and self._termination_result.parent_reaped
                        )
                        break

                    read_any = False
                    for key, _ in selector.select(timeout=0.1):
                        try:
                            chunk = os.read(key.fd, 65536)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                chunk = b""
                            else:
                                raise

                        if not chunk:
                            try:
                                selector.unregister(key.fd)
                            except Exception:
                                pass
                            break

                        read_any = True
                        raw_handle.write(chunk)
                        raw_handle.flush()
                        self._record_event(event_handle, "output", chunk)
                        self._emit_many(self.parser.feed(chunk))

                    process_done = self.process.poll() is not None
                    if process_done:
                        if live_process_group_members(self.process.pid):
                            orphaned_descendants = True
                            cleanup_deadline = self._default_cleanup_deadline()
                            if deadline is not None:
                                cleanup_deadline = min(cleanup_deadline, deadline)
                            self._termination_result = stop_process_group(
                                self.process.pid,
                                cleanup_deadline=cleanup_deadline,
                            )
                            termination_incomplete = not self._termination_result.group_drained
                        if not selector.get_map():
                            break
                        if not read_any:
                            try:
                                chunk = os.read(master_fd, 65536)
                            except OSError as error:
                                if error.errno == errno.EIO:
                                    chunk = b""
                                else:
                                    raise
                            if chunk:
                                raw_handle.write(chunk)
                                self._record_event(event_handle, "output", chunk)
                                self._emit_many(self.parser.feed(chunk))
                            else:
                                break
                    elif not selector.get_map():
                        cleanup_deadline = self._default_cleanup_deadline()
                        if deadline is not None:
                            cleanup_deadline = min(cleanup_deadline, deadline)
                        self._termination_result = stop_process_group(
                            self.process.pid,
                            parent=self.process,
                            cleanup_deadline=cleanup_deadline,
                        )
                        termination_incomplete = not (
                            self._termination_result.group_drained
                            and self._termination_result.parent_reaped
                        )
                        break
        except BaseException:
            cleanup_deadline = self._default_cleanup_deadline()
            if deadline is not None:
                cleanup_deadline = min(cleanup_deadline, deadline)
            if self._cancel_deadline is not None:
                cleanup_deadline = min(cleanup_deadline, self._cancel_deadline)
            self._termination_result = stop_process_group(
                self.process.pid,
                parent=self.process,
                cleanup_deadline=cleanup_deadline,
            )
            raise
        finally:
            selector.close()
            with self._write_lock:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                self.master_fd = None

        self._emit_many(self.parser.feed(b"", final=True))
        returncode = self.process.poll()
        if returncode is None:
            returncode = -signal.SIGKILL
        normalized_text = "\n".join(self.parser.normalized_lines).rstrip()
        if normalized_text:
            normalized_text += "\n"
        text_log.write_text(normalized_text, encoding="utf-8")
        self._emit(ParserEvent("completed", str(returncode)))

        return PtyResult(
            returncode=returncode,
            raw_log=raw_log,
            event_log=event_log,
            text_log=text_log,
            normalized_text=normalized_text,
            termination_result=self._termination_result,
            cancelled=self._cancelled.is_set(),
            timed_out=timed_out,
            orphaned_descendants=orphaned_descendants,
            termination_incomplete=termination_incomplete,
        )

    def send_line(self, value: str) -> None:
        payload = value.encode("utf-8") + b"\n"
        with self._write_lock:
            if self.master_fd is None:
                raise RuntimeError("Packwiz PTY is not running")
            os.write(self.master_fd, payload)

    @property
    def termination_result(self) -> ProcessTerminationResult | None:
        return self._termination_result

    def cancel(
        self,
        *,
        deadline: float | None = None,
    ) -> ProcessTerminationResult | None:
        self._cancelled.set()
        if deadline is not None:
            if self._cancel_deadline is None:
                self._cancel_deadline = deadline
            else:
                self._cancel_deadline = min(self._cancel_deadline, deadline)
        process = self.process
        if process is None:
            return self._termination_result
        parent_done = process.poll() is not None
        if parent_done:
            members = live_process_group_members(process.pid)
            if deadline is not None and members:
                self._termination_result = stop_process_group(
                    process.pid,
                    cleanup_deadline=deadline,
                )
            elif not members:
                self._termination_result = ProcessTerminationResult(
                    True,
                    True,
                    (
                        self._termination_result.forced
                        if self._termination_result is not None
                        else False
                    ),
                )
            return self._termination_result
        try:
            os.killpg(process.pid, signal.SIGINT)
        except OSError:
            pass
        if deadline is None:
            return self._termination_result
        with self._termination_lock:
            if self._termination_result is not None and (
                self._termination_result.group_drained
                and self._termination_result.parent_reaped
            ):
                return self._termination_result
            interrupt_deadline = min(
                deadline,
                time.monotonic() + PTY_INTERRUPT_GRACE_SECONDS,
            )
            while (
                live_process_group_members(process.pid)
                and time.monotonic() < interrupt_deadline
            ):
                process.poll()
                time.sleep(
                    min(
                        PROCESS_POLL_SECONDS,
                        max(0.0, interrupt_deadline - time.monotonic()),
                    )
                )
            if not live_process_group_members(process.pid):
                self._termination_result = ProcessTerminationResult(
                    True,
                    process.poll() is not None,
                    False,
                )
            else:
                self._termination_result = stop_process_group(
                    process.pid,
                    parent=process,
                    cleanup_deadline=deadline,
                )
            return self._termination_result

    @staticmethod
    def _default_cleanup_deadline() -> float:
        return (
            time.monotonic()
            + PTY_INTERRUPT_GRACE_SECONDS
            + PROCESS_TERMINATE_GRACE_SECONDS
            + PROCESS_KILL_GRACE_SECONDS
            + PROCESS_REAP_GRACE_SECONDS
        )

    def resize(self, columns: int, rows: int) -> None:
        master_fd = self.master_fd
        if master_fd is None:
            return
        winsize = struct.pack("HHHH", max(1, rows), max(1, columns), 0, 0)
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            if self.process is not None and self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def _set_window_size(self, fd: int) -> None:
        size = shutil.get_terminal_size(fallback=(120, 40))
        winsize = struct.pack("HHHH", size.lines, size.columns, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    def _record_event(self, handle, direction: str, data: bytes) -> None:
        record = {
            "ts": time.time(),
            "direction": direction,
            "data_b64": base64.b64encode(data).decode("ascii"),
        }
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()

    def _emit_many(self, events: list[ParserEvent]) -> None:
        for event in events:
            self._emit(event)

    def _emit(self, event: ParserEvent) -> None:
        if self.on_event is not None:
            self.on_event(event)
