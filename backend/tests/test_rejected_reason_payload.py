"""A rejected strategy's card must state ITS OWN reason, not a shared paragraph.

The owner's screenshot: the Library's "Rejected (1) — did not pass the rigor
gate" card showed "—" for Sharpe / CAGR / Max DD, a "Gen tokens" count, and one
paragraph of prose asserting that "most rejections at this stage are 'return
series too short' … A longer backtest window typically unlocks them". Nothing
had measured that, and the card carried no field from which the real reason
could be read — so for a candidate rejected for a DIFFERENT reason the page
stated something false about it.

``services/rigor_reasons.rigor_reasons_for_verdict`` derives the per-check
report from the strategy's own stored ``rigor_verdict`` — the blob
``debate_engine._rigor_verdict_dict`` writes on the graded fusion/debate path,
``debate_engine._abstain_result`` and ``generation_pipeline``'s fixture branch
write on the non-graded ones, and ``generation_pipeline._patch_pbo`` then patches
— and ``GET /api/strategies/generated`` serves it as the additive
``rigor_reasons`` field.

Hermetic (tmp-sqlite, no .env / network / Redis); the DB fixture copies the
``_use_tmp_db`` pattern from ``test_generated_citation_truth.py``.
"""

from __future__ import annotations

import json
import time

import archimedes.db as db
import pytest
from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session
from archimedes.services.rigor_profiles import OOS_ABS_FLOOR, STRICTEST_LEVEL, get_profile
from archimedes.services.rigor_reasons import (
    FAIL,
    NOT_COMPUTED,
    PASS,
    SHORT_SERIES_REASON,
    rigor_reasons_for_verdict,
)
from httpx import ASGITransport, AsyncClient

_W_OWNER = "0xAbC0000000000000000000000000000000000009"
_BADGE = get_profile(STRICTEST_LEVEL)

# A candidate the gate actually GRADED and rejected on the numbers. Shape copied
# from debate_engine._rigor_verdict_dict, which is the only writer of a graded
# verdict — note it always emits the four-state ``look_ahead_status`` alongside
# the fail-closed boolean, and derives the boolean FROM it. A graded row without
# a status does not exist on any live path, and this module will not read one as
# an audit pass (see test_a_bare_lookahead_true_is_not_a_passed_audit).
_GRADED_FAIL = {
    "dsr": 0.31,
    "dsr_p_value": 0.08,
    "pbo": 0.62,
    "oos_sharpe": -0.14,
    "in_sample_sharpe": 1.4,
    "lookahead_audit_passed": True,
    "look_ahead_status": "pass",
    "passing": False,
    "num_trials": 6,
}

# A candidate the gate never graded — the branch the deleted paragraph claimed
# was "most" of them. Shape copied from generation_pipeline._rigor_verdict_for's
# too-short return.
_TOO_SHORT = {
    "dsr": None,
    "pbo": None,
    "oos_sharpe": None,
    "in_sample_sharpe": None,
    "lookahead_audit_passed": True,
    "passing": False,
    "reason": SHORT_SERIES_REASON,
}


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'rejected_reasons.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
    db.init_db()
    yield


def _siwe_cookies(wallet: str) -> dict[str, str]:
    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


def _mk_strategy(sid: str, verdict: dict | None, *, name: str = "Rejected candidate"):
    from archimedes.models.strategy_store import StrategyRecord

    with db.get_session() as session:
        session.add(
            StrategyRecord(
                id=sid,
                content_hash=("0x" + sid).ljust(66, "0"),
                generation_method="fusion",
                source_papers="[]",
                strategy_name=name,
                thesis="test thesis",
                asset_universe="[]",
                risk_profile="moderate",
                status="rejected",
                rigor_verdict=json.dumps(verdict) if verdict is not None else None,
                is_example=False,
                owner_wallet=_W_OWNER.lower(),
                is_published=False,
            )
        )
        session.commit()


def _by_key(report: dict) -> dict[str, dict]:
    return {c["key"]: c for c in report["checks"]}


# ── The report names the checks, with the gate's own thresholds ─────────────


