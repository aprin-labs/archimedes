"""Shared AsyncWeb3 client singleton for Arc chain interactions.

Connects to Arc testnet RPC, loads agent account from env vars.
All contract calls route through this client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientTimeout
from eth_account import Account
from eth_account.signers.local import LocalAccount
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings
from web3 import AsyncWeb3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.providers import AsyncHTTPProvider
from web3.providers.rpc.utils import ExceptionRetryConfiguration

from archimedes.chain.rate_limit import (
    DEFAULT_BASE_BACKOFF_SECONDS,
    DEFAULT_MAX_BACKOFF_SECONDS,
    HTTP_TOO_MANY_REQUESTS,
    RateLimitBackoff,
    RpcRateLimited,
    retry_after_seconds,
)
from archimedes.deadline import drain_abandoned, run_with_deadline

logger = logging.getLogger(__name__)

# Deployed synth token + oracle addresses for the FULL on-chain universe (all 281),
# loaded from the committed deploy-address SSOT (data/synthetic_addresses.json,
# regenerated per contract redeploy by scripts/gen_synthetic_addresses.py from the
# Foundry deploy manifest). Replaces the old hand-maintained 2-entry map so EVERY
# synth in ON_CHAIN_SYNTHS resolves — the whole 281 become priced + tradable, not just
# a demo handful. Per-synth ARC_<SYMBOL>_ADDRESS / _ORACLE_ADDRESS env vars still
# override at runtime (see _resolve_ssot_addresses). Address metadata lives here
# (per-deploy lifecycle); symbol metadata lives in synthetic_universe.json.
_ADDRESS_SSOT_PATH = Path(__file__).resolve().parent.parent / "data" / "synthetic_addresses.json"


def _load_address_defaults() -> tuple[dict[str, str], dict[str, str]]:
    """Load ``{symbol: token}`` and ``{symbol: oracle}`` from the deploy-address SSOT.

    Fail-safe: on any read/parse error, returns empty maps + logs an error (the app
    boots with env-only resolution rather than crashing) — mirrors universe.py's loader.
    """
    try:
        raw = json.loads(_ADDRESS_SSOT_PATH.read_text())
        tokens = {s: v["token"] for s, v in raw.items() if isinstance(v, dict) and v.get("token")}
        oracles = {s: v["oracle"] for s, v in raw.items() if isinstance(v, dict) and v.get("oracle")}
        return tokens, oracles
    except Exception as exc:  # Never let an address-file issue crash import.
        logger.error("client: could not load %s (%s) — synth addresses resolve from env only", _ADDRESS_SSOT_PATH, exc)
        return {}, {}


_SYNTH_DEFAULTS, _ORACLE_DEFAULTS = _load_address_defaults()


def _resolve_ssot_addresses(defaults: dict[str, str], suffix: str) -> dict[str, str]:
    """Build ``{SSOT symbol: address}`` over the deploy-eligible universe (#764).

    For each symbol in ``universe.ON_CHAIN_SYNTHS``, resolve ``ARC_<SYMBOL>_<suffix>``
    else the committed transitional default. Symbols with no address — most of the SSOT
    until the T3.2 redeploy mints them — are EXCLUDED, so consumers only ever see DEPLOYED
    synths with non-empty addresses (the invariant the previous 7-field map upheld).
    Compliance-held single stocks (sTSLA/sNVDA) are not in the SSOT, so they no longer
    appear on the live path.

    **Resolution source (important):** overrides are read from ``os.environ`` via
    ``os.getenv`` — NOT through pydantic-settings' ``env_file`` source. Unlike the declared
    ``ChainSettings`` fields (which pydantic resolves from ``.env`` directly), these
    per-synth keys are only seen when the variables are present in the *process
    environment*. Every real entrypoint satisfies this: the FastAPI app via
    ``main.load_dotenv`` (it loads ``.env`` into ``os.environ`` at import); the
    ``oracle`` / ``agent_runner`` processes via docker-compose's ``env_file: .env``
    (which injects ``.env`` into the container environment); AND a *bare, non-docker*
    ``python -m archimedes.chain.{oracle,agent}_runner`` run, because those modules
    now call ``load_dotenv`` in their ``__main__`` block (mirroring ``main.py``;
    ``override=False`` so an exported env / docker env_file still wins). Tests use
    ``monkeypatch.setenv``. So the only way to miss the per-synth overrides is to
    embed ``ChainSettings`` in a *new* custom entrypoint that neither loads ``.env``
    nor exports the vars — in which case add a ``load_dotenv`` there too.
    """
    from archimedes.universe import ON_CHAIN_SYNTHS

    resolved: dict[str, str] = {}
    for symbol in ON_CHAIN_SYNTHS:
        addr = os.getenv(f"ARC_{symbol.upper()}_{suffix}", "") or defaults.get(symbol, "")
        if addr:
            resolved[symbol] = addr
    return resolved


@dataclass(frozen=True)
class ChainEndpoint:
    """One resolved chain: which id, which RPC, and whether it was chosen.

    ``explicit`` records whether this endpoint came from its own
    ``ARC_PAYMENTS_*`` / ``ARC_EXECUTION_*`` variables or fell back to the
    single-chain ``chain_id`` / ``arc_rpc_url``. Callers that need to know a
    chain was *decided* rather than *inherited* — a mainnet payment path, a
    startup assertion — read this instead of comparing ids and guessing.
    """

    chain_id: int
    rpc_url: str
    explicit: bool


class ChainSettings(BaseSettings):
    """On-chain connection settings — loaded from .env or environment variables.

    **Contract addresses are externalized (roadmap T2.3).** Every address field
    below is read from an environment variable, falling back to the value shown as
    its default when the variable is unset — so nothing breaks if the env is
    unset, while a redeploy can repoint the backend at new contracts without a
    code change. Because ``env_prefix = "ARC_"``, the override variable for a
    field is its name upper-cased with the ``ARC_`` prefix:

    - ``usdc_address``      ← ``ARC_USDC_ADDRESS``
    - ``amm_router_address`` ← ``ARC_AMM_ROUTER_ADDRESS``
    - ``vault_factory_address`` ← ``ARC_VAULT_FACTORY_ADDRESS``

    Per-synth token + oracle addresses are NOT individual fields — they are resolved
    SSOT-driven over ``universe.ON_CHAIN_SYNTHS`` in the ``synth_addresses`` /
    ``oracle_addresses`` properties (``ARC_<SYMBOL>_ADDRESS`` /
    ``ARC_<SYMBOL>_ORACLE_ADDRESS`` env, else a committed transitional default;
    undeployed synths excluded). See #764.

    The defaults match the deployed Arc-testnet contracts and the ``ARC_*=...`` lines
    emitted by ``backend/archimedes/scripts/deploy_contracts.py``; the full set of
    override variables is documented in ``.env.example``.
    """

    # RPC
    arc_rpc_url: str = "https://rpc.testnet.arc.network"
    chain_id: int = 5042002  # Arc testnet chain ID (0x4cef52)
    # TOTAL wall-clock budget for one JSON-RPC call, retries and backoff included
    # (N2 — dead-egress detection). A blackholed NAT (e.g. a NAT instance down,
    # see infra/cloudwatch.tf's per-NAT StatusCheckFailed alarm) otherwise hangs
    # the call with no timeout at all, which can exceed /health's own budget (the
    # ECS task healthCheck timeout is 5s — infra/ecs.tf) and gets a perfectly
    # healthy app process killed for an RPC-layer problem. 3s leaves headroom
    # inside that 5s budget for /health's other work (DB, corpus, LLM probes).
    # ARC_RPC_TIMEOUT_SECONDS overrides.
    #
    # "TOTAL" is load-bearing and is why rpc_retries exists below. Passing this
    # as the aiohttp timeout alone bounds ONE ATTEMPT, not the call: web3 7.x
    # retries allowlisted methods (eth_call, eth_chainId, eth_getBalance, ... —
    # 43 of them) five times by default, so the real ceiling was
    # 5 x 3s + backoff = 16.9s, measured, against a documented 3s. That gap is
    # what turns a brief RPC blip into a 90s nginx 504 on /api/config/contracts
    # instead of a 3s degraded read, and it defeats the very cascade this
    # timeout was added to prevent. rpc_retry_policy() below re-derives the
    # per-attempt timeout from this total so the number here is the truth.
    rpc_timeout_seconds: float = 3.0
    # Attempts per call (1 = no retry). web3 counts this as the TOTAL attempt
    # count, not extra tries beyond the first — see AsyncHTTPProvider._make_request,
    # `for i in range(retries)`. Two keeps one cheap retry for a genuine transient
    # blip while leaving each attempt ~1.4s, roughly 9x the live Arc round trip.
    # ARC_RPC_RETRIES overrides.
    rpc_retries: int = 2
    # 429 backoff knobs (#1594). The Arc RPC rate-limits our egress IP, and
    # every ECS task shares ONE of those (the NAT gateway), so its per-IP limit
    # is a FLEET-wide budget — task churn multiplies RPC pressure on itself.
    # These two are env-overridable (ARC_RPC_RATE_LIMIT_BASE_BACKOFF_SECONDS /
    # ARC_RPC_RATE_LIMIT_MAX_BACKOFF_SECONDS) specifically so the numbers can be
    # tuned during an incident without a code change; the semantics — doubling
    # window, full jitter, Retry-After as a floor — live in chain/rate_limit.py.
    rpc_rate_limit_base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS
    rpc_rate_limit_max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS

    # ── Two-chain split (#1240) ────────────────────────────────────────────
    # Payments (USDC / x402 / Gateway / PaymentSplitter) and execution (vault,
    # synth, AMM, oracle) run on one chain today and become two at the Arc
    # mainnet cutover, where the payment rail goes to mainnet and execution
    # stays on testnet.
    #
    # All four default to the single-chain values above, so an environment that
    # sets none of them behaves exactly as it does now. That is the point: this
    # is a seam, not a cutover. Setting them is what moves a chain.
    #
    # A HALF-CONFIGURED SPLIT IS REJECTED at construction rather than filled in
    # from the other chain — see _reject_half_configured_split below. Silently
    # borrowing the execution RPC for a payments chain id would mean settling
    # real USDC against an endpoint nobody chose, which is the fail-soft
    # pattern docs/architectural-principles.md forbids for anything a claim
    # depends on. Money is the strongest such claim we make.
    payments_chain_id: int | None = None  # ARC_PAYMENTS_CHAIN_ID
    payments_rpc_url: str = ""  # ARC_PAYMENTS_RPC_URL
    execution_chain_id: int | None = None  # ARC_EXECUTION_CHAIN_ID
    execution_rpc_url: str = ""  # ARC_EXECUTION_RPC_URL

    # Agent account (the address that calls rebalance, publishes traces, etc.)
    agent_private_key: str = ""
    agent_address: str = ""  # Will be derived from private key if empty

    # Marketplace subscription readiness thresholds
    min_vault_usdc: float = 10.0  # ARC_MIN_VAULT_USDC
    min_active_action_buffer: int = 20  # ARC_MIN_ACTIVE_ACTION_BUFFER — in action-fees

    # Owner account (for admin operations like oracle price updates)
    owner_private_key: str = ""

    # Contract addresses — env-overridable via ARC_<FIELD>; defaults = deployed
    # Arc testnet contracts (Deploy.s.sol / deploy_contracts.py emits the
    # ARC_*=... lines). Empty defaults mark deployment-specific addresses that
    # must be supplied via .env before that contract can be used.
    usdc_address: str = "0x3600000000000000000000000000000000000000"  # ARC_USDC_ADDRESS
    amm_router_address: str = ""  # ARC_AMM_ROUTER_ADDRESS
    synthetic_factory_address: str = ""  # ARC_SYNTHETIC_FACTORY_ADDRESS
    vault_factory_address: str = ""  # ARC_VAULT_FACTORY_ADDRESS
    reasoning_trace_registry_address: str = ""  # ARC_REASONING_TRACE_REGISTRY_ADDRESS
    asset_registry_address: str = ""  # ARC_ASSET_REGISTRY_ADDRESS
    strategy_registry_address: str = ""  # ARC_STRATEGY_REGISTRY_ADDRESS
    payment_splitter_address: str = ""  # ARC_PAYMENT_SPLITTER_ADDRESS

    # NOTE: per-synth token + oracle addresses are NO LONGER individual fields. They
    # are resolved SSOT-driven over universe.ON_CHAIN_SYNTHS in synth_addresses /
    # oracle_addresses below — each from ARC_<SYMBOL>_ADDRESS / ARC_<SYMBOL>_ORACLE_ADDRESS
    # (env) else a committed transitional default (_SYNTH_DEFAULTS / _ORACLE_DEFAULTS),
    # with undeployed synths excluded. This keeps the map keyed to the SSOT (no
    # sTSLA/sNVDA drift) and can't go stale as the universe grows. (#764)

    # Paths
    abi_dir: str = str(Path(__file__).resolve().parents[3] / "contracts" / "abis")

    model_config = {"env_prefix": "ARC_", "env_file": ".env", "extra": "ignore"}

    @field_validator("payments_chain_id", "execution_chain_id", mode="before")
    @classmethod
    def _blank_chain_id_is_unset(cls, v: object) -> object:
        """Treat an empty string as unset, because .env.example ships these blank.

        ``cp .env.example .env`` — the documented first step in SETUP.md — puts
        ``ARC_PAYMENTS_CHAIN_ID=`` in the environment as an empty string, and
        pydantic will not parse that as an int. Without this the whole backend
        refuses to start for anyone who follows the setup instructions, while
        working fine for anyone whose .env predates these lines. Blank means
        "no split configured", which is the same thing as absent.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _reject_half_configured_split(self) -> ChainSettings:
        """Refuse to boot on a chain id without its RPC, or an RPC without its id.

        The tempting default is to fill the missing half from the single-chain
        settings. That produces a process that reports one chain and talks to
        another, and on the payments side it would do so while moving real USDC.
        A missing half is an incomplete decision, so it stops here with the
        variable name that would complete it.
        """
        for label, chain_id, rpc_url in (
            ("PAYMENTS", self.payments_chain_id, self.payments_rpc_url),
            ("EXECUTION", self.execution_chain_id, self.execution_rpc_url),
        ):
            if chain_id is not None and not rpc_url:
                raise ValueError(
                    f"ARC_{label}_CHAIN_ID is set to {chain_id} but ARC_{label}_RPC_URL is empty. "
                    f"Set both or neither — a chain id without its own endpoint would silently "
                    f"reuse the ARC_ARC_RPC_URL endpoint while reporting a different chain."
                )
            if rpc_url and chain_id is None:
                raise ValueError(
                    f"ARC_{label}_RPC_URL is set but ARC_{label}_CHAIN_ID is empty. "
                    f"Set both or neither — an endpoint with no declared chain id cannot be "
                    f"checked against what the RPC actually reports."
                )
        return self

    @property
    def payments_chain(self) -> ChainEndpoint:
        """The chain USDC settles on. Falls back to the single-chain settings."""
        if self.payments_chain_id is None:
            return ChainEndpoint(chain_id=self.chain_id, rpc_url=self.arc_rpc_url, explicit=False)
        return ChainEndpoint(chain_id=self.payments_chain_id, rpc_url=self.payments_rpc_url, explicit=True)

    @property
    def execution_chain(self) -> ChainEndpoint:
        """The chain vaults, synths, AMM and oracles live on.

        Every contract address on this settings object belongs to THIS chain,
        which is why ``chain_id`` / ``arc_rpc_url`` keep their present meaning
        and every existing signing call site stays correct without change.
        """
        if self.execution_chain_id is None:
            return ChainEndpoint(chain_id=self.chain_id, rpc_url=self.arc_rpc_url, explicit=False)
        return ChainEndpoint(chain_id=self.execution_chain_id, rpc_url=self.execution_rpc_url, explicit=True)

    @property
    def is_split_chain(self) -> bool:
        """True when payments and execution resolve to different chain ids.

        The browser wallet UX turns on this: one chain needs no switching, two
        do. Reading it beats comparing ids at each call site and drifting.
        """
        return self.payments_chain.chain_id != self.execution_chain.chain_id

    @property
    def agent_account(self) -> LocalAccount | None:
        if not self.agent_private_key:
            return None
        return Account.from_key(self.agent_private_key)

    @property
    def owner_account(self) -> LocalAccount | None:
        if not self.owner_private_key:
            return None
        return Account.from_key(self.owner_private_key)

    @property
    def synth_addresses(self) -> dict[str, str]:
        """Deployed synth token addresses keyed by SSOT symbol (#764).

        SSOT-driven over ``universe.ON_CHAIN_SYNTHS``; each resolved from
        ``ARC_<SYMBOL>_ADDRESS`` (env, loaded from ``.env`` by ``main.load_dotenv``) else a
        committed transitional default. Undeployed synths (most of the SSOT until the T3.2
        redeploy) are EXCLUDED, so consumers only ever see deployed synths with non-empty
        addresses. Compliance-held single stocks (sTSLA/sNVDA) are not in the SSOT and no
        longer appear.
        """
        return _resolve_ssot_addresses(_SYNTH_DEFAULTS, "ADDRESS")

    @property
    def oracle_addresses(self) -> dict[str, str]:
        """Deployed price-oracle addresses keyed by SSOT symbol — see ``synth_addresses`` (#764)."""
        return _resolve_ssot_addresses(_ORACLE_DEFAULTS, "ORACLE_ADDRESS")


# web3's retry backoff sleeps ``backoff_factor * 2**i`` between attempts, for
# i = 0 .. retries-2 (AsyncHTTPProvider._make_request). Keeping the default here
# means the only thing rpc_retry_policy has to solve for is the per-attempt timeout.
RPC_BACKOFF_FACTOR = 0.125

# Mirrors web3's own default set (AsyncHTTPProvider.exception_retry_configuration)
# so this override changes ONLY the attempt count, never which failures retry.
# Python 3.11+ aliases asyncio.TimeoutError to the builtin, so the builtin covers
# the aiohttp ClientTimeout expiry too.
RPC_RETRY_ERRORS = (ClientError, TimeoutError)


def rpc_retry_policy(
    total_seconds: float, retries: int, backoff_factor: float = RPC_BACKOFF_FACTOR
) -> tuple[float, int]:
    """Split a TOTAL wall-clock budget into web3's ``(per-attempt timeout, attempts)``.

    web3 bounds one attempt; callers care about the whole call. Worst case is
    ``attempts * per_attempt + backoff_factor * (2**(attempts-1) - 1)``, so this
    subtracts the backoff web3 will sleep and divides what is left. Feed the
    first element to ``ClientTimeout(total=...)`` and the second to
    ``ExceptionRetryConfiguration(retries=...)`` — passing the requested
    ``retries`` instead of the returned one reintroduces the bug.

    Both halves are returned because a budget too small to split cannot be fixed
    by shortening attempts alone: at 0.2s over 5 attempts the mandatory backoff
    is 1.875s on its own, and a per-attempt timeout that fits would be negative.
    That case drops to a single attempt spending the whole budget and logs it.
    Fewer attempts is a degraded retry policy; a non-positive timeout is a broken
    client, and holding ``retries`` while shortening the timeout is neither — it
    just multiplies the budget by the attempt count again.
    """
    attempts = max(1, retries)
    backoff_total = backoff_factor * (2 ** (attempts - 1) - 1)
    per_attempt = (total_seconds - backoff_total) / attempts
    if per_attempt <= 0:
        logger.warning(
            "rpc budget %.3fs cannot cover %d attempts (backoff alone is %.3fs) — falling back to a single attempt",
            total_seconds,
            attempts,
            backoff_total,
        )
        return total_seconds, 1
    return per_attempt, attempts


class BoundedAsyncHTTPProvider(AsyncHTTPProvider):
    """``AsyncHTTPProvider`` with a hard ceiling on EVERY request (#1592).

    The aiohttp ``ClientTimeout`` below bounds *the HTTP request*. It does not
    bound *the call*, and the gap between those two is what wedged production on
    2026-08-31: with the Arc RPC unreachable from inside the VPC, ``/health``
    hung past the ALB's 5s check, no new task ever turned healthy, and the
    rollout sat at 1/2 for its full 1200s budget while the serving task's event
    loop starved every other route.

    What lives in that gap, in ``web3`` 7.16, on the path of every single async
    RPC (``HTTPSessionManager.async_get_response_from_post_request`` →
    ``async_cache_and_return_session``)::

        async with async_lock(self.session_pool, self._lock):   # ← before any timeout

    ``async_lock`` is ``await loop.run_in_executor(thread_pool, lock.acquire)``
    over a **class-level ``threading.Lock``** and a **5-worker**
    ``ThreadPoolExecutor``. ``lock.acquire`` is uninterruptible once it is in a
    worker thread, it runs entirely before the request timeout is armed, and
    five stalled acquires exhaust the pool so every later RPC in the process
    queues behind them. One dark endpoint therefore starves a whole event loop
    instead of failing one call — and it does so *outside* the timeout the
    settings promise.

    #1507 bounded one call (``rpc_timeout_seconds`` split across attempts so
    web3's retry loop could not multiply it). This bounds the CLIENT: the
    documented total budget becomes a wall-clock ceiling on every request that
    leaves this provider, whichever layer inside web3 stalls. On expiry the call
    raises :class:`TimeoutError`, which every caller already treats as a failed
    read — ``ChainClient.is_connected`` maps it to ``False``, ``oracle_health``
    records it as a read error. No caller learns a *different* answer than it
    would have; it just learns one in bounded time.

    **It is also 429-aware (#1594).** ``raise_for_status=True`` on web3's session
    turns Arc's throttle into a ``ClientResponseError`` that is a subclass of
    ``ClientError`` — which is in this client's retry set — so web3 already
    retried a "you are sending too much" response, immediately, on a fixed
    unjittered schedule, adding to the burst that caused it. Two behaviours are
    layered on top here, both outside web3's retry loop:

    * **Backoff with full jitter before a retry**, spent from THIS call's
      remaining budget so the documented total is still the total.
    * **A cooldown that fails the next request fast** rather than sending it.
      During the incident every ECS task shared one NAT egress IP, so Arc's
      per-IP limit was a fleet-wide budget and each cold start's RPC burst
      throttled the fleet it was joining. A request skipped during a cooldown
      fails as :class:`RpcRateLimited` — an honest, named error, never a
      fabricated success — and costs the fleet nothing.

    See chain/rate_limit.py for why a throttle is not just another transport
    error and why the jitter is load-bearing rather than cosmetic.
    """

    # How long ``disconnect`` waits for deadline-abandoned calls to unwind before
    # it gives up and quarantines the session instead of closing it (#1632). Sized
    # off the same clock the strays are on: a stray is at worst one full
    # ``total_budget_seconds`` behind, and a shutdown that waits a couple of
    # seconds is invisible next to ECS's 30s SIGTERM grace.
    TEARDOWN_DRAIN_SECONDS: float = 2.0

    def __init__(
        self,
        *args: Any,
        total_budget_seconds: float,
        rate_limit_backoff: RateLimitBackoff | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._total_budget_seconds = total_budget_seconds
        self._rate_limit = rate_limit_backoff or RateLimitBackoff()

    async def disconnect(self) -> None:
        """Close the provider's sessions — but NEVER out from under a stray (#1632).

        ``AsyncHTTPProvider.disconnect`` walks the session cache and closes every
        session. That is unconditional, and this provider is precisely the object
        that also *abandons* calls, so before this override the abandonment path
        satisfied the one condition #1632 says must never hold: **an abandoned
        call sharing a session object whose lifecycle the abandoner controls.**

        Measured, not assumed (transcript in the PR): with one deadline-abandoned
        ``eth_chainId`` in flight against a loopback RPC, the provider's connector
        held exactly one acquired protocol, and ``disconnect()`` took it to::

            BEFORE  session.closed=False  connector.closed=False  acquired=1
            AFTER   session.closed=True   connector.closed=True   acquired=0

        ``BaseConnector._close`` does that by calling ``proto.close()`` on every
        entry of ``_acquired`` — the stray's live transport included — and then
        clearing the set.

        Scope of what is proven, because #1632 is explicit that its cause is a
        *hypothesis*: the teardown-under-a-live-reader above is measured. The step
        from there to SIGSEGV is not — no crash was reproduced here or anywhere,
        on plain HTTP the stray unwound without incident, and this override does
        not make the segfault reproducible. What it does is remove the ordering
        that would make one possible, which is worth doing on its own terms: an
        in-flight request whose transport is closed under it is a bug whether or
        not it is *the* bug, and it is cheap to stop doing.

        So the order is inverted: **drain first, close second, and if the drain
        does not reach zero, do not close at all.** Declining is the safe branch —
        a quarantined session leaks a socket in a process that is on its way out,
        which costs nothing next to freeing a buffer a native reader still holds.
        Quarantine is also stable rather than merely deferred: returning early
        leaves the session in web3's cache, so it keeps a strong reference and
        cannot become garbage while the stray is alive (``Connection.__del__``
        would otherwise close the transport from a GC pass — the same free, minus
        the stack frame that would tell you where it happened).

        Not chosen, and why — the other two options #1632 lists:

        * *Per-call sessions.* Sound in principle, but it pays a TCP+TLS handshake
          on every JSON-RPC, and web3 7.16 caches by ``(loop id, endpoint)`` inside
          ``HTTPSessionManager``, so getting there means fighting the library on
          the hot path to fix a shutdown-ordering bug.
        * *A dedicated never-closed session for strays.* Unsound as stated: you
          cannot migrate an in-flight request onto a different session after the
          fact, and #1593's stall is in ``async_lock`` — BEFORE the session is in
          hand — so at abandonment time there is frequently no particular session
          the stray owns yet. Quarantining "the current one" would sometimes
          quarantine a session the stray never touches while it later picks up the
          replacement.

        Draining has neither problem: it makes no claim about *which* session a
        stray holds, only that no stray is holding anything when we start freeing.
        """
        remaining = await drain_abandoned(self.TEARDOWN_DRAIN_SECONDS)
        if remaining:
            logger.error(
                "SESSION_QUARANTINED: %d abandoned RPC call(s) still in flight after a "
                "%.1fs drain — NOT closing %s's sessions. Freeing a connection a stray "
                "still holds is the #1632 segfault; leaking one is not.",
                remaining,
                self.TEARDOWN_DRAIN_SECONDS,
                self.endpoint_uri,
            )
            return
        await super().disconnect()

    async def make_request(self, method: Any, params: Any) -> Any:
        inner = super().make_request
        return await self._request_backing_off_on_429(
            lambda: inner(method, params),
            label=f"eth-rpc {method}",
        )

    async def make_batch_request(self, batch_requests: Any) -> Any:
        inner = super().make_batch_request
        return await self._request_backing_off_on_429(
            lambda: inner(batch_requests),
            label=f"eth-rpc batch({len(batch_requests)})",
        )

    async def _request_backing_off_on_429(self, call: Any, *, label: str) -> Any:
        """Run ``call()`` under the total budget, backing off on HTTP 429.

        ``call`` is a zero-argument callable returning a fresh awaitable —
        callable rather than awaitable because a retry needs a *new* request,
        and because nothing should be constructed before the deadline is armed
        (same contract as ``HealthProbeCache.probe``).

        The budget is spent, never extended: every attempt and every backoff
        sleep comes out of the same ``total_budget_seconds`` deadline, so a
        throttled endpoint can make this call *fail sooner* but never make it
        take longer than the number ``rpc_timeout_seconds`` documents. That
        matters because /health's probe budgets are sized against it.
        """
        deadline = time.monotonic() + self._total_budget_seconds

        cooling = self._rate_limit.remaining()
        if cooling > 0:
            # Loud, greppable literal for a CloudWatch metric filter, matching
            # the HEALTH_CHAIN_DISCONNECTED / HEALTH_ORACLE_STALE convention.
            logger.warning(
                "RPC_RATE_LIMIT_COOLDOWN: %s not sent — endpoint asked for %.2fs more backoff (consecutive_429=%d)",
                label,
                cooling,
                self._rate_limit.consecutive,
            )
            raise RpcRateLimited(
                f"{label} skipped: Arc RPC returned 429 and asked for {cooling:.2f}s more backoff "
                f"(consecutive_429={self._rate_limit.consecutive})"
            )

        while True:
            remaining = deadline - time.monotonic()
            try:
                result = await run_with_deadline(call(), max(0.0, remaining), label=label)
            except ClientResponseError as exc:
                if exc.status != HTTP_TOO_MANY_REQUESTS:
                    raise
                delay = self._rate_limit.note_rate_limited(retry_after_seconds(exc.headers))
                budget_left = deadline - time.monotonic()
                logger.warning(
                    "RPC_RATE_LIMITED: %s got HTTP 429 (consecutive_429=%d) — backing off %.2fs "
                    "with %.2fs of budget left",
                    label,
                    self._rate_limit.consecutive,
                    delay,
                    budget_left,
                )
                if delay >= budget_left:
                    # No honest way to retry inside the promise this call made.
                    # Re-raise the endpoint's own answer rather than blowing the
                    # budget or inventing a value; the cooldown set above is
                    # what protects the NEXT caller.
                    raise
                await asyncio.sleep(delay)
                continue

            # Only a completed response clears the backoff. A timeout or a
            # connection error propagates from run_with_deadline untouched and
            # leaves the consecutive count alone — neither is evidence that the
            # endpoint has stopped throttling us.
            self._rate_limit.note_success()
            return result


class ChainClient:
    """Singleton Web3 client for all on-chain interactions."""

    def __init__(self, settings: ChainSettings | None = None):
        self.settings = settings or ChainSettings()
        # request_kwargs={"timeout": ClientTimeout(...)} — NOT a bare number:
        # aiohttp's ClientSession._request coerces a bare number fine on the
        # request itself, but web3's HTTPSessionManager.async_cache_and_return_session
        # separately accesses `request_timeout.total` on its own session-eviction
        # path, which requires an actual ClientTimeout instance (see N2).
        per_attempt, attempts = rpc_retry_policy(self.settings.rpc_timeout_seconds, self.settings.rpc_retries)
        self.w3 = AsyncWeb3(
            # BoundedAsyncHTTPProvider, not AsyncHTTPProvider: the aiohttp timeout
            # below is the inner bound and total_budget_seconds is the outer one
            # that holds even when the stall is in web3's own session-manager
            # lock, outside aiohttp entirely. See the class docstring (#1592).
            BoundedAsyncHTTPProvider(
                self.settings.arc_rpc_url,
                total_budget_seconds=self.settings.rpc_timeout_seconds,
                # 429-aware backoff (#1594). web3's own retry set already
                # contains ClientResponseError, so without this a throttle was
                # retried like a connection reset — immediately, unjittered,
                # by every task behind the shared NAT IP at once.
                rate_limit_backoff=RateLimitBackoff(
                    base_seconds=self.settings.rpc_rate_limit_base_backoff_seconds,
                    max_seconds=self.settings.rpc_rate_limit_max_backoff_seconds,
                ),
                request_kwargs={"timeout": ClientTimeout(total=per_attempt)},
                # Passed explicitly, never left to default: web3's default is five
                # attempts, which multiplies rpc_timeout_seconds by five and breaks
                # the total-budget promise the setting documents.
                exception_retry_configuration=ExceptionRetryConfiguration(
                    errors=RPC_RETRY_ERRORS,
                    retries=attempts,
                    backoff_factor=RPC_BACKOFF_FACTOR,
                ),
            )
        )

        # Arc uses POA consensus — add the middleware
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    async def is_connected(self) -> bool:
        try:
            return await self.w3.is_connected()
        except Exception:
            return False

    async def get_chain_id(self) -> int:
        return await self.w3.eth.chain_id

    async def get_block_number(self) -> int:
        return await self.w3.eth.block_number

    async def get_native_balance(self, address: str) -> int:
        return await self.w3.eth.get_balance(address)

    def to_checksum(self, address: str) -> str:
        return self.w3.to_checksum_address(address)

    def to_wei(self, value: float, unit: str = "ether") -> int:
        return self.w3.to_wei(value, unit)

    def from_wei(self, value: int, unit: str = "ether") -> float:
        return self.w3.from_wei(value, unit)


@lru_cache(maxsize=1)
def get_chain_client() -> ChainClient:
    """Get or create the singleton chain client."""
    return ChainClient()


# Module-level convenience
chain_client = get_chain_client()
