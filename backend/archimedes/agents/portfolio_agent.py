"""Single-turn LLM portfolio constructor.

The rule-based ``StrategySignalEvaluator`` produces per-(strategy × asset)
signals against a hard-coded universe.  This module takes those signals
(plus a global market scan) and asks an LLM, in **one** completion, to
construct the final portfolio — picking specific instruments (including
*individual* stocks and bonds, not just ETFs), justifying each pick, and
anchoring every position to one of our paper-grounded strategies (the
"strategy passport" model from CLAUDE.md § "Architectural primitives").

Two live consumers, and only two:

* ``agents/generation_pipeline.py`` calls :func:`get_portfolio_agent` for
  the ``available`` / ``model_id`` probe behind ``_llm_available()`` and
  ``_served_model_for()``, and constructs a per-job ``PortfolioAgent`` to
  carry a model-pinned backend. It never asks this module to generate a
  strategy — the debate society is the sole generation pipeline
  (``docs/adr/debate-society-sole-generation-pipeline.md``).
* ``evaluation/stockbench/adapter.py`` calls
  :meth:`PortfolioAgent.propose_portfolio` for the benchmark harness.

This is **not** an agent in the tool-use sense. The multi-turn tool loop
that once lived here (``propose_portfolio_with_tools`` and its
``get_asset_stats`` / ``get_correlation`` / ``stress_test_portfolio``
tools) had no callers and was deleted 2026-08-31; nothing in the tree
runs a tool-use loop today.

If no LLM backend is available, ``propose_portfolio`` returns ``None`` and
the caller falls back to rule-based aggregation.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from archimedes.agents.prompts import PROMPTS
from archimedes.services.brief_screen import quote_for_prompt
from archimedes.services.llm_backend import LLMBackend, make_llm_backend
from archimedes.services.strategy_signal_evaluator import GLOBAL_ASSETS

logger = logging.getLogger(__name__)


@dataclass
class AgentPick:
    """A single agent-chosen position."""

    ticker: str  # display symbol (e.g. "NVDA", "USD/TRY", "BIST100")
    synth: str  # internal synth code (e.g. "sNVDA")
    asset_class: str  # us_stock, us_bond_long, fx, crypto, ...
    exchange: str
    weight: float  # 0 - 1, fraction of the *synth budget* (not total)
    paper_anchor: str  # which paper-strategy supports this pick
    reasoning: str  # one-sentence justification


@dataclass
class AgentPortfolio:
    """Full agent output: thesis + picks + the model that served them."""

    thesis: str
    picks: list[AgentPick]
    model_id: str
    served_model: str


# ── Response cache ─────────────────────────────────────────────────
# Avoid hammering the LLM for every advisor request.
_RESPONSE_CACHE: dict[str, tuple[AgentPortfolio, float]] = {}
_CACHE_TTL_SEC = 300  # 5 minutes


def _cache_key(regime: str, risk_profile: str, top_synths: tuple[str, ...]) -> str:
    return f"{regime}|{risk_profile}|{','.join(sorted(top_synths))}"


def _build_system_prompt() -> str:
    # The template lives in the prompt registry (`agents/prompts.py`), rendered
    # into `docs/specs/prompt-inventory.md` under a drift test (#1800).
    return PROMPTS["portfolio.construction.system"].text


def _format_universe(scan_universe_synths: set[str]) -> str:
    """Render the available-universe table by asset class."""
    by_class: dict[str, list[tuple[str, str, str]]] = {}
    for synth, (_yf, display, asset_class, exchange) in GLOBAL_ASSETS.items():
        by_class.setdefault(asset_class, []).append((synth, display, exchange))

    parts: list[str] = []
    for asset_class in sorted(by_class):
        rows = by_class[asset_class]
        names = ", ".join(f"{d}" + ("*" if s in scan_universe_synths else "") for s, d, _e in sorted(rows))
        parts.append(f"  - {asset_class:20s}: {names}")
    return "\n".join(parts)


def _format_market_scan(market_ranking: list[dict]) -> str:
    """Format the top-ranked market opportunities."""
    if not market_ranking:
        return "  (none — live data unavailable)"
    lines: list[str] = []
    for r in market_ranking:
        lines.append(
            f"  {r['display']:12s} {r['asset_class']:18s} "
            f"score={r['score']:+6.2f}  90d_mom={r['momentum_90d'] * 100:+6.1f}%  "
            f"vol={r['vol_ann'] * 100:5.1f}%  [{r['exchange']}]"
        )
    return "\n".join(lines)


def _format_strategies(strategies: list[Any], rigor_statuses: dict[str, str] | None = None) -> str:
    """Render paper strategies with their backtest stats + signal rules.

    ``rigor_statuses`` maps strategy id → LIVE four-state gate status
    ("pass" | "fail" | "pending" | "degenerate", #1184), computed by the caller
    (the advisor route
    runs the same memoized live-gate batch that serves the library badge).
    ``Strategy.passes_rigor_gate`` on the in-memory object is a fail-closed
    sentinel (always ``False``, #821) and must never be presented to the LLM
    as a verdict — before this parameter existed, every curated strategy was
    labeled ``candidate``, so the model was systematically told the whole
    library had failed rigor. A missing map or id renders ``pending``:
    "verdict not computed" is honest; "candidate"/"fail" from a sentinel is not.
    """
    rule_summary = {
        "faber": "long when price > 200-day SMA, else flat",
        "moreira": "scale exposure by target_vol / realized_vol (vol-managed)",
        "moskowitz": "long if trailing 12m return > 0 (time-series momentum)",
        "george_hwang": "long when within 5% of 52-week high",
        "capital_preservation": "always long the asset (T-bill proxy)",
        "buy_hold": "always long the asset",
    }

    def _summarize_rule(strategy: Any) -> str:
        path = (strategy.strategy_code_path or "").lower()
        title = (strategy.paper_title or "").lower()
        for key, rule in rule_summary.items():
            if key in path or key in title:
                return rule
        return "see paper"

    _RIGOR_LABELS = {
        "pass": "PASS (live gate)",
        "fail": "FAIL (live gate)",
        "pending": "pending (no live verdict)",
        # #1184: without this entry a degenerate (zero-variance) strategy would
        # fall through the .get() default below and be told to the LLM as
        # "pending (no live verdict)" — the exact conflation this issue exists
        # to fix, just one layer up (advisor context instead of the API badge).
        "degenerate": "DEGENERATE (zero-variance data, not a real evaluation)",
    }

    lines: list[str] = []
    for s in strategies:
        sr = s.real_sharpe if s.real_sharpe is not None else float("nan")
        cagr = (s.real_cagr or 0.0) * 100
        rule = _summarize_rule(s)
        status = (rigor_statuses or {}).get(s.id, "pending")
        rigor_label = _RIGOR_LABELS.get(status, "pending (no live verdict)")
        # `paper_title` is third-party arXiv metadata landing raw in a
        # line-oriented prompt: the very next line of this same entry carries
        # `sharpe=` and `rigor=`, so an unquoted title containing a newline
        # writes a metric onto the agent's own evidence line. `quote_for_prompt`
        # (#1801) screens the title and returns it as ONE quoted JSON token; a
        # refused title leaves the field empty rather than being rewritten, and
        # the id — which is what the agent must anchor every pick to — is
        # unaffected either way.
        quoted_title, _ = quote_for_prompt(
            str(s.paper_title or ""), field="paper_title", context=f"strategy {s.id[:8]}"
        )
        lines.append(
            f"  - id={s.id[:8]}  title={quoted_title}\n"
            f"      sharpe={sr:.2f}  cagr={cagr:+.1f}%  rigor={rigor_label}\n"
            f"      signal rule: {rule}"
        )
    return "\n".join(lines)


def _build_user_prompt(
    regime: str,
    regime_confidence: float,
    risk_profile: str,
    usdc_floor: float,
    synth_budget: float,
    market_ranking: list[dict],
    strategies: list[Any],
    scan_universe_synths: set[str],
    rigor_statuses: dict[str, str] | None = None,
) -> str:
    # Percentages are formatted HERE, not in the template: string.Template has no
    # format specs, so `{x:.0%}` becomes an explicit `f"{x:.0%}"` at the seam.
    return PROMPTS["portfolio.construction.user"].render(
        regime=regime,
        regime_confidence=f"{regime_confidence:.0%}",
        risk_profile=risk_profile,
        usdc_floor=f"{usdc_floor:.0%}",
        synth_budget=f"{synth_budget:.0%}",
        market_scan=_format_market_scan(market_ranking),
        strategies=_format_strategies(strategies, rigor_statuses),
        universe=_format_universe(scan_universe_synths),
    )


# ── Parsing helpers ────────────────────────────────────────────────


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of an LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Greedy {...} match as last resort
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _resolve_ticker(ticker: str) -> tuple[str, tuple[str, str, str, str]] | None:
    """Look up an LLM-provided ticker in GLOBAL_ASSETS.

    The LLM is told to use display symbols; this also accepts synth
    codes (sNVDA) and yfinance tickers (NVDA, BRK-B, XU100.IS) as
    fallbacks.
    """
    if ticker in GLOBAL_ASSETS:
        return ticker, GLOBAL_ASSETS[ticker]
    for synth, entry in GLOBAL_ASSETS.items():
        yf_t, display, _ac, _ex = entry
        if ticker in (display, yf_t):
            return synth, entry
    norm = ticker.upper().replace(" ", "")
    for synth, entry in GLOBAL_ASSETS.items():
        yf_t, display, _ac, _ex = entry
        if norm in (display.upper(), yf_t.upper()):
            return synth, entry
    return None


# ── PortfolioAgent ─────────────────────────────────────────────────


class PortfolioAgent:
    """LLM-driven portfolio constructor.

    Construct once at process startup; the held LLM backend caches its
    own client.  Method ``propose_portfolio`` is sync but the caller
    runs it in a thread (the LLM call blocks on the network).
    """

    def __init__(self, backend: LLMBackend | None = None) -> None:
        self._backend: LLMBackend = backend or make_llm_backend()

    @property
    def available(self) -> bool:
        return getattr(self._backend, "available", False)

    @property
    def model_id(self) -> str:
        return getattr(self._backend, "model_id", "unknown")

    def propose_portfolio(
        self,
        regime: str,
        regime_confidence: float,
        risk_profile: str,
        usdc_floor: float,
        synth_budget: float,
        market_ranking: list[dict],
        strategies: list[Any],
        scan_universe_synths: set[str],
        rigor_statuses: dict[str, str] | None = None,
    ) -> AgentPortfolio | None:
        if not self.available:
            logger.info("PortfolioAgent unavailable — no LLM backend configured")
            return None

        top_synth_codes = tuple(r["synth"] for r in market_ranking)
        cache_key = _cache_key(regime, risk_profile, top_synth_codes)
        cached = _RESPONSE_CACHE.get(cache_key)
        if cached and (time.time() - cached[1]) < _CACHE_TTL_SEC:
            return cached[0]

        system = _build_system_prompt()
        user = _build_user_prompt(
            regime,
            regime_confidence,
            risk_profile,
            usdc_floor,
            synth_budget,
            market_ranking,
            strategies,
            scan_universe_synths,
            rigor_statuses,
        )

        try:
            raw = self._backend.complete(system=system, user=user)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("PortfolioAgent LLM call failed: %s", e)
            return None

        data = _extract_json(raw)
        if not data or not isinstance(data, dict):
            logger.warning("PortfolioAgent: could not parse JSON from response: %r", raw[:300])
            return None

        thesis = str(data.get("thesis", "")).strip()
        raw_picks = data.get("picks") or []
        if not isinstance(raw_picks, list):
            logger.warning("PortfolioAgent: picks not a list")
            return None

        picks: list[AgentPick] = []
        for entry in raw_picks:
            if not isinstance(entry, dict):
                continue
            ticker = str(entry.get("ticker", "")).strip()
            if not ticker:
                continue
            resolved = _resolve_ticker(ticker)
            if not resolved:
                logger.info("PortfolioAgent: unknown ticker %r — skipping", ticker)
                continue
            synth, (_yf, display, asset_class, exchange) = resolved
            try:
                weight = float(entry.get("weight", 0.0))
            except (TypeError, ValueError):
                continue
            if weight <= 0:
                continue
            weight = min(weight, 0.20)  # enforce per-asset cap
            picks.append(
                AgentPick(
                    ticker=display,
                    synth=synth,
                    asset_class=asset_class,
                    exchange=exchange,
                    weight=weight,
                    paper_anchor=str(entry.get("paper_anchor", "")).strip(),
                    reasoning=str(entry.get("reasoning", "")).strip(),
                )
            )

        if not picks:
            logger.warning("PortfolioAgent: parsed zero valid picks from LLM response")
            return None

        # Normalize weights to the synth budget
        total = sum(p.weight for p in picks)
        if total > 0:
            for p in picks:
                p.weight = round(p.weight / total * synth_budget, 4)

        portfolio = AgentPortfolio(
            thesis=thesis or f"{regime} regime, {risk_profile} risk: diversified across asset classes",
            picks=picks,
            model_id=self.model_id,
            served_model=getattr(self._backend, "served_model", self.model_id),
        )
        _RESPONSE_CACHE[cache_key] = (portfolio, time.time())
        return portfolio


# Singleton — constructed lazily to honor env vars set after import.
_AGENT: PortfolioAgent | None = None


def get_portfolio_agent() -> PortfolioAgent:
    global _AGENT  # pylint: disable=global-statement
    if _AGENT is None:
        _AGENT = PortfolioAgent()
    return _AGENT