def test_graded_rejection_names_every_failing_check_with_its_threshold():
    """The screenshot's missing half: which checks said no, and against what."""
    report = rigor_reasons_for_verdict(_GRADED_FAIL)
    checks = _by_key(report)

    assert checks["dsr"]["status"] == FAIL
    assert checks["dsr"]["value"] == pytest.approx(0.08)
    assert checks["dsr"]["threshold"] == pytest.approx(_BADGE.dsr_p_min)
    assert "0.08" in checks["dsr"]["detail"] and f"{_BADGE.dsr_p_min:.2f}" in checks["dsr"]["detail"]

    assert checks["pbo"]["status"] == FAIL
    assert checks["pbo"]["threshold"] == pytest.approx(_BADGE.pbo_max)

    assert checks["oos_sharpe"]["status"] == FAIL
    assert checks["oos_sharpe"]["threshold"] == pytest.approx(OOS_ABS_FLOOR)

    # Passed checks are shown as passed — the report is not a list of grievances.
    assert checks["look_ahead"]["status"] == PASS

    assert report["recorded_reason"] is None
    assert report["reason_code"] is None
    assert report["unattributed"] is False


def test_thresholds_are_the_gates_own_numbers_not_a_second_copy():
    """Every threshold printed must come from rigor_profiles, so the card can
    never quote a bar the gate does not enforce."""
    checks = _by_key(rigor_reasons_for_verdict(_GRADED_FAIL))
    assert checks["dsr"]["threshold"] == _BADGE.dsr_p_min
    assert checks["pbo"]["threshold"] == _BADGE.pbo_max
    assert checks["oos_is_ratio"]["threshold"] == _BADGE.oos_is_ratio_min
    assert checks["oos_sharpe"]["threshold"] == OOS_ABS_FLOOR


def test_short_series_rejection_reads_its_own_recorded_reason():
    report = rigor_reasons_for_verdict(_TOO_SHORT)
    assert report["recorded_reason"] == SHORT_SERIES_REASON
    assert report["reason_code"] == "short_return_series"
    assert report["min_returns_for_gate"] == 10


def test_two_strategies_rejected_for_different_reasons_get_different_reports():
    """THE defect, at the data layer. One paragraph cannot be true of both of
    these rows — a graded DSR/PBO/OOS failure and a candidate the gate never
    scored — so the payload has to distinguish them."""
    graded = rigor_reasons_for_verdict(_GRADED_FAIL)
    short = rigor_reasons_for_verdict(_TOO_SHORT)

    assert graded["reason_code"] != short["reason_code"]
    assert [c["status"] for c in graded["checks"]] != [c["status"] for c in short["checks"]]
    # The short-series row must NOT be described with the graded row's numbers.
    assert all("0.08" not in c["detail"] for c in short["checks"])


# ── Fail-closed: nothing on record is never rendered as a finding ───────────


def test_ungraded_checks_are_not_computed_never_failed():
    """A check that never ran must not be printed as a check that ran and
    failed — the same FAIL-vs-NOT_RUN distinction gate_details already keeps.
    It still does not count as a pass."""
    checks = _by_key(rigor_reasons_for_verdict(_TOO_SHORT))
    for key in ("dsr", "pbo", "oos_sharpe", "oos_is_ratio"):
        assert checks[key]["status"] == NOT_COMPUTED, key
        assert checks[key]["value"] is None, key
    assert not any(c["status"] == FAIL for c in rigor_reasons_for_verdict(_TOO_SHORT)["checks"])


def test_fixture_mode_does_not_claim_a_look_ahead_failure():
    """generation_pipeline's fixture verdict carries lookahead_audit_passed=False
    as a fail-closed DEFAULT, not an audit finding — the gate never ran. Calling
    that a failed audit would invent a finding out of a default."""
    from archimedes.services.rigor_reasons import FIXTURE_REASON

    report = rigor_reasons_for_verdict(
        {
            "dsr": None,
            "pbo": None,
            "oos_sharpe": None,
            "in_sample_sharpe": None,
            "lookahead_audit_passed": False,
            "passing": False,
            "reason": FIXTURE_REASON,
        }
    )
    assert _by_key(report)["look_ahead"]["status"] == NOT_COMPUTED
    assert report["reason_code"] == "fixture_mode"


