"""Orchestration for the two-sided, snapshot-bound publish operation."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PurePosixPath
import threading
import time
from typing import Callable, Literal

import packctl
from pack_publish import (PackPublishCancelled as ManifestCancelled,
    PackPublishDeadlineExceeded as ManifestDeadlineExceeded, PackPublishError as ManifestError,
    PackPublishManifest, PackPublishManifestBundle, plan_pack_publish_manifest_bundle)
from publish_activation import (PublishActivatedGeneration, PublishActivationCleanupError,
    PublishActivationError, PublishActivationUncertainError, PublishSemanticVerification,
    PublishSemanticVerificationUncertainError, activate_publish_generation,
    retry_publish_activation_cleanup, verify_activated_publish_generation,
    verify_publish_generation)
from publish_restart import (PublishRestartCancelled, PublishRestartDeadlineExceeded,
    PublishRestartError, PublishRestartIntegrityError, PublishRestartResult,
    restart_activated_publish)
from publish_target import (LEGACY_SERVER_ID, PublishClientTarget, PublishRemoteTarget,
    PublishTargetError, publish_client_target_from_remote_target,
    publish_remote_target_from_legacy_settings)
from publish_transfer import (PublishStagedGeneration, PublishTransferCleanupError,
    PublishTransferPlan, PublishTransferProgress, PublishTransferUncertainError,
    compute_publish_generation_id, discard_publish_transfer_plan,
    execute_publish_transfer, prepare_publish_transfer, retry_discard_publish_transfer_plan)

PublishRestartStatus = Literal["not_started", "succeeded", "failed", "uncertain"]
PackPublishFinalStatus = Literal["published", "publication_failed", "restart_failed",
    "restart_uncertain", "restart_not_started", "cancelled", "cleanup_pending"]
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
class PublishVariantFacts:
    target_side: Literal["client", "server"]
    transferred: bool = False
    remote_verified: bool = False
    activated: bool = False
    active_verified: bool = False
    cleanup_pending: bool = False

    def __post_init__(self) -> None:
        if self.target_side not in {"client", "server"}:
            raise ValueError("invalid Publish variant side")
        if self.remote_verified and not self.transferred:
            raise ValueError("Publish variant verification requires transfer")
        if self.activated and not self.remote_verified:
            raise ValueError("Publish variant activation requires verification")
        if self.active_verified and not self.activated:
            raise ValueError("active Publish verification requires activation")


PublishVariantResult = PublishVariantFacts

@dataclass(frozen=True)
class PackPublishResult:
    # The first fields are retained for callers written against the server-only API.
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
    client: PublishVariantFacts | None = None
    server: PublishVariantFacts | None = None

    @property
    def client_facts(self): return self.client
    @property
    def server_facts(self): return self.server
    @property
    def variants(self): return {"client": self.client, "server": self.server}

    def __post_init__(self) -> None:
        if self.client is None or self.server is None:
            compatibility_facts = dict(
                transferred=self.remote_verified,
                remote_verified=self.remote_verified,
                activated=self.activated,
                active_verified=self.publication_succeeded,
            )
            if self.client is None:
                object.__setattr__(
                    self,
                    "client",
                    PublishVariantFacts("client", **compatibility_facts),
                )
            if self.server is None:
                object.__setattr__(
                    self,
                    "server",
                    PublishVariantFacts("server", **compatibility_facts),
                )
        assert self.client is not None and self.server is not None
        if self.client.target_side != "client" or self.server.target_side != "server":
            raise ValueError("Pack Publish variant result sides are invalid")
        if self.publication_succeeded != (
            self.client.active_verified and self.server.active_verified
        ):
            raise ValueError("Pack Publish success disagrees with variant facts")
        if self.remote_verified != (
            self.client.remote_verified and self.server.remote_verified
        ):
            raise ValueError("Pack Publish verification disagrees with variant facts")
        if self.activated != (self.client.activated and self.server.activated):
            raise ValueError("Pack Publish activation disagrees with variant facts")
        if self.restart_status not in {"not_started", "succeeded", "failed", "uncertain"}:
            raise ValueError("invalid Pack Publish restart status")
        if self.final_status not in {"published", "publication_failed", "restart_failed",
            "restart_uncertain", "restart_not_started", "cancelled", "cleanup_pending"}:
            raise ValueError("invalid Pack Publish final status")
        if self.publication_succeeded and not (
            self.remote_verified and self.activated
        ):
            raise ValueError("Pack Publish success requires verification and activation")
        if self.restart_succeeded != (self.restart_status == "succeeded"):
            raise ValueError("Pack Publish restart result is inconsistent")
        if self.restart_status == "not_started" and (self.restart_attempted or self.restart_succeeded):
            raise ValueError("Pack Publish restart was not started")
        if self.restart_status != "not_started" and not self.restart_attempted:
            raise ValueError("Pack Publish restart status requires an attempt")
        if self.restart_attempted and not self.publication_succeeded:
            raise ValueError("Pack Publish restart requires verified publication")
        if self.final_status == "published" and not (self.publication_succeeded and self.restart_succeeded):
            raise ValueError("published Pack Publish result is incomplete")
        if self.final_status == "publication_failed" and (
            self.publication_succeeded or self.restart_attempted
        ):
            raise ValueError("publication failure contains successful publication or restart")
        if self.final_status == "restart_failed" and not (
            self.publication_succeeded and self.restart_status == "failed"
        ):
            raise ValueError("restart failure requires a known failed attempt")
        if self.final_status == "restart_uncertain" and not (
            self.publication_succeeded and self.restart_status == "uncertain"
        ):
            raise ValueError("restart uncertainty requires an uncertain attempt")
        if self.final_status == "restart_not_started" and (
            not self.publication_succeeded or self.restart_attempted
        ):
            raise ValueError(
                "restart-not-started requires verified publication without an attempt"
            )

class PackPublishExecutionError(RuntimeError):
    def __init__(self, message, *, result, phase, primary_error, cleanup_error=None, plan=None):
        super().__init__(message); self.result = result; self.phase = phase
        self.primary_error = primary_error; self.cleanup_error = cleanup_error; self.plan = plan
    @property
    def publication_succeeded(self): return self.result is not None and self.result.publication_succeeded
class PackPublishCancelled(PackPublishExecutionError): pass
class PackPublishDeadlineExceeded(PackPublishExecutionError): pass
class PackPublishCleanupError(PackPublishExecutionError): pass
class PackPublishRestartError(PackPublishExecutionError): pass
class PackPublishRestartUncertainError(PackPublishRestartError): pass

class PackPublishPlan:
    def __init__(self, *, pack_id, bundle: PackPublishManifestBundle, server_target,
                 client_target, cancel_event, deadline, token):
        if token is not _PLAN_TOKEN: raise TypeError("PackPublishPlan must be created by plan_pack_publish")
        self._pack_id, self._bundle = pack_id, bundle
        self._server_target, self._client_target = server_target, client_target
        self._cancel_event, self._deadline = cancel_event, deadline
        self._generation_ids = {"client": compute_publish_generation_id(bundle.client, client_target),
                                "server": compute_publish_generation_id(bundle.server, server_target)}
        self._state = "planned"; self._result = None; self._terminal_result = None
        self._primary_error = None; self._cleanup_error = None
        self._transfer_plans = {}; self._activation_cleanup = {}; self._activation_staged = {}
        self._transfer_cleanup_pending = set(); self._lock = threading.RLock(); self._terminal_state = "completed"
    @property
    def pack_id(self): return self._pack_id
    @property
    def bundle(self): return self._bundle
    @property
    def manifest_bundle(self): return self._bundle
    @property
    def client_manifest(self): return self._bundle.client
    @property
    def server_manifest(self): return self._bundle.server
    @property
    def manifest(self): return self._bundle.server
    @property
    def target(self): return self._server_target
    @property
    def client_target(self): return self._client_target
    @property
    def server_target(self): return self._server_target
    @property
    def target_side(self): return "server"
    @property
    def manifest_digest(self): return self.manifest.manifest_digest
    @property
    def source_snapshot_digest(self): return self._bundle.source_snapshot_digest
    @property
    def target_config_digest(self): return self.target.config_digest
    @property
    def generation_id(self): return self._generation_ids["server"]
    @property
    def client_generation_id(self): return self._generation_ids["client"]
    @property
    def server_generation_id(self): return self._generation_ids["server"]
    @property
    def cancel_event(self): return self._cancel_event
    @property
    def deadline(self): return self._deadline
    @property
    def state(self):
        with self._lock: return self._state
    @property
    def result(self):
        with self._lock: return self._result
    def __repr__(self):
        return f"PackPublishPlan(pack_id={self.pack_id!r}, state={self.state!r}, bundle_digest={self.bundle.bundle_digest!r})"

def _emit(progress, event):
    if progress is None: return
    try: progress(event)
    except Exception: pass
def _phase_progress(progress, phase):
    def emit(value):
        if isinstance(value, PublishTransferProgress):
            _emit(progress, PackPublishProgress(phase, value.phase, value.completed_files, value.total_files,
                value.completed_bytes, value.total_bytes, value.current_path))
        else: _emit(progress, PackPublishProgress(phase, str(value)))
    return emit
def _checkpoint(event, deadline):
    if event.is_set(): raise PackPublishCancelled("Pack Publish was cancelled", result=None, phase="checkpoint", primary_error=None)
    if time.monotonic() >= deadline: raise PackPublishDeadlineExceeded("Pack Publish deadline exceeded", result=None, phase="checkpoint", primary_error=None)
def _resolve_target(pack_id, remote_path):
    s = packctl.deployment_settings(pack_id)
    return publish_remote_target_from_legacy_settings(rsync_target=s.rsync_target, ssh_host=s.ssh_host,
        stack_dir=s.stack_dir, service=s.service, server_id=LEGACY_SERVER_ID, remote_path=remote_path)

def plan_pack_publish(pack_id: str, *, target_side: str = "server", remote_path=None,
                      cancel_event=None, deadline=None, progress=None):
    if target_side != "server":
        raise ValueError(
            "Pack Publish is a dual client/server operation; target_side must be server"
        )
    event = cancel_event or threading.Event(); end = deadline if deadline is not None else time.monotonic() + _OPERATION_TIMEOUT_SECONDS
    _checkpoint(event, end); _emit(progress, PackPublishProgress("planning"))
    try:
        bundle = plan_pack_publish_manifest_bundle(pack_id, cancel_event=event, deadline=end,
            progress=_phase_progress(progress, "planning"))
        _checkpoint(event, end); server = _resolve_target(pack_id, remote_path)
        client = publish_client_target_from_remote_target(server); _checkpoint(event, end)
    except ManifestCancelled as e: raise PackPublishCancelled(str(e), result=None, phase="planning", primary_error=e) from e
    except ManifestDeadlineExceeded as e: raise PackPublishDeadlineExceeded(str(e), result=None, phase="planning", primary_error=e) from e
    except (ManifestError, packctl.ConfigError, PublishTargetError) as e:
        raise PackPublishExecutionError("Pack Publish planning failed", result=None, phase="planning", primary_error=e) from e
    _emit(progress, PackPublishProgress("validated"))
    return PackPublishPlan(pack_id=pack_id, bundle=bundle, server_target=server, client_target=client,
        cancel_event=event, deadline=end, token=_PLAN_TOKEN)

def _facts(plan, states):
    return {side: PublishVariantFacts(side, **states[side]) for side in ("client", "server")}
def _result(plan, states, *, restart=None, status="publication_failed"):
    facts = _facts(plan, states); sf, cf = facts["server"], facts["client"]
    rs = restart.status if restart else "not_started"; attempted = restart.attempted if restart else False
    succeeded = restart.succeeded if restart else False
    return PackPublishResult(plan.pack_id, "server", plan.manifest_digest, plan.target_config_digest,
        plan.generation_id, all(f.active_verified for f in facts.values()),
        all(f.remote_verified for f in facts.values()), all(f.activated for f in facts.values()),
        attempted, succeeded, rs, status, cf, sf)

def _error_for(error, result, phase, plan=None):
    if isinstance(error, PackPublishExecutionError):
        error.result = result
        error.phase = phase
        return error
    if isinstance(error, (PackPublishCancelled, PackPublishDeadlineExceeded)): return error.__class__(str(error), result=result, phase=phase, primary_error=error)
    if isinstance(error, (PublishRestartCancelled,)): return PackPublishCancelled(str(error), result=result, phase=phase, primary_error=error)
    if isinstance(error, PublishRestartDeadlineExceeded): return PackPublishDeadlineExceeded(str(error), result=result, phase=phase, primary_error=error)
    if isinstance(error, PublishRestartIntegrityError): return PackPublishRestartUncertainError(str(error), result=result, phase=phase, primary_error=error)
    if isinstance(error, PublishRestartError): return PackPublishRestartError(str(error), result=result, phase=phase, primary_error=error)
    if phase == "restarting":
        return PackPublishRestartUncertainError(
            "Pack publication succeeded, but restart outcome is uncertain",
            result=result,
            phase=phase,
            primary_error=error,
        )
    if not isinstance(error, (PublishTransferUncertainError, PublishSemanticVerificationUncertainError, PublishActivationUncertainError)):
        if error.__class__ is not PackPublishCancelled and error.__class__ is not PackPublishDeadlineExceeded:
            if (plan is not None and plan.cancel_event.is_set()) or getattr(error, "cancelled", False):
                return PackPublishCancelled(str(error), result=result, phase=phase, primary_error=error)
            if plan is not None and time.monotonic() >= plan.deadline:
                return PackPublishDeadlineExceeded(str(error), result=result, phase=phase, primary_error=error)
    return PackPublishExecutionError("Pack Publish execution failed", result=result, phase=phase, primary_error=error)

def execute_pack_publish(plan, *, cancel_event=None, deadline=None, progress=None):
    if type(plan) is not PackPublishPlan: raise TypeError("execute_pack_publish requires a PackPublishPlan")
    if (cancel_event is not None and cancel_event is not plan.cancel_event) or (deadline is not None and deadline != plan.deadline): raise ValueError("Pack Publish execution controls do not match the plan")
    with plan._lock:
        if plan._state != "planned": raise PackPublishExecutionError("PackPublishPlan is one-shot", result=plan._result, phase="execution", primary_error=None, plan=plan if plan._state == "cleanup-pending" else None)
        plan._state = "executing"
    states = {s: {"transferred": False, "remote_verified": False, "activated": False, "active_verified": False, "cleanup_pending": False} for s in ("client", "server")}
    owners, staged, verified, activated = {}, {}, {}, {}; primary = None; phase = "preparing"
    try:
        for side in ("client", "server"):
            _checkpoint(plan.cancel_event, plan.deadline); phase = f"preparing-{side}"; _emit(progress, PackPublishProgress(phase))
            manifest, target = getattr(plan.bundle, side), getattr(plan, f"{side}_target")
            try: owners[side] = prepare_publish_transfer(plan.pack_id, manifest, target, cancel_event=plan.cancel_event, deadline=plan.deadline, progress=_phase_progress(progress, phase))
            except PublishTransferCleanupError as e:
                if e.plan is not None:
                    owners[side] = e.plan
                    plan._transfer_plans[side] = e.plan
                raise
            plan._transfer_plans[side] = owners[side]
        for side in ("client", "server"):
            _checkpoint(plan.cancel_event, plan.deadline); phase = f"transferring-{side}"; _emit(progress, PackPublishProgress(phase))
            staged[side] = execute_publish_transfer(owners[side], cancel_event=plan.cancel_event, deadline=plan.deadline, progress=_phase_progress(progress, phase)); states[side]["transferred"] = True
        for side in ("client", "server"):
            _checkpoint(plan.cancel_event, plan.deadline); phase = f"verifying-{side}"; _emit(progress, PackPublishProgress(phase))
            verified[side] = verify_publish_generation(staged[side], getattr(plan.bundle, side), getattr(plan, f"{side}_target"), cancel_event=plan.cancel_event, deadline=plan.deadline, progress=_phase_progress(progress, phase)); states[side]["remote_verified"] = True
        for side in ("client", "server"):
            _checkpoint(plan.cancel_event, plan.deadline)
            phase = f"activating-{side}"
            _emit(progress, PackPublishProgress(phase))
            try:
                activated[side] = activate_publish_generation(staged[side], verified[side], getattr(plan, f"{side}_target"), manifest=getattr(plan.bundle, side), cancel_event=plan.cancel_event, deadline=plan.deadline, progress=_phase_progress(progress, phase)); states[side]["activated"] = True
            except PublishActivationCleanupError as e:
                if e.operation_id is not None:
                    plan._activation_cleanup[side] = e
                    plan._activation_staged[side] = staged[side]
                if e.activated is not None:
                    activated[side] = e.activated; states[side]["activated"] = True
                raise
            except PublishActivationUncertainError as e:
                if e.operation_id is not None and e.recovery_path is not None:
                    plan._activation_cleanup[side] = e
                    plan._activation_staged[side] = staged[side]
                raise
            _checkpoint(plan.cancel_event, plan.deadline)
            phase = f"verifying-active-{side}"
            _emit(progress, PackPublishProgress(phase))
            verify_activated_publish_generation(
                activated[side],
                getattr(plan.bundle, side),
                getattr(plan, f"{side}_target"),
                cancel_event=plan.cancel_event,
                deadline=plan.deadline,
                progress=_phase_progress(progress, phase),
            )
            states[side]["active_verified"] = True
        _checkpoint(plan.cancel_event, plan.deadline); phase = "restarting"; _emit(progress, PackPublishProgress(phase))
        restart = restart_activated_publish(activated["server"], plan.bundle.server, plan.server_target, cancel_event=plan.cancel_event, deadline=plan.deadline, progress=_phase_progress(progress, phase))
        status = {"succeeded": "published", "failed": "restart_failed", "uncertain": "restart_uncertain"}[restart.status]
        result = _result(plan, states, restart=restart, status=status)
        if status != "published":
            primary = (
                PackPublishRestartUncertainError(
                    "restart outcome is uncertain",
                    result=result,
                    phase=phase,
                    primary_error=None,
                )
                if status == "restart_uncertain"
                else PackPublishRestartError(
                    "restart failed",
                    result=result,
                    phase=phase,
                    primary_error=None,
                )
            )
    except BaseException as error:
        # Transfer preparation may retain an owner while reporting the
        # original failure (including a BaseException).  Keep that
        # authority as the operation's primary error; the wrapper is only
        # the cleanup/lifecycle carrier.
        if (
            isinstance(error, PublishTransferCleanupError)
            and error.primary_error is not None
        ):
            primary = error.primary_error
        else:
            primary = error
        cancelled = isinstance(
            error,
            (
                PackPublishCancelled,
                PackPublishDeadlineExceeded,
                PublishRestartCancelled,
                PublishRestartDeadlineExceeded,
            ),
        ) or plan.cancel_event.is_set() or time.monotonic() >= plan.deadline
        failed_restart: PublishRestartResult | None = None
        failure_status = (
            "restart_not_started"
            if all(state["active_verified"] for state in states.values())
            else "publication_failed"
        )
        if phase == "restarting" and isinstance(error, PublishRestartIntegrityError):
            failed_restart = error.result
            failure_status = "restart_uncertain"
        elif phase == "restarting" and not isinstance(
            error,
            (
                PublishRestartError,
                PublishRestartCancelled,
                PublishRestartDeadlineExceeded,
                PackPublishCancelled,
                PackPublishDeadlineExceeded,
            ),
        ):
            failed_restart = PublishRestartResult(
                plan.manifest_digest,
                plan.target_config_digest,
                plan.generation_id,
                True,
                False,
                "uncertain",
                None,
            )
            failure_status = "restart_uncertain"
        result = _result(
            plan,
            states,
            restart=failed_restart,
            status=(
                "cancelled"
                if cancelled
                else failure_status
            ),
        )
    finally:
        plan._terminal_result = result
        plan._terminal_state = "completed" if primary is None else "failed"
        cleanup_error = None
        for side, activation_error in plan._activation_cleanup.items():
            states[side]["cleanup_pending"] = True
            cleanup_error = cleanup_error or activation_error
        for side, owner in owners.items():
            try: discard_publish_transfer_plan(owner, deadline=time.monotonic() + _CLEANUP_TIMEOUT_SECONDS)
            except BaseException as e: cleanup_error = cleanup_error or e; states[side]["cleanup_pending"] = True; plan._transfer_cleanup_pending.add(side)
        if cleanup_error or plan._activation_cleanup:
            result = replace(result, final_status="cleanup_pending", client=_facts(plan, states)["client"], server=_facts(plan, states)["server"])
            plan._result = result; plan._primary_error = primary; plan._cleanup_error = cleanup_error
            plan._state = "cleanup-pending"
            raise PackPublishCleanupError("Pack Publish cleanup is pending", result=result, phase="cleanup", primary_error=primary, cleanup_error=cleanup_error, plan=plan) from (primary or cleanup_error)
    plan._result = result; plan._terminal_result = result; plan._primary_error = primary; plan._state = plan._terminal_state
    if primary is not None:
        if not isinstance(primary, Exception):
            raise primary
        err = _error_for(primary, result, phase, plan)
        raise err from primary
    _emit(progress, PackPublishProgress("published")); return result

def retry_pack_publish_cleanup(plan, *, deadline=None, progress=None):
    if type(plan) is not PackPublishPlan: raise TypeError("retry_pack_publish_cleanup requires a PackPublishPlan")
    with plan._lock:
        if plan._state != "cleanup-pending": raise PackPublishCleanupError("Pack Publish cleanup is not pending", result=plan._result, phase="cleanup", primary_error=plan._primary_error, cleanup_error=plan._cleanup_error, plan=plan)
        plan._state = "cleaning"
    error = None

    def cleanup_deadline() -> float:
        return (
            deadline
            if deadline is not None
            else time.monotonic() + _CLEANUP_TIMEOUT_SECONDS
        )

    for side, activation_error in tuple(plan._activation_cleanup.items()):
        staged = plan._activation_staged.get(side)
        try:
            retry_publish_activation_cleanup(staged, getattr(plan.bundle, side), getattr(plan, f"{side}_target"), activation_error.operation_id, deadline=cleanup_deadline(),
                finalize_receipt=isinstance(activation_error, PublishActivationCleanupError) and activation_error.activated is not None,
                expected_status=activation_error.expected_status if isinstance(activation_error, PublishActivationCleanupError) and activation_error.activated is not None else None)
        except BaseException as e: error = error or e
        else:
            del plan._activation_cleanup[side]; del plan._activation_staged[side]
    for side in tuple(plan._transfer_cleanup_pending):
        try: retry_discard_publish_transfer_plan(plan._transfer_plans[side], deadline=cleanup_deadline())
        except BaseException as e: error = error or e
        else: plan._transfer_cleanup_pending.discard(side)
    if error:
        plan._cleanup_error = error; plan._state = "cleanup-pending"
        raise PackPublishCleanupError("Pack Publish cleanup is still pending", result=plan.result, phase="cleanup", primary_error=plan._primary_error, cleanup_error=error, plan=plan) from (plan._primary_error or error)
    plan._cleanup_error = None; plan._state = plan._terminal_state; plan._result = plan._terminal_result
