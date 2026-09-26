"""T1.1 — Multi-agent debate society (Phase 3: debate is the sole pipeline).

Design of record: ``docs/specs/multi-agent-debate-spec.md`` (v2).

The society is unconditional as of Phase-3 (T1.1 flag audit, issue #834).
The ``ARCHIMEDES_DEBATE_ENABLED`` flag is retired; ``_debate_can_run`` only
checks corpus size. The pipeline:

  1. **Proposer pool** — fans ``StrategyFusion(model=...).propose`` across
     ``select_candidates(regime_bias=R)`` evidence sets (the A3 model seam
     threads the user's Generate-page model pick). Drops non-actionable
     (``FusionProposal.is_actionable``) and non-conformant (``_dsl_conformance_ok``,
     fix A5) specs, then dedups by canonical spec hash (fix #893 — different
     steers can converge on the same spec under a different name). ``pool_size
     = len(POOL)`` counts unique specs only.
  2. **Adversarial round** — a thin, best-effort bull/bear transcript.
     Surfaces adversarial topology on the SSE stream but never gates;
     deterministic critics do the real culling (the budget trick).
  3. **C-rigor** — backtests EVERY survivor via ``evaluate_fusion_spec``
     (deterministic Python, 0 tokens), each wrapped in try/except (fix A5
     backstop), with ``num_trials=_society_num_trials(pool_size)`` (decouple
     #2) so the DSR multiple-testing correction counts only the strategy's
     OWN selection-from-pool search — never the library it joins.
  4. **C-null** — a survivor must beat the passive null (buy-and-hold) net of
     cost by ``MIN_COST_BENEFIT``. If none clears it → first-class ABSTAIN.
  5. **Synthesizer** — deterministic rank of the survivors → top-N leaderboard.

``_run_debate_candidate`` returns the **leader** ``_CandidateResult`` (for
back-compat callers); ``_run_debate_leaderboard`` returns the FULL ranked board
(the Phase-3 fan-out path used by ``run_generation``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

# Imported at module top: generation_pipeline does NOT import this module at top
# level (only lazily inside the dispatch), so there is no import cycle.
from archimedes.agents.generation_pipeline import (
    FusionUnavailable,
    _CandidateResult,
    _paper_attribution_entry,
    _society_num_trials,
)
from archimedes.agents.prompts import PROMPTS

# The WRITE-time scrubber, imported here because the SSE path never touches
# `record_debate_transcript` (which is where it normally runs). A debate turn
# pushed onto the stream is the same paid model text that lands in
# `debate_transcripts.transcript_json`; skipping the scrub would re-open the
# exact leak `_sanitize_claim` / `_sanitize_paper_verdict` exist to close, one
# transport to the left. No import cycle: models.debate_transcript imports only
# models.chat.
from archimedes.models.debate_transcript import sanitize_transcript
from archimedes.services import cost_meter
from archimedes.services._fusion_helpers import equity_curve_to_daily_returns
from archimedes.services.brief_screen import omit_if_rejected, quote_for_prompt
from archimedes.services.dsl_to_backtrader import SUPPORTED_INDICATORS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from archimedes.agents.generation_pipeline import _Emitter
    from archimedes.api.generate_schemas import GenerateBrief

logger = logging.getLogger(__name__)

# Steer grid = regime × mechanism. Each (regime_bias, mechanism) pair gives the
# proposer a distinct evidence ranking (regime_bias → select_candidates) AND a
# distinct mechanism hint (appended to strategic_direction), so the pool diverges
# on TWO axes — not just the 3 regime_bias values. This is the non-corpus diversity
# dimension that mitigates the "diversity theater" risk when the corpus is degraded
# (a degraded reranker can collapse regime steers; the mechanism hint still varies
# the proposer prompt). `_pool_max()` bounds how many of these steers actually fan out.
_REGIME_AXIS: tuple[str | None, ...] = ("bull", "bear", None)
_MECHANISM_AXIS: tuple[str, ...] = (
    "momentum / trend-following",
    "volatility-managed / defensive",
    "carry",
    "breakout",
    "mean-reversion",
    "minimum-variance",
)
# Cartesian product (regime × mechanism) = 18 distinct steers; `_pool_max()` caps the fan-out.
_STEERS: tuple[tuple[str | None, str], ...] = tuple((r, m) for r in _REGIME_AXIS for m in _MECHANISM_AXIS)

# Indicator stems interpret_spec actually supports — IMPORTED, not re-typed.
# The A5 guard drops specs the interpreter cannot build BEFORE they reach
# evaluate_fusion_spec, where a DSLError would take down the whole leaderboard
# build. It used to be a hand-maintained literal that excluded ``realized_vol``
# because interpret_spec raised on it; now that the interpreter implements the
# indicator, a hand-maintained copy would silently drop backtestable specs
# instead. Binding to the interpreter's own set means the guard tracks what the
# interpreter can actually do, in both directions.
_CONFORMANT_INDICATORS = set(SUPPORTED_INDICATORS)

# DSL price operands (not indicator aliases) — excluded from the conformance scan.
_PRICE_OPERANDS = {"close", "open", "high", "low", "volume"}

# ── Backtest fan-out pool (bounded, dedicated) ────────────────────────────────
#
# C-rigor backtests EVERY pooled survivor, so the fan-out is up to `_pool_max()`
# (default 10) concurrent `cerebro.run()` calls. backtrader is pure Python and
# therefore GIL-bound: those are not "waiting on IO" threads, they are N runnable
# CPU threads contending with the uvicorn event loop's own thread on a task that
# has 1-2 vCPU. Ten of them do not make the cohort ~10x faster; they make every
# concurrently-served request wait behind GIL handoffs.
#
# They also used to run on the *default* executor (`asyncio.to_thread`), which is
# the same pool `asset_market_service`, `traces_routes`, `chat_routes`,
# `portfolio_routes` and `strategies_routes` block on. A pool whose CPython
# default width is `min(32, os.cpu_count() + 4)` — 5 or 6 on the prod task — was
# fully occupied by one generation's backtests, so unrelated routes queued behind
# them. `GENERATION_MAX_CONCURRENT` (generate_routes.py) caps *pipelines*, not
# *threads*, so it never bounded this.
#
# A dedicated, named, small pool fixes both halves: the CPU-bound work is capped
# independently of how wide the pool is, and it can no longer starve the default
# pool that serving depends on. Same motivation as the `torch.set_num_threads(1)`
# guardrail in services/paper_rag.py (2026-07-04 reranker-starvation incident).
_BACKTEST_POOL_HARD_MAX = 2
_backtest_executor_lock = threading.Lock()
_backtest_executor_instance: ThreadPoolExecutor | None = None


def _backtest_max_workers() -> int:
    """``DEBATE_BACKTEST_WORKERS`` clamped to ``[1, 2]`` (default 2).

    The clamp is the point: the knob can make the pool narrower, never wider.
    Widening it is what the GIL contention above forbids, so an operator typo
    (or a well-meant "just bump it") cannot reintroduce the fan-out.
    """
    try:
        requested = int(os.getenv("DEBATE_BACKTEST_WORKERS", str(_BACKTEST_POOL_HARD_MAX)))
    except ValueError:
        requested = _BACKTEST_POOL_HARD_MAX
    return max(1, min(_BACKTEST_POOL_HARD_MAX, requested))


def _backtest_executor() -> ThreadPoolExecutor:
    """The process-wide, lazily-built backtest pool (never the default executor)."""
    global _backtest_executor_instance
    with _backtest_executor_lock:
        if _backtest_executor_instance is None:
            width = _backtest_max_workers()
            _backtest_executor_instance = ThreadPoolExecutor(
                max_workers=width,
                thread_name_prefix="debate-backtest",
            )
            logger.info("debate C-rigor: backtest pool created (max_workers=%d, GIL-bound work)", width)
        return _backtest_executor_instance


# C-null passive-null bar (V_check min_cost_benefit_bps = 5 bps): a survivor must
# beat buy-and-hold net of cost by at least this. Phase-1 proxy uses the
# backtest's own annualized edge (cagr); the explicit buy-and-hold differential
# is Phase 2.
MIN_COST_BENEFIT = 0.0005  # 5 bps


def _pool_max() -> int:
    """``DEBATE_POOL_MAX`` clamped to [2, 24] (default 10 for Phase 1).

    Bounds how many of the regime×mechanism ``_STEERS`` (18 total) actually fan out
    as proposer calls — so the env knob meaningfully caps the LLM cost.
    """
    try:
        return max(2, min(len(_STEERS), int(os.getenv("DEBATE_POOL_MAX", "10"))))
    except ValueError:
        return 10


def _leaderboard_max() -> int:
    """``DEBATE_LEADERBOARD_MAX`` clamped to [1, 24] (default 10 — the spec's top-10)."""
    try:
        return max(1, min(24, int(os.getenv("DEBATE_LEADERBOARD_MAX", "10"))))
    except ValueError:
        return 10


class DebateUnavailable(FusionUnavailable):
    """The society produced no actionable + conformant + backtestable candidate.

    Subclasses ``FusionUnavailable`` so ``run_generation``'s
    ``except FusionUnavailable`` clause catches it and emits
    ``GENERATION_UNAVAILABLE`` — no silent fallback (T1.1 Phase-3).
    """


# ── DSL conformance guard (fix A5) ───────────────────────────────────────────


def _indicator_alias_stems(spec: dict[str, Any]) -> set[str]:
    """Collect the indicator stems referenced as ``{indicator}_{period}`` operands.

    Walks the entry/exit condition trees. A string operand is an indicator alias
    iff it has a trailing ``_<int>`` (e.g. ``sma_50`` → ``sma``,
    ``realized_vol_5`` → ``realized_vol``). Price operands (``close`` …) and
    numeric operands are ignored.
    """
    stems: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and node not in _PRICE_OPERANDS:
            stem, sep, period = node.rpartition("_")
            if sep and stem and period.isdigit():
                stems.add(stem)

    walk(spec.get("entry"))
    walk(spec.get("exit"))
    return stems


def _dsl_conformance_ok(spec: dict[str, Any] | None) -> bool:
    """True iff ``spec`` is backtestable by ``interpret_spec`` (fix A5).

    Rejects specs whose indicator aliases fall outside
    ``dsl_to_backtrader.SUPPORTED_INDICATORS`` — anything else validates but
    raises ``DSLError`` at interpret time, which would otherwise throw inside
    C-rigor and take down the whole leaderboard build. A spec must also carry
    both an ``entry`` and an ``exit`` tree.
    """
    if not isinstance(spec, dict):
        return False
    if not isinstance(spec.get("entry"), dict) or not isinstance(spec.get("exit"), dict):
        return False
    return all(stem in _CONFORMANT_INDICATORS for stem in _indicator_alias_stems(spec))


# Non-behavioral spec keys: the LLM-generated marketing name and the citation
# list vary freely across steers (each proposer call re-fuses/re-labels) even
# when the actual trading logic (entry/exit/universe/sizing) is identical.
# #893's whole premise is that this happens; hashing these in would make the
# dedup a no-op against the exact failure mode it's meant to catch.
_HASH_IGNORED_SPEC_KEYS = frozenset({"name", "source_arxiv_ids"})


def _canonical_spec_hash(spec: dict[str, Any]) -> str:
    """Content hash of the BEHAVIORAL fields of ``spec`` (fix #893).

    Different regime/mechanism steers can independently converge on the same
    strategy (byte-identical ``entry``/``exit`` trees) under a different marketing
    name and/or citation set. Excludes ``_HASH_IGNORED_SPEC_KEYS`` so two specs
    that only differ by name/provenance still dedupe. Rounding floats to 6dp
    absorbs float-repr jitter without collapsing genuinely distinct
    parameterizations (e.g. sma_20 vs sma_21).
    """

    def _normalize(node: Any, *, top_level: bool = False) -> Any:
        if isinstance(node, dict):
            keys = node.keys() - _HASH_IGNORED_SPEC_KEYS if top_level else node.keys()
            return {k: _normalize(node[k]) for k in sorted(keys)}
        if isinstance(node, list):
            return [_normalize(v) for v in node]
        if isinstance(node, float):
            return round(node, 6)
        return node

    canonical = json.dumps(_normalize(spec, top_level=True), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ── Viability precheck ────────────────────────────────────────────────────────


def _debate_can_run(brief: GenerateBrief) -> bool:
    """No-LLM corpus viability precheck: ≥ MIN_PAPERS available for the steer.

    Phase-3: the ``ARCHIMEDES_DEBATE_ENABLED`` flag is retired — the society is
    unconditional. This precheck only guards against an empty/insufficient corpus
    (which would cause every proposer call to return ``insufficient_corpus``).
    Never raises — any failure degrades to ``False`` so ``run_generation`` emits
    ``GENERATION_UNAVAILABLE`` honestly rather than crashing.

    The retrieval itself lives in ``corpus_viability.assess_corpus_viability``,
    which returns the same verdict PLUS the count it measured and the
    corpus-derived ways forward. This wrapper keeps the long-standing bool
    contract (every caller and test binds to it) while guaranteeing that the
    gate and the failure explanation can never be computed by two different
    implementations. They remain two INVOCATIONS over a corpus that can move
    between them, so ``generation_pipeline`` guards the case where the second
    one comes back viable after this gate has already said no.
    """
    from archimedes.agents.corpus_viability import assess_corpus_viability

    return assess_corpus_viability(brief).can_run


# ── Step 1 — proposer pool ────────────────────────────────────────────────────


async def _propose_pool(
    brief: GenerateBrief, model: str | None, corpus: list[Any]
) -> tuple[list[Any], dict[str, dict[str, str]]]:
    """Fan ``StrategyFusion(model=...).propose`` across regime-steered evidence.

    Returns ``(pool, evidence_by_id)``:

    * **pool** — proposals that are both *actionable*
      (``FusionProposal.is_actionable``) AND *conformant* (``_dsl_conformance_ok``).
      ``pool_size = len(pool)`` is the DSR multiple-testing selection set.
    * **evidence_by_id** — ``{arxiv_id: {"title", "published"}}`` over every paper
      that entered ANY proposer prompt on this run (#1636). This is the debate's
      attribution whitelist: the bull/bear turns may cite an id only if it is in
      here, and an id outside it is a hallucination that gets stripped rather
      than recorded. It is deliberately the union over *attempted* proposals,
      not just pooled ones — a paper the model actually read is real evidence
      even when the spec built on it was later dropped for DSL non-conformance.
    """
    from archimedes.agents.strategy_fusion import (
        MIN_PAPERS,
        FusionBrief,
        StrategyFusion,
        select_candidates,
    )

    steers = list(_STEERS)[: _pool_max()]

    def _propose_one(steer: tuple[str | None, str]) -> tuple[Any, list[Any]] | None:
        regime_bias, mechanism = steer
        # Thread BOTH axes: regime_bias steers select_candidates' evidence ranking,
        # and the mechanism hint (appended to strategic_direction) steers the proposer
        # prompt so the same evidence + a different mechanism yields a genuinely
        # different spec — not an LLM-noise duplicate. (The caller-gap fix:
        # select_candidates accepts regime_bias but StrategyFusion.propose never
        # threads it; the proposer fuses over THIS steered set via the injected corpus.)
        fb = FusionBrief(
            asset_classes=list(brief.asset_classes or []),
            risk_appetite=brief.risk_appetite,
            strategic_direction=f"{brief.intent or ''} — favor {mechanism} mechanisms".strip(" —"),
            max_papers=brief.max_papers,
        )
        evidence = select_candidates(fb, corpus, regime_bias=regime_bias)
        if len(evidence) < MIN_PAPERS:
            return None
        # `candidates=` (not just `corpus=`) so propose() uses THIS set verbatim.
        # Passing it as the corpus made propose() re-run select_candidates over
        # it WITHOUT regime_bias — a second rerank that threw away the ordering
        # this steer just paid a rerank for (#1636).
        proposal = StrategyFusion(model=model, corpus=evidence, candidates=evidence).propose(fb)
        return proposal, evidence

    results = await asyncio.gather(
        *(asyncio.to_thread(_propose_one, s) for s in steers),
        return_exceptions=True,
    )

    pool: list[Any] = []
    evidence_by_id: dict[str, dict[str, str]] = {}
    seen_hashes: set[str] = set()
    dropped = 0
    for r in results:
        if isinstance(r, BaseException) or r is None:
            continue
        p, evidence = r
        for paper in evidence:
            arxiv_id = str(getattr(paper, "arxiv_id", "") or "")
            if arxiv_id and arxiv_id not in evidence_by_id:
                evidence_by_id[arxiv_id] = {
                    "title": str(getattr(paper, "title", "") or ""),
                    "published": str(getattr(paper, "published", "") or ""),
                }
        if not (p.is_actionable and _dsl_conformance_ok(p.strategy_spec)):
            continue
        spec_hash = _canonical_spec_hash(p.strategy_spec)
        if spec_hash in seen_hashes:
            dropped += 1
            continue
        seen_hashes.add(spec_hash)
        pool.append(p)

    if dropped:
        logger.info("debate proposer pool_deduped: kept=%d dropped=%d", len(pool), dropped)
    return pool, evidence_by_id


# ── Step 2 — best-effort adversarial round (transcript only, never gates) ─────

# The turn template, the two stances and the round-2 rebuttal preamble all live
# in the prompt registry (`agents/prompts.py`), rendered into
# `docs/specs/prompt-inventory.md` under a drift test (#1800). The registry
# templates on `$name`, so the JSON schema the turn prints no longer needs its
# braces doubled the way `.format` required.
_DEBATE_SYSTEM = PROMPTS["debate.turn.system"]

_DEBATE_STANCES = {
    "bull": PROMPTS["debate.stance.bull"].text,
    "bear": PROMPTS["debate.stance.bear"].text,
}

# How many pooled candidates get a card in the debate prompt. Unchanged from
# the pre-#1636 `pool[:5]` slice — what changed is that the pool is SORTED
# first, so the five are a stable, explainable set rather than whichever five
# steers happened to finish in that order.
_DEBATE_CARD_MAX = 5


def _debate_pool_order(pool: list[Any]) -> list[Any]:
    """Deterministic debate ordering: most-evidenced candidate first.

    ``_debate_round`` shows only the first ``_DEBATE_CARD_MAX`` candidates. It
    used to take them straight off ``pool``, whose order is the order the
    regime×mechanism steers happened to be enumerated in — so which candidates
    got debated was an artifact of the steer grid, not a property of the
    candidates. This key is ``(-cited_paper_count, strategy_name, spec_hash)``:

    * cited-paper count first — the debate is about evidence, so the candidates
      standing on the most papers are the ones worth the tokens;
    * name then spec-hash as pure tiebreaks, so the order is total and stable
      across runs (R3 determinism) with no randomness and no LLM call.

    It is explicitly NOT a quality ranking — at debate time nothing has been
    backtested yet. C-rigor / C-null still do all the culling.
    """
    return sorted(
        pool,
        key=lambda p: (
            -len(getattr(p, "source_arxiv_ids", None) or []),
            str(getattr(p, "strategy_name", "") or ""),
            _canonical_spec_hash(getattr(p, "strategy_spec", None) or {}),
        ),
    )


def _candidate_cards(pool: list[Any], evidence_by_id: dict[str, dict[str, str]]) -> str:
    """Per-candidate evidence cards — the debate's whole user message (#1636).

    Was a ``"; "``-joined list of strategy NAMES: four paid LLM turns argued
    about strings like "Momentum/vol fusion" with no paper anywhere in the
    prompt, while the product claim is a multi-agent debate over the corpus.
    Each line is now ``[C1] Name — cites arXiv:xxxx "Title"``, which is both
    what the researchers argue over and the id vocabulary the anti-hallucination
    guard in ``_turn`` checks their claims against. ~200 chars → ~2 KB.
    """
    lines: list[str] = []
    for i, p in enumerate(_debate_pool_order(pool)[:_DEBATE_CARD_MAX], start=1):
        # STRUCT screen on model-authored text re-entering a prompt (#1801).
        # `strategy_name` is proposer output and lands unescaped in a
        # LINE-ORIENTED format: a name carrying "\n[C6] … — cites arXiv:0000"
        # forges a card no proposer produced, and `_turn`'s anti-hallucination
        # guard checks claims against the ids printed on these cards — so a
        # forged card is a forged evidence base. A refused name is OMITTED
        # (the card falls back to its positional label, exactly as it already
        # does for an empty name) and the omission is logged. The stored name
        # is never rewritten; only this outgoing prompt declines to carry it.
        screened, _ = omit_if_rejected(
            str(getattr(p, "strategy_name", "") or "").strip(),
            field="strategy_name",
            context=f"card C{i}",
        )
        name = screened or f"Candidate {i}"
        cites: list[str] = []
        for arxiv_id in getattr(p, "source_arxiv_ids", None) or []:
            # The paper title is THIRD-PARTY text — arXiv metadata, which
            # `arxiv_pipeline._doc_safe` already treats as attacker-controlled
            # for the code-generation seam (#920) — and it lands on the same
            # line-oriented card as the strategy name. Two of the four ingest
            # paths only `.strip()` it, so a title carrying "\n[C6] … — cites
            # arXiv:0000" forges a card whose ids `_turn`'s anti-hallucination
            # guard then trusts. `quote_for_prompt` (#1801) screens it and
            # returns it as ONE quoted JSON token; a refused title is omitted
            # and the card keeps the bare id, exactly as it already does for a
            # paper with no title at all. The stored corpus row is untouched.
            title = (evidence_by_id.get(str(arxiv_id)) or {}).get("title", "").strip()
            quoted, _ = quote_for_prompt(title, field="paper_title", context=f"card C{i} arXiv:{arxiv_id}")
            cites.append(f"arXiv:{arxiv_id} {quoted}" if quoted else f"arXiv:{arxiv_id}")
        suffix = f" — cites {'; '.join(cites)}" if cites else " — cites no listed paper"
        lines.append(f"[C{i}] {name}{suffix}")
    return "\n".join(lines)


def _claim_text(claim: Any) -> str:
    """The prose of one claim, for either shape.

    Turns produced after #1636 carry ``{"claim", "candidate_id", "arxiv_ids"}``;
    rows persisted before it are bare strings. Both must read back.
    """
    if isinstance(claim, dict):
        return str(claim.get("claim", "") or "")
    return str(claim or "")


def _rebuttal_clause(opponent_claims: list[Any], *, role: str, rnd: int) -> str:
    """The round-2 rebuttal sentence, built from the opponent's own prose.

    This is the one place in the debate where a model writes another model's
    instructions: the text lands unescaped inside ``_DEBATE_SYSTEM``'s
    ``${rebuttal}`` slot, so a claim carrying a newline, a ``[C6]`` card marker
    or an override directive would be read as system-level framing by the next
    turn.

    Every claim is screened (#1801) and a refused one is **omitted** from this
    clause — never rewritten, never truncated, never redacted. If every claim
    is refused the clause is empty, which is exactly the prompt round 1 gets.
    The claims themselves stay byte-for-byte intact in the transcript.
    """
    if not opponent_claims:
        return ""
    texts = [
        screened
        for screened, _ in (
            omit_if_rejected(t, field="rebuttal_claim", context=f"{role} r{rnd}")
            for t in (_claim_text(c) for c in opponent_claims[:3])
            if t
        )
        if screened
    ]
    if not texts:
        return ""
    return PROMPTS["debate.rebuttal_preamble"].render(opponent_claims="; ".join(texts))


def _normalize_claim(raw: Any, known_ids: set[str]) -> dict[str, Any] | None:
    """One claim, id-checked against the evidence actually shown (#1636).

    Mirrors ``strategy_fusion.propose``'s ``valid_ids`` filter: an arxiv_id the
    proposers never put in front of anyone is dropped. What it does NOT do is
    drop the claim — a claim whose every id was invented is kept with
    ``arxiv_ids: []`` so it is **recorded as unattributed**. Deleting it would
    quietly shrink the transcript; keeping it with its fake citation would
    launder a hallucination into the provenance record. Neither is honest.

    Returns None only for a claim with no prose at all (nothing to record).
    """
    if isinstance(raw, str):
        text = raw.strip()
        return {"claim": text, "candidate_id": None, "arxiv_ids": []} if text else None
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("claim") or raw.get("text") or "").strip()
    if not text:
        return None
    requested = [str(a).strip() for a in (raw.get("arxiv_ids") or []) if isinstance(a, str) and str(a).strip()]
    deduped = list(dict.fromkeys(requested))
    kept = [a for a in deduped if a in known_ids]
    if len(kept) != len(deduped):
        logger.info(
            "debate: dropped %d unknown arxiv id(s) from a %s claim — recorded as unattributed",
            len(deduped) - len(kept),
            "partially attributed" if kept else "fully unattributed",
        )
    candidate_id = raw.get("candidate_id")
    return {
        "claim": text,
        "candidate_id": str(candidate_id).strip() if isinstance(candidate_id, str) and candidate_id.strip() else None,
        "arxiv_ids": kept,
    }


def _normalize_discards(raw: Any, known_ids: set[str]) -> list[dict[str, str]]:
    """``[{arxiv_id, reason}]``, restricted to papers actually shown (#1636).

    A discard naming an id nobody was shown is the same hallucination the claim
    guard catches, and unlike a claim it carries no prose worth keeping on its
    own — so it is dropped outright.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        arxiv_id = str(entry.get("arxiv_id", "") or "").strip()
        if arxiv_id not in known_ids or arxiv_id in seen:
            continue
        seen.add(arxiv_id)
        out.append({"arxiv_id": arxiv_id, "reason": str(entry.get("reason", "") or "").strip()})
    return out


def _aggregate_paper_verdicts(
    transcript: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Per-paper tally over a finished transcript — deterministic, 0 tokens.

    This is what turns "the papers were debated" from a claim into something a
    reader can check: for every paper that entered a proposer prompt, which
    side cited it, how often, and who threw it out for what reason. Papers
    nobody touched are listed with ``verdict="unused"`` rather than omitted —
    the absence is the interesting part when 30 papers were retrieved and 3
    were argued over.

    Sorted by arxiv_id so two runs over the same transcript are byte-identical.
    """
    tallies: dict[str, dict[str, Any]] = {
        arxiv_id: {
            "arxiv_id": arxiv_id,
            "title": meta.get("title", ""),
            "cited_by": [],
            "citations": 0,
            "discarded_by": [],
            "discard_reasons": [],
        }
        for arxiv_id, meta in evidence_by_id.items()
    }
    for turn in transcript:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role", "") or "")
        for claim in turn.get("claims") or []:
            if not isinstance(claim, dict):
                continue  # a pre-#1636 string claim carries no attribution
            for arxiv_id in claim.get("arxiv_ids") or []:
                row = tallies.get(str(arxiv_id))
                if row is None:
                    continue
                row["citations"] += 1
                if role and role not in row["cited_by"]:
                    row["cited_by"].append(role)
        for discard in turn.get("discard") or []:
            row = tallies.get(str((discard or {}).get("arxiv_id", "")))
            if row is None:
                continue
            if role and role not in row["discarded_by"]:
                row["discarded_by"].append(role)
            reason = str(discard.get("reason", "") or "").strip()
            if reason and reason not in row["discard_reasons"]:
                row["discard_reasons"].append(reason)

    out: list[dict[str, Any]] = []
    for arxiv_id in sorted(tallies):
        row = tallies[arxiv_id]
        cited, discarded = bool(row["cited_by"]), bool(row["discarded_by"])
        row["verdict"] = (
            "contested" if cited and discarded else "cited" if cited else "discarded" if discarded else "unused"
        )
        out.append(row)
    return out


#: Human nouns for the two debate roles and the two rounds. The stream used to
#: carry the debate as four ``tool_called`` markers whose ``args_summary`` was
#: ``cards[:120]`` — a truncated evidence card, never the turn — so a user
#: watching a generation saw that a "tool" named ``debate_bear_r2`` had been
#: called and nothing about what either researcher actually argued.
_ROLE_NOUNS = {"bull": "Bull researcher", "bear": "Bear researcher"}
_ROUND_NOUNS = {1: "opening argument", 2: "rebuttal"}


def _turn_headline(turn: dict[str, Any]) -> str:
    """One human sentence for a finished debate turn — server-written copy.

    Built from the SANITIZED turn (see :func:`_emit_debate_turn`), so the
    verdict it quotes is the scrubbed one, not the raw model string.

    Counts rather than adjectives: how many claims the researcher made, how
    many of them were grounded in a paper it was actually shown, and how many
    papers it threw out. "2 of 3 claims grounded in a named paper" is checkable
    against the claim list rendered underneath it; "a well-argued case" is not.

    A turn whose backend output could not be parsed degrades to
    ``{verdict: "n/a", claims: [], discard: []}``; that says so plainly instead
    of rendering an empty card, because an unreadable answer IS the record.
    """
    role = str(turn.get("role") or "")
    who = _ROLE_NOUNS.get(role, "Researcher")
    rnd = turn.get("round")
    stage = _ROUND_NOUNS.get(rnd if isinstance(rnd, int) else None, "argument")
    claims = list(turn.get("claims") or [])
    discards = list(turn.get("discard") or [])
    verdict = str(turn.get("verdict") or "").strip()
    has_verdict = bool(verdict) and verdict.lower() != "n/a"

    if not claims and not discards and not has_verdict:
        return f"{who}, {stage} — no argument recorded (the researcher's answer could not be read)."

    parts = [f"{who}, {stage}"]
    if has_verdict:
        parts.append(f"verdict: {verdict}")
    head = " — ".join(parts)

    tail: list[str] = []
    if claims:
        grounded = sum(1 for c in claims if isinstance(c, dict) and (c.get("arxiv_ids") or []))
        tail.append(f"{len(claims)} claim{'' if len(claims) == 1 else 's'}, {grounded} grounded in a named paper")
    else:
        tail.append("no claims recorded")
    if discards:
        tail.append(f"{len(discards)} paper{'' if len(discards) == 1 else 's'} set aside")
    return f"{head}. {'; '.join(tail)}."


async def _emit_debate_turn(emit: _Emitter, candidate_id: str, turn: dict[str, Any]) -> None:
    """Push ONE finished debate turn onto the SSE stream — sanitized, never raw.

    Called immediately after the turn is appended to the transcript, so the
    stream is 1:1 with what gets stored: what the user watched is what was
    written. The payload carries the turn itself (``role``/``round``/
    ``verdict``/``claims``/``discard``) plus a server-written ``headline``, and
    nothing else — the whole transcript is never re-sent per turn.

    ``sanitize_transcript`` is mandatory here and is the reason this helper
    exists at all. It normally runs inside ``record_debate_transcript``, on the
    way into ``debate_transcripts``; the SSE path bypasses the DB entirely, so
    without this call the raw model text would reach a browser through a
    transport the write-time scrubber never sees. Best-effort like the rest of
    the debate round: a failure here drops the EVENT, never the turn, and never
    falls back to emitting the unsanitized dict.
    """
    try:
        safe = sanitize_transcript([turn])
    except Exception:
        logger.debug("debate: could not sanitize a turn for the stream — event dropped", exc_info=True)
        return
    if not safe:
        return
    payload = safe[0]
    await emit.emit(
        "debate_turn",
        candidate_id=candidate_id,
        role=payload.get("role"),
        round=payload.get("round"),
        verdict=payload.get("verdict"),
        claims=payload.get("claims") or [],
        discard=payload.get("discard") or [],
        headline=_turn_headline(payload),
    )


async def _debate_round(
    pool: list[Any],
    model: str | None,
    emit: _Emitter,
    candidate_id: str,
    evidence_by_id: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Best-effort bull/bear research debate with ONE visible rebuttal round.

    Round 1: bull + bear state initial positions. Round 2: each REBUTS the other's
    round-1 claims (the visible adversarial turn — the "debate" the roadmap names).
    Transcript ONLY — it never gates; the deterministic critics do the real culling.
    Built in fixed ``[bull-r1, bear-r1, bull-r2, bear-r2]`` order for R3 determinism
    (sort-before-hash). Any failure (no backend, unparseable output) degrades to a
    neutral entry; the whole round is skipped if no backend is available.

    ``evidence_by_id`` (from ``_propose_pool``) is what makes the round about
    PAPERS: it supplies the titles printed on the candidate cards and the id
    vocabulary every claim is checked against. Omitted/empty ⇒ no id can be
    validated, so every claim is recorded as unattributed — degraded, but never
    silently attributed to a paper nobody read.
    """
    from archimedes.agents.generation_json import extract_json
    from archimedes.services.llm_backend import make_llm_backend

    known_ids = set(evidence_by_id or {})
    cards = _candidate_cards(pool, evidence_by_id or {})
    transcript: list[dict[str, Any]] = []
    try:
        backend = make_llm_backend(model=model)
    except Exception:
        logger.debug("debate round skipped (backend construction failed)", exc_info=True)
        return transcript
    if not getattr(backend, "available", False):
        return transcript

    def _turn(role: str, rnd: int, opponent_claims: list[Any]) -> dict[str, Any]:
        rebuttal = _rebuttal_clause(opponent_claims, role=role, rnd=rnd)
        try:
            raw = backend.complete(
                _DEBATE_SYSTEM.render(role=role, rnd=rnd, stance=_DEBATE_STANCES[role], rebuttal=rebuttal),
                cards,
            )
            parsed = extract_json(raw)
            raw_claims = parsed.get("key_claims") or parsed.get("fatal_flaws") or []
            claims = [c for c in (_normalize_claim(rc, known_ids) for rc in raw_claims) if c is not None]
            return {
                "role": role,
                "round": rnd,
                "verdict": str(parsed.get("verdict", "n/a")),
                "claims": claims,
                "discard": _normalize_discards(parsed.get("discard"), known_ids),
            }
        except Exception:
            return {"role": role, "round": rnd, "verdict": "n/a", "claims": [], "discard": []}

    # Round 1 — initial positions (fixed bull→bear order). Each finished turn
    # is pushed onto the stream as it lands (`_emit_debate_turn`), so the four
    # `tool_called` markers below are no longer the only trace of the debate a
    # watching user ever sees.
    for role in ("bull", "bear"):
        await emit.emit(
            "tool_called", candidate_id=candidate_id, tool_name=f"debate_{role}_r1", args_summary=cards[:120]
        )
        turn = await asyncio.to_thread(_turn, role, 1, [])
        transcript.append(turn)
        await _emit_debate_turn(emit, candidate_id, turn)

    # Round 2 — visible rebuttal: each researcher sees the other's round-1 claims.
    claims_by_role = {t["role"]: t["claims"] for t in transcript}
    for role, opponent in (("bull", "bear"), ("bear", "bull")):
        await emit.emit(
            "tool_called", candidate_id=candidate_id, tool_name=f"debate_{role}_r2", args_summary="rebuttal"
        )
        turn = await asyncio.to_thread(_turn, role, 2, claims_by_role.get(opponent, []))
        transcript.append(turn)
        await _emit_debate_turn(emit, candidate_id, turn)

    return transcript


# ── Step 3 — C-rigor (deterministic, real backtests) ──────────────────────────


async def _critic_rigor(pool: list[Any], num_trials: int) -> list[tuple[Any, Any]]:
    """Backtest every pooled spec for real; return ``[(proposal, eval_result)]``.

    ``num_trials`` is ``_society_num_trials(pool_size)`` (decouple #2), so the
    DSR deflation counts only the strategy's OWN selection-from-pool search —
    never the library it joins. Each ``evaluate_fusion_spec`` is wrapped in
    try/except so one bad spec (despite the A5 pre-guard) drops with an honest
    emit, never aborting the cohort.
    """
    from archimedes.services.fusion_evaluator import evaluate_fusion_spec
    from archimedes.services.fusion_market_data import real_data_enabled

    # Real data per candidate (#788/#818): the fetch is cached per ticker-set,
    # so a pool sharing a universe costs one yfinance round-trip, not N.
    _use_real = real_data_enabled()

    def _backtest(proposal: Any) -> Any:
        try:
            return evaluate_fusion_spec(proposal.strategy_spec, num_trials=num_trials, use_real_data=_use_real)
        except Exception as exc:
            logger.info("debate C-rigor: dropped a candidate on backtest error: %s", exc)
            return None

    # Bounded + dedicated, NOT `asyncio.to_thread` (which is the default pool).
    # See the `_backtest_executor` block above for why the fan-out is capped at 2.
    loop = asyncio.get_running_loop()
    executor = _backtest_executor()
    results = await asyncio.gather(*(loop.run_in_executor(executor, _backtest, p) for p in pool))
    out: list[tuple[Any, Any]] = []
    for proposal, ev in zip(pool, results, strict=True):
        if ev is not None and ev.success and ev.rigor is not None:
            out.append((proposal, ev))
    return out


# ── C-prov (deterministic provenance/embargo gate — Xia 1/2/4) ────────────────


def _critic_prov(pool: list[Any], corpus: list[Any]) -> tuple[list[Any], list[Any]]:
    """C-prov (Xia 1/2/4, non-votable): hard-fail any candidate citing a paper
    OUTSIDE the shared embargo + decay-applied evidence surface.

    The surface is the ``load_corpus()`` output, which already excludes post-embargo
    papers (``apply_outcome_embargo`` runs inside ``load_papers_from_db``), so a
    candidate whose ``source_arxiv_ids`` are all in the corpus is provenance-clean.
    A candidate citing an id NOT in the surface (a post-embargo leak or a
    hallucination that slipped the proposer's ``valid_ids`` filter) is dropped —
    deterministic defense-in-depth that cannot be argued out of its position.

    Does NOT change ``pool_size`` (the DSR denominator counts every conformant spec
    we proposed/searched, per spec §5c); it only culls which survivors reach C-rigor.
    Returns ``(kept, dropped)``.
    """
    surface = {getattr(p, "arxiv_id", None) for p in corpus}
    surface.discard(None)
    kept: list[Any] = []
    dropped: list[Any] = []
    for prop in pool:
        # Robust to a proposal missing/None source_arxiv_ids — treat as empty (→ drop,
        # "not provenance-verifiable"), never raise and abort the whole run (Copilot review).
        cited = set(getattr(prop, "source_arxiv_ids", None) or [])
        if cited and cited <= surface:
            kept.append(prop)
        else:
            dropped.append(prop)
    return kept, dropped


# ── Step 4/5 — C-null + synthesize → leaderboard ──────────────────────────────


def _survives_null(ev: Any) -> bool:
    """C-null: the candidate beats the passive null net of cost by ≥ 5 bps.

    Phase-1 proxy: the backtest's own annualized edge (cagr) clears
    ``MIN_COST_BENEFIT``. The explicit buy-and-hold differential is Phase 2.
    """
    cagr = getattr(ev.backtest, "cagr", None)
    return cagr is not None and cagr > MIN_COST_BENEFIT


def _score(ev: Any) -> tuple[int, float, float]:
    """Deterministic leaderboard rank key: (passing, DSR, OOS Sharpe), desc."""
    r = ev.rigor
    dsr = r.dsr if r.dsr is not None else -1e18
    oos = r.oos_sharpe if r.oos_sharpe is not None else -1e18
    return (1 if r.passing else 0, dsr, oos)


def _rigor_verdict_dict(ev: Any) -> dict[str, Any]:
    """Build the passport ``rigor_verdict`` from a FusionEvalResult.

    Canonical rigor_verdict shape consumed by the passport renderer.
    """
    r = ev.rigor
    bt = ev.backtest
    return {
        "dsr": r.dsr,
        "dsr_p_value": r.dsr_p_value,
        "pbo": r.pbo_score,
        "oos_sharpe": r.oos_sharpe,
        "in_sample_sharpe": r.in_sample_sharpe,
        # DERIVED by the structural audit (services/dsl_lookahead_audit.py),
        # never declared by the generating model — the DSL has no field in which
        # it could declare it. True only on a computed "pass"; this bool renders
        # "pending" (nothing audited) identically to a real failure, so any
        # surface making a look-ahead claim must key off look_ahead_status.
        "lookahead_audit_passed": bool(r.look_ahead_clean),
        # The honest surfaced field, four-state: "pass" | "fail" | "pending" |
        # "degenerate". The last two are NOT passes and are NOT failures.
        "look_ahead_status": r.look_ahead_status,
        "look_ahead_label": r.look_ahead_label,
        "look_ahead_reason": r.look_ahead_reason,
        "num_trials": int(r.num_trials),  # own pool_size, decouple #2
        "passing": bool(r.passing),
        "data_source": r.data_source,
        "admissible": bool(ev.admissible),
        "sharpe_ratio": bt.sharpe_ratio,
        "sortino_ratio": bt.sortino_ratio,
        "max_drawdown": bt.max_drawdown,
        "cagr": bt.cagr,
        "calmar_ratio": bt.calmar_ratio,
        "win_rate": bt.win_rate,
        "total_trades": bt.total_trades,
        # A8: which runner produced this row and how it combined assets. The
        # sleeve runner grades N independent single-asset backtests and
        # equal-weights them, so a multi-asset spec is NOT a cross-sectionally
        # allocated portfolio. Carried here rather than re-derived downstream —
        # the runner is the only place that knows.
        "backtest_engine": getattr(bt, "backtest_engine", None),
        "portfolio_construction": getattr(bt, "portfolio_construction", None),
    }


def _make_entry(
    candidate_id: str,
    proposal: Any,
    ev: Any,
    *,
    regime: str,
    evidence_by_id: dict[str, dict[str, str]] | None = None,
) -> _CandidateResult:
    """One leaderboard entry — a fully-populated ``_CandidateResult``.

    ``has_real_rigor=True`` (carries C-rigor's real backtest) so the downstream
    ``_patch_pbo`` and buy-and-hold gather correctly SKIP it (keyed on
    ``has_real_rigor``), preserving the CSCV PBO from ``evaluate_fusion_spec``.

    ``evidence_by_id`` (#1739) is the ``{arxiv_id: {"title", "published"}}`` map
    ``_propose_pool`` already built — the only place in the run that knows a
    cited paper's TITLE. Without it every ``source_papers`` entry shipped
    ``"title": ""`` and the Library card had nothing but an id to print.
    Defaults to ``None`` (→ blank titles, the old behaviour) so the pure
    ranking tests can call ``build_leaderboard`` without an evidence map.
    """
    spec = proposal.strategy_spec or {}
    # (#1739) The server-filtered paper→mechanism map, keyed for the join
    # below. First entry per id wins; a cited paper with no entry is carried
    # with an empty mechanism, which is the honest "cited but unattributed"
    # record (never dropped — dropping it would hide the shortfall).
    mechanisms = getattr(proposal, "paper_mechanisms", None) or []
    mech_by_id: dict[str, dict[str, Any]] = {}
    for entry in mechanisms:
        if not isinstance(entry, dict):
            continue
        arxiv_id = str(entry.get("arxiv_id", "") or "")
        if arxiv_id and arxiv_id not in mech_by_id:
            mech_by_id[arxiv_id] = entry
    titles = evidence_by_id or {}
    # Real per-bar returns for the live gate (#788/#818, mirrors the fusion
    # path): without return_series, _persist_real_returns SKIPS the winner and
    # the passport reads "pending" forever even though C-rigor just ran a real
    # backtest (first prod debate run surfaced exactly this).
    _ec = list(getattr(ev.backtest, "equity_curve", None) or [])
    real_returns = equity_curve_to_daily_returns(_ec)
    return _CandidateResult(
        candidate_id=candidate_id,
        strategy_name=proposal.strategy_name or "Debate candidate",
        thesis=proposal.thesis,
        asset_universe=list(spec.get("asset_universe", []) or []),
        source_papers=[
            {
                "arxiv_id": a,
                "title": (titles.get(a) or {}).get("title", ""),
                "mechanism": str((mech_by_id.get(a) or {}).get("mechanism", "") or ""),
                "spec_elements": list((mech_by_id.get(a) or {}).get("spec_elements", []) or []),
            }
            for a in proposal.source_arxiv_ids
        ],
        weights={},  # debate emits a DSL spec, not a static weight vector
        reasoning=proposal.fusion_reasoning or proposal.novelty_rationale or "",
        rigor_verdict=_rigor_verdict_dict(ev),
        passes_rigor=bool(ev.rigor.passing),
        regime=regime,
        generation_method="debate",
        source_arxiv_ids=list(proposal.source_arxiv_ids),
        has_real_rigor=True,
        return_series=real_returns or None,
        # #857: which branch _spec_universe took for this proposal's universe
        # (user steer / model suggestion / full SSOT fallback). Falls back to
        # "full" for the rare case a proposal predates the field.
        universe_source=getattr(proposal, "universe_source", None) or "full",
        # Rebalancer decouple (Part A #1): carry the full validated DSL spec
        # forward (not just the asset_universe slice pulled above) so
        # _persist_candidate can persist it onto the StrategyRecord — the
        # live agent runner later loads it to evaluate a deployed generated
        # strategy under its own signal rule instead of silently skipping it.
        strategy_spec=spec or None,
        # (#1739) The budget-vs-used pair and the per-paper prose, carried onto
        # the candidate instead of ending at a log line / a write-only field.
        papers_offered=int(getattr(proposal, "papers_offered", 0) or 0),
        distinct_mechanism_papers=int(getattr(proposal, "distinct_mechanism_papers", 0) or 0),
        fusion_reasoning=str(getattr(proposal, "fusion_reasoning", "") or ""),
    )


def _abstain_result(candidate_id: str, *, regime: str, reason: str) -> _CandidateResult:
    """First-class ABSTAIN — a populated, SKIP-shaped ``_CandidateResult``.

    ``generation_method="debate_abstain"`` flows through the existing emit/persist
    path (and V_check's SKIP-trace mechanism); it is NOT a new error code.
    """
    return _CandidateResult(
        candidate_id=candidate_id,
        strategy_name="Debate — abstain (hold current weights)",
        thesis=reason,
        asset_universe=[],
        source_papers=[],
        weights={},
        reasoning=reason,
        rigor_verdict={
            "dsr": None,
            "pbo": None,
            "oos_sharpe": None,
            "in_sample_sharpe": None,
            "lookahead_audit_passed": False,
            "passing": False,
            "reason": reason,
        },
        passes_rigor=False,
        regime=regime,
        generation_method="debate_abstain",
        source_arxiv_ids=[],
        has_real_rigor=False,
    )


def _critic_regime() -> dict[str, Any]:
    """C-regime (Xia §4.4 Hierarchy-of-Truth) — read the live exogenous regime.

    **Non-votable.** A live CRISIS read forces ABSTAIN regardless of how good the
    candidates look — crisis is exactly when you do NOT deploy a fresh strategy,
    and no bull argument can override it. DEGRADED (GMM artifact missing → VIX
    rule-based fallback) is surfaced honestly and lowers confidence, but does not
    by itself force abstain (else the society would always abstain when the model
    is unavailable). Never raises — any failure degrades to "unavailable, don't
    force" so the regime critic can only ABSTAIN, never spuriously APPROVE.

    Returns a dict: ``regime`` (str|None), ``confidence`` (float), ``degraded``
    (bool), ``force_abstain`` (bool), ``reason`` (str).
    """
    out: dict[str, Any] = {
        "regime": None,
        "confidence": 0.0,
        "degraded": True,
        "force_abstain": False,
        "reason": "regime detector unavailable — not gating",
    }
    try:
        from archimedes.models.regime import Regime
        from archimedes.services.gmm_regime_detector import current_regime, gmm_regime_health

        health = gmm_regime_health()
        degraded = health.status != "live"
        # Read the SHARED live detector (the one the oracle/agent runner feeds), NOT a
        # fresh GmmRegimeDetector — a new instance has no current classification, so it
        # would always read None and the gate would never fire (Copilot review).
        rc = current_regime()
        regime = rc.regime if rc is not None else None
        confidence = float(rc.confidence) if rc is not None else 0.0
        force_abstain = regime == Regime.CRISIS
        if regime is None:
            reason = "no regime read — not gating"
        elif force_abstain:
            reason = (
                f"CRISIS regime (confidence={confidence:.2f}"
                f"{', GMM degraded → VIX fallback' if degraded else ''}) — non-votable ABSTAIN"
            )
        else:
            reason = (
                f"regime={regime.value} confidence={confidence:.2f}"
                f"{' (GMM degraded → VIX rule-based fallback)' if degraded else ''}"
            )
        out = {
            "regime": regime.value if regime is not None else None,
            "confidence": confidence,
            "degraded": degraded,
            "force_abstain": force_abstain,
            "reason": reason,
        }
    except Exception:
        logger.debug("C-regime read failed; treating as unavailable (not gating)", exc_info=True)
    return out


def build_leaderboard(
    rigor_results: list[tuple[Any, Any]],
    *,
    regime: str,
    base_id: str,
    regime_force_abstain: bool = False,
    regime_reason: str = "",
    evidence_by_id: dict[str, dict[str, str]] | None = None,
) -> list[_CandidateResult]:
    """Deterministic C-regime gate → C-null cull + rank → top-N leaderboard.

    The **non-votable C-regime gate runs first**: a live-CRISIS
    ``regime_force_abstain`` short-circuits to ABSTAIN before C-null even runs —
    market regime structurally overrides candidate consensus (Hierarchy-of-Truth).
    Otherwise: C-null cull → rank → leaderboard (leader keeps ``base_id``,
    alternatives get ``base_id_alt{n}``). Pure + deterministic — directly tested.

    ``evidence_by_id`` (#1739) is threaded through to ``_make_entry`` purely so
    a cited paper's TITLE reaches ``source_papers``; it never influences
    ranking, culling, or the abstain paths. Optional, so the pure ranking tests
    keep calling this with rigor results alone.
    """
    if regime_force_abstain:
        return [
            _abstain_result(
                base_id,
                regime=regime,
                reason=f"Regime gate (non-votable, Hierarchy-of-Truth): {regime_reason}",
            )
        ]
    survivors = [(p, ev) for (p, ev) in rigor_results if _survives_null(ev)]
    if not survivors:
        return [
            _abstain_result(
                base_id,
                regime=regime,
                reason="No candidate beat the passive null by ≥ 5 bps net of cost — abstaining (hold current weights).",
            )
        ]
    survivors.sort(key=lambda pe: _score(pe[1]), reverse=True)
    # Cap to the top-N leaderboard (spec's top-10 contract) so the persisted/streamed
    # candidate set can't balloon when the pool is large.
    survivors = survivors[: _leaderboard_max()]
    return [
        _make_entry(
            base_id if i == 0 else f"{base_id}_alt{i}",
            p,
            ev,
            regime=regime,
            evidence_by_id=evidence_by_id,
        )
        for i, (p, ev) in enumerate(survivors)
    ]


# ── Runner (the dispatch entry point) ─────────────────────────────────────────


async def _run_debate_leaderboard(
    *,
    candidate_id: str,
    brief: GenerateBrief,
    emit: _Emitter,
    regime: str = "neutral",
    agent: Any = None,  # noqa: ARG001 — signature parity with the other runners
    model: str | None = None,
    selection_pool_size: int = 1,  # noqa: ARG001 — parity with the #770 runner contract; the debate computes its OWN pool_size (the real selection count) internally
) -> list[_CandidateResult]:
    """Run the debate society once and return the FULL ranked leaderboard.

    Phase-3 fan-out: entry [0] is the society's leader (its deterministic rank
    is authoritative — the orchestrator must NOT re-rank); the tail entries are
    the Considered Alternatives. Raises ``DebateUnavailable``
    (a ``FusionUnavailable`` subclass) when no candidate survives, so
    ``run_generation`` emits ``GENERATION_UNAVAILABLE`` (T1.1 Phase-3).

    ``selection_pool_size`` is accepted for parity with the #770 runner contract
    (the dispatch threads it to every runner), but the society ignores the passed
    value and uses its OWN internally-computed ``pool_size = len(POOL)`` — the
    actual count of conformant proposed specs, which is the correct DSR
    selection set, not the user's ``n_candidates``.
    """
    from archimedes.agents.strategy_fusion import load_corpus

    await emit.emit("agent_iteration", candidate_id=candidate_id, iteration_n=1, max_iterations=4)
    await emit.emit(
        "tool_called",
        candidate_id=candidate_id,
        tool_name="propose_pool",
        args_summary=f"steers={len(_STEERS)}, asset_classes={brief.asset_classes or '(any)'}",
    )

    # Cost instrumentation (#1217): the society's own phases are timed here so the
    # per-phase breakdown answers "which phase dominates" — corpus retrieval vs.
    # the LLM debate vs. the backtests — rather than collapsing into one number.
    with cost_meter.stage("corpus_load"):
        corpus = await asyncio.to_thread(load_corpus)
    with cost_meter.stage("debate_propose"):
        pool, evidence_by_id = await _propose_pool(brief, model, corpus)
    pool_size = len(pool)
    if pool_size == 0:
        raise DebateUnavailable("debate produced no actionable, DSL-conformant candidate (empty pool)")

    await emit.emit(
        "tool_result",
        candidate_id=candidate_id,
        tool_name="propose_pool",
        result_summary=f"pool_size={pool_size} actionable+conformant specs",
    )

    # Step 2 — best-effort adversarial transcript (never gates). The return
    # value is a real, paid 4-turn bull/bear transcript — captured here (not
    # discarded) and stamped onto every leaderboard entry below, so it
    # survives past this function into persistence (debate_transcripts,
    # #<transcript-capture>). It never gates: build_leaderboard is called
    # below whether this list is empty or not.
    with cost_meter.stage("debate_transcript"):
        transcript = await _debate_round(pool, model, emit, candidate_id, evidence_by_id)

    # Deterministic per-paper tally over the transcript we just paid for — 0
    # extra tokens, no extra call. This is the readable answer to "which of the
    # retrieved papers did the debate actually engage with", and it is stamped
    # onto every leaderboard entry below alongside the transcript itself.
    # Gated on `transcript`, not on the evidence map: with no backend the
    # debate never ran, and a table of all-"unused" rows would read as "the
    # researchers looked at 30 papers and engaged with none" rather than as
    # the honest absence it is.
    paper_verdicts = _aggregate_paper_verdicts(transcript, evidence_by_id) if transcript else []
    if paper_verdicts:
        engaged = sum(1 for v in paper_verdicts if v["verdict"] != "unused")
        await emit.emit(
            "tool_result",
            candidate_id=candidate_id,
            tool_name="debate_paper_verdicts",
            result_summary=f"{engaged} of {len(paper_verdicts)} retrieved paper(s) cited or discarded by name",
        )

    # Step 3a — C-prov (non-votable, Xia 1/2/4): cull candidates citing outside the
    # embargo+decay surface. Does NOT change pool_size (the DSR denominator counts
    # every conformant spec we proposed; §5c) — only which survivors reach C-rigor.
    prov_clean, prov_dropped = _critic_prov(pool, corpus)
    if prov_dropped:
        await emit.emit(
            "tool_result",
            candidate_id=candidate_id,
            tool_name="critic_prov",
            result_summary=f"dropped {len(prov_dropped)} candidate(s) citing outside the embargo surface",
        )
    if not prov_clean:
        raise DebateUnavailable("debate: all candidates failed provenance (cited outside the embargo+decay surface)")

    # Step 3b — C-rigor: num_trials = _society_num_trials(pool_size) = the strategy's
    # OWN candidate pool (N), NOT N + library_size. A strategy's rigor depends only on
    # itself, never on the library it joins (decouple #2, reverses #770/#811/#820 —
    # needs Önder's sign-off). pool_size (the full conformant proposed count) is the
    # selection set; C-prov only culls which survivors are backtested.
    num_trials = _society_num_trials(pool_size)
    await emit.emit("agent_iteration", candidate_id=candidate_id, iteration_n=2, max_iterations=4)
    await emit.emit(
        "tool_called",
        candidate_id=candidate_id,
        tool_name="evaluate_fusion_spec",
        args_summary=f"backtest ×{len(prov_clean)}, num_trials={num_trials} (own pool, decouple #2)",
    )
    with cost_meter.stage("debate_backtest"):
        rigor_results = await _critic_rigor(prov_clean, num_trials)
    if not rigor_results:
        raise DebateUnavailable("debate: no candidate produced a successful backtest")

    # Step 4 — C-regime (non-votable Hierarchy-of-Truth): read the live regime.
    regime_gate = await asyncio.to_thread(_critic_regime)
    await emit.emit(
        "tool_result",
        candidate_id=candidate_id,
        tool_name="critic_regime",
        result_summary=regime_gate["reason"],
    )

    # Step 5 — C-null cull + deterministic synthesize → leaderboard (C-regime gates first).
    await emit.emit("agent_iteration", candidate_id=candidate_id, iteration_n=3, max_iterations=4)
    leaderboard = build_leaderboard(
        rigor_results,
        regime=regime,
        base_id=candidate_id,
        regime_force_abstain=regime_gate["force_abstain"],
        regime_reason=regime_gate["reason"],
        evidence_by_id=evidence_by_id,
    )
    # Stamp the ONE transcript this run produced onto every entry (winner AND
    # the tail Considered Alternatives) — build_leaderboard is pure and knows
    # nothing about the debate, so this is the single attachment point. Every
    # entry shares the same list object: one debate happened, not one per
    # candidate.
    if transcript:
        for entry in leaderboard:
            entry.debate_transcript = transcript
    if paper_verdicts:
        for entry in leaderboard:
            entry.debate_paper_verdicts = paper_verdicts
    leader = leaderboard[0]
    await emit.emit(
        "tool_result",
        candidate_id=candidate_id,
        tool_name="synthesize",
        result_summary=(
            "ABSTAIN — no candidate beat the passive null"
            if leader.generation_method == "debate_abstain"
            else f"leader={leader.strategy_name} dsr={leader.rigor_verdict.get('dsr')} of {len(leaderboard)} entries"
        ),
    )

    # The SAME paper-attribution entry `_persist_debate_transcripts` will append
    # to this run's transcript row (#1739), pushed onto the live stream while
    # the user is still watching instead of only being readable afterwards on
    # the passport. Built by the one shared helper — the summary sentence lives
    # in `_paper_attribution_entry` and nowhere else, or the stream and the
    # passport would drift into two different claims about the same run.
    #
    # Sanitized here for the reason `_emit_debate_turn` documents: the SSE path
    # never reaches `record_debate_transcript`, and both of this entry's new
    # keys carry model prose.
    #
    # Gated on `transcript` for the same reason `paper_verdicts` is above: with
    # no LLM backend the debate never ran, and "the papers accounted for by
    # this debate" is not a thing that exists when there was no debate. The
    # PERSISTED entry keeps its own #1739 rule (it also rides on
    # `fusion_reasoning` alone) — this gate narrows the event, not the row.
    if transcript:
        attribution = _paper_attribution_entry(leader)
        if attribution is not None:
            # Built as ONE dict rather than spread into keyword arguments: a
            # `**sanitized` spread makes every key of the attribution entry a
            # kwarg, so the day `_paper_attribution_entry` grows a
            # `candidate_id` the call dies with "got multiple values for
            # keyword argument". Merging instead lets the entry's own keys land
            # and keeps the three stream-only fields authoritative.
            payload = {
                **sanitize_transcript([attribution])[0],
                "candidate_id": candidate_id,
                "papers_offered": leader.papers_offered,
                "distinct_mechanism_papers": leader.distinct_mechanism_papers,
            }
            await emit.emit("debate_attribution", **payload)
    return leaderboard


async def _run_debate_candidate(**kwargs: Any) -> _CandidateResult:
    """Back-compat wrapper: the leader only (leaderboard[0])."""
    board = await _run_debate_leaderboard(**kwargs)
    return board[0]