def test_an_inconclusive_look_ahead_audit_is_not_a_failed_audit():
    report = rigor_reasons_for_verdict(
        {
            "dsr_p_value": 0.97,
            "pbo": 0.2,
            "oos_sharpe": 0.8,
            "in_sample_sharpe": 1.0,
            "lookahead_audit_passed": False,
            "look_ahead_status": "pending",
            "look_ahead_reason": "no inspectable source for the audit",
            "passing": False,
        }
    )
    look_ahead = _by_key(report)["look_ahead"]
    assert look_ahead["status"] == NOT_COMPUTED
    assert "no inspectable source for the audit" in look_ahead["detail"]
    assert "fail-closed" in look_ahead["detail"]


def test_a_real_look_ahead_finding_is_a_failure():
    report = rigor_reasons_for_verdict(
        {"dsr_p_value": 0.97, "pbo": 0.2, "oos_sharpe": 0.8, "lookahead_audit_passed": False, "passing": False}
    )
    assert _by_key(report)["look_ahead"]["status"] == FAIL


def test_a_default_number_on_an_ungraded_verdict_is_not_a_passed_check():
    """THE GUARD for the sentinel-as-measurement bug.

    ``_patch_pbo`` stamps ``pbo = 0.0  # PBO undefined for N<2`` onto every
    candidate that carries a recorded reason (they are all ``has_real_rigor
    =False``, so they are all in its ``agent_cands``). 0.0 is below the 0.50
    ceiling, so a naive read renders "PBO — 0.00 < 0.50 required" under
    **Passed** on a candidate the gate never scored. The fixture here is not
    hand-written: it is the real blob ``debate_engine._abstain_result`` produces
    after the real ``_patch_pbo`` runs, so it cannot drift from the pipeline."""
    from archimedes.agents.debate_engine import _abstain_result
    from archimedes.agents.generation_pipeline import _patch_pbo

    cand = _abstain_result("c-abstain", regime="neutral", reason="society could not agree on a candidate")
    _patch_pbo([cand])
    assert cand.rigor_verdict["pbo"] == 0.0, "pipeline no longer stamps the N<2 sentinel — re-check this guard"

    report = rigor_reasons_for_verdict(cand.rigor_verdict)
    assert _by_key(report)["pbo"]["status"] == NOT_COMPUTED
    assert _by_key(report)["pbo"]["value"] is None
    # Nothing on an ungraded verdict may be reported as a check that cleared.
    assert not [c for c in report["checks"] if c["status"] == PASS]
    assert report["recorded_reason"] == "society could not agree on a candidate"


def test_a_bare_lookahead_true_is_not_a_passed_audit():
    """``_lookahead_for_candidate`` is "vacuously True when none expose auditable
    source", and ``dsl_lookahead_audit.verdict_from_persisted_row`` retired
    exactly this rendering. With no four-state ``look_ahead_status`` there is no
    audit result to report — a stored ``True`` is a default, not a finding."""
    report = rigor_reasons_for_verdict(
        {"dsr_p_value": 0.97, "pbo": 0.2, "oos_sharpe": 0.8, "lookahead_audit_passed": True, "passing": False}
    )
    look_ahead = _by_key(report)["look_ahead"]
    assert look_ahead["status"] == NOT_COMPUTED
    assert "not an audit result" in look_ahead["detail"]


def test_an_inconclusive_status_beats_a_stored_lookahead_true():
    """The four-state status is the ONLY field a look-ahead claim keys off. A
    verdict that explicitly says the audit reached no verdict must not be
    rendered as a pass because a fail-closed boolean happens to read True."""
    report = rigor_reasons_for_verdict(
        {
            "lookahead_audit_passed": True,
            "look_ahead_status": "pending",
            "look_ahead_reason": "spec had no auditable source",
            "passing": False,
        }
    )
    look_ahead = _by_key(report)["look_ahead"]
    assert look_ahead["status"] == NOT_COMPUTED
    assert "spec had no auditable source" in look_ahead["detail"]


