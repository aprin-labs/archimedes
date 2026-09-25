"""Tests for the platform-admin-gated private metrics routes (issue #830).

The public metrics endpoints (/api/metrics, /funnel, /visitors) are PII-free and
stay anonymous. The private cost/ops dashboard (/api/metrics/private/*) requires
a signed-in account that is a platform admin (cost/ops data is operationally
sensitive, not just PII, so "any authenticated wallet" was too wide a bar).
Anonymous → 401; signed-in non-admin → 403; admin → 200.

Hermetic: the session cookie is built with the real ``_sign_session`` (the auth
layer's own signer), exercising the production ``_verify_session`` gate rather
than a mock — the same ``_siwe_cookies`` helper pattern as
``test_user_routes.py``. The distinct-user count is mocked at the DB boundary so
no live Postgres is touched, the account store is a per-test tmp SQLite, and
both admin allowlist envs are set via monkeypatch rather than read from a real
``.env``.

**#1648 — the gate is keyed on the ACCOUNT now, and this file changed with it.**
It used to depend on ``require_linked_wallet``, which resolves the caller's
wallet from the ``X-Wallet-Address`` request header; ``conftest.py``'s autouse
``_legacy_siwe_test_adapter`` monkeypatches ``get_linked_wallet_address`` to
derive that wallet straight from the SIWE cookie, so session identity and
"linked wallet" agreed BY CONSTRUCTION here and the header path had no coverage
at all — which is how a bug that made admin visibility follow the browser's
connected wallet lived behind a green suite. Two consequences for this file:
the sessions it signs in as are now backed by REAL seeded ``AuthUser`` +
``LinkedWallet`` rows (``_seed_account``, tmp-sqlite via
``tests/db_isolation.py``), because those rows are what decides admin; and the
two 403 flavours collapsed into one ("Admin access required."), since "has no
linked wallet" is no longer a precondition that can fail on its own.
The header-vs-account behavior itself is pinned in
``backend/tests/test_platform_admin_gate.py``, which drives the real header
against the production resolver.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session
from fastapi.testclient import TestClient

from tests.db_isolation import redirect_to_tmp_sqlite

_ADMIN_WALLET = "0x1111111111111111111111111111111111111111"
_NON_ADMIN_WALLET = "0x2222222222222222222222222222222222222222"
# A THIRD wallet, used only as the SIWE-cookie session identity in the
# linked-wallet-set tests below — distinct from both wallets above so a passing
# test can only be explained by the DB-backed LinkedWallet lookup actually
# running (as opposed to the gate keying off the session cookie's own wallet).
_SESSION_IDENTITY_WALLET = "0x3333333333333333333333333333333333333333"
# A signed-in account with NO linked wallet at all (#1648): used to pin what
# that account now gets, which is the ordinary admin 403 rather than the
# separate "A verified linked wallet is required" 403 the pre-#1648 gate
# raised as a precondition.
_NO_WALLET_SESSION = "0x4444444444444444444444444444444444444444"
# A SECOND allowlisted admin wallet. ``linked_wallets.normalized_identity`` is
# UNIQUE per (chain_id, address), so one address cannot be linked to two
# accounts — the tests below that link an admin wallet to a DIFFERENT account
# than the one the `client` fixture already seeds need their own allowlisted
# address rather than reusing _ADMIN_WALLET.
_SECOND_ADMIN_WALLET = "0x5555555555555555555555555555555555555555"
# ...and the same for a second NON-admin address, for the same uniqueness reason.
_SECOND_NON_ADMIN_WALLET = "0x6666666666666666666666666666666666666666"


def _siwe_cookies(wallet: str) -> dict[str, str]:
    """Build a valid SIWE session cookie for `wallet` (copied from test_user_routes)."""
    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


@pytest.fixture(autouse=True)
def tmp_db(tmp_path):
    """Per-test tmp SQLite — AUTOUSE since #1648.

    The admin gate is now keyed on the account and reads that account's
    linked-wallet set from the database on every request, so every test in
    this file needs a real, isolated account store rather than whatever
    ``DATABASE_URL`` happens to point at. Tests that already declared
    ``tmp_db`` explicitly keep the same single instance.
    """
    yield from redirect_to_tmp_sqlite(tmp_path)


def _seed_account(*, session_wallet: str, linked_address: str | None = None, is_primary: bool = True) -> None:
    """Seed the canonical account (and optionally one linked wallet) behind a
    SIWE test cookie.

    The account id matches what the autouse shim's ``legacy_session``
    (conftest.py) derives from ``session_wallet``'s cookie. Since #1648 the
    admin gate resolves from THIS row set — the account's own linked wallets —
    rather than from the cookie's wallet or a request header, so these rows
    are what makes a session admin or not.
    """
    from archimedes.db import get_session
    from archimedes.models.account import AuthUser, LinkedWallet

    now_dt = datetime.now(UTC)
    user_id = f"legacy-test:{session_wallet}"
    with get_session() as session:
        session.add(
            AuthUser(
                id=user_id,
                name=user_id,
                email=f"{session_wallet[2:10]}@legacy.test",
                email_verified=True,
                created_at=now_dt,
                updated_at=now_dt,
            )
        )
        if linked_address is not None:
            session.add(
                LinkedWallet(
                    user_id=user_id,
                    normalized_identity=f"5042002:{linked_address}",
                    address=linked_address,
                    display_address=linked_address,
                    chain_id=5042002,
                    provider="metamask",
                    is_primary=is_primary,
                    verified_at=now_dt,
                    created_at=now_dt,
                    updated_at=now_dt,
                )
            )
        session.commit()


@pytest.fixture
def client(monkeypatch, tmp_db):
    """Test client over the real app, with the user-count boundary pinned to a known value.

    ``get_distinct_user_count`` is imported by-name into ``metrics_private_routes``, so we
    patch it where it is *used*, not where it is defined — otherwise a sibling test that
    seeds real ``user_profiles`` rows into the shared in-memory DB would leak into the
    assertion (order-dependent flake). ``metrics_routes`` (the public ``/api/metrics``
    endpoint) imports the honest-null variant instead (round 4 fix — see
    ``services/user_stats.py``), so it is patched by ITS name there.

    ``PLATFORM_ADMIN_WALLETS`` is pinned to ``_ADMIN_WALLET`` so admin-gate tests
    are hermetic (independent of whatever a real deploy's env sets), and
    ``PLATFORM_ADMIN_ACCOUNTS`` is cleared so the wallet-evidence path is the
    one under test here (the account-allowlist path has its own coverage in
    ``test_platform_admin_gate.py``).

    Since #1648 the gate reads the ACCOUNT's linked-wallet set, so the two
    accounts every test in this file signs in as are seeded for real: the
    admin session's account genuinely has ``_ADMIN_WALLET`` linked, the
    non-admin session's account genuinely has ``_NON_ADMIN_WALLET`` linked,
    and ``_NO_WALLET_SESSION``'s account has none. Before #1648 the SIWE
    cookie's own wallet WAS the answer (via conftest's shim), so no rows were
    needed — which is precisely how the header-driven bug stayed invisible to
    this file.
    """
    from archimedes.main import app

    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", f"{_ADMIN_WALLET} {_SECOND_ADMIN_WALLET}")
    monkeypatch.delenv("PLATFORM_ADMIN_ACCOUNTS", raising=False)
    _seed_account(session_wallet=_ADMIN_WALLET, linked_address=_ADMIN_WALLET)
    _seed_account(session_wallet=_NON_ADMIN_WALLET, linked_address=_NON_ADMIN_WALLET)
    _seed_account(session_wallet=_NO_WALLET_SESSION)
    with (
        patch("archimedes.api.metrics_private_routes.get_distinct_user_count", return_value=2),
        patch("archimedes.api.metrics_routes.get_distinct_user_count_or_none", return_value=2),
    ):
        yield TestClient(app)


def test_private_cost_401_without_session(client):
    """Anonymous GET /api/metrics/private/cost → 401 (no SIWE session)."""
    res = client.get("/api/metrics/private/cost")
    assert res.status_code == 401


def test_private_cost_401_with_bad_cookie(client):
    """A forged/garbage session cookie must not authenticate → 401."""
    res = client.get("/api/metrics/private/cost", cookies={_COOKIE_NAME: "not-a-valid-token"})
    assert res.status_code == 401


def test_private_cost_403_for_non_admin_wallet(client):
    """A verified SIWE session for a wallet NOT in PLATFORM_ADMIN_WALLETS → 403.

    This is the core of the Insights-page lockdown: before this fix, ANY
    authenticated wallet could read Bedrock/infra/cost-per-user data. Now only
    admin wallets can.
    """
    res = client.get("/api/metrics/private/cost", cookies=_siwe_cookies(_NON_ADMIN_WALLET))
    assert res.status_code == 403


def test_private_cost_200_with_valid_admin_siwe_session(client):
    """Valid SIWE session for an admin wallet → 200, with the honest real-users denominator present."""
    res = client.get("/api/metrics/private/cost", cookies=_siwe_cookies(_ADMIN_WALLET))
    assert res.status_code == 200
    body = res.json()
    # The honest distinct-user count is present (denominator for any per-user $).
    assert body["real_users"] == 2
    assert body["authenticated_wallet"] == _ADMIN_WALLET.lower()
    # The AWS-billing fields are still draft placeholders; `source` describes
    # THEM, not the measured generation cost (#1217 narrowed its scope).
    assert body["source"] == "draft"


def test_private_cost_admin_wallets_parsed_case_insensitively(client, monkeypatch):
    """Admin match is case-insensitive, mirroring wallet_can_publish's parsing."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN_WALLET.upper())
    res = client.get("/api/metrics/private/cost", cookies=_siwe_cookies(_ADMIN_WALLET))
    assert res.status_code == 200


