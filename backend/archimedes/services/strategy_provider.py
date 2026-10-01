"""Strategy provider service — Dan's implementation of `IStrategyProvider`.

Source of truth is the analytics-engine strategy directory
(`analytics-engine/strategies/*.py`). Each file is a self-describing
paper-grounded strategy: a `bt.Strategy` subclass plus module-level
constants that encode the passport metadata.

The provider does not import backtrader. It reads the metadata via AST so
the backend can list and serve strategies without pulling the backtest
engine into the API process.

References:
- `docs/specs/component-interfaces-spec.md` — Dan owns IStrategyProvider
- `docs/specs/strategy-passport-spec.md` — passport schema
- `analytics-engine/src/archimedes_analytics_engine/strategy_loader.py` —
  parallel runtime loader used by the backtest engine
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from archimedes.db import get_session
from archimedes.models.backtest import BacktestResult
from archimedes.models.backtest_fixtures_store import FIXTURE_FIELDS, StrategyBacktestFixture
from archimedes.models.paper_ref import PaperRef
from archimedes.models.strategy import (
    PositionSizing,
    RebalanceFrequency,
    Strategy,
    StrategyStatus,
)
from archimedes.services.backtest_repository import latest_backtests_by_strategy

logger = logging.getLogger(__name__)


# Constants the analytics-engine strategy_loader recognizes plus passport
# extensions specific to the provider. Anything not present is treated as
# missing rather than an error — strategies remain valid with partial
# metadata.
_METADATA_KEYS: tuple[str, ...] = (
    "PAPER_ARXIV_ID",
    "PAPER_TITLE",
    "PAPER_AUTHORS",
    "PAPER_VENUE",
    "PAPER_YEAR",
    "PAPER_DOI",
    "PAPER_CITATION_COUNT",
    "METHODOLOGY_SUMMARY",
    "METHODOLOGY_TEXT",
    "PAPER_CLAIMED_SHARPE",
    "PAPER_CLAIMED_CAGR",
    "PAPER_CLAIMED_MAX_DD",
    "ASSET_UNIVERSE",
    "POSITION_SIZING",
    "REBALANCE_FREQUENCY",
    "RISK_PROFILES",
    "RISK_CONSTRAINTS",
    "CURATOR_WALLET",
    "CURATOR_NOTE",
    "EXTRACTION_LLM",
    # Lifecycle status
    "STATUS",
    "REGIME_TAG",
)


# Keyword heuristic for risk profile inference when a strategy file does
# not declare RISK_PROFILES explicitly. Lowercased substring match.
_RISK_PROFILE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fixed_income": ("treasury", "t-bill", "tbill", "capital preservation", "usyc", "cash equivalent", "fixed income"),
    "conservative": ("mean reversion", "low-vol", "bond", "defensive", "yield"),
    "moderate": ("trend", "balanced", "factor", "value", "carry"),
    "aggressive": ("momentum", "high-conviction", "concentrated", "growth"),
    "hyper_risky": ("leveraged", "leverage", "sector concentration", "volatility short"),
}


def _read_module_constants(path: Path) -> dict[str, Any]:
    """Parse a Python file and return its module-level constant assignments.

    Uses `ast.literal_eval` on right-hand sides so backtrader (or any other
    import) is never executed. Only literals of the standard JSON-ish shape
    are recoverable; anything more exotic is silently skipped.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    out: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is None:
                continue  # bare annotation, no value to read
            targets = [node.target]
            value_node = node.value
        else:
            continue
        for target in targets:
            if target.id not in _METADATA_KEYS:
                continue
            try:
                out[target.id] = ast.literal_eval(value_node)
            except (ValueError, SyntaxError) as exc:
                logger.debug("skip non-literal %s in %s: %s", target.id, path, exc)
    return out


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_methodology(metadata: dict[str, Any]) -> str:
    text = metadata.get("METHODOLOGY_TEXT") or metadata.get("METHODOLOGY_SUMMARY") or ""
    return str(text).strip()


