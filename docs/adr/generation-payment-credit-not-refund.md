# ADR: An undelivered generation is repaid as a credit, never refunded

> **Audience:** Archimedes team
> **Status:** **Accepted** (implementation shipped; payment-stack owner's sequencing unchanged)
> **Date:** 2026-08-29
> **Owner:** Önder Akkaya (payment-stack reviewer of record: Dan Browne)
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** The x402 generation paywall settles a charge before the job is enqueued. When the job never delivers, does the payer get their money back, or something else?
> **Related:** [#1441](https://github.com/aprin-labs/archimedes/issues/1441), [`backend/archimedes/models/generation_credit.py`](../../backend/archimedes/models/generation_credit.py), [`backend/archimedes/services/generation_credits.py`](../../backend/archimedes/services/generation_credits.py), [`backend/archimedes/api/generate_routes.py`](../../backend/archimedes/api/generate_routes.py) (`_paywall_with_credit`), [#975](https://github.com/aprin-labs/archimedes/issues/975) (custody migration), [`architectural-principles.md`](../architectural-principles.md) § fail-soft.

## TL;DR

**A settled generation payment buys a durable *credit*, and a generation spends the credit.
A run that does not deliver hands the credit back, and the payer's next attempt spends it
instead of paying again.** Refunds were considered and rejected: we cannot execute one
today, and a refund promise we cannot keep is exactly the kind of claim this repo's first
rule forbids.

## Context

`POST /api/generate/start` settled the charge and enqueued the job afterwards, with the
premium-model entitlement gate sitting between the two. Every failure in that window — the
gate raising 402, the enqueue erroring — and every failure after it — worker crash, LLM
failure, a container roll mid-run, the payer cancelling — kept the money with nothing
delivered. Nothing released the charge, and nothing recorded that the payer was owed
anything.

Separately, the payment step took no idempotency key. x402 is not crash-retry-idempotent:
a client retrying after an ambiguous failure signs a **fresh** EIP-3009 authorization,
which settles as a second real payment.

`PAYMENTS_DRY_RUN` has held throughout, so no real value has ever moved on this path. That
is what makes this a design decision rather than an incident — and why it had to be settled
before the payment stack is flipped on, not after.

## Options considered

**1. Refund the charge.** The obvious answer, and the one the issue listed first.

Rejected on feasibility, not on principle. Settlement runs one way through Circle's
facilitator: we hand it a signed authorization and it moves the payer's USDC to the
recipient DCW. There is no reverse call. A refund is a **new outbound transfer** out of that
DCW, which needs the Circle signer on the money path — the surface #975's custody migration
is still open on, and the one thing this repo treats as highest-risk. Shipping a refund path
we could not actually execute would put a promise in the product that the code does not
keep.

**2. Credit the payer.** A settled payment creates a durable credit; a generation spends
one. Undelivered runs return it.

Chosen. It is local, needs no value to move, needs no signer, and is fully testable without
touching Circle. It also composes with a refund later: if #975 lands and outbound transfers
become safe, "redeem a credit as a refund" is a new operation on an existing ledger, not a
redesign.

**3. Reserve, then capture.** Authorize at request time and capture only on delivery — the
card-network model.

Rejected: x402/EIP-3009 has no authorize-and-capture split. The authorization *is* the
transfer. Building a reservation would mean holding signed authorizations server-side and
settling them later, which is strictly more custody risk than we have today, in the
direction #975 is trying to move away from.

## Decision

Credits, with the claim taken **before** the money moves.

The ledger is `generation_credits`, one row per logical charge:

```
pending ──settle ok──> available ──enqueue ok──> consumed
   │                        ▲                        │
   │                        └────job did not finish──┘
   └──settle failed/refused──> void
```

`pending` is claimed ahead of the settle, mirroring
[`marketplace/service.py`](../../backend/archimedes/marketplace/service.py)'s
`_claim_settlement_intent`. Claiming afterwards would leave the window — settle returns,
process dies before the write — wide open, and that window is precisely where a client
retry lands. The caller's `Idempotency-Key` is the logical key;
`uq_generation_credits_user_key` is what makes the claim atomic.

Three consequences worth stating explicitly, because they are choices rather than
mechanics:

- **A cancelled run returns its credit.** A cancelled generation produced no strategy.
  Charging for it would make the paywall a fee on trying rather than a price for delivery.
- **A job whose record has expired counts as undelivered.** The safe direction to be wrong
  in. The alternative silently keeps money for a run nobody can prove finished.
- **Credits do not expire.** One is owed until it is spent. An expiring credit would be a
  way of keeping money for undelivered work on a slower clock.

## Consequences

**Good.** Money taken is always money accounted for, provably and in one table. A payer
whose generation dies is never asked to pay twice. A retry cannot double-settle. None of it
needs a signer, so none of it is blocked on #975.

**The cost.** A payer holding an unspent credit has money with us they cannot get back in
cash — this is store credit, and it should be described that way rather than as a refund
wherever we describe it publicly.

**Fail-soft, applied asymmetrically and deliberately.** Before the settle, a ledger failure
is loud and stops the request: proceeding would take money with no protection behind it.
After the settle, a ledger failure is swallowed and logged at `error`: the payer's funds are
already gone, and raising would hand them a 500 *and* keep their money — strictly worse than
the inconsistency it would be reporting. Fail-soft is wrong for anything a claim depends on,
and right when the alternative harms the person the claim was meant to protect.

**Inert until the flip.** The whole mechanism hangs off `settles_real_value()`
(`payment_required() and not _payments_dry_run()`). Under flag-off or dry-run — production
today — no row is ever written.

## Ratification

The payment stack is Dan's lane and its flip sequencing (#1427→#1414→#1426→#1428) is
unchanged by this. What this ADR settles is the semantics that sequence turns on, so that
"what happens to the money when a job dies" has an answer written down before real value
moves rather than after.

**Open, and deliberately not decided here:** the public wording. There is no Terms page on
`main` — it arrives with [#1432](https://github.com/aprin-labs/archimedes/pull/1432) — so
whoever lands it must describe this as credit toward a future generation, not as a refund.
