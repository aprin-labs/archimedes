"""Run analytics-engine backtests and persist latest metrics.

Usage:
  cd backend
  python -m archimedes.scripts.run_backtests
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from archimedes.db import get_session, init_db
from archimedes.services.backtest_mapper import (
    AnalyticsArtifactModel,
    canonical_artifact_hash,
    map_artifact_to_backtest_result,
)
from archimedes.services.backtest_repository import (
    SOURCE_PIPELINE_RUN_BACKTESTS,
    get_daily_returns,
    insert_backtest_if_missing,
)
from archimedes.services.curated_grading import grade_curated_library
from archimedes.services.strategy_provider import default_provider
from archimedes.services.vol_plausibility import (
    VolPlausibilityError,
    assess_strategy,
    compute_vol_stats,
    daily_returns_from_equity_curve,
)

logger = logging.getLogger(__name__)

# The confirmed-correct reference strategy for the Rule-B equity-baseline-match
# check (see services/vol_plausibility.py module docstring) — a stem match
# against analytics-engine/strategies/pipeline_buy_hold.py.
_BASELINE_STRATEGY_STEM = "pipeline_buy_hold"


@dataclass(frozen=True)
class RunConfig:
    operations: list[str]
    start: str
    end: str
    initial_cash: float
    tx_cost_bps: int
    slippage_bps: int


def _repo_root() -> Path:
    """Nearest ancestor that contains ``analytics-engine`` — layout-robust.

    Host layout: .../backend/archimedes/scripts/... with analytics-engine at
    the repo root (parents[3]). Container layout: /app/archimedes/scripts/...
    with /app/analytics-engine mounted (parents[2]). A fixed parents[3]
    resolved to "/" inside the container, so the prod backfill raised
    ModuleNotFoundError (same class of bug as #846's portfolio_backtester fix).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "analytics-engine").is_dir():
            return parent
    return here.parents[3]  # historical fallback; loudly wrong paths beat a crash here


def _analytics_strategy_dir(repo_root: Path) -> Path:
    return repo_root / "analytics-engine" / "strategies"


def _dir_writable(d: Path) -> bool:
    """Probe by actually creating a file — ``os.access`` lies in containers."""
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".write-probe-{os.getpid()}"
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False


def _artifact_dir(repo_root: Path) -> Path:
    """Where run artifacts get written. Must be writable, which the packaged
    default is NOT on Fargate: the image ships read-only for the nonroot user,
    so every backtest in the scheduled refresh died with
    ``PermissionError: /app/analytics-engine/artifacts/<run>.json`` — at the
    artifact write, BEFORE the DB insert — and the leaderboard silently froze
    at its last pre-cutover rows (2026-08-18 diagnosis of the stale-refresh
    incident; newest row was 2026-07-01).

    Resolution order:
      1. ``ARCHIMEDES_ARTIFACT_DIR`` env — the deployment's explicit choice
         (ecs.tf sets it to task-local scratch; the artifact JSON is also
         persisted into ``backtest_results.artifact_json``, which is the
         durable record, so ephemeral task storage is honest here).
      2. The repo-local ``analytics-engine/artifacts`` when writable — the
         dev/EC2 behaviour, unchanged.
      3. A stable path under the system temp dir, logged at WARNING — loud,
         because a silent location change is how artifacts "disappear".
    """
    env = os.getenv("ARCHIMEDES_ARTIFACT_DIR")
    if env:
        return Path(env)
    default = repo_root / "analytics-engine" / "artifacts"
    if _dir_writable(default):
        return default
    fallback = Path(tempfile.gettempdir()) / "archimedes-artifacts"
    logger.warning(
        "artifact dir %s is not writable (read-only container image?); writing run artifacts to %s instead. "
        "Set ARCHIMEDES_ARTIFACT_DIR to choose explicitly. The DB row's artifact_json remains the durable record.",
        default,
        fallback,
    )
    return fallback


def _ensure_analytics_import(repo_root: Path) -> None:
    analytics_src = repo_root / "analytics-engine" / "src"
    src_text = str(analytics_src)
    if src_text not in sys.path:
        sys.path.insert(0, src_text)


def _load_run_command(repo_root: Path):
    _ensure_analytics_import(repo_root)
    from archimedes_analytics_engine.cli import run_command

    return run_command


def _typed_error_types(repo_root: Path) -> tuple[type[BaseException], ...]:
    """Typed, explanatory strategy-run errors — log the message, not a stack trace.

    ``FeedArityError`` (issue #887) is the analytics engine failing closed with
    a passport-grade explanation (e.g. "pairs needs >= 2 instruments"); a
    traceback adds nothing. Returns an empty tuple when the engine is not
    importable so the caller degrades to generic exception handling.
    """
    try:
        _ensure_analytics_import(repo_root)
        from archimedes_analytics_engine.engine import FeedArityError

        return (FeedArityError,)
    except Exception:
        return ()


def _read_config() -> RunConfig:
    operations = [op.strip().upper() for op in os.getenv("BACKTEST_OPERATIONS", "SPY").split(",") if op.strip()]
    # Full curated evidence depth by default (~5,400 daily bars). The old
    # 2018 default silently produced 8-year runs with materially weaker DSR
    # statistics than the seed-time store — the scheduler and the CLI should
    # both grade on the same evidence window the passports were built on.
    start = os.getenv("BACKTEST_START", "2004-01-02")
    end = os.getenv("BACKTEST_END", datetime.now(UTC).date().isoformat())
    initial_cash = float(os.getenv("BACKTEST_INITIAL_CASH", "100000"))
    tx_cost_bps = int(os.getenv("BACKTEST_TX_COST_BPS", "10"))
    slippage_bps = int(os.getenv("BACKTEST_SLIPPAGE_BPS", "5"))
    return RunConfig(
        operations=operations,
        start=start,
        end=end,
        initial_cash=initial_cash,
        tx_cost_bps=tx_cost_bps,
        slippage_bps=slippage_bps,
    )


def _load_baseline_vol(strategy_by_path: dict) -> float | None:
    """Best-effort fetch of the pipeline_buy_hold realized vol for Rule B.

    Deliverable-2 permanent guard (audit 2026-07-27, services/vol_plausibility.py):
    every strategy's freshly-computed backtest is checked against BOTH the
    self-contained asset-class-band rule (Rule A) AND, when available, this
    pre-existing equity baseline (Rule B) — the check that specifically catches a
    diversified/non-equity strategy whose realized vol is suspiciously close to
    pure S&P equity (the exact signature of the confirmed single-feed-default
    fallback). Reads whatever ``pipeline_buy_hold`` last persisted BEFORE this
    run started — one query, done once, not per strategy.

    Returns ``None`` (never raises) when the reference strategy file is missing,
    has no persisted backtest yet (e.g. a brand new clone before the first ever
    run), or its own persisted series is degenerate/too short — every case logs
    a clear warning so the "Rule B skipped this run" gap is visible, not silent.
    """
    baseline = next(
        (s for s in strategy_by_path.values() if Path(s.strategy_code_path or "").stem == _BASELINE_STRATEGY_STEM),
        None,
    )
    if baseline is None:
        logger.warning(
            "vol-plausibility guard: no strategy file matching stem %r found — "
            "Rule B (equity-baseline match) skipped this run; only Rule A runs",
            _BASELINE_STRATEGY_STEM,
        )
        return None

    with get_session() as session:
        baseline_returns = get_daily_returns(session, baseline.id)

    if not baseline_returns:
        logger.warning(
            "vol-plausibility guard: %s has no persisted backtest yet — "
            "Rule B (equity-baseline match) skipped this run; only Rule A runs",
            _BASELINE_STRATEGY_STEM,
        )
        return None

    stats = compute_vol_stats(baseline_returns)
    if stats.annualized_vol is None:
        logger.warning(
            "vol-plausibility guard: %s's persisted series is degenerate/too short "
            "(n_obs=%d) — Rule B (equity-baseline match) skipped this run; only Rule A runs",
            _BASELINE_STRATEGY_STEM,
            stats.n_obs,
        )
        return None

    return stats.annualized_vol


def _computed_at_from_payload(artifact_payload: dict) -> datetime | None:
    """Parse the analytics-engine artifact's own ``timestamp_utc`` (cli.py's
    ``run_command``) — when the backtest was actually COMPUTED, as opposed to
    when this row lands in the DB (``created_at``). Usually near-identical for
    this script (compute and persist in the same call), but this is the exact
    field that makes ``computed_at`` meaningful for
    ``seed_backtests_from_artifacts.py``, which loads artifacts written by an
    earlier run. Returns ``None`` (never raises) on a missing/malformed field
    — the caller falls back to "now", the same honest default as before this
    column existed.
    """
    raw = artifact_payload.get("timestamp_utc")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _daily_returns_for_operation(artifact_payload: dict, operation: str | None) -> list[float] | None:
    """Raw ``daily_returns`` for one named operation, read directly off the
    parsed artifact payload — ``None`` if not found/empty.

    ``AnalyticsArtifactModel``/``EngineMetricsModel`` (``backtest_mapper.py``)
    declare ``extra="ignore"`` and never define a ``daily_returns`` field, so
    the raw per-bar returns array the analytics engine actually wrote
    (``archimedes_analytics_engine.cli.run_command``'s ``asdict(bt_result)``,
    which includes ``daily_returns``) only survives in the plain-dict
    ``artifact_payload`` from ``json.loads`` — not in the pydantic-validated
    ``artifact`` object. Matches by operation name (case-insensitive), the same
    identity ``select_operation_result`` (``backtest_mapper.py``) used to pick
    the row ``map_artifact_to_backtest_result`` mapped from — so this reads the
    CHOSEN operation's own series, never whichever entry happens to sit first
    in ``results`` (see ``get_daily_returns`` in ``backtest_repository.py``,
    which — unlike ``select_operation_result`` — does exactly that and is the
    reason this guard cannot just reuse it).
    """
    if not operation:
        return None
    wanted = operation.upper()
    for row in artifact_payload.get("results", []):
        if str(row.get("operation", "")).upper() != wanted:
            continue
        raw = row.get("metrics", {}).get("daily_returns")
        return [float(v) for v in raw] if raw else None
    return None


def _payload_with_selected_operation_first(artifact_payload: dict, operation: str | None) -> dict:
    """``artifact_payload`` with the chosen operation's row moved to the front of
    ``results`` — everything else preserved, order of the remaining rows intact.

    Exists so that ``backtest_repository.get_daily_returns()``, which returns the
    first non-empty ``daily_returns`` and ignores the ``operation`` column, reads
    back the SAME series the write-time vol guard validated. Returns the payload
    unchanged when there is nothing to do (no operation, single/zero result, or
    the chosen row is already first), so the common single-operation case is
    byte-identical and does not perturb ``canonical_artifact_hash``.
    """
    if not operation:
        return artifact_payload
    results = artifact_payload.get("results")
    if not isinstance(results, list) or len(results) < 2:
        return artifact_payload

    wanted = operation.upper()
    idx = next(
        (i for i, row in enumerate(results) if str(row.get("operation", "")).upper() == wanted),
        None,
    )
    if idx is None or idx == 0:
        return artifact_payload

    reordered = dict(artifact_payload)
    reordered["results"] = [results[idx], *results[:idx], *results[idx + 1 :]]
    return reordered


def run_backtests() -> dict:
    repo_root = _repo_root()
    strategy_dir = _analytics_strategy_dir(repo_root)
    artifact_dir = _artifact_dir(repo_root)
    cfg = _read_config()

    run_command = _load_run_command(repo_root)
    typed_errors = _typed_error_types(repo_root)

    provider = default_provider(repo_root=repo_root)
    strategy_by_path = {
        Path(s.strategy_code_path).resolve(): s for s in provider.list_strategies() if s.strategy_code_path
    }

    init_db()

    # Deliverable-2 permanent guard: fetch the pre-existing equity baseline ONCE
    # (not per strategy) so every freshly-computed artifact below can be checked
    # against both plausibility rules before it is persisted. See
    # services/vol_plausibility.py's module docstring for Rule A / Rule B.
    baseline_vol = _load_baseline_vol(strategy_by_path)

    inserted = 0
    skipped = 0
    failed = 0
    errors: dict[str, str] = {}

    for strategy_file in sorted(strategy_dir.glob("*.py")):
        if strategy_file.name.startswith("_"):
            continue

        strategy = strategy_by_path.get(strategy_file.resolve())
        if strategy is None:
            logger.warning("skip %s: no strategy_id from provider", strategy_file)
            failed += 1
            continue

        try:
            out = run_command(
                operations=cfg.operations,
                start=cfg.start,
                end=cfg.end,
                initial_cash=cfg.initial_cash,
                tx_cost_bps=cfg.tx_cost_bps,
                slippage_bps=cfg.slippage_bps,
                artifact_dir=artifact_dir,
                strategy_path=strategy_file,
            )

            artifact_path = Path(str(out["artifact_path"]))
            artifact_json = artifact_path.read_text(encoding="utf-8")
            artifact_payload = json.loads(artifact_json)
            artifact = AnalyticsArtifactModel.model_validate(artifact_payload)
            mapped, selected_operation = map_artifact_to_backtest_result(
                artifact,
                strategy_id=strategy.id,
            )

            # Persist the SELECTED operation first, and hash what we persist.
            #
            # Making the guard read the chosen operation's series (above) only
            # closed half the gap: backtest_repository.get_daily_returns() —
            # what the rigor gate and the audit script actually read back —
            # ignores the `operation` column and returns the FIRST non-empty
            # daily_returns in artifact_json["results"]. So with
            # BACKTEST_OPERATIONS="NIKKEI,SPY" the guard would validate SPY
            # while every downstream consumer graded NIKKEI. The divergence
            # would just have moved rather than closed, and a guard that
            # validates a different series than the gate reads is worse than
            # no guard: it certifies the wrong thing.
            #
            # Reordering at write time makes "first entry" and "selected
            # operation" the same row by construction, so the naive reader is
            # correct without changing it. Fixing get_daily_returns() to honour
            # the operation column is the better long-term shape, but it is a
            # shared reader whose behaviour change would re-grade already
            # persisted rows — that belongs in its own PR, not this guard's.
            #
            # No-op under the current default (BACKTEST_OPERATIONS="SPY" ⇒ one
            # result), so no content_hash churn and no duplicate re-inserts for
            # existing rows; it only starts mattering the moment a second
            # operation is configured, which is exactly when it must be right.
            artifact_payload = _payload_with_selected_operation_first(artifact_payload, selected_operation)
            artifact_json = json.dumps(artifact_payload)
            content_hash = canonical_artifact_hash(artifact_payload)

            # Deliverable-2 permanent guard (audit 2026-07-27): refuse to persist
            # a backtest whose realized vol is wildly inconsistent with the
            # strategy's own declared ASSET_UNIVERSE — this is exactly the
            # confirmed "backtesting the wrong asset" defect class (e.g.
            # capital_preservation_tbill declaring BIL but reading pure equity
            # vol). Checked BEFORE insert_backtest_if_missing so a flagged
            # artifact is never written, loudly, fail-closed.
            #
            # Pull the CHOSEN operation's raw daily_returns straight off the
            # artifact payload rather than re-deriving them from
            # mapped.equity_curve via pct-change: select_operation_result()
            # (backtest_mapper.py) picks the row by NAME (searches for "SPY"
            # regardless of position), but this file's own _load_baseline_vol()
            # — and every other downstream reader, via
            # backtest_repository.get_daily_returns() — reads artifact
            # results[0], which is not guaranteed to be the same row once
            # BACKTEST_OPERATIONS lists more than one op (e.g.
            # "NIKKEI,SPY" puts SPY, the chosen op, at index 1). Deriving from
            # mapped.equity_curve happened to coincide with the chosen op
            # today, but named lookup is what actually keeps this guard
            # checking the row that gets persisted and later re-read — falling
            # back to the equity-curve derivation only if the raw field is
            # absent (e.g. an older artifact schema).
            candidate_returns = _daily_returns_for_operation(artifact_payload, selected_operation)
            if not candidate_returns:
                candidate_returns = daily_returns_from_equity_curve(mapped.equity_curve)
            verdict = assess_strategy(strategy.asset_universe, candidate_returns, baseline_vol=baseline_vol)
            if verdict.status in ("flagged", "degenerate"):
                raise VolPlausibilityError(
                    f"strategy {strategy.id} ({strategy.paper_title}) declares ASSET_UNIVERSE="
                    f"{strategy.asset_universe!r}; realized vol/return check status={verdict.status!r}: "
                    f"{'; '.join(verdict.reasons)}"
                )

            with get_session() as session:
                _, was_inserted = insert_backtest_if_missing(
                    session,
                    strategy_id=strategy.id,
                    content_hash=content_hash,
                    result=mapped,
                    run_id=artifact.run_id,
                    operation=selected_operation,
                    artifact_json=artifact_json,
                    source_pipeline=SOURCE_PIPELINE_RUN_BACKTESTS,
                    computed_at=_computed_at_from_payload(artifact_payload),
                )
                session.commit()

            if was_inserted:
                inserted += 1
                logger.info("inserted backtest row: %s (%s)", strategy.paper_title, strategy.id)
            else:
                skipped += 1
                logger.info("skip duplicate content hash: %s (%s)", strategy.paper_title, strategy.id)

        except VolPlausibilityError as exc:
            # Loud + fail-closed by construction: this exception is only ever
            # raised AFTER the artifact was computed but BEFORE
            # insert_backtest_if_missing — so nothing was written. ERROR (not
            # WARNING) because this is the exact defect class the whole guard
            # exists to catch, not a routine engine limitation.
            failed += 1
            errors[strategy.id] = f"VolPlausibilityError: {exc}"
            logger.error("REFUSING to persist backtest for %s: %s", strategy_file.name, exc)
        except Exception as exc:
            failed += 1
            if typed_errors and isinstance(exc, typed_errors):
                # Typed fail-closed error from the engine (e.g. FeedArityError,
                # issue #887): the message is the explanation — log it clean
                # and carry it in the summary instead of dumping a stack trace.
                errors[strategy.id] = f"{type(exc).__name__}: {exc}"
                logger.warning("backtest failed for %s: %s: %s", strategy_file.name, type(exc).__name__, exc)
            else:
                logger.exception("backtest failed for %s: %s", strategy_file, exc)

    summary: dict = {
        "inserted": inserted,
        "skipped": skipped,
        "failed": failed,
    }
    if errors:
        summary["errors"] = errors

    # ── Grade what was just measured (#1746 / PR-B) ─────────────────────────
    # Backtesting and grading are ONE event (docs/adr/rigor-verdict-of-record.md).
    # New evidence for a curated strategy is exactly the moment its rigor verdict
    # should be produced and stored — and the only moment, because no read
    # surface is allowed to compute one. The cohort is the full library either
    # way, so this grades every curated row, not only the ones that inserted:
    # a strategy whose row was content-hash-deduped still gets a verdict if it
    # never had one, and a cohort-scoped input (PBO, average correlation) that
    # moved because SOMEONE ELSE got a new row is a real reason to re-grade the
    # neighbours. Failing to grade does not fail the run: the rows are written,
    # the evidence is real, and the passports keep the verdict they already had.
    #
    # A grading pass that could not reach its data writes NOTHING and returns a
    # summary carrying an `error` key (services/curated_grading — it must never
    # blank the library's verdicts just because the DB blinked), so both the
    # except branch below and that key put the same shape in `summary["graded"]`
    # and the runbook's "check the summary" step catches either.
    try:
        with get_session() as session:
            grade_summary = grade_curated_library(session)
            session.commit()
        summary["graded"] = grade_summary.as_dict()
    except Exception as exc:
        logger.exception("curated grading failed after the backtest run: %s", exc)
        summary["graded"] = {"error": f"{type(exc).__name__}: {exc}"}

    logger.info("backtest run summary: %s", summary)
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    summary = run_backtests()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
