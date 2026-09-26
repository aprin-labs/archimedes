"""Agent-discoverability manifest — GET /api/agent/manifest.

A small, unauthenticated, machine-readable summary of the agent-facing API contract
documented in full at ``docs/agent-api.md`` (and curated for low-token consumption at
``/llms.txt``). Intent: give an autonomous AI agent landing on the API surface with no
prior context a single cheap GET that says what this product is, how to authenticate,
and which endpoints matter.

Deliberately a SEPARATE module from ``agent_routes.py`` (``/api/agent/status`` etc.):
that file's "agent" is the autonomous on-chain trading/rebalancing agent (internal
system health). This file's "agent" is an external AI agent consuming the product as a
user. Same URL prefix (``/api/agent``), different concept — kept in separate modules so
neither docstring has to disambiguate the other's meaning inline.

Honesty note (mirrors docs/agent-api.md): READ — including the rigor readback
``GET /api/strategies/{strategy_id}``, which is a PUBLIC read like its siblings — plus
AUTH, WALLETLINK, GENERATE, ACCOUNT, RIGOR, and PAPER (simulated deployments, no chain,
no funds) are live today. DEPLOY, marketplace PUBLISH/SUBSCRIBE, and MONITOR exist as
routes but are **not a public surface**: vault execute/monitor is roadmap (no user vault
has ever been created; the UI journey is gated off every shipped build), and marketplace
is not a product a visitor can use. This manifest makes no claim about marketplace
payment/billing settlement.

The ``erc8004`` block (#1527) is the on-chain identity leg and is deliberately the
weakest claim in this document: the spec-typed registration file is published, and
that is ALL it says today. ``agentId``/``tokenURI`` are null and ``status`` is
``registration_pending`` until an owner-signed ``register()`` lands on Arc — an agent
reading this must not treat Archimedes as an ERC-8004-registered, reputed, or
validated counterparty. That status is not a constant in this file: it is re-derived
per request from a live ``ownerOf()`` read against the registry
(``chain/erc8004_identity.py``), and the sibling ``erc8004_verification`` block names
the reading it came from — including when the registry gave no ownership answer, which
keeps the claim at pending rather than letting an RPC outage read as a settled state.

``auth_required`` is a per-GROUP flag, so a route belongs to the group whose gating it
actually shares — the three ``/api/wallets/*`` routes all sit behind
``require_current_user`` and therefore live in ``walletLink`` (auth_required True), NOT
alongside the anonymous sign-up/sign-in routes in ``auth``. The one deliberate exception
is ``generate.quote``, which is public inside an otherwise-gated group and is called out
inline there.

Every route string here is asserted to resolve against the running app's OpenAPI in
``backend/tests/test_agent_discovery.py`` — a manifest that advertises a 404 is worse
than one that omits the endpoint, so the drift is caught in CI rather than by the agent.
That file also asserts that every route shared with the static
``ui/public/.well-known/agent.json`` card carries the SAME auth flag on both surfaces:
two discovery documents that disagree about whether a call needs a session send the
agent into a retry loop it cannot diagnose.
"""

from __future__ import annotations

import os

from fastapi import APIRouter

from archimedes.api.rigor_verify_routes import _MIN_RETURN_ROWS as _MIN_VERIFY_WINDOW_BARS
from archimedes.api.wallet_routes import WALLET_PROVIDERS

agent_manifest_router = APIRouter(prefix="/api/agent", tags=["agent"])

# Same env var, same default, as wallet_routes._SUPPORTED_CHAIN_ID and
# auth_siwe._EXPECTED_CHAIN_ID — sourced here rather than a separately
# hardcoded literal so the manifest's advertised chain can never drift from
# the value the rest of the backend actually enforces.
_CHAIN_ID = int(os.getenv("ARC_CHAIN_ID", "5042002"))

