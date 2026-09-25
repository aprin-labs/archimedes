"""Strategy visibility predicates implementing #850 privacy rules and the
#1557 card-vs-reasoning split.

TWO predicates live here, and picking the wrong one is an authorization bug:

    is_strategy_visible          -> may the caller read the CARD?
    is_strategy_reasoning_visible -> may the caller read the REASONING?

────────────────────────────────────────────────────────────────────────────
THE VISIBILITY MATRIX (#1557)
────────────────────────────────────────────────────────────────────────────

                                CARD                    REASONING
                                (name, papers,          (debate transcript,
                                 methodology,            full daily-return
                                 headline metrics,       series, machine-
                                 rigor badge +           readable DSL spec,
                                 its gate detail)        the owner's brief)
  ──────────────────────────────────────────────────────────────────────────
  curated / is_example row      PUBLIC                  PUBLIC
  (house demo content)          `is_strategy_visible`   `is_strategy_reasoning_visible`

  user row, is_published=True   PUBLIC                  OWNER ONLY
  (owner opted in)              `is_strategy_visible`   `is_strategy_reasoning_visible`

  user row, private             OWNER ONLY              OWNER ONLY
  (the default)                 `is_strategy_visible`   `is_strategy_reasoning_visible`

**Publishing consents to sharing the RESULT, not the DERIVATION.** That is the
whole rule. A user who publishes a strategy is putting a card on a public
board — its name, the papers it came from, the methodology writeup, the
measured metrics, and the rigor verdict that certifies those metrics. They are
not handing over the multi-agent debate that produced it, the day-by-day
return series it was graded on, or the executable spec that runs it. Those are
the derivation, and they stay with the owner.

Why ``is_example`` reasoning stays public: those rows are house-curated demo
content with no owner to protect, and the product already renders their
reasoning to anonymous visitors — ``GET /{id}/returns`` and ``GET /{id}/debate``
both short-circuit BEFORE any row check when
``strategy_provider().get_strategy(id)`` resolves, and ``/quant`` (QuantLab)
fetches the full return series for every curated library row with no session at
all. Gating them would break a live public page while protecting nobody.
(Verified against ui/src/components/QuantLab.jsx and the ``is_curated``
short-circuits in ``strategies_routes``, not assumed.)

Where the line was drawn, route by route, and why — the deliberate calls:

  * ``GET /api/strategies/{id}/debate`` — REASONING, and nothing but. The full
    bull/bear LLM transcript. 404s for a non-owner.
  * ``GET /api/strategies/{id}/returns`` — REASONING. The per-day series is the
    raw backtest output; from it a reader can reconstruct positions and clone
    the strategy. The *headline* stats derived from it (Sharpe, CAGR, max
    drawdown) stay on the public card. 404s for a non-owner.
  * ``POST /api/paper/deployments`` (``_spec_for_strategy``) — REASONING. The
    validated DSL spec is the strategy's executable logic: the thing a
    marketplace would license, not give away. There is no licensing flow yet,
    so this fails closed; it can be reopened deliberately when one exists.
  * ``GET /api/strategies/{id}`` — MIXED. Stays card-public (a 404 here would
    take the public detail page down for published strategies); its two
    reasoning fields are stripped for non-owners instead. ``brief_intent``
    takes ``owns_strategy`` (#1547) — a curated house row has no owner and no
    brief, so "public for is_example" would be meaningless there.
    ``strategy_spec`` (#1646) takes ``is_strategy_reasoning_visible``, because
    curated rows DO carry specs and are public demo content. Two fields, two
    predicates, the difference deliberate — do not "unify" them.
  * ``GET /api/selection-bias/gate/{id}`` — CARD, deliberately. Every number it
    returns (``dsr_p_value`` / ``pbo_score`` / ``out_of_sample_sharpe`` /
    ``deflated_sharpe_ratio`` / ``passes_rigor_gate``) is ALREADY served
    anonymously for published rows by ``_public_generated_strategy_responses``
    on ``GET /api/leaderboard``. Gating it would close nothing while the same
    values stayed public one route over, and would break the public
    verify-the-claim affordance the product's whole positioning rests on.
    Pinned by ``test_generated_strategy_published_visible_to_anonymous_caller``.
  * List surfaces (``/``, ``/generated``, ``/passports``, the leaderboard) —
    CARD. Unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from archimedes.models.strategy_store import StrategyRecord


def _field(row: StrategyRecord | dict, name: str, default: Any = None) -> Any:
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def owns_strategy(
    row: StrategyRecord | dict | None,
    caller_wallet: str | None,
    *,
    caller_user_id: str | None = None,
) -> bool:
    """Does *caller* OWN this row? Publish/example state is irrelevant here.

    THE single implementation of the two-tier ownership match. Both public
    predicates below delegate to it, and so does every call site that needs
    "is this MINE" rather than "am I allowed to see this"
    (``_owned_generated_strategy_responses``, the ``brief_intent`` gate in
    ``get_strategy``). Never re-implement it at a call site: this codebase's
    characteristic defect is a rule being fixed in the one function the current
    ticket touches while sibling readers keep the old behaviour, and an
    ownership rule that disagrees with itself across two routes is an
    authorization bug, not an inconsistency.

    OWNERSHIP IS TWO-TIERED, because canonical identity was introduced after
    rows already existed (Better Auth ``auth_users.id``):

      - If the row carries an ``owner_user_id``, that is the ONLY thing that
        grants ownership. A matching wallet must NOT, or the canonical model is
        bypassable by anyone who controls a wallet the row happens to name --
        which would make the migration to canonical identity a downgrade in
        security rather than an upgrade.
      - If ``owner_user_id`` is NULL, the row predates canonical identity and
        falls back to the wallet comparison this module has always done.
    """
    if row is None:
        return False

    owner_user_id = _field(row, "owner_user_id")
    if owner_user_id is not None:
        # Canonical ownership. Note both sides must be non-empty: a caller with
        # no resolved user must never match a row, and `None == None` is not
        # ownership.
        return bool(caller_user_id) and str(owner_user_id) == str(caller_user_id)

    owner = _field(row, "owner_wallet")
    if not caller_wallet or not owner:
        return False

    return str(owner).strip().lower() == str(caller_wallet).strip().lower()


def is_strategy_visible(
    row: StrategyRecord | dict | None,
    caller_wallet: str | None,
    *,
    caller_user_id: str | None = None,
) -> bool:
    """CARD-level visibility: may the caller read this strategy AT ALL?

    A strategy's card is visible if:
      1. row is not None AND row.is_example is True, OR
      2. row.is_published is True, OR
      3. the caller owns it (see ``owns_strategy``).

    **This predicate means "readable by ANYONE", including anonymous callers,
    the moment ``is_published`` flips.** That is correct for a card and WRONG
    for reasoning — it was the #1557 hole: five consumers used it to gate
    reasoning-disclosure surfaces, so publishing a strategy handed its full
    generation debate to the internet. Reasoning surfaces call
    ``is_strategy_reasoning_visible`` instead. Read the module docstring's
    matrix before choosing between them.

    This predicate is the single source of truth for card-level visibility. It
    is deliberately not re-implemented at call sites: a visibility rule that
    disagrees with itself across two routes is an authorization bug, not an
    inconsistency.
    """
    if row is None:
        return False

    if _field(row, "is_example", False) or _field(row, "is_published", False):
        return True

    return owns_strategy(row, caller_wallet, caller_user_id=caller_user_id)


def is_strategy_reasoning_visible(
    row: StrategyRecord | dict | None,
    caller_wallet: str | None,
    *,
    caller_user_id: str | None = None,
) -> bool:
    """REASONING-level visibility: may the caller read HOW this strategy was
    derived — the debate transcript, the full daily-return series, the
    executable DSL spec?

    Reasoning is visible if:
      1. row is not None AND row.is_example is True (house demo content — the
         product already renders its reasoning to anonymous visitors; see the
         module docstring for the verification), OR
      2. the caller owns it.

    ``is_published`` is DELIBERATELY ABSENT from that list, and its absence is
    the entire point of this function (#1557). Publishing consents to sharing
    the result, not the derivation. If a future change adds an ``is_published``
    clause here, it re-opens the hole: an anonymous ``GET
    /api/strategies/{id}/debate`` on any published row returns that user's full
    generation transcript.
    """
    if row is None:
        return False

    if _field(row, "is_example", False):
        return True

    return owns_strategy(row, caller_wallet, caller_user_id=caller_user_id)
