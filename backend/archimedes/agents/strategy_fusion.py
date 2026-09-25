"""Strategy fusion — multi-paper, user-steered, novelty-seeking synthesis.

A primitive that originally sat *beside* the interactive Strategy Architect
(retired — issue #1064; the debate society is now the sole strategy-generation
path), behind a feature flag that was itself retired on 2026-09-02 (deck Q4). The architect used to select + weight pre-curated
single-paper library strategies (the verified-library path that fed the
strategy-passport / reasoning-trace data flow). Fusion does the
opposite-direction thing: synthesizes a *new* strategy hypothesis by fusing
>=2 raw arXiv q-fin papers, steered by the user, optimizing for novelty
(McLean & Pontiff 2016: published alpha decays — the un-decayed edge is
combinations not yet in the literature).

Why a separate module (owner-decided HARD constraint):
- The construction-trace path is contract-review-grade (the live
  `ReasoningTraceRegistry`). Fusion started additive and revertible by
  deleting this file + its spec. Nothing in the audited flow is touched.
- Fusion is now UNCONDITIONAL. `ARCHIMEDES_FUSION_ENABLED` was retired on
  2026-09-02: the debate society is the sole generation pipeline and every
  proposer routes through `StrategyFusion.propose()`, so the only thing the
  flag's OFF branch could do in production was return a `disabled` sentinel
  and make Generate silently produce nothing. A lever that can only break
  prod is not a lever. Do not reintroduce a switch here —
  `backend/tests/test_fusion_flag_retired.py` fails if one comes back.
- The LLM-backend seam, lazy `anthropic` import, `extract_json` (now in
  `agents/generation_json.py`), frozen artifact and honest-fallback
  labelling deliberately mirrored the (now-retired) architect so a later
  route-wiring was a small, familiar diff.

True-model honesty: our backend is routed through a GLM-backed,
Anthropic-compatible endpoint. `messages.create(model=...)` gets the
*configured* string, but `response.model` is the model that actually served
the request (e.g. `glm-4.7`). The proposal records `response.model` as the
provenance field of record and keeps the configured/requested string
separately. See `docs/specs/strategy-fusion-spec.md`.

References:
- `docs/specs/strategy-fusion-spec.md` — the design this implements
- `backend/archimedes/agents/generation_json.py` — the shared `extract_json` seam
- `backend/archimedes/services/strategy_provider.py` — env-override precedent
- `backend/archimedes/models/portfolio.py` — RiskProfile, RISK_PROFILE_PARAMS
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from archimedes.agents.generation_json import extract_json
from archimedes.agents.prompts import PROMPTS
from archimedes.models.portfolio import RISK_PROFILE_PARAMS, RiskProfile
from archimedes.services.llm_backend import LLMBackend, make_llm_backend
from archimedes.services.strategy_dsl import DSLError, validate_strategy_spec
from archimedes.services.strategy_signal_evaluator import GLOBAL_ASSETS

logger = logging.getLogger(__name__)

# Serializes load_corpus's DB/file branch. See the comment inside load_corpus:
# concurrent full-corpus ORM loads abort the interpreter (#1632, prod rev 214).
_CORPUS_LOAD_LOCK = threading.Lock()

# ── The three paper knobs (#1636) ───────────────────────────────────────────
#
# These used to be ONE knob wearing three hats, which is why a generated
# strategy cited two papers almost every time: `max_papers` was retrieval
# width AND the fusion budget AND (via the `>= 2` gate) the definition of a
# successful fusion. Split apart:
#
#   MIN_PAPERS        — the HARD validity floor. A one-paper "fusion" is just
#                       extraction, so this rejects. It is deliberately NOT
#                       raised to 5: raising it converts a thin corpus into a
#                       GENERATION_UNAVAILABLE instead of into an honest,
#                       narrower strategy.
#   FUSE_TARGET_MIN   — what we ASK the model to fuse when that many papers
#                       are actually on the table. Never enforced as a reject:
#                       a shortfall is justified (in `fusion_reasoning`) and
#                       logged, never blocked. Padding the citation list with a
#                       paper whose mechanism the model cannot name launders
#                       weak evidence into the provenance record and the
#                       passport, where citation count reads as evidence depth
#                       — that is strictly worse than an honest 2.
#   FUSION_MAX_PAPERS — retrieval width + prompt budget. `max_papers` clamps
#                       into [MIN_PAPERS, FUSION_MAX_PAPERS].
MIN_PAPERS = 2
FUSE_TARGET_MIN = 5
FUSION_MAX_PAPERS = 30

# The one default every entry point uses. It is deliberately > FUSE_TARGET_MIN:
# a brief that offers the model exactly as many papers as we ask it to cite is
# an invitation to pad, because rejecting even one paper is then automatically
# a shortfall. Above the target there is room to reject honestly.
#
# It is deliberately NOT FUSION_MAX_PAPERS either: at ~300 input tokens/paper,
# 30 papers × the debate's default 10 proposer steers is ~90k input tokens of
# evidence per generation before a single candidate is backtested. 30 stays
# available as an explicit user pick; nobody is defaulted into it.
DEFAULT_MAX_PAPERS = 8

# ── Asset-class synonym map (deterministic candidate filtering) ──
#
# Lowercased substring match against primary_category + categories + title +
# abstract. Intentionally simple and reviewable rather than embedding-based;
# a SPECTER2 ranker is a clean post-hackathon swap behind this same seam.
_ASSET_SYNONYMS: dict[str, tuple[str, ...]] = {
    "equities": ("equit", "stock", "share", "q-fin.pm", "cross-section"),
    "rates": ("rate", "bond", "treasury", "yield curve", "fixed income", "duration"),
    "credit": ("credit", "default", "cds", "spread", "bankruptcy"),
    "fx": ("fx", "currency", "exchange rate", "carry trade"),
    "commodities": ("commodit", "oil", "energy", "metal", "gold", "futures"),
    "crypto": ("crypto", "bitcoin", "blockchain", "defi", "token"),
    "vol": ("volatil", "variance", "option", "vix", "implied vol"),
    "macro": ("macro", "regime", "business cycle", "monetary", "inflation"),
}

# ── Investable-universe SSOT (issue #682 derived) ───────────────────────────
#
# The fusion strategy_spec's `asset_universe` MUST be steered by the user's
# selected assets — never a hardcoded `["SPY"]` literal. `GLOBAL_ASSETS`
# (backend-local, hermetic, importable without the analytics-engine package) is
# the single source of truth for the supported instruments; its display symbols
# are the user-facing tickers the UI picker exposes. When the user gives no
# steer, we fall back to this full SSOT-derived universe rather than to SPY.
SUPPORTED_UNIVERSE: tuple[str, ...] = tuple(
    # Display symbol (e.g. "SPY", "QQQ", "GOLD_FUT") — the user-facing label.
    sorted({display for (_yf, display, _asset_class, _exchange) in GLOBAL_ASSETS.values()})
)
# Case-folded membership index: accept either a display symbol ("SPY") or the
# synth key ("sSPY") the user / UI might send, mapping both to the canonical
# display symbol used in the strategy_spec.
_UNIVERSE_LOOKUP: dict[str, str] = {}
# Display symbol -> SSOT asset_class tag (e.g. "BTC" -> "crypto"). Used by
# `_unrepresented_asset_classes` (#892) to recognize that a classified class
# name IS represented when the universe already contains a ticker of that
# SSOT class — even where the class name has no entry in
# `_ASSET_CLASS_PROXIES` below (e.g. "crypto": BTC/ETH are named directly by
# the model, so the class itself needs no proxy-ticker injection, but it
# should still read as covered rather than a false-positive gap).
_DISPLAY_TO_ASSET_CLASS: dict[str, str] = {}
for _synth, (_yf, _display, _ac, _exch) in GLOBAL_ASSETS.items():
    _UNIVERSE_LOOKUP[_display.casefold()] = _display
    _UNIVERSE_LOOKUP[_synth.casefold()] = _display
    _UNIVERSE_LOOKUP[_yf.casefold()] = _display
    _DISPLAY_TO_ASSET_CLASS[_display.casefold()] = _ac

# ── Asset-CLASS (not ticker) → representative SSOT proxies (#892) ──────────
#
# `brief.asset_classes` can carry a coarse class NAME (e.g. "treasuries") from
# either an explicit user steer or the LLM brief-validator's
# `asset_classes_inferred` — never a ticker. `_UNIVERSE_LOOKUP` above only
# indexes concrete display/synth/yfinance symbols, so a class name silently
# resolved to nothing and the leg vanished from the universe with no signal
# (issue #892: "treasuries" requested + correctly classified, but every
# candidate traded crypto only). This map gives each class name a small,
# deterministic set of representative tickers actually present in
# `GLOBAL_ASSETS`, so a classified class with real data-source coverage is
# never dropped just because the caller said the class name instead of a
# ticker.
#
# Deliberately DISJOINT from `_ASSET_SYNONYMS`' keys (equities/rates/credit/
# fx/commodities/crypto/vol/macro): those are established, test-pinned as
# PAPER-RETRIEVAL-ONLY class filters that intentionally do NOT resolve to a
# concrete universe (`derive_asset_universe`'s docstring: broad-class steers
# "are dropped from the *universe*"; see test_universe_falls_back_to_ssot_
# when_no_instrument_steer et al. in test_strategy_fusion.py, which assert
# `["equities", "rates"]` falls through to the model/full branch). Overloading
# those same keys here would silently flip that established behavior. Instead
# this map only covers class names that (a) have no existing universe
# resolution path at all today and (b) are what the brief-validator / a user
# steer realistically emits for a class the SSOT actually has tickers for —
# "treasuries" (this issue's exact reproduction) plus its close synonyms.
_ASSET_CLASS_PROXIES: dict[str, tuple[str, ...]] = {
    "treasuries": ("IEF", "SHY", "TLT"),
    "treasury": ("IEF", "SHY", "TLT"),
    "treasury_bonds": ("IEF", "SHY", "TLT"),
    "us_treasuries": ("IEF", "SHY", "TLT"),
    "bonds": ("IEF", "SHY", "TLT", "AGG"),
    "bond": ("IEF", "SHY", "TLT", "AGG"),
    "fixed_income": ("IEF", "SHY", "TLT", "AGG"),
    "fixed income": ("IEF", "SHY", "TLT", "AGG"),
    "govt_bonds": ("IEF", "SHY", "TLT"),
    "government_bonds": ("IEF", "SHY", "TLT"),
    "munis": ("MUB",),
    "municipal_bonds": ("MUB",),
    "reits": ("VNQ",),
    "real_estate": ("VNQ",),
    "real estate": ("VNQ",),
}


def _class_proxy_tickers(class_name: str) -> list[str]:
    """Representative SSOT tickers for a class NAME, filtered to what's actually
    registered in ``GLOBAL_ASSETS`` (defensive — the literal map above is hand
    maintained and could drift from the SSOT)."""
    candidates = _ASSET_CLASS_PROXIES.get(class_name.strip().lower(), ())
    return [_UNIVERSE_LOOKUP[c.casefold()] for c in candidates if c.casefold() in _UNIVERSE_LOOKUP]


def _repair_spec(backend: Any, brief: FusionBrief, parsed: dict[str, Any]) -> dict[str, Any] | None:
    """One bounded retry when the model omitted ``strategy_spec``.

    Sends the already-accepted proposal back and asks for ONLY the spec JSON.
    Tolerates a ``{"strategy_spec": {...}}`` wrapper. Returns None (text-only
    fallback, honest pre-backtest verdict) if the retry also fails — never
    loops, never fabricates.
    """
    try:
        user_msg = json.dumps(
            {
                "strategy_name": parsed.get("strategy_name"),
                "thesis": parsed.get("thesis"),
                "fusion_reasoning": parsed.get("fusion_reasoning"),
                "source_arxiv_ids": parsed.get("source_arxiv_ids", []),
                "user_steer": {"asset_classes": brief.asset_classes},
            }
        )
        raw = backend.complete(_SPEC_REPAIR_SYSTEM, user_msg)
        repaired = extract_json(raw)
        if isinstance(repaired, dict) and isinstance(repaired.get("strategy_spec"), dict):
            repaired = repaired["strategy_spec"]
        if isinstance(repaired, dict) and repaired.get("entry") and repaired.get("exit"):
            logger.info("fusion: strategy_spec repaired on retry (model omitted it)")
            return repaired
        logger.warning("fusion: spec-repair retry returned no usable spec — falling back to text-only")
    except Exception as exc:
        logger.warning("fusion: spec-repair retry failed (%s) — falling back to text-only", exc)
    return None


def _resolve_user_assets(selected_assets: list[str]) -> list[str]:
    """Resolve user tokens to canonical SSOT display symbols (may be empty).

    Concrete-ticker resolution ONLY (exact SSOT display/synth/yfinance match).
    Coarse asset-CLASS names (e.g. "equities", "treasuries") deliberately do
    NOT resolve here — that's long-established behavior
    (``derive_asset_universe``'s docstring: broad-class steers "are dropped
    from the *universe*") that several tests pin. Class-name → proxy-ticker
    resolution for #892 lives in ``_class_proxy_tickers`` / ``_gap_fill_tickers``
    below and is applied as a supplement, never a same-function override, so it
    can never silently clobber a concrete user/model ticker pick.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    for token in selected_assets or []:
        canonical = _UNIVERSE_LOOKUP.get(str(token).strip().casefold())
        if canonical and canonical not in seen:
            seen.add(canonical)
            resolved.append(canonical)
    return resolved


def _class_represented_by_ssot_tag(class_name: str, universe: list[str]) -> bool:
    """True iff ``universe`` already contains a ticker whose SSOT ``asset_class``
    tag matches ``class_name`` (#892: e.g. "crypto" is represented once BTC/ETH
    — SSOT tag "crypto" — are in the universe, even with no
    ``_ASSET_CLASS_PROXIES`` entry, since the model/user already named concrete
    tickers for it).

    Reuses ``_ASSET_SYNONYMS`` (the existing class-name -> keyword-stem map) so
    "equities" also recognizes tags like "us_equity_etf" via its "equit" stem,
    not just a literal "equities" substring — the same vocabulary the paper
    filter already trusts for this class name. Falls back to a direct
    substring match (both directions) for class names outside that map (e.g.
    "treasuries", which isn't an ``_ASSET_SYNONYMS`` key).
    """
    key = class_name.strip().lower()
    if not key:
        return False
    stems = _ASSET_SYNONYMS.get(key, (key,))
    for sym in universe:
        tag = _DISPLAY_TO_ASSET_CLASS.get(sym.casefold(), "")
        if not tag:
            continue
        tag_words = tag.replace("_", " ")
        if any(stem in tag_words for stem in stems) or key in tag_words or tag_words in key:
            return True
    return False


def _unrepresented_asset_classes(asset_classes: list[str], universe: list[str]) -> list[str]:
    """Which classified ``asset_classes`` have ZERO representation in ``universe``.

    #892: the honest-surfacing half of the fix. A class name is "represented"
    if any of: (1) it's itself a concrete ticker already in the universe; (2)
    its ``_ASSET_CLASS_PROXIES`` proxy tickers are present; (3) the universe
    already contains a ticker whose SSOT ``asset_class`` tag matches the name
    (covers classes like "crypto" that the model names directly with no proxy
    map entry needed). A class matching none of these is reported as a gap —
    either there is no data source for it, or the caller's classification is a
    broad paper-retrieval-only filter (e.g. "equities"/"rates") we have no
    independent universe evidence for. Order-preserving, de-duped,
    case-insensitive.
    """
    universe_set = {u.casefold() for u in universe}
    gaps: list[str] = []
    seen: set[str] = set()
    for ac in asset_classes or []:
        name = str(ac).strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        # Already a concrete ticker that made it in? Not a gap.
        canonical = _UNIVERSE_LOOKUP.get(name.casefold())
        if canonical and canonical.casefold() in universe_set:
            continue
        proxies = _class_proxy_tickers(name)
        if proxies and any(p.casefold() in universe_set for p in proxies):
            continue
        if _class_represented_by_ssot_tag(name, universe):
            continue
        gaps.append(name)
    return gaps


def _gap_fill_tickers(asset_classes: list[str], universe: list[str]) -> list[str]:
    """Proxy tickers for any classified class that ``_ASSET_CLASS_PROXIES`` covers
    but that has zero representation in ``universe`` yet (#892).

    This is an ADDITIVE supplement, never a replacement: it only fills in a
    class the caller explicitly named (e.g. "treasuries") that a concrete user
    ticker pick or the model's own universe failed to cover — it never removes
    or overrides anything the user/model already chose (so a "crypto +
    treasuries" brief keeps the model's BTC/ETH AND gains the treasury proxy,
    rather than one leg clobbering the other). Order-preserving, de-duped
    against what's already present.

    A class with ANY of its proxy tickers already in ``universe`` (e.g. "IEF"
    present for "treasuries") counts as already represented — a gap-fill tops
    up a *missing* leg, it does not pad out a partially-present one, so no
    further proxies are injected once at least one is present (mirrors the
    "already represented" check in ``_unrepresented_asset_classes`` above;
    Copilot review comment on PR #1033).
    """
    universe_set = {u.casefold() for u in universe}
    fill: list[str] = []
    seen: set[str] = set(universe_set)
    for ac in asset_classes or []:
        name = str(ac).strip()
        if not name or name.casefold() in universe_set:
            continue  # already a concrete ticker present in the universe
        proxies = _class_proxy_tickers(name)
        if any(p.casefold() in universe_set for p in proxies):
            continue  # class already has at least one proxy represented
        for proxy in proxies:
            if proxy.casefold() not in seen:
                seen.add(proxy.casefold())
                fill.append(proxy)
    return fill


_MODEL_UNIVERSE_CAP = 8


def _spec_universe(brief: FusionBrief, strategy_spec: dict[str, Any]) -> tuple[list[str], str, list[str]]:
    """Pick the spec's asset universe: user steer > model suggestion > full SSOT.

    Fixes #847: the old unconditional ``derive_asset_universe`` override sent
    every UNSTEERED brief to the full ~300-asset supported universe, so the
    real-data backtest graded an arbitrary alphabetical basket unrelated to the
    thesis. Order now: (1) the user's resolved assets always win (their lever);
    (2) else the MODEL's emitted universe, validated against the SSOT and
    capped at ``_MODEL_UNIVERSE_CAP`` (the thesis's own instruments); (3) else
    the full supported universe (preserving #682's "never a hardcoded SPY"
    floor).

    #892 gap-fill: whichever branch above fires, any ``brief.asset_classes``
    entry that has a known proxy (``_ASSET_CLASS_PROXIES``) but zero
    representation in that branch's universe gets its proxy tickers appended
    — additively, never replacing what the branch already chose. This is what
    keeps a "trend-following on BTC/ETH with a defensive rotation into
    treasuries" brief from losing the treasury leg just because the model only
    named BTC/ETH explicitly: the user/model branch still wins for the assets
    it *did* address, and the gap-fill covers the classified class it missed.

    Returns ``(universe, universe_source, universe_gaps)``:
      - ``universe_source`` is one of ``"user" | "model" | "full"`` (#857) — a
        model-picked universe is a mild look-ahead channel (the model can pick
        names it already "knows" did well over the window from training
        data), so which branch fired is recorded for the passport rather than
        silently collapsed into just the resulting list. Gap-filling does not
        change which branch is recorded — it is applied on top.
      - ``universe_gaps`` (#892) lists any ``brief.asset_classes`` entry that
        STILL has zero representation after gap-fill — i.e. a classified class
        with no known data-source proxy at all (or, for the broad
        paper-retrieval-only class names like "equities"/"rates" that
        deliberately have no proxy map entry, no independent universe
        evidence). Empty when every classified class is represented. This is
        the claim-integrity signal: the passport/reasoning trace must say "X
        was requested but not available" rather than silently proceeding as
        if X was honored.
    """
    user_assets = _resolve_user_assets(brief.asset_classes)
    if user_assets:
        universe = user_assets + _gap_fill_tickers(brief.asset_classes, user_assets)
        gaps = _unrepresented_asset_classes(brief.asset_classes, universe)
        return universe, "user", gaps
    model_assets = _resolve_user_assets([str(a) for a in strategy_spec.get("asset_universe") or []])
    # Parrot defense: a bare single proxy (the classic ["SPY"] default weak
    # models emit regardless of thesis) is indistinguishable from a non-choice —
    # only a deliberate MULTI-instrument selection is trusted as the thesis's
    # own universe. Single-asset intent is still expressible via the user steer.
    if len(model_assets) >= 2:
        if len(model_assets) > _MODEL_UNIVERSE_CAP:
            logger.info("fusion: model universe capped %d → %d", len(model_assets), _MODEL_UNIVERSE_CAP)
        capped = model_assets[:_MODEL_UNIVERSE_CAP]
        universe = capped + _gap_fill_tickers(brief.asset_classes, capped)
        gaps = _unrepresented_asset_classes(brief.asset_classes, universe)
        return universe, "model", gaps
    full = list(SUPPORTED_UNIVERSE)
    # No user/model steer fired, so the full SSOT is already in play — nothing
    # to gap-fill (every proxy ticker is already a member of "full" by
    # construction); a classified class is only a real gap here if it has no
    # known proxy at all.
    gaps = _unrepresented_asset_classes(brief.asset_classes, full)
    return full, "full", gaps


def derive_asset_universe(selected_assets: list[str]) -> list[str]:
    """Derive the strategy_spec asset_universe from the user's selected assets.

    The universe is the user's chosen instruments (resolved to canonical
    display symbols via the SSOT), de-duped and order-preserved. Tokens that
    don't resolve to a supported instrument (e.g. broad-class steers like
    "equities" the paper filter consumes) are dropped from the *universe* — the
    universe is a concrete instrument list, not a class filter. When nothing in
    the steer resolves, fall back to the full supported universe (issue #682) —
    never a bare `["SPY"]`.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    for token in selected_assets:
        canonical = _UNIVERSE_LOOKUP.get(str(token).strip().casefold())
        if canonical and canonical not in seen:
            seen.add(canonical)
            resolved.append(canonical)
    return resolved if resolved else list(SUPPORTED_UNIVERSE)


# ── Regime-biased keyword sets for bull/bear paper retrieval (Issue #163) ──
_REGIME_BIAS_TERMS: dict[str, tuple[str, ...]] = {
    "bull": (
        "momentum",
        "trend",
        "trend-following",
        "risk-on",
        "carry",
        "growth",
        "breakout",
        "relative strength",
        "cross-section",
        "factor",
        "alpha",
        "long",
        "bull",
        "expansion",
        "recovery",
        "upside",
    ),
    "bear": (
        "volatility",
        "vol-managed",
        "defensive",
        "hedge",
        "tail risk",
        "drawdown",
        "inverse",
        "mean-reversion",
        "safe haven",
        "flight to quality",
        "risk-off",
        "bear",
        "contraction",
        "recession",
        "downside",
        "protection",
        "minimum variance",
        "low volatility",
        "short",
    ),
}


# ── Fusion-specific canned fallback ──────────────────────────────


class FusionCannedBackend:
    """Deterministic offline fallback. Explicitly NOT model reasoning.

    Keeps the path demoable with no API key and gives the parser a stable
    fixture in tests. The text is emphatic that this is a non-novel
    placeholder so it can never masquerade as a real cross-paper synthesis
    in a provenance record.
    """

    model_id = "canned-fusion-fallback"
    served_model = "canned-fusion-fallback"

    @property
    def available(self) -> bool:
        return False

    def complete(self, system: str, user: str) -> str:  # noqa: ARG002 — Protocol-shaped offline placeholder; signature matches live LLM backends
        ids = re.findall(r'"arxiv_id"\s*:\s*"([^"]+)"', user)
        ids = ids[:FUSION_MAX_PAPERS] or ["__none__"]
        return json.dumps(
            {
                "strategy_name": "Offline fusion placeholder",
                "thesis": (
                    "Offline fallback: no LLM backend available, so no genuine "
                    "cross-paper synthesis was performed. Set ANTHROPIC_API_KEY "
                    "or ANTHROPIC_AUTH_TOKEN+ANTHROPIC_BASE_URL for a real, novelty-seeking fusion."
                ),
                "source_arxiv_ids": ids,
                "fusion_reasoning": (
                    "Not model reasoning. Papers are echoed back unfused; this "
                    "is a labelled placeholder, not a novel combination."
                ),
                "novelty_rationale": ("None claimed — a fallback is by definition not novel."),
                "risk_notes": (
                    "Fallback output. Pre-backtest hypothesis only; the "
                    "selection-bias gate (DSR/PBO/OOS/look-ahead) still applies."
                ),
            }
        )


# ── User-steering input ─────────────────────────────────────────


@dataclass
class FusionBrief:
    """The user's steer. Fusion never free-runs the whole corpus.

    `asset_classes` is a required-overlap filter (empty = no asset filter).
    `risk_appetite` shapes the synthesis envelope (RISK_PROFILE_PARAMS), it
    does not hard-filter papers. `strategic_direction` biases ranking and is
    passed verbatim to the prompt. `max_papers` is RETRIEVAL WIDTH (how many
    abstracts the model is shown), clamped to [MIN_PAPERS, FUSION_MAX_PAPERS];
    the >=2 floor is non-negotiable. It is NOT how many papers we ask the
    model to fuse — that is FUSE_TARGET_MIN, and it is a request, not a gate.
    `market_context` carries live regime/market data (3rd input).
    """

    asset_classes: list[str] = field(default_factory=list)
    risk_appetite: RiskProfile | str = RiskProfile.MODERATE
    strategic_direction: str = ""
    max_papers: int = DEFAULT_MAX_PAPERS
    market_context: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_profile(self) -> RiskProfile:
        rp = self.risk_appetite
        return RiskProfile(rp) if isinstance(rp, str) else rp

    @property
    def paper_budget(self) -> int:
        """max_papers clamped into the enforced [MIN_PAPERS, cap] range."""
        return max(MIN_PAPERS, min(FUSION_MAX_PAPERS, int(self.max_papers)))


# ── Corpus manifest (read-only, defensive, not a hard dependency) ──


@dataclass(frozen=True)
class CorpusPaper:
    """One manifest line, reduced to the fields fusion needs."""

    arxiv_id: str
    title: str
    abstract: str
    primary_category: str
    categories: tuple[str, ...]
    published: str

    @property
    def haystack(self) -> str:
        """Lowercased text used for asset-class + direction matching."""
        cats = " ".join(self.categories)
        return f"{self.primary_category} {cats} {self.title} {self.abstract}".lower()


def _manifest_path() -> Path | None:
    """Resolve the corpus manifest. Mirrors default_provider() precedence.

    1. ARCHIMEDES_CORPUS_MANIFEST env override (deployment / tests).
    2. First existing candidate among host + container-plausible layouts.
    Returns None if nothing resolvable — caller degrades, never raises.
    """
    env = os.getenv("ARCHIMEDES_CORPUS_MANIFEST")
    if env:
        p = Path(env)
        return p if p.exists() else None

    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "data" / "corpus" / "manifest.jsonl",  # host repo
        Path("/app/data/corpus/manifest.jsonl"),  # repo-root build context
        Path("/data/corpus/manifest.jsonl"),  # bind-mount at root
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def load_corpus(path: Path | None = None) -> list[CorpusPaper]:
    """Load corpus papers — DB-backed, with file-based fallback.

    Precedence:

    1. **An explicit ``path`` is authoritative.** That file, and nothing else:
       the DB is not consulted at all, and a ``path`` that does not exist yields
       an empty corpus rather than silently substituting another source. A
       caller who names a manifest is answering "which papers", not offering a
       hint (issue #1640 — the argument used to be discarded whenever the
       ``papers`` table happened to be non-empty, which made the result depend
       on ambient database state the caller never asked about).
    2. With no ``path``: the DB first — every production caller takes this
       branch, and it is the source of record post-#1240 (seeded from the
       manifest, then extended by arXiv intake, then embargo- and decay-filtered
       by ``load_papers_from_db``).
    3. Still no ``path`` and an empty/unavailable DB: the file fallback resolved
       by ``_manifest_path()``, which honours ``ARCHIMEDES_CORPUS_MANIFEST``.
       Backward-compat for local dev without DB seeding.

    Note what rule 1 does *not* say: ``ARCHIMEDES_CORPUS_MANIFEST`` is not a
    DB bypass. It names where the *file fallback* reads from — production sets
    it (``infra/ecs.tf``, ``docker-compose.yml``) while still wanting the DB —
    so it is consulted only once step 2 has come up empty.
    """
    if path is not None:
        return _load_corpus_from_file(path)

    # Serialized: two threads running this branch CONCURRENTLY is the #1632
    # abort. Prod rev 214 died with two executor threads both inside
    # load_papers_from_db's session teardown (SQLAlchemy _detach_states /
    # InstanceState._cleanup), piled up by abandoned /health corpus probes on a
    # cold task. The lock makes the race unrepresentable for every caller —
    # generation, warmers, anything — not just the probe path (which no longer
    # loads at all; /health reads count_corpus_papers instead). The cost is a
    # waiting thread, which is exactly the safe outcome: the interpreter never
    # dies from waiting.
    with _CORPUS_LOAD_LOCK:
        # DB path first
        try:
            from archimedes.services.corpus_service import load_papers_from_db

            db_rows = load_papers_from_db()
            if db_rows:
                papers = [
                    CorpusPaper(
                        arxiv_id=r["arxiv_id"],
                        title=r["title"],
                        abstract=r["abstract"],
                        primary_category=r.get("primary_category", ""),
                        categories=tuple(r.get("categories", [])),
                        published=r.get("published", ""),
                    )
                    for r in db_rows
                    if r.get("arxiv_id") and (r.get("title") or r.get("abstract"))
                ]
                logger.info("fusion: loaded %d corpus papers from DB", len(papers))
                return papers
        except Exception as exc:
            logger.debug("fusion: DB corpus load failed, falling back to file: %s", exc)

        # File fallback
        return _load_corpus_from_file(path)


def _load_corpus_from_file(path: Path | None = None) -> list[CorpusPaper]:
    """Legacy file-based manifest load (backward-compat fallback)."""
    path = path or _manifest_path()
    if path is None or not path.exists():
        logger.info("fusion: no corpus manifest resolvable; empty corpus")
        return []

    papers: list[CorpusPaper] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("fusion: cannot read manifest %s: %s", path, exc)
        return []

    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.debug("fusion: skip manifest line %d (bad JSON): %s", lineno, exc)
            continue
        arxiv_id = str(obj.get("arxiv_id", "")).strip()
        title = str(obj.get("title", "")).strip()
        abstract = str(obj.get("abstract", "")).strip()
        if not arxiv_id or not (title or abstract):
            logger.debug("fusion: skip manifest line %d (missing core fields)", lineno)
            continue
        cats = obj.get("categories") or []
        papers.append(
            CorpusPaper(
                arxiv_id=arxiv_id,
                title=title,
                abstract=abstract,
                primary_category=str(obj.get("primary_category", "")).strip(),
                categories=tuple(str(c) for c in cats if isinstance(c, str)),
                published=str(obj.get("published", "")).strip(),
            )
        )
    logger.info("fusion: loaded %d corpus papers from file %s", len(papers), path)
    return papers


# ── Deterministic pre-LLM candidate selection ───────────────────


def _asset_terms(asset_classes: list[str]) -> list[str]:
    """Expand requested asset classes through the synonym map (+ raw term)."""
    terms: list[str] = []
    for ac in asset_classes:
        key = ac.strip().lower()
        if not key:
            continue
        terms.append(key)
        terms.extend(_ASSET_SYNONYMS.get(key, ()))
    return terms


def select_candidates(
    brief: FusionBrief,
    corpus: list[CorpusPaper],
    regime_bias: str | None = None,
) -> list[CorpusPaper]:
    """The papers only — see :func:`select_candidates_scored` for the scores.

    Kept as the module's primary entry point (every existing caller wants the
    papers). It drops the rerank float; a caller that needs to cut at a
    similarity floor rather than at a rank uses the scored variant.
    """
    return [p for p, _score in select_candidates_scored(brief, corpus, regime_bias)]


def select_candidates_scored(
    brief: FusionBrief,
    corpus: list[CorpusPaper],
    regime_bias: str | None = None,
) -> list[tuple[CorpusPaper, float | None]]:
    """Deterministic, explainable, pre-LLM. The model never widens this set.

    1. Asset-class overlap filter (skipped if no asset_classes given).
    2. Rank by strategic_direction keyword hits + regime bias hits, then
       recency (newer first — alpha decay favours fresher results), then
       arxiv_id for total order.
    3. Semantic rerank via paper_rag (defense-in-depth: keyword + semantic).
    4. Take top `paper_budget`.

    Returns ``(paper, score)`` pairs. The score is whatever the rerank seam
    (``paper_rag.augment_candidate_scores``) reported, and it is NOT always a
    measured similarity — that seam returns a uniform ``1.0`` when semantic
    retrieval is disabled, which is a sentinel, not a score. ``None`` means
    the rerank did not run at all (import/call failure), so the ordering is
    keyword-only. This is retained rather than discarded (#1636) so a later
    change can cut the candidate set at a similarity FLOOR instead of at a
    rank — at a 30-paper width the rank tail is where fabricated mechanisms
    would come from. Nothing cuts on it today.

    Args:
        regime_bias: "bull" or "bear" — biases retrieval toward momentum/trend
            (bull) or vol-managed/defensive (bear) papers. None = no bias.
    """
    terms = _asset_terms(brief.asset_classes)
    filtered = [p for p in corpus if any(t in p.haystack for t in terms)] if terms else list(corpus)

    direction_kws = list(re.findall(r"[a-z]{3,}", brief.strategic_direction.lower()))
    # Add regime-biased keywords to boost papers matching the regime
    regime_kws: list[str] = []
    if regime_bias and regime_bias in _REGIME_BIAS_TERMS:
        regime_kws = list(_REGIME_BIAS_TERMS[regime_bias])

    def score(p: CorpusPaper) -> tuple[int, str, str]:
        hits = sum(1 for kw in direction_kws if kw in p.haystack)
        # Regime bias adds extra weight for papers matching the regime
        hits += sum(2 for kw in regime_kws if kw in p.haystack)
        # Negative hits → higher hits sort first; published desc → newer
        # first; arxiv_id asc as the final deterministic tiebreak.
        return (-hits, _recency_key(p.published), p.arxiv_id)

    ranked = sorted(filtered, key=score)

    # Semantic rerank: defense-in-depth behind the keyword filter.
    # When FUSION_SEMANTIC_RETRIEVAL is off or fails, keyword ranking is
    # preserved unchanged.
    scored: list[tuple[CorpusPaper, float | None]] = [(c, None) for c in ranked]
    try:
        from archimedes.services.paper_rag import augment_candidate_scores

        scored = list(augment_candidate_scores(brief.strategic_direction, ranked))
    except Exception as exc:
        logger.debug("fusion: semantic rerank skipped, keyword-only: %s", exc)

    return scored[: brief.paper_budget]


def _recency_key(published: str) -> str:
    """Sort key making newer `published` sort first (we sort ascending).

    ISO dates sort lexicographically; invert by complementing digits so a
    later date yields a smaller key. Non-dates sort last (treated as oldest).
    """
    digits = re.sub(r"\D", "", published)[:8]
    if len(digits) != 8:
        return "99999999"
    return "".join(str(9 - int(c)) for c in digits)


# ── Output artifact ─────────────────────────────────────────────


@dataclass(frozen=True)
class FusionProposal:
    """A novel cross-paper strategy hypothesis. Pre-backtest, pre-curation.

    `status` is explicit so callers never infer failure from emptiness
    (architect parity). `model` is the TRUE served model (`response.model`)
    — the provenance field of record; `requested_model` is what we asked
    for, kept separately.
    """

    status: str  # ok | disabled | insufficient_corpus | unparseable
    brief: FusionBrief
    strategy_name: str
    thesis: str
    source_arxiv_ids: list[str]
    fusion_reasoning: str
    novelty_rationale: str
    risk_notes: str
    model: str
    requested_model: str
    strategy_spec: dict[str, Any] | None = None
    # Which branch _spec_universe took to pick strategy_spec["asset_universe"]:
    # "user" | "model" | "full" (#857). None when no spec was ever produced
    # (disabled / insufficient_corpus / unparseable) — there is no universe to
    # attribute a source to.
    universe_source: str | None = None
    # (#892) Any brief.asset_classes entries with ZERO representation in the
    # final strategy_spec["asset_universe"] — e.g. a classified asset class
    # with no data source wired up, or one whose proxies were dropped by the
    # model-universe cap. Empty (the common case) when every classified class
    # is represented. Claim-integrity signal: surfaced in risk_notes and
    # persisted so the passport/reasoning trace says so honestly instead of
    # silently proceeding as if the request was fully honored.
    universe_gaps: list[str] = field(default_factory=list)
    # (#1636) How many papers were actually PUT IN FRONT OF THE MODEL for this
    # proposal. Without it, `len(source_arxiv_ids) == 2` is unreadable: it could
    # be a model that rejected 28 papers with named reasons, or a steer so thin
    # that two is everything there was. The budget-vs-used pair is what makes a
    # shortfall auditable instead of merely small. 0 on the non-fusion statuses
    # (disabled / insufficient_corpus / unparseable), where no prompt was built.
    papers_offered: int = 0
    # (#1739) The paper→mechanism map: one entry per CITED paper the model
    # could tie to a named mechanism AND to indicator aliases that literally
    # appear in the validated spec's entry/exit conditions. Server-filtered in
    # ``propose`` — an id the model invented, or a spec_element that is not in
    # the spec, never survives into this list. Empty on every non-``ok``
    # status, and empty on an ``ok`` proposal whose model emitted no map.
    paper_mechanisms: list[dict[str, Any]] = field(default_factory=list)
    # (#1739) How many of ``source_arxiv_ids`` survive that filter with at
    # least one spec element attached. This is the honest read of "how many
    # papers does this strategy actually trade the mechanism of", as opposed
    # to ``len(source_arxiv_ids)``, which is the model's own claim. It LABELS,
    # it never gates: a 5-citation / 1-mechanism proposal is still actionable,
    # it just says so (#1636's honest-shortfall rule).
    distinct_mechanism_papers: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_actionable(self) -> bool:
        """True only for a real, parseable, >=2-paper fusion.

        Still keyed on MIN_PAPERS, never on FUSE_TARGET_MIN: a justified
        shortfall is a first-class outcome, not a failure (#1636). Making the
        target a gate here is exactly the "fail generation instead of improving
        it" move the issue rules out.
        """
        return self.status == "ok" and len(self.source_arxiv_ids) >= MIN_PAPERS

    @property
    def is_shortfall(self) -> bool:
        """True when fewer than FUSE_TARGET_MIN papers were cited (#1636).

        A signal, never a gate — read it to surface "2 of 30, here's why"
        rather than to reject. False for a non-``ok`` proposal, which has a
        status of its own to report.
        """
        return self.status == "ok" and len(self.source_arxiv_ids) < FUSE_TARGET_MIN


# ── Prompt construction ─────────────────────────────────────────
#
# The prompt TEXT lives in `agents/prompts.py` — one registry for every live LLM
# prompt in the tree, rendered into `docs/specs/prompt-inventory.md` under a
# drift test and byte-guarded by `test_prompt_registry_goldens.py` (#1800). The
# three module constants below stay, because that is what the rest of this file
# (and `test_multi_paper_utilization`) reads; only their SOURCE moved.


_SPEC_CONTRACT = PROMPTS["fusion.spec_contract"].text

# The paper-count rule is the only interpolated half of the proposer prompt.
# Rendering it here — rather than storing "5" and "2" in the registry — keeps
# the sentence the model reads tied to the constants this module actually
# enforces (MIN_PAPERS hard-rejects; FUSE_TARGET_MIN is a request, #1636).
_SYSTEM_PROMPT = PROMPTS["fusion.proposer.system"].render(
    fuse_target_min=FUSE_TARGET_MIN,
    min_papers=MIN_PAPERS,
)

_SPEC_REPAIR_SYSTEM = PROMPTS["fusion.spec_repair.system"].text


def _build_user_prompt(brief: FusionBrief, candidates: list[CorpusPaper]) -> str:
    rp = brief.risk_profile
    payload: dict[str, Any] = {
        "user_steer": {
            "asset_classes": brief.asset_classes,
            "risk_appetite": rp.value,
            "risk_envelope": RISK_PROFILE_PARAMS[rp],
            "strategic_direction": brief.strategic_direction
            or "(none given — optimize for novelty within the asset steer)",
            # Three distinct numbers, not one repeated (#1636): the hard floor
            # that rejects, the target we ask for, and the width of the set we
            # are showing. Only min_papers_to_fuse is enforced server-side.
            "min_papers_to_fuse": MIN_PAPERS,
            "target_papers_to_fuse": FUSE_TARGET_MIN,
            "max_papers_to_fuse": brief.paper_budget,
        },
        "candidate_papers": [
            {
                "arxiv_id": p.arxiv_id,
                "title": p.title,
                "primary_category": p.primary_category,
                "categories": list(p.categories),
                "published": p.published,
                "abstract": p.abstract,
            }
            for p in candidates
        ],
    }
    if brief.market_context:
        payload["market_context"] = brief.market_context
    return json.dumps(payload, indent=2)


# ── The fusion service ──────────────────────────────────────────


def _inert_proposal(brief: FusionBrief, status: str, thesis: str) -> FusionProposal:
    """A well-formed, self-describing non-fusion (disabled / declined)."""
    return FusionProposal(
        status=status,
        brief=brief,
        strategy_name="",
        thesis=thesis,
        source_arxiv_ids=[],
        fusion_reasoning="",
        novelty_rationale="",
        risk_notes="No fusion performed.",
        model="",
        requested_model="",
    )


class StrategyFusion:
    """User-steered, novelty-seeking, multi-paper strategy synthesizer.

    Pure service — no FastAPI, no on-chain, not wired into the architect or
    the construction-trace flow. Flag-gated: flag-off is a hard inert path
    (no anthropic import, no manifest read, sentinel proposal).
    """

    def __init__(
        self,
        backend: LLMBackend | None = None,
        corpus: list[CorpusPaper] | None = None,
        model: str | None = None,
        candidates: list[CorpusPaper] | None = None,
    ) -> None:
        # Backend/corpus are injectable for offline tests. They are resolved
        # lazily in `propose` so constructing the service never triggers an
        # anthropic import or a manifest read (matters for the flag-off path
        # and dependency-light import sites).
        self._backend = backend
        self._corpus = corpus
        # Model id to thread into the lazily-resolved backend (A3 seam, T1.1).
        # When set (and no explicit backend was injected), `_resolve_backend`
        # builds `make_llm_backend(model=...)` so the user's Generate-page model
        # pick is honored and `served_model` reports the TRUE model rather than
        # the env default. Was the gap that let the debate proposer silently run
        # on Nova regardless of the user's pick (spec §8 item 10 / fix A3).
        self._model = model
        # An ALREADY-SELECTED candidate set, used verbatim (#1636). The debate
        # proposer runs `select_candidates(fb, corpus, regime_bias=R)` itself
        # and then handed the result in as `corpus=`, so `propose` re-ran
        # `select_candidates` over it — a second rerank that DROPPED the
        # regime_bias, silently discarding the very ordering the steer paid
        # for. Passing the set here skips the re-selection entirely. None
        # keeps the original behavior (select from `corpus`).
        self._candidates = candidates

    def _resolve_backend(self) -> LLMBackend:
        if self._backend is not None:
            return self._backend
        self._backend = default_backend(self._model)
        return self._backend

    def _resolve_corpus(self) -> list[CorpusPaper]:
        if self._corpus is not None:
            return self._corpus
        self._corpus = load_corpus()
        return self._corpus

    def propose(self, brief: FusionBrief) -> FusionProposal:
        # Unconditional since 2026-09-02 (deck Q4): no flag check here. See the
        # module docstring for why the OFF branch was deleted rather than
        # defaulted ON.
        if self._candidates is not None:
            # Pre-selected by the caller (the debate proposer, which already
            # ran select_candidates WITH its regime_bias) — used verbatim so
            # that ordering is not thrown away by a second, bias-free rerank.
            candidates = list(self._candidates)
        else:
            corpus = self._resolve_corpus()
            candidates = select_candidates(brief, corpus)
        if len(candidates) < MIN_PAPERS:
            return _inert_proposal(
                brief,
                "insufficient_corpus",
                f"Need at least {MIN_PAPERS} papers matching the steer to "
                f"fuse; the corpus yielded {len(candidates)}. Broaden "
                "asset_classes / strategic_direction or grow the corpus. "
                "(Single-paper output is intentionally not produced — that "
                "is the strategy architect's job, not fusion's.)",
            )

        backend = self._resolve_backend()
        valid_ids = {p.arxiv_id for p in candidates}
        raw = backend.complete(_SYSTEM_PROMPT, _build_user_prompt(brief, candidates))

        try:
            parsed = extract_json(raw)
        except ValueError:
            logger.warning("fusion: unparseable LLM output; declined proposal")
            return FusionProposal(
                status="unparseable",
                brief=brief,
                strategy_name="",
                thesis=(
                    "Could not parse a valid fusion from the model. No "
                    "hypothesis proposed — safer than shipping a guess."
                ),
                source_arxiv_ids=[],
                fusion_reasoning="",
                novelty_rationale="",
                risk_notes="Model output was not valid JSON.",
                model=backend.served_model,
                requested_model=backend.model_id,
            )

        # Anti-hallucination: drop any arxiv_id not in the deterministically
        # selected candidate set (architect parity — it drops unknown ids).
        raw_ids = parsed.get("source_arxiv_ids", [])
        source_ids = [str(i) for i in raw_ids if isinstance(i, str) and i in valid_ids]
        # De-dupe, preserve order.
        seen: set[str] = set()
        source_ids = [i for i in source_ids if not (i in seen or seen.add(i))]

        # (#1739) Paper→mechanism map, id half — the SAME anti-hallucination
        # shape as the valid_ids filter directly above: an entry naming a paper
        # this proposal does not cite is dropped, never repaired. The
        # spec_elements half runs further down, once there is a VALIDATED spec
        # whose indicator aliases can be checked against.
        cited_ids = set(source_ids)
        paper_mechanisms: list[dict[str, Any]] = [
            e
            for e in (parsed.get("paper_mechanisms") or [])
            if isinstance(e, dict) and str(e.get("arxiv_id", "")) in cited_ids
        ]

        if len(source_ids) < MIN_PAPERS:
            logger.warning(
                "fusion: model fused %d valid papers (<%d); declined",
                len(source_ids),
                MIN_PAPERS,
            )
            return _inert_proposal(
                brief,
                "insufficient_corpus",
                f"Model did not fuse at least {MIN_PAPERS} of the provided "
                "papers (after dropping any hallucinated ids). No "
                "single-paper hypothesis is produced.",
            )

        # (#1636) The honest-fewer record. A citation count below the target is
        # ACCEPTED — MIN_PAPERS is the only hard reject — but it is never
        # silent: the budget-vs-used pair is logged on every proposal that
        # falls short, so "cited 2" is distinguishable from "cited 2 of 30"
        # in the logs and, via `papers_offered`, on the artifact itself.
        papers_offered = len(candidates)
        if len(source_ids) < FUSE_TARGET_MIN:
            logger.warning(
                "fusion: shortfall — model cited %d paper(s) of %d offered "
                "(target %d, hard floor %d); accepted, not blocked — the "
                "justification belongs in fusion_reasoning",
                len(source_ids),
                papers_offered,
                FUSE_TARGET_MIN,
                MIN_PAPERS,
            )

        # Extract strategy_spec if present. Weak-JSON models (Nova Micro) often
        # omit it despite the REQUIRED contract — without a spec there is no
        # backtest, no rigor verdict, and the strategy is stuck at "pending"
        # forever (#788); the debate society outright DROPS spec-less proposals
        # (A5 conformance), so its pool would come up empty. One bounded repair
        # retry asks for ONLY the spec before we fall back to text-only.
        strategy_spec = parsed.get("strategy_spec")
        if not isinstance(strategy_spec, dict):
            strategy_spec = _repair_spec(backend, brief, parsed)
        universe_source: str | None = None
        universe_gaps: list[str] = []
        validated_spec = None
        if not isinstance(strategy_spec, dict):
            strategy_spec = None
        else:
            # Universe steering (#847): user's resolved assets > the model's
            # SSOT-validated suggestion (capped) > full supported universe.
            # (#857) the branch taken is recorded as universe_source so the
            # passport can surface which one fired — a model-picked universe
            # is a mild look-ahead channel worth being auditable, not blocked.
            # (#892) universe_gaps carries any classified asset_classes that
            # ended up with zero representation — surfaced below, never
            # silently dropped.
            strategy_spec["asset_universe"], universe_source, universe_gaps = _spec_universe(brief, strategy_spec)
            # Validate the FINAL dict (post-steering). An invalid spec — a
            # partial repair that happened to carry entry/exit, or a malformed
            # model emission — must degrade to honest text-only HERE, not
            # surface later as a DSLError mid-evaluation/debate.
            try:
                validated_spec = validate_strategy_spec(strategy_spec)
            except DSLError as exc:
                logger.warning("fusion: strategy_spec failed DSL validation (%s) — falling back to text-only", exc)
                strategy_spec = None
                universe_source = None
                universe_gaps = []
                validated_spec = None

        # (#1739) Paper→mechanism map, spec_elements half. ``indicators`` on the
        # VALIDATED spec is exactly the alias set ``strategy_dsl`` checks
        # ``parameter_variants`` keys against (strategy_dsl.py:269-274) — it is
        # rebuilt from the entry/exit conditions (``sorted(all_indicators)``),
        # so it is what the spec TRADES, not the ``indicators`` list the model
        # declared alongside it. Checking the declared list would validate one
        # self-report against another, which is the bug this issue is about: an
        # alias that entry/exit never uses is not part of what this spec
        # trades, so a paper "attributed" to it is not attributed at all. The
        # entry is KEPT with its claim and its id — only the unsupported
        # element is stripped, the debate's keep-the-claim/strip-the-id honesty
        # pattern (debate_engine.py:467-501) — and it then contributes 0 to the
        # count. No validated spec (text-only fallback) → no aliases → every
        # entry is unattributed, which is the honest read of a proposal that
        # trades nothing yet.
        valid_elements: set[str] = set(validated_spec.indicators) if validated_spec is not None else set()
        paper_mechanisms = [
            {
                "arxiv_id": str(e.get("arxiv_id", "")),
                "mechanism": str(e.get("mechanism", "") or "").strip(),
                "spec_elements": [
                    el for el in (e.get("spec_elements") or []) if isinstance(el, str) and el in valid_elements
                ],
            }
            for e in paper_mechanisms
        ]
        distinct_mechanism_papers = len({e["arxiv_id"] for e in paper_mechanisms if e["spec_elements"]})
        if distinct_mechanism_papers < len(source_ids):
            # LABEL, never gate (#1636): a citation the model cannot tie to a
            # traded indicator is recorded as unattributed, not deleted and not
            # a reject. The pair is what makes "cites 5" readable.
            logger.warning(
                "fusion: paper→mechanism attribution — %d of %d cited paper(s) name a mechanism "
                "tied to an indicator this spec actually trades; the remainder are recorded as "
                "unattributed (labelled, never blocked)",
                distinct_mechanism_papers,
                len(source_ids),
            )

        risk_notes = str(parsed.get("risk_notes", "")).strip()
        if universe_gaps:
            gap_note = (
                f"Universe gap: {', '.join(universe_gaps)} requested/classified but not available "
                "in the current data universe — the traded universe above does not include it."
            )
            logger.warning("fusion: universe gap for brief asset_classes=%s -> %s", brief.asset_classes, universe_gaps)
            risk_notes = f"{risk_notes} {gap_note}".strip() if risk_notes else gap_note

        return FusionProposal(
            status="ok",
            brief=brief,
            strategy_name=str(parsed.get("strategy_name", "")).strip(),
            thesis=str(parsed.get("thesis", "")).strip(),
            source_arxiv_ids=source_ids,
            fusion_reasoning=str(parsed.get("fusion_reasoning", "")).strip(),
            novelty_rationale=str(parsed.get("novelty_rationale", "")).strip(),
            risk_notes=risk_notes,
            model=backend.served_model,  # TRUE served model — field of record
            requested_model=backend.model_id,  # what we asked for
            strategy_spec=strategy_spec,
            universe_source=universe_source,
            universe_gaps=universe_gaps,
            papers_offered=papers_offered,
            paper_mechanisms=paper_mechanisms,
            distinct_mechanism_papers=distinct_mechanism_papers,
        )


def default_backend(model: str | None = None) -> LLMBackend:
    """Claude or GLM when credentials are present; canned fallback otherwise.

    Delegates to the provider-agnostic ``llm_backend.make_llm_backend()`` factory.
    ``model`` threads the user's Generate-page model pick through to the factory
    (A3 seam, T1.1); ``None`` keeps the env default — behavior unchanged.
    """
    backend = make_llm_backend(model=model)
    if backend.available:
        return backend  # type: ignore[return-value]
    logger.warning("No LLM credentials (LLM_* or ANTHROPIC_* env vars) — strategy fusion using canned fallback")
    return FusionCannedBackend()


# NOTE: ``default_fusion(model=None)`` used to live here as the factory for the
# fusion job path (``_run_fusion_job`` in ``api/strategies_routes.py``). That
# route was deleted on 2026-08-31 and the factory went with it — it had no other
# caller in the tree or on any open branch. The debate society deliberately does
# NOT use a shared factory: ``debate_engine._propose_pool`` constructs
# ``StrategyFusion(model=..., corpus=evidence)`` per proposal so the user's model
# pick and the regime-steered evidence set are both explicit (the A3 seam,
# docs/specs/multi-agent-debate-spec.md §8). Re-adding a module-level factory
# would reintroduce the model-blind singleton that seam exists to prevent.
