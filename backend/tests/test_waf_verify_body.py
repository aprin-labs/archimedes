"""The body ceiling on POST /api/rigor/verify is owned by the app (#1749).

History: on the hosted deployment an AWS WAF managed rule
(`SizeRestrictions_BODY`) blocked any body over 8,192 bytes at the edge, and
`POST /api/rigor/verify` serialises one JSON object per bar, so a real returns
series crossed 8 KB at ~163 rows. The edge half of #1749's fix was a scoped WAF
exemption for that one path; it, and the tests that parsed it, went with the
hosted stack on 2026-11-01.

The application half stays and is what these tests pin:
`RigorVerifyRequest` caps `returns` at 2,600 rows (a decade of daily bars),
floors it at the evaluation window, declares both bounds in the OpenAPI
schema, and fails closed with a 422 that names the limit.

Hermetic: exercises the pydantic model plus the router over an in-process ASGI
transport. No network, no DB.

Run:
    /path/to/env/bin/pytest backend/tests/test_waf_verify_body.py -q
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from archimedes.api import account_auth, rigor_verify_routes
from archimedes.api.rigor_verify_routes import _MAX_RETURN_ROWS, _MIN_RETURN_ROWS, RigorVerifyRequest
from archimedes.services.rigor_evaluator import DSR_MIN_BARS
from fastapi import FastAPI
from pydantic import ValidationError

# ── 1. The application owns the ceiling ──────────────────────────────────


def _rows(n: int) -> list[dict]:
    base = datetime(2024, 1, 1)
    return [
        {"date": (base + timedelta(days=i)).date().isoformat(), "daily_return": round(-0.0031 + i * 1e-6, 6)}
        for i in range(n)
    ]


def test_a_decade_of_daily_bars_fits_under_the_cap():
    assert _MAX_RETURN_ROWS == 2600
    assert _MAX_RETURN_ROWS >= 10 * 252, "the cap must admit ten years of daily bars"
    payload = len(json.dumps({"returns": _rows(_MAX_RETURN_ROWS), "trials": 1}, separators=(",", ":")).encode())
    assert 118_000 < payload < 126_000, (
        f"a full-cap payload is {payload} B; the ~122 KB measured claim in the code comment and the PR body is wrong"
    )


def test_schema_accepts_one_trading_year():
    model = RigorVerifyRequest(returns=_rows(252), trials=1)
    assert len(model.returns) == 252


def test_schema_accepts_exactly_the_cap():
    assert len(RigorVerifyRequest(returns=_rows(_MAX_RETURN_ROWS)).returns) == _MAX_RETURN_ROWS


def test_schema_rejects_one_row_over_the_cap_with_a_message_that_names_the_limit():
    with pytest.raises(ValidationError) as excinfo:
        RigorVerifyRequest(returns=_rows(_MAX_RETURN_ROWS + 1))
    message = str(excinfo.value)
    assert str(_MAX_RETURN_ROWS) in message
    assert str(_MAX_RETURN_ROWS + 1) in message, "the error must say how many rows were sent"


def test_length_bounds_are_declared_on_the_field_as_the_backstop():
    """The validator produces the message; min/max_length are the contract in the OpenAPI schema.

    ``minItems`` moved 1 -> ``_MIN_RETURN_ROWS`` with #1803: the endpoint
    refuses a series shorter than the minimum evaluation window (250 daily
    bars, one trading year — the owner's call) instead of answering 200 with a
    verdict nothing evaluable stands behind, and the published schema has to
    say so rather than advertise a length the server rejects.
    """
    schema = RigorVerifyRequest.model_json_schema()
    assert schema["properties"]["returns"]["maxItems"] == _MAX_RETURN_ROWS
    assert schema["properties"]["returns"]["minItems"] == _MIN_RETURN_ROWS
    assert _MIN_RETURN_ROWS == 250, "the window is one trading year"
    assert _MIN_RETURN_ROWS >= DSR_MIN_BARS, (
        "the product window may sit above the gate's own sample floor, never below it"
    )
    # The schema is the contract a generated client sees; the window has to be
    # legible there, not only in the refusal message.
    assert "window" in schema["properties"]["returns"]["description"].lower()


# ── 2. …and returns a 422 over HTTP, not a truncation or a silent accept ──


@pytest.fixture()
def app():
    application = FastAPI()
    application.middleware("http")(account_auth.better_auth_session_middleware)
    application.include_router(rigor_verify_routes.rigor_verify_router)
    return application


def _sign_in(monkeypatch, user_id: str = "user-1"):
    async def fetch(_request):
        return {
            "user": {"id": user_id, "name": user_id, "email": f"{user_id}@example.com", "emailVerified": True},
            "session": {"id": f"s-{user_id}", "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        }

    monkeypatch.setattr(account_auth, "_fetch_session", fetch)


@pytest.mark.asyncio
async def test_over_cap_payload_is_a_422_over_http(app, monkeypatch):
    _sign_in(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"cookie": "better-auth.session_token=opaque", "host": "archimedes-arc.com"},
    ) as client:
        resp = await client.post("/api/rigor/verify", json={"returns": _rows(_MAX_RETURN_ROWS + 1), "trials": 1})
    assert resp.status_code == 422
    assert str(_MAX_RETURN_ROWS) in json.dumps(resp.json())
