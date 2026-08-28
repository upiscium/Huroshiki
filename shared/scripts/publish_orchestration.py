"""Typed Core orchestration for the complete hardened Publish pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PurePosixPath
import threading
import time
from typing import Callable, Literal

import packctl
from pack_publish import (
    PackPublishCancelled as ManifestCancelled,
    PackPublishDeadlineExceeded as ManifestDeadlineExceeded,
    PackPublishError as ManifestError,
    PackPublishManifest,
    plan_pack_publish_manifest,
)
from publish_activation import (
    PublishActivatedGeneration,
    PublishActivationCleanupError,
    PublishActivationError,
    PublishActivationUncertainError,
    PublishSemanticVerification,
    PublishSemanticVerificationUncertainError,
    activate_publish_generation,
    retry_publish_activation_cleanup,
    verify_publish_generation,
)
from publish_restart import (
    PublishRestartCancelled,
    PublishRestartDeadlineExceeded,
    PublishRestartError,
    PublishRestartIntegrityError,
    PublishRestartResult,
    restart_activated_publish,
)
from publish_target import (
    LEGACY_SERVER_ID,
    PublishRemoteTarget,
    PublishTargetError,
    publish_remote_target_from_legacy_settings,
)
from publish_transfer import (
    PublishStagedGeneration,
    PublishTransferPlan,
    PublishTransferProgress,
    PublishTransferCleanupError,
    PublishTransferUncertainError,
    compute_publish_generation_id,
    discard_publish_transfer_plan,
    execute_publish_transfer,
    prepare_publish_transfer,
    retry_discard_publish_transfer_plan,
)


PublishRestartStatus = Literal["not_started", "succeeded", "failed", "uncertain"]
PackPublishFinalStatus = Literal[
    "published",
    "publication_failed",
    "restart_failed",
    "restart_uncertain",
    "restart_not_started",
    "cancelled",
    "cleanup_pending",
]

_OPERATION_TIMEOUT_SECONDS = 600.0
_CLEANUP_TIMEOUT_SECONDS = 30.0
_PLAN_TOKEN = object()


@dataclass(frozen=True)
class PackPublishProgress:
    phase: str
    detail: str | None = None
    completed_files: int | None = None
    total_files: int | None = None
    completed_bytes: int | None = None
    total_bytes: int | None = None
    current_path: PurePosixPath | None = None


@dataclass(frozen=True)
class PackPublishResult:
    pack_id: str
    target_side: str
    manifest_digest: str
    target_config_digest: str
    generation_id: str
    publication_succeeded: bool
    remote_verified: bool
    activated: bool
    restart_attempted: bool
    restart_succeeded: bool
    restart_status: PublishRestartStatus
    final_status: PackPublishFinalStatus

    def __post_init__(self) -> None:
        if self.restart_status not in {
            "not_started",
            "succeeded",
            "failed",
            "uncertain",
        }:
            raise ValueError("invalid Pack Publish restart status")
        if self.final_status not in {
            "published",
            "publication_failed",
            "restart_failed",
            "restart_uncertain",
            "restart_not_started",
            "cancelled",
            "cleanup_pending",
        }:
            raise ValueError("invalid Pack Publish final status")
        if self.publication_succeeded != self.activated:
            raise ValueError("Pack Publish activation and publication disagree")
        if self.activated and not self.remote_verified:
            raise ValueError("Pack Publish activation requires verification")
        if self.restart_status == "not_started":
            if self.restart_attempted or self.restart_succeeded:
                raise ValueError("Pack Publish restart was not started")
        elif not self.restart_attempted:
            raise ValueError("Pack Publish restart status requires an attempt")
        if self.restart_succeeded != (self.restart_status == "succeeded"):
            raise ValueError("Pack Publish restart result is inconsistent")
        if self.restart_attempted and not self.activated:
            raise ValueError("Pack Publish restart requires activation")
        if self.final_status == "published" and not (
            self.publication_succeeded and self.restart_succeeded
        ):
            raise ValueError("published Pack Publish result is incomplete")
        if self.final_status == "publication_failed" and self.publication_succeeded:
            raise ValueError("publication failure cannot contain activation success")
        if self.final_status == "restart_failed" and not self.publication_succeeded:
            raise ValueError("restart failure requires successful publication")
        if self.final_status == "restart_uncertain" and (
            not self.publication_succeeded or self.restart_status != "uncertain"
        ):
            raise ValueError("restart uncertainty requires successful publication")
        if self.final_status == "restart_not_started" and (
            not self.publication_succeeded
            or self.restart_status != "not_started"
            or self.restart_attempted
        ):
            raise ValueError("restart-not-started requires successful publication")


class PackPublishExecutionError(RuntimeError):
    """A Publish operation failed while retaining its immutable partial result."""

    def __init__(
        self,
        message: str,
        *,
        result: PackPublishResult | None,
        phase: str,
        primary_error: BaseException | None,
        cleanup_error: BaseException | None = None,
        plan: PackPublishPlan | None = None,
    ) -> None:
        super().__init__(message)
        self.result = result
        self.phase = phase
        self.primary_error = primary_error
        self.cleanup_error = cleanup_error
        self.plan = plan

    @property
    def publication_succeeded(self) -> bool:
        return self.result is not None and self.result.publication_succeeded


class PackPublishCancelled(PackPublishExecutionError):
    """The operation was cancelled without weakening completed publication facts."""


class PackPublishDeadlineExceeded(PackPublishExecutionError):
    """The operation deadline expired without weakening completed publication facts."""


class PackPublishCleanupError(PackPublishExecutionError):
    """Transfer-plan cleanup is pending and remains owned by ``plan``."""


class PackPublishRestartError(PackPublishExecutionError):
    """Publication succeeded, but restart did not succeed."""


class PackPublishRestartUncertainError(PackPublishRestartError):
    """Publication succeeded, but restart may or may not have occurred."""


class PackPublishPlan:
    """Opaque one-shot owner of a fixed manifest, target, and cleanup lifecycle."""

    def __init__(
        self,
        *,
        pack_id: str,
        manifest: PackPublishManifest,
        target: PublishRemoteTarget,
        cancel_event: threading.Event,
        deadline: float,
        token: object,
    ) -> None:
        if token is not _PLAN_TOKEN:
            raise TypeError("PackPublishPlan must be created by plan_pack_publish")
        self._pack_id = pack_id
        self._manifest = manifest
        self._target = target
        self._generation_id = compute_publish_generation_id(manifest, target)
        self._cancel_event = cancel_event
        self._deadline = deadline
        self._state = "planned"
        self._transfer_plan: PublishTransferPlan | None = None
        self._result: PackPublishResult | None = None
        self._terminal_result: PackPublishResult | None = None
        self._primary_error: BaseException | None = None
        self._cleanup_error: BaseException | None = None
        self._activation_cleanup_error: PublishActivationError | None = None
        self._activation_staged: PublishStagedGeneration | None = None
        self._transfer_cleanup_pending = False
        self._terminal_state = "completed"
        self._lock = threading.RLock()

    @property
    def pack_id(self) -> str:
        return self._pack_id

    @property
    def target_side(self) -> str:
        return self._manifest.target_side

    @property
    def manifest(self) -> PackPublishManifest:
        return self._manifest

    @property
    def target(self) -> PublishRemoteTarget:
        return self._target

    @property
    def manifest_digest(self) -> str:
        return self._manifest.manifest_digest

    @property
    def source_snapshot_digest(self) -> str:
        return self._manifest.source_snapshot_digest

    @property
    def target_config_digest(self) -> str:
        return self._target.config_digest

    @property
    def generation_id(self) -> str:
        return self._generation_id

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def result(self) -> PackPublishResult | None:
        with self._lock:
            return self._result

    def __repr__(self) -> str:
        return (
            "PackPublishPlan("
            f"pack_id={self.pack_id!r}, target_side={self.target_side!r}, "
            f"manifest_digest={self.manifest_digest!r}, "
            f"target_config_digest={self.target_config_digest!r}, "
            f"generation_id={self.generation_id!r}, state={self.state!r})"
        )


def _emit(
    progress: Callable[[PackPublishProgress], object] | None,
    event: PackPublishProgress,
) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception:
        pass


def _phase_progress(
    progress: Callable[[PackPublishProgress], object] | None,
    phase: str,
) -> Callable[[PublishTransferProgress | str], None]:
    def emit(value: PublishTransferProgress | str) -> None:
        if isinstance(value, PublishTransferProgress):
            event = PackPublishProgress(
                phase,
                value.phase,
                value.completed_files,
                value.total_files,
                value.completed_bytes,
                value.total_bytes,
                value.current_path,
            )
        else:
            event = PackPublishProgress(phase, str(value))
        _emit(progress, event)

    return emit


def _checkpoint(cancel_event: threading.Event, deadline: float) -> None:
    if cancel_event.is_set():
        raise PackPublishCancelled(
            "Pack Publish was cancelled",
            result=None,
            phase="checkpoint",
            primary_error=None,
        )
    if time.monotonic() >= deadline:
        raise PackPublishDeadlineExceeded(
            "Pack Publish deadline exceeded",
            result=None,
            phase="checkpoint",
            primary_error=None,
        )


def _resolve_target(pack_id: str, remote_path: str | None) -> PublishRemoteTarget:
    settings = packctl.deployment_settings(pack_id)
    return publish_remote_target_from_legacy_settings(
        rsync_target=settings.rsync_target,
        ssh_host=settings.ssh_host,
        stack_dir=settings.stack_dir,
        service=settings.service,
        server_id=LEGACY_SERVER_ID,
        remote_path=remote_path,
    )


def _partial_result(
    plan: PackPublishPlan,
    *,
    verified: bool,
    activated: bool,
    restart: PublishRestartResult | None = None,
    restart_status: PublishRestartStatus = "not_started",
    final_status: PackPublishFinalStatus = "publication_failed",
) -> PackPublishResult:
    attempted = restart.attempted if restart is not None else restart_status != "not_started"
    succeeded = restart.succeeded if restart is not None else restart_status == "succeeded"
    return PackPublishResult(
        plan.pack_id,
        plan.target_side,
        plan.manifest_digest,
        plan.target_config_digest,
        plan.generation_id,
        activated,
        verified,
        activated,
        attempted,
        succeeded,
        restart_status,
        final_status,
    )


def plan_pack_publish(
    pack_id: str,
    *,
    target_side: str = "server",
    remote_path: str | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[PackPublishProgress], object] | None = None,
) -> PackPublishPlan:
    """Create a network-free immutable Publish authority and operation budget."""

    operation_event = cancel_event if cancel_event is not None else threading.Event()
    operation_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + _OPERATION_TIMEOUT_SECONDS
    )
    _checkpoint(operation_event, operation_deadline)
    _emit(progress, PackPublishProgress("planning"))
    try:
        manifest = plan_pack_publish_manifest(
            pack_id,
            target_side=target_side,
            cancel_event=operation_event,
            deadline=operation_deadline,
            progress=_phase_progress(progress, "planning"),
        )
        _checkpoint(operation_event, operation_deadline)
        target = _resolve_target(pack_id, remote_path)
        _checkpoint(operation_event, operation_deadline)
    except ManifestCancelled as error:
        raise PackPublishCancelled(
            str(error), result=None, phase="planning", primary_error=error
        ) from error
    except ManifestDeadlineExceeded as error:
        raise PackPublishDeadlineExceeded(
            str(error), result=None, phase="planning", primary_error=error
        ) from error
    except (ManifestError, packctl.ConfigError, PublishTargetError) as error:
        raise PackPublishExecutionError(
            "Pack Publish planning failed",
            result=None,
            phase="planning",
            primary_error=error,
        ) from error
    _emit(progress, PackPublishProgress("validated"))
    return PackPublishPlan(
        pack_id=pack_id,
        manifest=manifest,
        target=target,
        cancel_event=operation_event,
        deadline=operation_deadline,
        token=_PLAN_TOKEN,
    )


def _classify_failure(
    plan: PackPublishPlan,
    error: Exception,
    *,
    phase: str,
    verified: bool,
    activated: bool,
) -> tuple[PackPublishResult, PackPublishExecutionError]:
    if isinstance(error, PublishRestartIntegrityError):
        result = _partial_result(
            plan,
            verified=verified,
            activated=activated,
            restart=error.result,
            restart_status="uncertain",
            final_status="restart_uncertain",
        )
        return result, PackPublishRestartUncertainError(
            "Pack publication succeeded, but restart outcome is uncertain",
            result=result,
            phase=phase,
            primary_error=error,
        )
    if isinstance(error, PublishRestartCancelled):
        result = _partial_result(
            plan,
            verified=verified,
            activated=activated,
            final_status="cancelled",
        )
        return result, PackPublishCancelled(
            str(error), result=result, phase=phase, primary_error=error
        )
    if isinstance(error, PublishRestartDeadlineExceeded):
        result = _partial_result(
            plan,
            verified=verified,
            activated=activated,
            final_status="cancelled",
        )
        return result, PackPublishDeadlineExceeded(
            str(error), result=result, phase=phase, primary_error=error
        )
    if isinstance(error, PublishRestartError):
        result = _partial_result(
            plan,
            verified=verified,
            activated=activated,
            final_status="restart_failed",
        )
        return result, PackPublishRestartError(
            "Pack publication succeeded, but restart failed",
            result=result,
            phase=phase,
            primary_error=error,
        )

    uncertain = isinstance(
        error,
        (
            PublishTransferUncertainError,
            PublishSemanticVerificationUncertainError,
            PublishActivationUncertainError,
        ),
    )
    if not uncertain and plan.cancel_event.is_set():
        result = _partial_result(
            plan,
            verified=verified,
            activated=activated,
            final_status="cancelled",
        )
        return result, PackPublishCancelled(
            str(error), result=result, phase=phase, primary_error=error
        )
    if not uncertain and time.monotonic() >= plan.deadline:
        result = _partial_result(
            plan,
            verified=verified,
            activated=activated,
            final_status="cancelled",
        )
        return result, PackPublishDeadlineExceeded(
            str(error), result=result, phase=phase, primary_error=error
        )
    result = _partial_result(
        plan,
        verified=verified,
        activated=activated,
        final_status=("restart_failed" if activated else "publication_failed"),
    )
    error_type = PackPublishRestartError if activated else PackPublishExecutionError
    return result, error_type(
        "Pack Publish execution failed",
        result=result,
        phase=phase,
        primary_error=error,
    )


def _set_terminal(
    plan: PackPublishPlan,
    result: PackPublishResult,
    error: PackPublishExecutionError | None,
) -> None:
    with plan._lock:
        plan._result = result
        plan._terminal_result = result
        plan._primary_error = error.primary_error if error is not None else None
        if error is None:
            plan._terminal_state = "completed"
        elif isinstance(error, (PackPublishCancelled, PackPublishDeadlineExceeded)):
            plan._terminal_state = "cancelled"
        else:
            plan._terminal_state = "failed"


def execute_pack_publish(
    plan: PackPublishPlan,
    *,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    progress: Callable[[PackPublishProgress], object] | None = None,
) -> PackPublishResult:
    """Execute every hardened phase once and discharge transfer ownership."""

    if type(plan) is not PackPublishPlan:
        raise TypeError("execute_pack_publish requires a PackPublishPlan")
    operation_event = plan.cancel_event if cancel_event is None else cancel_event
    operation_deadline = plan.deadline if deadline is None else deadline
    if operation_event is not plan.cancel_event or operation_deadline != plan.deadline:
        raise ValueError("Pack Publish execution controls do not match the plan")
    with plan._lock:
        if plan._state != "planned":
            raise PackPublishExecutionError(
                "PackPublishPlan is one-shot",
                result=plan._result,
                phase="execution",
                primary_error=None,
                plan=plan if plan._state == "cleanup-pending" else None,
            )
        plan._state = "executing"

    transfer_plan: PublishTransferPlan | None = None
    staged: PublishStagedGeneration | None = None
    verification: PublishSemanticVerification | None = None
    activated_token: PublishActivatedGeneration | None = None
    phase = "preparing-transfer"
    result: PackPublishResult
    outcome_error: PackPublishExecutionError | None = None
    interrupt: BaseException | None = None

    try:
        _checkpoint(operation_event, operation_deadline)
        _emit(progress, PackPublishProgress(phase))
        transfer_plan = prepare_publish_transfer(
            plan.pack_id,
            plan.manifest,
            plan.target,
            cancel_event=operation_event,
            deadline=operation_deadline,
            progress=_phase_progress(progress, phase),
        )
        with plan._lock:
            plan._transfer_plan = transfer_plan

        phase = "transferring"
        _checkpoint(operation_event, operation_deadline)
        _emit(progress, PackPublishProgress(phase))
        staged = execute_publish_transfer(
            transfer_plan,
            cancel_event=operation_event,
            deadline=operation_deadline,
            progress=_phase_progress(progress, phase),
        )

        phase = "verifying"
        _checkpoint(operation_event, operation_deadline)
        _emit(progress, PackPublishProgress(phase))
        verification = verify_publish_generation(
            staged,
            plan.manifest,
            plan.target,
            cancel_event=operation_event,
            deadline=operation_deadline,
            progress=_phase_progress(progress, phase),
        )

        phase = "activating"
        _checkpoint(operation_event, operation_deadline)
        _emit(progress, PackPublishProgress(phase))
        activated_token = activate_publish_generation(
            staged,
            verification,
            plan.target,
            manifest=plan.manifest,
            cancel_event=operation_event,
            deadline=operation_deadline,
            progress=_phase_progress(progress, phase),
        )

        phase = "restarting"
        _checkpoint(operation_event, operation_deadline)
        _emit(progress, PackPublishProgress(phase))
        restart = restart_activated_publish(
            activated_token,
            plan.manifest,
            plan.target,
            cancel_event=operation_event,
            deadline=operation_deadline,
            progress=_phase_progress(progress, phase),
        )
        if restart.status == "succeeded":
            result = _partial_result(
                plan,
                verified=True,
                activated=True,
                restart=restart,
                restart_status="succeeded",
                final_status="published",
            )
        elif restart.status == "failed":
            result = _partial_result(
                plan,
                verified=True,
                activated=True,
                restart=restart,
                restart_status="failed",
                final_status="restart_failed",
            )
            outcome_error = PackPublishRestartError(
                "Pack publication succeeded, but restart failed",
                result=result,
                phase=phase,
                primary_error=None,
            )
        else:
            result = _partial_result(
                plan,
                verified=True,
                activated=True,
                restart=restart,
                restart_status="uncertain",
                final_status="restart_uncertain",
            )
            outcome_error = PackPublishRestartUncertainError(
                "Pack publication succeeded, but restart outcome is uncertain",
                result=result,
                phase=phase,
                primary_error=None,
            )
    except BaseException as error:
        if not isinstance(error, Exception):
            interrupt = error
            result = _partial_result(
                plan,
                verified=verification is not None,
                activated=activated_token is not None,
                restart_status=(
                    "uncertain"
                    if phase == "restarting" and activated_token is not None
                    else "not_started"
                ),
                final_status=(
                    "restart_uncertain"
                    if phase == "restarting" and activated_token is not None
                    else "cancelled"
                ),
            )
        else:
            classification_error = error
            retained_interrupt: BaseException | None = None
            if (
                transfer_plan is None
                and isinstance(error, PublishTransferCleanupError)
                and error.plan is not None
            ):
                transfer_plan = error.plan
                with plan._lock:
                    plan._transfer_plan = transfer_plan
                if isinstance(error.primary_error, Exception):
                    classification_error = error.primary_error
                elif error.primary_error is not None:
                    retained_interrupt = error.primary_error
            if retained_interrupt is not None:
                interrupt = retained_interrupt
                result = _partial_result(
                    plan,
                    verified=verification is not None,
                    activated=activated_token is not None,
                    final_status="cancelled",
                )
            elif isinstance(error, PublishActivationCleanupError) and error.activated is not None:
                activated_token = error.activated
                with plan._lock:
                    plan._activation_cleanup_error = error
                    plan._activation_staged = staged
                result = _partial_result(
                    plan,
                    verified=verification is not None,
                    activated=True,
                    final_status="restart_not_started",
                )
                outcome_error = PackPublishExecutionError(
                    "Pack publication activated, but activation cleanup is pending",
                    result=result,
                    phase=phase,
                    primary_error=error,
                    plan=plan,
                )
            else:
                if (
                    isinstance(error, PublishActivationUncertainError)
                    and error.recovery_path is not None
                    and error.operation_id is not None
                    and staged is not None
                ):
                    with plan._lock:
                        plan._activation_cleanup_error = error
                        plan._activation_staged = staged
                result, outcome_error = _classify_failure(
                    plan,
                    classification_error,
                    phase=phase,
                    verified=verification is not None,
                    activated=activated_token is not None,
                )

    _set_terminal(plan, result, outcome_error)
    if interrupt is not None:
        with plan._lock:
            plan._primary_error = interrupt
            plan._terminal_state = "failed"
    cleanup_error: BaseException | None = None
    transfer_cleanup_error: BaseException | None = None
    if transfer_plan is not None:
        _emit(progress, PackPublishProgress("cleanup"))
        try:
            discard_publish_transfer_plan(
                transfer_plan,
                deadline=time.monotonic() + _CLEANUP_TIMEOUT_SECONDS,
            )
        except BaseException as error:
            cleanup_error = error
            transfer_cleanup_error = error

    activation_cleanup_error = plan._activation_cleanup_error
    if activation_cleanup_error is not None and cleanup_error is None:
        cleanup_error = activation_cleanup_error

    if cleanup_error is not None:
        cleanup_result = replace(result, final_status="cleanup_pending")
        primary_error = (
            outcome_error.primary_error
            if outcome_error is not None and outcome_error.primary_error is not None
            else (outcome_error if outcome_error is not None else interrupt)
        )
        cleanup_exception = PackPublishCleanupError(
            "Pack Publish cleanup is pending",
            result=cleanup_result,
            phase="cleanup",
            primary_error=primary_error,
            cleanup_error=cleanup_error,
            plan=plan,
        )
        with plan._lock:
            plan._state = "cleanup-pending"
            plan._result = cleanup_result
            plan._cleanup_error = cleanup_error
            plan._transfer_cleanup_pending = transfer_cleanup_error is not None
        _emit(progress, PackPublishProgress("cleanup-pending"))
        if primary_error is not None:
            raise cleanup_exception from primary_error
        raise cleanup_exception from cleanup_error

    with plan._lock:
        plan._state = plan._terminal_state
    if interrupt is not None:
        raise interrupt
    if outcome_error is not None:
        terminal_phase = (
            "cancelled"
            if isinstance(outcome_error, (PackPublishCancelled, PackPublishDeadlineExceeded))
            else "failed"
        )
        _emit(progress, PackPublishProgress(terminal_phase))
        if outcome_error.primary_error is not None:
            raise outcome_error from outcome_error.primary_error
        raise outcome_error

    _emit(progress, PackPublishProgress("published"))
    return result


def retry_pack_publish_cleanup(
    plan: PackPublishPlan,
    *,
    deadline: float | None = None,
    progress: Callable[[PackPublishProgress], object] | None = None,
) -> None:
    """Retry only retained transfer cleanup; never repeat a Publish phase."""

    if type(plan) is not PackPublishPlan:
        raise TypeError("retry_pack_publish_cleanup requires a PackPublishPlan")
    with plan._lock:
        if plan._state != "cleanup-pending" or plan._transfer_plan is None:
            raise PackPublishCleanupError(
                "Pack Publish cleanup is not pending",
                result=plan._result,
                phase="cleanup",
                primary_error=plan._primary_error,
                cleanup_error=plan._cleanup_error,
                plan=plan,
            )
        plan._state = "cleaning"
        transfer_plan = plan._transfer_plan
    _emit(progress, PackPublishProgress("cleanup"))
    cleanup_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + _CLEANUP_TIMEOUT_SECONDS
    )
    retry_error: BaseException | None = None
    activation_cleanup = plan._activation_cleanup_error
    if activation_cleanup is not None:
        staged = plan._activation_staged
        if staged is None or activation_cleanup.operation_id is None:
            retry_error = PackPublishCleanupError(
                "retained activation cleanup authority is incomplete",
                result=plan.result,
                phase="cleanup",
                primary_error=plan._primary_error,
                cleanup_error=activation_cleanup,
                plan=plan,
            )
        else:
            try:
                retry_publish_activation_cleanup(
                    staged,
                    plan.manifest,
                    plan.target,
                    activation_cleanup.operation_id,
                    deadline=cleanup_deadline,
                    finalize_receipt=(
                        isinstance(activation_cleanup, PublishActivationCleanupError)
                        and activation_cleanup.activated is not None
                    ),
                    expected_status=(
                        activation_cleanup.expected_status
                        if isinstance(
                            activation_cleanup, PublishActivationCleanupError
                        )
                        and activation_cleanup.activated is not None
                        else None
                    ),
                )
            except BaseException as error:
                retry_error = error
            else:
                with plan._lock:
                    confirmed = (
                        isinstance(activation_cleanup, PublishActivationCleanupError)
                        and activation_cleanup.activated is not None
                    )
                    if confirmed:
                        plan._activation_cleanup_error = None
                        plan._activation_staged = None
                    else:
                        retry_error = activation_cleanup
    if plan._transfer_cleanup_pending:
        try:
            retry_discard_publish_transfer_plan(
                transfer_plan,
                deadline=cleanup_deadline,
            )
        except BaseException as error:
            if retry_error is None:
                retry_error = error
        else:
            with plan._lock:
                plan._transfer_cleanup_pending = False
    if retry_error is not None:
        with plan._lock:
            plan._state = "cleanup-pending"
            plan._cleanup_error = retry_error
        _emit(progress, PackPublishProgress("cleanup-pending"))
        cleanup_exception = PackPublishCleanupError(
            "Pack Publish cleanup is still pending",
            result=plan.result,
            phase="cleanup",
            primary_error=plan._primary_error,
            cleanup_error=retry_error,
            plan=plan,
        )
        if plan._primary_error is not None:
            raise cleanup_exception from plan._primary_error
        raise cleanup_exception from retry_error
    with plan._lock:
        plan._state = plan._terminal_state
        plan._result = plan._terminal_result
        plan._cleanup_error = None
        terminal_result = plan._result
    if terminal_result is not None and terminal_result.final_status == "published":
        _emit(progress, PackPublishProgress("published"))
