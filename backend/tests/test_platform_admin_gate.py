"""The platform-admin gate is keyed on CANONICAL ACCOUNT IDENTITY (issue #1648).

**The bug this file exists to pin.** Before #1648 the admin gate resolved
"which wallet is the caller" from the request's ``X-Wallet-Address`` header
(``wallet_routes.get_linked_wallet_address`` filters ``LinkedWallet`` to the
exact address the header names), and ``ui/src/api.js``'s ``walletHeaders()``
attaches that header on **every** ``apiGet`` from whatever wallet the browser
extension happens to have selected in *this tab* — a value sourced from
in-memory connection state, not from the account's linked-wallet set. So the
Insights page appeared and disappeared across browsers for one and the same
signed-in owner account: browser A (admin wallet selected) → 200, browser B
(some other wallet selected, or none linked) → the header named a row that
does not exist for this ``user_id``, the lookup returned ``None``, and the
gate 403'd an account that has the admin wallet linked as ``is_primary``.

**What the fix keys on now.** ``services/platform_admin.resolve_platform_admin``
takes the Better Auth ``CurrentUser`` and answers from server-side state only:
the ``PLATFORM_ADMIN_ACCOUNTS`` allowlist (canonical ``auth_users.id`` /
email), and the account's OWN linked-wallet set looked up by ``user_id``
intersected with ``PLATFORM_ADMIN_WALLETS``. The request's
``X-Wallet-Address`` header is not read on this path at all — it can neither
grant admin (the spoofing hole, guarded below) nor take it away (the reported
bug, guarded below).

**Why these tests are not vacuous (CLAUDE.md rule 3).** ``conftest.py``'s
autouse ``_legacy_siwe_test_adapter`` monkeypatches
``wallet_routes.get_linked_wallet_address`` to derive the wallet straight from
the SIWE cookie, which *ignores the header* — so a header-based test written
on top of that shim would pass against the unfixed code and guard nothing.
The autouse ``_real_wallet_resolver`` fixture below restores the PRODUCTION
resolver for every test in this file, so the pre-fix code path really does
read the header. Revert transcript is in the PR body: with
``require_platform_admin`` restored to its pre-#1648 form, the four
account-identity tests fail (403 != 200) and the header-spoof guard fails on
its detail assertion.

Hermetic: tmp-file SQLite via ``tests/db_isolation.redirect_to_tmp_sqlite``
(seeded with real ``AuthUser`` + ``LinkedWallet`` rows, so "the query ran and
found nothing" is distinguishable from "the query never ran"), a real signed
SIWE cookie, and both env allowlists set by monkeypatch. No Postgres, no
Redis, no network.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from archimedes.api import wallet_routes
from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session
from archimedes.api.wallet_routes import get_linked_wallet_address as _production_resolver
from fastapi.testclient import TestClient

from tests.db_isolation import redirect_to_tmp_sqlite

# The wallet on PLATFORM_ADMIN_WALLETS — the owner's real admin wallet.
_ADMIN_WALLET = "0x1111111111111111111111111111111111111111"
# Linked to the same account, but NOT on the allowlist (the "other browser
# has my second wallet selected" case).
_SECOND_LINKED_WALLET = "0x2222222222222222222222222222222222222222"
# Linked to NOBODY. The header value browser B sends when its extension is
# connected to a wallet this account never linked.
_UNLINKED_WALLET = "0x3333333333333333333333333333333333333333"
# Session identity only — deliberately different from every wallet above, so
# a passing test can never be explained by the gate keying off the SIWE
# cookie's own wallet.
_SESSION_WALLET = "0x4444444444444444444444444444444444444444"
# A second, unrelated account's session identity.
_STRANGER_SESSION_WALLET = "0x5555555555555555555555555555555555555555"

_GATED_PATHS = (
    "/api/metrics/private/whoami",
    "/api/metrics/private/engagement",
    "/api/metrics/private/cost",
    "/api/metrics/private/wallets",
    "/api/metrics/private/wallets/connections",
)


def _siwe_cookies(wallet: str) -> dict[str, str]:
    """A real signed SIWE session cookie (test_user_routes.py's helper)."""
    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


def _account_id(session_wallet: str) -> str:
    """The canonical account id conftest's legacy adapter mints for a cookie."""
    return f"legacy-test:{session_wallet}"


def _account_email(session_wallet: str) -> str:
    return f"{session_wallet[2:10]}@legacy.test"


def _seed_account(session_wallet: str, linked: tuple[tuple[str, bool, int], ...] = ()) -> str:
    """Seed one canonical account plus its linked wallets.

    ``linked`` items are ``(address, is_primary, chain_id)``. Returns the
    canonical ``auth_users.id``.
    """
    from archimedes.db import get_session
    from archimedes.models.account import AuthUser, LinkedWallet

    now = datetime.now(UTC)
    user_id = _account_id(session_wallet)
    with get_session() as session:
        session.add(
            AuthUser(
                id=user_id,
                name=user_id,
                email=_account_email(session_wallet),
                email_verified=True,
                created_at=now,
                updated_at=now,
            )
        )
        for index, (address, is_primary, chain_id) in enumerate(linked):
            session.add(
                LinkedWallet(
                    user_id=user_id,
                    normalized_identity=f"{chain_id}:{address}",
                    address=address,
                    display_address=address,
                    chain_id=chain_id,
                    provider="metamask",
                    is_primary=is_primary,
                    verified_at=now,
                    created_at=now + timedelta(seconds=index),
                    updated_at=now,
                )
            )
        session.commit()
    return user_id


@pytest.fixture(autouse=True)
def tmp_db(tmp_path):
    """Fresh per-test SQLite; real rows, real queries, no Postgres."""
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture(autouse=True)
def _real_wallet_resolver(monkeypatch):
    """Undo conftest's SIWE shim so the PRODUCTION resolver is in play.

    Load-bearing, not hygiene: the shim derives the wallet from the cookie and
    never reads ``X-Wallet-Address``, so every header test below would pass
    against the unfixed code with the shim active — the exact "passes both
    ways, guards nothing" trap CLAUDE.md rule 3 names.
    """
    monkeypatch.setattr(wallet_routes, "get_linked_wallet_address", _production_resolver)


@pytest.fixture
def client(monkeypatch):
    """TestClient with both admin allowlists pinned (never read from a real .env)."""
    from archimedes.main import app

    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN_WALLET)
    monkeypatch.delenv("PLATFORM_ADMIN_ACCOUNTS", raising=False)
    # Boundary mocks for the two gated payload builders, so these gate tests
    # assert on status codes and never on live metric values.
    with (
        patch("archimedes.api.metrics_private_routes.get_distinct_user_count", return_value=2),
        patch(
            "archimedes.api.metrics_private_routes.get_engagement_snapshot",
            return_value={"accounts": {"total": 0, "new_7d": 0, "new_30d": 0}},
        ),
    ):
        yield TestClient(app)


