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


EventCallback = Callable[[ParserEvent], None]


@dataclass(frozen=True)
class PtyResult:
    returncode: int
    raw_log: Path
    event_log: Path
    text_log: Path
    normalized_text: str


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

    def run(self) -> PtyResult:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        raw_log = self.log_dir / "session.raw"
        event_log = self.log_dir / "session.jsonl"
        text_log = self.log_dir / "session.txt"

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

        if self._cancelled.is_set():
            try:
                os.killpg(self.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

        os.set_blocking(master_fd, False)
        selector = selectors.DefaultSelector()
        selector.register(master_fd, selectors.EVENT_READ)

        with (
            raw_log.open("wb") as raw_handle,
            event_log.open("w", encoding="utf-8") as event_handle,
        ):
            while True:
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
                if process_done and not selector.get_map():
                    break
                if process_done and not read_any:
                    # PTYs report EOF as EIO, but allow one final drain cycle.
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

        self._emit_many(self.parser.feed(b"", final=True))
        returncode = self.process.wait()
        normalized_text = "\n".join(self.parser.normalized_lines).rstrip() + "\n"
        text_log.write_text(normalized_text, encoding="utf-8")
        self._emit(ParserEvent("completed", str(returncode)))

        try:
            os.close(master_fd)
        finally:
            self.master_fd = None

        return PtyResult(
            returncode=returncode,
            raw_log=raw_log,
            event_log=event_log,
            text_log=text_log,
            normalized_text=normalized_text,
        )

    def send_line(self, value: str) -> None:
        payload = value.encode("utf-8") + b"\n"
        with self._write_lock:
            if self.master_fd is None:
                raise RuntimeError("Packwiz PTY is not running")
            os.write(self.master_fd, payload)

    def cancel(self) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1.5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

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
