#!/usr/bin/env python3
"""Drive Archimedes account journey over HTTP without browser.

Better Auth account session owns generation and application data. Optional EIP-4361
wallet link is created only for ``--deploy``; wallet connection never authenticates.
After generation the winner is deployed to FREE paper trading by default
(``--no-paper`` to skip) — account session only, no wallet, no rigor precondition.
Credentials and private keys come from environment only and are never printed.

Usage:
    ARCHIMEDES_EMAIL=agent@example.test ARCHIMEDES_PASSWORD='...' \
      python scripts/agent_journey.py --base https://archimedes-arc.com
    python scripts/agent_journey.py --ephemeral
    python scripts/agent_journey.py --no-auth --read-only
    AGENT_WALLET_KEY=0x... ARCHIMEDES_EMAIL=... ARCHIMEDES_PASSWORD=... \
      python scripts/agent_journey.py --deploy

Exit code is nonzero when a required step fails.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
import uuid
from datetime import UTC, datetime

import httpx

AGENT_UA = "archimedes-agent-journey/0.2 (+https://github.com/aprin-labs/archimedes/issues/788)"
_TERMINAL_EVENTS = {"done", "error"}
ARC_CHAIN_ID = 5042002
_EMAIL_ENV = "ARCHIMEDES_EMAIL"
_PASSWORD_ENV = "ARCHIMEDES_PASSWORD"
_KEY_ENV = "AGENT_WALLET_KEY"


def step_auth(client: httpx.Client, _base: str, *, ephemeral: bool) -> str | None:
    """Create disposable account or sign in existing account. Return email."""
    _hr("AUTH — Better Auth account")

    if ephemeral:
        email = f"agent-{uuid.uuid4().hex}@example.test"
        password = secrets.token_urlsafe(24)
        try:
            signup = client.post(
                "/api/auth/sign-up/email",
                json={"name": "Ephemeral agent", "email": email, "password": password},
            )
        except httpx.HTTPError as exc:
            print(f"  ✗ sign-up transport error: {exc}")
            return None
        if not signup.is_success:
            print(f"  ✗ sign-up failed: HTTP {signup.status_code}")
            return None
        print(f"  · disposable account: {email}")
    else:
        email = os.environ.get(_EMAIL_ENV, "").strip()
        password = os.environ.get(_PASSWORD_ENV, "")
        if not email or not password:
            print("  ✗ account email and password are required")
            return None

    try:
        signin = client.post(
            "/api/auth/sign-in/email",
            json={"email": email, "password": password},
        )
    except httpx.HTTPError as exc:
        print(f"  ✗ sign-in transport error: {exc}")
        return None
    if not signin.is_success:
        print(f"  ✗ sign-in failed: HTTP {signin.status_code}")
        return None

    session = _get(client, "/api/auth/get-session")
    user = session.get("user") if isinstance(session, dict) else None
    if not isinstance(user, dict) or not user.get("id"):
        print("  ✗ account session cookie did not round-trip")
        return None
    print(f"  ✓ authenticated as {email}")
    return email


def step_wallet_link(client: httpx.Client, *, ephemeral: bool) -> str | None:
    """Link programmatic EOA to current account for wallet/on-chain actions."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    _hr("WALLET — optional EIP-4361 link")
    if ephemeral:
        account = Account.create()
        print("  · disposable wallet (key held in memory only)")
    else:
        key = os.environ.get(_KEY_ENV, "").strip()
        if not key:
            print(f"  ✗ {_KEY_ENV} required for wallet operation")
            return None
        try:
            account = Account.from_key(key)
        except (TypeError, ValueError):
            print(f"  ✗ {_KEY_ENV} is not a valid private key")
            return None

    try:
        challenge = client.post(
            "/api/wallets/challenge",
            # "headless", not "browser": this client holds a raw key and has no
            # MetaMask, no injected provider, and no Circle passkey (#1293).
            json={"address": account.address, "chain_id": ARC_CHAIN_ID, "provider": "headless"},
        )
    except httpx.HTTPError as exc:
        print(f"  ✗ wallet challenge transport error: {exc}")
        return None
    if not challenge.is_success:
        print(f"  ✗ wallet challenge failed: HTTP {challenge.status_code}")
        return None
    data = challenge.json()
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, str):
        print("  ✗ wallet challenge returned no message")
        return None

    signature = account.sign_message(encode_defunct(text=message)).signature.hex()
    try:
        verified = client.post(
            "/api/wallets/verify",
            json={"message": message, "signature": signature},
        )
    except httpx.HTTPError as exc:
        print(f"  ✗ wallet verification transport error: {exc}")
        return None
    if not verified.is_success:
        print(f"  ✗ wallet verification failed: HTTP {verified.status_code}")
        return None
    print(f"  ✓ linked wallet {account.address}")
    return account.address


