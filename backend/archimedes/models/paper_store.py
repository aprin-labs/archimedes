"""Paper-trading deployments and their forward return ledgers (MVP: verdict → paper-trade).

Design (Dan, 2026-08-18, validated with one amendment): a paper deployment is
an incremental forward run of the BACKTEST engine's semantics — the engine
that graded the verdict the user paid for — one bar per day from the deploy
date. NOT the live signal evaluator: the divergence audit (F2/F3) established
it grades a different strategy.

The amendment: this is deliberately a SIBLING of the daily-returns machinery,
not a tenant of it. ``strategy_daily_returns``' own documented law is
"re-measuring a stem replaces that stem's rows wholesale" — a measurement
store. A paper ledger obeys the OPPOSITE law: append-only per deployment,
never rewritten, because it is a user-facing track record. Same row shape, so
every metric function that eats (date, daily_return) series works unchanged.

Provenance: ``spec_json`` snapshots the strategy spec AT DEPLOY TIME. The
strategy row's spec can be regenerated later; the ledger must keep grading
the spec the user actually deployed — the same lesson as backtest provenance
(#1220): a row must not silently change what it asserts about the past.
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from archimedes.models.chat import Base

STATUS_ACTIVE = "active"
STATUS_STOPPED = "stopped"

# ─── paper_marks granularity + retention (intraday design §3) ──────────

GRANULARITY_RAW = "raw"
GRANULARITY_HOURLY = "hourly"

# Retention defaults. Every one is an env knob because the cadence is a
# product call and the arithmetic in §3.2 moves with it — but the DEFAULTS
# are the policy, and they exist before the first row is written rather than
# after the first bill. See PaperMark's docstring for why that ordering is
# the whole point.
DEFAULT_RAW_RETENTION_DAYS = 7
DEFAULT_HOURLY_RETENTION_DAYS = 90
# ~7x the crypto-24/7 steady-state ceiling (2,664 rows/deployment). Not a
# capacity plan — a runaway-loop tripwire that fires in minutes.
DEFAULT_MAX_ROWS_PER_DEPLOYMENT = 20_000
DEFAULT_MARKS_INTERVAL_MINUTES = 15
DEFAULT_MAX_STALENESS_MINUTES = 60
#: Publication outcome for one paper decision (#1575 §7). Every decision the
#: replay detects lands in exactly one of these, and the settle asserts the
#: accounting identity over them — a decision that falls out of the pipeline
#: uncounted is the failure mode that produces a silent zero.
TRACE_PUBLISHED = "published"
TRACE_FAILED = "failed"
TRACE_UNOWNED = "unowned"
TRACE_DISABLED = "disabled"
TRACE_STATUSES = (TRACE_PUBLISHED, TRACE_FAILED, TRACE_UNOWNED, TRACE_DISABLED)
#: The states a later settle re-attempts. ``unowned`` is deliberately absent:
#: it is a data problem, not a transient one, and retrying it forever would
#: turn a loud ERROR into a recurring one that operators learn to ignore.
TRACE_RETRYABLE = (TRACE_FAILED, TRACE_DISABLED)


def _new_id() -> str:
    return secrets.token_hex(16)


class PaperDeployment(Base):
    """One user's paper-trade of one strategy, from one start date."""

    __tablename__ = "paper_deployments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    # Width normalized to VARCHAR(128) (schema-relations Phase 1) to match
    # strategy_store.id's column width ahead of the FK below — same reasoning
    # strategy_store.id itself documents (issue #1028): actual values are
    # content_hash[:16] (16 chars), so this is headroom, not a live-data
    # change. FK retrofit: closes the gap this module's own docstring already
    # named ("Same pattern as strategy_store" — strategy_store carries this
    # FK, this table didn't). Added NOT VALID in fb8d0bae8112; historical
    # orphans (if any) are left un-enforced rather than blocking the deploy —
    # VALIDATE CONSTRAINT runs, gated on a live orphan count, in the separate
    # follow-up revision 9c2e7b5a1f4d.
    strategy_id: Mapped[str] = mapped_column(String(128), ForeignKey("strategy_store.id"), nullable=False)
    # Ownership carries BOTH identity columns during the canonical-identity
    # transition (#1194): wallet for the SIWE model live today, user id for
    # Better Auth once it lands. Same pattern as strategy_store. Both FKs
    # retrofitted in schema-relations Phase 1 (#1438) — added NOT VALID in
    # fb8d0bae8112, validated (gated on a live orphan count) in 9c2e7b5a1f4d.
    owner_wallet: Mapped[str | None] = mapped_column(
        String(42), ForeignKey("wallet_identities.wallet_address"), nullable=True
    )
    # CASCADE (issue #1367, D3): a paper deployment is a private per-user
    # ledger — no other account reads or depends on someone else's paper
    # trades (unlike strategy_store/strategy_passports, which can be public
    # marketplace/audit artifacts other users rely on). Deleting the owning
    # account should remove it outright, and PaperDailyReturn already
    # cascades off `paper_deployments.id` (see below), so the whole ledger
    # goes with it — no orphaned rows either direction.
    #
    # #1438 created this FK (`fk_paper_deployments_owner_user_id`) with
    # ondelete="SET NULL", matching the five sibling ownership columns
    # `b7e3f1a2c9d4` established. `85ca5310b7a1` ALTERS that constraint to
    # CASCADE — it does not create a second one. The FK's existence is #1438's;
    # only its ON DELETE action is this PR's.
    owner_user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # The deploy-time snapshot of the validated spec — the thing being graded.
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    deployed_at: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    # Set when a replay disagrees with already-written ledger rows AND the
    # disagreement is ATTRIBUTABLE to the data, not to us: the row carries the
    # same grading-engine version the replay just ran under, so the only thing
    # left that can have moved is upstream history (#1449). The ledger is never
    # rewritten; this flags that a fresh replay would tell a different story —
    # surfaced, not hidden.
    #
    # Narrower than it was before #1449, deliberately. It used to fire on ANY
    # disagreement, which meant a grading-side cost-model change (#1379's
    # slippage floor) would stamp every open deployment at once and tell every
    # user their track record had restated — a claim about THEM for a change
    # that was ours. Those cases now land on ``engine_regrade_at`` instead.
    drift_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when a replay disagrees with already-written ledger rows and the
    # cause is the GRADING ENGINE, not the data (#1449). Two ways in:
    #
    #   1. the disagreeing row carries a grading-engine version different from
    #      the one this replay ran under — an expected, disclosed re-grade;
    #   2. the row carries NO version at all (written before ``engine_version``
    #      existed), so the disagreement cannot be attributed either way.
    #
    # Case 2 is annotated rather than alarmed for the same reason case 1 is:
    # calling it a data restatement would assert something we cannot show. Both
    # are still counted, logged, and reported on the deployment payload — what
    # is withheld is the attribution, never the fact.
    engine_regrade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Opt-in on-chain anchoring for this deployment's reasoning traces (#1575
    # §6). Default false, and PER DEPLOYMENT rather than a global switch,
    # because the commit-reveal `reveal()` puts `portfolio_before/after` —
    # the user's holdings — on-chain permanently. Anchoring a private
    # simulation by default would defeat #1556's ownership gate on purpose,
    # irreversibly. Both this and PAPER_TRACE_ANCHOR must be true to anchor.
    anchor_traces: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Set when a decision this deployment made did NOT get a published trace.
    # Distinct from drift_detected_at: that is the ledger disagreeing with
    # itself, this is the provenance claim having a hole in it.
    trace_gap_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when a re-replay produces a decision for a date whose trace is
    # already published. The trace is NOT rewritten — the hash is the point —
    # so the disagreement is stamped and surfaced, exactly as the ledger's own
    # drift is.
    trace_drift_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ── The position-set cache (intraday design §4.1) ──────────────────
    # WRITTEN by the daily advance, READ by the 15-minute marks loop, and by
    # nothing else. That one-way arrow is the entire safety argument for
    # intraday marks: the marks loop cannot change what the strategy does
    # because it has no path to do so (§4.0). It re-decides nothing — it
    # applies prices to a position set the DAILY replay established.
    #
    # A column rather than Redis or an in-process dict because the marks loop
    # runs in a DIFFERENT PROCESS (the runner box) from the advance loop (the
    # web tier), and must survive a restart of either. Nullable: every
    # deployment that existed before this shipped has no cache until its next
    # advance, and "no cache" must read as "no marks yet", never as a
    # fabricated flat line. Shape is documented on
    # ``paper_trading.PositionSet``.
    position_cache_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_cache_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_paper_deployments_owner_wallet", "owner_wallet"),
        Index("ix_paper_deployments_strategy", "strategy_id"),
    )