# ── The measured $/generation figure (#1217) ─────────────────────────────
# `cost_per_generation_usd` was a hard-coded `None`. It is now the mean of the
# `generation_costs` measurements priced against the env rate card. What these
# tests pin is the pair of properties that make that safe: it is null (never 0)
# when nothing can be priced, and it never leaves the admin gate.


def test_private_cost_generation_cost_is_null_without_a_rate_card(client, monkeypatch):
    """No rate card configured → null, and a stated reason. Never $0.00.

    This is the default state of every environment that has not been handed a
    card, so it is the shape the dashboard renders most of the time — and the
    one where a plausible-looking zero would do the most damage.
    """
    monkeypatch.delenv("GENERATION_COST_RATE_CARD", raising=False)
    res = client.get("/api/metrics/private/cost", cookies=_siwe_cookies(_ADMIN_WALLET))
    assert res.status_code == 200
    body = res.json()
    assert body["cost_per_generation_usd"] is None
    assert body["generation_cost"]["rate_card_configured"] is False
    assert body["generation_cost"]["schema"] == "cost_rollup_v1"


def test_private_cost_serves_the_measured_figure_when_priceable(client, monkeypatch):
    """A priced rollup reaches the flat field the dashboard already reads."""
    rollup = {
        "schema": "cost_rollup_v1",
        "rate_card_configured": True,
        "lane": "fargate_inline",
        "jobs_priced": 2,
        "cost_per_generation_usd": {"mean": "2.20000000", "median": "2.20000000"},
        "by_n_candidates": [],
        "unavailable": False,
    }
    with patch(
        "archimedes.api.metrics_private_routes.get_measured_generation_cost",
        return_value=rollup,
    ):
        res = client.get("/api/metrics/private/cost", cookies=_siwe_cookies(_ADMIN_WALLET))
    assert res.status_code == 200
    body = res.json()
    assert body["cost_per_generation_usd"] == "2.20000000"
    assert body["generation_cost"]["jobs_priced"] == 2