def _hr(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * (60 - len(title))}")


def _get(client: httpx.Client, path: str, *, required: bool = False) -> object | None:
    """GET a JSON path; print a one-line result. Returns parsed JSON or None."""
    try:
        r = client.get(path)
    except httpx.HTTPError as exc:
        print(f"  ✗ GET {path} — transport error: {exc}")
        if required:
            raise SystemExit(2) from exc
        return None
    if r.status_code != 200:
        print(f"  ✗ GET {path} — HTTP {r.status_code}")
        if required:
            raise SystemExit(2)
        return None
    try:
        return r.json()
    except ValueError:
        # A 200 with a non-JSON body (e.g. an nginx/proxy HTML page) is a hard
        # failure for a required surface like /health — don't let it slip through
        # as a soft None and continue the journey on misleading output. (Copilot #790)
        print(f"  ✗ GET {path} — 200 but non-JSON body")
        if required:
            raise SystemExit(2) from None
        return None


def step_read(client: httpx.Client) -> None:
    """Read-only surfaces: health, traction, funnel, the strategy library."""
    _hr("READ — public surfaces (no auth)")

    health = _get(client, "/health", required=True)
    if isinstance(health, dict):
        print(f"  ✓ /health — status={health.get('status')!r}")

    metrics = _get(client, "/api/metrics")
    if isinstance(metrics, dict):
        print(f"  ✓ /api/metrics — humans={metrics.get('human_count')} agents={metrics.get('agent_count')}")

    funnel = _get(client, "/api/metrics/funnel")
    if isinstance(funnel, dict):
        stages = funnel.get("stages", [])
        summary = ", ".join(f"{s['stage']}={s['distinct_visitors']}" for s in stages)
        print(f"  ✓ /api/metrics/funnel [{funnel.get('window')}] — {summary}")
    else:
        print("  · /api/metrics/funnel — unavailable; skipping")

    strategies = _get(client, "/api/strategies/")
    if isinstance(strategies, list):
        print(f"  ✓ /api/strategies/ — {len(strategies)} strategies in the library")
    elif isinstance(strategies, dict):
        items = strategies.get("strategies") or strategies.get("items") or []
        print(f"  ✓ /api/strategies/ — {len(items)} strategies in the library")