class PaperDailyReturn(Base):
    """One appended daily observation of one deployment. Append-only by law."""

    __tablename__ = "paper_daily_returns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("paper_deployments.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    daily_return: Mapped[float] = mapped_column(Float, nullable=False)
    appended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    # WHICH GRADING ENGINE produced this number (#1449) —
    # ``fusion_evaluator.GRADING_ENGINE_VERSION`` at append time. The row is
    # append-only like every other column here, so this is the durable record of
    # the cost basis and replay semantics the user was actually shown.
    #
    # Nullable, and NEVER backfilled: rows written before this column existed
    # were graded by a build that did not record its own version, and stamping
    # them with today's string would be inventing provenance to make a
    # comparison come out clean — the exact class of claim this ledger exists to
    # oppose. NULL means "unrecorded", and ``paper_trading.classify_drift`` gives
    # it its own bucket rather than folding it into either answer.
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("deployment_id", "date", name="uq_paper_daily_returns_dep_date"),
        Index("ix_paper_daily_returns_dep", "deployment_id"),
    )


class PaperDecisionTrace(Base):
    """One paper DECISION and what happened when we tried to publish its trace.

    The idempotency key AND the loud-failure record, in one row — deliberately
    the same table, because a design where the "published" bookkeeping is
    durable and the "failed" bookkeeping is a log line degrades into a silent
    zero the first time Redis blips. ``advance_deployment`` re-derives every
    historical decision on every settle (the engine is a position FSM with no
    serialisable state), so without a durable key each deployment would
    republish its whole decision history daily.

    ``(deployment_id, decision_date)`` is the key, not the leg: the universe
    runs as independent dollar sleeves, so one decision date can carry several
    symbol legs, and the user-visible unit of "a move" is the date. The legs
    live inside the trace body's ``trades_executed``.
    """

    __tablename__ = "paper_decision_traces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    deployment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("paper_deployments.id", ondelete="CASCADE"), nullable=False
    )
    decision_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Null for every non-published status: a trace id we never wrote is not a
    # trace id, and a placeholder here would make `trace_coverage` countable
    # but wrong.
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # "settle" or "backfill" — the value that was HASHED INTO the published
    # trace. Stored so a later settle can re-derive that trace's hash exactly
    # and tell "the replay now decides differently" from "this row was written
    # on a different settle". Without it every re-replay would look like drift.
    provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint("deployment_id", "decision_date", name="uq_paper_decision_traces_dep_date"),
        Index("ix_paper_decision_traces_dep", "deployment_id"),
    )