def test_measured_generation_cost_never_reaches_a_public_surface(client, monkeypatch):
    """Cost/ops data is admin-only — the measured $/generation included.

    The adversarial half of the "surfaced, never public" claim: with a rollup
    that WOULD price (a real card, a real number), sweep the public metrics
    endpoints and assert the figure appears on none of them. A prose claim that
    something is admin-only is worth exactly as much as the check behind it.
    """
    rollup = {
        "schema": "cost_rollup_v1",
        "rate_card_configured": True,
        "lane": "fargate_inline",
        "jobs_priced": 1,
        "cost_per_generation_usd": {"mean": "2.20000000"},
        "by_n_candidates": [],
        "unavailable": False,
    }
    with patch(
        "archimedes.api.metrics_private_routes.get_measured_generation_cost",
        return_value=rollup,
    ):
        # Present behind the gate...
        admin = client.get("/api/metrics/private/cost", cookies=_siwe_cookies(_ADMIN_WALLET))
        assert admin.status_code == 200
        assert "2.20000000" in admin.text

        # ...and absent from every public metrics surface.
        for path in ("/api/metrics", "/api/metrics/funnel", "/api/metrics/visitors"):
            public = client.get(path)
            assert public.status_code in (200, 404), f"{path} → {public.status_code}"
            assert "2.20000000" not in public.text, f"measured $/generation leaked onto {path}"
            assert "cost_per_generation_usd" not in public.text, f"priced key leaked onto {path}"

        # ...and unreadable by a non-admin wallet.
        assert client.get("/api/metrics/private/cost", cookies=_siwe_cookies(_NON_ADMIN_WALLET)).status_code == 403
        assert client.get("/api/metrics/private/cost").status_code == 401