# ── ERC-8004 identity (#1527) ────────────────────────────────────────────────
#
# Arc names ERC-8004 as its on-chain agent-identity standard (docs/arc-integration.md →
# submodules/context-arc/.../build/agentic-economy.md). This block advertises the leg
# that EXISTS today — the spec-typed registration file and the registry we would
# register against — and asserts nothing beyond it.
#
# HOW "registered" GETS SAID. #1552 shipped this block driven by a module constant
# (``_ERC8004_AGENT_ID = None``), which was the honest value and the wrong mechanism:
# editing that constant to a number would have made every surface claim a registration
# with nothing behind it but a diff. The constants are gone. ``status``, ``agentId`` and
# ``tokenURI`` now come from ``chain.erc8004_identity.verify_identity()``, which returns
# "registered" only after a live ``ownerOf(agentId)`` on the Arc IdentityRegistry returns
# the configured platform wallet. ``ERC8004_AGENT_ID`` is a POINTER at the token to check,
# never the claim itself — set it to the wrong id and the read simply keeps saying pending.
#
# Cost when nothing is configured (the state on main today): zero RPC calls. The verifier
# short-circuits to "unconfigured" before touching the network.

# The URL an ``agentURI`` would point at — the ERC-8004 registration file served from
# ui/public/.well-known/. It is NOT a tokenURI: nothing on-chain references it yet.
_ERC8004_REGISTRATION_URI = "https://archimedes-arc.com/.well-known/agent-registration.json"

_ERC8004_PENDING_NOTE = (
    "Not registered on-chain: no register() transaction has been confirmed for this "
    "deployment's wallet, so agentId and tokenURI are null and NO ERC-8004 identity, "
    "reputation, or validation claim is made. Publishing a spec-typed registration file at "
    "registrationUri is not the same as being registered. This status is re-derived from a "
    "live registry read on every request; the sibling erc8004_verification block says which "
    "reading produced it. Plan the call with scripts/register_erc8004_identity.py --plan."
)


async def erc8004_identity() -> tuple[dict, dict]:
    """The ERC-8004 identity block plus the verification record behind it.

    Returns ``(block, verification)``:

    - ``block`` is the seven-key document every discovery surface publishes — the served
      manifest, ``ui/public/.well-known/agent.json``, and the two registration files. One
      builder so they cannot disagree about whether we are registered, which is the exact
      failure mode #1448 caught between the first two surfaces.
    - ``verification`` is manifest-only, because it is inherently a *reading* and a
      committed JSON file cannot hold one. It records how the claim above was established
      (``onchain`` / ``unconfigured`` / ``unavailable``) so a consumer — or an operator —
      can tell "the chain says no identity" apart from "the RPC was dark", which are the
      same claim and very different facts.
    """
    from archimedes.chain.erc8004_identity import registry_address, verify_identity

    registry = registry_address()
    result = await verify_identity()
    block = {
        "chain": f"eip155:{_CHAIN_ID}",
        "identityRegistry": registry,
        "agentId": result.agent_id,
        "tokenURI": result.token_uri,
        "status": result.status,
        "registrationUri": _ERC8004_REGISTRATION_URI,
        "note": (
            f"Registered as agent {result.agent_id} on {registry}, confirmed by a live "
            "ownerOf() read at request time. Registration is an identity record only — it "
            "asserts nothing about reputation or validation, neither of which this agent claims."
            if result.registered
            else _ERC8004_PENDING_NOTE
        ),
    }
    return block, result.as_dict()


