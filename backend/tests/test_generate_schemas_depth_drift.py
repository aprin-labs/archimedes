"""Issue #1363 — the Depth control must never offer a value the pipeline
silently clamps. Issue #1636 raised the ceiling 6 → 30, added a fuse TARGET
distinct from the hard floor, and gave every entry point ONE default.

Four things pinned here:

  1. GenerateBrief.max_papers' Pydantic bounds equal MIN_PAPERS /
     FUSION_MAX_PAPERS from strategy_fusion.py — the DRIFT GUARD. The two
     constants can't live as a single shared import (generate_schemas.py's
     own docstring explains the circular-import reason: strategy_fusion ->
     generation_json -> llm_backend -> archimedes.services.__init__
     [backwards-compat re-export of generation_pipeline] -> generation_pipeline
     -> back to generate_schemas, still mid-definition). This test is what
     keeps the two copies from drifting apart: raise FUSION_MAX_PAPERS
     without raising generate_schemas.py's ceiling (or vice versa) and this
     fails.
  2. The same guard for the DEFAULT (#1636). Before it, `/api/generate/start`
     defaulted to 5 and the (since-deleted, #1595) `/api/strategies/generate`
     bypass to 4, so two live generation routes retrieved different amounts of
     evidence for the same brief. The bypass died in #1595; the surviving
     copies — the schema and FusionBrief — must equal
     strategy_fusion.DEFAULT_MAX_PAPERS.
  3. An over-cap max_papers is REFUSED at the schema level, not silently
     downgraded (previously ge=1, le=20 accepted anything up to 20 and the
     pipeline clamped it to 6 with no signal to the caller). The over-cap
     probe moved 7 → 31 with the ceiling.
  4. The route rejects an over-cap request with 422 before any job is
     enqueued — same hermetic harness as test_generate_payment_gate.py
     (TestClient(app), auth_cookies(), mocked job store, asyncio.create_task
     patched out).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.agents.strategy_fusion import DEFAULT_MAX_PAPERS, FUSE_TARGET_MIN, FUSION_MAX_PAPERS, MIN_PAPERS
from archimedes.api.generate_schemas import GenerateBrief
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.auth_helpers import auth_cookies


def test_max_papers_bounds_match_strategy_fusion_enforced_range() -> None:
    """The drift guard: GenerateBrief's declared ge/le must equal the
    pipeline's actually-enforced [MIN_PAPERS, FUSION_MAX_PAPERS]. Bumping
    FUSION_MAX_PAPERS (or MIN_PAPERS) in strategy_fusion.py without updating
    generate_schemas.py's mirrored constants fails this test."""
    field = GenerateBrief.model_fields["max_papers"]
    bounds = {type(m).__name__: m for m in field.metadata}
    assert bounds["Ge"].ge == MIN_PAPERS
    assert bounds["Le"].le == FUSION_MAX_PAPERS


def test_max_papers_default_is_the_one_shared_default_everywhere() -> None:
    """#1636: the two generation routes disagreed (5 vs 4), so the same brief
    retrieved a different amount of evidence depending on which endpoint the
    caller hit. The bypass route was deleted by #1595 mid-flight of this PR,
    so the surviving copies are the schema and FusionBrief — one default,
    pinned across both."""
    from archimedes.agents.strategy_fusion import FusionBrief

    assert GenerateBrief.model_fields["max_papers"].default == DEFAULT_MAX_PAPERS
    assert FusionBrief().max_papers == DEFAULT_MAX_PAPERS
    # And the default is retrieval width ABOVE the fuse target, so the model
    # is never asked to cite every paper it was shown.
    assert MIN_PAPERS < FUSE_TARGET_MIN < DEFAULT_MAX_PAPERS <= FUSION_MAX_PAPERS


def test_max_papers_within_range_is_accepted() -> None:
    for n in range(MIN_PAPERS, FUSION_MAX_PAPERS + 1):
        assert GenerateBrief(intent="x", max_papers=n).max_papers == n


def test_max_papers_over_cap_is_refused_not_silently_downgraded() -> None:
    """Confirmed on pre-#1363 main: max_papers=7 (well under the old le=20)
    was accepted silently and the pipeline clamped it to 6 with zero signal.
    Now it's a hard ValidationError naming the enforced ceiling — which #1636
    moved to 30, so the over-cap probe is 31."""
    with pytest.raises(ValidationError, match="less than or equal to 30"):
        GenerateBrief(intent="x", max_papers=FUSION_MAX_PAPERS + 1)


def test_max_papers_below_floor_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        GenerateBrief(intent="x", max_papers=1)


# ── Route level — mirrors test_generate_payment_gate.py's hermetic harness ──


def _mock_store() -> MagicMock:
    store = MagicMock()
    store.enqueue = AsyncMock(return_value="job-depth")
    return store


def _close_background_coroutine(coro):
    coro.close()
    return MagicMock()


def _harness(store):
    return (
        patch("archimedes.api.generate_routes.get_job_store", return_value=store),
        patch("archimedes.api.generate_routes.asyncio.create_task", side_effect=_close_background_coroutine),
    )


def test_start_route_rejects_over_cap_max_papers_with_422_not_202() -> None:
    from archimedes.main import app

    store = _mock_store()
    p1, p2 = _harness(store)
    with p1, p2:
        resp = TestClient(app).post(
            "/api/generate/start",
            json={"brief": {"intent": "momentum equities", "max_papers": FUSION_MAX_PAPERS + 1}},
            cookies=auth_cookies(),
        )
    assert resp.status_code == 422
    store.enqueue.assert_not_called()


def test_start_route_accepts_max_papers_at_the_new_ceiling() -> None:
    """The other half of the same contract: 30 is now a REAL choice, not a
    value the schema rejects. Without this, raising the ceiling in the picker
    while leaving the schema at 6 would look identical to the test above."""
    from archimedes.main import app

    store = _mock_store()
    p1, p2 = _harness(store)
    with p1, p2:
        resp = TestClient(app).post(
            "/api/generate/start",
            json={"brief": {"intent": "momentum equities", "max_papers": FUSION_MAX_PAPERS}},
            cookies=auth_cookies(),
        )
    assert resp.status_code == 202
    store.enqueue.assert_called_once()