def _methodology_hash(metadata: dict[str, Any]) -> str:
    canonical = _canonical_methodology(metadata)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strategy_id(metadata: dict[str, Any], code_hash: str) -> str:
    """Deterministic ID: hash of arxiv_id (or DOI, or title) + methodology hash."""
    paper_key = metadata.get("PAPER_ARXIV_ID") or metadata.get("PAPER_DOI") or metadata.get("PAPER_TITLE") or code_hash
    payload = f"{paper_key}|{_methodology_hash(metadata)}".encode()
    return hashlib.sha256(payload).hexdigest()[:32]


def _infer_risk_profiles(methodology_summary: str) -> list[str]:
    text = methodology_summary.lower()
    matched = [profile for profile, keywords in _RISK_PROFILE_KEYWORDS.items() if any(kw in text for kw in keywords)]
    return matched or ["moderate"]


# Env vars selecting a dynamic fixture source (issue #465). When neither is
# set, the loader reads the bundled ``backtest_fixtures.json`` exactly as
# before — the bundled file IS the fallback, so served (gate-sensitive)
# values are unchanged when nothing is configured.
_FIXTURES_PATH_ENV = "ARCHIMEDES_FIXTURES_PATH"  # filesystem override
_FIXTURES_URL_ENV = "ARCHIMEDES_FIXTURES_URL"  # http(s) source


def _parse_fixtures_payload(text: str, source: str) -> dict[str, Any]:
    """Parse fixture JSON text into the keyed dict shape, validating type.

    Returns ``{}`` (so the caller can fall back) when the payload is empty.
    Raises on malformed JSON or a non-object top level so the caller treats it
    as a failed source. ``source`` is for log/error context only.
    """
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{source}: expected a JSON object, got {type(data).__name__}")
    return data


def _load_fixtures_from_path(path: Path) -> dict[str, Any]:
    """Load + parse a fixtures JSON file. Raises on missing/invalid file."""
    return _parse_fixtures_payload(path.read_text(encoding="utf-8"), str(path))


def _load_fixtures_from_url(url: str) -> dict[str, Any]:
    """Fetch + parse fixtures JSON over HTTP. Raises on any failure.

    ``httpx`` is the established HTTP client in this package (corpus_service,
    llm_backend); imported lazily so the API process doesn't pay for it unless
    a URL source is actually configured.
    """
    import httpx  # lazy — only when a URL source is configured

    resp = httpx.get(url, timeout=10.0)
    resp.raise_for_status()
    return _parse_fixtures_payload(resp.text, url)


def _load_dynamic_fixtures() -> dict[str, Any] | None:
    """Try the env-configured dynamic source (path, then URL).

    Returns the loaded dict on success, or ``None`` when no dynamic source is
    configured OR the configured source fails / yields nothing — signalling the
    caller to fall back to the bundled file. A configured-but-failing source is
    deliberately non-fatal: a transient remote outage must never take down the
    served strategy library, it just reverts to the bundled fallback.
    """
    path_override = os.getenv(_FIXTURES_PATH_ENV)
    if path_override:
        try:
            loaded = _load_fixtures_from_path(Path(path_override))
            if loaded:
                logger.info("loaded backtest fixtures from %s=%s", _FIXTURES_PATH_ENV, path_override)
                return loaded
            logger.warning("%s=%s yielded no fixtures; falling back to bundled", _FIXTURES_PATH_ENV, path_override)
        except Exception as exc:
            logger.warning("dynamic fixtures path %s failed (%s); falling back to bundled", path_override, exc)

    url_override = os.getenv(_FIXTURES_URL_ENV)
    if url_override:
        try:
            loaded = _load_fixtures_from_url(url_override)
            if loaded:
                logger.info("loaded backtest fixtures from %s=%s", _FIXTURES_URL_ENV, url_override)
                return loaded
            logger.warning("%s=%s yielded no fixtures; falling back to bundled", _FIXTURES_URL_ENV, url_override)
        except Exception as exc:
            logger.warning("dynamic fixtures url %s failed (%s); falling back to bundled", url_override, exc)

    return None


