"""The passport card's executable fields are DERIVED from the validated DSL spec.

Issue #1769. A generated strategy carries two descriptions of itself: the prose
its proposal was written in, and the ``strategy_spec`` that
``services.strategy_dsl.validate_strategy_spec`` accepted and the backtester
actually ran. Only one of them was executed. When the two disagree, the passport
card — the flagship read surface, and the thing a first user reads as "what this
strategy does" — must show the one that ran.

Exactly three fields are in scope, and the list is closed on purpose:
``asset_universe``, ``rebalance_frequency`` and ``position_sizing``. Each has a
literal counterpart in the DSL's closed-enum schema, so "what the spec says" is a
lookup, not an interpretation. Everything else on the card (thesis, methodology
summary, papers) has no spec counterpart and is left exactly as it was — this
module cannot quietly grow into a general card rewriter.

Two call sites, because the card could go wrong in two different places:

* **WRITE** — ``agents.generation_pipeline``'s three passport writers. They
  passed ``asset_universe`` from the candidate and passed neither cadence nor
  sizing at all, so every generated ``strategy_passports`` row took the column
  defaults (``weekly`` / ``equal_weight``) — including rows whose spec said
  ``monthly`` / ``full_invested_when_in_market``. That is the observed defect:
  two descriptions of two different strategies on one passport.
* **READ** — ``api.strategies_routes``. The rows written before that fix are
  still in the table and the table is append-only, so a read path that trusted
  them would keep serving the same contradiction to the same user. The
  reconciliation therefore runs again on the way out, and LOGS the disagreement
  naming the strategy id rather than repairing it silently: a card that was
  wrong for months is a fact about the data, and the operator should be able to
  find every row it happened to. That log is deduped by strategy id for the
  life of the process — see ``_LOGGED_DISAGREEMENTS``; the read path corrects
  the *response* without repairing the *row*, so an undeduped line would repeat
  on every request forever, on a list surface that has no LIMIT.

The spec wins, always — never the reverse. A stored column can only ever be a
copy of something; the spec is the artifact the backtest consumed.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The closed set of card fields the DSL determines. Anything not named here is
# not this module's business.
SPEC_DERIVED_CARD_FIELDS: tuple[str, ...] = (
    "asset_universe",
    "rebalance_frequency",
    "position_sizing",
)

# Strategy ids whose card/spec disagreement has already been logged by this
# process.
#
# WHY this exists: ``_passport_to_strategy_response`` is the per-row mapper for
# the Library list and the public leaderboard, and ``list_passports`` has no
# LIMIT. Every generated row written before #1769 disagrees with its spec by
# construction — that is the bug — and the read path corrects the RESPONSE
# without repairing the ROW, so an undeduped WARNING is one line per row per
# request, forever, on unauthenticated traffic.
#
# WHICH mechanism (owner call, 2026-09-01): dedupe by strategy id and keep
# WARNING, rather than demoting the list path to DEBUG. The point of the line is
# that a stale row can be FOUND; DEBUG on the surface where most rows are read
# would have hidden exactly the ids worth finding. One line per bad row per
# process is the operator's inventory; every repeat after it is noise.
#
# Not thread-synchronised on purpose: the worst race duplicates one log line.
_LOGGED_DISAGREEMENTS: set[str] = set()

# A process that serves every strategy holds one short id per disagreeing row.
# Past the cap the memo is dropped wholesale rather than evicted one by one:
# forgetting re-logs a line (harmless), unbounded growth is not.
_LOGGED_DISAGREEMENTS_CAP = 50_000


def card_fields_from_spec(spec: Any, *, strategy_id: str = "") -> dict[str, Any] | None:
    """The three card fields a *validated* DSL spec determines, or ``None``.

    ``None`` means "no spec to defer to", and it is returned for every shape
    that is not an executable spec: absent (the fixture / buy-and-hold path
    stores none), empty, the wrong type, or — the case worth naming — a spec
    that does not pass ``validate_strategy_spec``.

    That last one is deliberate and is the whole reason this goes through the
    validator instead of reading three keys off a dict. The claim this module
    exists to make is "the card shows the spec the backtest RAN". A blob that
    the DSL validator rejects was never run by anything, so promoting its
    ``rebalance_frequency`` over the stored column would swap one unverified
    string for another and call the result derived. When the validator says no,
    this returns ``None`` and the caller keeps what it had.
    """
    if not isinstance(spec, dict) or not spec:
        return None

    from archimedes.services.strategy_dsl import DSLError, validate_strategy_spec

    try:
        validated = validate_strategy_spec(spec)
    except DSLError as exc:
        logger.warning(
            "strategy %s: persisted strategy_spec does not validate (%s) — card fields keep their stored values",
            strategy_id or "<unknown>",
            exc,
        )
        return None
    except Exception as exc:  # pragma: no cover — defensive; a malformed blob must not 500 a read
        logger.warning(
            "strategy %s: strategy_spec validation raised %s — card fields keep their stored values",
            strategy_id or "<unknown>",
            type(exc).__name__,
        )
        return None

    return {
        "asset_universe": list(validated.asset_universe),
        "rebalance_frequency": str(validated.rebalance_frequency),
        # The DSL carries sizing as ``{"type": <enum>, ...tuning}``; the card
        # field is the type. The tuning parameters (``annual_pct``,
        # ``reference_vol_annual``) are rendered by the Generated-DSL panel,
        # which shows the whole spec — collapsing them into this one string
        # would be a lossy second rendering of something already on the page.
        "position_sizing": str(validated.position_sizing.get("type")),
    }


def reconcile_card_fields(
    strategy_id: str,
    spec: Any,
    *,
    asset_universe: Any,
    rebalance_frequency: Any,
    position_sizing: Any,
) -> dict[str, Any]:
    """Return the three card fields with the validated spec winning every tie.

    The stored values are passed in and returned unchanged when there is no
    usable spec, which is the correct answer for curated rows (no spec at all)
    and for the fixture path.

    A disagreement is logged at WARNING naming the id and every field that
    differed, with both sides of the difference — **once per strategy id for the
    life of the process**, not once per call. This function runs on the Library
    list and the public leaderboard, once per row, on a query with no LIMIT, and
    it corrects the response without repairing the row; the second and every
    later line about the same id would say exactly what the first one said. See
    ``_LOGGED_DISAGREEMENTS`` for why WARNING was kept over a DEBUG demotion.

    The log is not an error and does not fail the read: the served card is now
    right, and the line is how the stale row that produced it gets found and
    rewritten.
    """
    derived = card_fields_from_spec(spec, strategy_id=strategy_id)
    stored = {
        "asset_universe": asset_universe,
        "rebalance_frequency": rebalance_frequency,
        "position_sizing": position_sizing,
    }
    if derived is None:
        return stored

    disagreements = [
        f"{field}: stored {stored[field]!r} != spec {derived[field]!r}"
        for field in SPEC_DERIVED_CARD_FIELDS
        if not _same(stored[field], derived[field])
    ]
    if disagreements and _first_time_for(strategy_id):
        logger.warning(
            "strategy %s: passport card disagreed with its validated DSL spec (%s) — the spec wins",
            strategy_id or "<unknown>",
            "; ".join(disagreements),
        )
    return derived


def _first_time_for(strategy_id: str) -> bool:
    """Whether this process has yet to log a disagreement for *strategy_id*.

    Records the id as a side effect. An empty id collapses to one shared key:
    a caller that passed no id gives the operator nothing to look up anyway, so
    repeating the line buys nothing.
    """
    key = strategy_id or "<unknown>"
    if key in _LOGGED_DISAGREEMENTS:
        return False
    if len(_LOGGED_DISAGREEMENTS) >= _LOGGED_DISAGREEMENTS_CAP:
        _LOGGED_DISAGREEMENTS.clear()
    _LOGGED_DISAGREEMENTS.add(key)
    return True


def _same(stored: Any, derived: Any) -> bool:
    """Whether a stored card value already agrees with the spec's.

    Lists compare as ORDERED sequences of strings: ``["QQQ", "SHV"]`` and
    ``["SHV", "QQQ"]`` are the same set of names but not the same universe as
    the interpreter reads it (``dsl_to_backtrader`` binds data feeds in spec
    order), so treating them as equal would suppress a warning for a real
    difference. Enum members compare by value, because the stored side arrives
    as a plain column string on one path and as a ``PositionSizing`` /
    ``RebalanceFrequency`` member on the other.
    """
    if isinstance(stored, (list, tuple)) or isinstance(derived, (list, tuple)):
        return [str(x) for x in (stored or [])] == [str(x) for x in (derived or [])]
    return str(getattr(stored, "value", stored)) == str(getattr(derived, "value", derived))