@agent_manifest_router.get("/manifest")
async def get_agent_manifest():
    """Small JSON contract for programmatic discovery by AI agents.

    Unauthenticated, rate-limited like sibling public GETs (no decorator -> the
    limiter's default_limits apply, matching e.g. /api/config/contracts).
    """
    erc8004_block, erc8004_verification = await erc8004_identity()
    return {
        "name": "Archimedes",
        # Byte-identical to .well-known/agent.json's `description` — the two are
        # the same sentence served two ways, and #1650 was filed because an agent
        # reading either one was told strategies execute on-chain today. Pinned by
        # tests/test_agent_manifest_static_consistency.py so the pair cannot drift
        # again, in either direction.
        "blurb": (
            "Turns a natural-language investment intent into a research-grounded, "
            "rigor-gated portfolio strategy on Arc public testnet "
            "(chain ID 5042002). Generation is not anchored on-chain. Simulated "
            "paper deployments are live: paper_daily_returns is the "
            "graded track record the rigor gate sees. paper_agent_trades "
            "(PR #1704, not on main) is not a live path. Neither is on-chain "
            "execution proof. Executing strategies in non-custodial USDC "
            "vaults on Arc is roadmap, not shipped."
        ),
        "docs": {
            "llms_txt": "/llms.txt",
            "agent_api": "https://github.com/aprin-labs/archimedes/blob/main/docs/agent-api.md",
            "quickstart": "https://github.com/aprin-labs/archimedes/blob/main/docs/agent-quickstart.md",
            "agent_card": "/.well-known/agent.json",
        },
        # On-chain identity leg — pending, and says so. See erc8004_identity().
        "erc8004": erc8004_block,
        # HOW the block above was established. Manifest-only: a live reading has no
        # business being frozen into a committed .well-known file. `source: "unavailable"`
        # means the registry produced no ownership answer — the pending status beside it is
        # then a refusal to claim, NOT a finding that we are unregistered.
        "erc8004_verification": erc8004_verification,
        "auth": {
            "scheme": "Better Auth session",
            "methods": ["emailPassword", "google", "github"],
            "description": (
                "Create or sign in to canonical account at /api/auth/* and retain session cookie. "
                "Provider availability is returned by GET /api/auth/providers. Wallet proof is a "
                "separate optional link under /api/wallets/* and never creates a session."
            ),
            "wallet_link_spec": "EIP-4361",
            # Read straight off the request model's Literal so the advertised set can
            # never drift from the set the endpoint actually accepts (#1293). A
            # non-browser client should send "headless": the other three name browser
            # wallet software an API caller does not have.
            "wallet_link_providers": list(WALLET_PROVIDERS),
            "wallet_link_provider_default_for_agents": "headless",
            "chain_id": _CHAIN_ID,
        },
        "endpoints": {
            "read": {
                "status": "live",
                "auth_required": False,
                "routes": {
                    "strategies": "GET /api/strategies/",
                    # The rigor readback. Anonymous-OK like its siblings here: the
                    # route takes no auth dependency, and visibility is enforced
                    # per-row (a private strategy 404s rather than 401s), so an
                    # agent can read a public strategy's verdict before signing in.
                    "strategy": "GET /api/strategies/{strategy_id}",
                    "assets": "GET /api/explore/assets",
                    "health": "GET /api/health",
                },
            },
            "auth": {
                "status": "live",
                "auth_required": False,
                "routes": {
                    "providers": "GET /api/auth/providers",
                    "sign_up": "POST /api/auth/sign-up/email",
                    "sign_in": "POST /api/auth/sign-in/email",
                    "session": "GET /api/auth/get-session",
                    "sign_out": "POST /api/auth/sign-out",
                },
            },
            # Separate from `auth` because the gating is the opposite: linking a
            # wallet PROVES an address for an account that already holds a session,
            # so all three routes sit behind require_current_user and 401 without
            # one. Grouping them under the anonymous `auth` block told an agent it
            # could start here, which it cannot.
            "walletLink": {
                "status": "live",
                "auth_required": True,
                "spec": "EIP-4361",
                "routes": {
                    "challenge": "POST /api/wallets/challenge",
                    "verify": "POST /api/wallets/verify",
                    "list": "GET /api/wallets",
                },
            },
            "generate": {
                "status": "live",
                "auth_required": True,
                "routes": {
                    # Public and unauthenticated, unlike its siblings: an agent can
                    # price the run before holding a session (#1293).
                    "quote": "GET /api/generate/quote",
                    "start": "POST /api/generate/start",
                    "stream": "GET /api/generate/stream/{job_id}",
                    "candidates": "GET /api/generate/jobs/{job_id}/candidates",
                },
            },
            # Backs the CLI's `archimedes meter` (#1305): today's generation usage
            # against both daily caps, plus the live price quote.
            "account": {
                "status": "live",
                "auth_required": True,
                "routes": {
                    "usage": "GET /api/account/usage",
                },
            },
            # Backs the CLI's `archimedes verify` (#1305). Distinct from the rigor
            # READBACK under `read.strategy`: this one runs the gate's admission
            # checks over a bare returns series the caller submits. Only DSR and
            # walk-forward OOS are evaluable from a bare series — PBO and the
            # look-ahead audit report `not_evaluable`, never a silent pass.
            # #1481: `passes` is a quorum over the two RUNNABLE legs, so an agent
            # reading the scalar alone cannot mistake it for the passport gate.
            # #1803: the minimum evaluation window is stated here as well as in
            # ui/public/.well-known/agent.json. An agent that discovers us through
            # the served manifest builds its body from THIS note; leaving the window
            # to the static file only would make the answer depend on which surface
            # it happened to read, which is the #1448 drift all over again.
            "rigor": {
                "status": "live",
                "auth_required": True,
                "verdict_note": (
                    "`passes` requires BOTH runnable legs (DSR, OOS consistency) to have run "
                    "and passed. PBO and look-ahead can never run on a bare returns series, so "
                    "the verdict is capped: it is NOT equivalent to the strategy passport gate. "
                    "The response carries legs_evaluated / legs_runnable / legs_total / "
                    "verdict_capped so the scalar is qualifiable without re-deriving leg statuses. "
                    f"Minimum evaluation window is {_MIN_VERIFY_WINDOW_BARS} daily bars (one "
                    "trading year): under it the route answers 422 with reason window_too_short "
                    "and bars_received/bars_required, never a verdict."
                ),
                "routes": {
                    "verify": "POST /api/rigor/verify",
                },
            },
            # Simulated paper deployments (POST /api/paper/deployments) are live.
            # The graded book is paper_daily_returns. paper_agent_trades is an
            # executor ledger from PR #1704 and is not on main — status "live"
            # below does not advertise that table. `deploy` is roadmap.
            "paper": {
                "status": "live",
                "auth_required": True,
                "routes": {
                    "deploy": "POST /api/paper/deployments",
                    "list": "GET /api/paper/deployments",
                    "read": "GET /api/paper/deployments/{deployment_id}",
                    "stop": "POST /api/paper/deployments/{deployment_id}/stop",
                },
            },
            # Roadmap, not a public surface. create_vault calls the deployed
            # VaultFactory, but the journey is gated off every shipped UI and
            # no user vault has ever been created.
            "deploy": {
                "status": "roadmap",
                "auth_required": True,
                "routes": {
                    "create_vault": "POST /api/vaults/create",
                },
            },
            # Marketplace is not a public surface. Route strings exist; do not
            # present publish/subscribe as a shipped journey. Billing settlement
            # rides PAYMENTS_DRY_RUN and is not claimed here.
            "marketplace": {
                "status": "roadmap",
                "auth_required": True,
                "routes": {
                    "publish": "POST /api/marketplace/publish",
                    "subscribe": "POST /api/marketplace/subscribe",
                },
            },
            # Roadmap: the health route resolves, but with no user vault there
            # is no live position of a user's to read.
            "monitor": {
                "status": "roadmap",
                "auth_required": False,
                "routes": {
                    "vault_health": "GET /api/vaults/{address}/health",
                },
            },
        },
        "faucet": {
            "url": "https://faucet.circle.com/",
            "description": "Free Arc testnet USDC (also used as gas on Arc). No mainnet money; generation still settles real testnet USDC — read GET /api/generate/quote.",
        },
    }