def _fixture_row_to_dict(row: StrategyBacktestFixture) -> dict[str, Any]:
    """Project one ORM row into the exact dict shape the JSON records used —
    same keys, same native Python types (SQLAlchemy Float/Integer/Boolean/
    String round-trip to float/int/bool/str, matching ``json.loads``)."""
    return {field: getattr(row, field) for field in FIXTURE_FIELDS}


def _load_fixtures_from_db(session: Session | None = None) -> dict[str, Any]:
    """Load backtest fixtures from the ``strategy_backtest_fixtures`` table.

    Replaces the committed ``backtest_fixtures.json`` as the terminal
    (non-override) fixture source. Same ``{stem: {...}}`` output shape the
    bundled-file loader always produced, so ``_to_strategy``'s ``fx.get(...)``
    calls are unchanged.

    **Never raises.** A DB read failure (unreachable DB, table not yet
    migrated) degrades gracefully to ``{}`` — identical fail-open-to-empty
    contract the bundled-file loader had for a missing/invalid file. A
    strategy just serves without real backtest numbers layered in; it does
    not take down strategy listing.

    Args:
        session: An existing DB session to query on (tests pass an isolated
            one). When ``None``, opens and closes its own session via
            ``archimedes.db.get_session``.
    """

    def _query(sess: Session) -> dict[str, Any]:
        rows = sess.query(StrategyBacktestFixture).all()
        return {row.stem: _fixture_row_to_dict(row) for row in rows}

    try:
        if session is not None:
            return _query(session)
        with get_session() as sess:
            return _query(sess)
    except Exception as exc:
        logger.warning("could not load backtest fixtures from DB: %s", exc)
        return {}


def _load_fixtures(session: Session | None = None) -> dict[str, Any]:
    """Load backtest fixtures, preferring a dynamic source over the DB.

    Resolution order (issue #465 dynamic overrides; DB migration):
      1. ``ARCHIMEDES_FIXTURES_PATH`` — a filesystem path override.
      2. ``ARCHIMEDES_FIXTURES_URL`` — an http(s) source.
      3. The ``strategy_backtest_fixtures`` DB table (replaces the committed
         ``backtest_fixtures.json``).

    The dynamic source is *preferred* only when configured and it loads
    successfully with non-empty content; otherwise the DB is queried. When
    neither env var is set, behavior is unchanged from before the DB
    migration except for the storage medium of the terminal source.

    Args:
        session: passed through to ``_load_fixtures_from_db`` (tests only;
            production callers use the default and let it open its own).
    """
    dynamic = _load_dynamic_fixtures()
    if dynamic is not None:
        return dynamic
    return _load_fixtures_from_db(session)


