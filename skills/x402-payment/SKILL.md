---
name: x402-payment
description: The x402 / Circle Gateway micropayment flow Archimedes actually implements for copy-trading fee charges — an in-process 402-requirements → sign → verify → settle protocol over Circle Developer-Controlled Wallets, not a public HTTP payment endpoint. Covers PAYMENTS_DRY_RUN semantics and testnet posture, grounded in backend/archimedes/marketplace/payments.py.
triggers:
  - "how does Archimedes charge subscribers for copy-trading"
  - working on backend/archimedes/marketplace/payments.py or settlement.py
  - "what does PAYMENTS_DRY_RUN do"
  - x402 / Circle Gateway / circlekit integration questions
  - "is this on mainnet or testnet, for the marketplace money path"
---

# x402 / Circle Gateway payment flow

**Read this first — the shape is not what "x402" usually means.** In the canonical
x402 pattern, a client hits an HTTP endpoint, gets back a real `402 Payment
Required` response, and retries with a payment header. Archimedes' implementation
is the *same three-step protocol* (requirements → signed header → verify/settle)
but run **entirely in-process, with no HTTP round-trip between the two parties**:

> "Flow per charge (all in-process, no HTTP between publisher/subscriber): 1.
> `middleware.require(price, path)` -> 402 requirements (publisher side) 2.
> `create_payment_header(signer, reqs)` -> EIP-712 signature with the subscriber's
> ephemeral key (subscriber side, same process) 3. `middleware.settle(header,
> price)` -> Circle facilitator verifies and records the micropayment."
> — [`marketplace/payments.py`](../../backend/archimedes/marketplace/payments.py):7-13

This exists to charge a copy-trading **subscriber's** Circle wallet a flat fee
per pipeline step, on the **publisher's** behalf, once per tick of the in-process
marketplace engine — not to gate a public API route. If you came here looking for
"how do I pay to call an Archimedes endpoint," that's not what this is; there is
no paid public endpoint today.

## Who calls what

```
MarketService (backend/archimedes/marketplace/service.py)
  └─ per tick, per subscriber, per pipeline step:
       _charge_one()  ──────────────────────────────  service.py:825-909
         ├─ dry-run short-circuit                       service.py:833-834
         ├─ idempotency claim (SettlementIntent row)     service.py:842-859
         ├─ spend-cap reservation                        service.py:861-885
         └─ payments.charge(...)  ─────────────────────  payments.py:85-155
              ├─ 1. middleware.require()  → 402 reqs      payments.py:118-126
              ├─ 2. create_payment_header() (EIP-712)     payments.py:128-136
              └─ 3. middleware.verify() + middleware.settle()  payments.py:138-151
```