def step_generate(client: httpx.Client, intent: str, risk: str, timeout_s: float) -> str | None:
    """Start a generation, tail the SSE stream, return the job_id (or None on failure).

    Timeout tradeoff (reference harness): the SSE read timeout is disabled (``read=None``)
    so a slow generation that writes infrequently isn't killed mid-stream; the ``deadline``
    is enforced per-line inside the loop. The known gap: if the backend accepts the
    connection but never writes a *single* byte, ``iter_lines()`` blocks and the per-line
    deadline check never runs, so the harness can hang until the process is killed. Acceptable
    for a dogfooding harness; a production client should add a wall-clock watchdog / cancel.
    """
    _hr("GENERATE — start + stream (session cookie if authenticated)")

    body = {
        "brief": {"intent": intent, "risk_appetite": risk, "max_papers": 5},
        "n_candidates": 1,
    }
    try:
        r = client.post("/api/generate/start", json=body)
    except httpx.HTTPError as exc:
        print(f"  ✗ POST /api/generate/start — transport error: {exc}")
        return None
    if r.status_code not in (200, 202):
        print(f"  ✗ POST /api/generate/start — HTTP {r.status_code}: {r.text[:200]}")
        return None

    # Don't assume a well-formed JSON body with the expected keys — a non-JSON
    # body or an error shape behind a 200/202 should produce a clean failure +
    # message, not a traceback (the whole point of the harness). (Copilot #790)
    try:
        start = r.json()
        job_id = start["job_id"]
        stream_url = start["stream_url"]
    except (ValueError, KeyError, TypeError) as exc:
        print(f"  ✗ POST /api/generate/start — unexpected response shape ({exc}): {r.text[:200]}")
        return None
    print(f"  ✓ job_id={job_id}  stream={stream_url}")

    _hr("STREAM — live reasoning events")
    deadline = time.monotonic() + timeout_s
    seen = 0
    terminal: str | None = None
    # Disable the httpx READ timeout for the SSE stream: the backend only writes
    # when a new event arrives, so a long quiet gap (a slow LLM/tool call) would
    # trip a ReadTimeout even though the overall journey isn't over. We bound the
    # stream with the explicit `deadline` check inside the loop instead. Keep a
    # SHORT, finite connect/write/pool timeout so a dead endpoint still fails fast
    # (read=None alone would otherwise inherit the full generation timeout on the
    # connect phase too — the opposite of fail-fast). (Copilot, #790)
    stream_timeout = httpx.Timeout(read=None, connect=10.0, write=10.0, pool=10.0)
    try:
        with client.stream("GET", stream_url, timeout=stream_timeout) as resp:
            if resp.status_code != 200:
                print(f"  ✗ stream — HTTP {resp.status_code}")
                return job_id
            event_name = None
            for line in resp.iter_lines():
                if time.monotonic() > deadline:
                    print("  · stream timeout reached; moving on")
                    break
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    seen += 1
                    payload = line.split(":", 1)[1].strip()
                    label = event_name or "?"
                    snippet = payload[:110] + ("…" if len(payload) > 110 else "")
                    print(f"  • {label:<20} {snippet}")
                    if event_name in _TERMINAL_EVENTS:
                        terminal = event_name
                        break
    except httpx.HTTPError as exc:
        print(f"  ✗ stream — transport error: {exc}")
        return job_id

    print(f"  → {seen} events; terminal={terminal!r}")
    return job_id


def step_readback(client: httpx.Client, job_id: str) -> dict:
    """Read the externalized rigor verdict + considered alternatives for the job.

    Returns ``{"read_ok": bool, "winner": {...} | None}``. ``winner`` (when the
    selected candidate resolved to a persisted strategy) carries the fields
    ``step_deploy`` needs: ``strategy_id``, ``strategy_name``,
    ``rigor_gate_status``, ``deployable``.
    """
    _hr("RIGOR — externalized verdict + considered alternatives")
    data = _get(client, f"/api/generate/jobs/{job_id}/candidates")
    if not isinstance(data, dict):
        print("  ✗ could not read candidates")
        return {"read_ok": False, "winner": None}
    candidates = data.get("candidates", [])
    best_id = data.get("best_candidate_id")
    if not candidates:
        print("  · no candidates yet (generation may still be running)")
        return {"read_ok": False, "winner": None}
    for c in candidates:
        marker = "🏆 WINNER" if c.get("selected") else "  rejected"
        passes = "PASS" if c.get("passes_rigor") else "FAIL"
        verdict = c.get("rigor_verdict") or {}
        keys = ", ".join(sorted(verdict.keys())[:6]) if isinstance(verdict, dict) else ""
        print(f"  {marker}  {c.get('strategy_name')!r}  rigor={passes}  [{keys}]")
    print(f"  → best_candidate_id={best_id!r}")

    # ── LIVE rigor gate — the SAME tri-state verdict a human sees on the passport ──
    # (#833 live_rigor_gate, compute-on-read from real persisted returns). After the
    # #788 returns-persistence fix, a fusion/debate winner with a real backtest reads
    # "pass"/"fail" + deployable, not the misleading "pending".
    _hr("LIVE GATE — deployability (read the persisted strategy passport)")
    saw_non_pending = False
    winner: dict | None = None
    for c in candidates:
        sid = c.get("strategy_id")
        if not sid:
            continue
        strat = _get(client, f"/api/strategies/{sid}")
        if not isinstance(strat, dict):
            print(f"  · /api/strategies/{sid} — unavailable")
            continue
        status = strat.get("rigor_gate_status")
        deployable = bool(strat.get("passes_rigor_gate"))
        if status and status != "pending":
            saw_non_pending = True
        print(
            f"  {'🏆' if c.get('selected') else '·'}  {c.get('strategy_name')!r}  "
            f"live_gate={status!r}  deployable={deployable}  sharpe={strat.get('sharpe_ratio')}"
        )
        if c.get("selected"):
            winner = {
                "strategy_id": sid,
                "strategy_name": c.get("strategy_name"),
                "rigor_gate_status": status,
                "deployable": deployable,
            }
    if not saw_non_pending:
        print(
            "  ⚠ all strategies read live_gate='pending' — generated candidates have no real "
            "persisted returns (the #788/#818 deployability gap)."
        )
    return {"read_ok": True, "winner": winner}


