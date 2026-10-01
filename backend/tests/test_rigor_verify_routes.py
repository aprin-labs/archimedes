"""``POST /api/rigor/verify`` — the CLI ``verify`` command backend (#1305).

Covers the honesty contract this endpoint exists to enforce:

  1. every route 401s without a Better Auth session;
  2. DSR and walk-forward OOS consistency are EVALUABLE and reuse the exact
     same gate functions/thresholds the passport verdict uses
     (``compute_dsr_hac_and_iid`` gated on ``DSR_P_FLOOR``,
     ``compute_oos_sharpe`` gated on ``OOS_ABS_FLOOR``) — never new thresholds;
  3. PBO and look-ahead are ALWAYS ``not_evaluable`` for a bare returns series
     — never silently passed, never defaulted, never reported as a FAIL they
     were never evaluated for;
  4. ``passes`` is true only when EVERY RUNNABLE leg (DSR + OOS consistency)
     actually ran and passed — a quorum, not "no evaluable check failed"
     (#1481). Neither the zero-evaluable case (vacuous truth) nor the
     one-evaluable case (a leg that could not run) may read as passing;
  4a. the MINIMUM EVALUATION WINDOW is 250 daily bars — one trading year
     (owner decision, #1803). 249 bars is a TYPED REFUSAL naming
     ``bars_received`` and ``bars_required``, never a verdict and never a
     warning-labelled verdict; 250 is graded, and at 250 both runnable legs
     really do run;
  5. ``trials`` is self-attested, `>= 1` enforced at the schema (0 is a 422,
     not a silently-accepted degenerate deflation);
  6. the ADVERSARIAL demonstration the repo's guard-review rule requires: a
     synthetic series constructed to FAIL both evaluable checks is shown
     failing both, not merely "not passing" for an unrelated reason.

Hermetic: mocks only the Better Auth session fetch (the account-session
boundary); the rigor math runs for real against synthetic numpy series with a
fixed seed, so the DSR/OOS numbers below are reproducible, not fixtures.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from archimedes.api import account_auth, rigor_verify_routes
from archimedes.services.rigor_evaluator import DSR_MIN_BARS, compute_dsr_hac_and_iid, compute_oos_sharpe
from fastapi import FastAPI

# ── Synthetic, reproducible fixture series ──────────────────────────────
# Deterministic (fixed seed) so CI and local runs see byte-identical DSR/OOS
# numbers — not tuned by hand to "happen" to pass/fail.

_STRONG_SERIES = np.random.default_rng(7).normal(0.0015, 0.004, 300).tolist()  # DSR PASS, OOS PASS
_WEAK_SERIES = np.random.default_rng(11).normal(-0.002, 0.01, 300).tolist()  # DSR FAIL, OOS FAIL

# The minimum evaluation window — one trading year (owner decision, #1803).
# Read from the module rather than restated, so a change to the constant lands
# in these tests instead of leaving them green against a number that moved.
_WINDOW = rigor_verify_routes._MIN_RETURN_ROWS

# Exactly at the floor, and exactly one bar under it. The pair is the whole
# point: 250 is graded, 249 is REFUSED — not graded with a caveat.
_AT_WINDOW_SERIES = np.random.default_rng(7).normal(0.0015, 0.004, _WINDOW).tolist()
_ONE_BAR_SHORT_SERIES = _AT_WINDOW_SERIES[:-1]

_SHORT_SERIES = [0.01, -0.02, 0.015]  # T=3: far under the window -> refused at the schema (#1803)
# Long enough to be accepted, zero-variance so NEITHER runnable leg can run.
# Post-window this is the only way to reach `legs_evaluated == 0` through the
# route: shortness cannot produce it any more, degeneracy still can.
_DEGENERATE_SERIES = [0.0] * _WINDOW

# #1481: the ONE-evaluable shape. DSR becomes evaluable at T>=4; the OOS split
# needs >=21 OOS bars, i.e. ~70 total at 70/30. So for 4 <= T < ~70 exactly one
# runnable leg could run, and the pre-quorum rule returned `passes: true` on it.
# The 250-bar window now refuses that body outright (asserted below), so the
# quorum itself is pinned on a full-length series with the OOS leg forced
# not_evaluable — the invariant survives the transport that used to reach it.
_PARTIAL_SERIES = np.random.default_rng(7).normal(0.0015, 0.004, 4).tolist()


def _returns_body(series: list[float], trials: int = 1) -> dict:
    base = datetime(2024, 1, 1)
    return {
        "returns": [
            {"date": (base + timedelta(days=i)).date().isoformat(), "daily_return": r} for i, r in enumerate(series)
        ],
        "trials": trials,
    }


@pytest.fixture()
def app():
    application = FastAPI()
    application.middleware("http")(account_auth.better_auth_session_middleware)
    application.include_router(rigor_verify_routes.rigor_verify_router)
    return application


def _session_for(user_id: str):
    async def fetch(_request):
        return {
            "user": {"id": user_id, "name": user_id, "email": f"{user_id}@example.com", "emailVerified": True},
            "session": {"id": f"s-{user_id}", "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        }

    return fetch


def _sign_in(monkeypatch, user_id: str = "user-1"):
    monkeypatch.setattr(account_auth, "_fetch_session", _session_for(user_id))


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"cookie": "better-auth.session_token=opaque", "host": "archimedes-arc.com"},
    )


# ── 1. Auth gate ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_requires_a_better_auth_session(app, monkeypatch):
    monkeypatch.setattr(account_auth, "_fetch_session", AsyncMock(return_value=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES))
    assert resp.status_code == 401


# ── 2/3/4. The honesty contract ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_strong_lookahead_free_series_passes_both_evaluable_checks(app, monkeypatch):
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES))
    assert resp.status_code == 200
    body = resp.json()

    assert body["dsr"]["status"] == "pass"
    assert body["dsr"]["dsr_p_value"] >= 0.50  # DSR_P_FLOOR, reused not reinvented
    assert body["oos_consistency"]["status"] == "pass"
    assert body["oos_consistency"]["oos_sharpe"] > 0.0

    # PBO and look-ahead are structurally not_evaluable for a bare series —
    # even on a strong, clean series, never silently passed.
    assert body["pbo"]["status"] == "not_evaluable"
    assert body["pbo"]["reason"]
    assert body["look_ahead"]["status"] == "not_evaluable"
    assert body["look_ahead"]["reason"]

    assert body["passes"] is True
    assert body["trials"] == 1
    assert body["self_attested"] is True


@pytest.mark.asyncio
async def test_weak_series_fails_dsr_and_oos_the_adversarial_demonstration(app, monkeypatch):
    """The guard-review rule (CLAUDE.md): show the input that SHOULD fail the
    guard actually failing it — not merely absent from the passing case."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_WEAK_SERIES))
    assert resp.status_code == 200
    body = resp.json()

    assert body["dsr"]["status"] == "fail"
    assert body["dsr"]["dsr_p_value"] < 0.50
    assert body["oos_consistency"]["status"] == "fail"
    assert body["oos_consistency"]["oos_sharpe"] <= 0.0

    assert body["pbo"]["status"] == "not_evaluable"
    assert body["look_ahead"]["status"] == "not_evaluable"

    assert body["passes"] is False


