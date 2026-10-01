"""Oracle updater — fetches real-world prices and pushes them on-chain.

Implements IOracleUpdater from archimedes/interfaces/chain.py.
Equity/ETF prices (and the #775 cross-check's secondary reading) come from
the market-data provider seam (``archimedes.services.market_data_provider``,
#1218) — default provider yfinance, unchanged behavior; vendor-swappable via
``MARKET_DATA_PROVIDER``. **Every read this module makes is on the ``intraday``
seam (#1798)**, including ``_fetch_sp500_moving_averages``'s daily ^GSPC bars:
``fetch_market_snapshot`` reads ^VIX and ^GSPC inside ONE run, and the ADR's
"never mix vendors inside one run" rule is what pins both to the same vendor.
Flipping the daily-bar vendor (``MARKET_DATA_DAILY_PROVIDER``) therefore does
not touch this module. Crypto reaches that same seam as of #1710, through
an ordered ``ORACLE_CRYPTO_SOURCE`` cascade whose default keeps CoinGecko as
the primary and adds the provider seam as the documented fallback. Pushes
prices via Circle Developer Controlled Wallets API (the oracle owner wallet is
a Circle-managed wallet — no raw private key available).

Every symbol in the push set that ends a cycle with NO price from ANY
configured source is logged by name with its reason
(``_log_push_exclusions``) and left unpushed. No price is ever fabricated,
defaulted or carried forward to fill a source gap.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from datetime import UTC, datetime

import aiohttp

from archimedes.models.asset import AssetPrice, MarketSnapshot

logger = logging.getLogger(__name__)

# Symbol → yfinance ticker mapping.
#
# Only synths that are actually on the live on-chain universe belong here — the
# fetch loop (fetch_prices) and the secondary cross-check (_cross_check_secondary)
# both key off this map, so a stale entry means a wasted download and a misleading
# "fetched N prices" log every cycle. Retired single-stock synths (sTSLA/sNVDA,
# dropped from the SSOT in #725 pending securities-compliance review) and the
# futures/index synths retired in #842 (sGOLD → sGLD/sXAU, sOIL/sNKY dropped) are
# no longer in universe.ON_CHAIN_SYNTHS, so they are removed here too. sSPY is the
# only live synth with a yfinance ticker; the ^-prefixed entries are index tickers
# (not synths) used for regime signals — the S&P 500 moving averages
# (_fetch_sp500_moving_averages) and VIX (fetch_market_snapshot) — and are excluded
# from the synth fetch loop by the leading-"s" filter, so they stay.
YFINANCE_MAP = {
    "sSPY": "SPY",
    "^GSPC": "^GSPC",  # S&P 500 index (regime signal, not a synth)
    "^VIX": "^VIX",  # VIX index (regime signal, not a synth)
}

# Symbol → CoinGecko ID
CRYPTO_MAP = {
    "sBTC": "bitcoin",
}

# Symbol → market-data-provider vendor ticker for the SAME crypto synths (#1710).
#
# CRYPTO_MAP above stays the push-set SSOT (``oracle_health._push_set_symbols``
# reads it) and keeps holding CoinGecko ids; this map is the second coordinate
# the same symbols need to be askable through the market-data provider seam
# (``services.market_data_provider``, ``seam="intraday"``). Ticker shape is the yfinance/SSOT
# convention ("<BASE>-USD", matching ``synthetic_universe.json``'s
# ``yfinance_ticker`` for every crypto entry) because that is what the seam's
# adapters take: ``YFinanceProvider`` passes it straight through and
# ``TiingoProvider._classify_tiingo_ticker`` routes it to the crypto endpoint
# family by that exact shape.
#
# A symbol present in CRYPTO_MAP but absent here is not a silent gap: the
# provider leg records "no vendor ticker mapped" as its named exclusion reason.
CRYPTO_VENDOR_TICKERS = {
    "sBTC": "BTC-USD",
}

# ─── Crypto source order (#1710) ───
# Before this, the crypto leg was hardcoded to CoinGecko with NO fallback and
# NO seam involvement, so a CoinGecko miss (429/timeout/shape change) silently
# dropped the symbol from the push cycle — the on-chain oracle then aged out
# and `/health`'s `oracle_fresh` flipped false with nothing in the log naming
# the symbol as excluded. Both halves of that are fixed here: crypto is now
# askable through the ``MARKET_DATA_PROVIDER`` seam like every other price
# (docs/adr/market-data-sourcing.md's "reversible by build" claim was not true
# of this leg), and a symbol no configured source could price is excluded from
# the push attempt with a NAMED reason (never a fabricated or substituted
# price).
#
# Modes map to an ordered (primary, …fallback) source list:
#   • ``coingecko``      (DEFAULT) — CoinGecko first, provider seam as the
#                        documented fallback. Deploying this change is a
#                        no-op on the happy path: the price still comes from
#                        CoinGecko with ``source="coingecko"``, byte-for-byte
#                        as before. Only a CoinGecko MISS behaves differently
#                        (it now has somewhere to go instead of vanishing).
#   • ``coingecko_only`` — literal pre-#1710 behavior: CoinGecko, no fallback.
#   • ``provider``       — seam first, CoinGecko as the documented fallback.
#                        This is the flip to make once the active provider can
#                        actually serve intraday crypto quotes.
#   • ``provider_only``  — seam only, NO CoinGecko fallback. The licensing-
#                        strict mode: the ADR's "never mix vendors" rule means
#                        an operator who flipped to a licensed vendor may want
#                        a miss to stay a miss rather than be quietly filled
#                        from an unlicensed free API.
#
# NOTE (verified against the adapter and the seam table, not assumed):
# ``TiingoProvider`` raises ``NotImplementedError`` from
# ``get_intraday_quotes_batch`` — Tiingo serves crypto DAILY bars only today.
# Since #1798 that no longer decides this leg's fate: this leg asks the
# ``intraday`` seam, and ``_VENDOR_SEAMS`` lists Tiingo on ``daily`` only, so
# ``MARKET_DATA_PROVIDER=tiingo`` resolves the intraday seam to yfinance (with
# a warning naming the substitution) and ``provider`` / ``provider_only`` keep
# pricing this leg from yfinance. The ``NotImplementedError`` handler below
# stays as the belt-and-braces path for a future vendor that declares the
# intraday seam without implementing all of it.
DEFAULT_CRYPTO_SOURCE = "coingecko"
_CRYPTO_SOURCE_ORDER: dict[str, tuple[str, ...]] = {
    "coingecko": ("coingecko", "provider"),
    "coingecko_only": ("coingecko",),
    "provider": ("provider", "coingecko"),
    "provider_only": ("provider",),
}


def _crypto_source_order() -> tuple[str, ...]:
    """Ordered crypto source legs from ``ORACLE_CRYPTO_SOURCE``.

    Fails SAFE to the default on an unrecognized value (logged), matching
    ``_int_env`` and ``price_source.price_source_mode`` — a config typo must
    not crash a funds-adjacent singleton runner.
    """
    raw = os.getenv("ORACLE_CRYPTO_SOURCE", DEFAULT_CRYPTO_SOURCE).strip().lower()
    order = _CRYPTO_SOURCE_ORDER.get(raw)
    if order is None:
        logger.warning(
            "unknown ORACLE_CRYPTO_SOURCE=%r (expected one of %s) — falling back to %r",
            raw,
            ", ".join(sorted(_CRYPTO_SOURCE_ORDER)),
            DEFAULT_CRYPTO_SOURCE,
        )
        return _CRYPTO_SOURCE_ORDER[DEFAULT_CRYPTO_SOURCE]
    return order


CIRCLE_API_BASE = "https://api.circle.com/v1/w3s"
CIRCLE_BLOCKCHAIN = "ARC-TESTNET"

# ─── Push-confirmation polling (#905) ───
# A 201 from Circle's contractExecution endpoint means "submission accepted",
# NOT "landed on-chain" — the tx can still FAIL/revert later. Every push is
# polled to a terminal state before the price is treated as pushed; mirrors
# circle_signer._poll_transaction's states/cadence.
#
# Split into success/failure subsets (#1525) so a submission response can be
# graded correctly: "COMPLETE"/"CONFIRMED" are terminal successes (Circle can
# return either depending on how far the tx got before the API call returned
# — e.g. a fast testnet tx that already landed by the time the create-call's
# response is built), "FAILED"/"DENIED"/"CANCELLED" are terminal failures.
# Before #1525, the create-response handler only trusted HTTP 201 and logged
# EVERY OTHER response — including a 200 carrying a terminal-success state —
# as "Circle API error", which buried genuine failures under bogus ones.
_TX_SUCCESS_STATES = {"COMPLETE", "CONFIRMED"}
_TX_FAILURE_STATES = {"FAILED", "DENIED", "CANCELLED"}
_TX_TERMINAL_STATES = _TX_SUCCESS_STATES | _TX_FAILURE_STATES
_TX_POLL_INTERVAL_S = 2.0
_TX_MAX_POLLS = 60  # 2 minutes max per tx

# ─── Wedged-tx abandonment (#1525) ───
# _TX_MAX_POLLS/_TX_POLL_INTERVAL_S bound how long ONE push cycle will poll a
# tx before giving up (2 minutes). They do NOT protect against a tx that is
# wedged on Circle's side across MANY CYCLES: the per-symbol idempotency key
# is deterministic on (wallet, contract, price), so an unchanged price (e.g.
# an equity synth over a weekend) reproduces the SAME key every cycle, Circle
# dedups to the SAME never-resolving tx id, and this runner re-polls that one
# id forever with no path back to a fresh submission. After this many
# consecutive cycles seeing the identical unresolved id for a symbol, abandon
# it (WARN) and force a new Circle tx next cycle. Override via
# ORACLE_TX_MAX_REPOLLS.
DEFAULT_TX_MAX_REPOLLS = 10

# ─── Sanity bounds for on-chain pushes (audit #13 / issue #508) ───
# Max allowed move vs the last known good price, in basis points.
# Default mirrors PriceOracle.sol's maxDeviationBps (2000 bps = 20%) so the
# backend rejects a corrupted upstream price before spending a tx the
# contract would revert anyway. Override via ORACLE_MAX_DEVIATION_BPS.
DEFAULT_MAX_DEVIATION_BPS = 2000
# Max age of the upstream observation before we refuse to push it on-chain.
# Override via ORACLE_MAX_UPSTREAM_STALENESS_SECONDS.
DEFAULT_MAX_UPSTREAM_STALENESS_SECONDS = 900  # 15 minutes
# Secondary-source cross-check band (#775): the max divergence (bps) between a
# NON-yfinance primary (Pyth/Stork via the PRICE_SOURCE cascade) and an
# independent yfinance reading before we fail closed. Generous default — both
# sources lag between updates; Önder owns tuning. 0 disables the cross-check.
DEFAULT_CROSSCHECK_BAND_BPS = 5000  # 50%
# Secondary-source staleness window (#775 Phase 2): max age (seconds) of the
# yfinance secondary's bar timestamp before it's treated as unusable for the
# cross-check (fail-open, same as a missing secondary). yfinance's
# interval="1m" intraday bar goes stale over weekends/holidays for equity-like
# instruments — a Friday-close bar can be up to ~65 hours old by Monday
# morning before market open, so the threshold needs headroom past a normal
# 2-day weekend to avoid spurious staleness flags every Monday; 4 days covers
# a 3-day holiday weekend too.
DEFAULT_CROSSCHECK_MAX_STALENESS_SECONDS = 345_600  # 4 days

# ─── Staleness-aware re-seed escape (#1341) ───
# The deviation cap above is measured against the LAST ON-CHAIN PRICE, which
# makes it self-deadlocking after an outage: once the runner has been down long
# enough for the market to move past the band, every subsequent push is refused
# for deviating from a baseline that only a push could refresh. Observed
# 2026-08-20 after a 13-day runner outage — sSPY refused at 2983 bps and sBTC at
# 3341 bps vs 2026-08-07 prices, forever, with no code path back.
#
# The escape is deliberately narrow. It fires ONLY when all of these hold:
#   1. the normal band already rejected the push (nothing else changes),
#   2. the on-chain baseline's own age (PriceOracle.lastUpdated) is CONFIRMED
#      older than ORACLE_STALE_RESEED_AFTER_SECONDS — an unreadable age fails
#      closed, exactly like an unobtainable reference price, and
#   3. the move is still inside a hard absolute ceiling
#      (ORACLE_MAX_RESEED_DEVIATION_BPS), so a decimal-shift glitch or a
#      corrupted feed is refused even during an outage window.
# It then pushes via forceSetPrice rather than setPrice, because PriceOracle.sol
# enforces the SAME 2000 bps maxDeviationBps on-chain — a widened backend band
# alone would just move the revert from the backend to the chain and burn a tx.
# forceSetPrice is owner-only and bounded on-chain by FORCE_MAX_DEVIATION_BPS.
#
# Default staleness threshold: 24h, matching PriceOracle.MAX_STALENESS. Below
# that the on-chain price still reads as fresh to every consumer, so there is no
# emergency to recover from; at/beyond it the price is ALREADY degraded, so a
# bounded re-seed can only improve the state. Set to 0 to disable the escape
# entirely and restore the pre-#1341 (deadlocking) behavior.
DEFAULT_STALE_RESEED_AFTER_SECONDS = 86_400  # 24h — mirrors PriceOracle.MAX_STALENESS
# Hard absolute ceiling on a recovery push, in bps vs the stale baseline. 7500
# (75%) comfortably clears any plausible multi-week move in the live push set
# (the real 13-day gaps were 2983 and 3341 bps) while still refusing the classic
# corruption shapes: a ÷10 decimal shift is 9000 bps and a ×10 is 90_000 bps,
# both rejected. Override via ORACLE_MAX_RESEED_DEVIATION_BPS.
DEFAULT_MAX_RESEED_DEVIATION_BPS = 7500

# Contract entry points. The normal cadence uses setPrice (updater-or-owner,
# band-limited on-chain); a #1341 re-seed uses forceSetPrice (owner-only escape
# hatch, bounded by FORCE_MAX_DEVIATION_BPS). Both are in contracts/abis/PriceOracle.json.
_SET_PRICE_FN = "setPrice(uint256)"
_FORCE_SET_PRICE_FN = "forceSetPrice(uint256)"


def _int_env(name: str, default: int) -> int:
    """Parse an integer env var, failing SAFE to ``default`` rather than raising at
    oracle-runner startup.

    A missing or blank value silently uses ``default`` — an unset optional var is
    normal and must not log on every startup. A value that is PRESENT but non-numeric
    (a typo) is a real misconfiguration, so it logs a warning before falling back. A
    bare ``int(os.getenv(...))`` would instead crash the runner on either.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default  # unset/blank optional var → default, no warning (not a misconfig)
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("invalid %s=%r (not an integer) — falling back to %d", name, raw, default)
        return default