# ── The reported bug: admin visibility followed the browser's wallet ───────


def test_admin_survives_a_header_naming_an_unlinked_wallet(client):
    """THE acceptance case. Account has admin wallet A linked as ``is_primary``
    and A is on ``PLATFORM_ADMIN_WALLETS``; the request carries
    ``X-Wallet-Address: B`` for an unlinked B (browser B's extension). → 200.

    Pre-fix this is a 403: the header pinned the ``LinkedWallet`` query to B,
    found no row for this ``user_id``, returned ``None``, and
    ``require_linked_wallet`` raised before the admin check ever ran.
    """
    _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))

    res = client.get(
        "/api/metrics/private/whoami",
        cookies=_siwe_cookies(_SESSION_WALLET),
        headers={"X-Wallet-Address": _UNLINKED_WALLET, "X-Wallet-Chain-Id": "5042002"},
    )

    assert res.status_code == 200, res.text
    assert res.json() == {"admin": True, "wallet": _ADMIN_WALLET}


def test_admin_survives_a_header_naming_a_linked_but_non_admin_wallet(client):
    """The multi-wallet variant: B *is* linked to this account, just not the
    admin one. Pre-fix the gate resolved B and 403'd on admin membership.

    This is the case the old ``docs/api/admin-private.md`` "self-lockout note"
    told the owner to recover from by switching browser extensions.
    """
    _seed_account(
        _SESSION_WALLET,
        ((_ADMIN_WALLET, True, 5042002), (_SECOND_LINKED_WALLET, False, 5042002)),
    )

    res = client.get(
        "/api/metrics/private/whoami",
        cookies=_siwe_cookies(_SESSION_WALLET),
        headers={"X-Wallet-Address": _SECOND_LINKED_WALLET, "X-Wallet-Chain-Id": "5042002"},
    )

    assert res.status_code == 200, res.text
    assert res.json()["wallet"] == _ADMIN_WALLET