@pytest.mark.asyncio
async def test_synthetic_failing_series_pbo_is_not_evaluable_never_a_default_pass_or_fail(app, monkeypatch):
    """Mirrors the issue's stated acceptance check: on a failing series,
    pbo.status == "not_evaluable" — never silently passed, never a FAIL for a
    check that was structurally never run."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_WEAK_SERIES))
    body = resp.json()
    assert body["passes"] is False
    assert body["pbo"]["status"] == "not_evaluable"


@pytest.mark.asyncio
async def test_zero_evaluable_legs_never_read_as_a_pass(app, monkeypatch):
    """No evaluable check at all — passes must be False by construction
    (vacuous truth is not honesty), not True because nothing failed.

    #1803 changed how this state is reached, not that it must hold. A short
    series is refused at the schema (``window_too_short``), so the
    zero-evaluable case is now a DEGENERATE one: a full trading year of
    IDENTICAL bars is long enough to be accepted, and zero-variance, so neither
    the DSR nor the walk-forward split can produce a number. Both runnable legs
    come back ``not_evaluable`` and the verdict must still be False. This is
    also the proof that the window did not make ``legs_evaluated == 0``
    unreachable — it made SHORTNESS an unreachable cause of it.
    """
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_DEGENERATE_SERIES))
    assert resp.status_code == 200
    body = resp.json()

    assert body["dsr"]["status"] == "not_evaluable"
    assert body["oos_consistency"]["status"] == "not_evaluable"
    assert body["pbo"]["status"] == "not_evaluable"
    assert body["look_ahead"]["status"] == "not_evaluable"
    assert body["legs_evaluated"] == 0
    assert body["passes"] is False  # nothing evaluable -> never vacuously "passes"


# ── 5. trials validation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trials_zero_is_rejected_422(app, monkeypatch):
    """trials is self-attested but must be >= 1 — a degenerate/absent trial
    count must not silently pass schema validation and reach the DSR math."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES, trials=0))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_trials_default_is_one_and_is_echoed_self_attested(app, monkeypatch):
    _sign_in(monkeypatch)
    body = _returns_body(_STRONG_SERIES)
    del body["trials"]  # omit -> default
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=body)
    assert resp.status_code == 200
    assert resp.json()["trials"] == 1