# ── /whoami — the admin-gate probe the frontend calls on entry to Insights
# (owner directive 2026-08-20: /app/insights is now admin-only; supersedes
# #1028 D8). Six-case pattern mirrors /cost above. ─────────────────────────


def test_whoami_401_without_session(client):
    """Anonymous GET /api/metrics/private/whoami → 401 (no SIWE session)."""
    res = client.get("/api/metrics/private/whoami")
    assert res.status_code == 401


def test_whoami_401_with_bad_cookie(client):
    """A forged/garbage session cookie must not authenticate → 401."""
    res = client.get("/api/metrics/private/whoami", cookies={_COOKIE_NAME: "not-a-valid-token"})
    assert res.status_code == 401


def test_whoami_403_for_non_admin_wallet(client):
    """A verified SIWE session for a wallet NOT in PLATFORM_ADMIN_WALLETS → 403.

    This is the exact case the frontend gate depends on: a signed-in,
    non-admin visitor probing /app/insights must get a 4xx here so the page
    falls through to the unknown-page treatment instead of rendering.
    """
    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_NON_ADMIN_WALLET))
    assert res.status_code == 403


def test_whoami_403_for_signed_in_account_with_no_linked_wallet(client):
    """A valid session whose account has NO linked wallet → 403, with the SAME
    "Admin access required." detail as any other non-admin.

    Changed by #1648, deliberately. The pre-#1648 gate raised a distinct
    403 ("A verified linked wallet is required") from ``require_linked_wallet``
    as a *precondition* — having a wallet at all had to succeed before admin
    membership was even consulted. With the account as the key that
    precondition no longer exists: an account listed in
    ``PLATFORM_ADMIN_ACCOUNTS`` is an admin with no wallet at all
    (``test_platform_admin_gate.py``), so "no linked wallet" is not by itself
    a reason to deny — not being an admin is. Collapsing to one message also
    discloses less about why a request was refused.
    """
    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_NO_WALLET_SESSION))
    assert res.status_code == 403
    assert res.json()["detail"] == "Admin access required."


def test_whoami_200_with_valid_admin_siwe_session(client):
    """Valid SIWE session for an admin wallet → 200 {admin: true, wallet}."""
    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_ADMIN_WALLET))
    assert res.status_code == 200
    body = res.json()
    assert body["admin"] is True
    assert body["wallet"] == _ADMIN_WALLET.lower()