def test_admin_survives_a_malformed_wallet_header(client):
    """A junk header value must be irrelevant, not fatal.

    Pre-fix, ``get_linked_wallet_address`` returned ``None`` for anything not
    matching ``0x[0-9a-f]{40}`` — so a stale/garbage header locked the owner
    out exactly like an unlinked one.
    """
    _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))

    res = client.get(
        "/api/metrics/private/whoami",
        cookies=_siwe_cookies(_SESSION_WALLET),
        headers={"X-Wallet-Address": "not-an-address"},
    )

    assert res.status_code == 200, res.text


def test_admin_survives_a_wallet_header_for_a_different_chain(client):
    """The admin wallet is linked on chain 5042002; the browser is on chain 1.

    Pre-fix the ``chain_id`` came off ``X-Wallet-Chain-Id`` and was ANDed into
    the lookup, so a wallet linked on another chain resolved to ``None``.
    Post-fix the linked-wallet set is evidence of *who the account is*, which
    is not a per-chain fact — the chain a given request targets is a separate
    concern from whether this account is a platform admin.
    """
    _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))

    res = client.get(
        "/api/metrics/private/whoami",
        cookies=_siwe_cookies(_SESSION_WALLET),
        headers={"X-Wallet-Address": _ADMIN_WALLET, "X-Wallet-Chain-Id": "1"},
    )

    assert res.status_code == 200, res.text


@pytest.mark.parametrize("path", _GATED_PATHS)
def test_every_gated_route_survives_the_wrong_wallet_header(client, path):
    """The gate is a router-level dependency — the fix must hold on all of it,
    not just the ``/whoami`` probe the frontend happens to call first."""
    _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))

    res = client.get(
        path,
        cookies=_siwe_cookies(_SESSION_WALLET),
        headers={"X-Wallet-Address": _UNLINKED_WALLET},
    )

    assert res.status_code == 200, f"{path} → {res.status_code} {res.text}"


# ── The anti-goal: the header must never GRANT admin ───────────────────────


def test_header_naming_an_admin_wallet_does_not_grant_admin_to_a_stranger(client):
    """Companion adversarial case from the issue's acceptance criteria.

    An account that has NO admin wallet linked, sending
    ``X-Wallet-Address: <the admin wallet>``, must still get 403. Trading the
    reported bug for "any request carrying an admin address in a header is
    admin" would be a strictly worse hole — the header is attacker-controlled
    and proves nothing.
    """
    _seed_account(_STRANGER_SESSION_WALLET, ((_SECOND_LINKED_WALLET, True, 5042002),))

    res = client.get(
        "/api/metrics/private/whoami",
        cookies=_siwe_cookies(_STRANGER_SESSION_WALLET),
        headers={"X-Wallet-Address": _ADMIN_WALLET, "X-Wallet-Chain-Id": "5042002"},
    )

    assert res.status_code == 403, res.text
    assert res.json()["detail"] == "Admin access required."