@pytest.mark.asyncio
async def test_higher_declared_trials_deflates_dsr_p_value(app, monkeypatch):
    """The DSR must actually respond to the declared trial count (deflation is
    real, not decorative) — more trials -> harder to clear the same floor."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        p1 = (await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES, trials=1))).json()["dsr"][
            "dsr_p_value"
        ]
        p50 = (await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES, trials=50))).json()["dsr"][
            "dsr_p_value"
        ]
    assert p50 < p1


# ── 6. rf convention (#1409 review fix) ─────────────────────────────────
# Prior to this fix, `verify_rigor` discarded `ReturnPoint.date` entirely —
# the request already carries real per-bar dates (the CLI builds them, see
# `cli/src/archimedes_cli/cli.py`), but every DSR/OOS call ran on the flat
# 5% fallback with no `rf_convention` disclosed at all. Far-future dates
# below use year 3000 (matches the convention in
# `test_rf_convention_gate.py::test_run_rigor_gate_rf_convention_falls_back_beyond_coverage`)
# so this stays out-of-coverage regardless of how far `DGS3MO.csv` is ever
# refreshed.


def _far_future_body(series: list[float], trials: int = 1) -> dict:
    # #1803: consecutive far-future days, not a `% 28` cycle. The cycle produced
    # duplicate, non-ascending dates, which the strict input contract now
    # refuses outright — and which were never a legitimate way to reach the
    # fallback path in the first place.
    base = datetime(3000, 1, 1)
    return {
        "returns": [
            {"date": (base + timedelta(days=i)).date().isoformat(), "daily_return": r} for i, r in enumerate(series)
        ],
        "trials": trials,
    }


@pytest.mark.asyncio
async def test_rf_convention_is_the_series_when_request_dates_are_in_coverage(app, monkeypatch):
    """The request's dates (2024, well within the vendored series'
    coverage) must resolve to the T-bill series, not the flat fallback."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES))
    assert resp.status_code == 200
    assert resp.json()["rf_convention"] == "excess_tbill_series"


@pytest.mark.asyncio
async def test_rf_convention_falls_back_and_arithmetic_matches_the_flat_formula(app, monkeypatch):
    """Out-of-coverage dates must disclose the fallback AND produce
    byte-identical arithmetic to calling the same gate function with no
    dates at all — the marker and the arithmetic can never disagree, by
    construction (mirrors the same invariant `_resolve_gate_rf` enforces for
    `run_rigor_gate`)."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_far_future_body(_STRONG_SERIES))
    assert resp.status_code == 200
    body = resp.json()
    assert body["rf_convention"] == "excess_flat_fallback"

    dsr_flat, p_flat, _dsr_iid, _p_iid = compute_dsr_hac_and_iid(
        _STRONG_SERIES, 1, average_correlation=0.0, hac_lags="auto"
    )
    assert body["dsr"]["deflated_sharpe"] == pytest.approx(dsr_flat, abs=1e-6)
    assert body["dsr"]["dsr_p_value"] == pytest.approx(p_flat, abs=1e-6)


@pytest.mark.asyncio
async def test_rf_convention_series_arithmetic_measurably_differs_from_fallback(app, monkeypatch):
    """POSITIVE-direction regression guard (2026-08-21 review finding): the
    SAME return series graded with in-coverage dates vs out-of-coverage
    dates must produce a MEASURABLY DIFFERENT DSR — not merely a different
    `rf_convention` string next to identical numbers. This is exactly the
    guard whose absence let the pre-fix code discard `dates` silently: the
    marker would have been the only thing that ever changed."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        in_coverage = (await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES))).json()
        fallback = (await client.post("/api/rigor/verify", json=_far_future_body(_STRONG_SERIES))).json()

    assert in_coverage["rf_convention"] == "excess_tbill_series"
    assert fallback["rf_convention"] == "excess_flat_fallback"
    assert in_coverage["dsr"]["dsr_p_value"] != fallback["dsr"]["dsr_p_value"]
    assert in_coverage["dsr"]["deflated_sharpe"] != fallback["dsr"]["deflated_sharpe"]
    assert in_coverage["oos_consistency"]["oos_sharpe"] != fallback["oos_consistency"]["oos_sharpe"]