# ── PAPER — the free spine leg (#1268) ─────────────────────────────────────

# The summary fields this harness prints; pinned against the REAL
# deployment_summary() output by backend/tests/scripts/test_agent_journey_paper.py
# so a rename on the service side fails that test, not a live readback.
PAPER_SUMMARY_FIELDS = ("deployment_id", "status", "days", "total_return")


def build_paper_deploy_payload(strategy_id: str) -> dict:
    """The exact JSON body ``POST /api/paper/deployments`` reads. Factored out
    of ``step_paper`` so the hermetic test can pin this shape (and
    ``PAPER_SUMMARY_FIELDS``) against the real route/service —
    ``backend/tests/scripts/test_agent_journey_paper.py``."""
    return {"strategy_id": strategy_id}


def step_paper(client: httpx.Client, winner: dict | None) -> str | None:
    """Deploy the winning strategy to PAPER trading and read it back.

    ``POST /api/paper/deployments`` needs only the account session — no
    wallet, no gas, free by design (#1268), and deliberately NO rigor
    precondition on the server: paper trading is where a pending strategy can
    prove itself honestly. So unlike ``step_deploy`` this step does not
    refuse a non-deployable winner; it prints the live-gate status and lets
    the server decide (404 for invisible strategies, 422 for spec-less ones).

    Returns the deployment_id; None when skipped (no persisted winner) or
    failed — ``main`` turns a failure into a nonzero exit the same way
    ``--deploy`` does. The post-create readback is part of the leg: a
    deployment the owner cannot immediately GET back is a failure, not a
    footnote.
    """
    _hr("PAPER — free paper-trading deployment (account session only)")
    if not winner or not winner.get("strategy_id"):
        print("  · no winning candidate with a persisted strategy — skipping paper deploy")
        return None
    sid = winner["strategy_id"]
    print(
        f"  · {winner.get('strategy_name')!r}  live_gate={winner.get('rigor_gate_status')!r} "
        "(paper has no gate precondition)"
    )
    try:
        r = client.post("/api/paper/deployments", json=build_paper_deploy_payload(sid))
    except httpx.HTTPError as exc:
        print(f"  ✗ POST /api/paper/deployments — transport error: {exc}")
        return None
    if r.status_code != 201:
        print(f"  ✗ POST /api/paper/deployments — HTTP {r.status_code}: {r.text[:300]}")
        return None
    try:
        dep = r.json()
        deployment_id = dep["deployment_id"]
    except (ValueError, KeyError, TypeError) as exc:
        print(f"  ✗ POST /api/paper/deployments — unexpected response shape ({exc}): {r.text[:200]}")
        return None
    print(f"  ✓ paper deployment created: {deployment_id}")

    summary = _get(client, f"/api/paper/deployments/{deployment_id}")
    if not isinstance(summary, dict):
        print("  ✗ created but could not read the deployment back — failing the leg")
        return None
    fields = "  ".join(f"{k}={summary.get(k)!r}" for k in PAPER_SUMMARY_FIELDS)
    print(f"  ✓ readback: {fields}")
    return deployment_id


# ── DEPLOY + MONITOR (slice 2 continued) ───────────────────────────────────


