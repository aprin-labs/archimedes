# SES suppression list — when to look, when to remove, when to leave it alone

> **status:** runbook
> **owner:** Dan Browne
> **updated:** 2026-09-01
> **superseded-by:** —

**Scope:** the AWS **account-level** SES suppression list — what puts an address on it, how
to see it, and the single, narrow circumstance in which an address comes off. The tool is
[`backend/archimedes/scripts/ses_suppression.py`](../../backend/archimedes/scripts/ses_suppression.py).

**Read this first if:** a user reports that verification or password-reset mail never
arrives and you need to know whether AWS is dropping it. **This script is the only way to
see that fact today.** The in-product signal — `GET /api/auth/verification-status`, which
will report `suppressed` for the signed-in caller's own address — is
[#1748](https://github.com/aprin-labs/archimedes/issues/1748) item 2 and is **not shipped
yet**; until it is, every reference to it below describes the state after it lands. The human
validation procedure for the mail flows themselves is
[`email-verification-validation.md`](email-verification-validation.md).

---

## 1. What the suppression list is, and why silence is the symptom

When a message hard-bounces or draws a spam complaint, SES adds the recipient to the
**account-level suppression list** automatically. From that moment:

- `SendEmail` to that address **succeeds**. It returns a `MessageId`. The message is then
  **dropped** and never leaves AWS.
- Nothing in the send path errors. Nothing in CloudWatch shows a failure.
- The recipient sees silence, and so does anyone reading our logs.

That is why this list is dangerous rather than merely annoying: **the failure mode is a
success response.** It is also why item 2 of the same issue will have the product ask SES
directly rather than trust a successful send: `GET /api/auth/verification-status`
([#1748](https://github.com/aprin-labs/archimedes/issues/1748) item 2, **not shipped yet**)
calls `GetSuppressedDestination` for the signed-in caller's own address and reports
`suppressed` instead of the eternal `200 {status:true}` the product answers today.

**How ours got populated.** Dogfooding signup with throwaway addresses. Every bounce from a
fake address is a real, correct suppression entry — AWS behaved exactly as designed. The
residue is the problem: a **real** address that was once typo'd, temporarily undeliverable,
or used as a probe stays blocked forever, and the person retrying it gets nothing.

## 2. Look before you touch

Read-only. Safe at any time, including during an incident.

```bash
# Everything on the list, with the reason and the date AWS added it.
PYTHONPATH=backend python -m archimedes.scripts.ses_suppression list

# Only hard bounces, machine-readable.
PYTHONPATH=backend python -m archimedes.scripts.ses_suppression list --reason BOUNCE --json

# One address.
PYTHONPATH=backend python -m archimedes.scripts.ses_suppression check dan@example.com
```

Exit codes: `0` found / listed, `3` the address is **not** on the list, `2` the AWS call
itself failed. A failed call is never printed as "not suppressed" — that distinction is the
whole point, and it is pinned by `backend/tests/scripts/test_ses_suppression.py`.

Credentials come from the ambient AWS profile or role. Run these commands under an operator
profile that holds `ses:ListSuppressedDestinations` and `ses:GetSuppressedDestination` — the
read path needs both.

The app's own ECS task role is a **separate** identity, and it is narrower in both states:

- **Today**, the only SES statement on it is `ses:SendEmail` / `ses:SendRawEmail`
  ([`infra/ecs.tf`](../../infra/ecs.tf)). It can send; it can neither read the suppression
  list nor delete from it.
- **After the `terraform apply`** that adds the `ses:GetSuppressedDestination` grant for
  `GET /api/auth/verification-status` ([#1748](https://github.com/aprin-labs/archimedes/issues/1748)
  item 2), it can look up **one** address at a time — still no list, and still no delete.

`ses:DeleteSuppressedDestination` is granted to the task role in neither state, by design:
removal is an operator action (§ 4), never something the running app can do.

## 3. When to remove — the only qualifying case

Remove an address **only** when all of these are true:

1. **A human owns it and has confirmed it is real and reachable.** Not "looks real". Not
   "is on our team". Confirmed by its owner, in writing, in the ticket.
2. **The cause is understood and is over.** A typo that has been corrected; a mailbox that
   was full and is not now; a probe address that has become a real one. If you cannot say
   *why* it bounced, you cannot say it will not bounce again.
3. **It is one address.** One person, one entry, one command.

**Never bulk-clear.** There is deliberately no flag that empties the list, and adding one is
not a convenience — it is a reputation decision. Clearing the list re-sends to every address
that ever hard-bounced; AWS reads that as a sender who does not honour bounces, our bounce
rate climbs, and delivery degrades **for every real user**, including the ones who never had
a problem. A high enough bounce rate ends in a sending pause on the whole account.

**Do not remove** an address suppressed for `COMPLAINT` unless its owner has explicitly asked
to receive our mail again. A complaint is a person pressing "this is spam". Re-adding them is
worse than leaving them blocked.

**Do not remove** dogfood/probe addresses at all. They bounced because they are fake. Leaving
them on the list costs nothing and protects the bounce rate. If a probe address needs to
work, use a real mailbox instead of clearing the entry.

## 4. Removing one address

`remove` is a **dry run by default**. It looks the address up, prints exactly what it would
delete, and changes nothing:

```bash
PYTHONPATH=backend python -m archimedes.scripts.ses_suppression remove dan@example.com
```

```
DRY RUN — would remove dan@example.com (reason BOUNCE, since 2026-08-30T10:00:00+00:00)
Nothing was changed. Re-run with --apply only if the address's owner has confirmed it is
real and reachable; see docs/runbooks/ses-suppression.md.
```

Only after § 3 is satisfied:

```bash
PYTHONPATH=backend python -m archimedes.scripts.ses_suppression remove dan@example.com --apply
```

```
removed dan@example.com (reason BOUNCE, since 2026-08-30T10:00:00+00:00)
```

`--apply` needs `ses:DeleteSuppressedDestination`. If the entry disappeared between the
lookup and the delete (someone else removed it), the script exits `3` and says the address is
not on the list — it does **not** claim a removal it did not perform.

### 4a. Removing from this list is only HALF the unblock (#1804)

Since [#1804](https://github.com/aprin-labs/archimedes/issues/1804), a permanent bounce is
also recorded on the user row (`auth_users.emailBouncedAt`), and **that** is what makes
signup and the signed-in resend refuse the address with `EMAIL_ADDRESS_BOUNCED`. It is a
separate fact in a separate place: taking the address off the AWS list above does not clear
it, so the person still cannot sign up.

Run both, in either order:

```bash
PYTHONPATH=backend python -m archimedes.scripts.ses_events clear dan@example.com --apply
```

Full procedure, including how to tell which half is blocking:
[`ses-bounce-signal.md`](ses-bounce-signal.md) § 5.

## 5. Verify, then watch

1. Re-run `check <address>` — it should exit `3` ("not on the suppression list").
   Then confirm the user row carries no stamp either (§ 4a) — one without the other leaves
   the address blocked.
2. Have the owner request a verification email and confirm it arrives.
3. Once #1748 item 2 has shipped, confirm the account's own
   `GET /api/auth/verification-status` reports `sent`, not `suppressed`. Until then, step 2
   is the confirmation.
4. **If it bounces again, leave it suppressed.** A second removal for the same address is a
   sign that step 2 of § 3 was not actually satisfied.

## 6. The way back

There is no undo, and none is needed: removal is not destructive. If the address bounces
again SES re-suppresses it automatically, which is the system working. The irreversible
mistake in this area is the bulk clear in § 3 — a damaged sender reputation takes weeks of
clean sending to repair, and no command reverses it.

## 7. Related

- [`ses-bounce-signal.md`](ses-bounce-signal.md) — the push half (#1804): the SES
  configuration set, the SNS → SQS event queue, `ses_events drain`, and the
  `auth_users.emailBouncedAt` stamp that the refusal reads. That page owns the two-command
  unblock procedure this one links to from § 4a.
- [`email-verification-validation.md`](email-verification-validation.md) — the human,
  real-inbox validation procedure for verification and reset mail, and its delivery-state
  triage table, which is where a `suppressed` finding comes from.
- [`../api/auth-and-accounts.md`](../api/auth-and-accounts.md) — the Better Auth sidecar's
  HTTP contract. It will gain `GET /api/auth/verification-status`, the in-product surface for
  the same fact, when #1748 item 2 ships; it does not document that route today.