# ── empty returns rejected ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_returns_rejected_422(app, monkeypatch):
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json={"returns": [], "trials": 1})
    assert resp.status_code == 422
    # Zero bars is a window refusal like any other count under the floor —
    # same code, and it still reports what it received.
    assert _reason(resp) == "window_too_short"
    assert resp.json()["detail"]["bars_received"] == 0


# ── #1481: `passes` is a quorum over runnable legs ──────────────────────


@pytest.mark.asyncio
async def test_the_four_bar_one_evaluable_body_is_now_refused_outright(app, monkeypatch):
    """#1481's exact body, after the window (#1803). Four bars is where DSR
    becomes computable and the OOS split cannot run, so it is the shape that
    used to return ``passes: true`` on one leg of four. The endpoint no longer
    grades it at all — it is a `window_too_short` refusal, which is the
    strongest form of "this must not read as a pass".

    The quorum itself is still pinned, on a full-length series, by the test
    below: this one proves the transport can no longer even present the case.
    """
    _sign_in(monkeypatch)
    # The precondition, asserted rather than assumed: at 4 bars the DSR really
    # is computable and the OOS split really is not — this IS the one-evaluable
    # shape, it just can no longer be submitted.
    assert compute_dsr_hac_and_iid(_PARTIAL_SERIES, 1, average_correlation=0.0)[0] is not None
    assert compute_oos_sharpe(_PARTIAL_SERIES) is None

    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_PARTIAL_SERIES))
    assert resp.status_code == 422
    assert _reason(resp) == "window_too_short"
    assert resp.json()["detail"]["bars_received"] == 4


@pytest.mark.asyncio
async def test_a_leg_that_could_not_run_never_produces_a_pass(app, monkeypatch):
    """#1481 REGRESSION, kept reachable after the window closed its old door.

    A full-length series clearing DSR, with the walk-forward leg forced
    ``not_evaluable`` (a degenerate holdout does this for real — see the
    zero-variance case above), must still answer ``passes: false``: one leg of
    four is not a quorum. The old rule ("no evaluable check failed AND at least
    one was evaluable") returned ``passes: true`` on exactly this leg pattern.
    Reverting the quorum in ``rigor_verify_routes`` makes this test fail.
    """
    _sign_in(monkeypatch)
    monkeypatch.setattr(
        rigor_verify_routes,
        "_evaluate_oos_consistency",
        lambda *a, **k: rigor_verify_routes.OosConsistencyResult(
            status="not_evaluable", reason="forced: the walk-forward split could not run"
        ),
    )
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES))
    assert resp.status_code == 200
    body = resp.json()

    assert body["dsr"]["status"] == "pass"
    assert body["oos_consistency"]["status"] == "not_evaluable"
    assert body["pbo"]["status"] == "not_evaluable"
    assert body["look_ahead"]["status"] == "not_evaluable"

    assert body["passes"] is False
    assert body["legs_evaluated"] == 1
    assert body["legs_runnable"] == 2


@pytest.mark.asyncio
async def test_response_qualifies_the_scalar(app, monkeypatch):
    """`passes` must be qualifiable without the caller re-deriving leg statuses:
    the quorum counts and the structural cap are on every response."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES))
    body = resp.json()

    assert body["legs_total"] == 4
    assert body["legs_runnable"] == 2
    # Always capped on this transport — PBO and look-ahead can never run here,
    # so the verdict can never stand in for the passport gate.
    assert body["verdict_capped"] is True
    assert sorted(body["legs_not_run"]) == ["look_ahead", "pbo"]


@pytest.mark.asyncio
async def test_full_length_series_clearing_every_runnable_leg_still_passes(app, monkeypatch):
    """The quorum must not make the endpoint permanently unable to pass: a
    series long enough for both runnable legs, clearing both, still passes."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES))
    body = resp.json()

    assert body["dsr"]["status"] == "pass"
    assert body["oos_consistency"]["status"] == "pass"
    assert body["legs_evaluated"] == body["legs_runnable"] == 2
    assert body["passes"] is True