def build_vault_create_payload(
    *,
    strategy_id: str,
    name: str,
    symbol: str,
    management_fee_bps: int = 0,
    performance_fee_bps: int = 0,
    agent_assisted: bool = True,
    strictness_level: int = 1,
) -> dict:
    """The exact JSON body ``POST /api/vaults/create`` (``VaultCreateRequest``)
    expects. Factored out of ``step_deploy`` so a hermetic test can pin this
    shape against the REAL ``VaultCreateRequest`` Pydantic model (field names
    + validation ranges) — see
    ``backend/tests/scripts/test_agent_journey_deploy.py``. The test binds the
    model directly; it does not exercise account/wallet auth or server-side rigor gate
    (integration-level concerns covered by the route's own tests in
    ``backend/tests/test_vault_rigor_gate.py``).
    """
    return {
        "name": name,
        "symbol": symbol,
        "management_fee_bps": management_fee_bps,
        "performance_fee_bps": performance_fee_bps,
        "agent_assisted": agent_assisted,
        "strategy_ids": [strategy_id],
        "strictness_level": strictness_level,
    }


def step_deploy(
    client: httpx.Client,
    winner: dict | None,
    *,
    execute: bool,
    vault_name: str,
    vault_symbol: str,
    management_fee_bps: int = 0,
    performance_fee_bps: int = 0,
    strictness_level: int = 1,
) -> str | None:
    """Deploy a vault from the winning generated strategy via ``POST /api/vaults/create``.

    Reuses same account-authenticated client after ``step_wallet_link``. Backend
    signer creates vault, then transfers ``Ownable`` ownership to caller's verified
    linked wallet (see route docstring in
    ``backend/archimedes/api/vaults_routes.py::create_vault``). So the agent
    wallet itself needs no funding for this path — ``scripts/fund_agent_wallet.py``
    is for a DIFFERENT, not-yet-built path (an agent signing ``createVault``
    itself, the way the browser UI's ``CreateVaultModal`` does client-side).

    Refuses outright (never calls the endpoint) when the winning candidate isn't
    LIVE-GATE deployable — the harness must never spend gas on a call guaranteed
    to 422, and must never make it look like a pending/failing strategy can be
    forced through (#788 anti-goal: never weaken or bypass the rigor gate; the
    server enforces this independently via ``_assert_strategies_pass_rigor``
    regardless of what this client does).

    ``execute=False`` (the default wired from ``--deploy`` being absent) prints
    the exact payload and returns without sending anything — mirrors
    ``fund_agent_wallet.py``'s ``--execute`` convention for anything that spends
    real (even if testnet) value. Default OFF on purpose: as of this revision
    the contract suite was T3.2-redeployed 2026-07-09 (#1079), issue #588 (the
    redeploy keystone ``docs/agent-api.md`` gates contract-touching agent work
    on) is still open, and ``backend/tests/chain/test_create_vault_abi_consistency.py``
    itself documents that whether the repo's ABI matches the LIVE deployed
    bytecode is unverified pending #588. No vault — agent or human — had been
    created against the new deployment yet at the time this was written. Flip
    ``--deploy`` on once that's confirmed settled.
    """
    _hr("DEPLOY — create a vault from the generated strategy")

    if not winner:
        print("  · no winning candidate available — skipping deploy")
        return None
    if not winner.get("deployable"):
        print(
            f"  ✗ {winner.get('strategy_name')!r} is not deployable "
            f"(live_gate={winner.get('rigor_gate_status')!r}) — refusing to deploy. "
            "This harness never bypasses the rigor gate."
        )
        return None

    payload = build_vault_create_payload(
        strategy_id=winner["strategy_id"],
        name=vault_name,
        symbol=vault_symbol,
        management_fee_bps=management_fee_bps,
        performance_fee_bps=performance_fee_bps,
        strictness_level=strictness_level,
    )
    print(f"  · POST /api/vaults/create  {payload}")

    if not execute:
        print(
            "  · DRY RUN — nothing sent (default). The backend signer would spend real\n"
            "    testnet gas on this call. Re-run with --deploy to actually create it —\n"
            "    see step_deploy's docstring / docs/agent-api.md for why this defaults off."
        )
        return None

    try:
        r = client.post("/api/vaults/create", json=payload)
    except httpx.HTTPError as exc:
        print(f"  ✗ POST /api/vaults/create — transport error: {exc}")
        return None
    if r.status_code != 200:
        print(f"  ✗ POST /api/vaults/create — HTTP {r.status_code}: {r.text[:300]}")
        return None
    try:
        data = r.json()
        vault_address = data["vault_address"]
    except (ValueError, KeyError, TypeError) as exc:
        print(f"  ✗ POST /api/vaults/create — unexpected response shape ({exc}): {r.text[:200]}")
        return None
    print(f"  ✓ vault deployed at {vault_address}")
    return vault_address


