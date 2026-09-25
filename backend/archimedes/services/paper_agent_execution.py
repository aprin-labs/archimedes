"""Drive paper deployments with the agent tick loop (#1410).

One pass = one agent tick per active paper deployment, on the existing
``paper_advance_loop``'s cadence, in the existing web-tier process. No new
runner, no new container, no dependency on the EC2 runner box (#1402) — #1410's
second anti-goal, taken literally.

WHAT ONE TICK DOES, and why each step is the vault's step and not a paper-only
imitation of it:

  1. Evaluate the deployment's DEPLOYED spec through
     ``strategy_evaluator.evaluate_strategies``. That is the same call the vault
     runner makes, and it routes a spec-carrying strategy into ``_spec_signal``
     → ``_replay_position_state`` — the unified F2/F3 replay-derived position
     FSM. There is no second interpretation of a signal anywhere in this file;
     ``test_execution_venue_parity`` pins that the FSM has exactly one
     definition.
  2. ``execution.core.targets_from_signals`` — aggregate, throttle, resolve.
     Byte-identical to the vault path but for the venue's address resolution.
  3. ``PaperVenue.read_portfolio`` — the fold of this deployment's own
     agent-trade ledger.
  4. ``execution.core.compute_trades`` — the same 15% drift gate the vault uses.
  5. ``PaperVenue.execute_trades`` — append, each row naming its tick and signal.

THE SPEC THIS TRADES IS THE DEPLOYED ONE. ``PaperDeployment.spec_json``
snapshots the spec at deploy time precisely because the strategy row's spec can
be regenerated later; the ledger must keep grading what the user deployed. This
pass reads the deployment's snapshot for the same reason, and stamps its hash on
every trade so a row can always say which spec text produced it.

NO REGIME SNAPSHOT IS FETCHED HERE. The vault runner classifies an exogenous
market regime each tick from a VIX/S&P snapshot; doing that in the web tier
would add live market I/O to a loop that has no need of it. This pass passes
``regime=None``, which takes ``PortfolioConstructor``'s own documented
conservative default — the SAME branch a vault tick takes whenever its regime
classification fails. The consequence is disclosed rather than hidden: the paper
book is sized as a vault with an unavailable regime would be sized, not as one
with a healthy detector.

FAIL-SOFT, PER DEPLOYMENT AND AS A WHOLE. A deployment whose spec is invalid,
whose prices cannot be fetched, or whose evaluation raises is counted and
skipped; one bad universe must not stall everyone else's, the same isolation
rule ``advance_all`` follows. And the whole pass is wrapped by its caller: the
agent-execution experiment must never be able to stop a ledger advance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime

from archimedes.execution import core as execution_core
from archimedes.execution.paper_venue import PaperVenue
from archimedes.models.paper_ref import PaperRef
from archimedes.models.paper_store import STATUS_ACTIVE, PaperDeployment
from archimedes.models.strategy import StrategyPassport
from archimedes.services.portfolio_constructor import PortfolioConstructor
from archimedes.services.strategy_dsl import DSLError, validate_strategy_spec

logger = logging.getLogger(__name__)

#: Kill switch, not a ritual. Default ON: the pass is additive (it writes to one
#: new table and to nothing else), self-idempotent, and fail-soft, so requiring
#: an operator to turn it on would be a step that exists only to be forgotten.
#: The flag is here so a bad cycle can be stopped without a deploy.
ENV_ENABLED = "PAPER_AGENT_EXECUTION"

#: Minimum USDC allocation, matching the vault runner's ``AGENT_USDC_FLOOR``.
#: Read from the SAME env var on purpose — a paper book with a different cash
#: floor than the vault would be reporting on a strategy the vault does not run.
ENV_USDC_FLOOR = "AGENT_USDC_FLOOR"


def enabled() -> bool:
    """Whether the agent-execution pass runs this cycle."""
    return os.getenv(ENV_ENABLED, "1").strip().lower() not in ("0", "false", "no", "off")


def spec_hash(spec_dict: dict) -> str:
    """SHA-256 over the deployed spec's canonical JSON.

    Canonicalised (sorted keys, no incidental whitespace) so the same spec
    always hashes the same regardless of how it was serialised on the way in —
    a hash that moved with formatting would make "which spec produced this
    trade" unanswerable, which is the one question the column exists for.
    """
    canonical = json.dumps(spec_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _passport_for(dep: PaperDeployment, spec_dict: dict) -> StrategyPassport:
    """Adapt a deployment's deployed spec to the carrier the evaluator eats.

    ``StrategyPassport`` is the shape ``evaluate_strategies`` already accepts —
    curated strategies, generated strategies (via
    ``StrategyRecord.to_strategy_passport``) and now paper deployments all flow
    through it, so no bespoke duck-typed carrier is needed and no second
    evaluator entry point exists to drift.

    ``created_at`` is carried through because the evaluator uses it for the F2
    known-limit guard (a strategy older than its price window can hold a
    position the replay cannot see). Dropping it would silently disarm that
    warning on the paper path.
    """
    name = str(spec_dict.get("name") or dep.strategy_id)
    return StrategyPassport(
        id=dep.strategy_id,
        papers=[PaperRef(title=name)],
        asset_universe=list(spec_dict.get("asset_universe") or []),
        strategy_spec=spec_dict,
        created_at=dep.created_at,
    )


async def _tick_deployment(session, dep: PaperDeployment, *, usdc_floor: float, constructor) -> dict:
    """One agent tick for one deployment. Returns per-deployment counters."""
    from archimedes.services.strategy_signal_evaluator import (
        strategy_evaluator,
        synths_for_universe,
    )

    tick_id = uuid.uuid4().hex[:8]
    spec_dict = json.loads(dep.spec_json)
    # Validate before trading it, and fail this deployment's tick if it does not
    # validate. Not belt-and-braces: ``_spec_signal`` never raises on a broken
    # spec — it logs and returns FLAT — so an unvalidated bad spec would arrive
    # here as a full set of legitimate-looking flat signals, aggregate to
    # all-cash, and have the agent SELL the whole book. A broken spec must
    # produce no decision, never a confident one.
    validate_strategy_spec(spec_dict)

    passport = _passport_for(dep, spec_dict)
    synths = synths_for_universe(passport.asset_universe)
    if not synths:
        logger.info(
            "paper agent: deployment %s declares no resolvable synth (%s) — skipping",
            dep.id,
            passport.asset_universe,
        )
        return {"ticked": 0, "skipped": 1, "trades": 0}

    # yfinance-backed and synchronous, exactly as the vault runner treats it.
    all_signals = await asyncio.to_thread(strategy_evaluator.evaluate_strategies, [passport], synths)
    if not all_signals:
        logger.info("paper agent: deployment %s produced no signals — skipping", dep.id)
        return {"ticked": 0, "skipped": 1, "trades": 0}

    venue = PaperVenue(session, spec_hash(spec_dict))
    raw_weights, targets = execution_core.targets_from_signals(
        all_signals,
        venue=venue,
        constructor=constructor,
        usdc_floor=usdc_floor,
        # See the module docstring: no market snapshot is fetched on this path.
        regime=None,
        ensemble_consensus=None,
    )
    portfolio = await venue.read_portfolio(dep.id)
    trades = execution_core.compute_trades(portfolio, targets)

    decision = execution_core.TickDecision(
        tick_id=tick_id,
        portfolio=portfolio,
        targets=targets,
        trades=trades,
        signals=all_signals,
        decided_at=datetime.now(UTC),
    )
    written = await venue.execute_trades(dep.id, decision)

    logger.info(
        "paper agent: deployment %s tick %s | ensemble vote %s → %d trade(s) [%s]",
        dep.id,
        tick_id,
        " ".join(f"{k}={v:.0%}" for k, v in raw_weights.items()),
        len(written),
        " | ".join(
            f"{t.direction.value} {t.symbol} "
            f"{decision.prior_weight(t.symbol):.0%}→{decision.target_weight(t.symbol):.0%}"
            for t in trades
        )
        or "aligned",
    )
    return {"ticked": 1, "skipped": 0, "trades": len(written)}


async def advance_agent_execution(session) -> dict:
    """Run one agent tick per active deployment. Never raises for one bad row.

    Returns a cycle summary. ``failed`` counts deployments whose tick blew up;
    ``skipped`` counts ones that had nothing to decide (no resolvable universe,
    no signals) — kept apart because a skip is a normal outcome and a failure is
    not, and one summary line that conflated them would hide a real outage
    inside a normal number.
    """
    if not enabled():
        return {"enabled": False, "deployments": 0, "ticked": 0, "skipped": 0, "failed": 0, "trades": 0}

    usdc_floor = float(os.getenv(ENV_USDC_FLOOR, "0.20"))
    constructor = PortfolioConstructor()
    deps = session.query(PaperDeployment).filter(PaperDeployment.status == STATUS_ACTIVE).all()

    ticked = skipped = failed = trades = 0
    for dep in deps:
        try:
            result = await _tick_deployment(session, dep, usdc_floor=usdc_floor, constructor=constructor)
            ticked += result["ticked"]
            skipped += result["skipped"]
            trades += result["trades"]
        except (DSLError, ValueError, TypeError) as exc:
            # A bad spec / bad JSON is this deployment's data problem, and it is
            # permanent until the row changes — logged at WARNING, not ERROR,
            # and never retried into a loop.
            failed += 1
            logger.warning("paper agent: deployment %s has an unusable spec (%s) — skipping", dep.id, exc)
        except Exception:
            failed += 1
            logger.exception("paper agent: tick crashed for deployment %s — continuing", dep.id)

    return {
        "enabled": True,
        "deployments": len(deps),
        "ticked": ticked,
        "skipped": skipped,
        "failed": failed,
        "trades": trades,
    }