# ── #1803: the strict input contract, one red case per reason code ──────
#
# Every limit below exists because an unchecked input could move a LEG'S
# ANSWER without moving the data. The rule this file already follows (show the
# input that SHOULD fail the guard actually failing it) applies one case per
# code — and, for the ordering guard, with a demonstration that the shuffle it
# refuses is one that would otherwise have flipped the verdict.


def _reason(resp) -> str:
    """The machine-readable reason code on a 422 — `detail.reason`.

    The router renders its own refusals (`_input_rejected_response`) so the
    code an agent branches on is the one the validator raised, not a substring
    of an English sentence — and so the response never echoes the payload back
    (a NaN in `input` is literally unserialisable: `JSONResponse` uses
    `allow_nan=False`, which would turn the 422 into a 500).
    """
    detail = resp.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["error"] == "input_rejected", detail
    return detail["reason"]


def _dated(series: list[float], start: datetime | None = None) -> list[dict]:
    base = start or datetime(2024, 1, 1)
    return [{"date": (base + timedelta(days=i)).date().isoformat(), "daily_return": r} for i, r in enumerate(series)]


@pytest.mark.asyncio
async def test_reason_code_window_too_short(app, monkeypatch):
    """Under one trading year there is no verdict — only a typed refusal."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_SHORT_SERIES))
    assert resp.status_code == 422
    assert _reason(resp) == "window_too_short"
    assert str(_WINDOW) in json.dumps(resp.json())


@pytest.mark.asyncio
async def test_one_bar_under_the_window_is_refused_and_names_both_counts(app, monkeypatch):
    """THE owner decision (#1803), at its boundary. 249 bars is a refusal that
    states what it got and what it needs — as FIELDS, not only as prose, so a
    caller can decide "fetch more history" without parsing English.

    The refusal is also all there is: no `passes`, no leg statuses, nothing a
    consumer branching on `passes` could mistake for a graded answer.
    """
    _sign_in(monkeypatch)
    assert len(_ONE_BAR_SHORT_SERIES) == _WINDOW - 1
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_ONE_BAR_SHORT_SERIES))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "window_too_short"
    assert detail["bars_received"] == _WINDOW - 1
    assert detail["bars_required"] == _WINDOW
    assert str(_WINDOW - 1) in detail["message"] and str(_WINDOW) in detail["message"]
    assert "passes" not in resp.json(), "a refusal must not carry a verdict, warning-labelled or otherwise"


@pytest.mark.asyncio
async def test_exactly_the_window_is_accepted_and_both_runnable_legs_actually_run(app, monkeypatch):
    """The floor is a floor, not a wall — and it is set where the window BUYS
    something: at exactly 250 bars both runnable legs run, so the graded answer
    is complete (`legs_evaluated == legs_runnable`). The 4..69-bar hole that
    produced a DSR-only INCOMPLETE is unreachable through this route now."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_AT_WINDOW_SERIES))
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_bars"] == _WINDOW
    assert body["dsr"]["status"] in {"pass", "fail"}
    assert body["oos_consistency"]["status"] in {"pass", "fail"}
    assert body["legs_evaluated"] == body["legs_runnable"] == 2


@pytest.mark.asyncio
async def test_the_window_is_one_trading_year_and_never_below_the_gates_own_floor():
    """The floor is now a PRODUCT number (250 = one trading year, owner call),
    deliberately above the gate's arithmetic floors — but it may never fall
    BELOW `DSR_MIN_BARS`, or the route would accept a series whose DSR leg
    cannot run for want of bars. `max()` in the module makes that structural;
    this pins the value and the relationship."""
    assert rigor_verify_routes._MIN_WINDOW_BARS == 250
    assert rigor_verify_routes._MIN_RETURN_ROWS == 250
    assert rigor_verify_routes._MIN_RETURN_ROWS >= DSR_MIN_BARS
    # The window really is above BOTH arithmetic floors: the DSR is computable
    # here (it is not, one bar under `DSR_MIN_BARS`) and so is the OOS split.
    assert compute_dsr_hac_and_iid([0.01, -0.02, 0.015], 1, average_correlation=0.0)[0] is None, (
        "precondition: the DSR really is un-computable below the gate's own sample floor"
    )
    assert compute_dsr_hac_and_iid(_AT_WINDOW_SERIES, 1, average_correlation=0.0)[0] is not None
    assert compute_oos_sharpe(_AT_WINDOW_SERIES) is not None, (
        "the window must be long enough for the walk-forward split to run, or it buys nothing"
    )


@pytest.mark.asyncio
async def test_reason_code_too_many_rows(app, monkeypatch):
    """The #1749 cap, unchanged at 2,600 — now with a reason code on it."""
    _sign_in(monkeypatch)
    rows = _dated([0.001] * (rigor_verify_routes._MAX_RETURN_ROWS + 1))
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json={"returns": rows, "trials": 1})
    assert resp.status_code == 422
    assert _reason(resp) == "too_many_rows"
    assert "2600" in json.dumps(resp.json())