def _to_strategy(
    path: Path, metadata: dict[str, Any], code_hash: str, fixture: dict[str, Any] | None = None
) -> Strategy:
    methodology_summary = str(metadata.get("METHODOLOGY_SUMMARY") or metadata.get("METHODOLOGY_TEXT") or "")
    methodology_text = metadata.get("METHODOLOGY_TEXT")
    risk_profiles = metadata.get("RISK_PROFILES") or _infer_risk_profiles(methodology_summary)

    position_sizing_raw = str(metadata.get("POSITION_SIZING") or "equal_weight").lower()
    try:
        position_sizing = PositionSizing(position_sizing_raw)
    except ValueError:
        logger.warning("unknown POSITION_SIZING=%s in %s, defaulting", position_sizing_raw, path)
        position_sizing = PositionSizing.EQUAL_WEIGHT

    rebalance_raw = str(metadata.get("REBALANCE_FREQUENCY") or "weekly").lower()
    try:
        rebalance_frequency = RebalanceFrequency(rebalance_raw)
    except ValueError:
        logger.warning("unknown REBALANCE_FREQUENCY=%s in %s, defaulting", rebalance_raw, path)
        rebalance_frequency = RebalanceFrequency.WEEKLY

    paper_year = metadata.get("PAPER_YEAR")
    if paper_year is not None:
        paper_year = int(paper_year)

    status_raw = str(metadata.get("STATUS") or "candidate").lower()
    try:
        status = StrategyStatus(status_raw)
    except ValueError:
        logger.warning("unknown STATUS=%s in %s, defaulting to candidate", status_raw, path)
        status = StrategyStatus.CANDIDATE

    # CANDIDATE → VALIDATED promotion is NOT driven by the fixture boolean (#821).
    # The fixture's ``passes_rigor_gate`` is a stored value the live gate never
    # re-derives — exactly the cached-pass pattern the #1 rule forbids. Promotion
    # is now driven off the LIVE gate verdict computed on persisted real returns
    # (see api/strategies_routes.py + services/live_rigor_gate.py): the served
    # status is overlaid there from the live verdict. Here we keep only the file's
    # declared STATUS (so a curator can still hand-declare live/retired); the
    # fixture boolean no longer promotes anything.

    paper_claimed_sharpe = metadata.get("PAPER_CLAIMED_SHARPE")
    paper_claimed_cagr = metadata.get("PAPER_CLAIMED_CAGR")
    paper_claimed_max_dd = metadata.get("PAPER_CLAIMED_MAX_DD")

    # Stub backtest values from BACKTEST_* constants (PLACEHOLDER until IBacktestEvaluator runs)
    stub_sharpe = metadata.get("BACKTEST_SHARPE")
    stub_cagr = metadata.get("BACKTEST_CAGR")
    stub_max_dd = metadata.get("BACKTEST_MAX_DD")
    stub_win_rate = metadata.get("BACKTEST_WIN_RATE")
    stub_calmar = metadata.get("BACKTEST_CALMAR")
    stub_corr_spy = metadata.get("BACKTEST_CORR_SPY")

    # ── Real backtest results from fixture ────────────────────────────
    fx = fixture or {}
    real_sharpe = fx.get("sharpe_ratio")
    real_sortino = fx.get("sortino_ratio")
    real_cagr = fx.get("cagr")
    real_max_dd = fx.get("max_drawdown")
    real_win_rate = fx.get("win_rate")
    real_calmar = fx.get("calmar_ratio")
    real_corr_spy = fx.get("correlation_to_spy")
    real_total_trades = fx.get("total_trades")
    real_backtest_start = fx.get("backtest_start")
    real_backtest_end = fx.get("backtest_end")
    deflated_sharpe_ratio = fx.get("deflated_sharpe_ratio")
    dsr_p_value = fx.get("dsr_p_value")
    num_trials_in_selection = fx.get("num_trials_in_selection")
    pbo_score = fx.get("pbo_score")
    out_of_sample_sharpe = fx.get("out_of_sample_sharpe")
    # NOTE (#821): the badge ``passes_rigor_gate`` is NOT sourced from the fixture
    # boolean. The fixture value (``fx.get("passes_rigor_gate")``) is a stored
    # constant the live gate never re-derives — the cached-pass anti-pattern the
    # #1 rule forbids. The served badge is overlaid from the LIVE gate verdict
    # computed on persisted real returns in api/strategies_routes (single source of
    # truth via services/live_rigor_gate). We initialise to False (fail-closed) here
    # so nothing downstream that reads ``Strategy.passes_rigor_gate`` directly can
    # leak a fixture pass; the served path replaces it with the live verdict.
    passes_rigor_gate = False
    kelly_fraction = fx.get("kelly_fraction")
    n_obs_daily = fx.get("n_obs_daily")

    # Validate regime_tag — required per issue #162 anti-goals.
    regime_tag_raw = str(metadata.get("REGIME_TAG") or "").strip().lower()
    _VALID_REGIME_TAGS = {"bull", "bear", "regime_neutral"}
    if not regime_tag_raw or regime_tag_raw not in _VALID_REGIME_TAGS:
        raise ValueError(
            f"Invalid or missing REGIME_TAG={regime_tag_raw!r} in {path}. Must be one of {_VALID_REGIME_TAGS}."
        )
    regime_tag = regime_tag_raw

    # Compute Lo (2002) Sharpe 95% CI when real Sharpe and n_obs are available
    sharpe_ci_lower = sharpe_ci_upper = None
    if real_sharpe is not None and n_obs_daily is not None:
        from archimedes.services.rigor_evaluator import (
            compute_sharpe_ci,  # local import to avoid circulars
        )

        sharpe_ci_lower, sharpe_ci_upper = compute_sharpe_ci(float(real_sharpe), int(n_obs_daily))

    # Use file mtime so timestamps reflect the strategy file's curation
    # time rather than process start time — otherwise every restart would
    # bump created_at/updated_at for unchanged strategies.
    file_mtime = datetime.fromtimestamp(path.stat().st_mtime)

    # Build PaperRef from the curated strategy file's metadata.
    # Curated strategies reference a single paper; fusion strategies have N.
    paper_ref = PaperRef(
        arxiv_id=str(metadata.get("PAPER_ARXIV_ID") or "") or None,
        title=str(metadata.get("PAPER_TITLE") or path.stem),
        authors=list(metadata.get("PAPER_AUTHORS") or []),
        doi=metadata.get("PAPER_DOI"),
        venue=metadata.get("PAPER_VENUE"),
        year=paper_year,
        citation_count=metadata.get("PAPER_CITATION_COUNT"),
        contribution=None,  # Curated = single paper; no fusion contribution
    )

    return Strategy(
        id=_strategy_id(metadata, code_hash),
        papers=[paper_ref],
        methodology_summary=methodology_summary,
        methodology_text=methodology_text if isinstance(methodology_text, str) else None,
        asset_universe=list(metadata.get("ASSET_UNIVERSE") or []),
        signals=[],
        position_sizing=position_sizing,
        rebalance_frequency=rebalance_frequency,
        risk_constraints=dict(metadata.get("RISK_CONSTRAINTS") or {}),
        risk_profiles=list(risk_profiles),
        status=status,
        extraction_reasoning="",
        created_at=file_mtime,
        updated_at=file_mtime,
        paper_claimed_sharpe=float(paper_claimed_sharpe) if paper_claimed_sharpe is not None else None,
        paper_claimed_cagr=float(paper_claimed_cagr) if paper_claimed_cagr is not None else None,
        paper_claimed_max_dd=float(paper_claimed_max_dd) if paper_claimed_max_dd is not None else None,
        methodology_hash=_methodology_hash(metadata),
        extraction_llm=metadata.get("EXTRACTION_LLM"),  # None for hand-curated
        extraction_prompt_hash=None,
        curator_wallet=metadata.get("CURATOR_WALLET"),
        curator_validation_at=None,
        curator_note=metadata.get("CURATOR_NOTE"),
        strategy_code_path=str(path),
        strategy_code_hash=code_hash,
        on_chain_registration_tx=None,
        stub_sharpe=float(stub_sharpe) if stub_sharpe is not None else None,
        stub_cagr=float(stub_cagr) if stub_cagr is not None else None,
        stub_max_dd=float(stub_max_dd) if stub_max_dd is not None else None,
        stub_win_rate=float(stub_win_rate) if stub_win_rate is not None else None,
        stub_calmar=float(stub_calmar) if stub_calmar is not None else None,
        stub_corr_spy=float(stub_corr_spy) if stub_corr_spy is not None else None,
        real_sharpe=float(real_sharpe) if real_sharpe is not None else None,
        real_sortino=float(real_sortino) if real_sortino is not None else None,
        real_cagr=float(real_cagr) if real_cagr is not None else None,
        real_max_dd=float(real_max_dd) if real_max_dd is not None else None,
        real_win_rate=float(real_win_rate) if real_win_rate is not None else None,
        real_calmar=float(real_calmar) if real_calmar is not None else None,
        real_corr_spy=float(real_corr_spy) if real_corr_spy is not None else None,
        real_total_trades=int(real_total_trades) if real_total_trades is not None else None,
        real_backtest_start=str(real_backtest_start) if real_backtest_start else None,
        real_backtest_end=str(real_backtest_end) if real_backtest_end else None,
        deflated_sharpe_ratio=float(deflated_sharpe_ratio) if deflated_sharpe_ratio is not None else None,
        dsr_p_value=float(dsr_p_value) if dsr_p_value is not None else None,
        num_trials_in_selection=int(num_trials_in_selection) if num_trials_in_selection is not None else None,
        pbo_score=float(pbo_score) if pbo_score is not None else None,
        out_of_sample_sharpe=float(out_of_sample_sharpe) if out_of_sample_sharpe is not None else None,
        passes_rigor_gate=passes_rigor_gate,
        kelly_fraction=float(kelly_fraction) if kelly_fraction is not None else None,
        sharpe_ci_lower=round(sharpe_ci_lower, 4) if sharpe_ci_lower is not None else None,
        sharpe_ci_upper=round(sharpe_ci_upper, 4) if sharpe_ci_upper is not None else None,
        n_obs_daily=int(n_obs_daily) if n_obs_daily is not None else None,
        regime_tag=regime_tag,
    )