def _encrypt_entity_secret(entity_secret_hex: str, public_key_pem: str) -> str:
    """Encrypt entity secret with Circle's RSA public key (OAEP/SHA-256).

    Circle requires a fresh ciphertext per request to prevent replay attacks.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    public_key = serialization.load_pem_public_key(public_key_pem.encode())
    plaintext = bytes.fromhex(entity_secret_hex)
    ciphertext = public_key.encrypt(
        plaintext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode()


class OracleUpdater:
    """Fetches market prices and pushes them to on-chain PriceOracle contracts."""

    def __init__(self) -> None:
        self._price_cache: dict[str, AssetPrice] = {}
        # symbol → why no source produced an observation THIS cycle (#1710).
        # Cleared at the top of every fetch_prices(); read by
        # _log_push_exclusions to name each excluded symbol in the runner log.
        self._source_miss_reasons: dict[str, str] = {}
        self._circle_public_key: str | None = None  # cached per instance lifetime

        # Circle credentials from env
        self._api_key: str = os.getenv("CIRCLE_API_KEY", "")
        self._entity_secret: str = os.getenv("CIRCLE_ENTITY_SECRET", "")
        self._wallet_id: str = os.getenv("WALLET_ID", "")

        # Sanity bounds (audit #13 / issue #508). Parsed fail-safe: a non-numeric
        # env var degrades to the default instead of crashing the runner at startup.
        self._max_deviation_bps: int = _int_env("ORACLE_MAX_DEVIATION_BPS", DEFAULT_MAX_DEVIATION_BPS)
        self._max_upstream_staleness_s: int = _int_env(
            "ORACLE_MAX_UPSTREAM_STALENESS_SECONDS", DEFAULT_MAX_UPSTREAM_STALENESS_SECONDS
        )
        # Last price (6-dec int) we successfully submitted, per symbol —
        # fallback deviation reference when the on-chain read fails.
        self._last_pushed_price_int: dict[str, int] = {}

        # Staleness-aware re-seed escape (#1341).
        self._stale_reseed_after_s: int = _int_env(
            "ORACLE_STALE_RESEED_AFTER_SECONDS", DEFAULT_STALE_RESEED_AFTER_SECONDS
        )
        self._max_reseed_deviation_bps: int = _int_env(
            "ORACLE_MAX_RESEED_DEVIATION_BPS", DEFAULT_MAX_RESEED_DEVIATION_BPS
        )
        # Symbols whose CURRENT validation pass qualified for a re-seed and must
        # therefore be pushed via forceSetPrice instead of setPrice. Rewritten on
        # every _validate_for_push call (the symbol is discarded first), so a mark
        # can never outlive the cycle that earned it.
        self._pending_reseed: set[str] = set()

        # Secondary-source cross-check band (#775).
        self._crosscheck_band_bps: int = _int_env("PRICE_CROSSCHECK_BAND_BPS", DEFAULT_CROSSCHECK_BAND_BPS)
        # Secondary-source staleness window (#775 Phase 2).
        self._crosscheck_max_staleness_s: int = _int_env(
            "PRICE_CROSSCHECK_MAX_STALENESS_SECONDS", DEFAULT_CROSSCHECK_MAX_STALENESS_SECONDS
        )

        # Wedged-tx abandonment (#1525).
        self._tx_max_repolls: int = _int_env("ORACLE_TX_MAX_REPOLLS", DEFAULT_TX_MAX_REPOLLS)
        # symbol -> (tx id, consecutive cycles seen unresolved). Cleared as soon
        # as that symbol's tx reaches any terminal state, or a different tx id
        # shows up (a legitimate new submission, not a wedge).
        self._wedge_tracking: dict[str, tuple[str, int]] = {}
        # symbol -> salt bumped on abandonment so the next idempotency key is
        # guaranteed to differ from the wedged one even if the price hasn't
        # moved (the whole reason the old key kept deduping to the dead tx).
        self._tx_retry_salt: dict[str, int] = {}

    # ─── Public API ──────────────────────────────────────────────

    async def fetch_prices(self) -> list[AssetPrice]:
        """Fetch current prices for all synthetic assets.

        The source is governed by the ``PRICE_SOURCE`` env (North Star §10.5):
          • ``yfinance`` (default) — legacy behavior, unchanged; a deploy of this
            change is a no-op until the flag is flipped.
          • ``cascade`` — Pyth Hermes for the symbols it covers, yfinance for the
            rest, CoinGecko for crypto Pyth misses.
          • ``pyth_hermes`` — Pyth only (no yfinance/CoinGecko fallback).
        Admin overrides (``ADMIN_PRICES_JSON``) apply on top in every mode.

        The cascade is the safety net: any Pyth miss/error falls back
        transparently, and every price keeps its true ``source`` + upstream
        timestamp so the on-chain push staleness/deviation gates stay meaningful.
        Zero contract change — only *where* the price comes from differs.
        """
        # Lazy import: keep the `archimedes.services` package (and its import-time
        # cycle) off oracle_updater's module-load path so the standalone oracle
        # runner imports cleanly.
        from archimedes.services.price_source import load_admin_prices, price_source_mode

        # Named-exclusion bookkeeping is per-cycle (#1710): last cycle's reasons
        # must never be reported against this cycle's misses.
        self._source_miss_reasons = {}

        now = datetime.now(UTC)
        mode = price_source_mode()
        equity_symbols = {k: v for k, v in YFINANCE_MAP.items() if k.startswith("s")}

        if mode in ("cascade", "pyth_hermes"):
            prices = await self._fetch_cascade(equity_symbols, now, strict_pyth=(mode == "pyth_hermes"))
        else:
            prices = list(await asyncio.to_thread(self._fetch_yfinance, equity_symbols, now))
            prices.extend(await self._fetch_crypto(now))

        # Manual admin overrides (pin/demo) win over the fetched price, in any mode.
        admin = load_admin_prices()
        if admin:
            prices = [admin.get(p.symbol, p) for p in prices]
            have = {p.symbol for p in prices}
            prices.extend(ap for sym, ap in admin.items() if sym not in have)

        for p in prices:
            self._price_cache[p.symbol] = p

        logger.info("Fetched %d prices (source=%s)", len(prices), mode)
        self._log_push_exclusions(prices)
        return prices

    def _log_push_exclusions(self, prices: list[AssetPrice]) -> None:
        """Name every push-set symbol this cycle produced no price for (#1710).

        The push set is derived the SAME way ``oracle_health._push_set_symbols``
        derives the probed set — ``YFINANCE_MAP``'s leading-``"s"`` synth keys
        plus ``CRYPTO_MAP`` — so the set that gets a "why it is missing" line
        here is exactly the set ``/health``'s ``oracle_fresh`` keys on. Without
        this, a symbol whose upstream returned nothing simply never appeared in
        ``push_prices_on_chain``'s loop: no rejection line (that only fires for
        a price that EXISTS and fails a gate), no exclusion line, nothing —
        while its on-chain oracle quietly aged past ``MAX_STALENESS`` and held
        ``archimedes-oracle-stale`` in ALARM. That is the "starved symbol"
        failure mode #1710 reports, and it was invisible to a runner-log grep.

        WARNING level on purpose: this is not a routine event, and the issue's
        acceptance criterion ("no push-universe symbol whose source errored in
        the prior 24h") is a grep over exactly these lines.

        No price is ever synthesized to fill the gap — the anti-goal is
        explicit in the issue and restated in the log text so a reader of the
        line cannot mistake exclusion for substitution.
        """
        expected = {s for s in YFINANCE_MAP if s.startswith("s")} | set(CRYPTO_MAP)
        have = {p.symbol for p in prices}
        for symbol in sorted(expected - have):
            reason = self._source_miss_reasons.get(symbol) or "no observation returned by any configured source"
            logger.warning(
                "oracle push exclusion: %s EXCLUDED from this push cycle — %s. "
                "No price fabricated or substituted; the on-chain oracle keeps its previous "
                "value and will read stale until a source returns data for this symbol.",
                symbol,
                reason,
            )

    async def _fetch_cascade(
        self, equity_symbols: dict[str, str], now: datetime, *, strict_pyth: bool
    ) -> list[AssetPrice]:
        """Pyth-first cascade: Pyth for covered symbols, yfinance/CoinGecko for the
        rest (unless ``strict_pyth``). Pyth failures degrade gracefully to fallback."""
        from archimedes.services.price_source import PYTH_FEED_IDS, fetch_pyth_prices

        pyth_targets = [s for s in (*equity_symbols, *CRYPTO_MAP) if s in PYTH_FEED_IDS]
        pyth = await fetch_pyth_prices(pyth_targets)

        if strict_pyth:
            # Strict Pyth: no fallback by contract. Keep every observation — stale
            # ones are rejected downstream by _validate_for_push and no source fills
            # the gap (that's the strict-mode promise).
            return list(pyth.values())

        # Cascade: a STALE Pyth observation must NOT count as "covered". Otherwise the
        # symbol is dropped downstream by the staleness gate (_validate_for_push) with
        # no yfinance/CoinGecko fallback to fill it — the exact gap the cascade exists
        # to close (e.g. off-hours equity feeds). Only FRESH Pyth prices count as
        # covered; stale ones fall through to the fallback sources below.
        cap = self._max_upstream_staleness_s

        def _is_fresh(p: AssetPrice) -> bool:
            observed = p.timestamp if p.timestamp.tzinfo else p.timestamp.replace(tzinfo=UTC)
            return (now - observed).total_seconds() <= cap

        fresh_pyth = {sym: p for sym, p in pyth.items() if _is_fresh(p)}
        covered = set(fresh_pyth)
        prices: list[AssetPrice] = list(fresh_pyth.values())

        remaining_equity = {k: v for k, v in equity_symbols.items() if k not in covered}
        if remaining_equity:
            prices.extend(await asyncio.to_thread(self._fetch_yfinance, remaining_equity, now))

        # Only fill crypto symbols Pyth didn't freshly cover. _fetch_crypto fetches the
        # whole CRYPTO_MAP, so filter its result to the uncovered set — otherwise a
        # symbol Pyth already covered would be pushed twice.
        uncovered_crypto = {s for s in CRYPTO_MAP if s not in covered}
        if uncovered_crypto:
            crypto = await self._fetch_crypto(now)
            prices.extend(p for p in crypto if p.symbol in uncovered_crypto)

        return prices

    async def push_prices_on_chain(self, prices: list[AssetPrice]) -> str | None:
        """Call PriceOracle.setPrice() for each asset via Circle Wallets API.

        One exception (#1341): a symbol the validation pass marked as an outage
        re-seed is sent via ``forceSetPrice`` instead — see
        ``_stale_reseed_permitted``. Still exactly one submission per symbol per
        cycle; the function selector is the only thing that differs.

        Two phases (#905): submit every price, then poll each Circle tx to a
        terminal state. ``_last_pushed_price_int`` — the deviation guard's
        fallback reference — is updated ONLY for txs that reach ``COMPLETE``.
        Recording it on HTTP 201 (submission accepted) treated a later on-chain
        revert as success: the next tick's deviation guard then compared against
        a price that was never written, silently defeating the guard and masking
        a stuck oracle.
        """
        if not self._api_key or not self._entity_secret or not self._wallet_id:
            logger.warning(
                "Circle credentials not configured "
                "(CIRCLE_API_KEY / CIRCLE_ENTITY_SECRET / WALLET_ID) — "
                "skipping on-chain price push"
            )
            return None

        from archimedes.chain.client import chain_client

        oracle_addresses = chain_client.settings.oracle_addresses
        # (symbol, human price, 6-dec int, Circle tx id, state already reported
        # by the create-transaction call itself — None when the submission was
        # still in flight) per accepted submission, pending on-chain
        # confirmation.
        submitted: list[tuple[str, float, int, str, str | None]] = []
        confirmed_tx_ids: list[str] = []

        async with aiohttp.ClientSession() as session:
            public_key = await self._get_circle_public_key(session)
            if not public_key:
                logger.error("Failed to fetch Circle public key — aborting price push")
                return None

            for price in prices:
                oracle_addr = oracle_addresses.get(price.symbol)
                if not oracle_addr:
                    logger.debug(f"No oracle address for {price.symbol} — skipping")
                    continue

                price_int = int(price.price_usd * 1e6)  # 6 decimals, matches PriceOracle.sol

                # Sanity gate (audit #13 / issue #508): refuse to push prices
                # that are non-positive, stale upstream, or deviate too far
                # from the last known good price.
                rejection = await self._validate_for_push(price, price_int)
                # Secondary-source cross-check (#775): only when the sanity gate
                # already passed (avoid the extra yfinance fetch on a rejected price).
                if rejection is None:
                    rejection = await self._cross_check_secondary(price)
                if rejection is not None:
                    logger.warning(f"Refusing to push {price.symbol} price {price.price_usd}: {rejection}")
                    continue

                # Staleness-aware re-seed (#1341): the validation above marks a
                # symbol here only when the normal band rejected it AND the
                # on-chain baseline was confirmed stale past the threshold. Such
                # a push must use the owner-only forceSetPrice escape hatch —
                # setPrice would be rejected by the contract's own 2000 bps
                # maxDeviationBps for the same reason the backend band rejected
                # it, so the tx would revert and the oracle would stay frozen.
                is_reseed = price.symbol in self._pending_reseed
                fn_signature = _FORCE_SET_PRICE_FN if is_reseed else _SET_PRICE_FN

                # Wedged-tx abandonment (#1525): if the LAST cycle's tx for this
                # symbol has now been seen unresolved for `_tx_max_repolls`
                # consecutive cycles, it is never going to resolve on its own —
                # bump the retry salt so the idempotency key below is
                # guaranteed fresh (Circle would otherwise dedup right back to
                # the same dead tx) and drop the old tracking entry.
                wedged_id, wedged_count = self._wedge_tracking.get(price.symbol, (None, 0))
                if wedged_count >= self._tx_max_repolls:
                    logger.warning(
                        "Circle tx %s for %s re-polled %d consecutive cycles with no "
                        "terminal state — abandoning it and submitting fresh",
                        wedged_id,
                        price.symbol,
                        wedged_count,
                    )
                    self._tx_retry_salt[price.symbol] = self._tx_retry_salt.get(price.symbol, 0) + 1
                    self._wedge_tracking.pop(price.symbol, None)

                try:
                    ciphertext = _encrypt_entity_secret(self._entity_secret, public_key)

                    # Deterministic idempotency key (#F5): derived from the
                    # stable identifying content of this exact call (wallet +
                    # oracle contract + function + price), not a fresh random
                    # UUID per call. A retry of THIS SAME push (same symbol,
                    # same price) now produces the same key, so Circle's
                    # dedup can actually recognize it as a retry, while a
                    # genuinely different push (different oracle or price)
                    # still produces a different key. uuid5 is used so the
                    # result is a validly-formatted UUID per Circle's
                    # IdempotencyKey schema. `retrySalt` defaults to 0 (no
                    # behavior change) and only moves once wedge-abandonment
                    # above has fired for this symbol (#1525).
                    # `abiFunctionSignature` is part of the key source, so a #1341
                    # re-seed (forceSetPrice) can never dedup onto a previously
                    # submitted setPrice for the same price — it is a genuinely
                    # different call and gets a genuinely different key.
                    idempotency_source = json.dumps(
                        {
                            "walletId": self._wallet_id,
                            "contractAddress": oracle_addr,
                            "abiFunctionSignature": fn_signature,
                            "abiParameters": [str(price_int)],
                            "retrySalt": self._tx_retry_salt.get(price.symbol, 0),
                        },
                        sort_keys=True,
                    )
                    idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_source))

                    payload = {
                        "idempotencyKey": idempotency_key,
                        "walletId": self._wallet_id,
                        "contractAddress": oracle_addr,
                        "abiFunctionSignature": fn_signature,
                        "abiParameters": [str(price_int)],
                        "feeLevel": "MEDIUM",
                        "blockchain": CIRCLE_BLOCKCHAIN,
                        "entitySecretCiphertext": ciphertext,
                    }

                    async with session.post(
                        f"{CIRCLE_API_BASE}/developer/transactions/contractExecution",
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                    ) as resp:
                        body = await resp.json()
                        # Circle's create-transaction call can validly return
                        # either 201 or 200 with a transaction id — including a
                        # terminal-success state already (a fast testnet tx, or
                        # an idempotency-key dedup hitting an already-resolved
                        # tx). Grading solely on `resp.status == 201` treated
                        # every one of those legitimate 200s as an error (#1525:
                        # "Circle API error for sBTC (200): {'state': 'COMPLETE'}"
                        # logged every cycle for a push that had already
                        # succeeded). Grade on the payload instead: a usable tx
                        # id whose state isn't a genuine failure is a success;
                        # only a terminal failure state or a response with no
                        # usable id at all is a real error, named explicitly.
                        data = body.get("data") if isinstance(body, dict) else None
                        tx_id = data.get("id") if isinstance(data, dict) else None
                        state = data.get("state") if isinstance(data, dict) else None
                        if tx_id and state not in _TX_FAILURE_STATES:
                            submitted.append((price.symbol, price.price_usd, price_int, tx_id, state))
                            logger.info(
                                f"Submitted {price.symbol} price {price.price_usd:.2f} → Circle tx {tx_id}"
                                + (f" ({state})" if state else "")
                            )
                        else:
                            logger.error(
                                "Circle API error for %s (%s): %s",
                                price.symbol,
                                resp.status,
                                f"tx {tx_id} state={state}" if tx_id else body,
                            )
                except Exception:
                    logger.exception(f"Failed to push price for {price.symbol}")

            # ── Confirmation phase (#905): poll each submission to a terminal
            # state. Only a terminal-success tx (COMPLETE/CONFIRMED, #1525)
            # counts as pushed; anything else leaves the cached deviation
            # reference untouched and is logged loudly so a stuck oracle is
            # visible to operators, never masked.
            for symbol, price_usd, price_int, tx_id, submit_state in submitted:
                # The create-transaction call itself can already report a
                # terminal-success state (#1711: an idempotency-key dedup hit
                # on a tx that completed several cycles ago — unchanged price,
                # e.g. an equity synth over a weekend, keeps producing the
                # same idempotency key). Re-polling that tx via
                # `_poll_circle_tx` (`/transactions?pageSize=50`) is not just
                # wasteful, it is WRONG: that endpoint only returns the most
                # recent 50 transactions account-wide, so an already-resolved
                # tx from a prior cycle can have scrolled off the window by
                # the time this poll runs. `_poll_circle_tx` would then report
                # "TIMEOUT" for a push that had already succeeded — logging a
                # false error AND feeding the wedge tracker below, which after
                # `_tx_max_repolls` consecutive false hits abandons a
                # perfectly good tx and submits a genuinely NEW on-chain
                # transaction for a price already pushed, burning gas for
                # nothing. A submit-time terminal-success state is already
                # authoritative — narrow the parse to trust it instead of
                # re-deriving the same fact from a narrower, staler view.
                if submit_state in _TX_SUCCESS_STATES:
                    state = submit_state
                else:
                    state = await self._poll_circle_tx(session, tx_id)
                was_reseed = symbol in self._pending_reseed
                if state in _TX_SUCCESS_STATES:
                    self._last_pushed_price_int[symbol] = price_int
                    confirmed_tx_ids.append(tx_id)
                    self._wedge_tracking.pop(symbol, None)
                    logger.info(f"Pushed {symbol} price {price_usd:.2f} on-chain (Circle tx {tx_id} {state})")
                    if was_reseed:
                        # Loud close-out of the #1341 recovery. The baseline is now
                        # `now`, so the next cycle's staleness check fails and the
                        # normal band is back in force — stated here so the log
                        # itself shows the band re-tightening, not just the escape.
                        self._pending_reseed.discard(symbol)
                        logger.warning(
                            "STALE-RESEED COMPLETE for %s: on-chain baseline re-seeded to %.2f via %s "
                            "(Circle tx %s %s); the normal %d bps deviation band governs from the next cycle",
                            symbol,
                            price_usd,
                            _FORCE_SET_PRICE_FN,
                            tx_id,
                            state,
                            self._max_deviation_bps,
                        )
                else:
                    logger.error(
                        "Oracle push for %s (price %.2f, Circle tx %s) ended %s — "
                        "on-chain price NOT updated; deviation-guard reference unchanged",
                        symbol,
                        price_usd,
                        tx_id,
                        state,
                    )
                    if was_reseed:
                        # forceSetPrice is onlyOwner. If the Circle wallet is wired as
                        # `updater` but not `owner`, every recovery attempt reverts here
                        # — name the manual escape rather than leaving an operator to
                        # infer it from a bare terminal state.
                        logger.error(
                            "STALE-RESEED FAILED for %s: the %s recovery tx did not land, so the oracle is "
                            "STILL FROZEN on its stale baseline. If this repeats, check that the Circle "
                            "wallet is the PriceOracle owner (forceSetPrice is onlyOwner); the manual "
                            "unblock is PriceOracle.forceSetPrice(%d) from the owner key.",
                            symbol,
                            _FORCE_SET_PRICE_FN,
                            price_int,
                        )
                    # Wedged-tx tracking (#1525): only a TIMEOUT — this exact
                    # tx never reaching ANY terminal state within the poll
                    # budget — is evidence of a wedge. A genuine terminal
                    # failure (FAILED/DENIED/CANCELLED) is a resolved outcome,
                    # not a wedge: clear any prior tracking so a legitimately
                    # failed-then-freshly-retried tx doesn't inherit a stale
                    # count.
                    if state == "TIMEOUT":
                        wedged_id, wedged_count = self._wedge_tracking.get(symbol, (None, 0))
                        self._wedge_tracking[symbol] = (
                            tx_id,
                            wedged_count + 1 if wedged_id == tx_id else 1,
                        )
                    else:
                        self._wedge_tracking.pop(symbol, None)

        return confirmed_tx_ids[0] if confirmed_tx_ids else None

    async def _poll_circle_tx(self, session: aiohttp.ClientSession, circle_tx_id: str) -> str:
        """Poll a Circle tx to a terminal state (#905).

        Same states/cadence as ``circle_signer._poll_transaction``, but returns
        the terminal state string (``"TIMEOUT"`` after ``_TX_MAX_POLLS``) instead
        of raising, so one failed price push cannot abort the confirmation of
        the other symbols in the same batch. Poll errors are treated as
        still-processing — only an explicit terminal state or the timeout ends
        the loop, and only ``COMPLETE`` is ever treated as success.
        """
        for _ in range(_TX_MAX_POLLS):
            try:
                async with session.get(
                    f"{CIRCLE_API_BASE}/transactions?pageSize=50",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        for tx in body.get("data", {}).get("transactions", []):
                            if tx.get("id") == circle_tx_id:
                                state = tx.get("state", "UNKNOWN")
                                if state in _TX_TERMINAL_STATES:
                                    return state
                                break  # found but still processing
            except Exception:
                logger.warning("Circle tx %s poll attempt failed — retrying", circle_tx_id, exc_info=True)
            await asyncio.sleep(_TX_POLL_INTERVAL_S)
        return "TIMEOUT"

    async def fetch_market_snapshot(self) -> MarketSnapshot:
        """Fetch a full market snapshot with prices + regime signals."""
        prices = await self.fetch_prices()
        price_map = {p.symbol: p.price_usd for p in prices}
        now = datetime.now(UTC)

        vix_result = await self._fetch_yfinance_single("^VIX")
        vix = vix_result[0] if vix_result is not None else None
        sp500_data = await asyncio.to_thread(self._fetch_sp500_moving_averages)

        snapshot = MarketSnapshot(
            timestamp=now,
            prices=price_map,
            vix=vix,
            sp500_ma50=sp500_data.get("ma50"),
            sp500_ma200=sp500_data.get("ma200"),
        )
        await self._write_redis_price_snapshots(price_map)
        return snapshot

    async def _write_redis_price_snapshots(self, price_map: dict[str, float]) -> None:
        """Publish oracle price snapshot to Redis (#1103)."""
        try:
            import redis.asyncio as _aioredis

            redis_url = (
                os.getenv("REDIS_URL")
                or f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}/0"
            )
            r = _aioredis.from_url(redis_url, decode_responses=True)
            await r.set("oracle:prices:latest", json.dumps(price_map))
            await r.aclose()
        except Exception:
            logger.debug("Redis price snapshot write failed", exc_info=True)

    def get_cached_price(self, symbol: str) -> AssetPrice | None:
        return self._price_cache.get(symbol)

    # ─── Private helpers ──────────────────────────────────────────

    async def _validate_for_push(self, price: AssetPrice, price_int: int) -> str | None:
        """Sanity-check a price before pushing on-chain (audit #13 / issue #508).

        Returns a human-readable rejection reason, or None when the price is
        safe to push. Checks, in order:

        1. Zero/non-positive guard — the 6-decimal int must be > 0 (the
           contract rejects zero; sub-microdollar floats truncate to 0).
        2. Upstream staleness — the observation must be younger than
           ``ORACLE_MAX_UPSTREAM_STALENESS_SECONDS`` (default 15 min).
        3. Deviation cap — the move vs the last known good price (on-chain
           read, falling back to the last successfully pushed value) must be
           within ``ORACLE_MAX_DEVIATION_BPS`` (default 2000 = 20%, mirroring
           PriceOracle.sol's maxDeviationBps). A *confirmed-absent* reference
           (genuine first push) is allowed; an *unobtainable* reference (the
           on-chain read failed and there is no cached fallback) fails CLOSED —
           we refuse the push rather than send it unchecked (issue #587).
           A breach of this cap is NOT weakened by #1341: it is still a
           rejection unless the narrow stale-re-seed escape below applies.
        4. Stale re-seed escape (#1341) — consulted ONLY after step 3 has
           already rejected. See ``_stale_reseed_permitted``: it requires a
           *confirmed* on-chain baseline age past
           ``ORACLE_STALE_RESEED_AFTER_SECONDS`` and a move still inside
           ``ORACLE_MAX_RESEED_DEVIATION_BPS``. When both hold the push is
           allowed and the symbol is marked in ``_pending_reseed`` so
           ``push_prices_on_chain`` sends it via ``forceSetPrice`` (the
           contract enforces the same 2000 bps band on ``setPrice``, so a
           backend-only widening would revert on-chain and burn a tx).

        Single-source design (deferred multi-source to v2 per issue #508):
        Each symbol still resolves to exactly ONE upstream observation per
        cycle. #1710 gave the crypto leg an ordered FALLBACK chain
        (``ORACLE_CRYPTO_SOURCE``) — a second source is consulted only when the
        first produced nothing, and the winner's true vendor is stamped on
        ``AssetPrice.source``. That is failover, not corroboration; no two
        sources are ever compared here. Multi-source corroboration
        would require N independent feeds; that is a v2 enhancement documented
        in docs/design.md § Price Oracle. Today, we validate upstream
        freshness and deviation bounds against the last known good on-chain
        price, but do not require cross-source agreement. This is an
        intentional tradeoff: simplicity for v1 MVP, and the protocol is
        extensible should a second feed be added.
        """
        # Any prior re-seed mark for this symbol is void the moment we re-validate:
        # the mark is a property of THIS decision, never a sticky mode.
        self._pending_reseed.discard(price.symbol)

        if price_int <= 0:
            return f"non-positive on-chain price ({price.price_usd} → {price_int})"

        observed = price.timestamp if price.timestamp.tzinfo else price.timestamp.replace(tzinfo=UTC)
        age_s = (datetime.now(UTC) - observed).total_seconds()
        if age_s > self._max_upstream_staleness_s:
            return f"stale upstream data ({age_s:.0f}s old > {self._max_upstream_staleness_s}s cap)"

        reference_int, reference_known = await self._get_reference_price_int(price.symbol)
        if reference_int is None:
            if not reference_known:
                return (
                    "reference price unobtainable (on-chain read failed, no cached "
                    "fallback) — failing closed to avoid unchecked push"
                )
            # reference confirmed absent (genuine first push) → allow
        else:
            deviation_bps = abs(price_int - reference_int) * 10_000 / reference_int
            if deviation_bps > self._max_deviation_bps:
                if await self._stale_reseed_permitted(price.symbol, reference_int, price_int, deviation_bps):
                    self._pending_reseed.add(price.symbol)
                    return None
                return (
                    f"deviation {deviation_bps:.0f} bps vs last known price "
                    f"{reference_int / 1e6:.2f} exceeds {self._max_deviation_bps} bps cap"
                )

        return None

    async def _stale_reseed_permitted(
        self, symbol: str, reference_int: int, price_int: int, deviation_bps: float
    ) -> bool:
        """Decide whether an already-rejected deviation qualifies as an outage re-seed (#1341).

        Called ONLY from the deviation branch of ``_validate_for_push``, after that
        branch has decided to reject. Returning ``False`` therefore leaves the
        normal rejection exactly as it was — this can widen nothing on its own.

        Three conditions, all required, evaluated in this order:

        1. The escape is enabled (``ORACLE_STALE_RESEED_AFTER_SECONDS > 0``).
        2. The on-chain baseline's age is *confirmed* past that threshold.
           ``_reference_age_seconds`` returns ``None`` for every unreadable case
           (RPC failure, no address, never-set timestamp, a future timestamp), and
           an unknown age is never treated as stale — the same
           confirmed-absent-vs-unobtainable discipline the reference price itself
           uses (#587 part 2).
        3. The move is inside the hard absolute ceiling
           (``ORACLE_MAX_RESEED_DEVIATION_BPS``). This is the guard that keeps an
           outage from becoming a blank cheque: a corrupted feed during a stale
           window is still refused, and refused LOUDLY (ERROR), because "the
           baseline is stale AND the new price is implausible" is exactly the
           state an operator must look at by hand.

        A permitted re-seed logs at WARNING naming the old price, the new price,
        the deviation, and the baseline's age, per the issue's "LOUD log"
        requirement. It is inherently one-shot: a confirmed forceSetPrice moves
        ``lastUpdated`` to now, so the very next cycle sees a fresh baseline,
        condition 2 fails, and the normal 2000 bps band governs again. If the
        recovery tx does NOT land, the baseline stays stale and the next cycle
        retries — deliberately, since a silently-unrecovered oracle is the
        failure mode this issue exists to end. Every attempt is logged.
        """
        if self._stale_reseed_after_s <= 0:
            return False  # escape disabled → pre-#1341 behavior

        age_s = await self._reference_age_seconds(symbol)
        if age_s is None:
            logger.warning(
                "%s deviates %.0f bps (beyond the %d bps cap) but the on-chain baseline age is "
                "UNREADABLE — refusing the push rather than re-seeding on an unknown baseline",
                symbol,
                deviation_bps,
                self._max_deviation_bps,
            )
            return False
        if age_s < self._stale_reseed_after_s:
            return False  # baseline still fresh → an ordinary out-of-band price, reject it

        if deviation_bps > self._max_reseed_deviation_bps:
            logger.error(
                "STALE-RESEED REFUSED for %s: on-chain baseline %.6f is %.1fh old (past the %ds "
                "re-seed threshold) but the candidate %.6f moves %.0f bps, beyond the %d bps "
                "recovery ceiling — this looks like a corrupted feed, not an outage gap. "
                "On-chain price stays frozen; operator action required.",
                symbol,
                reference_int / 1e6,
                age_s / 3600,
                self._stale_reseed_after_s,
                price_int / 1e6,
                deviation_bps,
                self._max_reseed_deviation_bps,
            )
            return False

        logger.warning(
            "STALE-RESEED for %s: on-chain baseline %.6f is %.1fh old (past the %ds re-seed "
            "threshold), so the %.0f bps move to %.6f is an outage gap, not a feed glitch "
            "(within the %d bps recovery ceiling). Pushing ONCE via %s to unfreeze the oracle; "
            "the normal %d bps band applies again on the next cycle.",
            symbol,
            reference_int / 1e6,
            age_s / 3600,
            self._stale_reseed_after_s,
            deviation_bps,
            price_int / 1e6,
            self._max_reseed_deviation_bps,
            _FORCE_SET_PRICE_FN,
            self._max_deviation_bps,
        )
        return True

    async def _reference_age_seconds(self, symbol: str) -> float | None:
        """Age in seconds of the on-chain deviation baseline, or ``None`` when unknown.

        Reads ``PriceOracle.lastUpdated()`` through the same contract-loader call
        shape ``_get_reference_price_int`` uses for ``price()`` (and
        ``oracle_health`` uses for its freshness probe). ``lastUpdated`` — not
        ``lastSetPriceTime`` — is the right clock here: it is seeded by the
        constructor and moved by BOTH ``setPrice`` and ``forceSetPrice``, so a
        freshly deployed oracle and a manually force-seeded one both read as
        fresh, and it is the exact field the contract's own ``MAX_STALENESS``
        check keys off. ``lastSetPriceTime`` is 0 until the first ``setPrice``,
        which would read as infinitely stale on a healthy new deploy.

        Returns ``None`` — never a guess — on any read failure, a zero/unset
        timestamp, or a timestamp in the future (malformed chain state). The
        caller treats ``None`` as "not confirmed stale" and refuses the re-seed.
        """
        try:
            from archimedes.chain.contracts import get_contract_loader

            last_updated = await get_contract_loader().oracle_for(symbol).functions.lastUpdated().call()
        except Exception as e:
            logger.warning("Could not read on-chain lastUpdated for %s: %s", symbol, e)
            return None
        if not last_updated or int(last_updated) <= 0:
            return None
        age_s = datetime.now(UTC).timestamp() - int(last_updated)
        if age_s < 0:
            logger.warning(
                "On-chain lastUpdated for %s is in the future (%s) — treating age as unknown", symbol, last_updated
            )
            return None
        return age_s

    async def _cross_check_secondary(self, price: AssetPrice) -> str | None:
        """Secondary-source cross-check (#775): compare a primary that is NOT
        the active market-data provider (Pyth/Stork via the PRICE_SOURCE
        cascade) against an independent reading FROM the active provider
        (``MARKET_DATA_PROVIDER``, the #1218 seam's ``intraday`` half — default
        yfinance, today's behavior) and fail closed when they diverge beyond
        the band. Returns a rejection reason, or None to proceed.

        **The #775/#1218 seam tie-in.** The secondary reading is fetched via
        ``_fetch_yfinance_single`` → ``get_provider(seam="intraday")``,
        and the "same source, skip" guard below compares against
        ``provider_name("intraday")`` rather than a hardcoded ``"yfinance"``. Swap
        ``MARKET_DATA_PROVIDER`` to a new vendor and this guardrail's
        secondary source swaps with it automatically — no separate change
        needed here. (The variable/log names below keep saying "yfinance"
        because that is still the only implemented provider; the mechanism is
        vendor-generic.)

        **Asymmetric, by design.** A stale / missing / flaky secondary NEVER
        blocks a healthy primary — otherwise a secondary-provider outage
        (yfinance is known-flaky, #772) would become a trading halt and the
        guardrail itself the failure. Secondary problems only proceed-and-log;
        only a *confident* divergence between two healthy sources fails closed.

        Honest claim this earns: *"primary cross-checked against an independent
        market-data-provider reading, fail-closed on relative-magnitude divergence
        beyond a band, with the secondary's own bar timestamp checked for staleness
        first."* NOT "decentralized 2-of-3" (yfinance — the default/only provider
        today — is centralized + off-chain).

        **Staleness gate (#775 Phase 2).** Before comparing magnitudes, the
        secondary's bar timestamp is checked against
        ``PRICE_CROSSCHECK_MAX_STALENESS_SECONDS``. A stale secondary is treated the
        same as a missing one (fail-open, proceed on primary) — a stale reading was
        never validly comparable, so no divergence verdict is computed or reported,
        only a staleness verdict. This closes the previous magnitude-only gap where a
        stale-but-numerically-close secondary value could pass silently, or a
        stale-and-wildly-different one could in principle trip a false divergence.

        The check is a no-op when the primary already IS the active provider
        (same source, not an independent second opinion), an admin pin (operator
        last-resort override — must not be second-guessed), or when the symbol
        has no ticker mapped for the secondary read.
        """
        if self._crosscheck_band_bps <= 0:
            return None  # disabled
        from archimedes.services.market_data_provider import provider_name

        if price.source in (provider_name("intraday"), "admin"):
            # active provider: same source, not an independent second opinion.
            # admin: a last-resort operator pin (ADMIN_PRICES_JSON) — the whole point
            # is to override upstream, so the secondary guardrail must not block it.
            return None
        yf_ticker = YFINANCE_MAP.get(price.symbol)
        if not yf_ticker:
            return None  # no ticker mapped for this symbol → can't cross-check → proceed
        try:
            result = await self._fetch_yfinance_single(yf_ticker)
        except Exception as exc:
            logger.warning(
                "cross-check: yfinance fetch failed for %s (%s) — proceeding on primary (asymmetric)",
                price.symbol,
                exc,
            )
            return None
        if result is None:
            logger.info(
                "cross-check: no usable yfinance secondary for %s — proceeding on primary (asymmetric)",
                price.symbol,
            )
            return None
        secondary, bar_ts = result
        if secondary <= 0:
            logger.info(
                "cross-check: no usable yfinance secondary for %s — proceeding on primary (asymmetric)",
                price.symbol,
            )
            return None

        age_s = (datetime.now(UTC) - bar_ts).total_seconds()
        if age_s > self._crosscheck_max_staleness_s:
            logger.info(
                "cross-check: stale yfinance secondary for %s (bar age %.0fs > %ds cap) — "
                "proceeding on primary (asymmetric)",
                price.symbol,
                age_s,
                self._crosscheck_max_staleness_s,
            )
            return None

        deviation_bps = abs(price.price_usd - secondary) / secondary * 10_000
        if deviation_bps > self._crosscheck_band_bps:
            return (
                f"cross-check FAIL: primary({price.source}) {price.price_usd:.4f} diverges "
                f"{deviation_bps:.0f} bps from independent yfinance {secondary:.4f} "
                f"(band {self._crosscheck_band_bps} bps) — failing closed"
            )
        logger.debug(
            "cross-check OK for %s: primary(%s) %.4f vs yfinance %.4f (%.0f bps)",
            price.symbol,
            price.source,
            price.price_usd,
            secondary,
            deviation_bps,
        )
        return None

    async def _get_reference_price_int(self, symbol: str) -> tuple[int | None, bool]:
        """Resolve the deviation reference price (6-dec int) for a symbol.

        Returns a ``(reference_int, reference_known)`` tuple so the caller can
        distinguish a *confirmed-absent* reference (genuine first push, safe to
        allow) from an *unobtainable* one (on-chain read failed and no cached
        fallback — must fail closed to avoid an unchecked push). The two cases
        both used to surface as a bare ``None``, which let an RPC outage bypass
        the only deviation protection (issue #587, part 2).

        Cases:

        - on-chain read succeeds, price > 0 → ``(price_int, True)``
        - on-chain read succeeds but 0/empty, last-pushed cached → ``(last_pushed, True)``
        - on-chain read succeeds but 0/empty, no cache → ``(None, True)``
          (reference *confirmed absent* — genuine first push)
        - on-chain read throws, last-pushed cached → ``(last_pushed, True)``
          (RPC down, but we still hold a fallback reference)
        - on-chain read throws, no cache → ``(None, False)``
          (reference *unobtainable* — caller must fail closed)
        """
        try:
            from archimedes.chain.contracts import get_contract_loader

            onchain = await get_contract_loader().oracle_for(symbol).functions.price().call()
            if onchain and int(onchain) > 0:
                return int(onchain), True
            # On-chain read succeeded but reports no price yet.
            cached = self._last_pushed_price_int.get(symbol)
            if cached is not None:
                return cached, True
            # Confirmed absent: the read worked and there is genuinely no price.
            return None, True
        except Exception as e:
            logger.warning(f"Could not read on-chain reference price for {symbol}: {e}")
            cached = self._last_pushed_price_int.get(symbol)
            if cached is not None:
                return cached, True
            # Unobtainable: read threw and we have no cached fallback.
            return None, False

    async def _get_circle_public_key(self, session: aiohttp.ClientSession) -> str | None:
        """Fetch Circle's RSA public key (cached per instance)."""
        if self._circle_public_key:
            return self._circle_public_key
        try:
            async with session.get(
                f"{CIRCLE_API_BASE}/config/entity/publicKey",
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    self._circle_public_key = body["data"]["publicKey"]
                    return self._circle_public_key
                logger.error(f"Failed to fetch Circle public key: {resp.status}")
        except Exception:
            logger.exception("Error fetching Circle public key")
        return None

    def _fetch_yfinance(self, symbols: dict[str, str], timestamp: datetime) -> list[AssetPrice]:
        """Fetch equity prices via the market-data provider seam (#1218; sync —
        call via to_thread). Default provider is yfinance. ``source`` on the
        returned prices is the ACTIVE provider name, not a hardcoded
        ``"yfinance"``, so a vendor swap is visible on every downstream
        consumer of these prices (including the #775 cross-check).

        **This leg still stamps the POLL time, deliberately — on-chain
        behavior is unchanged here.** ``get_intraday_quotes_batch`` now
        returns ``(price, bar_ts)`` (widened for the paper-marks loop, which
        must store an upstream observation time), so the tuple is unpacked —
        but ``bar_ts`` is discarded on this path and ``timestamp`` (``now``,
        from ``fetch_prices``) is what lands on the ``AssetPrice``.

        That is a known, tracked honesty gap and NOT a silent one:
        ``_validate_for_push`` computes ``age_s`` from this same field, so on
        the yfinance leg the staleness gate compares now against now and
        cannot reject a stale bar (the Pyth cascade does carry a real
        observation time; this leg does not). Stamping ``bar_ts`` here would
        close it — and would ALSO stop every off-hours equity push, because a
        Friday-close bar is hours older than
        ``ORACLE_MAX_UPSTREAM_STALENESS_SECONDS``. That is a live-chain
        behavior change with weekend-freshness consequences (#1528), so it
        does not ride along in a paper-trading change: it is split out to
        ``dbrowneup/oracle-bar-time-stamp`` for its own review.
        ``test_stamps_the_poll_time_unchanged_by_the_widened_batch_seam``
        pins that this branch did not quietly flip it.
        """
        from archimedes.services.market_data_provider import get_provider, provider_name

        quotes = get_provider(seam="intraday").get_intraday_quotes_batch(symbols)
        source = provider_name("intraday")
        # Named-exclusion bookkeeping (#1710): a ticker the provider had no data
        # for is absent from `quotes` per the seam's per-item-skip contract. Record
        # WHY here so _log_push_exclusions can name it instead of the push cycle
        # dropping the symbol with nothing in the log tying it to a source gap.
        for synth_symbol, ticker in symbols.items():
            if synth_symbol not in quotes:
                self._source_miss_reasons[synth_symbol] = (
                    f"provider {source!r} returned no intraday observation for {ticker}"
                )
        return [
            AssetPrice(symbol=synth_symbol, price_usd=price, timestamp=timestamp, source=source)
            for synth_symbol, (price, _bar_ts) in quotes.items()
        ]

    async def _fetch_crypto(self, timestamp: datetime) -> list[AssetPrice]:
        """Fetch crypto prices through an ordered, NAMED source cascade (#1710).

        Order comes from ``ORACLE_CRYPTO_SOURCE`` (see ``_crypto_source_order``
        and the constant block's mode table). Default ``coingecko`` keeps
        CoinGecko as the primary — an unchanged happy path — and adds the
        ``MARKET_DATA_PROVIDER`` seam as the documented fallback, which is what
        puts this leg behind the vendor abstraction the market-data ADR claims
        the whole codebase already sits behind.

        Every returned price carries its TRUE ``source`` (``"coingecko"`` or the
        active provider name), so a downstream consumer — including the #775
        cross-check, which treats "same source" as "skip" — can still tell the
        vendors apart. Nothing is filled in from a stale cache and no default
        is invented: a symbol no leg could price is left OUT of the result with
        its reason recorded in ``_source_miss_reasons`` for
        ``_log_push_exclusions`` to name.
        """
        order = _crypto_source_order()
        results: list[AssetPrice] = []
        served: set[str] = set()
        reasons: dict[str, list[str]] = {}

        for leg in order:
            remaining = {s: cg_id for s, cg_id in CRYPTO_MAP.items() if s not in served}
            if not remaining:
                break
            if leg == "coingecko":
                got, why = await self._fetch_crypto_coingecko(remaining, timestamp)
            else:
                got, why = await self._fetch_crypto_provider(sorted(remaining), timestamp)
            results.extend(got)
            served.update(p.symbol for p in got)
            for symbol, msg in why.items():
                reasons.setdefault(symbol, []).append(msg)

        for symbol in CRYPTO_MAP:
            if symbol in served:
                continue
            self._source_miss_reasons[symbol] = (
                f"crypto sources exhausted (ORACLE_CRYPTO_SOURCE order: {'→'.join(order)}): "
                + " | ".join(reasons.get(symbol, ["no source attempted"]))
            )
        return results

    async def _fetch_crypto_coingecko(
        self, wanted: dict[str, str], timestamp: datetime
    ) -> tuple[list[AssetPrice], dict[str, str]]:
        """CoinGecko leg. Returns ``(prices, {symbol: named failure reason})``.

        Behavior for a symbol CoinGecko DOES serve is unchanged from the
        pre-#1710 implementation, down to the URL and the ``source`` string.
        What changed is that a failure now produces a reason string the caller
        can attribute to the symbol, instead of only a log line that the push
        loop never sees.
        """
        results: list[AssetPrice] = []
        reasons: dict[str, str] = {}
        try:
            async with aiohttp.ClientSession() as session:
                for symbol, cg_id in wanted.items():
                    try:
                        url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                price = data[cg_id]["usd"]
                                results.append(
                                    AssetPrice(
                                        symbol=symbol,
                                        price_usd=price,
                                        timestamp=timestamp,
                                        source="coingecko",
                                    )
                                )
                            else:
                                reasons[symbol] = f"coingecko HTTP {resp.status} for id {cg_id!r}"
                    except Exception as e:
                        logger.warning(f"Failed to fetch {symbol} from CoinGecko: {e}")
                        reasons[symbol] = f"coingecko fetch failed for id {cg_id!r}: {e}"
        except Exception as e:
            logger.warning(f"Crypto fetch error: {e}")
            for symbol, cg_id in wanted.items():
                reasons.setdefault(symbol, f"coingecko session error (id {cg_id!r}): {e}")
        return results, reasons

    async def _fetch_crypto_provider(
        self, wanted: list[str], timestamp: datetime
    ) -> tuple[list[AssetPrice], dict[str, str]]:
        """Market-data-provider leg (#1218 seam) for crypto. Returns
        ``(prices, {symbol: named failure reason})``.

        Uses the same batched intraday call the equity leg uses
        (``get_intraday_quotes_batch``) so the two legs share one vendor
        abstraction rather than two. Like ``_fetch_yfinance``, this stamps the
        POLL time rather than the vendor's bar time — the known, tracked
        honesty gap documented on that method (a bar-time change is split out
        to its own review because it would stop every off-hours push), and
        keeping the two legs identical here means this change cannot quietly
        alter what ``_validate_for_push``'s staleness gate compares.

        A provider that cannot serve intraday at all (``TiingoProvider`` raises
        ``NotImplementedError`` — the ADR's stated "live oracle push is not
        cutover-ready" consequence) is reported by name for every requested
        symbol rather than swallowed. Since #1798 the seam no longer routes
        such a vendor here in the first place (``_VENDOR_SEAMS``), so this is
        the belt-and-braces path, not the expected one.
        """
        from archimedes.services.market_data_provider import get_provider, provider_name

        results: list[AssetPrice] = []
        reasons: dict[str, str] = {}

        tickers = {s: CRYPTO_VENDOR_TICKERS[s] for s in wanted if s in CRYPTO_VENDOR_TICKERS}
        for symbol in wanted:
            if symbol not in CRYPTO_VENDOR_TICKERS:
                reasons[symbol] = "no vendor ticker mapped in CRYPTO_VENDOR_TICKERS — provider leg cannot ask for it"
        if not tickers:
            return results, reasons

        name = provider_name("intraday")
        try:
            quotes = await asyncio.to_thread(get_provider(seam="intraday").get_intraday_quotes_batch, tickers)
        except NotImplementedError as e:
            for symbol, ticker in tickers.items():
                reasons[symbol] = (
                    f"provider {name!r} does not implement intraday quotes for {ticker} "
                    f"(daily bars only — see docs/adr/market-data-sourcing.md): {e}"
                )
            logger.warning(
                "crypto provider leg unavailable: intraday-seam vendor %s cannot serve intraday quotes (%s)",
                name,
                e,
            )
            return results, reasons
        except Exception as e:
            for symbol, ticker in tickers.items():
                reasons[symbol] = f"provider {name!r} fetch failed for {ticker}: {e}"
            logger.warning("crypto provider leg failed (provider=%s): %s", name, e)
            return results, reasons

        for symbol, ticker in tickers.items():
            quote = quotes.get(symbol)
            if quote is None:
                reasons[symbol] = f"provider {name!r} returned no observation for {ticker}"
                continue
            price = quote[0] if isinstance(quote, tuple) else quote
            results.append(AssetPrice(symbol=symbol, price_usd=price, timestamp=timestamp, source=name))
        return results, reasons

    async def _fetch_yfinance_single(self, symbol: str) -> tuple[float, datetime] | None:
        """Fetch a single provider price + its bar timestamp (e.g. VIX, or the
        secondary-source cross-check's independent reading, #775) via the
        market-data provider seam (#1218). Default provider is yfinance —
        identical behavior to before this seam existed (the fetch logic
        itself moved to ``YFinanceProvider.get_intraday_quote``, unchanged;
        this method now just runs it in a thread).

        Returns ``(price, bar_ts)`` on success with ``bar_ts`` normalized to a
        tz-aware UTC ``datetime``, or ``None`` on any failure (empty data, missing
        column, exception).
        """
        from archimedes.services.market_data_provider import get_provider

        return await asyncio.to_thread(get_provider(seam="intraday").get_intraday_quote, symbol)

    def _fetch_sp500_moving_averages(self) -> dict[str, float]:
        """Fetch S&P 500 50-day and 200-day moving averages via the
        market-data provider seam (#1218) — daily bars, so this also benefits
        from the provider's ``asset_daily_bars`` Postgres cache.

        Seam: ``intraday``, not ``daily``, and deliberately (#1798). This read
        is one half of ``fetch_market_snapshot``'s single regime run (^VIX is
        the other), so routing it to a different vendor than the ^VIX quote
        would mix vendors inside one run — the thing the ADR forbids. ^GSPC is
        also an index ticker, which Tiingo does not serve at all."""
        try:
            from archimedes.services.market_data_provider import get_provider

            close = get_provider(seam="intraday").get_daily_close_batch({"^GSPC": "^GSPC"}, period="1y").get("^GSPC")
            if close is None or close.empty:
                return {}

            return {
                "ma50": float(close.rolling(50).mean().iloc[-1]),
                "ma200": float(close.rolling(200).mean().iloc[-1]),
            }
        except Exception as e:
            logger.warning(f"Failed to fetch S&P MA data: {e}")
            return {}