def test_a_conclusive_look_ahead_pass_still_passes():
    """The fusion writer derives the boolean from ``look_ahead_status == "pass"``
    (fusion_evaluator.RigorVerdict), so a genuinely audited row is unaffected."""
    report = rigor_reasons_for_verdict(
        {"look_ahead_status": "pass", "lookahead_audit_passed": True, "pbo": 0.2, "passing": False}
    )
    assert _by_key(report)["look_ahead"]["status"] == PASS


def test_the_ratio_check_names_the_side_that_is_actually_missing():
    """ "No positive in-sample Sharpe to compare against" is false on a row that
    HAS one and is missing the out-of-sample leg."""
    with_is = _by_key(rigor_reasons_for_verdict({"in_sample_sharpe": 1.2, "oos_sharpe": None, "passing": False}))
    assert with_is["oos_is_ratio"]["status"] == NOT_COMPUTED
    assert with_is["oos_is_ratio"]["detail"] == "no out-of-sample Sharpe to compare"

    without_is = _by_key(rigor_reasons_for_verdict({"in_sample_sharpe": -0.3, "oos_sharpe": 0.4, "passing": False}))
    assert without_is["oos_is_ratio"]["detail"] == "no positive in-sample Sharpe to compare against"


def test_nan_is_not_a_measurement():
    """A NaN metric must not be printed as a number, nor skip its fail branch."""
    checks = _by_key(
        rigor_reasons_for_verdict(
            {"dsr_p_value": float("nan"), "pbo": float("inf"), "oos_sharpe": None, "passing": False}
        )
    )
    assert checks["dsr"]["status"] == NOT_COMPUTED
    assert checks["pbo"]["status"] == NOT_COMPUTED


def test_no_verdict_yields_no_report():
    """A row with nothing on record gets no block at all — an honest absence,
    not a set of checks that never ran."""
    assert rigor_reasons_for_verdict(None) is None
    assert rigor_reasons_for_verdict("not-a-dict") is None


def test_a_passing_verdict_reports_no_failures():
    """The block must not cry wolf on a strategy that cleared the bar."""
    report = rigor_reasons_for_verdict(
        {
            "dsr_p_value": 0.97,
            "pbo": 0.2,
            "oos_sharpe": 0.9,
            "in_sample_sharpe": 1.2,
            "lookahead_audit_passed": True,
            "look_ahead_status": "pass",
            "passing": True,
        }
    )
    assert not [c for c in report["checks"] if c["status"] == FAIL]
    assert [c["key"] for c in report["checks"] if c["status"] == PASS] == [
        "dsr",
        "pbo",
        "oos_sharpe",
        "oos_is_ratio",
        "look_ahead",
    ]
    assert report["unattributed"] is False


def test_unattributed_when_nothing_on_record_falls_below_the_bar():
    """A rejected row on which every recorded check CLEARS the bar.

    The shape was first produced by a DSR-bar divergence: the agent path graded
    DSR at its own hardcoded, stricter bar while the badge profile's ``dsr_p_min``
    was looser, so a p-value in between landed a rejected row with every badge-bar
    check clear. #1794 collapsed both onto ``DSR_P_BADGE_MIN``, so this fixture is
    built AGAINST the live bar instead of against a number that used to sit
    between two of them — the state it pins is still reachable (a stored row
    graded under the retired bar; a leg that never ran, which fails admission
    closed yet reports ``not_computed``, never ``fail``).

    The surface must say it cannot attribute the rejection, not name a culprit.
    """
    clears_the_bar = (_BADGE.dsr_p_min + 1.0) / 2
    report = rigor_reasons_for_verdict(
        {
            "dsr_p_value": clears_the_bar,
            "pbo": 0.2,
            "oos_sharpe": 0.9,
            "in_sample_sharpe": 1.2,
            "lookahead_audit_passed": True,
            "look_ahead_status": "pass",
            "passing": False,
        }
    )
    assert report["unattributed"] is True
    # Anti-vacuity: unattributed here must come from every leg CLEARING the bar,
    # not from a report full of not-computed legs (which is a different state
    # with a different surface).
    assert [c["status"] for c in report["checks"]] == [PASS] * 5
    assert not [c for c in report["checks"] if c["status"] == FAIL]


