"""Last-known-health cache: /health composes an answer instead of waiting (#1592).

INCIDENT 2026-08-31. ``/health`` awaited the chain-connection check and the
on-chain oracle probe with no deadline of its own. With the Arc RPC unreachable
from inside the VPC, both parked; the handler blew the ALB's 5s check and ECS's
container HEALTHCHECK; new tasks never turned healthy; the rollout wedged at 1/2
for its full 1200s budget while the serving task's event loop starved every
other route (leaderboard 10s, chain routes dead). The same RPC answered in 0.1s
from outside the VPC — nothing was slow, the calls were simply unbounded.

The fix is a change of contract for the endpoint, and only for the endpoint:

    /health is a REPORT ON what we know, not an ATTEMPT TO FIND OUT.

Every outbound probe gets a hard budget. If it answers, we record the value and
serve it. If it does not, we serve the last value we measured — labelled with
its age and the reason the fresh probe missed — and the endpoint answers anyway.

**No value's meaning changes.** ``chain_connected`` still means "the last chain
check we completed said connected"; ``oracle_fresh`` still means "every probed
oracle read succeeded and reported fresh". What changes is only how long the
handler is willing to wait to compute them.

**Honesty rules (docs/architectural-principles.md § fail-soft).** A stale value
served as if fresh is exactly the plausible-substitute failure this codebase
treats as its primary defect class, so a served-from-cache value is never
silent. Three states, and they are the payload's states too:

    live          the probe answered inside its budget; this is a fresh reading
    stale_cached  the probe blew its budget; the value below was measured
                  ``age_s`` seconds ago and is NOT a current reading
    probe_timeout the probe blew its budget and nothing was ever cached; there
                  is no value to report — loud absence, never a substitute

``stale_cached`` and ``probe_timeout`` both carry a ``reason``. ``live`` carries
none, and the /health payload omits the staleness fields entirely in that case,
so "are these fields present?" is itself the signal (mirrors the four-state
``corpus_embedded_at_rest`` / ``paper_rerank_model_live`` pairing at #1488).

**Errors are not timeouts and are not cached.** Only :class:`TimeoutError` is
handled here; any other exception propagates to the caller's existing handler
untouched, so a broken probe still reports its own ``probe_error`` reason and
can never overwrite a good last-known value with a failure.

Process-local by design. This is a per-worker memo, not shared state: each task
answers from what IT has measured, so a task that has never reached the chain
reports ``probe_timeout`` rather than borrowing another task's optimism.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from archimedes.deadline import run_with_deadline

logger = logging.getLogger(__name__)

# One probe's slice of /health's total budget. The endpoint's hard ceiling is
# 5s, enforced twice — infra/alb.tf's target-group check and infra/ecs.tf's
# container HEALTHCHECK (which gates nginx's dependsOn: HEALTHY). Probes that
# share this budget run CONCURRENTLY in the handler, so the bounded outbound
# section costs one budget, not one per probe.
DEFAULT_PROBE_BUDGET_SECONDS = 1.5

ProbeState = Literal["live", "stale_cached", "probe_timeout"]


@dataclass(frozen=True)
class ProbeOutcome:
    """One component's health reading plus how much to trust its freshness."""

    name: str
    value: Any
    state: ProbeState
    measured_at: float | None
    age_s: float | None
    reason: str

    @property
    def is_live(self) -> bool:
        return self.state == "live"

    def payload_fields(self, prefix: str) -> dict[str, Any]:
        """The staleness fields to merge into /health's JSON for this component.

        ``{prefix}_probe_state`` is always present — a monitor should not have to
        infer "live" from the absence of keys. ``{prefix}_probe_age_s`` and
        ``{prefix}_probe_reason`` appear ONLY when the fresh probe missed, so
        their presence is the alarm and their absence is the all-clear. On
        ``probe_timeout`` the age is ``None`` on purpose: there is no cached
        reading, so there is no age, and reporting one would be an invention.
        """
        fields: dict[str, Any] = {f"{prefix}_probe_state": self.state}
        if self.state != "live":
            fields[f"{prefix}_probe_age_s"] = self.age_s
            fields[f"{prefix}_probe_reason"] = self.reason
        return fields


class HealthProbeCache:
    """Per-process last-known value for each health component."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[Any, float]] = {}

    def clear(self) -> None:
        self._entries.clear()

    def last_known(self, name: str) -> tuple[Any, float] | None:
        """``(value, measured_at)`` for ``name``, or ``None`` if never measured."""
        return self._entries.get(name)

    async def probe(
        self,
        name: str,
        factory: Any,
        *,
        budget_seconds: float = DEFAULT_PROBE_BUDGET_SECONDS,
        absent: Any = None,
    ) -> ProbeOutcome:
        """Run ``factory()`` under ``budget_seconds``; fall back to last-known.

        ``factory`` is a zero-argument callable returning an awaitable — a
        callable rather than an awaitable so a retry/second call is possible and
        so nothing is constructed until the deadline is armed.

        ``absent`` is the value reported when the probe misses AND nothing was
        ever cached. It must be the caller's own "we do not know" value (e.g.
        ``False`` for a connectivity flag, whose established meaning on any
        failure is already "not known to be connected") — never an optimistic
        default, which would be the substitute this module exists to prevent.
        """
        try:
            value = await run_with_deadline(factory(), budget_seconds, label=f"health probe {name!r}")
        except TimeoutError:
            reason = f"probe_timeout: {name} exceeded its {budget_seconds:.1f}s health budget"
            cached = self._entries.get(name)
            if cached is None:
                # Loud absence. No cached reading exists, so there is nothing
                # honest to serve — the caller's ``absent`` value goes out with
                # the reason attached, never dressed up as a measurement.
                logger.warning(
                    "HEALTH_PROBE_TIMEOUT: component=%s budget_s=%.1f cached=none state=probe_timeout",
                    name,
                    budget_seconds,
                )
                return ProbeOutcome(
                    name=name,
                    value=absent,
                    state="probe_timeout",
                    measured_at=None,
                    age_s=None,
                    reason=reason,
                )
            cached_value, measured_at = cached
            age_s = round(max(0.0, time.time() - measured_at), 1)
            logger.warning(
                "HEALTH_PROBE_TIMEOUT: component=%s budget_s=%.1f cached_age_s=%.1f state=stale_cached",
                name,
                budget_seconds,
                age_s,
            )
            return ProbeOutcome(
                name=name,
                value=cached_value,
                state="stale_cached",
                measured_at=measured_at,
                age_s=age_s,
                reason=reason,
            )

        measured_at = time.time()
        self._entries[name] = (value, measured_at)
        return ProbeOutcome(
            name=name,
            value=value,
            state="live",
            measured_at=measured_at,
            age_s=0.0,
            reason="",
        )


# Module-level singleton — the memo has to outlive a request to be worth
# anything. Tests reset it through clear_health_probe_cache (autouse fixture in
# backend/tests/conftest.py), same treatment as the rigor and vault-owner memos.
health_probe_cache = HealthProbeCache()


def clear_health_probe_cache() -> None:
    """Drop every last-known reading (test hygiene; see conftest)."""
    health_probe_cache.clear()
