from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Sequence


PACKWIZ_PROCESS_TIMEOUT_SECONDS = 120
PACKWIZ_OPERATION_TIMEOUT_SECONDS = 600
PROCESS_POLL_SECONDS = 0.05
PROCESS_TERMINATE_GRACE_SECONDS = 2.0
PROCESS_KILL_GRACE_SECONDS = 2.0
PROCESS_REAP_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    cancelled: bool
    timed_out: bool
    orphaned_descendants: bool = False
    termination_incomplete: bool = False

    @property
    def succeeded(self) -> bool:
        return (
            self.returncode == 0
            and not self.cancelled
            and not self.timed_out
            and not self.orphaned_descendants
            and not self.termination_incomplete
        )


@dataclass(frozen=True)
class ProcessTerminationResult:
    group_drained: bool
    parent_reaped: bool
    forced: bool


@dataclass(frozen=True)
class ProcessGroupMember:
    pid: int
    state: str


def live_process_group_members(
    process_group: int,
) -> tuple[ProcessGroupMember, ...]:
    members: list[ProcessGroupMember] = []
    proc = Path("/proc")
    try:
        entries = tuple(proc.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8")
            closing = text.rfind(") ")
            if closing < 0:
                continue
            suffix = text[closing + 2 :].split()
            state = suffix[0]
            member_group = int(suffix[2])
        except (OSError, ValueError, IndexError):
            continue
        if member_group == process_group and state not in {"Z", "X", "x"}:
            members.append(ProcessGroupMember(int(entry.name), state))
    return tuple(sorted(members, key=lambda member: member.pid))


def stop_process_group(
    process_group: int,
    *,
    parent: subprocess.Popen[bytes] | None = None,
    cleanup_deadline: float,
) -> ProcessTerminationResult:
    forced = False
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    grace_deadline = min(
        cleanup_deadline,
        time.monotonic() + PROCESS_TERMINATE_GRACE_SECONDS,
    )
    while live_process_group_members(process_group) and time.monotonic() < grace_deadline:
        if parent is not None:
            parent.poll()
        time.sleep(
            min(
                PROCESS_POLL_SECONDS,
                max(0.0, grace_deadline - time.monotonic()),
            )
        )
    if live_process_group_members(process_group):
        forced = True
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = min(
            cleanup_deadline,
            time.monotonic() + PROCESS_KILL_GRACE_SECONDS,
        )
        while live_process_group_members(process_group) and time.monotonic() < kill_deadline:
            if parent is not None:
                parent.poll()
            time.sleep(
                min(
                    PROCESS_POLL_SECONDS,
                    max(0.0, kill_deadline - time.monotonic()),
                )
            )
    group_drained = not live_process_group_members(process_group)
    parent_reaped = parent is None
    if parent is not None:
        if parent.poll() is not None:
            parent_reaped = True
        else:
            remaining = min(
                PROCESS_REAP_GRACE_SECONDS,
                max(0.0, cleanup_deadline - time.monotonic()),
            )
            try:
                parent.wait(timeout=remaining)
                parent_reaped = True
            except subprocess.TimeoutExpired:
                parent_reaped = False
    return ProcessTerminationResult(group_drained, parent_reaped, forced)


def _cleanup_deadline(*, include_reap: bool) -> float:
    return (
        time.monotonic()
        + PROCESS_TERMINATE_GRACE_SECONDS
        + PROCESS_KILL_GRACE_SECONDS
        + (PROCESS_REAP_GRACE_SECONDS if include_reap else 0.0)
    )


def run_bounded_process(
    command: Sequence[str],
    *,
    cwd: Path,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
) -> BoundedProcessResult:
    if cancel_event is not None and cancel_event.is_set():
        return BoundedProcessResult(-signal.SIGTERM, "", "", True, False)
    if deadline is not None and time.monotonic() >= deadline:
        return BoundedProcessResult(-signal.SIGTERM, "", "", False, True)
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=stdout_file,
            stderr=stderr_file,
            shell=False,
            start_new_session=True,
        )
        cancelled = False
        timed_out = False
        orphaned_descendants = False
        termination_incomplete = False
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    cleanup = stop_process_group(
                        process.pid,
                        parent=process,
                        cleanup_deadline=_cleanup_deadline(include_reap=True),
                    )
                    termination_incomplete = not (
                        cleanup.group_drained and cleanup.parent_reaped
                    )
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    cleanup = stop_process_group(
                        process.pid,
                        parent=process,
                        cleanup_deadline=_cleanup_deadline(include_reap=True),
                    )
                    termination_incomplete = not (
                        cleanup.group_drained and cleanup.parent_reaped
                    )
                    break
                time.sleep(PROCESS_POLL_SECONDS)
        except BaseException:
            stop_process_group(
                process.pid,
                parent=process,
                cleanup_deadline=_cleanup_deadline(include_reap=True),
            )
            raise
        if not cancelled and not timed_out and live_process_group_members(process.pid):
            orphaned_descendants = True
            cleanup = stop_process_group(
                process.pid,
                cleanup_deadline=_cleanup_deadline(include_reap=False),
            )
            termination_incomplete = not cleanup.group_drained
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
    return BoundedProcessResult(
        process.returncode,
        stdout,
        stderr,
        cancelled,
        timed_out,
        orphaned_descendants,
        termination_incomplete,
    )


def concise_process_output(result: BoundedProcessResult) -> str:
    text = (result.stderr or result.stdout).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:240] if lines else f"exit code {result.returncode}"


def process_failure_message(
    result: BoundedProcessResult,
    *,
    label: str,
) -> str | None:
    if result.termination_incomplete:
        return f"{label} process termination was incomplete"
    if result.orphaned_descendants:
        return f"{label} left background processes after completion"
    if result.cancelled:
        return f"{label} was cancelled"
    if result.timed_out:
        return f"{label} timed out"
    if result.returncode != 0:
        return f"{label} failed: {concise_process_output(result)}"
    return None