def test_whoami_admin_wallets_parsed_case_insensitively(client, monkeypatch):
    """Admin match is case-insensitive, mirroring wallet_can_publish's parsing."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN_WALLET.upper())
    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_ADMIN_WALLET))
    assert res.status_code == 200
    assert res.json()["admin"] is True


# ── The account's linked-wallet set decides, not the session's own wallet ──
# Introduced as the round-4 "real resolver" tests and kept, re-framed, after
# #1648 moved the key onto the account: the SIWE cookie only proves "there is
# a session for legacy-test:<_SESSION_IDENTITY_WALLET>", and that wallet is on
# no allowlist. Admin/non-admin is decided ENTIRELY by which addresses that
# ACCOUNT's LinkedWallet rows carry. (The `get_linked_wallet_address`
# monkeypatch these tests used to install is gone: since #1648 the admin gate
# does not call that helper at all, so restoring it here would have asserted
# nothing. The header-vs-account coverage it stood in for now lives in
# backend/tests/test_platform_admin_gate.py, which drives the real header.)


def test_whoami_200_when_the_accounts_linked_wallet_is_an_admin_wallet(client):
    """Session identity is _SESSION_IDENTITY_WALLET (not on the admin
    allowlist); the account's LINKED wallet is _ADMIN_WALLET → 200.

    Mutation-verified: making `resolve_platform_admin` key off the session
    wallet instead of the account's linked set fails this with `403 != 200`,
    since the session wallet is not on the allowlist.
    """
    _seed_account(session_wallet=_SESSION_IDENTITY_WALLET, linked_address=_SECOND_ADMIN_WALLET)

    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_IDENTITY_WALLET))
    assert res.status_code == 200
    body = res.json()
    assert body["admin"] is True
    # The reported wallet is the LINKED admin wallet, never the session
    # cookie's own (non-admin) wallet — proof the DB lookup, not the cookie,
    # produced this answer.
    assert body["wallet"] == _SECOND_ADMIN_WALLET


def test_whoami_403_when_the_accounts_linked_wallet_is_not_an_admin_wallet(client):
    """Same wiring, but the account's linked wallet is NOT on the allowlist →
    403, proving the row lookup really ran and really found a non-admin
    (as opposed to erroring out and denying for an unrelated reason)."""
    _seed_account(session_wallet=_SESSION_IDENTITY_WALLET, linked_address=_SECOND_NON_ADMIN_WALLET)

    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_IDENTITY_WALLET))
    assert res.status_code == 403
    assert res.json()["detail"] == "Admin access required."


def test_whoami_shape_has_no_extra_fields_and_never_reports_admin_false(client):
    """Shape guard: the response is exactly {admin, wallet} on success — no
    extra fields that could leak ops data through the probe endpoint, and
    'admin' is never anything but True (a non-admin never reaches a 200 at
    all — see the 403 case above; this negative control proves the guard by
    checking there is no code path that could emit {"admin": false})."""
    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_ADMIN_WALLET))
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"admin", "wallet"}
    assert body["admin"] is True


# ── /engagement — dashboard v2 engagement/adoption tiles (admin-only) ──────


def test_engagement_401_without_session(client):
    res = client.get("/api/metrics/private/engagement")
    assert res.status_code == 401


def test_engagement_401_with_bad_cookie(client):
    """A forged/garbage session cookie must not authenticate → 401."""
    res = client.get("/api/metrics/private/engagement", cookies={_COOKIE_NAME: "not-a-valid-token"})
    assert res.status_code == 401


def test_engagement_403_for_non_admin_wallet(client):
    res = client.get("/api/metrics/private/engagement", cookies=_siwe_cookies(_NON_ADMIN_WALLET))
    assert res.status_code == 403


def test_engagement_403_for_signed_in_account_with_no_linked_wallet(client):
    """Same case as test_whoami_403_for_signed_in_account_with_no_linked_wallet
    above, on the second endpoint — the gate is a router-level dependency, so
    both routes must answer identically."""
    res = client.get("/api/metrics/private/engagement", cookies=_siwe_cookies(_NO_WALLET_SESSION))
    assert res.status_code == 403
    assert res.json()["detail"] == "Admin access required."


def test_engagement_admin_wallets_parsed_case_insensitively(client, monkeypatch):
    """Admin match is case-insensitive, mirroring wallet_can_publish's parsing."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN_WALLET.upper())
    with patch(
        "archimedes.api.metrics_private_routes.get_engagement_snapshot",
        return_value={"accounts": {"total": 0, "new_7d": 0, "new_30d": 0}},
    ):
        res = client.get("/api/metrics/private/engagement", cookies=_siwe_cookies(_ADMIN_WALLET))
    assert res.status_code == 200


def test_engagement_shape_keys_present(client):
    """Shape guard: every documented tile key is present on a 200 response."""
    fake_snapshot = {
        "accounts": {"total": 5, "new_7d": 1, "new_30d": 3},
        "linked_wallets": {"total": 2},
        "strategies": {"total": 10, "new_7d": 4, "daily_new": []},
        "generation_costs": {
            "measured_count": 3,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "total_tokens": 150,
        },
        "paper_deployments": {"active": 1, "stopped": 0},
        "repeat_generation_users": {"generating_users": 2, "repeat_users": 1, "note": "x"},
        "payments": {"dry_run": True, "settled_volume_usd": None, "note": "dry-run"},
        "timestamp": "2026-08-20T00:00:00+00:00",
    }
    with patch("archimedes.api.metrics_private_routes.get_engagement_snapshot", return_value=fake_snapshot):
        res = client.get("/api/metrics/private/engagement", cookies=_siwe_cookies(_ADMIN_WALLET))
    assert res.status_code == 200
    body = res.json()
    for key in (
        "accounts",
        "linked_wallets",
        "strategies",
        "generation_costs",
        "paper_deployments",
        "repeat_generation_users",
        "payments",
        "authenticated_wallet",
        "timestamp",
    ):
        assert key in body, f"missing {key} in {list(body.keys())}"


