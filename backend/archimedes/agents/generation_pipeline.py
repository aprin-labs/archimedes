"""Streaming strategy generation orchestrator — T1.1 Phase-3 (debate-only).

The **debate society** is the sole generation pipeline. The legacy
fusion/architect/agent decision tree is retired as of Phase-3 (issue #834).

Pipeline lifecycle (SSE stream):

  job_queued
    → brief_validated
    → pipeline_selected (always "debate" on the live path)
    → candidates_selected
    → for each leaderboard entry:
        candidate_drafted → candidate_evaluated
    → best_selected
    → trace_hashed → persisted → done

Dispatch rules:

* **debate** — ``_llm_available()`` AND ``_debate_can_run(brief)`` → runs the
  full ``_run_debate_leaderboard`` path in ``debate_engine``.
* **fixture** — deterministic stub for hermetic tests (``GENERATION_PIPELINE_FIXTURE=1``
  or ``TESTING`` env + no LLM). No LLM call, no network.
* **error** (``GENERATION_UNAVAILABLE``) — prod environment with no reachable LLM
  or an empty corpus. No silent fallback.

``llm_call_recorded`` is emitted alongside the above, once per LLM call, from
whichever stage made it (#1800). It is a **pointer** — call id, sequence, served
model, body size and digest — never a prompt or a completion; the event log has
no per-event owner gate, so bodies are read through an owner-gated route instead.

K=1 persistence: only the leaderboard winner becomes a ``StrategyRecord``;
alternates are recorded in ``strategy_memory.persist_proposal`` (verdict="rejected")
and surfaced via the job's candidates payload.

See ``docs/specs/generation-streaming-spec.md`` for the full SSE contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from archimedes.agents.prompts import PROMPTS
from archimedes.api.generate_schemas import GenerateBrief
from archimedes.services import cost_meter, llm_trace
from archimedes.services.brief_screen import Surface, Verdict, screen
from archimedes.services.identity_events import emit_identity_event
from archimedes.services.job_queue import JobStore, get_job_store
from archimedes.services.rigor_profiles import DSR_P_BADGE_MIN

logger = logging.getLogger(__name__)


# ── Mock backend for tests / no-LLM environments ──────────────────────────


def _llm_available() -> bool:
    """True iff the portfolio agent can actually call an LLM.

    Used to decide between live-agent path and the deterministic fixture
    path. Tests can force the fixture path via the env var.
    """
    if os.getenv("GENERATION_PIPELINE_FIXTURE", "").lower() in ("1", "true"):
        return False
    try:
        from archimedes.agents.portfolio_agent import get_portfolio_agent

        return get_portfolio_agent().available
    except Exception:
        return False


# ── Pipeline auto-routing ──────────────────────────────────────────────────


def _pick_pipeline(
    mode_override: str | None = None,
) -> tuple[str, str]:
    """Return the generation pipeline to use: unconditionally ``"debate"``.

    T1.1 Phase-3 cutover: the debate society IS the generation pipeline. The
    legacy fusion/architect/agent decision tree (issue #167) is retired; a
    client-sent ``mode_override`` is accepted for API compatibility but no
    longer routes anything — the society owns candidate generation (spec
    §Phase-3; #834 flag audit).
    """
    if mode_override and mode_override != "debate":
        logger.info("generation: ignoring legacy mode override %r (debate-only cutover)", mode_override)
    # This reason string is USER-FACING: it rides the SSE stream verbatim
    # (run_generation's `pipeline_selected` emit) and GenerationStream.jsx
    # renders it into every viewer's event log. Internal shorthand ("T1.1
    # Phase-3 cutover") leaked to production screens this way (#1525-era
    # review, 2026-08-30) — keep it plain product copy, and keep the
    # no-internal-jargon regression test in test_generation_pipeline.py green.
    return "debate", "the debate society — proposer and critic agents argue each candidate before the rigor gate"


# ── Brief validation (real LLM step on the live path) ─────────────────────


# The template itself lives in the prompt registry (`agents/prompts.py`), which
# is rendered into `docs/specs/prompt-inventory.md` under a drift test (#1800).
_BRIEF_VALIDATION_SYSTEM = PROMPTS["brief_validation.system"].text


def _parse_validation_json(raw: str) -> dict[str, Any] | None:
    """Extract the validation JSON object from an LLM response.

    Tolerates a leading code fence or some prose chatter — finds the first
    `{` and last `}` and parses between them.
    """
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


def _invalid_brief_message(reason: object) -> str:
    """Frame a validator rejection as guidance, never as a bare category label.

    Lives in the ORCHESTRATOR (brief validation runs before pipeline dispatch),
    so the debate-society cutover keeps this path. The validator LLM can return
    a terse ``reason`` like "gibberish" — surfacing that verbatim reads as an
    insult with zero direction (a user typing "test" saw ``Error — gibberish``).
    Keep the honest reason, wrap it in what-to-do-instead.
    """
    raw = str(reason or "it did not describe an investment goal").strip().rstrip(".")
    return (
        f"We couldn't turn that into an investment brief ({raw}). "
        "Describe what you want from a portfolio in a sentence — e.g. "
        '"diversified low-volatility strategy for idle USDC".'
    )


# ── Deterministic brief screening (no LLM) — Lane 1.3c, then #1801 ────────
#
# "Never charge for a brief we can cheaply reject." A brief that fails the
# deterministic screen must surface BRIEF_INVALID BEFORE the caller pays
# (payment happens in generate_routes.start_generation, before the job runs);
# `_validate_brief` below only runs once the job is already running. So the
# pre-payment route gate and the validator's own prelude both funnel through
# ONE function — this one — and can never drift apart on what counts as
# invalid.
#
# The rules themselves moved to archimedes.services.brief_screen (#1801),
# which added the families this file never had: an upper length bound and
# injection screening. Before that move the LANG heuristics here were the
# only deterministic check in the system and jailbreak attempts were
# deliberately deferred to the LLM validator — which failed OPEN on every
# error path, so "deferred to the validator" meant "admitted whenever the
# validator was slow, down, or confused".
#
# What has NOT changed: this stays conservative about LANGUAGE. A false
# negative (missing real gibberish) is fine — the LLM validator still sees
# it. A false positive on a genuine brief is not, because it refuses a paying
# user before they are offered the chance to pay, so unfamiliar vocabulary is
# never a rejection reason ("muni ladder", "SPY covered calls", non-English
# text all pass). What HAS changed: text carrying instructions aimed at the
# model, a link, a code block or an encoded blob is now refused here, for
# free, instead of being billed and then argued about by an LLM.


def screen_brief(brief: GenerateBrief) -> Verdict:
    """Screen ``brief.intent`` deterministically. Returns the raw Verdict."""
    return screen(brief.intent or "", Surface.BRIEF)


def cheap_brief_reject(brief: GenerateBrief) -> dict[str, str] | None:
    """Deterministic, no-LLM screen of the brief — the shared pre-payment gate.

    Returns ``None`` when the brief passes, which does NOT mean it is a *good*
    brief, only that it is not junk and carries no injection payload; the LLM
    validator remains the authority on semantics (off-topic-but-grammatical
    text, coherence). Returns a ``{"reason", "hint", "code"}`` dict, shaped
    like the LLM validator's invalid-brief output plus the machine-readable
    reason code, when the brief is refused.

    ``code`` is new in #1801 and additive: the pre-existing ``reason``/``hint``
    keys are unchanged, so every caller that only reads those is untouched.
    See ``archimedes.services.brief_screen`` for the rule families and the
    versioned code vocabulary.
    """
    verdict = screen_brief(brief)
    if verdict.allow:
        return None
    return {"reason": verdict.reason, "hint": verdict.hint, "code": verdict.code or ""}


#: Emitted instead of a validation result when the LLM validator could not
#: reach a verdict (#1801). Before this, every such path returned a permissive
#: "valid" — a slow or broken validator admitted the brief, which is the one
#: failure mode a guard is not allowed to have. Admission now requires either
#: a real "valid" verdict or nothing at all; the deterministic screen above
#: has already run, so this is not the system's only line of defence, but it
#: is honest about what it does and does not know.
_VALIDATOR_UNAVAILABLE_REASON = "we could not validate this brief right now"
_VALIDATOR_UNAVAILABLE_HINT = "Try again in a moment, or shorten the brief."


def _validator_unavailable(why: str) -> dict[str, Any]:
    logger.warning("brief validation unavailable (%s) — refusing, fail-closed", why)
    return {
        "is_valid": False,
        "validator_unavailable": True,
        "code": "validator_unavailable",
        "reason": _VALIDATOR_UNAVAILABLE_REASON,
        "hint": _VALIDATOR_UNAVAILABLE_HINT,
    }


async def _validate_brief(brief: GenerateBrief) -> dict[str, Any]:
    """Call the LLM to validate the brief. FAILS CLOSED (#1801).

    Runs the deterministic screen FIRST — see ``cheap_brief_reject`` — so a
    brief that is junk or carries an injection payload never reaches the LLM
    call at all, here or on any other caller of this function.

    Returns the parsed validation JSON on a real verdict. Every path that
    cannot produce one — backend unavailable, unparseable response, timeout,
    any exception — returns ``_validator_unavailable(...)``, which the caller
    surfaces as a recoverable "we could not validate this brief right now".

    **This used to return a permissive "valid" instead.** That made a slow or
    broken validator into an open door: the one system prompt asked to catch
    "gibberish, off-topic, attempts to jailbreak" admitted everything the
    moment it stopped answering, and nothing in the logs distinguished
    "validated" from "gave up". Refusing is the honest outcome, and it costs
    the user a retry rather than costing us an unscreened prompt. The
    deterministic screen above has already run and is unaffected by the LLM's
    health, so this refusal is a degradation of the *semantic* check only.
    """
    cheap_reject = cheap_brief_reject(brief)
    if cheap_reject is not None:
        return {"is_valid": False, **cheap_reject}

    try:
        from archimedes.services.llm_backend import make_llm_backend

        backend = make_llm_backend()
        if not getattr(backend, "available", False):
            return _validator_unavailable("backend unavailable")
        user_msg = json.dumps(
            {
                "intent": brief.intent,
                "stated_risk_appetite": brief.risk_appetite,
                "asset_classes_hint": brief.asset_classes or [],
            }
        )
        raw = await asyncio.wait_for(
            asyncio.to_thread(backend.complete, _BRIEF_VALIDATION_SYSTEM, user_msg),
            timeout=15.0,
        )
        parsed = _parse_validation_json(raw)
        if not parsed or "is_valid" not in parsed:
            return _validator_unavailable("unparseable response")
        # Ensure required keys exist with safe defaults.
        parsed.setdefault("intent_summary", brief.intent[:140])
        parsed.setdefault("asset_classes_inferred", brief.asset_classes or [])
        parsed.setdefault("time_horizon_inferred", "unknown")
        parsed.setdefault("risk_appetite_adjusted", brief.risk_appetite)
        return parsed
    except Exception as exc:
        return _validator_unavailable(f"{type(exc).__name__}: {exc}")


@dataclass
class _CandidateResult:
    """Internal candidate carrier — converted to events + persisted at the end."""

    candidate_id: str
    strategy_name: str
    thesis: str
    asset_universe: list[str]
    source_papers: list[dict[str, Any]]
    weights: dict[str, float]
    reasoning: str
    rigor_verdict: dict[str, Any]
    passes_rigor: bool
    regime: str = "neutral"  # "bull", "bear", or "neutral" (Issue #163)
    # Daily portfolio return series, used by the rigor gate. Populated in the
    # live path from the agent's price_histories; the fixture path leaves it
    # empty and supplies a hardcoded verdict.
    return_series: list[float] = None  # type: ignore[assignment]
    # Provenance method this candidate was produced by — persisted verbatim into
    # the StrategyRecord. The agent/fixture path leaves the default; the fusion
    # runner sets ``"fusion"`` so the library distinguishes multi-paper synthesis
    # from single-agent allocation. (Fusion dispatch wire, Stack A.)
    generation_method: str = "portfolio_agent_streaming"
    # The ≥2 arXiv ids fused into this candidate (fusion path only). Empty on the
    # agent path; the fusion runner populates it from the FusionProposal so the
    # passport renders real cross-paper provenance, not a placeholder.
    source_arxiv_ids: list[str] = field(default_factory=list)
    # Backtest verdict already carries DSR/PBO/OOS when the fusion evaluator ran;
    # ``has_real_rigor`` flags that the verdict is a real DSL backtest (so the
    # downstream buy-and-hold backtest step is skipped — it would clobber the
    # already-computed fusion metrics with a different, weight-less read).
    has_real_rigor: bool = False
    # The society num_trials (``_society_num_trials(N)``, decouple #2 — the
    # candidate's OWN pool, never the library) this candidate's DSR was first
    # computed with. Recorded so the post-loop correlation patch (#822)
    # recomputes DSR at the SAME trial count — only the average_correlation term
    # changes, never the deflation count itself. 0 on the fixture/fusion paths,
    # which don't run the buy-and-hold DSR patch.
    dsr_num_trials: int = 0
    # Which branch strategy_fusion._spec_universe took to pick asset_universe:
    # "user" | "model" | "full" (#857, follow-up to #847). "full" is the honest
    # default for paths that never went through fusion's universe steering (the
    # buy-and-hold agent/fixture path derives its universe from the user's
    # portfolio weights directly, not from a model suggestion).
    universe_source: str = "full"
    # The validated DSL spec dict (rebalancer decouple, Part A #1 of
    # docs/CURATED-STRATEGY-DECOUPLE-AND-CONSOLIDATE-2026-07-08.md) — the SAME
    # dict ``proposal.strategy_spec`` carries and ``evaluate_fusion_spec``
    # graded. Persisted verbatim onto the StrategyRecord (see
    # ``_persist_candidate``) so the live agent runner can later load and
    # evaluate a deployed generated strategy's OWN spec, not just its weight
    # vector. None on the fixture/buy-and-hold-weights path (there is no DSL
    # spec to carry — it re-derives its universe from ``weights`` instead).
    strategy_spec: dict[str, Any] | None = None
    # The society's own bull/bear debate transcript (debate path only —
    # ``_run_debate_leaderboard`` stamps the SAME transcript object onto every
    # leaderboard entry it returns, winner and alternates alike, since the
    # debate runs once over the whole pool before C-null picks a winner).
    # ``None`` on every non-debate path (fusion, fixture, single-agent) — there
    # is no debate on those paths, so there is nothing to attach. Read by
    # ``_persist_debate_transcripts`` below; never touches ``_HASH_FIELDS``.
    debate_transcript: list[dict[str, Any]] | None = None
    # (#1636) The deterministic per-paper tally derived from that transcript —
    # ``[{arxiv_id, title, cited_by, citations, discarded_by, discard_reasons,
    # verdict}]`` over every paper that entered a proposer prompt. Computed by
    # ``debate_engine._aggregate_paper_verdicts`` (0 tokens) and stamped onto
    # every entry, same as the transcript. ``None`` on every non-debate path.
    # (#1739) Now DURABLE: ``_persist_debate_transcripts`` appends it to the
    # same ``debate_transcripts.transcript_json`` list the turns ride, so no
    # migration was needed to stop it dying with the request.
    debate_paper_verdicts: list[dict[str, Any]] | None = None
    # (#1739) The budget-vs-used pair, carried off the proposal instead of
    # being left in a log line. ``papers_offered`` is how many papers were put
    # in front of the proposer; ``distinct_mechanism_papers`` is how many of
    # the CITED ones name a mechanism tied to an indicator the spec actually
    # trades. Together they make ``len(source_arxiv_ids)`` readable: "2 of 30
    # offered, 2 attributed" is a different claim from "5 cited, 1 attributed".
    # Both 0 on every non-fusion path (fixture / buy-and-hold), which never saw
    # a paper — 0 there means "no papers were offered", not "attribution failed".
    papers_offered: int = 0
    distinct_mechanism_papers: int = 0
    # (#1739) The proposal's per-paper mechanism prose. ``reasoning`` above
    # already falls back to ``novelty_rationale``, so it cannot be read as "the
    # model named each paper's contribution"; this field is that text or "".
    fusion_reasoning: str = ""


def _is_deployable(c: _CandidateResult) -> bool:
    """Whether a winning candidate is safe to advertise as deployable (issue #937).

    ``passes_rigor`` alone is not enough: a candidate graded on the synthetic
    fallback series (``evaluate_fusion_spec`` defaults to a random walk when no
    ``data_feed`` is wired) can have ``passing=True`` yet is NOT admissible — its
    verdict is not grounded in real data. Require BOTH the rigor pass AND
    real-data grading, the same condition that gates real-returns persistence
    (``_persist_real_returns``): ``has_real_rigor`` and a non-synthetic
    ``data_source``. Otherwise the SSE ``deployable`` flag misrepresents rigor
    status even though the server-side vault gate still fail-closes on deploy.
    """
    if not c.passes_rigor:
        return False
    rv = c.rigor_verdict or {}
    return bool(c.has_real_rigor) and str(rv.get("data_source") or "synthetic") != "synthetic"


# ── Rigor adapter (Önder's rigor_evaluator on agent output) ───────────────


def _portfolio_return_series(weights: dict[str, float], price_histories: dict[str, Any]) -> list[float]:
    """Compute daily returns of a buy-and-hold weighted portfolio.

    Sources the per-asset close series from ``price_histories`` (the same dict
    the agent saw), aligns to the shortest series, and returns ``Σ wᵢ · rᵢ,t``
    bar by bar. Buy-and-hold is the simplest faithful read of an agent
    allocation that doesn't specify rebalancing logic — for v1 it's the
    right baseline. When fusion-to-backtest lands a real DSL interpreter,
    that path will produce a real return series under the strategy's own
    rebalance rule.
    """
    closes_by_symbol: dict[str, list[float]] = {}
    for sym, weight in weights.items():
        if not weight:
            continue
        hist = price_histories.get(sym) if isinstance(price_histories, dict) else None
        if not isinstance(hist, dict):
            continue
        closes = hist.get("close")
        if closes and len(closes) > 1:
            closes_by_symbol[sym] = list(closes)
    if not closes_by_symbol:
        return []

    T = min(len(c) for c in closes_by_symbol.values())
    if T < 5:
        return []

    out: list[float] = []
    for t in range(1, T):
        bar_ret = 0.0
        for sym, closes in closes_by_symbol.items():
            prev = closes[t - 1]
            if not prev:
                continue
            r = (closes[t] - prev) / prev
            bar_ret += weights.get(sym, 0.0) * r
        out.append(bar_ret)
    return out


def _rigor_verdict_for(
    return_series: list[float],
    num_trials: int,
    *,
    lookahead_passed: bool = True,
    average_correlation: float = 0.0,
) -> dict[str, Any]:
    """Run Önder's rigor primitives on a portfolio return series.

    Returns the same shape the fixture path uses so the consumer (event
    emitter + frontend) doesn't care which path produced the verdict.

    ``num_trials`` is the multiple-testing count fed to the Deflated Sharpe
    Ratio — the candidate's OWN N-candidate selection pool it was chosen from
    (``_society_num_trials(N)``, decouple #2), NEVER the curated library's
    count. With ``num_trials=1`` the DSR expectation-of-max term collapses to
    0 and the ratio is undeflated — correct only when the caller genuinely has
    no selection pool larger than 1.

    ``average_correlation`` is the mean pairwise correlation among the
    ``num_trials`` society candidates (approach B, issue #822). Defaults to
    ``0.0`` — the independent-trials assumption (approach A, #811/#770) — which
    the caller keeps as the fallback whenever a pool-wide correlation can't be
    estimated (fewer than two candidate return series).

    ``lookahead_passed`` is the look-ahead-audit verdict, computed by the caller
    (see ``_lookahead_for_candidate``) so the primitive actually gates the
    ``passing`` decision instead of being a hardcoded constant.
    """
    if not return_series or len(return_series) < 10:
        return {
            "dsr": None,
            "pbo": None,
            "oos_sharpe": None,
            "in_sample_sharpe": None,
            "lookahead_audit_passed": lookahead_passed,
            "passing": False,
            "reason": "return series too short for rigor evaluation",
        }
    from archimedes.services.rigor_evaluator import (
        compute_dsr,
        compute_in_sample_sharpe,
        compute_oos_sharpe,
    )

    dsr, dsr_p = compute_dsr(return_series, num_trials=max(1, num_trials), average_correlation=average_correlation)
    oos = compute_oos_sharpe(return_series)
    in_sample_sharpe = compute_in_sample_sharpe(return_series)
    # PBO is library-level (needs ≥2 candidate return series); the caller
    # computes it once over all candidates and patches the verdict below.
    # All four admission primitives gate `passing`: DSR (p ≥ DSR_P_BADGE_MIN —
    # the ONE bar, shared with the badge/curated path, #1794), OOS Sharpe
    # (> 0, with no IS/OOS cliff), look-ahead audit (clean), and PBO (< 0.5,
    # patched in _patch_pbo).
    oos_pass = oos is not None and oos > 0.0
    if (
        oos_pass
        and in_sample_sharpe is not None
        and math.isfinite(in_sample_sharpe)
        and in_sample_sharpe > 0
        and oos / in_sample_sharpe < 0.5
    ):
        oos_pass = False
    passing = bool(dsr_p is not None and dsr_p >= DSR_P_BADGE_MIN and oos_pass and lookahead_passed)
    return {
        "dsr": round(float(dsr), 4) if dsr is not None else None,
        "dsr_p_value": round(float(dsr_p), 4) if dsr_p is not None else None,
        "pbo": None,  # patched later by _patch_pbo
        "oos_sharpe": round(float(oos), 4) if oos is not None else None,
        "in_sample_sharpe": round(float(in_sample_sharpe), 4) if in_sample_sharpe is not None else None,
        "lookahead_audit_passed": lookahead_passed,
        "passing": passing,
        # Deflation provenance (#1075): the N actually used and the convention
        # marker distinguishing post-decouple verdicts from stored pre-change
        # blobs (formula A included the curated library count; this one never does).
        "num_trials": int(max(1, num_trials)),
        "num_trials_convention": "self_contained_v2",
    }


def _lookahead_for_candidate(referenced_strategies: list[Any]) -> bool:
    """Look-ahead-audit verdict for a generated buy-and-hold candidate.

    The live Generate candidate is a weight vector executed buy-and-hold: its
    return series (see ``_portfolio_return_series``) is built purely from
    realized prior-bar returns ``(close[t] − close[t−1]) / close[t−1]``, so the
    *series itself* cannot leak future data. There is no signal-generating code
    of the candidate's own to feed to ``look_ahead_audit`` (which is a static
    AST audit of strategy *source*, not a return series).

    What we can honestly audit is the source of the curated strategies the
    candidate cites as provenance — a candidate grounded in a strategy whose
    code leaks future data should not pass rigor. We audit each referenced
    strategy's ``strategy_code_path`` but treat the conservative
    "negative index — verify backtrader vs pandas" warning as non-fatal: the
    curated library is backtrader-based (``close[-N]`` = N bars ago, confirmed
    leak-free), so only genuinely forward-looking patterns (positive data index,
    ``shift(-n)``, look-ahead-named calls) fail the audit here.

    Returns True when no referenced strategy exposes a genuine forward-looking
    pattern (vacuously True when none expose auditable source).
    """
    from pathlib import Path

    from archimedes.services.rigor_evaluator import look_ahead_audit

    for s in referenced_strategies:
        path = getattr(s, "strategy_code_path", None)
        if not path:
            continue
        try:
            code = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        _, warnings = look_ahead_audit(code)
        genuine = [w for w in warnings if "negative index" not in w]
        if genuine:
            logger.warning(
                "look-ahead audit flagged referenced strategy %s: %s",
                getattr(s, "paper_title", path),
                genuine,
            )
            return False
    return True


def _patch_pbo(candidates: list[_CandidateResult]) -> None:
    """Compute library-level PBO across the agent/fixture candidate set; patch each verdict.

    Fusion candidates are SKIPPED: they carry a real PBO already computed by the
    fusion evaluator's CSCV over the strategy's own parameter-variant grid
    (``has_real_rigor=True``). Overwriting that with a buy-and-hold cross-candidate
    PBO — or with the 0.0 N<2 default — would clobber the correct value and
    silently relax the gate. Only the buy-and-hold (agent/fixture) candidates,
    which have a ``return_series`` and no real rigor, participate here.
    """
    agent_cands = [c for c in candidates if not c.has_real_rigor]
    series_map: dict[str, list[float]] = {c.candidate_id: c.return_series for c in agent_cands if c.return_series}
    if len(series_map) < 2:
        for c in agent_cands:
            c.rigor_verdict["pbo"] = 0.0  # PBO undefined for N<2
        return
    from archimedes.services.rigor_evaluator import compute_pbo

    pbo_by_id = compute_pbo(series_map)
    for c in agent_cands:
        pbo = pbo_by_id.get(c.candidate_id, 0.0)
        c.rigor_verdict["pbo"] = round(float(pbo), 4)
        # PBO ≥ 0.5 means the library overfits the in-sample winner; tighten
        # `passing` to require both the per-strategy DSR test AND library-PBO
        # under the 0.5 threshold.
        c.rigor_verdict["passing"] = bool(c.rigor_verdict.get("passing") and pbo < 0.5)


def _patch_dsr_with_pool_correlation(candidates: list[_CandidateResult]) -> None:
    """Re-deflate each buy-and-hold candidate's DSR using the REAL candidate-pool ρ̄.

    Approach B (#822) for the society DSR multiple-testing correction. #811 (#770)
    set ``average_correlation=0.0`` — i.e. treated the N society candidates as
    mutually independent trials. They're generated from the same user brief over
    overlapping universes, so they're typically strongly correlated (ρ̄ ≈ 0.5–0.9),
    and feeding ρ̄=0 over-deflates ``E[max_N]``, over-stating the gate's strictness.
    Under equicorrelation the expected best-of-N null Sharpe is the
    independent-trial ``E[max]`` scaled by ``√(1 − ρ̄)`` (#1559) — this patch
    estimates the real ρ̄ across the pool's return series
    (``compute_average_pairwise_correlation``) and recomputes DSR at the SAME
    ``dsr_num_trials`` each candidate was already deflated at, so only the
    correlation term changes.

    Runs AFTER ``_patch_pbo`` and mirrors its shape and scope: fusion/debate
    candidates (``has_real_rigor=True``) carry a real DSR from their own CSCV
    evaluator and are SKIPPED — recomputing over the buy-and-hold pool would
    clobber a correctly-computed value with an unrelated one. With fewer than
    two eligible return series (no correlation estimable), every candidate's DSR
    is left untouched — approach A (ρ̄=0) is the fallback, per the issue.

    A correlated pool can only RELAX the deflation relative to ρ̄=0 (never tighten
    beyond it — ``√(1 − ρ̄) <= 1`` for every ρ̄ ∈ [0, 1]), so ``dsr_p_value`` only
    ever moves up or stays the same here, never down. ``passing`` is re-derived from the full set
    of admission legs (DSR, OOS/cliff, look-ahead) rather than AND-ed in, since a
    higher DSR p-value can flip a candidate from failing to passing, not just the
    reverse (unlike the PBO patch above, which can only tighten).
    """
    agent_cands = [c for c in candidates if not c.has_real_rigor]
    series_map: dict[str, list[float]] = {c.candidate_id: c.return_series for c in agent_cands if c.return_series}
    if len(series_map) < 2:
        return  # approach A (ρ̄=0) fallback — nothing to patch, verdicts already reflect it

    from archimedes.services.rigor_evaluator import compute_average_pairwise_correlation, compute_dsr

    avg_correlation = compute_average_pairwise_correlation(series_map)
    if avg_correlation <= 0.0:
        return  # no correlation relief to apply — ρ̄=0 verdicts are already correct

    for c in agent_cands:
        v = c.rigor_verdict
        if v.get("dsr") is None or not c.return_series or c.dsr_num_trials < 1:
            continue  # too-short series or never ran the buy-and-hold DSR — nothing to recompute
        dsr, dsr_p = compute_dsr(c.return_series, num_trials=c.dsr_num_trials, average_correlation=avg_correlation)
        if dsr is None or dsr_p is None:
            continue
        v["dsr"] = round(float(dsr), 4)
        v["dsr_p_value"] = round(float(dsr_p), 4)
        oos = v.get("oos_sharpe")
        in_sample = v.get("in_sample_sharpe")
        oos_pass = oos is not None and oos > 0.0
        if oos_pass and in_sample is not None and math.isfinite(in_sample) and in_sample > 0 and oos / in_sample < 0.5:
            oos_pass = False
        # PBO's own leg is folded into `passing` by `_patch_pbo` already; re-derive
        # from the ORIGINAL passing value's PBO contribution by requiring it again
        # here (pbo < 0.5, undefined-PBO / N<2 patched to 0.0 which always passes).
        pbo = v.get("pbo")
        pbo_pass = pbo is None or pbo < 0.5
        v["passing"] = bool(
            dsr_p >= DSR_P_BADGE_MIN and oos_pass and v.get("lookahead_audit_passed", False) and pbo_pass
        )


# ── Event emitter ─────────────────────────────────────────────────────────


class _Emitter:
    """Push events to the job's Redis event log + maintain a monotonic ID.

    Decoupled from the agent loop so the pipeline can also emit synthetic
    events (e.g. ``brief_validated``) that the agent itself doesn't know about.
    """

    def __init__(self, job_id: str, store: JobStore) -> None:
        self.job_id = job_id
        self.store = store

    async def emit(self, event: str, **payload: Any) -> int:
        ts = datetime.now(UTC).isoformat()
        body = {"event": event, "data": {"ts": ts, "job_id": self.job_id, **payload}}
        return await self.store.push_event(self.job_id, body)


def _llm_pointer_sink(emit: _Emitter) -> Callable[[dict[str, Any]], None]:
    """Build the sink that turns each recorded LLM call into ONE stream event.

    **Pointer only.** The payload is ``{call_id, seq, model_served,
    completion_bytes, completion_sha256}`` — identity and integrity, no prompt
    and no completion. The generation event log has no per-event owner gate:
    whoever holds the stream reads everything pushed to it, so a body on this
    channel would be a body published to whoever is watching. Reading a body is
    a separate owner-gated route in a later PR (#1800).

    **Thread hop.** ``complete()`` runs off the event loop (``asyncio.to_thread``
    in the debate engine), so the recorder cannot await the emitter. Capture the
    loop that owns this job and schedule the push there with
    ``call_soon_threadsafe``; the returned callable is safe to invoke from any
    thread, including the loop's own.

    Failures are swallowed at every layer — the scheduling call, the task, and
    the recorder's own ``try`` around this sink. Losing a stream event must cost
    the event and nothing else: the record is already buffered before the sink
    runs, and instrumentation may not fail a generation.
    """
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[Any]] = set()

    def _done(task: asyncio.Task[Any]) -> None:
        pending.discard(task)
        # Retrieve the exception so a failed push is not re-reported by the loop
        # as "Task exception was never retrieved".
        with contextlib.suppress(Exception, asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.debug("llm-trace: pointer event push failed", exc_info=exc)

    def _push(pointer: dict[str, Any]) -> None:
        def _schedule() -> None:
            task = asyncio.ensure_future(emit.emit("llm_call_recorded", **pointer))
            pending.add(task)
            task.add_done_callback(_done)

        with contextlib.suppress(RuntimeError):  # loop already closed — job is over
            loop.call_soon_threadsafe(_schedule)

    return _push


async def _abort_if_cancel_requested(store: JobStore, job_id: str, stage: str) -> None:
    """Stage-boundary poll of the shared cancellation flag (#1667).

    The Cancel button writes a Redis flag (``JobStore.request_cancel``) rather
    than reaching for an in-process task handle, because the task serving the
    POST is usually NOT the task running this pipeline. This is the other half
    of that contract: between stages — i.e. before each block of real LLM /
    backtest spend — the runner asks the shared store whether the user has
    cancelled, and raises ``CancelledError`` if so. ``run_generation``'s
    existing ``except asyncio.CancelledError`` branch then emits the terminal
    event and records the status, exactly as for a locally-cancelled task.

    Fail-soft on a read error: an unreachable Redis must not crash the run into
    PIPELINE_CRASH, and the flag is durable, so the next stage boundary retries.
    A store that predates this surface (a test double) is skipped the same way.
    """
    checker = getattr(store, "is_cancel_requested", None)
    if checker is None:
        logger.debug("cancel-poll skipped at %s — store exposes no cancel surface", stage)
        return
    try:
        requested = await checker(job_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("cancel-poll failed at stage boundary %s for job %s — retrying next boundary", stage, job_id)
        return
    if requested:
        logger.info("job %s: cancel flag observed at stage boundary %s — stopping the pipeline", job_id, stage)
        raise asyncio.CancelledError(f"cancelled by user before stage {stage}")


# ── Fixture path (deterministic; used in tests + when LLM unavailable) ────


async def _run_fixture_candidate(
    *,
    candidate_id: str,
    brief: GenerateBrief,
    emit: _Emitter,
    regime: str = "neutral",
    agent: Any = None,  # noqa: ARG001 — signature parity; fixture path ignores it
    selection_pool_size: int = 1,  # noqa: ARG001 — signature parity; fixture path computes no DSR num_trials
) -> _CandidateResult:
    """Synthetic generation that exercises every event the live agent emits.

    Useful for: tests, demo on a laptop without an API key, smoke-tests.
    Each step has a short sleep so the SSE stream actually streams rather
    than dumping everything at once on connect.
    """
    await emit.emit("agent_iteration", candidate_id=candidate_id, iteration_n=1, max_iterations=3)
    await asyncio.sleep(0.1)

    await emit.emit(
        "tool_called", candidate_id=candidate_id, tool_name="get_asset_stats", args_summary="symbols=sBTC,sSPY,sGLD"
    )
    await asyncio.sleep(0.1)
    await emit.emit(
        "tool_result",
        candidate_id=candidate_id,
        tool_name="get_asset_stats",
        result_summary="3-asset stats fetched; sGLD lowest vol",
    )

    await emit.emit("agent_iteration", candidate_id=candidate_id, iteration_n=2, max_iterations=3)
    await emit.emit(
        "tool_called", candidate_id=candidate_id, tool_name="stress_test_portfolio", args_summary="scenarios=6"
    )
    await emit.emit(
        "tool_result",
        candidate_id=candidate_id,
        tool_name="stress_test_portfolio",
        result_summary="max drawdown −12.4% (2022_inflation)",
    )

    # Regime-aware fixture names and weights
    # Include the user's brief intent in the name so each generation
    # produces a distinct, meaningful title (Issue #336).
    intent_snippet = brief.intent[:50].strip() if brief.intent else "Multi-Asset"
    if regime == "bull":
        name = f"🟢 Bull {brief.risk_appetite.title()} — {intent_snippet}"
        weights = {"sSPY": 0.55, "sBTC": 0.30, "sGLD": 0.15}
    elif regime == "bear":
        name = f"🔴 Bear {brief.risk_appetite.title()} — {intent_snippet}"
        weights = {"sGLD": 0.45, "sSPY": 0.30, "sBTC": 0.05, "sUSDC": 0.20}
    else:
        name = f"{brief.risk_appetite.title()} Blend — {intent_snippet}"
        weights = {"sSPY": 0.5, "sGLD": 0.3, "sBTC": 0.2}
    # Fixture source_papers: pull from curated library (same fallback as live)
    fixture_source_papers: list[dict[str, Any]] = []
    try:
        from archimedes.services.strategy_provider import default_provider

        for s in default_provider().list_strategies()[:3]:
            title = getattr(s, "paper_title", "") or ""
            arxiv_id = getattr(s, "paper_arxiv_id", "") or ""
            if title or arxiv_id:
                fixture_source_papers.append({"arxiv_id": arxiv_id, "title": title})
    except Exception:
        logger.debug("failed to collect fixture source papers", exc_info=True)

    return _CandidateResult(
        candidate_id=candidate_id,
        strategy_name=name,
        thesis=f"Fixture-mode {regime} generation for brief: {brief.intent[:120]}",
        asset_universe=list(weights.keys()),
        source_papers=fixture_source_papers,
        weights=weights,
        reasoning=f"Fixture path ({regime} regime) — no LLM call. Weights chosen by deterministic stub.",
        rigor_verdict={
            "dsr": None,
            "pbo": None,
            "oos_sharpe": None,
            "in_sample_sharpe": None,
            "lookahead_audit_passed": False,
            "passing": False,
            "reason": "fixture mode — no LLM call, rigor gate not run",
        },
        passes_rigor=False,
        regime=regime,
    )


def _society_num_trials(selection_pool_size: int) -> int:
    """Effective DSR multiple-testing trial count on the agentic society path.

    A strategy's rigor must depend ONLY on that strategy — never on how many OTHER
    strategies happen to sit in the library (Dan's principle, 2026-07-09). The trial
    count that deflates this winner's Sharpe is therefore the size of ITS OWN
    selection set: the ``selection_pool_size`` (N) generated candidates it was chosen
    from — NOT ``N + library_size``. Promoting a strategy into a bigger library must
    not retroactively change its Deflated Sharpe. Floored at 1.

    NOTE: this REVERSES the ``N + library_size`` additive convention from #770/#811/#820
    (which treated the library as a second selection layer). See the decouple plan in
    ``docs/CURATED-STRATEGY-DECOUPLE-AND-CONSOLIDATE-2026-07-08.md`` Part A #2.
    Ratified by Önder Akkaya (portfolio math) 2026-08-31 — #1555 outcome 3; see
    ``docs/adr/num-trials-self-containment.md`` § Ratification.
    """
    return max(1, selection_pool_size)


class FusionUnavailable(Exception):
    """The debate society could not produce a conformant candidate for this brief.

    Raised by ``debate_engine.DebateUnavailable`` (a subclass) when no proposal
    survived the critics. ``run_generation`` catches it and emits
    ``GENERATION_UNAVAILABLE`` — no silent fallback (T1.1 Phase-3).
    """


# ── Pipeline entry point ──────────────────────────────────────────────────


def _resolve_name(brief: GenerateBrief, default: str) -> str:
    """Prefer the user's ``brief.name`` over an auto-derived default.

    Single choke point for user-chosen strategy names: every runner
    (fixture / debate) auto-derives a ``strategy_name``,
    and ``run_generation`` applies the user's name to the WINNER only — right
    before persistence, so the library record, passport, episodic memory, and
    job result all carry it. ``brief.name`` is already sanitized (stripped,
    whitespace-collapsed, 1–80 chars) by the ``GenerateBrief`` validator.
    """
    return brief.name or default


def _served_model_for(job_agent: Any, use_live: bool) -> str:
    """Resolve the model id that actually served this job, for provenance.

    Prefers the per-job agent's backend (when the user picked a model), else the
    shared singleton. Reads ``served_model`` (post-call truth, e.g. response.model)
    when present, falling back to ``model_id``. Returns the fixture marker on the
    non-live path so the UI never claims a real model ran when it didn't.
    """
    if not use_live:
        return "fixture"
    try:
        from archimedes.agents.portfolio_agent import get_portfolio_agent

        agent = job_agent or get_portfolio_agent()
        backend = getattr(agent, "_backend", None)
        served = getattr(backend, "served_model", None) or getattr(backend, "model_id", None)
        if served:
            return str(served)
        return getattr(agent, "model_id", "unknown")
    except Exception:
        return "unknown"


def _quote_in_force() -> dict[str, Any] | None:
    """The literal ``generation_payment.quote()`` payload, or ``None``.

    Read-only use of the quote seam (#1326 anti-goal: don't touch it). The
    payload is recorded verbatim next to the measurement so the passport can
    show "quoted X / measured Y" as two facts we wrote down, rather than as a
    conversion — no token count is ever turned into money server-side.

    ``None`` when the seam cannot be read. That is an honest "we did not record
    what was quoted", and it renders as unknown; it is never a zero price.
    """
    try:
        from archimedes.services import generation_payment

        return dict(generation_payment.quote())
    except Exception:
        logger.warning("cost record: could not read the generation quote — recording it as unknown", exc_info=True)
        return None


async def _persist_generation_cost(
    *,
    job_id: str,
    strategy_ids: dict[str, str],
    measurement: dict[str, Any],
    quote: dict[str, Any] | None,
) -> None:
    """Write the durable ``generation_costs`` row(s) for this job (#1326).

    No strategy row ⇒ nothing to key a durable record to, so nothing is written
    and the job record (with its TTL) remains the only copy. That is the issue's
    own boundary: persist "on rigor-failed/errored terminal paths **where a
    strategy row exists**".

    Two deliberate omissions:

    * The write is **not** tallied through ``cost_meter.record_write``. The
      snapshot being written is already sealed — a tally could not appear inside
      the document it is counting — and ``record_write("generation_costs")``
      raises :class:`~archimedes.services.cost_meter.PricingLeakError` anyway,
      because "cost" is pricing vocabulary inside a measurement record. The
      table's *name* is fine; a counter label inside a ``cost_v1`` snapshot is
      not, and that screen is working as designed.
    * Failure is logged, never raised. Instrumentation is not allowed to change
      the outcome of a generation (``docs/generation-cost-instrumentation.md``
      § guarantee 5), and this runs in a ``finally`` that may already be
      unwinding a cancellation.
    """
    if not strategy_ids:
        logger.debug("cost record: job %s produced no strategy row — durable cost not written", job_id)
        return

    def _write() -> int:
        from archimedes.db import get_session
        from archimedes.models.generation_cost import record_generation_cost

        written = 0
        with get_session() as session:
            for strategy_id in dict.fromkeys(strategy_ids.values()):
                if not strategy_id:
                    continue
                record_generation_cost(
                    session,
                    job_id=job_id,
                    strategy_id=strategy_id,
                    measurement=measurement,
                    quote=quote,
                )
                written += 1
            session.commit()
        return written

    try:
        count = await asyncio.to_thread(_write)
        logger.info("cost record: persisted %d durable generation cost row(s) for job %s", count, job_id)
    except (Exception, asyncio.CancelledError):
        logger.warning("cost record: durable persist failed for job %s (non-blocking)", job_id, exc_info=True)


def _passport_paper_refs(c: _CandidateResult) -> list[Any]:
    """The passport's paper rows, with the attributed mechanism in ``contribution`` (#1739).

    ``_persist_candidate`` and its two sibling passport rebuilds all built
    ``PaperRef(arxiv_id=…, title=…)`` and nothing else, so ``contribution`` —
    the column ``StrategyPassport.jsx`` renders as "per-paper contribution" —
    had no writer at all and every generated row showed an em-dash. The
    passport is what ``GET /strategies/{id}`` serves (``paper_refs``, not
    ``StrategyRecord.source_papers``), and unlike the owner-only debate panel it
    is public, so this is the only path on which the paper→mechanism link
    reaches a reader of a published strategy.

    ``contribution`` stays **None** for a cited paper whose ``spec_elements``
    did not survive validation. The em-dash IS the unattributed record: writing
    the model's unverified mechanism prose there would reinstate exactly the
    laundering this issue removes, one column to the right.
    """
    from archimedes.models.paper_ref import PaperRef

    refs: list[Any] = []
    for p in c.source_papers or []:
        if not isinstance(p, dict):
            continue
        mechanism = str(p.get("mechanism", "") or "").strip()
        refs.append(
            PaperRef(
                arxiv_id=p.get("arxiv_id"),
                title=p.get("title", ""),
                contribution=mechanism if (mechanism and p.get("spec_elements")) else None,
            )
        )
    return refs


def _paper_attribution_entry(c: _CandidateResult) -> dict[str, Any] | None:
    """The ONE paper-attribution entry for a candidate, or ``None`` when there is
    nothing to attribute.

    Extracted out of :func:`_transcript_with_paper_record` because this entry
    now has TWO consumers, and its summary sentence must exist in exactly one
    place or they drift into two different claims about the same run:

    * :func:`_transcript_with_paper_record` appends it to the persisted
      ``debate_transcripts.transcript_json`` list, which is what the passport
      reads back through ``GET /api/strategies/{id}/debate``;
    * ``debate_engine._run_debate_leaderboard`` pushes it onto the live SSE
      stream as ``debate_attribution``, so a user watching a generation sees
      the per-paper record while it happens rather than only afterwards.

    Callers on the SSE path MUST run it through
    ``models.debate_transcript.sanitize_transcript`` themselves: both new keys
    carry model prose (``fusion_reasoning`` directly, each ``paper_verdicts``
    row's ``discard_reasons`` by way of ``_aggregate_paper_verdicts``), and only
    the DB writer scrubs on the way in.
    """
    verdicts = c.debate_paper_verdicts or []
    reasoning = (c.fusion_reasoning or "").strip()
    if not verdicts and not reasoning:
        return None
    engaged = sum(1 for v in verdicts if isinstance(v, dict) and v.get("verdict") != "unused")
    summary = (
        f"Paper attribution: {engaged} of {len(verdicts)} retrieved paper(s) were cited or "
        f"discarded by name in this debate; {c.distinct_mechanism_papers} of "
        f"{len(c.source_arxiv_ids)} cited paper(s) name a mechanism this strategy trades."
    )
    return {
        "role": "attribution",
        "round": None,
        "verdict": summary,
        "paper_verdicts": verdicts,
        "fusion_reasoning": reasoning,
    }


def _passport_spec_fields(c: _CandidateResult) -> dict[str, Any]:
    """The passport's three executable card fields, derived from the DSL spec (#1769).

    ``**``-spread into every ``StrategyPassport(...)`` construction below, for
    the same reason ``_passport_paper_refs`` is shared: the three writers
    (``_persist_candidate`` and the two real-metric rebuilds) each re-declare the
    passport from scratch, and a field only one of them sets is a field the next
    rebuild silently reverts.

    Before this, all three passed ``asset_universe=c.asset_universe or []`` and
    passed NEITHER ``rebalance_frequency`` NOR ``position_sizing`` — so every
    generated row in ``strategy_passports`` took the column defaults, ``weekly``
    and ``equal_weight``, whatever its spec said. The card then described a
    strategy nobody had backtested. Owner dogfood 2026-09-01 caught it on a spec
    saying ``monthly`` / ``full_invested_when_in_market``.

    Falls back to the candidate's own universe (and the dataclass defaults for
    the other two) when there is no validating spec — the fixture /
    buy-and-hold path has none, and inventing a cadence for it would be the same
    defect with the sign flipped.
    """
    from archimedes.models.strategy import PositionSizing, RebalanceFrequency
    from archimedes.services.passport_spec_parity import card_fields_from_spec

    derived = card_fields_from_spec(c.strategy_spec, strategy_id=c.candidate_id)
    if derived is None:
        return {"asset_universe": c.asset_universe or []}
    return {
        "asset_universe": derived["asset_universe"],
        # Both enums are supersets of the DSL's closed vocabulary (see
        # PositionSizing's docstring), and `card_fields_from_spec` returns only
        # values `validate_strategy_spec` admitted — so these constructions
        # cannot raise on a spec that reached here.
        "rebalance_frequency": RebalanceFrequency(derived["rebalance_frequency"]),
        "position_sizing": PositionSizing(derived["position_sizing"]),
    }


def _transcript_with_paper_record(c: _CandidateResult) -> list[dict[str, Any]]:
    """The candidate's transcript plus one trailing paper-attribution entry (#1739).

    ``debate_paper_verdicts`` was computed for free off the transcript we
    already paid for and then thrown away at the end of the request — the run
    that argued over 30 papers left no durable record of which ones it engaged
    with. Same for ``fusion_reasoning``, the model's per-paper mechanism prose,
    which ``_CandidateResult.reasoning`` collapses with ``novelty_rationale``.

    Both ride the EXISTING ``transcript_json`` column as one extra list entry —
    no migration, no new table, no schema change (#1739 carries none). The
    entry keeps the turn shape (``role``/``verdict``) so a reader that walks
    turns sees an honest summary line rather than a blank card, and carries the
    machine-readable tally alongside it.

    Both new keys carry MODEL prose — ``fusion_reasoning`` directly, and each
    ``paper_verdicts`` row's ``discard_reasons``, which
    ``_aggregate_paper_verdicts`` copies out of the raw debate turns. They are
    scrubbed by ``sanitize_transcript``, which ``record_debate_transcript``
    applies to everything written to this table; teaching the sanitizer the new
    shape (rather than scrubbing at this one call site) is what keeps the
    table's stated contract true — a raw read of ``transcript_json`` is already
    safe, whoever wrote the row.
    """
    transcript = list(c.debate_transcript or [])
    entry = _paper_attribution_entry(c)
    return transcript if entry is None else [*transcript, entry]


async def _persist_debate_transcripts(
    *,
    job_id: str,
    candidates: list[_CandidateResult],
    strategy_ids: dict[str, str],
) -> None:
    """Persist each candidate's debate transcript, keyed to (generation, candidate).

    Every leaderboard entry a debate run produces carries the SAME transcript
    (``_run_debate_leaderboard`` stamps it onto every entry it returns, since
    the debate runs once over the whole pool before C-null picks a winner) —
    so a single pass over ``candidates`` here persists it for the K=1 winner
    (with its real ``strategy_id`` from ``strategy_ids``) AND every
    final-round loser (``strategy_id=None`` — losers are never threaded into
    ``strategy_store``; see ``_persist_candidate``'s K=1 docstring), entirely
    from data already collected by the time this is called. Non-debate
    candidates (fusion, fixture, single-agent) carry ``debate_transcript=None``
    and are skipped — there is nothing to persist for them.

    (#1739) Each row also carries the run's ``debate_paper_verdicts`` tally and
    the proposal's ``fusion_reasoning``, appended by
    :func:`_transcript_with_paper_record` as one extra entry on the same JSON
    list the column already holds — so the per-paper record outlives the
    request instead of being in-process + SSE-visible only.

    Best-effort, like the sibling :func:`_persist_generation_cost`: this runs
    after the winner's strategy row already landed, so a failure here must
    never fail (or retroactively un-succeed) a generation that already
    succeeded.
    """
    to_write = [c for c in candidates if c.debate_transcript]
    if not to_write:
        return

    def _write() -> int:
        from archimedes.db import get_session
        from archimedes.models.debate_transcript import record_debate_transcript

        written = 0
        with get_session() as session:
            for c in to_write:
                record_debate_transcript(
                    session,
                    strategy_id=strategy_ids.get(c.candidate_id),
                    generation_id=job_id,
                    candidate_id=c.candidate_id,
                    transcript=_transcript_with_paper_record(c),
                )
                written += 1
            session.commit()
        return written

    try:
        count = await asyncio.to_thread(_write)
        logger.info("debate transcript: persisted %d row(s) for job %s", count, job_id)
    except (Exception, asyncio.CancelledError):
        logger.warning("debate transcript: persist failed for job %s (non-blocking)", job_id, exc_info=True)


async def run_generation(
    *,
    job_id: str,
    brief: GenerateBrief,
    n_candidates: int = 1,
    store: JobStore | None = None,
    mode: str | None = None,
    model: str | None = None,
    owner_wallet: str | None = None,
    owner_user_id: str | None = None,
    dual_regime: bool = True,
) -> None:
    """Run the full streaming generation pipeline for one job.

    When ``dual_regime=True`` (the default since Issue #163), the pipeline
    generates BOTH a bull-tilted AND a bear-tilted candidate. Each regime
    run uses biased paper retrieval + regime-specific reasoning. The user
    sees both candidates with regime tags and can deploy one, both, or neither.

    ``model`` is the user's optional free-tier model pick (already allowlisted
    by the route). When set, the live path constructs a per-job LLM backend on
    that model; when ``None`` it uses the shared singleton on the env default —
    behavior UNCHANGED.

    ``owner_user_id`` is the canonical Better Auth owner, bound server-side.
    ``owner_wallet`` is optional verified-wallet provenance retained for
    on-chain compatibility. Neither value is client supplied.

    Designed to be called as a fire-and-forget asyncio task from the route
    handler. Exceptions are caught + emitted as ``error`` events so the SSE
    client always sees a terminal state.
    """
    store = store or get_job_store()
    emit = _Emitter(job_id, store)

    # Cost instrumentation (#1217). Bound for the whole job so every LLM call
    # made anywhere beneath this frame — including inside `asyncio.to_thread`
    # workers, which copy the context — lands in one per-job record. The
    # snapshot is persisted in the `finally` below, so it exists for the
    # rigor-FAILED and errored runs too; those spend the same backtest compute
    # and are the common case, which is exactly what this measurement is for.
    meter = cost_meter.CostMeter(job_id=job_id)
    meter_token = cost_meter.bind(meter)
    meter.set_meta("n_candidates_requested", n_candidates)
    meter.set_meta("model_requested", model)

    # Raw LLM trace capture (#1800). Bound over the same frame as the meter and
    # for the same reason: every `complete()` beneath here — including the ones
    # `asyncio.to_thread` runs on worker threads, which copy the context — hands
    # its PROVIDER RESPONSE to this recorder before any lossy extraction runs.
    # In-memory only in this PR: no S3, no Aurora, nothing outlives the job (the
    # `unbind` in the `finally` clears the buffer). What the stream gets is a
    # pointer per call, never a body.
    trace = llm_trace.LLMTraceRecorder(job_id=job_id, pointer_sink=_llm_pointer_sink(emit))
    trace_token = llm_trace.bind(trace)

    # The price quote in force as this job starts (#1326). Read once, here,
    # because that is the quote the caller was actually charged against — a
    # quote re-read at the end would be a different fact wearing this run's
    # name. Stored in its OWN column beside the measurement so quote-vs-measured
    # is a pairing of two recorded facts, never a conversion. Read-only: the
    # quote seam itself is untouched.
    quote_in_force = _quote_in_force()

    # Populated by the persist step inside the try. Declared out here so the
    # `finally` can write the durable cost row on the rigor-FAILED, errored and
    # cancelled paths too — every path where a strategy row already exists.
    strategy_ids: dict[str, str] = {}  # candidate_id → strategy_id

    try:
        # Stage boundary 0 (#1667): a job cancelled while it waited behind the
        # admission gate must not be promoted back to `running` and start
        # spending. The flag landed before this task ever woke up, so this is
        # the boundary that catches it — inside the try, so the CancelledError
        # branch below records the terminal state and the meter unbinds.
        await _abort_if_cancel_requested(store, job_id, "start")

        await store.update_status(job_id, "running")
        await emit.emit("job_queued", brief=brief.model_dump())

        # Real LLM validation step (live path only). The fixture path skips
        # the validator so tests stay hermetic.
        with meter.stage("brief_validation"):
            if _llm_available():
                validated = await _validate_brief(brief)
            else:
                validated = {
                    "is_valid": True,
                    "intent_summary": brief.intent[:140],
                    "asset_classes_inferred": brief.asset_classes or [],
                    "time_horizon_inferred": "unknown",
                    "risk_appetite_adjusted": brief.risk_appetite,
                }

        if not validated.get("is_valid", True):
            # Brief refused — emit a recoverable error and stop. Frontend
            # already handles `error` with recoverable=true by offering a
            # "regenerate" CTA with the reason inline.
            #
            # Two distinct outcomes ride this branch and they must not be
            # conflated (#1801). A REJECTED brief is a statement about the
            # brief, so it gets `_invalid_brief_message`'s "we couldn't turn
            # that into an investment brief (…)" framing. An UNVALIDATED
            # brief is a statement about US — the validator could not reach a
            # verdict — and telling that user their brief was invalid would
            # be a false claim about text we never actually judged.
            unavailable = bool(validated.get("validator_unavailable"))
            if unavailable:
                message = (
                    "We could not validate this brief right now — try again, or shorten it. "
                    "Nothing was generated and no work was spent."
                )
            else:
                message = _invalid_brief_message(validated.get("reason"))
            await emit.emit(
                "error",
                message=message,
                hint=validated.get("hint") or "Mention an asset class, a goal, or a risk appetite.",
                recoverable=True,
                code="BRIEF_UNVALIDATED" if unavailable else "BRIEF_INVALID",
                # Machine-readable reason code from services.brief_screen's
                # versioned vocabulary (or "validator_unavailable"). Additive:
                # every pre-#1801 field on this event is unchanged.
                reason_code=validated.get("code") or "",
            )
            meter.set_meta("outcome", "validator_unavailable" if unavailable else "brief_invalid")
            await store.update_status(
                job_id, "error", error="brief could not be validated" if unavailable else "brief invalid"
            )
            return

        # Honor any risk_appetite_adjusted from the validator (e.g. the user
        # said "conservative" but described 100x leverage on memecoins).
        if validated.get("risk_appetite_adjusted") and validated["risk_appetite_adjusted"] != brief.risk_appetite:
            brief = brief.model_copy(update={"risk_appetite": validated["risk_appetite_adjusted"]})

        # Fold the validator's classified asset_classes back into the brief
        # (#892). Bug: brief_validated correctly classified e.g.
        # ["crypto", "treasuries"] and emitted it on the SSE event for display,
        # but the classification was then discarded — brief.asset_classes kept
        # whatever the client originally sent (often empty, or only the
        # explicit ticker picks), so the debate society / fusion universe
        # steering below never saw the LLM's better read of the brief. Union
        # (not replace) so an explicit user ticker pick is never dropped by a
        # coarser class inference.
        # The validator's output is LLM-generated and not type-enforced, so
        # `asset_classes_inferred` could come back as something other than a
        # list (e.g. a bare string) — guard against that before iterating, or
        # a string would silently explode into a "class per character" and
        # pollute brief.asset_classes downstream (Copilot review comment on
        # PR #1033).
        _raw_inferred = validated.get("asset_classes_inferred", [])
        if not isinstance(_raw_inferred, list):
            _raw_inferred = [_raw_inferred] if _raw_inferred else []
        inferred_classes = [str(a).strip() for a in _raw_inferred if str(a).strip()]
        if inferred_classes:
            merged_classes = list(brief.asset_classes or [])
            seen_classes = {c.strip().lower() for c in merged_classes}
            for ac in inferred_classes:
                if ac.lower() not in seen_classes:
                    seen_classes.add(ac.lower())
                    merged_classes.append(ac)
            if merged_classes != (brief.asset_classes or []):
                brief = brief.model_copy(update={"asset_classes": merged_classes})

        await emit.emit(
            "brief_validated",
            asset_classes=validated.get("asset_classes_inferred", []),
            risk_appetite=brief.risk_appetite,
            intent_summary=validated.get("intent_summary", ""),
            time_horizon_inferred=validated.get("time_horizon_inferred", "unknown"),
        )

        # ── Auto-route to the best pipeline ──
        pipeline_name, pipeline_reason = _pick_pipeline(mode_override=mode)

        # ── T1.1 Phase-3 dispatch: debate is THE runner. ──
        # No silent fallback to the retired single-agent paths: if the society
        # cannot run, the job errors HONESTLY. The deterministic fixture runner
        # survives strictly for hermetic tests (TESTING / explicit fixture env).
        from archimedes.agents.corpus_viability import REASON_CORPUS_UNAVAILABLE, assess_corpus_viability
        from archimedes.agents.debate_engine import _debate_can_run, _run_debate_leaderboard

        use_live = _llm_available()
        fixture_mode = os.getenv("GENERATION_PIPELINE_FIXTURE", "").lower() in ("1", "true") or (
            not use_live and os.getenv("TESTING")
        )
        await _abort_if_cancel_requested(store, job_id, "pipeline_select")
        with meter.stage("pipeline_select"):
            debate_can_run = use_live and await asyncio.to_thread(_debate_can_run, brief)
        if debate_can_run:
            runner: Callable[..., Awaitable[Any]] = functools.partial(_run_debate_leaderboard, model=model)
        elif fixture_mode:
            pipeline_name = "fixture"
            pipeline_reason = "deterministic fixture runner (tests only — no LLM in the environment)"
            runner = _run_fixture_candidate
        else:
            # FAILED BEFORE SYNTHESIS. Nothing has been drafted, backtested or
            # persisted at this point in the pipeline, so there is no partial
            # strategy for Library/leaderboard to pick up — but the job record
            # has to SAY so, with the reason, rather than carrying a bare
            # "error" string. (The owner's screenshot of this failure: one red
            # line in the event log, and no way forward.)
            #
            # Two distinct failures share the GENERATION_UNAVAILABLE code:
            # no LLM backend (nothing about the brief can fix it), and a corpus
            # that yielded < MIN_PAPERS candidates for this steer (which the
            # user CAN act on). Only the second one pays for a corpus
            # assessment — it re-runs the same retrieval the precheck just ran,
            # this time keeping the count and deriving broadening suggestions
            # from the corpus itself. No LLM call.
            if not use_live:
                reason = "no LLM backend reachable"
                failure: dict[str, Any] = {"reason_code": "NO_LLM_BACKEND", "steer": brief.intent or ""}
                message = (
                    "Generation stopped before synthesis: no LLM backend is reachable right now, "
                    "so no strategy was drafted or saved. Nothing in your brief caused this."
                )
            else:
                viability = await asyncio.to_thread(assess_corpus_viability, brief)
                # The machine reason, preserved verbatim from the previous
                # wording so log greps and the job record read the same string
                # they always did. `reason_code` carries the finer distinction
                # (too few candidates vs. no corpus loaded at all).
                reason = "the corpus yielded <2 papers for this steer — the society cannot fuse"
                if viability.can_run:
                    # The gate already said no; this second retrieval says yes
                    # (transient DB failure into the file fallback, a concurrent
                    # intake, …). We are committed to the failure branch, so its
                    # counts would contradict it: "matched 3 papers … needs at
                    # least 2" under a run that did not happen. Report the
                    # disagreement with no numbers attached — CORPUS_UNAVAILABLE
                    # renders the one-line message and no ways forward, which is
                    # the honest reading when we cannot say what retrieval found.
                    failure = {"reason_code": REASON_CORPUS_UNAVAILABLE, "steer": brief.intent or ""}
                    message = (
                        "Generation stopped before synthesis: the corpus check that gates the society "
                        "and the one that explains it disagreed, so no strategy was drafted or saved."
                    )
                else:
                    failure = viability.as_event_fields()
                    message = viability.message()
            await emit.emit(
                "error",
                message=message,
                recoverable=True,
                code="GENERATION_UNAVAILABLE",
                reason=reason,
                **failure,
            )
            meter.set_meta("outcome", "generation_unavailable")
            await store.update_status(
                job_id,
                "error",
                error=f"generation unavailable: {reason}",
                result={"failed_before_synthesis": True, "failure": {"code": "GENERATION_UNAVAILABLE", **failure}},
            )
            return

        # Regime plan AFTER the runner is final: the society owns its own
        # regime/mechanism split internally (spec §5b) → a single "neutral"
        # pass; the fixture path keeps the legacy dual-regime shape for tests.
        if pipeline_name == "debate":
            regimes: list[str] = ["neutral"]
        elif dual_regime:
            regimes = ["bull", "bear"]
        else:
            regimes = ["neutral"] * n_candidates

        await emit.emit(
            "pipeline_selected",
            pipeline=pipeline_name,
            reason=pipeline_reason,
            regimes=regimes,
        )

        # If the user picked an allowlisted free-tier model, build a per-job
        # portfolio agent bound to that model; otherwise reuse the shared
        # singleton on the env default (behavior unchanged). Constructed once and
        # shared across both regime candidates so we don't rebuild a client twice.
        #
        # NOTE: `runner` is resolved ABOVE in the dispatch block. Do NOT redefine it here.
        job_agent = None
        if use_live and model:
            try:
                from archimedes.agents.portfolio_agent import PortfolioAgent
                from archimedes.services.llm_backend import make_llm_backend

                job_agent = PortfolioAgent(backend=make_llm_backend(model=model))
            except Exception as exc:
                logger.warning("could not build per-job agent for model %r (%s); using default", model, exc)
                job_agent = None
        # No papers claim here — deliberately. This event fires BEFORE any
        # corpus retrieval happens (paper selection runs inside each
        # candidate's debate), so no honest papers count exists yet. The old
        # payload sliced the CURATED LIBRARY's arxiv ids to max_papers, which
        # made the stream claim a constant "N papers" from the wrong
        # population at the wrong time. The real, provenance-checked citations
        # ride each candidate_drafted event below instead.
        await emit.emit(
            "candidates_selected",
            candidate_count=len(regimes),
            regimes=regimes,
        )

        candidates: list[_CandidateResult] = []
        for i, regime in enumerate(regimes):
            candidate_id = f"cand_{regime}" if dual_regime else f"cand_{i + 1}"
            # The token-burn boundary: each pass through this loop is a full
            # debate (sequential adversarial LLM turns + per-proposal
            # backtests). Never start another one after a cancel.
            await _abort_if_cancel_requested(store, job_id, f"candidate_generation:{candidate_id}")
            try:
                # The society's own sub-phases (corpus load, proposal fan-out,
                # adversarial transcript, per-proposal backtests) are timed
                # separately inside debate_engine; this outer stage is the total.
                with meter.stage("candidate_generation"):
                    cand = await runner(
                        candidate_id=candidate_id,
                        brief=brief,
                        emit=emit,
                        regime=regime,
                        agent=job_agent,
                        selection_pool_size=n_candidates,  # #770: DSR deflates for the N-candidate search
                    )
            except FusionUnavailable as exc:
                # T1.1 Phase-3: the society declined at runtime (empty pool /
                # nothing conformant). There is NO fallback pipeline anymore —
                # surface the honest first-class ABSTAIN and let the
                # NO_CANDIDATES path below report it if nothing else lands.
                logger.info("debate society abstained for %s (%s): %s", candidate_id, regime, exc)
                await emit.emit(
                    "candidate_failed",
                    candidate_id=candidate_id,
                    regime=regime,
                    error=str(exc),
                    message="The debate society abstained — no proposal survived the critics for this brief.",
                )
                continue
            except Exception as exc:
                logger.exception("candidate %s (%s) failed: %s", candidate_id, regime, exc)
                await emit.emit(
                    "candidate_failed",
                    candidate_id=candidate_id,
                    regime=regime,
                    error=str(exc),
                    message=f"No {regime} candidate available — your brief may be structurally one-sided.",
                )
                continue

            # Phase-3 fan-out: the debate runner returns the FULL ranked
            # leaderboard (leader first); legacy/fixture runners return one
            # candidate. Every entry is surfaced on the stream — the tail IS
            # the Considered-Alternatives panel's content.
            entries = cand if isinstance(cand, list) else [cand]
            for entry in entries:
                await emit.emit(
                    "candidate_drafted",
                    candidate_id=entry.candidate_id,
                    strategy_name=entry.strategy_name,
                    weights_preview=entry.weights,
                    regime=regime,
                    # The papers this accepted proposal actually cites —
                    # provenance-checked against the corpus surface by the
                    # debate critics (_critic_prov), never the depth knob.
                    source_arxiv_ids=[aid for p in entry.source_papers if (aid := p.get("arxiv_id", ""))]
                    or list(entry.source_arxiv_ids),
                )
                await emit.emit(
                    "candidate_evaluated",
                    candidate_id=entry.candidate_id,
                    rigor_verdict=entry.rigor_verdict,
                    regime=regime,
                )
            candidates.extend(entries)

        if not candidates:
            # Honest code + message (#818): this branch fires only when ZERO candidates
            # were generated (an upstream generation failure) — distinct from "candidates
            # exist but none passed rigor", which is NOT an error but is surfaced below via
            # best_selected's deployable=False (ABSTAIN) signal. Using RIGOR_FAIL here would
            # mislead clients/telemetry into reading a generation failure as a rigor-gate
            # failure, so this carries its own NO_CANDIDATES code.
            await emit.emit(
                "error",
                message="no candidates generated",
                recoverable=True,
                code="NO_CANDIDATES",
            )
            meter.set_meta("outcome", "no_candidates")
            await store.update_status(job_id, "error", error="no candidates generated")
            return

        await _abort_if_cancel_requested(store, job_id, "rigor_gate")

        # Patch PBO across the candidate set (library-level metric — Bailey
        # et al. CSCV needs N≥2 to be meaningful; the helper handles N<2
        # gracefully by setting PBO=0.0). After this, every candidate's
        # rigor_verdict has all four fields (DSR, PBO, OOS Sharpe, lookahead).
        with meter.stage("rigor_gate"):
            _patch_pbo(candidates)
            # Re-deflate DSR using the REAL candidate-pool correlation (#822, approach B
            # for #770/#811). Runs after PBO so it can re-derive `passing`'s PBO leg from
            # the already-patched value. Falls back to the untouched ρ̄=0 verdicts (approach
            # A) whenever fewer than two buy-and-hold return series are available.
            _patch_dsr_with_pool_correlation(candidates)
            for c in candidates:
                c.passes_rigor = c.rigor_verdict.get("passing", False)

        # Selection: prefer rigor-passing candidates; fall back to the full set only to
        # still surface a "considered best" for the alternatives panel. Whether that best
        # is DEPLOYABLE is a separate, honest signal (#818): a candidate that failed the
        # gate is persisted with passes_rigor_gate=False and the server-side vault gate
        # refuses to deploy it — we must not imply it is validated.
        validated = [c for c in candidates if c.passes_rigor]
        # The rejected path is the common case and costs the same compute — record
        # which one this run was, so a stored snapshot can be read as pass or fail
        # rather than assumed to be the happy path (#1217 anti-goal 2).
        meter.set_meta("candidates_considered", len(candidates))
        meter.set_meta("candidates_passing_rigor", len(validated))
        pool = validated or candidates
        if pipeline_name == "debate":
            # The society's deterministic synthesizer already ranked the board
            # (composite of rigor + null-margin + regime fit) — re-ranking by
            # raw DSR here would silently override the debate outcome. Take the
            # first pool member in board order.
            best = next(c for c in candidates if c in pool)
        else:
            best = max(
                pool,
                key=lambda c: c.rigor_verdict.get("dsr") or 0.0,
            )
        # User-chosen name (brief.name) applies to the WINNER only, and must land
        # BEFORE the persist loop below so every downstream surface (library
        # record, passport, episodic memory, job result) reads it. Considered-
        # rejects keep their auto-derived names — renaming them too would produce
        # multiple identically-named strategies per generation.
        best.strategy_name = _resolve_name(brief, best.strategy_name)
        await emit.emit(
            "best_selected",
            best_candidate_id=best.candidate_id,
            considered_count=len(candidates),
            validated_count=len(validated),
            # deployable=False ⇒ the surfaced best is a considered alternative (ABSTAIN),
            # not a validated, deployable winner. This requires BOTH a rigor pass AND
            # real-data grading — a synthetic-data-graded candidate can pass the gate but
            # is not admissible, so it must not be advertised deployable (issue #937).
            deployable=_is_deployable(best),
        )

        # K=1 persistence (Phase-3): only the WINNER becomes a library
        # strategy (+passport +trace). Considered alternatives are recorded in
        # the episodic strategy_proposals store (content-hashed, with their
        # rigor verdicts) and surfaced via the job's candidates payload — they
        # must NOT accumulate as rejected StrategyRecords (the orphan pattern
        # the ownership purge removed).
        # (`strategy_ids` is declared before the try — the `finally` reads it.)
        ownership = {"owner_wallet": owner_wallet}
        if owner_user_id is not None:
            ownership["owner_user_id"] = owner_user_id
        with meter.stage("persist_winner"):
            sid, thash = await _persist_candidate(best, brief, **ownership)
        # K=1: one strategy_store row. The strategy_passports mirror counts
        # itself inside _persist_candidate's success branch, because its
        # persist is deliberately non-blocking and can fail without raising.
        meter.record_write("strategy_store")
        strategy_ids[best.candidate_id] = sid
        await emit.emit(
            "trace_hashed",
            trace_hash=thash,
            candidate_id=best.candidate_id,
            regime=best.regime,
        )
        await emit.emit(
            "persisted",
            strategy_id=sid,
            candidate_id=best.candidate_id,
            regime=best.regime,
            redirect_url=f"/app/library?highlight={sid}",
        )
        strategy_id = strategy_ids.get(best.candidate_id, "")

        # ── Run real multi-year backtests on every persisted candidate ──
        # Closes the "Pending Backtest" gap: until this lands, generated
        # strategies sat in the Library with no `real_sharpe` row, so the
        # passport rendered the placeholder card forever. We backtest both
        # regime variants in parallel (yfinance + numpy is I/O-bound), then
        # upsert the BacktestResult row + updated passport metrics so the
        # next /api/strategies/ read surfaces empirical Sharpe/DSR/PBO/OOS.
        # Fusion candidates NEVER carry a static weight vector — they emit a DSL
        # strategy_spec (weights={}), evaluated by the fusion evaluator, not the
        # buy-and-hold portfolio_backtester. So skip ALL fusion candidates here,
        # keyed on generation_method — not just the has_real_rigor ones. A
        # *text-only* fusion candidate (model emitted no machine-readable spec, so
        # has_real_rigor stayed False) ALSO has weights={}; routing it into the
        # static backtester only emits a misleading
        # backtest_failed("no weights emitted by agent") — the live conversion bug
        # in #784 (every Generate result looked broken). Keying on
        # generation_method preserves the HONEST "no weights" signal for a genuine
        # agent-path failure (agent emitted no allocation) while never running the
        # static backtester on a fusion candidate that has nothing static to run.
        # Debate candidates ("debate"/"debate_abstain") are the same shape — they
        # emit a DSL spec (weights={}) scored by evaluate_fusion_spec, or are a
        # populated ABSTAIN — so they skip the static backtester too (T1.1).
        _static_skip = ("fusion", "debate", "debate_abstain")
        await _abort_if_cancel_requested(store, job_id, "backtest_persist")
        with meter.stage("backtest_persist"):
            await asyncio.gather(
                *[
                    # num_trials = the strategy's OWN candidate pool (N), never the library
                    # size — a strategy's rigor depends only on itself (decouple #2).
                    _backtest_and_persist(c, strategy_ids[c.candidate_id], emit, _society_num_trials(n_candidates))
                    for c in candidates
                    if c.generation_method not in _static_skip and c.candidate_id in strategy_ids
                ]
            )

            # Fusion (and debate) candidates skipped the static backtester above but carry
            # a REAL DSL backtest — persist its returns so live_rigor_gate reads pass/fail
            # (not "pending") and the strategy is deployable (#788/#818). Without this, a
            # fusion winner with a genuine backtest forever reads rigor_gate_status=pending
            # and the server-side create_vault gate (#829) refuses to deploy it.
            await asyncio.gather(
                *[
                    _persist_real_returns(c, strategy_ids[c.candidate_id], emit, _society_num_trials(n_candidates))
                    for c in candidates
                    if c.has_real_rigor and c.return_series and c.candidate_id in strategy_ids
                ]
            )

        # ── Persist all candidates to episodic memory (T-PE.8) ──
        try:
            from archimedes.services.strategy_memory import persist_proposal

            for cand in candidates:
                persist_proposal(
                    generation_id=job_id,
                    # "debate" for society candidates, "fusion" for fused, "agent"
                    # otherwise — the episodic record reflects which engine produced
                    # the proposal rather than always claiming "agent".
                    agent=(
                        "debate"
                        if cand.generation_method in ("debate", "debate_abstain")
                        else "fusion"
                        if cand.generation_method == "fusion"
                        else "agent"
                    ),
                    intent=brief.intent,
                    strategy_spec={
                        "strategy_name": cand.strategy_name,
                        "thesis": cand.thesis,
                        "weights": cand.weights,
                        "asset_universe": cand.asset_universe,
                    },
                    papers=[aid for p in cand.source_papers if (aid := p.get("arxiv_id", ""))]
                    or list(cand.source_arxiv_ids),
                    rigor_verdict=cand.rigor_verdict,
                    # K=1 (Phase-3): this loop is the SINGLE episodic writer for the
                    # whole board — winner marked selected, alternates carry the
                    # honest reject reason. (A second per-alternate writer here
                    # produced 2N-1 rows per run; verify finding, removed.)
                    verdict="selected" if cand is best else "rejected",
                    regime_tag=cand.regime,
                    extra={
                        "candidate_id": cand.candidate_id,
                        "selected": cand is best,
                        "strategy_name": cand.strategy_name,
                        "reject_reason": None
                        if cand is best
                        else (cand.rigor_verdict or {}).get("reason") or "outranked by the society leader",
                    },
                    owner_wallet=owner_wallet,
                    owner_user_id=owner_user_id,
                )
                meter.record_write("strategy_proposals")
        except Exception:
            pass  # Non-blocking per spec

        # Debate transcript capture: persist each candidate's bull/bear debate
        # transcript (winner + final-round losers) — see
        # _persist_debate_transcripts's docstring for why one pass over
        # `candidates` covers both. No-op on every non-debate path.
        await _persist_debate_transcripts(job_id=job_id, candidates=candidates, strategy_ids=strategy_ids)

        # Stash the full candidate list on the job for /candidates retrieval.
        # update_terminal_status (not update_status): a Cancel request can flip
        # Redis's status to "cancelled" while this coroutine is still mid-flight
        # (asyncio.Task.cancel() cannot interrupt a to_thread LLM call already
        # running — the awaiter only unblocks once that thread finishes on its
        # own), so this write must not clobber a cancellation that already
        # landed (#1355).
        await store.update_terminal_status(
            job_id,
            "done",
            result={
                "best_candidate_id": best.candidate_id,
                "best_strategy_id": strategy_id,
                "candidates": [
                    {
                        "candidate_id": c.candidate_id,
                        "strategy_id": strategy_ids.get(c.candidate_id),
                        "strategy_name": c.strategy_name,
                        "rigor_verdict": c.rigor_verdict,
                        "passes_rigor": c.passes_rigor,
                        "selected": c is best,
                        "regime": c.regime,
                        "generation_method": c.generation_method,
                    }
                    for c in candidates
                ],
            },
        )
        # Provenance: surface the model that actually served this job so the UI
        # can show what really ran (vs. what was requested). `served_model` is
        # the post-call value when available (e.g. response.model), else the
        # configured id; falls back to the fixture marker on the non-live path.
        served_model = _served_model_for(job_agent, use_live)
        meter.set_meta("served_model", served_model)
        meter.set_meta("pipeline", pipeline_name)
        meter.set_meta("outcome", "done")
        await emit.emit(
            "done",
            strategy_id=strategy_id,
            all_strategy_ids=strategy_ids,
            served_model=served_model,
        )

        # Legacy wallet identity ledger records only runs with linked-wallet
        # provenance. Account-only runs remain owned by owner_user_id but have no
        # wallet event to emit. Fail-safe; never affects job.
        emit_identity_event(
            wallet=owner_wallet,
            event_type="generation_completed",
            actor_class="human",
            meta={
                "job_id": job_id,
                "best_candidate_id": best.candidate_id,
                "best_strategy_id": strategy_id,
                "served_model": served_model,
            },
        )

    except asyncio.CancelledError:
        meter.set_meta("outcome", "cancelled")
        await emit.emit("error", message="job cancelled", recoverable=False, code="CANCELLED")
        await store.update_status(job_id, "cancelled", error="cancelled by client")
        raise
    except Exception as exc:
        meter.set_meta("outcome", "pipeline_crash")
        logger.exception("generation pipeline crashed: %s", exc)
        await emit.emit("error", message=str(exc), recoverable=False, code="PIPELINE_CRASH")
        await store.update_status(job_id, "error", error=str(exc))
    finally:
        # Persist the measurement on EVERY terminal path — done, rigor-failed,
        # errored, cancelled. Merged into the job's result rather than written
        # with it, because most of those paths never write a result at all.
        # Instrumentation is not allowed to change the outcome of a generation,
        # so a failure here is swallowed (CancelledError included: this runs
        # while a cancellation is already propagating).
        cost_meter.unbind(meter_token)
        # Clears the buffered prompts + completions with it (#1800): nothing is
        # persisted yet, so holding them past the job would be a retention
        # surface nobody asked for. The S3 flush the next PR adds goes ABOVE
        # this line.
        llm_trace.unbind(trace_token)
        snapshot: dict[str, Any] | None = None
        with contextlib.suppress(Exception, asyncio.CancelledError):
            snapshot = meter.snapshot()
            await store.merge_result(job_id, {"cost": snapshot})
        # …and durably, keyed to the strategy the run produced (#1326). The job
        # record above expires with JOB_TTL; this one is what the passport card
        # and the library column read an hour, a week or a year later. Same
        # snapshot object as the job record gets, so the two can never disagree.
        if snapshot is not None:
            await _persist_generation_cost(
                job_id=job_id,
                strategy_ids=strategy_ids,
                measurement=snapshot,
                quote=quote_in_force,
            )


async def _persist_candidate(
    c: _CandidateResult,
    brief: GenerateBrief,
    *,
    owner_wallet: str | None = None,
    owner_user_id: str | None = None,
) -> tuple[str, str]:
    """Upsert the candidate as a Strategy + return (strategy_id, trace_hash).

    Trace hash is the keccak of the canonical (brief, candidate) tuple — gives
    every generation a deterministic identifier mirrored on-chain in v1.5.

    ``owner_wallet`` (optional linked-wallet provenance) is stamped on
    both the strategy_store row and its strategy_passports mirror.
    """
    from web3 import Web3

    from archimedes.db import get_session
    from archimedes.models.strategy_store import upsert_strategy

    canonical = json.dumps(
        {
            "brief": brief.model_dump(),
            "candidate_id": c.candidate_id,
            "strategy_name": c.strategy_name,
            "weights": c.weights,
            "rigor_verdict": c.rigor_verdict,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    trace_hash = Web3.keccak(text=canonical).hex()

    def _do_persist() -> str:
        with get_session() as session:
            record = upsert_strategy(
                session,
                # Provenance of record — "fusion" for multi-paper synthesis,
                # "portfolio_agent_streaming" for the single-agent path. Was
                # hardcoded to the agent method; now reads the candidate's own
                # method so fusion candidates land in the library as fusion.
                generation_method=c.generation_method,
                strategy_name=c.strategy_name,
                thesis=c.thesis,
                source_papers=c.source_papers,
                asset_universe=c.asset_universe,
                risk_profile=brief.risk_appetite,
                rigor_verdict=c.rigor_verdict,
                provenance_hash=trace_hash,
                is_example=False,
                owner_wallet=owner_wallet,
                owner_user_id=owner_user_id,
                # Rebalancer decouple (Part A #1): persist the candidate's own
                # validated DSL spec so a vault later deployed from this
                # strategy can be autonomously rebalanced by the agent runner.
                # None on the fixture/buy-and-hold path — nothing to persist.
                strategy_spec=c.strategy_spec,
                # The user's own free-text ask (v8 Lane 3.3), surfaced on the
                # passport as "Your brief" — already in hand here, no
                # strategy_proposals lookup needed for a strategy persisted
                # from this call.
                brief_intent=brief.intent,
            )
            # Stamp the generating wallet so wallet_can_publish() returns True
            # for this wallet/strategy pair (D5 publish gate).
            if owner_wallet:
                from archimedes.models.strategy_generators import record_generator

                record_generator(session, strategy_id=record.id, wallet_address=owner_wallet)
            session.commit()

            # Also write to the unified strategy_passports table (Issue #160)
            try:
                from archimedes.models.strategy import StrategyPassport, StrategyStatus
                from archimedes.services.passport_loader import ingest_passport

                papers = _passport_paper_refs(c)
                # Map candidate regime to passport regime_tag
                _regime_tag_map = {"bull": "bull", "bear": "bear"}
                _regime_tag = _regime_tag_map.get(c.regime, "regime_neutral")
                passport = StrategyPassport(
                    id=record.id,
                    papers=papers,
                    methodology_summary=c.thesis or "",
                    # asset_universe / rebalance_frequency / position_sizing —
                    # the three executable card fields, from the validated spec
                    # (#1769). See `_passport_spec_fields`.
                    **_passport_spec_fields(c),
                    universe_source=c.universe_source,
                    status=StrategyStatus(record.status) if record.status else StrategyStatus.CANDIDATE,
                    regime_tag=_regime_tag,
                    # NO RIGOR VERDICT, AND NO RIGOR NUMBERS, FROM HERE.
                    #
                    # This call used to write the generation-time FUSION verdict
                    # onto the passport — `passes_rigor_gate` from
                    # c.rigor_verdict["passing"], plus its dsr / dsr_p_value /
                    # pbo / oos_sharpe. That made the passport's verdict column
                    # mixed-vintage: it held the fusion gate's answer until (and
                    # only if) the post-backtest re-grade below happened to run,
                    # and every read surface presented it as the strategy's
                    # grade. #1747 is what that looks like from the outside.
                    #
                    # Generation, backtesting and grading are one-time events
                    # (docs/adr/rigor-verdict-of-record.md). At THIS point the
                    # strategy has been generated and not yet graded, so the row
                    # is written ungraded — ingest_passport with no
                    # ``rigor_verdict=`` stores rigor_gate_status="pending",
                    # passes_rigor_gate=False, no graded_at, no gate_version.
                    #
                    # The fusion verdict is not lost and is not demoted: it stays
                    # on StrategyRecord.rigor_verdict (written by upsert_strategy
                    # above) as the DEBATE RECORD — what the synthesis gate
                    # thought, which is worth keeping precisely because the real
                    # gate can disagree with it. It is simply not a rigor grade.
                )
                with get_session() as sess2:
                    ingest_passport(
                        sess2,
                        passport,
                        generation_method=c.generation_method,
                        force_update=True,
                        owner_wallet=owner_wallet,
                        owner_user_id=owner_user_id,
                    )
                    sess2.commit()
                # Counted HERE, inside the success branch: the passport mirror
                # is a swallowed-failure write, and the outer caller cannot see
                # whether it landed — counting there overcounts on the logged
                # non-blocking failure path (#1314 review).
                cost_meter.record_write("strategy_passports")
            except Exception as exc:
                logger.warning("unified passport persist failed (non-blocking): %s", exc)

            return record.id

    strategy_id = await asyncio.to_thread(_do_persist)
    return strategy_id, trace_hash


def _look_ahead_audit_source(rigor_verdict: dict) -> str:
    """Provenance label for a DSL row's ``look_ahead_audit_passed`` boolean.

    ``BacktestResult.look_ahead_audit_source`` exists precisely so a reader can
    tell a genuine audit pass from a constant. This path used to hardcode
    ``"self_attested"`` — correct at the time, because the boolean really was the
    LLM's own ``look_ahead_safe`` declaration. That field no longer exists, so
    the label is derived from the four-state ``look_ahead_status`` verdict that
    ``dsl_lookahead_audit`` computes, on the axis a provenance column is actually
    about — did an audit reach a verdict, or not:

      * ``"dsl_structural_audit"`` — the audit CONCLUDED (``pass`` or ``fail``):
        the spec was checked against a DSL surface whose interpreter provably
        reads only bar ``t`` and earlier, and the broker cheat-on-close/open
        check ran. Note this is written for a ``fail`` too: the audit is still
        the provenance of that ``False``.
      * ``"dsl_audit_not_run"``    — the audit reached NO verdict (``pending`` /
        ``degenerate``), or the blob predates this field entirely. The boolean
        beside this label is False because nothing was proven, not because a
        leak was found.

    ``"self_attested"`` is never written again by any path.
    """
    from archimedes.services.dsl_lookahead_audit import (
        CONCLUSIVE_STATUSES,
        SOURCE_DSL_AUDIT,
        SOURCE_DSL_AUDIT_NOT_RUN,
    )

    if rigor_verdict.get("look_ahead_status") in CONCLUSIVE_STATUSES:
        return SOURCE_DSL_AUDIT
    return SOURCE_DSL_AUDIT_NOT_RUN


def _portfolio_daily_returns(artifact: dict) -> list[float]:
    """Pull the daily return series out of a ``backtest_portfolio`` artifact.

    The live gate grades a return series, so the series has to come back out of
    the artifact the simulator just built. Returns an empty list when the shape
    is not what we expect, which the gate reads as ``pending`` rather than as a
    pass — the same fail-closed direction ``verdict_from_returns`` already takes
    for a short series.
    """
    results = artifact.get("results") or []
    if not results:
        return []
    metrics = results[0].get("metrics") or {}
    returns = metrics.get("daily_returns") or []
    return [float(r) for r in returns]


def _refresh_passport_real_metrics(
    session: Any, c: _CandidateResult, strategy_id: str, result: Any, *, verdict: Any, n_obs: int
) -> None:
    """Refresh the strategy_passports ``real_*`` columns AND write the rigor verdict
    of record for a fusion/debate candidate whose real returns were just persisted.

    **This call is the grading event.** ``verdict`` is the
    :class:`~archimedes.services.live_rigor_gate.RigorGateVerdict` that
    ``verdict_from_returns`` produced by running the real gate over the real
    persisted return series — the one moment in a strategy's life when a gate
    actually looks at it. Every read surface serves what this writes; nothing
    recomputes a verdict on read (docs/adr/rigor-verdict-of-record.md).

    The four-state ``verdict.status`` is stored as-is, so ``degenerate`` (a
    zero-variance persisted series, #1184) survives to the badge as itself
    rather than being re-derived — or, worse, collapsing into ``fail``.
    ``cohort_n=1`` because this grade is self-contained: the strategy was graded
    against its own returns alone, not against a cohort
    (docs/adr/num-trials-self-containment.md).

    Mirrors the passport refresh in ``_backtest_and_persist``.
    """
    from datetime import date as _date

    from archimedes.models.strategy import StrategyPassport, StrategyStatus
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.services.passport_loader import RigorVerdictWrite, ingest_passport

    record = session.query(StrategyRecord).filter_by(id=strategy_id).first()
    status_val = StrategyStatus(record.status) if record and record.status else StrategyStatus.CANDIDATE
    papers = _passport_paper_refs(c)
    regime_tag = {"bull": "bull", "bear": "bear"}.get(c.regime, "regime_neutral")
    passport = StrategyPassport(
        id=strategy_id,
        papers=papers,
        methodology_summary=c.thesis or "",
        # Same three spec-derived card fields the initial persist writes
        # (#1769) — this rebuild replaces the row in place, so omitting them
        # here would revert the card to the ``weekly``/``equal_weight`` column
        # defaults the moment real metrics land.
        **_passport_spec_fields(c),
        universe_source=c.universe_source,
        status=status_val,
        regime_tag=regime_tag,
        real_sharpe=result.sharpe_ratio,
        real_sortino=result.sortino_ratio,
        real_cagr=result.cagr,
        real_max_dd=result.max_drawdown,
        real_calmar=result.calmar_ratio,
        real_corr_spy=result.correlation_to_spy,
        # The run measured a win rate; it was the one metric of the block this
        # refresh never handed on, so the column stayed NULL beside a fresh
        # Sharpe from the same run.
        real_win_rate=result.win_rate,
        real_total_trades=result.total_trades,
        real_backtest_start=(result.backtest_start.isoformat() if isinstance(result.backtest_start, _date) else None),
        real_backtest_end=(result.backtest_end.isoformat() if isinstance(result.backtest_end, _date) else None),
        num_trials_in_selection=result.num_trials_in_selection,
        n_obs_daily=n_obs,
    )
    # The four gate numbers ride WITH the verdict (#1746 / PR-B), not on the
    # passport dataclass: they are the grade's output, and `_update_record` no
    # longer writes them. Same values, same run, one indivisible write.
    ingest_passport(
        session,
        passport,
        generation_method=c.generation_method,
        force_update=True,
        rigor_verdict=RigorVerdictWrite.from_verdict(
            verdict,
            cohort_n=1,
            deflated_sharpe_ratio=result.deflated_sharpe_ratio,
            dsr_p_value=result.dsr_p_value,
            pbo_score=result.pbo_score,
            out_of_sample_sharpe=result.out_of_sample_sharpe,
        ),
    )


async def _persist_real_returns(c: _CandidateResult, strategy_id: str, emit: _Emitter, num_trials: int) -> None:
    """Persist a has_real_rigor candidate's real DSL-backtest returns so the live
    rigor gate reads ``pass``/``fail`` — not ``pending`` — and the strategy becomes
    deployable (#788/#818).

    Fusion/debate candidates emit a DSL spec (``weights={}``) scored by
    ``evaluate_fusion_spec``, so they skip the static buy-and-hold backtester (#829)
    and would otherwise leave NO ``backtest_results`` row → ``live_rigor_gate`` reads
    ``pending`` → the strategy is never deployable even though it carries a real
    backtest. This writes the row from the captured return series so the live gate
    has real returns to re-grade, and refreshes the passport ``real_*`` columns +
    ``passes_rigor_gate`` (which is what the single-strategy read path + the deploy
    gate actually consume). Non-blocking: any failure is logged + emitted, not raised.

    **Claim-integrity gate:** persists ONLY when the backtest ran on REAL data
    (``data_source != "synthetic"``). ``evaluate_fusion_spec`` defaults to a random
    synthetic series when no ``data_feed`` is wired, and grading a random walk as a
    real pass/fail would be a #1-rule violation — so a synthetic run stays honestly
    ``pending`` (never persisted, never deployable). Real-data wiring is tracked
    separately; until then fusion candidates correctly read ``pending``.
    """
    rv = c.rigor_verdict or {}
    if not c.has_real_rigor or not c.return_series or len(c.return_series) < 10:
        return
    if str(rv.get("data_source") or "synthetic") == "synthetic":
        # Synthetic backtest — grading it as real would be a claims-integrity violation.
        logger.info("persist_real_returns: %s ran on synthetic data — staying honestly 'pending'", strategy_id)
        return

    def _do() -> None:
        import json as _json

        from archimedes.db import get_session
        from archimedes.models.backtest import BacktestResult
        from archimedes.services.backtest_repository import (
            SOURCE_PIPELINE_DSL_FUSION,
            insert_backtest_if_missing,
        )
        from archimedes.services.dsl_lookahead_audit import not_run_reason_from_verdict
        from archimedes.services.fusion_evaluator import DEFAULT_COST_MODEL_ID, ENGINE_SINGLE_FEED
        from archimedes.services.live_rigor_gate import verdict_from_returns

        returns = list(c.return_series)
        # Rebuild an equity curve (base 1.0) so BOTH the artifact daily_returns AND the
        # equity-curve fallback in get_daily_returns resolve the same series.
        equity = [1.0]
        for ret in returns:
            equity.append(equity[-1] * (1.0 + ret))

        def _f(key: str) -> float:
            v = rv.get(key)
            return float(v) if v is not None else 0.0

        def _of(key: str) -> float | None:
            v = rv.get(key)
            return float(v) if v is not None else None

        result = BacktestResult(
            strategy_id=strategy_id,
            sharpe_ratio=_f("sharpe_ratio"),
            sortino_ratio=_f("sortino_ratio"),
            max_drawdown=_f("max_drawdown"),
            cagr=_f("cagr"),
            calmar_ratio=_f("calmar_ratio"),
            win_rate=_f("win_rate"),
            profit_factor=0.0,
            total_trades=int(rv.get("total_trades") or 0),
            avg_holding_period_days=0.0,
            # None, not a fabricated 0.0 — SPY/BTC correlation is not computed on
            # this path; 0.0 would assert "uncorrelated", a claim nothing measured
            # (#1242 review).
            correlation_to_spy=None,
            correlation_to_btc=None,
            equity_curve=equity,
            deflated_sharpe_ratio=_of("dsr"),
            dsr_p_value=_of("dsr_p_value"),
            num_trials_in_selection=int(num_trials),
            pbo_score=_of("pbo"),
            out_of_sample_sharpe=_of("oos_sharpe"),
            look_ahead_audit_passed=bool(rv.get("lookahead_audit_passed", False)),
            # A8: the runner's own label, not a hardcoded "dsl-fusion". The
            # sleeve runner reports "dsl-fusion-sleeves" so a row graded as N
            # independent equal-weighted single-asset backtests is
            # distinguishable from a genuine single-feed run. Falls back to the
            # single-feed label for verdict blobs written before A8.
            backtest_engine=(rv.get("backtest_engine") or ENGINE_SINGLE_FEED),
            # Fixed cost basis every fusion/DSL backtest is charged — tx_cost_bps
            # and slippage_bps are never overridden by a caller on this path today
            # (see fusion_evaluator.DEFAULT_COST_MODEL_ID). This is the
            # closed-DSL path, with no inspectable source for an AST audit
            # (#1242 review: cost_model_id used to stop at the artifact dict on
            # this path and never reach the persisted row).
            cost_model_id=DEFAULT_COST_MODEL_ID,
            # Provenance of look_ahead_audit_passed above, derived from the
            # four-state audit rather than hardcoded. It used to be a flat
            # "self_attested" because the boolean genuinely WAS the LLM's own
            # look_ahead_safe flag; that field no longer exists, so nothing is
            # attested and that value is never written again.
            look_ahead_audit_source=_look_ahead_audit_source(rv),
        )
        artifact = {
            "results": [{"metrics": {"daily_returns": returns}}],
            "source": c.generation_method,
            "data_source": rv.get("data_source"),
        }
        artifact_json = _json.dumps(artifact, default=str)
        content_hash = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
        # The live gate is the single source of truth: re-grade the REAL returns so the
        # persisted passes_rigor_gate matches what verdicts_for_strategies computes.
        # look_ahead_audit_passed threads the REAL structural audit computed above
        # (result.look_ahead_audit_passed, from rv["lookahead_audit_passed"], which
        # dsl_lookahead_audit derives — there IS no LLM look_ahead_safe boolean
        # any more) through to the gate. Without this, strategy_code=None (this
        # path has no inspectable source for the AST audit) left the gate's
        # look_ahead_passed unconditionally False — an always-on floor failure
        # that blocked deploy at EVERY strictness level regardless of DSR/PBO/OOS,
        # no matter how strong the strategy. A spec that cannot clear the
        # structural audit now arrives here as False and correctly fails that
        # floor.
        #
        # This boolean is the SAME term fusion_evaluator.apply_rigor_gate folded
        # into `passing` (both are `DslLookAheadAudit.passed`), which is what keeps
        # the two gates from disagreeing about one strategy — the badge gate here
        # and the fusion verdict cannot land on opposite sides of the look-ahead
        # leg. Pinned by test_dsl_lookahead_audit.TestTheTwoGatesAgree.
        #
        # Fail-closed on admission, honest on the surface: when the boolean above
        # is False because the audit reached NO verdict (rather than because it
        # caught a leak), the status and reason say so. The gate outcome is
        # identical either way — the always-on floor still blocks — but the user
        # is not told their strategy failed an audit that never ran.
        live = verdict_from_returns(
            strategy_id,
            returns,
            num_trials=int(num_trials),
            look_ahead_audit_passed=result.look_ahead_audit_passed,
            look_ahead_status=rv.get("look_ahead_status"),
            look_ahead_not_run_reason=not_run_reason_from_verdict(rv),
        )

        with get_session() as session:
            insert_backtest_if_missing(
                session,
                strategy_id=strategy_id,
                content_hash=content_hash,
                result=result,
                operation="DSL_FUSION",
                artifact_json=artifact_json,
                source_pipeline=SOURCE_PIPELINE_DSL_FUSION,
            )
            _refresh_passport_real_metrics(session, c, strategy_id, result, verdict=live, n_obs=len(returns))
            # WITHOUT this commit the flushed rows roll back on close → gate stays "pending"
            # (the sibling _backtest_and_persist commits too). Empirically: flush-only = 0 rows.
            session.commit()

    try:
        await asyncio.to_thread(_do)
        cost_meter.record_write("backtest_results")
        cost_meter.record_write("strategy_passports")
        await emit.emit(
            "backtest_done", candidate_id=c.candidate_id, strategy_id=strategy_id, source=c.generation_method
        )
    except Exception as exc:
        # LOUD, not a warning. This is the swallow that decides whether a
        # strategy ever gets graded at all: if it fires, no RigorVerdictWrite
        # reaches the passport and the row stays honestly "pending" forever —
        # invisible in prod unless someone goes looking for a strategy that
        # never got a badge. error + exc_info so the traceback and the strategy
        # id are both in the log line that matters
        # (docs/adr/rigor-verdict-of-record.md).
        logger.error(
            "persist_real_returns FAILED for strategy %s — the passport stays UNGRADED (rigor_gate_status='pending'): %s",
            strategy_id,
            exc,
            exc_info=True,
        )
        await emit.emit("backtest_failed", candidate_id=c.candidate_id, strategy_id=strategy_id, error=str(exc))


async def _backtest_and_persist(c: _CandidateResult, strategy_id: str, emit: _Emitter, num_trials: int = 1) -> None:
    """Backtest the generated strategy on real multi-year data and persist results.

    Closes the "Pending Backtest" gap on the Library page. The agent only
    emits ``{ticker: weight}`` + a rebalance period — no ``bt.Strategy``
    subclass — so the analytics-engine's single-asset Cerebro path doesn't
    fit. This function instead runs the pandas/numpy
    :func:`portfolio_backtester.backtest_portfolio` over real yfinance prices,
    persists a full :class:`BacktestResult` to ``backtest_results``, and
    refreshes the passport row with empirical metrics so
    ``is_backtest_placeholder`` flips false on the next API read.

    Failures are non-fatal: if yfinance is unreachable, a ticker doesn't
    resolve, or the historical overlap is too short, the placeholder remains
    and a ``backtest_failed`` SSE event surfaces the reason. The generation
    itself does not fail.

    Args:
        c: The candidate that was just persisted.
        strategy_id: The DB id returned by :func:`_persist_candidate`.
        emit: The SSE emitter to surface backtest progress to the UI.
        num_trials: Effective trial count for the DSR multiple-testing correction —
            the candidate's OWN N-candidate selection pool on the society path
            (``_society_num_trials(N)``, decouple #2 — never the curated-library
            size), fed to ``backtest_portfolio`` as ``num_trials_for_dsr``.
    """
    # Fixture mode (offline tests, no-LLM environments) — skip the network
    # round-trip. The test suite covers this function's behavior via direct
    # unit tests in test_portfolio_backtester.py and via the pipeline's
    # generation-event tests, neither of which need a live yfinance call.
    if os.getenv("GENERATION_PIPELINE_FIXTURE", "").lower() in ("1", "true") or os.getenv(
        "GENERATION_PIPELINE_SKIP_BACKTEST", ""
    ).lower() in ("1", "true"):
        logger.debug("skipping live backtest for %s (fixture mode)", strategy_id)
        return

    await emit.emit(
        "backtest_running",
        strategy_id=strategy_id,
        candidate_id=c.candidate_id,
        symbols=list((c.weights or {}).keys()),
    )

    if not c.weights:
        await emit.emit(
            "backtest_failed",
            strategy_id=strategy_id,
            candidate_id=c.candidate_id,
            error="no weights emitted by agent",
        )
        return

    def _do_backtest_and_persist() -> dict[str, Any] | None:
        # All heavy work — yfinance fetch, numpy compute, DB writes — runs
        # off the event loop. Returns the metrics dict for the SSE emit.
        import json as _json
        from datetime import date as _date

        from archimedes.db import get_session
        from archimedes.models.strategy import StrategyPassport, StrategyStatus
        from archimedes.models.strategy_store import StrategyRecord
        from archimedes.services.backtest_mapper import canonical_artifact_hash
        from archimedes.services.backtest_repository import (
            SOURCE_PIPELINE_PORTFOLIO_BACKTESTER,
            insert_backtest_if_missing,
        )
        from archimedes.services.live_rigor_gate import verdict_from_returns
        from archimedes.services.passport_loader import RigorVerdictWrite, ingest_passport
        from archimedes.services.portfolio_backtester import backtest_portfolio

        # Run the actual backtest. Raises on insufficient data / fetch failure.
        # num_trials_for_dsr = the candidate's OWN N-candidate society-search pool
        # (``_society_num_trials(N)``, decouple #2) — the DSR multiple-testing
        # correction for the selection set this winner survived, never the
        # curated library it happens to join.
        result, artifact = backtest_portfolio(
            strategy_id=strategy_id,
            weights=c.weights,
            paper_title=c.strategy_name,
            num_trials_for_dsr=max(1, num_trials),
        )
        artifact_json = _json.dumps(artifact, default=str)
        # Issue #1347 follow-up: this used to be an ad hoc
        # hashlib.sha256(artifact_json) — a hash `artifact` (built by
        # portfolio_backtester.backtest_portfolio, which stamps its own
        # "run_id"/"timestamp_utc" volatile keys, same shape as cli.py's
        # artifact) can never reproduce, because canonical_artifact_hash
        # excludes those keys and this ad hoc call didn't. That gap meant the
        # dedupe migration could normalize this writer's HISTORICAL rows to
        # the canonical hash while every NEW row from this exact call site
        # kept minting a fresh, non-matching hash — restarting duplicate
        # accumulation for this one pipeline the moment it ran again. Route
        # through the same canonical_artifact_hash run_backtests.py and
        # seed_backtests_from_artifacts.py already use, so all three writer
        # call sites that mint run_id/timestamp_utc-bearing artifacts compute
        # hashes the same, reproducible way.
        content_hash = canonical_artifact_hash(artifact)

        # Re-grade the REAL returns through the live gate, exactly as the DSL
        # sibling above does and as strategies_routes._to_strategy_response does
        # for curated strategies. This is what actually keeps generated and
        # curated on one scale.
        #
        # It previously read BacktestResult.passes_rigor_gate, a second gate
        # carrying its own thresholds (a raw-Sharpe floor, its own DSR bar,
        # pbo<0.5, oos/is>=0.5, max_dd<0.5) while the curated read path used
        # verdict.passes from live_rigor_gate. The comment here claimed the two
        # matched. They did not, and the mismatch ran in the fail-closed
        # direction for every generated portfolio strategy ever produced:
        # backtest_portfolio sets pbo_score=None (PBO is a library-level metric
        # a later scheduler refreshes), and that property short-circuits to
        # False whenever pbo_score is None. So this line was a constant False,
        # not a grade.
        daily_returns = _portfolio_daily_returns(artifact)
        live = verdict_from_returns(
            strategy_id,
            daily_returns,
            num_trials=max(1, num_trials),
            look_ahead_audit_passed=result.look_ahead_audit_passed,
        )
        passes = live.passes

        with get_session() as session:
            # 1. Persist the backtest_results row.
            insert_backtest_if_missing(
                session,
                strategy_id=strategy_id,
                content_hash=content_hash,
                result=result,
                run_id=artifact.get("run_id"),
                operation="PORTFOLIO",
                artifact_json=artifact_json,
                source_pipeline=SOURCE_PIPELINE_PORTFOLIO_BACKTESTER,
            )

            # 2. Refresh the strategy_passports row with real_* metrics.
            #    We rebuild the passport using the StrategyRecord we just
            #    persisted (for status + name) plus the candidate's papers /
            #    asset universe / regime mapping — same construction as
            #    `_persist_candidate`'s passport block, now decorated with
            #    real metrics. ingest_passport(force_update=True) does an
            #    in-place update.
            record = session.query(StrategyRecord).filter_by(id=strategy_id).first()
            status_val = StrategyStatus(record.status) if record and record.status else StrategyStatus.CANDIDATE
            papers = _passport_paper_refs(c)
            _regime_tag_map = {"bull": "bull", "bear": "bear"}
            _regime_tag = _regime_tag_map.get(c.regime, "regime_neutral")
            passport = StrategyPassport(
                id=strategy_id,
                papers=papers,
                methodology_summary=c.thesis or "",
                # Same three spec-derived card fields as the other two writers
                # (#1769) — this is an in-place `force_update` rebuild.
                **_passport_spec_fields(c),
                status=status_val,
                regime_tag=_regime_tag,
                # Real backtest fields — the whole point of this function
                real_sharpe=result.sharpe_ratio,
                real_sortino=result.sortino_ratio,
                real_cagr=result.cagr,
                real_max_dd=result.max_drawdown,
                real_calmar=result.calmar_ratio,
                real_corr_spy=result.correlation_to_spy,
                real_win_rate=result.win_rate,  # same gap as the DSL sibling above
                real_total_trades=result.total_trades,
                real_backtest_start=(
                    result.backtest_start.isoformat() if isinstance(result.backtest_start, _date) else None
                ),
                real_backtest_end=(result.backtest_end.isoformat() if isinstance(result.backtest_end, _date) else None),
                num_trials_in_selection=result.num_trials_in_selection,
                n_obs_daily=len(artifact["results"][0]["metrics"].get("daily_returns", [])),
            )
            # THE grading event for this path — the same one-time write the DSL
            # sibling makes (see _refresh_passport_real_metrics). `live` is the
            # real gate's four-state answer over the real returns; storing its
            # status verbatim is what keeps `degenerate` from collapsing into
            # `fail`. cohort_n=1: graded against itself alone.
            # The four gate numbers ride WITH the verdict (#1746 / PR-B) rather
            # than on the passport dataclass — same values, same run, one write.
            ingest_passport(
                session,
                passport,
                generation_method="fusion",
                force_update=True,
                rigor_verdict=RigorVerdictWrite.from_verdict(
                    live,
                    cohort_n=1,
                    deflated_sharpe_ratio=result.deflated_sharpe_ratio,
                    dsr_p_value=result.dsr_p_value,
                    pbo_score=result.pbo_score,
                    out_of_sample_sharpe=result.out_of_sample_sharpe,
                ),
            )
            session.commit()

        return {
            "sharpe_ratio": result.sharpe_ratio,
            "cagr": result.cagr,
            "max_drawdown": result.max_drawdown,
            "dsr_p_value": result.dsr_p_value,
            "out_of_sample_sharpe": result.out_of_sample_sharpe,
            "passes_rigor_gate": passes,
            "n_bars": len(artifact["results"][0]["metrics"].get("daily_returns", [])),
        }

    try:
        metrics = await asyncio.to_thread(_do_backtest_and_persist)
        if metrics is None:
            return
        cost_meter.record_write("backtest_results")
        cost_meter.record_write("strategy_passports")
        await emit.emit(
            "backtest_done",
            strategy_id=strategy_id,
            candidate_id=c.candidate_id,
            metrics=metrics,
        )
    except Exception as exc:
        # Non-fatal — the strategy stays in the Library with the placeholder,
        # which is honest. The generation succeeded; the backtest didn't.
        # LOUD anyway: this is the other swallow that decides whether the
        # grading event happens at all, and a silently ungraded strategy looks
        # exactly like one whose backtest is merely still running
        # (docs/adr/rigor-verdict-of-record.md).
        logger.error(
            "backtest_and_persist FAILED for strategy %s — the passport stays UNGRADED (rigor_gate_status='pending'): %s",
            strategy_id,
            exc,
            exc_info=True,
        )
        await emit.emit(
            "backtest_failed",
            strategy_id=strategy_id,
            candidate_id=c.candidate_id,
            error=str(exc),
        )