def test_header_naming_an_admin_wallet_owned_by_another_account_is_not_admin(client):
    """Sharper form of the same guard: the admin wallet genuinely exists as a
    ``LinkedWallet`` row — it just belongs to a DIFFERENT ``user_id``.

    A lookup that filtered on the address without also filtering on the
    caller's account id would pass the previous test (no such row) and fail
    this one (row exists, wrong owner). That is the mutation this case exists
    to catch.
    """
    _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))
    _seed_account(_STRANGER_SESSION_WALLET, ((_SECOND_LINKED_WALLET, True, 5042002),))

    res = client.get(
        "/api/metrics/private/whoami",
        cookies=_siwe_cookies(_STRANGER_SESSION_WALLET),
        headers={"X-Wallet-Address": _ADMIN_WALLET},
    )

    assert res.status_code == 403, res.text


@pytest.mark.parametrize("path", _GATED_PATHS)
def test_anonymous_still_gets_401_on_every_gated_route(client, path):
    """No session at all is still 401, never 403 — the frontend's fail-closed
    probe and the "page does not exist" treatment both depend on this."""
    assert client.get(path, headers={"X-Wallet-Address": _ADMIN_WALLET}).status_code == 401


def test_non_admin_account_with_a_linked_wallet_is_403(client):
    """A real, DB-resolved linked wallet that simply isn't on the allowlist."""
    _seed_account(_SESSION_WALLET, ((_SECOND_LINKED_WALLET, True, 5042002),))

    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_WALLET))

    assert res.status_code == 403
    assert res.json()["detail"] == "Admin access required."


def test_empty_allowlists_admit_nobody(client, monkeypatch):
    """Both allowlists empty (the default deploy) → no account is admin, even
    one whose wallets are all linked and verified. Guards against a parse that
    turns "" into a permissive wildcard."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", "")
    monkeypatch.setenv("PLATFORM_ADMIN_ACCOUNTS", "")
    _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))

    assert client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_WALLET)).status_code == 403


# ── The canonical key: PLATFORM_ADMIN_ACCOUNTS ─────────────────────────────


def test_account_allowlist_by_user_id_grants_admin_with_no_wallet_at_all(client, monkeypatch):
    """The point of "wallets are evidence, not the key": an account listed by
    canonical id is admin with zero linked wallets and zero wallet env."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", "")
    monkeypatch.setenv("PLATFORM_ADMIN_ACCOUNTS", _account_id(_SESSION_WALLET))
    _seed_account(_SESSION_WALLET)

    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_WALLET))

    assert res.status_code == 200, res.text
    # No admin wallet is linked, so there is no evidence wallet to report —
    # honest null, not a fabricated address.
    assert res.json() == {"admin": True, "wallet": None}


def test_account_allowlist_by_email_is_case_insensitive(client, monkeypatch):
    """Emails are case-insensitive identifiers; ``auth_users.email`` is unique."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", "")
    monkeypatch.setenv("PLATFORM_ADMIN_ACCOUNTS", _account_email(_SESSION_WALLET).upper())
    _seed_account(_SESSION_WALLET)

    assert client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_WALLET)).status_code == 200


def test_account_allowlist_matches_user_ids_case_sensitively(client, monkeypatch):
    """Better Auth ids are opaque, case-SENSITIVE tokens — folding their case
    would let one allowlist entry match two distinct accounts.

    The guard demo for that widening: the same id with its case flipped must
    NOT match.
    """
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", "")
    monkeypatch.setenv("PLATFORM_ADMIN_ACCOUNTS", _account_id(_SESSION_WALLET).upper())
    _seed_account(_SESSION_WALLET)

    assert client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_WALLET)).status_code == 403


def test_admin_wallet_env_still_works_unchanged_for_an_existing_deploy(client):
    """Migration guarantee: a deploy that only ever set ``PLATFORM_ADMIN_WALLETS``
    keeps working with no config change — the same wallet now grants admin to
    the ACCOUNT that has it linked, on every request that account makes."""
    _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))

    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_WALLET))

    assert res.status_code == 200
    assert res.json()["wallet"] == _ADMIN_WALLET


def test_admin_wallet_env_is_matched_case_insensitively(client, monkeypatch):
    """Checksummed (EIP-55, mixed-case) env entries must match the lowercased
    stored address — mirrors ``wallet_can_publish``'s parse."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN_WALLET.upper())
    _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))

    assert client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_WALLET)).status_code == 200


