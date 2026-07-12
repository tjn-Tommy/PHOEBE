"""Typed admission chain: the fixed, fail-closed command path (plan §6.4).

Dispatch v2 runs every ``CommandEnvelope`` through a **fixed, ordered, closed**
set of stages (the *shape* of a pipeline, with a typed context and stable
reason codes — never an ``_extras`` dict, never free-text classification)::

    route + payload boundary validation   (UNKNOWN_COMMAND / INVALID_PAYLOAD)
      → CommandLedger idempotency         (REPLAYED / COMMAND_ID_CONFLICT)
      → maintenance / operator policy     (MAINTENANCE_MODE)
      → plugin API compatibility          (PLUGIN_API_INCOMPATIBLE)
      → DI resolution                     (MISSING_ROLE / KIND_MISMATCH)
      → cached lifecycle + health age     (DEVICE_NOT_READY / HEALTH_STALE)
      → [profile/calibration binding      (CALIBRATION_EXPIRED) — Phase D+]
      → lease / queue policy              (DEVICE_BUSY / QUEUED)  [TaskManager]

Every stage consumes **cached snapshots only** — no stage performs device
I/O.  Stages either return an ``AdmissionDecision`` (chain stops) or ``None``
(continue); the lease/queue step stays in the TaskManager because admitting a
run and creating it must be atomic on the loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

from ..contracts.commands import AckCode, AdmissionDecision, CommandAck, CommandEnvelope
from ..contracts.errors import ErrorCode, ErrorInfo
from .contracts import InstrumentId, validate_boundary
from .di import KindMismatchError, MissingRoleError, ResolvedRequirement
from .plugin import PLUGIN_API_VERSION, PluginSpec
from .reconnect import DeviceLifecycleState

if TYPE_CHECKING:
    from collections.abc import Callable

    from .command_ledger import CommandLedger
    from .di import DependencyResolver
    from .plugin import PluginRegistry


@dataclass(slots=True)
class AdmissionContext:
    """Typed stage context — the closed set of facts stages may read/write."""

    envelope: CommandEnvelope
    # produced by the routing/validation stage:
    spec: PluginSpec | None = None
    config: Any = None
    entrypoint: Any = None
    # produced by the DI stage:
    requirements: list[ResolvedRequirement] = field(default_factory=list)
    # produced by the ledger stage on a replay:
    replayed_ack: CommandAck | None = None


class AdmissionStage(Protocol):
    def __call__(self, ctx: AdmissionContext) -> AdmissionDecision | None: ...


#: Ack-code → ErrorInfo.code projection for device-attributed rejections.
_ERROR_CODE_OF_ACK: dict[AckCode, ErrorCode] = {
    AckCode.DEVICE_NOT_READY: ErrorCode.DEVICE_NOT_READY,
    AckCode.HEALTH_STALE: ErrorCode.DEVICE_NOT_READY,
    AckCode.DEVICE_BUSY: ErrorCode.LEASE_UNAVAILABLE,
}


def _reject(code: AckCode, detail: str, *,
            instrument_id: str | None = None) -> AdmissionDecision:
    error = None
    if instrument_id is not None:
        error = ErrorInfo(code=_ERROR_CODE_OF_ACK.get(code, ErrorCode.INTERNAL),
                          message=detail, instrument_id=InstrumentId(instrument_id))
    return AdmissionDecision(code=code, detail=detail, error=error)


class AdmissionChain:
    """The fixed pre-lease part of the chain.  Constructed once per
    TaskManager with its collaborators; run synchronously per dispatch."""

    def __init__(
        self,
        *,
        registry: PluginRegistry,
        make_resolver: Callable[[], DependencyResolver],
        ledger: CommandLedger | None,
        maintenance_reason: Callable[[], str | None],
        lifecycle_state: Callable[[InstrumentId], DeviceLifecycleState],
        health_age_s: Callable[[InstrumentId], float | None],
        health_stale_after_s: float | None,
    ) -> None:
        self._registry = registry
        self._make_resolver = make_resolver
        self._ledger = ledger
        self._maintenance_reason = maintenance_reason
        self._lifecycle_state = lifecycle_state
        self._health_age_s = health_age_s
        self._health_stale_after_s = health_stale_after_s
        # the closed stage set, in chain order — plugins get hook points
        # elsewhere, never new stages here
        self._stages: tuple[AdmissionStage, ...] = (
            self._stage_route_and_validate,
            self._stage_ledger,
            self._stage_maintenance,
            self._stage_plugin_api,
            self._stage_resolve,
            self._stage_device_health,
        )

    def run(self, ctx: AdmissionContext) -> AdmissionDecision | None:
        """None → all pre-lease stages passed (TaskManager proceeds to the
        lease/queue step); a decision → the chain stopped."""
        for stage in self._stages:
            decision = stage(ctx)
            if decision is not None:
                return decision
        return None

    # ------------------------------------------------------------- stages
    def _stage_route_and_validate(self, ctx: AdmissionContext) -> AdmissionDecision | None:
        spec = self._registry.spec_for_command(ctx.envelope.command)
        if spec is None:
            return _reject(AckCode.UNKNOWN_COMMAND,
                           f"unknown command {ctx.envelope.command!r}")
        try:
            config = validate_boundary(spec.config_type, ctx.envelope.payload)
        except Exception as exc:
            return _reject(AckCode.INVALID_PAYLOAD, f"invalid payload: {exc}")
        ctx.spec = spec
        ctx.config = config
        return None

    def _stage_ledger(self, ctx: AdmissionContext) -> AdmissionDecision | None:
        if self._ledger is None:
            return None
        try:
            verdict = self._ledger.check(ctx.envelope)
        except Exception:
            # Fail-open, loudly: a broken/closed ledger DB must not brick
            # dispatch (idempotency degrades; the remaining stages still run —
            # during shutdown the maintenance gate right below rejects).
            logger.exception("command ledger check failed for {}; skipping "
                             "idempotency stage", ctx.envelope.command_id)
            return None
        if verdict is None:
            return None
        if verdict.code is AckCode.COMMAND_ID_CONFLICT:
            return AdmissionDecision(code=AckCode.COMMAND_ID_CONFLICT,
                                     detail=verdict.reason)
        ctx.replayed_ack = verdict
        return AdmissionDecision(code=AckCode.REPLAYED, detail=verdict.reason,
                                 task_id=verdict.task_id)

    def _stage_maintenance(self, ctx: AdmissionContext) -> AdmissionDecision | None:
        reason = self._maintenance_reason()
        if reason is not None:
            return _reject(AckCode.MAINTENANCE_MODE, f"maintenance mode: {reason}")
        return None

    def _stage_plugin_api(self, ctx: AdmissionContext) -> AdmissionDecision | None:
        assert ctx.spec is not None
        if ctx.spec.api_version != PLUGIN_API_VERSION:
            return _reject(
                AckCode.PLUGIN_API_INCOMPATIBLE,
                f"plugin {ctx.spec.plugin_id!r} declares api_version "
                f"{ctx.spec.api_version}, kernel implements {PLUGIN_API_VERSION}",
            )
        return None

    def _stage_resolve(self, ctx: AdmissionContext) -> AdmissionDecision | None:
        assert ctx.spec is not None
        instance = ctx.spec.instantiate()
        ctx.entrypoint = ctx.spec.entrypoint(instance)
        try:
            ctx.requirements = self._make_resolver().resolve(
                ctx.spec.plugin_id, ctx.entrypoint)
        except MissingRoleError as exc:
            return _reject(AckCode.MISSING_ROLE, str(exc))
        except KindMismatchError as exc:
            return _reject(AckCode.KIND_MISMATCH, str(exc))
        except Exception as exc:
            return _reject(AckCode.INTERNAL_ERROR, f"dependency resolution: {exc}")
        return None

    def _stage_device_health(self, ctx: AdmissionContext) -> AdmissionDecision | None:
        for req in ctx.requirements:
            state = self._lifecycle_state(req.instrument_id)
            # CONFIGURED = lifecycle supervision not engaged (direct wiring in
            # tests / connect=False) — keeps pre-lifecycle semantics.
            if state not in (DeviceLifecycleState.READY,
                             DeviceLifecycleState.CONFIGURED):
                return _reject(
                    AckCode.DEVICE_NOT_READY,
                    f"device not ready: instrument {str(req.instrument_id)!r} "
                    f"is not ready (lifecycle state: {state.value})",
                    instrument_id=str(req.instrument_id),
                )
            if (self._health_stale_after_s is not None
                    and state is DeviceLifecycleState.READY):
                age = self._health_age_s(req.instrument_id)
                if age is not None and age > self._health_stale_after_s:
                    return _reject(
                        AckCode.HEALTH_STALE,
                        f"instrument {str(req.instrument_id)!r} last proved "
                        f"healthy {age:.0f}s ago (> {self._health_stale_after_s:.0f}s)"
                        " — health poller may be down",
                        instrument_id=str(req.instrument_id),
                    )
        return None