def test_engagement_200_with_valid_admin_siwe_session(client):
    """Admin session → 200 with the composed engagement snapshot shape.

    Mocks the service-layer boundary (consistent with how this file mocks
    get_distinct_user_count above) — the query-level correctness of each
    tile is covered separately in test_engagement_metrics.py against real
    seeded DB rows.
    """
    fake_snapshot = {
        "accounts": {"total": 5, "new_7d": 1, "new_30d": 3},
        "linked_wallets": {"total": 2},
        "strategies": {"total": 10, "new_7d": 4, "daily_new": []},
        "generation_costs": {
            "measured_count": 3,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "total_tokens": 150,
        },
        "paper_deployments": {"active": 1, "stopped": 0},
        "repeat_generation_users": {"generating_users": 2, "repeat_users": 1, "note": "x"},
        "payments": {"dry_run": True, "settled_volume_usd": None, "note": "dry-run"},
        "timestamp": "2026-08-20T00:00:00+00:00",
    }
    with patch("archimedes.api.metrics_private_routes.get_engagement_snapshot", return_value=fake_snapshot):
        res = client.get("/api/metrics/private/engagement", cookies=_siwe_cookies(_ADMIN_WALLET))
    assert res.status_code == 200
    body = res.json()
    assert body["accounts"]["total"] == 5
    assert body["payments"]["settled_volume_usd"] is None
    assert body["authenticated_wallet"] == _ADMIN_WALLET.lower()


# ── The account's linked-wallet set decides — /engagement's twin ──────────
# Mirrors the /whoami pair above; see that block's note on the #1648 reframe.


def test_engagement_200_when_the_accounts_linked_wallet_is_an_admin_wallet(client):
    """Mirrors test_whoami_200_when_the_accounts_linked_wallet_is_an_admin_wallet
    on the /engagement route.

    Mutation-verified: making `resolve_platform_admin` key off the session
    wallet rather than the account's linked set fails this with `403 != 200`.
    """
    _seed_account(session_wallet=_SESSION_IDENTITY_WALLET, linked_address=_SECOND_ADMIN_WALLET)

    fake_snapshot = {
        "accounts": {"total": 1, "new_7d": 0, "new_30d": 1},
        "linked_wallets": {"total": 1},
        "strategies": {"total": 0, "new_7d": 0, "daily_new": []},
        "generation_costs": {"measured_count": 0, "total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0},
        "paper_deployments": {"active": 0, "stopped": 0},
        "repeat_generation_users": {"generating_users": 0, "repeat_users": 0, "note": "x"},
        "payments": {"dry_run": True, "settled_volume_usd": None, "note": "dry-run"},
        "timestamp": "2026-08-20T00:00:00+00:00",
    }
    with patch("archimedes.api.metrics_private_routes.get_engagement_snapshot", return_value=fake_snapshot):
        res = client.get("/api/metrics/private/engagement", cookies=_siwe_cookies(_SESSION_IDENTITY_WALLET))
    assert res.status_code == 200
    assert res.json()["authenticated_wallet"] == _SECOND_ADMIN_WALLET


def test_engagement_403_when_the_accounts_linked_wallet_is_not_an_admin_wallet(client):
    """Mirrors test_whoami_403_when_the_accounts_linked_wallet_is_not_an_admin_wallet
    on the /engagement route: a real, DB-resolved linked wallet that simply
    isn't on the admin allowlist -> the admin-membership 403.
    """
    _seed_account(session_wallet=_SESSION_IDENTITY_WALLET, linked_address=_SECOND_NON_ADMIN_WALLET)

    res = client.get("/api/metrics/private/engagement", cookies=_siwe_cookies(_SESSION_IDENTITY_WALLET))
    assert res.status_code == 403
    assert res.json()["detail"] == "Admin access required."


def test_public_metrics_stays_public_and_pii_free(client):
    """The public /api/metrics endpoint is anonymous + PII-free (diagnosis #5)."""
    res = client.get("/api/metrics")
    assert res.status_code == 200
    body = res.json()
    # Request tallies + the honest real-users count; no PII.
    assert "human_count" in body
    assert "agent_count" in body
    assert body["real_users"] == 2
    assert "email" not in body
    assert "display_name" not in body
