"""Shared retry/backoff with a transient-vs-fatal classifier (plan §3.1 A1/A3).

One classifier for the whole platform: reconnect loops, health probing and
any opt-in retry of idempotent operations all agree on what is worth
retrying.  Classification is by exception TYPE plus the explicit
``InstrumentError.fatal`` flag — never by message text (plan §3.3: instruments
have deterministic error types; string matching is how capability discovery
rots).

Retries are NOT applied automatically to state-mutating commands (plan §3.3:
never auto-retry a command whose first attempt may have half-executed on the
hardware) — ``retry_call`` is for connect/probe/idempotent-query paths.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Annotated, TypeVar

from loguru import logger
from pydantic import Field

from .contracts import ContractModel
from .errors import (
    CapabilityContractError,
    DeviceReportedError,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentProtocolError,
    InstrumentTimeoutError,
    InvalidInstrumentStateError,
    PhoebeConfigError,
    SafetyViolationError,
    UnsupportedCapabilityError,
    UnsupportedInstrumentModelError,
)

T = TypeVar("T")


class ErrorClass(StrEnum):
    TRANSIENT = "transient"    # link glitch / timeout — worth backing off and retrying
    FATAL = "fatal"            # retrying cannot change the outcome — surface now


# Type-based rules.  Order matters: fatal subtypes are checked before the
# broad transient bases they may share ancestry with.
_FATAL_TYPES: tuple[type[BaseException], ...] = (
    DeviceReportedError,             # the device said no — a retry asks the same question
    InvalidInstrumentStateError,
    SafetyViolationError,
    UnsupportedCapabilityError,
    UnsupportedInstrumentModelError,
    CapabilityContractError,
    PhoebeConfigError,
)

_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    InstrumentTimeoutError,
    InstrumentConnectionError,       # link dropped — a rebuild can fix it
    InstrumentProtocolError,         # garbled/partial reply — usually link noise
    ConnectionError,
    TimeoutError,
    OSError,
)


def classify_error(exc: BaseException) -> ErrorClass:
    """Transient (retry with backoff) vs fatal (surface immediately)."""
    if isinstance(exc, InstrumentError) and exc.fatal:
        return ErrorClass.FATAL
    if isinstance(exc, _FATAL_TYPES):
        return ErrorClass.FATAL
    if isinstance(exc, _TRANSIENT_TYPES):
        return ErrorClass.TRANSIENT
    return ErrorClass.FATAL           # unknown → never loop blindly


class RetryPolicy(ContractModel):
    """Exponential backoff with ceiling and jitter."""

    max_attempts: Annotated[int, Field(ge=1)] = 5
    base_delay_s: Annotated[float, Field(gt=0)] = 0.5
    max_delay_s: Annotated[float, Field(gt=0)] = 30.0
    multiplier: Annotated[float, Field(ge=1.0)] = 2.0
    jitter: Annotated[float, Field(ge=0.0, le=1.0)] = 0.1   # ± fraction of the delay

    def delay_for(self, failure_count: int) -> float:
        """Backoff delay after the Nth consecutive failure (1-based)."""
        exponent = max(0, failure_count - 1)
        delay = min(self.base_delay_s * self.multiplier ** exponent, self.max_delay_s)
        if self.jitter:
            delay *= 1.0 + random.uniform(-self.jitter, self.jitter)
        return max(0.0, delay)


async def retry_call(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    label: str,
    classify: Callable[[BaseException], ErrorClass] = classify_error,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Await ``fn()``; retry transient failures per ``policy``.

    Fatal errors and exhausted attempts re-raise the original exception.
    Cancellation always propagates immediately.
    """
    failures = 0
    while True:
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failures += 1
            if classify(exc) is ErrorClass.FATAL or failures >= policy.max_attempts:
                raise
            delay = policy.delay_for(failures)
            logger.warning("{}: transient failure #{} ({}); retrying in {:.2f}s",
                           label, failures, exc, delay)
            if on_retry is not None:
                on_retry(failures, exc, delay)
            await asyncio.sleep(delay)