@pytest.mark.asyncio
async def test_the_cap_itself_is_unchanged_at_2600(app, monkeypatch):
    """A full-cap payload is still ACCEPTED — the hardening tightened the
    floor and the shape, it did not quietly lower the ceiling."""
    _sign_in(monkeypatch)
    rows = _dated(np.random.default_rng(3).normal(0.0005, 0.004, rigor_verify_routes._MAX_RETURN_ROWS).tolist())
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json={"returns": rows, "trials": 1})
    assert resp.status_code == 200
    assert resp.json()["n_bars"] == 2600


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["01/02/2024", "20240102", "2024-W01-1", "2024-1-2", "2024-02-30", 1704153600])
async def test_reason_code_invalid_date(app, monkeypatch, bad):
    """Strict `YYYY-MM-DD`. Epoch seconds, ISO week dates, `YYYYMMDD` and an
    unpadded month are all things pydantic or `date.fromisoformat` would
    otherwise accept or guess at; `2024-02-30` is well-formed and not real."""
    _sign_in(monkeypatch)
    rows = _dated(_STRONG_SERIES)
    rows[5]["date"] = bad
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json={"returns": rows, "trials": 1})
    assert resp.status_code == 422
    assert _reason(resp) == "invalid_date"


@pytest.mark.asyncio
async def test_reason_code_duplicate_date(app, monkeypatch):
    _sign_in(monkeypatch)
    rows = _dated(_STRONG_SERIES)
    rows[40]["date"] = rows[39]["date"]  # two bars, one calendar day
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json={"returns": rows, "trials": 1})
    assert resp.status_code == 422
    assert _reason(resp) == "duplicate_date"
    assert rows[39]["date"] in json.dumps(resp.json()), "the message must name the repeated date"


@pytest.mark.asyncio
async def test_reason_code_unsorted_dates(app, monkeypatch):
    _sign_in(monkeypatch)
    rows = _dated(_STRONG_SERIES)
    rows[10], rows[11] = rows[11], rows[10]  # one adjacent swap is enough
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json={"returns": rows, "trials": 1})
    assert resp.status_code == 422
    assert _reason(resp) == "unsorted_dates"


@pytest.mark.asyncio
@pytest.mark.parametrize("poison", [float("nan"), float("inf"), float("-inf")])
async def test_reason_code_non_finite(app, monkeypatch, poison):
    """JSON's `NaN`/`Infinity` literals parse into Python floats — `json.dumps`
    emits them and `json.loads` accepts them, so this reaches the schema."""
    _sign_in(monkeypatch)
    rows = _dated(_STRONG_SERIES)
    rows[3]["daily_return"] = poison
    async with _client(app) as client:
        resp = await client.post(
            "/api/rigor/verify",
            content=json.dumps({"returns": rows, "trials": 1}),
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 422
    assert _reason(resp) == "non_finite"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [1.0000001, 5.0, -1.5, 100.0])
async def test_reason_code_out_of_range(app, monkeypatch, bad):
    """|r| <= 1.0 is the honest ceiling for ONE DAILY bar. A column of
    percentages (5.0 meaning +5%) or annualized figures inflates the Sharpe the
    whole verdict rests on, so it is refused rather than graded."""
    _sign_in(monkeypatch)
    rows = _dated(_STRONG_SERIES)
    rows[7]["daily_return"] = bad
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json={"returns": rows, "trials": 1})
    assert resp.status_code == 422
    assert _reason(resp) == "out_of_range"