`payments.py`'s module docstring claims it is the **only** file that imports
`circlekit` (payments.py:1-5: "This module is the ONLY place circlekit is
imported... keeping the import surface here gives API drift a one-file blast
radius") — **that claim is now stale**: `settlement.py` also imports directly
from `circlekit` (`circlekit.client.GatewayClient`,
`circlekit.wallets.CircleTxExecutor`/`CircleWalletSigner`, settlement.py:21-22)
for its own sweep signers/executors. If you need to touch the Circle SDK
surface, check both files, not just this one.

## The three steps, with line numbers

1. **Requirements (`402`).** `get_gateway_middleware(seller_address)`
   (payments.py:52-72) returns a cached `GatewayMiddleware` per **creator's**
   Circle wallet address — the zero address is unconditionally refused
   (payments.py:62-63). `middleware.require(price, path)` (payments.py:118)
   builds the 402 payment-requirements object; `path` here is a **logical**
   resource id (`/charge/{strategy_id}/{tick_id}/{sub_id}[/{step}]`,
   payments.py:116-117) — no real HTTP route exists at that path, it's purely an
   identifier the requirements object carries.
2. **Sign.** The **subscriber's** side signs an EIP-712 payment header with
   their Circle-managed wallet key via `create_payment_header`
   (payments.py:128-136), run through `asyncio.to_thread` because the signer
   and the header call both make blocking HTTPS calls to Circle
   (payments.py:129-130 comment). `_get_signer` caches one `CircleWalletSigner`
   per `wallet_id` (payments.py:42-49) so constructing a signer — which
   re-initializes the Circle client — doesn't happen on every tick.
3. **Verify + settle.** `middleware.verify(header, price)` (payments.py:139)
   checks the signed header against Circle's facilitator; on failure the charge
   returns `False` with the `invalid_reason` logged (payments.py:140-147).
   `middleware.settle(header, price)` (payments.py:149) then **records** the
   micropayment — Circle batches and settles the underlying value on-chain
   later; **this module does not run any settlement logic of its own** (that's
   `settlement.py`, see below).

`charge()` (payments.py:85-155) **never raises** — every failure path is caught,
logged, and returned as `False` (payments.py:153-155 + the docstring at
85-99), so the caller's existing "unpaid subscriber → halt" path is the single
place that has to reason about payment failure.

**Zero-amount ticks are free, not zero-charged**: if `fee_to_price(...)` computes
`"$0.000000"`, `charge()` returns `True` immediately without touching the
middleware at all (payments.py:110-112) — nothing to verify/settle when nothing
is owed.

## `fee_to_price` — the money math

```python
def fee_to_price(action_count: int, flat_fee_raw: int) -> str:
    ...
```
(payments.py:75-82). Converts `action_count × flat_fee_raw` (raw 6-decimal USDC
units) into the `"$X.XXXXXX"` string `circlekit` expects, using `Decimal` —
**never floats** (payments.py:77 docstring). Rejects negative inputs
(payments.py:78-79). `flat_fee_raw` itself is `FLAT_FEE_PER_ACTION`, env-tunable,
default `100` raw units = **$0.0001 per action**
([`marketplace/service.py`](../../backend/archimedes/marketplace/service.py):53).

## What actually gets charged, and when

The charge granularity is one flat fee per named pipeline step
(`TickStep` enum, [`marketplace/tick_registry.py`](../../backend/archimedes/marketplace/tick_registry.py):13-30)
— `load_strategy`, `evaluate_signals`, `aggregate_weights`, … through the
publisher-pipeline boundaries, plus a per-subscriber `rebalance` step whose
`action_count` scales with the number of trades actually generated
(tick_registry.py:28-29). `_charge_one` in `service.py` wraps `payments.charge`
with three things `payments.py` itself does **not** do:

- **Idempotency.** x402 payment headers are **not** crash-retry-idempotent — a
  retry signs a fresh EIP-3009 nonce and settles as a *second* payment. A
  `SettlementIntent` row is claimed before calling `payments.charge`
  (service.py:842-859) so a crash/retry short-circuits to "already settled"
  instead of double-charging.
- **Per-wallet 24h spend cap**, reserved atomically immediately before the
  charge call to avoid a check-then-record TOCTOU race across concurrent ticks
  (service.py:861-885, referencing issue #1099).
- **Treating an unexpected raise as a failed charge anyway** (service.py:887-908)
  — `payments.charge`'s "never raises" contract is documented, not blindly
  trusted; a raise still releases the spend-cap reservation.

## Testnet posture

- Default chain: `DEFAULT_GATEWAY_CHAIN = "arcTestnet"`
  ([`marketplace/config.py`](../../backend/archimedes/marketplace/config.py):7),
  overridable via the `GATEWAY_CHAIN` env var (payments.py:65, settlement.py:34).
- `.env.example`:194 sets `CIRCLE_BLOCKCHAIN=ARC-TESTNET` for the
  subscriber/publisher wallet-provisioning path — this whole seam is wired for
  Arc **testnet**, not mainnet, on `main` today.
- On-chain settlement of the *aggregated* fee pool is a separate, three-stage
  sweep in [`marketplace/settlement.py`](../../backend/archimedes/marketplace/settlement.py):
  Stage A (Gateway balance → agent wallet, threshold `SWEEP_WITHDRAW_THRESHOLD_USDC`,
  default 10 USDC, settlement.py:29-36, 92-126), Stage B (wallet USDC →
  `PaymentSplitter.depositToPool`, min 1 USDC, settlement.py:130-189), Stage C
  (`PaymentSplitter.withdraw` — creator/platform payout on demand,
  settlement.py:193-222). None of the three stages ever raises out of
  `sweep_publisher` (settlement.py:12-13) — same fail-soft posture as `charge()`.

## `PAYMENTS_DRY_RUN` — semantics

**Default is dry (safe).** `main.py`:212 —
`payments_dry_run = os.getenv("PAYMENTS_DRY_RUN", "true").lower() in ("1", "true", "yes")`
— an out-of-the-box deploy does not move real money. `.env.example`:195-198 labels
it explicitly: "FAIL-SAFE money switch. Defaults to true (no real charges)."

Two independent money switches must be turned on **together and deliberately**
(`main.py`:206-211 comment): `PAYMENTS_DRY_RUN=false` *and* whatever gates real
trade mirroring (`PAPER_TRADING`, also defaulting to `true`). The comment is
explicit about why: the two used to default asymmetrically (`PAYMENTS_DRY_RUN`
defaulting false while `PAPER_TRADING` defaulted true), so an out-of-the-box
deploy mirrored *no* real trades yet still charged *real* USDC — "the worst
possible asymmetry." Both now default to the safe side.

**Where the flag is enforced — read the actual gates, not `payments.py`:**
`payments.py`'s `charge()` function has **no dry-run branch of its own** — it
always runs the full sign/verify/settle sequence if called. The gate lives one
layer up, at every call site:

- `MarketService._charge_one`: `if self.payments_dry_run: return True, None`
  **before** `payments.charge` is ever invoked (service.py:833-834) — dry-run
  ticks are treated as "paid" without touching Circle at all.
- `SettlementSweeper.sweep_publisher` / `withdraw_publisher` /
  `withdraw_subscriber`: each independently short-circuits on
  `self._payments_dry_run` at the **top** of the method
  (settlement.py:75-79, 199-205, 240-242) — the constructor comment explains
  why the check is duplicated at every fund-moving method rather than checked
  once by a caller: "Gating lives HERE (not only at call sites) so a future
  caller can't forget it — the manual withdraw endpoint (M1') did exactly that,
  bypassing PAYMENTS_DRY_RUN on a real on-chain path" (settlement.py:48-51).
- `/api/marketplace/*` manual-withdraw route: also fails soft when dry-run is on
  (`api/marketplace_routes.py`:750-755, returning
  `{"status": "dry_run_noop", ...}` rather than attempting a real settlement).

**Practical implication for anyone testing this flow:** with `PAYMENTS_DRY_RUN`
unset (or `true`), every subscriber charge in the marketplace tick loop reports
success without a single call into `circlekit`, and every sweep/withdraw is a
logged no-op. To exercise the real Circle Gateway path — even on testnet — you
must explicitly set `PAYMENTS_DRY_RUN=false` and have real `CIRCLE_API_KEY` /
`CIRCLE_ENTITY_SECRET` configured (`.env.example`:137-147, 192-198).

## Custody model (read before assuming "non-custodial")

The copy-trading fee flow runs through **Circle Developer-Controlled Wallets**
(DCWs) that the platform, not the subscriber, controls the entity secret for —
this fee seam is **custodial, interim**, by explicit team decision (2026-07,
"DCW fees = custodial-INTERIM"), separate from the non-custodial ERC-4626 vault
architecture that holds actual portfolio funds. `withdraw_subscriber`
(settlement.py:224-267) is the subscriber's exit path — it sweeps any remaining
prepaid-fee balance out of the custodial DCW back to the subscriber's own SIWE
wallet on unsubscribe. Don't describe this fee mechanism as non-custodial; it
isn't, today.

## Verify (re-run these before trusting this document)

```bash
# circlekit's import surface — expect payments.py AND settlement.py (the
# module docstring's "ONLY place" claim is stale; see the note above):
grep -rln "^from circlekit\|^import circlekit" backend/archimedes/

# PAYMENTS_DRY_RUN defaults to true, and where each fund-moving method gates on it:
grep -n 'PAYMENTS_DRY_RUN' backend/archimedes/main.py
grep -n 'self\._payments_dry_run\|self\.payments_dry_run' \
  backend/archimedes/marketplace/settlement.py backend/archimedes/marketplace/service.py

# Testnet chain default:
grep -n 'DEFAULT_GATEWAY_CHAIN' backend/archimedes/marketplace/config.py

# charge() never raises:
sed -n '85,155p' backend/archimedes/marketplace/payments.py
```

## What this skill deliberately does not cover

- The vault/portfolio contracts (non-custodial by design) — a different money
  seam entirely; see `docs/specs/ecosystem-design-spec.md` § 3.2.
- Strategy verdict fields (DSR/PBO/passport) — see `skills/strategy-passport/SKILL.md`.
- The public `/api/marketplace/*` HTTP routes (publish/subscribe/withdraw) in
  `api/marketplace_routes.py` — those are the human-facing onboarding surface
  around this engine, not the charge protocol itself; skim them separately if
  you need the subscribe/publish request shapes. `POST /publish` and `POST
  /subscribe` answer `404` while the server's `FEATURE_ROADMAP_SURFACES` flag
  is off, its default (#1432): marketplace is roadmap, not shipped.