class PaperMark(Base):
    """One intraday mark-to-market of one deployment's open position set.

    **Marks are NOT the track record.** ``paper_daily_returns`` stays
    append-only-by-law and stays the recorded paper track record (Arc testnet,
    no real funds). This table is a DECORATION WITH A TTL and is safe to delete
    wholesale — that single sentence is what makes the aggressive retention
    policy below safe, and it is why the third tier of that policy is
    ``DELETE`` rather than a rollup.
    Beyond 90 days there is nothing worth aggregating to: the daily close is
    already stored, authoritatively and permanently, one table over. Rolling
    marks up to daily would be a second, less-trustworthy source of truth for
    a fact the ledger already owns.

    **Why the retention policy ships in the same migration as the table.**
    ``backtest_results`` reached 6.3 GB because it stores full curve blobs per
    row with no retention policy and no size alarm — nobody chose that, it
    accumulated. A marks table is a higher-volume version of the same shape
    (a crypto-24/7 deployment writes 96 rows/day), so the policy exists before
    the first row rather than after the first bill. Under the three tiers the
    steady state is BOUNDED, not linear in time: ~2,664 rows/deployment for a
    24/7 universe, ~543 for an equity-session one.

    **Every honesty-bearing fact is a stored column, not a render-time
    inference** (§2.4):

      - ``ts`` is the UPSTREAM OBSERVATION time, never the write time. A mark
        written at 14:47 from a 14:32 bar is a 14:32 mark. For a multi-leg
        universe it is the OLDEST contributing leg's bar time — a mark is only
        as current as the stalest price inside it.
      - ``prices_json`` records what was ACTUALLY OBSERVED, per symbol. For a
        mixed equity+crypto universe outside market hours some legs are fresh
        and some are not; storing the per-symbol map keeps that recoverable
        instead of collapsing it into one opaque number. A leg too stale to
        use is ABSENT here rather than carried at a stale price.
      - ``source`` is ``provider_name()`` at fetch time, so a future vendor
        swap cannot retroactively relabel history (same reasoning as
        ``asset_daily_bars.source``).
      - ``is_delayed`` is set by the FETCH PATH from what the provider
        declares about its own feed, so the UI's "delayed" badge reads a fact
        rather than guessing from a timestamp.

    ``portfolio_value`` is an INDEX (1.0 == deploy-time capital), not dollars.
    ``PaperDeployment`` has no notional/capital column — there is no deployed
    capital amount anywhere in this system — so rendering "$10,347" would
    require inventing the $10,000, and an invented number on a track-record
    page is the exact class of claim this product exists to oppose. The index
    matches how ``deployment_summary`` already computes ``equity_index``.

    ``granularity`` marks rolled-up rows IN PLACE rather than in a second
    table: the daily rollup rewrites the surviving row as ``'hourly'`` and
    deletes the ``'raw'`` rows it covers. One table, one query shape, and the
    unique constraint makes a re-run of the rollup a no-op instead of a
    duplicate.
    """

    __tablename__ = "paper_marks"

    # BIGSERIAL on Postgres (a 15-min cadence across many deployments makes a
    # 32-bit key a real, if distant, ceiling), plain INTEGER on SQLite — a
    # SQLite BIGINT primary key is NOT a rowid alias and therefore does not
    # autoincrement, which would break every hermetic test on this table.
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("paper_deployments.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prices_json: Mapped[str] = mapped_column(Text, nullable=False)
    portfolio_value: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    is_delayed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    granularity: Mapped[str] = mapped_column(String(8), nullable=False, default=GRANULARITY_RAW)

    __table_args__ = (
        UniqueConstraint("deployment_id", "ts", "granularity", name="uq_paper_marks_dep_ts_gran"),
        Index("ix_paper_marks_dep_ts", "deployment_id", "ts"),
    )


#: Direction of one agent-driven paper trade. Deliberately the SAME two literals
#: ``models.portfolio.TradeDirection`` uses — the venue writes ``.value``, so a
#: paper row and a vault trade name a buy the same way.
AGENT_TRADE_BUY = "buy"
AGENT_TRADE_SELL = "sell"


class PaperAgentTrade(Base):
    """One trade the AGENT TICK LOOP decided for one paper deployment (#1410).

    **This table is not the track record, and it is not a valuation.**
    ``paper_daily_returns`` remains the append-only ledger produced by the
    graded replay, and ``paper_marks`` remains the mark-to-market surface;
    #1410's third anti-goal is "do NOT change paper-trading valuation math" and
    nothing here writes to either. What this table records is *who decided
    what, and why* when the vault's own decision loop — signal FSM → aggregated
    target weights → diff against current positions → trades — is pointed at a
    paper deployment instead of a vault. That loop had zero vaults to run
    against, so it had zero validation; this is where its output becomes
    inspectable.

    **Weights, never dollars.** ``PaperDeployment`` has no notional/capital
    column — there is no deployed capital amount anywhere in this system (see
    ``PaperMark``'s docstring, which refuses to render "$10,347" for the same
    reason). So a trade is stored as the portfolio FRACTION that moved:
    ``prior_weight`` → ``target_weight``, with ``weight_delta`` their signed
    distance. Rendering a dollar size would require inventing the notional, and
    an invented number next to a track record is the exact class of claim this
    product exists to oppose.

    **Positions are the fold of this ledger.** ``PaperVenue.read_portfolio``
    replays these rows from an all-cash start rather than caching a position
    blob, for the same reason ``strategy_signal_evaluator`` derives position
    state by replay rather than persisting it: a pure function of an
    append-only ledger cannot be double-advanced, cannot silently reset on
    restart, and needs no reconciliation.

    KNOWN LIMIT, stated because this table's whole job is honest provenance:
    the folded position is a SIGNAL-STATE book, not a marked-to-market one.
    Between ticks the recorded weights do not move with prices, so the drift
    this venue can see is drift the SIGNALS created — never drift a price move
    created. That means the paper venue exercises the signal→weights→diff→trade
    mechanic faithfully and does NOT exercise price-drift-triggered
    rebalancing. Closing it would mean this table growing a second opinion
    about what a paper portfolio is worth, alongside ``paper_marks``; that is a
    deliberate non-goal here, not an oversight.

    Provenance columns are what make a row a claim rather than a number:
    ``tick_id`` (which agent tick), ``decided_at`` (when), ``signal_strategy_id``
    / ``signal_state`` / ``signal_reason`` (which signal, in what state, and the
    evaluator's own words for why), and ``spec_hash`` (which deployed spec
    produced the signal — the deployment's ``spec_json``, not the strategy
    row's, which can be regenerated later).
    """

    __tablename__ = "paper_agent_trades"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    deployment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("paper_deployments.id", ondelete="CASCADE"), nullable=False
    )
    #: The agent tick that produced this trade. Not nullable: a trade that
    #: cannot name its tick is exactly the thing #1410 asked this table to make
    #: impossible.
    tick_id: Mapped[str] = mapped_column(String(32), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(4), nullable=False)
    prior_weight: Mapped[float] = mapped_column(Float, nullable=False)
    target_weight: Mapped[float] = mapped_column(Float, nullable=False)
    #: Signed: positive is a buy, negative is a sell. Stored rather than derived
    #: from the two weights so the row still reads correctly if a later change
    #: alters how weights are rounded.
    weight_delta: Mapped[float] = mapped_column(Float, nullable=False)
    #: Which strategy's signal drove this leg. Nullable only for the USDC leg of
    #: a rebalance, which no strategy votes on — it is the residual.
    signal_strategy_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signal_state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    signal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    __table_args__ = (
        # One row per (deployment, tick, symbol). A tick that is somehow applied
        # twice writes nothing the second time instead of doubling a position —
        # the same "a re-run is a no-op, not a duplicate" rule paper_marks and
        # paper_decision_traces already follow.
        UniqueConstraint("deployment_id", "tick_id", "symbol", name="uq_paper_agent_trades_dep_tick_symbol"),
        Index("ix_paper_agent_trades_dep_decided", "deployment_id", "decided_at"),
    )