@pytest.mark.asyncio
async def test_exactly_plus_and_minus_one_are_still_accepted(app, monkeypatch):
    """The boundary is inclusive: -1.0 is a total loss and +1.0 is a doubling,
    both of which really happen. The guard rejects >100%, not 100%."""
    _sign_in(monkeypatch)
    rows = _dated(_STRONG_SERIES)
    rows[7]["daily_return"] = 1.0
    rows[8]["daily_return"] = -1.0
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json={"returns": rows, "trials": 1})
    assert resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, 10_001, 10**18])
async def test_reason_code_trials_out_of_range(app, monkeypatch, bad):
    """`trials` deflates the DSR. Unbounded, `trials=10**18` drove the
    deflation to -inf and turned a FAIL into `not_evaluable` — a
    caller-controlled way to erase a failing verdict."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES, trials=bad))
    assert resp.status_code == 422
    assert _reason(resp) == "trials_out_of_range"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [1e18, 1e9, "10000000000", "0", " 10001 "])
async def test_trials_out_of_range_survives_every_spelling_of_the_number(app, monkeypatch, bad):
    """Review round 2. The guard used to test `isinstance(value, int)` only, so a
    JSON float or a numeric string with the same value slipped past it onto the
    declarative `le` — which types as `less_than_equal`, NOT one of this route's
    codes, so the refusal degraded to the generic list shape and the documented
    `trials_out_of_range` envelope never reached the caller. Both spellings are
    ordinary: `json.dumps(10**18)` emits `1e+18`. (`1e400` parses to `inf`.)"""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES, trials=bad))
    assert resp.status_code == 422
    assert _reason(resp) == "trials_out_of_range"


@pytest.mark.asyncio
@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN"])
async def test_a_non_finite_trials_literal_is_refused_with_the_documented_code(app, monkeypatch, literal):
    """Python's own `json.dumps(float("inf"))` emits `Infinity`, and `json.loads`
    accepts it — the same door the `non_finite` rule closed on `returns`. On
    `trials` it has to answer with a code too, not with the generic list shape."""
    _sign_in(monkeypatch)
    body = json.dumps(_returns_body(_STRONG_SERIES, trials=1)).replace('"trials": 1', f'"trials": {literal}')
    async with _client(app) as client:
        resp = await client.post(
            "/api/rigor/verify",
            content=body,
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 422
    assert _reason(resp) == "trials_out_of_range"


@pytest.mark.asyncio
async def test_trials_true_is_refused_rather_than_read_as_one(app, monkeypatch):
    """Review round 2. Pydantic's lax mode coerces `True` to `1`, so `trials: true`
    returned 200 with `trials: 1`: a route that refuses `20240102` as a date rather
    than guessing at it was guessing a boolean into a count of variants tried."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES, trials=True))
    assert resp.status_code == 422
    assert _reason(resp) == "trials_out_of_range"


@pytest.mark.asyncio
@pytest.mark.parametrize("good", [1, 10_000, 1.0, "7", 7.0])
async def test_a_well_formed_trial_count_is_still_accepted_in_every_spelling(app, monkeypatch, good):
    """The guard widens in order to BOUND, not in order to refuse: everything
    pydantic would have accepted still arrives."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES, trials=good))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_a_non_numeric_trials_keeps_pydantics_own_error(app, monkeypatch):
    """`trials: "many"` has no range to be out of. Inventing `trials_out_of_range`
    for it would be the wrong reason code, so it keeps the generic shape."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES, trials="many"))
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)


@pytest.mark.asyncio
async def test_trials_at_the_cap_is_accepted_and_still_deflates(app, monkeypatch):
    """10,000 is a bound, not a wall the endpoint stops working at."""
    _sign_in(monkeypatch)
    async with _client(app) as client:
        resp = await client.post("/api/rigor/verify", json=_returns_body(_STRONG_SERIES, trials=10_000))
    assert resp.status_code == 200
    body = resp.json()
    assert body["dsr"]["status"] in {"pass", "fail"}
    assert math.isfinite(body["dsr"]["deflated_sharpe"])


def test_every_declared_reason_code_has_a_red_case_in_this_file():
    """`INPUT_REJECTED_CODES` is the published list (CLI + MCP branch on it).
    A code added there without a test here is a claim with nothing behind it."""
    source = Path(__file__).read_text(encoding="utf-8")
    missing = [code for code in rigor_verify_routes.INPUT_REJECTED_CODES if f'_reason(resp) == "{code}"' not in source]
    assert not missing, f"reason codes with no red case in this file: {missing}"


# ── the shuffle attack, and its sorted twin ─────────────────────────────