class LocalStrategyProvider:
    """File-system-backed `IStrategyProvider`.

    Owner: Dan. Wired into the agent orchestrator via dependency injection
    in `archimedes.api.routes`.

    Reload semantics: strategies are loaded eagerly on construction and
    cached in-memory. Call `refresh()` to re-scan the directory after a
    new strategy file lands.
    """

    def __init__(self, strategies_dir: Path) -> None:
        self._strategies_dir = strategies_dir
        self._strategies: dict[str, Strategy] = {}
        self._backtests: dict[str, BacktestResult] = {}
        self._fixtures: dict[str, Any] = {}
        self._synced_to_unified = False  # one-time sync flag
        self.refresh()

    def refresh(self) -> int:
        """Re-scan the strategies directory. Returns the loaded count."""
        if not self._strategies_dir.exists():
            logger.warning("strategies dir not found: %s", self._strategies_dir)
            self._strategies = {}
            return 0

        self._fixtures = _load_fixtures()

        loaded: dict[str, Strategy] = {}
        for path in sorted(self._strategies_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                metadata = _read_module_constants(path)
                if not metadata.get("PAPER_TITLE"):
                    logger.debug("no PAPER_TITLE in %s, skipping", path)
                    continue
                code_hash = _hash_file(path)
                fixture = self._fixtures.get(path.stem)
                strategy = _to_strategy(path, metadata, code_hash, fixture)
                loaded[strategy.id] = strategy
            except Exception as exc:
                logger.exception("failed to load strategy %s: %s", path, exc)
        self._strategies = loaded
        self._backtests = self._load_backtests(loaded.keys())
        for sid, strategy in self._strategies.items():
            bt = self._backtests.get(sid)
            if bt is None:
                continue
            strategy.stub_sharpe = bt.sharpe_ratio
            strategy.stub_cagr = bt.cagr
            strategy.stub_max_dd = bt.max_drawdown
            strategy.stub_win_rate = bt.win_rate
            strategy.stub_calmar = bt.calmar_ratio
            strategy.stub_corr_spy = bt.correlation_to_spy

        logger.info(
            "loaded %d strategies from %s (%d with backtests)",
            len(loaded),
            self._strategies_dir,
            len(self._backtests),
        )

        # Sync to unified strategy_passports table (Phase 2, Issue #160).
        # One-time at startup only — not on every refresh() call.
        # PR #283's force_update=True on every request caused 8s latency
        # on /api/vaults/ (Issue #288).
        if not self._synced_to_unified:
            self._sync_to_unified_table(loaded)
            self._synced_to_unified = True

        return len(loaded)

    def _sync_to_unified_table(self, strategies: dict[str, Strategy]) -> None:
        """Sync loaded strategies to the unified strategy_passports table.

        Writes the CARD — file metadata, papers, and the resolved display
        metrics. It never writes a grade: the nine verdict columns move only
        through ``passport_loader._apply_rigor_verdict``, so this sync can run on
        every process start without disturbing what a gate decided
        (``docs/adr/rigor-verdict-of-record.md``).

        ``with_display_metrics`` is the #1746 half. The row used to store only
        the FIRST link of the display chain (the #1187 fixture snapshot), which
        is why ``GET /api/strategies/passports/{id}`` served ``sharpe_ratio:
        null`` for ``harvey_2018_volatility_targeting`` while
        ``GET /api/strategies/{id}`` served ``0.406`` from the persisted backtest
        — one strategy, two endpoints, two Sharpes. The chain is resolved here,
        once, and both endpoints now read the answer.
        """
        try:
            from archimedes.services.curated_metrics import display_metrics_source, with_display_metrics
            from archimedes.services.passport_loader import ingest_passport

            with get_session() as session:
                synced = 0
                for strategy in strategies.values():
                    try:
                        bt = self._backtests.get(strategy.id)
                        ingest_passport(
                            session,
                            with_display_metrics(strategy, bt),
                            generation_method="curated",
                            force_update=True,
                            # The label travels with the numbers. Derived from
                            # the SAME (strategy, bt) pair `with_display_metrics`
                            # resolves from, and written in the same call, so no
                            # reader has to re-derive it from a provider memo of
                            # a different vintage — and so the passport payload
                            # can say which link a number came from at all.
                            display_metrics_source=display_metrics_source(strategy, bt),
                        )
                        synced += 1
                    except Exception as exc:
                        logger.debug("passport sync failed for %s: %s", strategy.id, exc)
                session.commit()
            logger.info("synced %d strategies to strategy_passports table", synced)
        except Exception as exc:
            logger.warning("unified table sync failed (non-blocking): %s", exc)

    def _load_backtests(self, strategy_ids: Iterable[str]) -> dict[str, BacktestResult]:
        ids = list(strategy_ids)
        if not ids:
            return {}
        try:
            with get_session() as session:
                rows = latest_backtests_by_strategy(session, ids)
            return {strategy_id: row.to_backtest_result() for strategy_id, row in rows.items()}
        except Exception as exc:
            logger.warning("backtest load failed (using None fallback): %s", exc)
            return {}

    # ── IStrategyProvider ───────────────────────────────────

    def list_strategies(
        self,
        status: StrategyStatus | None = None,
        asset_universe: list[str] | None = None,
    ) -> list[Strategy]:
        results = list(self._strategies.values())
        if status is not None:
            results = [s for s in results if s.status == status]
        if asset_universe:
            wanted = set(asset_universe)
            results = [s for s in results if wanted.intersection(s.asset_universe)]
        return results

    def get_strategy(self, strategy_id: str) -> Strategy | None:
        return self._strategies.get(strategy_id)

    def get_backtest_result(self, strategy_id: str) -> BacktestResult | None:
        """Return latest persisted backtest for a strategy, if present."""
        cached = self._backtests.get(strategy_id)
        if cached is not None:
            return cached
        latest = self._load_backtests([strategy_id]).get(strategy_id)
        if latest is not None:
            self._backtests[strategy_id] = latest
            strategy = self._strategies.get(strategy_id)
            if strategy is not None:
                strategy.stub_sharpe = latest.sharpe_ratio
                strategy.stub_cagr = latest.cagr
                strategy.stub_max_dd = latest.max_drawdown
                strategy.stub_win_rate = latest.win_rate
                strategy.stub_calmar = latest.calmar_ratio
                strategy.stub_corr_spy = latest.correlation_to_spy
        return latest

    def get_strategies_for_risk_profile(self, risk_profile_name: str) -> list[Strategy]:
        wanted = risk_profile_name.lower()
        return [s for s in self._strategies.values() if wanted in s.risk_profiles]

    def extract_from_paper(self, arxiv_id: str) -> Strategy | None:
        """[DEMO] Extract a CANDIDATE strategy passport from an arxiv paper.

        Runs `archimedes.services.arxiv_pipeline` (arxiv metadata + pypdf
        text, KnowledgeBase extract pattern, + Claude methodology
        synthesis), writes a self-describing strategy module into the
        strategies directory, refreshes, and returns the new Strategy.
        Returns None on any failure — no partial junk. The result is a
        CANDIDATE: it still needs human curation and the selection-bias
        gate before it can be promoted past CANDIDATE.

        Lazy import: arxiv_pipeline no longer imports this module (the old
        circular-import rationale is stale — review 2026-07-14); the import
        stays function-local because the pipeline pulls in the LLM backend and
        the lazy arxiv/pypdf machinery, none of which the provider's hot
        list/get paths should pay for at module load.
        """
        from archimedes.services.arxiv_pipeline import extract_strategy

        before = set(self._strategies)
        path = extract_strategy(arxiv_id, strategies_dir=self._strategies_dir)
        if path is None:
            logger.info("extract_from_paper(%s): no strategy produced", arxiv_id)
            return None

        self.refresh()
        for strat in self._strategies.values():
            if strat.strategy_code_path == str(path):
                return strat
        new_ids = set(self._strategies) - before
        if new_ids:
            return self._strategies[next(iter(new_ids))]
        logger.warning(
            "extract_from_paper(%s): file written but not picked up by refresh",
            arxiv_id,
        )
        return None


def resolve_strategies_dir(repo_root: Path | None = None) -> Path:
    """Resolve the strategies directory robustly across host and container —
    WITHOUT constructing a provider.

    Priority:
      1. explicit ``repo_root`` arg (tests)
      2. ``ARCHIMEDES_STRATEGIES_DIR`` env var — the deployment override
      3. first existing candidate among the known host repo layout and
         container-plausible mount points

    Factored out of ``default_provider`` (issue #1039) so cheap callers — the
    ``/health`` ``strategy_count`` probe — can locate the corpus without paying
    for ``LocalStrategyProvider`` construction (filesystem refresh + DB backtest
    load + a one-time unified-table sync side effect on every call).

    The original ``parents[3]`` math only holds for the host checkout layout —
    in the backend image ``__file__`` is ``/app/...`` so it resolved to a
    nonexistent ``/analytics-engine/strategies``. This keeps host dev identical
    while making the deployed path configurable.
    """
    if repo_root is not None:
        return repo_root / "analytics-engine" / "strategies"

    env_dir = os.getenv("ARCHIMEDES_STRATEGIES_DIR")
    if env_dir:
        return Path(env_dir)

    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "analytics-engine" / "strategies",  # host repo layout
        Path("/app/analytics-engine/strategies"),  # repo-root build context
        Path("/analytics-engine/strategies"),  # bind-mount at root
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # None found: fall back to the host-layout path so the warning log names a
    # sensible location rather than an arbitrary container root.
    return candidates[0]


def count_strategy_files() -> int:
    """Cheap count of baked strategy files in the resolved strategies dir.

    Counts ``*.py`` (excluding dunder helpers like ``__init__``), mirroring how
    ``LocalStrategyProvider`` discovers strategies (``glob("*.py")``) — but with
    NO filesystem refresh, DB load, or table sync. Used by ``/health`` to detect
    whether the strategy corpus is baked into the image: ``0`` means it is
    missing from the build (the Fargate-cutover regression, issue #1039).
    """
    d = resolve_strategies_dir()
    if not d.exists():
        return 0
    return sum(1 for p in d.glob("*.py") if not p.name.startswith("_"))


def default_provider(repo_root: Path | None = None) -> LocalStrategyProvider:
    """Construct a ``LocalStrategyProvider`` over the resolved strategies dir.

    Directory resolution lives in ``resolve_strategies_dir`` (shared with the
    cheap ``count_strategy_files`` probe). See that function for the resolution
    priority and the container-vs-host path rationale.
    """
    return LocalStrategyProvider(resolve_strategies_dir(repo_root))