# ── The pipeline still writes what this module classifies on ───────────────


def test_pipeline_still_writes_the_short_series_reason():
    """Pins SHORT_SERIES_REASON and the 10-observation minimum to the
    pipeline's ACTUAL behaviour, so the classification (and the count the
    frontend prints) can never drift away from what gets stored."""
    from archimedes.agents.generation_pipeline import _rigor_verdict_for
    from archimedes.services.rigor_reasons import MIN_RETURNS_FOR_GATE

    series = [0.001 * (i % 3 - 1) for i in range(MIN_RETURNS_FOR_GATE - 1)]
    verdict = _rigor_verdict_for(series, 1)
    assert verdict["reason"] == SHORT_SERIES_REASON
    assert rigor_reasons_for_verdict(verdict)["reason_code"] == "short_return_series"

    # One more observation and the gate grades it — no recorded reason at all.
    graded = _rigor_verdict_for([*series, 0.002], 1)
    assert "reason" not in graded


# ── The route serves it, per row, with no extra query ──────────────────────


async def test_generated_route_carries_each_rows_own_reasons():
    """THE GUARD: the rejected row's payload carries ITS OWN reasons — the field
    the card reads. Drop `d["rigor_reasons"] = ...` from
    strategies_routes.list_generated_strategies and this goes red."""
    _mk_strategy("rej00000000000001", _GRADED_FAIL, name="Graded rejection")
    _mk_strategy("rej00000000000002", _TOO_SHORT, name="Too short")

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/generated", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    by_id = {s["id"]: s for s in resp.json()["strategies"]}
    assert set(by_id) == {"rej00000000000001", "rej00000000000002"}

    graded = by_id["rej00000000000001"]["rigor_reasons"]
    short = by_id["rej00000000000002"]["rigor_reasons"]
    assert graded is not None and short is not None

    graded_checks = {c["key"]: c for c in graded["checks"]}
    assert graded_checks["dsr"]["status"] == FAIL
    assert f"{_BADGE.dsr_p_min:.2f}" in graded_checks["dsr"]["detail"]
    assert graded["recorded_reason"] is None

    # Each row carries its OWN reason — not the other's, and not a shared one.
    assert short["recorded_reason"] == SHORT_SERIES_REASON
    assert short["reason_code"] == "short_return_series"
    assert graded["reason_code"] != short["reason_code"]


async def test_generated_route_reasons_add_no_queries():
    """Additive and batched: the reasons are derived from the rigor_verdict the
    row already carries, so a page of rows costs the same number of SELECTs as
    a single row did."""
    from sqlalchemy import event

    for i in range(6):
        _mk_strategy(f"rejq0000000000{i:02d}", _GRADED_FAIL, name=f"Rejected {i}")

    statements: list[str] = []

    def _record(_conn, _cursor, statement, *_rest):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    from archimedes.main import app

    event.listen(db.engine, "before_cursor_execute", _record)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/strategies/generated", cookies=_siwe_cookies(_W_OWNER))
    finally:
        event.remove(db.engine, "before_cursor_execute", _record)

    assert resp.status_code == 200
    rows = resp.json()["strategies"]
    assert len(rows) == 6
    assert all(r["rigor_reasons"] is not None for r in rows)
    # Six rows must not cost six lookups. The route's own per-page reads
    # (strategies, costs, publish rights, corpus meta) are a small constant;
    # anything scaling with row count would blow past it.
    assert len(statements) <= 8, f"expected a constant number of SELECTs, got {len(statements)}: {statements}"


async def test_a_row_with_no_verdict_carries_a_null_reasons_field():
    """Honest absence, on the wire: the field is present and null, so the card
    renders no block rather than an empty one."""
    _mk_strategy("rejnull000000001", None, name="No verdict on record")

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/strategies/generated", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    row = resp.json()["strategies"][0]
    assert "rigor_reasons" in row
    assert row["rigor_reasons"] is None