@pytest.mark.asyncio
async def test_a_shuffle_that_would_have_flipped_the_oos_verdict_is_refused(app, monkeypatch):
    """THE finding. `compute_oos_sharpe` splits POSITIONALLY, and dates were
    carried but never used for ordering — so a caller could sort their own
    series by return, park the best 30% in the holdout, and collect
    `oos_consistency: pass` on numbers that fail chronologically.

    Both halves are asserted: the sorted-by-date twin FAILS the OOS leg, the
    sorted-by-return shuffle WOULD have passed it (proved by running the same
    gate function the endpoint calls on the shuffled values), and the endpoint
    refuses the shuffle rather than grading it.
    """
    _sign_in(monkeypatch)
    series = _WEAK_SERIES[:]  # fails OOS chronologically
    gamed = sorted(series)  # worst bars first, best 30% in the holdout

    # The attack really is an attack: same numbers, opposite OOS answer.
    assert compute_oos_sharpe(series) <= 0.0
    assert compute_oos_sharpe(gamed) > 0.0

    # The same (date, return) PAIRS, re-ordered worst-first. Nothing is
    # fabricated: every bar the caller really had is present exactly once, only
    # the row order is a lie — which is the whole attack, because row order is
    # what the split reads.
    chronological = _dated(series)
    shuffled = sorted(chronological, key=lambda row: row["daily_return"])
    assert [row["daily_return"] for row in shuffled] == gamed
    shuffled_dates = [row["date"] for row in shuffled]
    assert shuffled_dates != sorted(shuffled_dates), "precondition: the payload really is out of order"

    async with _client(app) as client:
        gamed_resp = await client.post("/api/rigor/verify", json={"returns": shuffled, "trials": 1})
        honest_resp = await client.post("/api/rigor/verify", json={"returns": chronological, "trials": 1})

    assert gamed_resp.status_code == 422
    assert _reason(gamed_resp) == "unsorted_dates"

    assert honest_resp.status_code == 200
    assert honest_resp.json()["oos_consistency"]["status"] == "fail"
    assert honest_resp.json()["passes"] is False


@pytest.mark.asyncio
async def test_a_shuffled_series_is_rejected_or_gives_its_sorted_twins_verdict(app, monkeypatch):
    """The invariant the whole ordering rule exists to buy, stated as the
    property rather than as one attack: a permutation of a series can never
    buy a DIFFERENT verdict from the series itself. Today it is rejected; if a
    future change ever chooses to sort instead, this still holds."""
    _sign_in(monkeypatch)
    rows = _dated(_STRONG_SERIES)
    shuffled = rows[:]
    np.random.default_rng(19).shuffle(shuffled)
    assert [r["date"] for r in shuffled] != [r["date"] for r in rows], "precondition: actually shuffled"

    async with _client(app) as client:
        sorted_resp = await client.post("/api/rigor/verify", json={"returns": rows, "trials": 1})
        shuffled_resp = await client.post("/api/rigor/verify", json={"returns": shuffled, "trials": 1})

    assert sorted_resp.status_code == 200
    if shuffled_resp.status_code == 200:
        assert shuffled_resp.json()["passes"] == sorted_resp.json()["passes"]
    else:
        assert shuffled_resp.status_code == 422
        assert _reason(shuffled_resp) == "unsorted_dates"


# ── in_sample_sharpe gets the legs' isfinite guard ──────────────────────


def test_non_finite_in_sample_sharpe_is_not_evaluable_never_a_null_metric(monkeypatch):
    """#1803 finding (2): `in_sample_sharpe` had no isfinite guard, so a
    non-finite value rendered as `null` — indistinguishable from "this metric
    was not computed". Non-finite bars are refused at the schema now, so this
    drives the defence-in-depth layer directly."""
    monkeypatch.setattr(rigor_verify_routes, "compute_in_sample_sharpe", lambda *a, **k: float("inf"))
    result = rigor_verify_routes._evaluate_oos_consistency(_STRONG_SERIES)
    assert result.status == "not_evaluable"
    assert result.in_sample_sharpe is None
    assert "not finite" in (result.reason or "")


def test_the_same_leg_still_grades_when_the_in_sample_sharpe_is_finite():
    """The guard above must not have made the leg permanently unevaluable."""
    result = rigor_verify_routes._evaluate_oos_consistency(_STRONG_SERIES)
    assert result.status == "pass"
    assert result.in_sample_sharpe is not None and math.isfinite(result.in_sample_sharpe)