def test_admin_wallet_evidence_is_found_on_a_non_primary_wallet(client):
    """The allowlisted wallet does not have to be ``is_primary`` — the old
    header-absent fallback checked only the primary, so an owner whose primary
    is a different wallet was locked out with no header at all."""
    _seed_account(
        _SESSION_WALLET,
        ((_SECOND_LINKED_WALLET, True, 5042002), (_ADMIN_WALLET, False, 5042002)),
    )

    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_WALLET))

    assert res.status_code == 200, res.text
    assert res.json()["wallet"] == _ADMIN_WALLET


# ── Fail-closed behavior when the account store is unreachable ─────────────


def test_wallet_evidence_fails_closed_when_the_database_is_down(client, monkeypatch):
    """A DB outage must deny, never admit. The wallet-evidence path needs the
    account store; if it cannot be read, the answer is 403."""
    from archimedes.services import platform_admin

    def _boom():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(platform_admin, "get_session", _boom)
    _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))

    assert client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_WALLET)).status_code == 403


def test_account_allowlist_still_admits_when_the_database_is_down(client, monkeypatch):
    """...and the canonical key is the break-glass that survives that outage:
    ``PLATFORM_ADMIN_ACCOUNTS`` is answered from the session + env alone, with
    no account-store read, so the ops dashboard stays reachable during exactly
    the incident an operator needs it for."""
    from archimedes.services import platform_admin

    def _boom():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(platform_admin, "get_session", _boom)
    monkeypatch.setenv("PLATFORM_ADMIN_ACCOUNTS", _account_id(_SESSION_WALLET))

    res = client.get("/api/metrics/private/whoami", cookies=_siwe_cookies(_SESSION_WALLET))

    assert res.status_code == 200, res.text
    assert res.json() == {"admin": True, "wallet": None}


# ── The migration aid: PLATFORM_ADMIN_WALLETS → PLATFORM_ADMIN_ACCOUNTS ────


def test_migration_report_maps_admin_wallets_to_their_owning_accounts(monkeypatch):
    """``derive_admin_accounts_from_wallets`` turns the wallet env into the
    account env, so the cutover is a read-only lookup rather than a guess."""
    from archimedes.services.platform_admin import derive_admin_accounts_from_wallets

    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", f"{_ADMIN_WALLET.upper()}, {_UNLINKED_WALLET}")
    user_id = _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))

    report = derive_admin_accounts_from_wallets()

    assert report["account_ids"] == [user_id]
    assert report["env_line"] == f"PLATFORM_ADMIN_ACCOUNTS={user_id}"
    # The wallets that resolve to nothing are the ones that would silently
    # become non-admins on cutover — named, not dropped.
    assert report["unlinked_wallets"] == [_UNLINKED_WALLET]
    assert report["resolved"] == [
        {"wallet": _ADMIN_WALLET, "user_id": user_id, "email": _account_email(_SESSION_WALLET)}
    ]


def test_migration_report_reports_every_account_sharing_one_admin_wallet(monkeypatch):
    """One address can be linked to more than one account. Reporting only the
    first would silently drop an admin on cutover, so all of them are listed."""
    from archimedes.services.platform_admin import derive_admin_accounts_from_wallets

    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN_WALLET)
    first = _seed_account(_SESSION_WALLET, ((_ADMIN_WALLET, True, 5042002),))
    second = _seed_account(_STRANGER_SESSION_WALLET, ((_ADMIN_WALLET, True, 1),))

    report = derive_admin_accounts_from_wallets()

    assert sorted(report["account_ids"]) == sorted([first, second])


def test_migration_report_is_empty_when_no_admin_wallets_are_configured(monkeypatch):
    from archimedes.services.platform_admin import derive_admin_accounts_from_wallets

    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", "")

    report = derive_admin_accounts_from_wallets()

    assert report["account_ids"] == []
    assert report["unlinked_wallets"] == []
    assert report["env_line"] == "PLATFORM_ADMIN_ACCOUNTS="