def step_monitor(client: httpx.Client, vault_address: str) -> bool:
    """Read vault health back via ``GET /api/vaults/{address}/health``.

    Public — no auth required (matches the live route). Safe to call for ANY
    address, including one with no deployed contract behind it yet: the
    monitor reads off Redis-backed snapshot/heartbeat state keyed by address
    string, not a chain existence check, so an unknown address returns an
    (empty-ish) health snapshot rather than erroring — verified against prod.
    """
    _hr("MONITOR — vault health")
    health = _get(client, f"/api/vaults/{vault_address}/health")
    if not isinstance(health, dict):
        print("  ✗ could not read vault health")
        return False
    print(
        f"  ✓ agent_alive={health.get('agent_alive')}  "
        f"last_rebalance={health.get('last_rebalance')!r}  "
        f"aum_trend_pct={health.get('aum_trend_pct')}  "
        f"snapshots={health.get('snapshot_count')}"
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Drive the Archimedes journey as an agent (#788).")
    ap.add_argument("--base", default="https://archimedes-arc.com", help="API base URL")
    ap.add_argument("--intent", default="diversified low-volatility strategy for idle USDC")
    ap.add_argument(
        "--risk",
        default="moderate",
        choices=["fixed_income", "conservative", "moderate", "aggressive", "hyper_risky"],
    )
    ap.add_argument("--read-only", action="store_true", help="skip generation (cheap smoke test)")
    ap.add_argument("--timeout", type=float, default=120.0, help="generation stream timeout (s)")
    ap.add_argument(
        "--auth",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"Better Auth sign-in with ${_EMAIL_ENV} + ${_PASSWORD_ENV}. "
        "Default: on when both exist; --no-auth forces anonymous.",
    )
    ap.add_argument(
        "--ephemeral",
        action="store_true",
        help="create disposable account; --deploy also links disposable in-memory wallet; implies --auth",
    )
    ap.add_argument(
        "--deploy",
        action="store_true",
        help="actually call POST /api/vaults/create for the generated winner (backend signer spends real "
        "testnet gas). Default is a DRY RUN that prints the exact payload and sends nothing — see "
        "step_deploy's docstring for why (contract suite T3.2-redeployed 2026-07-09, #588 still open, "
        "no vault yet created against it by anyone as of this revision).",
    )
    ap.add_argument(
        "--vault-name", default=None, help="vault name for --deploy (default: derived, 'Agent Journey <UTC timestamp>')"
    )
    ap.add_argument("--vault-symbol", default="AGTJRN", help="vault symbol for --deploy (<=16 chars, default AGTJRN)")
    ap.add_argument("--management-fee-bps", type=int, default=0, help="vault management fee, basis points (default 0)")
    ap.add_argument(
        "--performance-fee-bps", type=int, default=0, help="vault performance fee, basis points (default 0)"
    )
    ap.add_argument(
        "--strictness-level",
        type=int,
        default=1,
        choices=[1, 2, 3, 4, 5],
        help="rigor strictness for deploy (1=strictest/safest, matches the server default; never bypasses "
        "the always-on correctness floors)",
    )
    ap.add_argument(
        "--paper",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="after generation, deploy the winner to FREE paper trading (POST /api/paper/deployments; "
        "account session only — no wallet, no gas, no rigor precondition). --no-paper skips. "
        "Stop later via POST /api/paper/deployments/{id}/stop.",
    )
    ap.add_argument(
        "--monitor-address",
        default=None,
        help="GET /api/vaults/{address}/health for an existing vault, independent of deploying one this run "
        "(no auth needed; safe read-only check)",
    )
    args = ap.parse_args()

    configured_account = bool(os.environ.get(_EMAIL_ENV) and os.environ.get(_PASSWORD_ENV))
    want_auth = args.ephemeral or args.deploy or (args.auth if args.auth is not None else configured_account)

    print(f"Archimedes agent journey → {args.base}")
    client = httpx.Client(base_url=args.base, headers={"User-Agent": AGENT_UA}, timeout=30.0, follow_redirects=True)
    identity: str | None = None
    try:
        if want_auth:
            identity = step_auth(client, args.base, ephemeral=args.ephemeral)
            if identity is None and not args.read_only:
                print("\nRESULT: account auth failed and generation requires it — aborting.")
                return 2
        if args.deploy and identity and step_wallet_link(client, ephemeral=args.ephemeral) is None:
            print("\nRESULT: wallet link failed and deploy requires it — aborting.")
            return 2
        print(f"\n  auth status: {'authenticated as ' + identity if identity else 'anonymous'}")
        step_read(client)
        if args.read_only:
            if args.monitor_address:
                # Health-check-only runs must fail loudly when the GET fails —
                # exit 0 on a failed monitor would defeat the point of using
                # this as a probe (review).
                monitor_ok = step_monitor(client, args.monitor_address)
                print("\n(read-only — skipping generation)")
                return 0 if monitor_ok else 1
            print("\n(read-only — skipping generation)")
            return 0
        job_id = step_generate(client, args.intent, args.risk, args.timeout)
        if not job_id:
            print("\nRESULT: generation failed to start — see above.")
            return 1
        readback = step_readback(client, job_id)
        ok = readback["read_ok"]
        print(f"\nRESULT: journey {'completed' if ok else 'partial (no verdict read)'} — job_id={job_id}")

        paper_id = step_paper(client, readback.get("winner")) if args.paper else None
        if paper_id and args.ephemeral:
            # A disposable account is unreachable after this run, so its paper
            # deployment would accumulate as an orphan on --base forever. Stop
            # it (freezes the ledger honestly); real-account runs deliberately
            # leave the deployment ACTIVE — the running ledger is the point.
            try:
                stop = client.post(f"/api/paper/deployments/{paper_id}/stop")
                print(
                    f"  · ephemeral run: paper deployment {'stopped' if stop.is_success else f'stop failed (HTTP {stop.status_code})'}"
                )
            except httpx.HTTPError as exc:
                print(f"  · ephemeral run: paper deployment stop failed ({exc})")

        vault_address = step_deploy(
            client,
            readback.get("winner"),
            execute=args.deploy,
            vault_name=args.vault_name or f"Agent Journey {datetime.now(UTC):%Y-%m-%d %H:%M}",
            vault_symbol=args.vault_symbol,
            management_fee_bps=args.management_fee_bps,
            performance_fee_bps=args.performance_fee_bps,
            strictness_level=args.strictness_level,
        )
        # Requested hard steps must move the exit code (review): a --deploy
        # run that deployed nothing, or a failed monitor read, is not success.
        # Same for the paper leg, but only when it was actually ATTEMPTED:
        # no persisted winner means there was nothing to deploy — the journey
        # RESULT line above already reports how the generation went (note
        # `ok` covers verdict readability, not winner existence, so a
        # winnerless-but-readable run legitimately exits 0 with paper
        # skipped). A failed POST/readback on a real attempt is a failure.
        paper_ok = True
        if args.paper and (readback.get("winner") or {}).get("strategy_id"):
            if paper_id:
                print(
                    f"RESULT: paper deployment {paper_id} created (free; stop via POST /api/paper/deployments/{paper_id}/stop)"
                )
            else:
                paper_ok = False
                print("RESULT: paper deployment failed — see above.")
        deploy_ok = True
        if args.deploy and not vault_address:
            deploy_ok = False
            print("RESULT: --deploy requested but no vault was deployed — see above.")
        monitor_ok = True
        monitor_target = vault_address or args.monitor_address
        if monitor_target:
            monitor_ok = step_monitor(client, monitor_target)
        if vault_address:
            print(f"RESULT: vault deployed — {vault_address}")

        return 0 if (ok and paper_ok and deploy_ok and monitor_ok) else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
