"""Redis state store — persists agent state across ticks.

Stores the latest regime classification and agent heartbeat in Redis
so the API layer and frontend can read live agent state.

Two distinct signals live here, under two distinct keys (issue #659):
  - ``KEY_REGIME`` — the *exogenous* market regime from a regime detector
    (VIX / momentum / spreads). May be absent until a detector is wired.
  - ``KEY_ENSEMBLE_CONSENSUS`` — the *endogenous* strategy-ensemble consensus
    derived from ``flat_pct``. Always available once the agent ticks. This is
    "how decisive is the ensemble", NOT a market regime, and must not shadow
    the market-regime key.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime

import redis.asyncio as aioredis

from archimedes.models.regime import EnsembleConsensus, RegimeClassification

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Keys
KEY_REGIME = "archimedes:regime:current"
KEY_ENSEMBLE_CONSENSUS = "archimedes:ensemble_consensus"
KEY_HEARTBEAT = "archimedes:agent:heartbeat"
KEY_LAST_REBALANCE_PREFIX = "archimedes:agent:last_rebalance:"
KEY_TRACE_PREFIX = "archimedes:trace:"
KEY_TRACE_INDEX = "archimedes:trace:index"
KEY_SIWE_NONCE_PREFIX = "archimedes:auth:nonce:"

# How many trace blobs ``list_traces`` fetches per MGET (#1577).
#
# The read it batches is deliberately unbounded at the index (the caller's
# window is applied only after filtering, so the store cannot know how far to
# read), which rules out a single MGET over everything: Redis executes MGET as
# one blocking command on its single thread, so an index-sized key list would
# turn one client's listing into a latency spike for every other client, and
# the entire reply would land in this process at once. A fixed batch keeps
# both the per-command cost and the peak buffer flat while still collapsing
# the round-trip count from O(index) to O(index / 500).
TRACE_MGET_BATCH = 500

# Reveal-reconciliation durable index (#1353, hardening the #1276 pass).
#
# ``list_recent_traces(N)`` bounds its scan at the newest N entries of
# KEY_TRACE_INDEX — correct for a per-tick cost bound, but a dangling
# commitment that ages past that window is neither retried nor terminaled,
# just silently unseen. These three keys are maintained INSIDE ``save_trace``
# itself — the single write choke-point every trace record (dangling or not,
# agent_runner.py / traces_routes.py / strategies_routes.py alike) passes
# through — so a record enters and leaves them exactly when its
# dangling-ness actually changes, with no separate "don't forget to update
# the index" call site to miss:
#   KEY_TRACE_RECONCILE_PENDING  — SET of trace_hash currently dangling
#                                   (commit+trade, no reveal, not closed).
#                                   SMEMBERS is therefore an EXACT dangling
#                                   scan regardless of how much trace history
#                                   has accumulated — unlike the bounded scan.
#   KEY_TRACE_RECONCILE_TERMINAL — SET of trace_hash that gave up for good.
#                                   Members are NEVER removed (terminal is a
#                                   state, not a deletion), which makes SCARD
#                                   of it a CUMULATIVE lifetime counter, not a
#                                   level: it only ever goes up for the life of
#                                   the Redis keyspace, and a single historical
#                                   give-up pins it above zero permanently.
#                                   Read its RATE OF INCREASE, never its
#                                   absolute value. O(1) to read and published
#                                   on /health — see the honesty note on
#                                   ``get_reveal_reconcile_terminal_count`` for
#                                   what "alertable" does and does not mean
#                                   here today.
#   KEY_TRACE_RECONCILE_FIRST_SEEN — HASH trace_hash -> ISO timestamp of the
#                                   FIRST save_trace call that observed the
#                                   record dangling, written with HSETNX (set
#                                   once, never overwritten while still
#                                   dangling). This lives OUTSIDE the trace
#                                   record's own JSON blob on purpose: the
#                                   persisted ``reveal_reconcile_attempts``
#                                   counter is only as durable as save_trace's
#                                   own write succeeding, so a compound
#                                   failure (a broken Redis write path on top
#                                   of a broken reveal) can leave that counter
#                                   permanently stuck below its cap. A
#                                   max-age bound read from here is
#                                   independent of that counter's fate.
#
# #1403 review hardened this further: ``mark_reveal_reconcile_terminal``
# writes the TERMINAL/PENDING/FIRST_SEEN transition directly (three small,
# independent ops), NOT only as a side effect of the full trace blob's own
# ``SET`` inside save_trace — a broken write path on that specific (larger)
# key could otherwise keep failing forever while the terminal verdict itself
# is recomputed correctly every tick but never sticks, so the guarded
# ``reveal()`` call keeps firing regardless. ``_reconcile_dangling_reveals``
# cross-checks ``list_reveal_reconcile_terminal_hashes`` against every
# candidate each pass so a record closed this way can never re-enter the
# retry loop even while its JSON blob still reads "pending".
KEY_TRACE_RECONCILE_PENDING = "archimedes:trace:reconcile:pending"
KEY_TRACE_RECONCILE_TERMINAL = "archimedes:trace:reconcile:terminal"
KEY_TRACE_RECONCILE_FIRST_SEEN = "archimedes:trace:reconcile:first_seen"


# Mirrors agent_runner._RECONCILE_CLOSED_STATES exactly (duplicated, not
# imported: agent_runner imports FROM this module already — chain/ -> services/
# — so importing back would be circular. Both are covered by
# ``TestReconcileClosedStatesStaySynced`` in test_redis_state.py, which fails
# loudly the moment the two frozensets diverge.)
_RECONCILE_CLOSED_STATES = frozenset({"terminal", "reconciled", "reconciled_from_chain"})


def is_dangling_reveal(record: dict) -> bool:
    """True iff a persisted trace is a dangling commitment awaiting a reveal retry.

    Canonical source for this predicate (#1353) — ``agent_runner._needs_reveal_reconciliation``
    delegates here so the scan filter and the index-maintenance in ``save_trace``
    can never drift apart into two different ideas of "dangling".

    The exact shape (#1276): ``commit_tx_hash IS NOT NULL AND reveal_tx_hash IS
    NULL AND trade_tx_hash IS NOT NULL`` — a trade that really executed, whose
    reasoning was really committed, whose reveal never landed.

    Everything else must NOT match, and each exclusion is load-bearing:
      - no ``trade_tx_hash``  → nothing executed (a SKIP trace, a lease-not-held
        cycle, a dry run). There is no money-moved asymmetry to repair, and the
        commitment is simply unused.
      - ``reveal_tx_hash`` present → already revealed. Retrying would revert
        "Already revealed" and burn gas for nothing.
      - no ``commit_tx_hash`` → nothing was ever anchored, so there is no
        commitment to reveal against.
      - a CLOSED ``reveal_reconcile_state`` → already resolved or already given
        up on. This is what makes "terminal" terminal: without it the bounded
        retry counter would still be re-scanned forever.
    """
    if not isinstance(record, dict):
        return False  # a corrupt store entry is not a reconciliation candidate
    if record.get("reveal_reconcile_state") in _RECONCILE_CLOSED_STATES:
        return False
    return bool(record.get("commit_tx_hash")) and bool(record.get("trade_tx_hash")) and not record.get("reveal_tx_hash")


# Runner exactly-once lease (#1043) — funds-adjacent singleton runners
# (oracle_runner, agent_runner, kb_runner) use this to make sure only ONE live
# copy performs on-chain writes at a time. `KEY_LEASE_PREFIX + runner_name` is
# the lock; `KEY_LEASE_PREFIX + runner_name + KEY_LEASE_FENCING_SUFFIX` is a
# monotonically increasing counter whose current value is folded into every
# issued token so each acquisition has a unique, ordered identity for
# audit/log correlation — independent of the SET NX itself, which is what
# actually enforces exclusivity. Deliberately a SEPARATE key namespace from
# KEY_HEARTBEAT: the heartbeat has no TTL, no owner, and is written once per
# tick by whichever process happens to be running — it cannot answer "am I
# the only one running?" and must not be repurposed for that.
KEY_LEASE_PREFIX = "archimedes:leader:"
KEY_LEASE_FENCING_SUFFIX = ":fencing"

# Lua: acquire the lease ATOMICALLY (single EVAL — no other command can
# interleave between the sub-steps). This is the exclusivity primitive.
#   KEYS[1] = lock key, KEYS[2] = fencing counter key
#   ARGV[1] = holder uuid, ARGV[2] = ttl_ms
# The whole thing runs atomically, which is what makes it correct: a naive
# client-side `SET NX` → `INCR` → `SET XX` (an earlier revision, PR #1046
# review) has a clobber race — if the lease TTL expires between the NX and
# the finalize, another runner can win the key and the finalize's `XX`
# (existence-only) write silently overwrites the NEW owner, violating
# exclusivity for a funds-adjacent singleton. Doing SET-NX + INCR + finalize
# inside ONE script removes the gap entirely: nothing can acquire the key
# between our NX win and our token write. INCR still fires only on a win, so
# a losing retry loop never burns fence numbers.
_LEASE_ACQUIRE_LUA = """
if redis.call("SET", KEYS[1], ARGV[1], "NX", "PX", ARGV[2]) then
    local fence = redis.call("INCR", KEYS[2])
    local token = ARGV[1] .. ":" .. fence
    redis.call("SET", KEYS[1], token, "PX", ARGV[2])
    return token
else
    return false
end
"""

# Lua: renew a lease's TTL — ONLY if the caller's token still matches the
# stored owner (compare-and-set). Re-issuing SET with PX (rather than
# PEXPIRE) keeps the value identical to a fresh acquire while proving
# ownership hasn't changed underneath the caller between check and renew.
_LEASE_RENEW_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    redis.call("SET", KEYS[1], ARGV[1], "PX", ARGV[2])
    return 1
else
    return 0
end
"""

# Lua: release a lease — ONLY if the caller's token still matches the stored
# owner (compare-and-delete). Without this check, a stale holder (e.g. one
# whose lease already expired and was re-acquired by someone else) could
# delete a lock it no longer owns.
_LEASE_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


def safe_json_loads(raw, *, context: str):
    """``json.loads`` that degrades gracefully on malformed data (#919).

    Public helper (promoted from ``_safe_json_loads`` in the #1107 review so
    cross-module consumers — e.g. ``marketplace/state.py`` — depend on a
    stable name rather than a private internal).

    A truncated, partial, or externally-tampered Redis value must not crash a
    read path with a 500. Logs the decode failure and returns ``None`` so the
    caller can skip the bad entry or return a null/empty response.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Malformed JSON in Redis (%s) — dropping value: %s", context, exc)
        return None


#: Decision types whose ``strategies_referenced`` really does hold strategy ids.
#:
#: The field's NAME promises strategy ids everywhere; its contents do not, and
#: that is the whole reason this constant exists rather than a blanket match:
#:
#:   * ``chain/agent_runner.py`` (rebalance / rotation / regime_change / skip)
#:     writes ``[ss.strategy_id for ss in all_signals]`` — genuine strategy ids.
#:   * ``api/strategies_routes.py`` ``_run_fusion_job`` writes
#:     ``result.source_arxiv_ids`` on a ``construction`` trace — arXiv ids.
#:   * ``api/strategies_routes.py``'s construction-trace writer writes the set of
#:     ``paper_anchor`` values from the allocations — paper anchors.
#:
#: Both non-conforming writers emit ``decision_type="construction"``, so scoping
#: the strategy filter to the decision types above is what makes the filter's
#: promise true instead of accidentally-true. Without the scope a construction
#: trace can never match a strategy id anyway — it just fails *silently*, which
#: reads as "this strategy has no construction trace" when the truth is "this
#: filter cannot see construction traces at all".
#:
#: If a construction writer that records real strategy ids is ever wired, add
#: ``"construction"`` here AND fix the two writers above — do not special-case
#: it at a call site. (``services/construction_trace.py`` used to build such a
#: trace without persisting it; it was deleted as a zero-caller surface, and
#: #1595 deleted the fusion bypass route that held the other writer, so today
#: nothing writes a construction trace at all.)
#:
#: #1637 considered admitting ``"construction"`` and deliberately did not
#: (owner decision Q4 on #1688, 2026-09-03). Its case for admission was "fix
#: the fusion job's writer so the type becomes honest"; #1595 removed that
#: writer instead. Widening a provenance filter for a decision type nothing
#: produces would buy nothing and cost the one thing this constant exists for
#: — the promise that everything inside the scope really does hold strategy
#: ids. If a construction writer ever returns, it admits itself in that PR.
STRATEGY_REFERENCE_DECISION_TYPES = frozenset({"rebalance", "rotation", "regime_change", "skip"})


def trace_references_strategy(trace: dict, strategy_id: str) -> bool:
    """Does this trace record a decision that consulted ``strategy_id``?

    A provenance claim, so the match is EXACT and the shapes it accepts are
    closed. Three ways a looser rule lies:

    * ``strategy_id in refs`` on a bare-string ``strategies_referenced`` is a
      **substring** test — ``"alpha"`` would match ``"alpha-momentum-v2"`` and
      attribute a decision to a strategy it never consulted. A bare string is
      matched only against the WHOLE string.
    * ``strategy_id in refs`` on a dict is a **key** test, silently promoting
      whatever a future writer happens to key a mapping by into a provenance
      claim. No writer produces a dict here; an unrecognised shape records no
      references, so it matches nothing.
    * A ``construction`` trace's list holds paper anchors and arXiv ids, not
      strategy ids — see :data:`STRATEGY_REFERENCE_DECISION_TYPES`.

    Everything unrecognised answers False. "I cannot establish that this
    decision consulted that strategy" is the honest answer, and it is the safe
    one: a false positive here puts someone else's trade on a strategy's
    passport.
    """
    if trace.get("decision_type") not in STRATEGY_REFERENCE_DECISION_TYPES:
        return False

    refs = trace.get("strategies_referenced")
    if isinstance(refs, str):
        return refs == strategy_id
    if isinstance(refs, list | tuple | set | frozenset):
        return any(isinstance(r, str) and r == strategy_id for r in refs)
    return False


class AgentStateStore:
    """Thin wrapper over Redis for agent state."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or REDIS_URL
        self._redis: aioredis.Redis | None = None
        self._lease_acquire_script = None  # lazy-registered atomic acquire+fence Lua script
        self._lease_renew_script = None  # lazy-registered compare-and-set Lua script
        self._lease_release_script = None  # lazy-registered compare-and-delete Lua script

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self._url, decode_responses=True)
        return self._redis

    # ─── Regime ───────────────────────────────────────────────────

    async def save_regime(self, classification: RegimeClassification) -> None:
        r = await self._get_redis()
        data = {
            "regime": classification.regime.value,
            "confidence": classification.confidence,
            "vix": classification.signals.vix_level,
            "sp500_above_ma50": classification.signals.sp500_above_ma50,
            "sp500_above_ma200": classification.signals.sp500_above_ma200,
            "regime_changed": classification.regime_changed,
            "timestamp": classification.timestamp.isoformat(),
        }
        await r.set(KEY_REGIME, json.dumps(data))
        logger.debug("Saved regime to Redis: %s", classification.regime.value)

    async def save_ensemble_consensus(
        self,
        consensus: EnsembleConsensus,
        all_signals: list,
    ) -> None:
        """Persist the strategy-ensemble consensus under its own Redis key.

        This is the endogenous "how decisive is the ensemble" signal — derived
        from ``flat_pct`` — and is stored under ``KEY_ENSEMBLE_CONSENSUS`` so it
        does NOT shadow the exogenous market regime at ``KEY_REGIME`` (#659).
        """
        r = await self._get_redis()
        signal_summary = {}
        for ss in all_signals:
            for s in ss.signals:
                signal_summary[s.asset] = {
                    "signal": s.signal.value,
                    "weight": s.weight,
                    "reason": s.reason,
                    "strategy": ss.paper_title[:40],
                }
        # Dynamic confidence from signal weights + dispersion (matches
        # _compute_confidence in agent_runner — same formula, different caller).
        # This is the ensemble's *decisiveness*, not a market-regime confidence.
        flat_pct = consensus.flat_pct
        if all_signals:
            directional = [s for ss in all_signals for s in ss.signals if s.signal.value != "flat"]
            vote_ratio = 1.0 - flat_pct
            avg_strength = sum(abs(s.weight) for s in directional) / max(len(directional), 1) if directional else 0.0
            avg_strength = min(avg_strength, 1.0)
            all_weights = [s.weight for ss in all_signals for s in ss.signals]
            if len(all_weights) >= 2:
                mean_w = sum(all_weights) / len(all_weights)
                variance = sum((w - mean_w) ** 2 for w in all_weights) / len(all_weights)
                dispersion_penalty = min(variance**0.5 * 2, 0.3)
            else:
                dispersion_penalty = 0.0
            dyn_confidence = max(0.05, min(0.99, vote_ratio * (0.5 + 0.5 * avg_strength) - dispersion_penalty))
        else:
            dyn_confidence = 0.5
        data = {
            "label": consensus.label.value,
            "confidence": round(dyn_confidence, 4),
            "flat_pct": round(flat_pct, 2),
            "strategy_count": consensus.signal_count or len(all_signals),
            "signals": signal_summary,
            "timestamp": datetime.now(UTC).isoformat(),
            "source": "strategy_consensus",
        }
        await r.set(KEY_ENSEMBLE_CONSENSUS, json.dumps(data))
        logger.debug("Saved ensemble consensus to Redis: %s", consensus.label.value)

    async def load_regime(self) -> dict | None:
        """Load the exogenous market regime (may be None until a detector writes it)."""
        r = await self._get_redis()
        raw = await r.get(KEY_REGIME)
        if raw:
            return safe_json_loads(raw, context=KEY_REGIME)
        return None

    async def load_ensemble_consensus(self) -> dict | None:
        """Load the endogenous strategy-ensemble consensus (#659)."""
        r = await self._get_redis()
        raw = await r.get(KEY_ENSEMBLE_CONSENSUS)
        if raw:
            return safe_json_loads(raw, context=KEY_ENSEMBLE_CONSENSUS)
        return None

    # ─── Heartbeat ────────────────────────────────────────────────

    async def save_heartbeat(self) -> None:
        r = await self._get_redis()
        await r.set(KEY_HEARTBEAT, datetime.now(UTC).isoformat())

    async def get_heartbeat(self) -> str | None:
        r = await self._get_redis()
        return await r.get(KEY_HEARTBEAT)

    # ─── Runner exactly-once lease (#1043) ─────────────────────────
    #
    # A real mutual-exclusion primitive — owner token + TTL + compare-and-set
    # renew/release — for funds-adjacent singleton runners. NOT a repurposing
    # of save_heartbeat/get_heartbeat above (those stay untouched: no TTL, no
    # owner, once-per-tick, and cannot prove exclusivity).

    async def acquire_lease(self, runner_name: str, ttl_ms: int) -> str | None:
        """Attempt to acquire the exactly-once lease for *runner_name*.

        Returns a fencing token ``"<uuid4>:<fence>"`` on success, or ``None``
        if another live copy already holds the lease. ``fence`` is a
        monotonically increasing per-runner counter folded into the token so
        every acquisition WIN gets a unique, ordered identity — useful for
        audit/log correlation, independent of the ``SET NX`` that actually
        enforces exclusivity.

        The acquire runs as a SINGLE atomic Lua script (``_LEASE_ACQUIRE_LUA``):
        ``SET NX`` (the exclusivity check) + ``INCR`` (fence, only on a win) +
        the final token write, with no window for another process to interleave.
        This is deliberately NOT a client-side ``SET NX`` → ``INCR`` → ``SET XX``
        sequence: that has a clobber race (if the TTL lapses between the NX and
        the finalize, the existence-only ``XX`` write can overwrite a lease a
        DIFFERENT runner just won — fatal for a funds-adjacent singleton). The
        atomic script closes that gap and still only consumes a fence on a win,
        so a losing retry loop never burns fence numbers.

        The returned token must be passed to ``renew_lease`` / ``release_lease``
        so only the current owner can mutate the lease.
        """
        r = await self._get_redis()
        key = f"{KEY_LEASE_PREFIX}{runner_name}"
        fencing_key = f"{KEY_LEASE_PREFIX}{runner_name}{KEY_LEASE_FENCING_SUFFIX}"
        if self._lease_acquire_script is None:
            self._lease_acquire_script = r.register_script(_LEASE_ACQUIRE_LUA)

        holder_uuid = str(uuid.uuid4())
        token = await self._lease_acquire_script(keys=[key, fencing_key], args=[holder_uuid, str(ttl_ms)])
        return token if token else None

    async def renew_lease(self, runner_name: str, token: str, ttl_ms: int) -> bool:
        """Extend the lease TTL — ONLY if *token* still matches the stored owner.

        Returns ``False`` (never raises for a lost lease) when the token no
        longer matches — e.g. the lease expired and another copy acquired it.
        Callers MUST treat a ``False`` return as "lease lost" and fail
        closed: skip the on-chain write this cycle and keep retrying to
        re-acquire. See ``chain/oracle_runner.py`` and ``chain/agent_runner.py``.
        """
        r = await self._get_redis()
        key = f"{KEY_LEASE_PREFIX}{runner_name}"
        if self._lease_renew_script is None:
            self._lease_renew_script = r.register_script(_LEASE_RENEW_LUA)
        result = await self._lease_renew_script(keys=[key], args=[token, str(ttl_ms)])
        return bool(result)

    async def release_lease(self, runner_name: str, token: str) -> None:
        """Release the lease — a no-op if *token* no longer matches the owner.

        Best-effort: safe to call on shutdown even if the lease already
        expired or was reclaimed by another copy (the compare-and-delete
        check makes that a no-op rather than deleting someone else's lease).
        """
        r = await self._get_redis()
        key = f"{KEY_LEASE_PREFIX}{runner_name}"
        if self._lease_release_script is None:
            self._lease_release_script = r.register_script(_LEASE_RELEASE_LUA)
        await self._lease_release_script(keys=[key], args=[token])

    # ─── Last rebalance per vault ─────────────────────────────────

    async def save_last_rebalance(self, vault_address: str) -> None:
        r = await self._get_redis()
        key = f"{KEY_LAST_REBALANCE_PREFIX}{vault_address.lower()}"
        await r.set(key, datetime.now(UTC).isoformat())

    async def get_last_rebalance(self, vault_address: str) -> datetime | None:
        r = await self._get_redis()
        key = f"{KEY_LAST_REBALANCE_PREFIX}{vault_address.lower()}"
        raw = await r.get(key)
        if raw:
            return datetime.fromisoformat(raw)
        return None

    # ─── Events ──────────────────────────────────────────────────

    async def save_event(self, event_type: str, data: dict) -> None:
        """Append an event to the agent event log (capped list)."""
        r = await self._get_redis()
        entry = json.dumps(
            {
                "type": event_type,
                "data": data,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        await r.lpush("archimedes:agent:events", entry)
        await r.ltrim("archimedes:agent:events", 0, 99)  # keep last 100

    async def get_events(self, count: int = 20) -> list[dict]:
        r = await self._get_redis()
        raw = await r.lrange("archimedes:agent:events", 0, count - 1)
        return [d for e in raw if (d := safe_json_loads(e, context="agent:events")) is not None]

    # ─── Vault Monitoring ─────────────────────────────────────────

    async def save_vault_snapshot(self, vault_address: str, metrics: dict) -> None:
        """Save a vault metrics snapshot. Keeps last 288 (= 24h at 5min)."""
        r = await self._get_redis()
        key = f"archimedes:vault:snapshots:{vault_address.lower()}"
        entry = json.dumps(
            {
                **metrics,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        await r.lpush(key, entry)
        await r.ltrim(key, 0, 287)

    async def get_vault_snapshots(self, vault_address: str, count: int = 50) -> list[dict]:
        r = await self._get_redis()
        key = f"archimedes:vault:snapshots:{vault_address.lower()}"
        raw = await r.lrange(key, 0, count - 1)
        return [d for e in raw if (d := safe_json_loads(e, context="vault:snapshots")) is not None]

    # ─── Reasoning Trace Persistence ────────────────────────────

    async def save_trace(self, trace_data: dict) -> None:
        """Store off-chain reasoning trace data keyed by trace_hash.

        Also maintains secondary index by trace UUID for lookup.

        Stamps ownership on the way in (#1556). This is the single write choke
        point for traces — ``publish_trace``, the agent runner's three persist
        sites and the generation-trace writer all land here — so stamping HERE
        is what makes "every persisted trace knows who owns it" true by
        construction rather than by five call sites remembering. The read gate
        (``services.trace_visibility``) then needs no database round-trip for
        anything published after this change, which is why a Postgres outage
        cannot downgrade a private trace to a public one.

        A caller that already knows the owner (the generation path, whose trace
        has no vault at all) sets ``owner_user_id``/``owner_wallet`` itself;
        the presence of either key suppresses the lookup, including when the
        value is ``None`` — "this writer resolved the owner and there isn't
        one" must not be overwritten by a vault guess.
        """
        r = await self._get_redis()
        trace_hash = trace_data.get("trace_hash", "")
        trace_id = trace_data.get("id", "")
        if not trace_hash:
            logger.warning("Cannot save trace without trace_hash")
            return

        if "owner_user_id" not in trace_data and "owner_wallet" not in trace_data:
            trace_data = {**trace_data, **self._resolve_trace_owner(trace_data.get("vault_address", ""))}

        # Store full trace data by hash
        key = f"{KEY_TRACE_PREFIX}{trace_hash}"
        await r.set(key, json.dumps(trace_data, default=str))

        # Secondary index by UUID
        if trace_id:
            await r.set(f"{KEY_TRACE_PREFIX}id:{trace_id}", trace_hash)

        # Add to sorted set by timestamp for listing
        ts = trace_data.get("timestamp", "")
        score = 0
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                score = dt.timestamp()
            except (ValueError, TypeError):
                score = datetime.now(UTC).timestamp()
        else:
            score = datetime.now(UTC).timestamp()

        await r.zadd(KEY_TRACE_INDEX, {trace_hash: score})

        # Reveal-reconciliation index maintenance (#1353) — see the key block
        # above. Runs on EVERY save_trace call (the single write choke-point
        # for all trace records, dangling or not) so a record enters/leaves
        # these sets exactly when its dangling-ness actually changes, with no
        # separate call site that could forget to do it.
        if is_dangling_reveal(trace_data):
            await r.sadd(KEY_TRACE_RECONCILE_PENDING, trace_hash)
            # HSETNX: set the first-seen marker ONLY if absent, so a record
            # that stays dangling across many retries keeps its ORIGINAL
            # timestamp — that's what makes it a valid age bound.
            await r.hsetnx(KEY_TRACE_RECONCILE_FIRST_SEEN, trace_hash, datetime.now(UTC).isoformat())
        else:
            await r.srem(KEY_TRACE_RECONCILE_PENDING, trace_hash)
            await r.hdel(KEY_TRACE_RECONCILE_FIRST_SEEN, trace_hash)
            if trace_data.get("reveal_reconcile_state") == "terminal":
                # Never removed — terminal is a state, not a deletion. That
                # makes SCARD of this set a CUMULATIVE lifetime counter (read
                # its rate of increase, not its level) — see
                # ``get_reveal_reconcile_terminal_count``.
                await r.sadd(KEY_TRACE_RECONCILE_TERMINAL, trace_hash)

        logger.debug("Saved trace %s to Redis", trace_hash[:16])

    @staticmethod
    def _resolve_trace_owner(vault_address: str) -> dict:
        """``{"owner_user_id": …, "owner_wallet": …}`` for a vault (#1556).

        Fail-soft by design — a trace must still persist when the identity
        database is unreachable, and an unstamped row is not a leak: the read
        gate falls back to looking the vault owner up itself, and to the
        house-vault allowlist below that.
        """
        try:
            from archimedes.services.trace_visibility import resolve_vault_owners

            owner_user_id, owner_wallet = resolve_vault_owners({str(vault_address or "")}).get(
                str(vault_address or "").strip().lower(), (None, None)
            )
        except Exception:
            logger.warning("save_trace: owner stamp lookup failed — persisting unstamped", exc_info=True)
            return {}
        return {"owner_user_id": owner_user_id, "owner_wallet": owner_wallet}

    async def get_trace(self, trace_id_or_hash: str) -> dict | None:
        """Get off-chain trace data by hash or UUID."""
        r = await self._get_redis()

        # Try direct hash lookup
        raw = await r.get(f"{KEY_TRACE_PREFIX}{trace_id_or_hash}")
        if raw:
            parsed = safe_json_loads(raw, context="trace:hash")
            if parsed is not None:
                return parsed

        # Try UUID → hash → data
        hash_val = await r.get(f"{KEY_TRACE_PREFIX}id:{trace_id_or_hash}")
        if hash_val:
            raw = await r.get(f"{KEY_TRACE_PREFIX}{hash_val}")
            if raw:
                parsed = safe_json_loads(raw, context="trace:uuid")
                if parsed is not None:
                    return parsed

        return None

    async def list_traces(
        self,
        vault_address: str | None = None,
        decision_type: str | None = None,
        strategy_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """List traces from index, newest first, optionally filtered.

        Returns ``(window, total)`` where ``total`` counts everything matching
        the filters, not just the returned page.

        ``strategy_id`` keeps only DECISION traces that name exactly this
        strategy in ``strategies_referenced`` — see
        :func:`trace_references_strategy` for why the match is exact and why
        construction/generation traces are out of scope (their
        ``strategies_referenced`` holds paper anchors and arXiv ids). It is
        applied **here**, alongside the other filters and *before* windowing,
        for the same reason they are: filtering a page after it has been cut
        would make ``total`` count unfiltered rows and hand the caller a
        short-or-empty page with a total that promises more, which is a broken
        paginator rather than a display quirk.
        """
        r = await self._get_redis()

        # Get all trace hashes sorted by timestamp (newest first)
        all_hashes = await r.zrevrange(KEY_TRACE_INDEX, 0, -1)

        # Load in MGET batches, then filter (#1577). This used to be one
        # awaited GET per member of the WHOLE index — on a TLS ElastiCache
        # connection that is one full round trip each, so a 2000-trace index
        # cost 2001 round trips before the first row was filtered. Batching
        # makes it ``1 + ceil(len(index) / TRACE_MGET_BATCH)``.
        #
        # Chunked rather than one MGET over the whole index because this read
        # is deliberately unbounded (the window is applied AFTER filtering —
        # see the docstring), so a single MGET would grow without limit: Redis
        # is single-threaded and serves a 100k-key MGET as one blocking
        # command that stalls every other client, and the whole reply would be
        # materialised in this process at once. The batch bounds both.
        traces: list[dict] = []
        for start in range(0, len(all_hashes), TRACE_MGET_BATCH):
            batch = all_hashes[start : start + TRACE_MGET_BATCH]
            # MGET preserves request order, and the batches are walked in
            # index order, so the newest-first ordering `zrevrange` returned
            # survives unchanged.
            raws = await r.mget([f"{KEY_TRACE_PREFIX}{h}" for h in batch])
            for raw in raws:
                # Same malformed-row tolerance as the per-key loop: a hash in
                # the index whose blob is gone (`None`) and a blob that no
                # longer parses are both skipped, never raised. MGET returns
                # `None` in the slot of a missing key, which is exactly what
                # the old `GET` returned for one.
                if not raw:
                    continue
                data = _safe_json_loads(raw, context="trace:index")
                if data is None:
                    continue

                # Apply filters
                if vault_address and data.get("vault_address", "").lower() != vault_address.lower():
                    continue
                if decision_type and data.get("decision_type") != decision_type:
                    continue
                if strategy_id and not trace_references_strategy(data, strategy_id):
                    continue

                traces.append(data)

        total = len(traces)
        window = traces[offset : offset + limit]
        return window, total

    async def list_recent_traces(self, limit: int = 200) -> list[dict]:
        """Newest-first window of persisted traces, bounded AT THE INDEX.

        Deliberately not ``list_traces(limit=…)``: that one loads every trace in
        the index and only windows afterwards, which is acceptable for a
        user-facing page but not for something the agent runs on every tick.
        Here the ``zrevrange`` bound is applied first, so the cost is O(limit)
        regardless of how much history has accumulated (#1276).
        """
        r = await self._get_redis()
        hashes = await r.zrevrange(KEY_TRACE_INDEX, 0, max(int(limit), 1) - 1)

        traces: list[dict] = []
        for h in hashes:
            raw = await r.get(f"{KEY_TRACE_PREFIX}{h}")
            if not raw:
                continue
            data = safe_json_loads(raw, context="trace:recent")
            if data is not None:
                traces.append(data)
        return traces

    async def get_last_trace(self, vault_address: str) -> dict | None:
        """Get the most recent trace for a specific vault."""
        traces, _ = await self.list_traces(vault_address=vault_address, limit=1)
        return traces[0] if traces else None

    async def get_trace_count(self) -> int:
        """Total number of stored off-chain traces."""
        r = await self._get_redis()
        return await r.zcard(KEY_TRACE_INDEX)

    # ─── Reveal-reconciliation durable index (#1353) ─────────────

    async def list_dangling_reveal_traces(self) -> list[dict]:
        """EXACT set of dangling commitments via the durable index, not a bounded scan.

        Unlike ``list_recent_traces(N)``, this reads ``KEY_TRACE_RECONCILE_PENDING``
        — a set ``save_trace`` maintains every time a record's dangling-ness
        changes — so a record can never age out of it regardless of how much
        trace history has accumulated. Callers should still union this with a
        bounded scan as a one-cycle migration backstop for any record written
        dangling by code that predates this index (see agent_runner.py).

        An index member whose trace blob is gone (ElastiCache maxmemory
        eviction, a manual DEL, a key-namespace change) is pruned right here
        (#1403 review) — SREM from the pending set and HDEL its first-seen
        field — rather than merely skipped. Left alone, an orphaned member
        would inflate ``get_reveal_reconcile_pending_count()`` (SCARD-backed)
        forever with no path back to zero; this is the one call site that
        already pays for a GET per member, so self-healing here is free.

        The prune is LOUD (round-2 review) and moves the member into the
        terminal set rather than dropping it silently: the blob carried the
        canonical bytes and storage pointer ``reveal()`` needs, so once it's
        gone this commitment can never be revealed again regardless of what
        the on-chain state says — that IS terminal, not a no-op. Without the
        SADD here, a dangling record whose blob got evicted simply vanished
        from BOTH ``/health`` gauges with zero telemetry, in a PR whose stated
        purpose is making exactly this state countable.

        KNOWN LIMIT — this read is UNBOUNDED (#1403 review, follow-up):
        ``SMEMBERS`` returns the whole pending set and this then issues one
        ``GET`` per member, so both the round-trip count and the peak memory
        scale linearly with however many commitments are dangling at once,
        with no cap. That is acceptable at today's volumes (the pending set is
        empty on a healthy system and the per-tick retry budget is
        ``REVEAL_RECONCILE_MAX_PER_TICK``, currently 5 — so a large pending
        set costs reads, not writes), but a pathological run that dangles
        thousands of commitments would make each tick's index read
        proportionally expensive. Deliberately not capped in this PR: a cap
        here re-introduces exactly the "aged past the window, never retried"
        blind spot the durable index exists to remove, so the right fix is
        ``SSCAN`` with a cursor plus an ``MGET`` batch, tracked as follow-up
        rather than bolted on late.
        """
        r = await self._get_redis()
        hashes = await r.smembers(KEY_TRACE_RECONCILE_PENDING)
        traces: list[dict] = []
        for h in hashes:
            raw = await r.get(f"{KEY_TRACE_PREFIX}{h}")
            if not raw:
                logger.warning(
                    "Pruned orphaned reconcile-index member %s — trace blob is gone; this dangling "
                    "commitment can no longer be revealed and is being marked terminal",
                    h[:16],
                )
                await r.srem(KEY_TRACE_RECONCILE_PENDING, h)
                await r.hdel(KEY_TRACE_RECONCILE_FIRST_SEEN, h)
                await r.sadd(KEY_TRACE_RECONCILE_TERMINAL, h)
                continue
            data = safe_json_loads(raw, context="trace:reconcile_pending")
            if data is not None:
                traces.append(data)
        return traces

    async def get_reveal_reconcile_pending_count(self) -> int:
        """O(1) count of currently-dangling commitments (SCARD, not a scan)."""
        r = await self._get_redis()
        return await r.scard(KEY_TRACE_RECONCILE_PENDING)

    async def get_reveal_reconcile_terminal_count(self) -> int:
        """O(1) CUMULATIVE count of permanently-given-up reveals.

        Members are never removed from ``KEY_TRACE_RECONCILE_TERMINAL``, so
        this is a monotonically non-decreasing lifetime total, NOT a level that
        falls back once the underlying problem is fixed: one historical give-up
        pins it at 1 forever. Interpret the RATE OF INCREASE (the delta between
        two samples) — an absolute-value threshold on this number would fire
        once and then stay fired, and "steady state is near zero" is only true
        of a keyspace that has never had a terminal reveal at all.

        Honesty note on "alertable" (#1403 review): the SURFACE exists — this
        count is O(1) to read and is published on ``GET /health`` — but NO
        alerting is wired to it. ``infra/cloudwatch.tf`` defines no metric
        filter and no alarm over it, and unlike ``HEALTH_CHAIN_DISCONNECTED`` /
        ``HEALTH_ORACLE_STALE`` (this repo's two working log-literal →
        ``aws_cloudwatch_log_metric_filter`` → alarm pairs) nothing emits a
        greppable literal a filter could key on. Wiring a pager to this is
        follow-up work, not something this code does today.
        """
        r = await self._get_redis()
        return await r.scard(KEY_TRACE_RECONCILE_TERMINAL)

    async def mark_reveal_reconcile_terminal(self, trace_hash: str) -> None:
        """Durably close a dangling commitment, independent of the trace blob write.

        ``save_trace`` normally maintains the three reconcile-index structures
        as a side effect of persisting the FULL trace JSON blob (#1353) — which
        works, but couples the terminal transition's durability to that SAME
        write. A broken write path specifically on the blob (oversized value,
        a serialization bug in one record's fields, ...) can keep failing that
        `SET` while smaller, unrelated writes still succeed, leaving
        ``reveal_reconcile_state`` stuck at "pending" in the persisted JSON
        forever — and ``agent_runner._reconcile_terminal`` would recompute
        "should be terminal" every tick without it ever sticking, so the
        actual on-chain ``reveal()`` call keeps being retried unboundedly
        (#1403 review of the max-age circuit breaker).

        Called directly by ``agent_runner._reconcile_terminal`` BEFORE (and
        independently of) the blob save, this closes the SAME three
        structures with three small, independent ops — SADD terminal, SREM
        pending, HDEL first_seen — so the durable index reflects "terminal"
        even when the blob write that same tick attempts right after this
        keeps failing. ``_reconcile_dangling_reveals`` cross-checks this set
        against every scan/index candidate each pass specifically so a stale
        blob can never re-enter the retry loop once this has run.
        """
        if not trace_hash:
            return
        r = await self._get_redis()
        await r.sadd(KEY_TRACE_RECONCILE_TERMINAL, trace_hash)
        await r.srem(KEY_TRACE_RECONCILE_PENDING, trace_hash)
        await r.hdel(KEY_TRACE_RECONCILE_FIRST_SEEN, trace_hash)

    async def list_reveal_reconcile_terminal_hashes(self) -> set[str]:
        """All trace_hashes durably marked terminal (SMEMBERS).

        Used to cross-check reconciliation candidates whose own JSON blob may
        be stale — see ``mark_reveal_reconcile_terminal``. Distinct from
        ``get_reveal_reconcile_terminal_count`` (SCARD, a count only): this
        needs the actual members to filter a candidate list by.
        """
        r = await self._get_redis()
        return set(await r.smembers(KEY_TRACE_RECONCILE_TERMINAL))

    async def seed_reveal_reconcile_first_seen(self, trace_hash: str) -> None:
        """Independently seed the first-seen marker (HSETNX only — #1403 review).

        Normally ``save_trace`` writes this marker as a side effect of
        persisting the full trace JSON blob. That coupling is exactly the gap
        this closes: a record that was ALREADY dangling before this index
        existed (a migration-era record) has no marker yet, and if the thing
        that's broken is that same blob write path, ``save_trace`` can keep
        raising before it ever reaches the HSETNX line — so the marker is
        never seeded and the max-age circuit breaker (which reads it) can
        never fire for exactly the compound failure it exists to close.

        Called from ``agent_runner._reconcile_failure`` the first time a
        reconciliation pass sees a dangling record with no first-seen marker,
        as a small write on its own key — independent of whether the blob
        save that tick succeeds or fails — so the record acquires a clock
        (starting from now, not its true unknown origin, same conservative
        choice ``save_trace`` already makes) on this pass rather than staying
        permanently unbounded.
        """
        if not trace_hash:
            return
        r = await self._get_redis()
        await r.hsetnx(KEY_TRACE_RECONCILE_FIRST_SEEN, trace_hash, datetime.now(UTC).isoformat())

    async def get_reveal_reconcile_first_seen(self, trace_hash: str) -> datetime | None:
        """When this trace_hash FIRST entered the dangling index, or None.

        Written once (HSETNX) by ``save_trace`` and left untouched across
        every subsequent retry of the same record — the independent clock the
        reconciliation pass's max-age circuit breaker reads (#1353).
        """
        if not trace_hash:
            return None
        r = await self._get_redis()
        raw = await r.hget(KEY_TRACE_RECONCILE_FIRST_SEEN, trace_hash)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None

    # ─── SIWE Nonces ────────────────────────────────────────────────

    async def save_nonce(self, nonce: str, ttl_seconds: int) -> None:
        """Store a SIWE challenge nonce with a Redis-managed expiry.

        Using SETEX means Redis itself evicts expired nonces -- no manual
        sweep needed. Shared across workers so /nonce on one worker and
        /verify on another see the same pending-nonce set.
        """
        r = await self._get_redis()
        await r.setex(f"{KEY_SIWE_NONCE_PREFIX}{nonce}", ttl_seconds, "1")

    async def pop_nonce(self, nonce: str) -> bool:
        """Atomically read-and-delete a pending nonce. Returns True if it existed.

        GETDEL makes the nonce single-use: a second pop for the same value
        returns False, matching the "Nonce not found or already used" check.
        """
        r = await self._get_redis()
        return await r.getdel(f"{KEY_SIWE_NONCE_PREFIX}{nonce}") is not None

    # ─── Lifecycle ────────────────────────────────────────────────

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None


# Backwards-compat alias — prefer safe_json_loads (public) going forward.
_safe_json_loads = safe_json_loads
