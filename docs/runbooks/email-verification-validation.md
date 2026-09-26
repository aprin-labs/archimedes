# Email verification and password reset — live validation before the enforcement flip

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-01
> **superseded-by:** —

**Scope:** proving, with a real inbox, that the two transactional mail flows Better Auth
owns actually work end to end — signup verification and password reset — *before*
[`EMAIL_VERIFICATION_ENFORCED`](../operations/feature-flag-fliplist.md) (row 3) is flipped
to `"true"`. Also covers the SES sandbox → production-access transition and how to roll the
flip back.

**Not in scope:** the OAuth (Google/GitHub) sign-in paths, account linking, and the
change-email flow. Those are covered by [`account-authentication.md`](../account-authentication.md)
and by `auth/test/auth.test.js`, and none of them is gated by the enforcement flag —
see § 6.

**Read this first if:** SES production access has been granted and you are deciding whether
to flip the flag; or verification / reset mail is reported not arriving.

**Nothing in this runbook is automated, and nothing in it is safe to hand to an agent.**
Every step is a human with a browser and a mailbox. The code-truth half is already
automated and runs in CI (`cd auth && npm test`); this document exists for the half a test
suite structurally cannot reach — that a message left AWS, survived the receiving provider's
spam filter, and that the link inside it works when a human clicks it.

---

## 1. State of the world before you start

| Thing | Where it is set | Value today |
|---|---|---|
| Mailer implementation | [`auth/mailer.js`](../../auth/mailer.js) | `ses` in deployed environments, `console` everywhere else (the default, so a missing env var degrades to a visible log line, not a silent failure) |
| SES send path | AWS SDK v3, `SESv2Client` / `SendEmailCommand` — **not SMTP**, no credentials in the image | credentials come from the ECS task role |
| From address | `EMAIL_SENDER` in [`infra/ecs.tf`](../../infra/ecs.tf) | `no-reply@archimedes-arc.com` |
| SES identity | AWS console (not Terraform-managed) | the **domain** identity `archimedes-arc.com` |
| IAM | `ecs_task_ses_send` in `infra/ecs.tf` | `ses:SendEmail` + `ses:SendRawEmail`, scoped to that one identity ARN |
| SPF / DKIM / DMARC | [`infra/dns_email.tf`](../../infra/dns_email.tf) | SPF `v=spf1 include:amazonses.com -all`; three DKIM CNAMEs; DMARC `p=none` with `rua` + `fo=1` |
| Verification mail | `emailVerification.sendOnSignUp` in [`auth/auth.js`](../../auth/auth.js) | **sent on every signup**, whatever the enforcement flag says |
| Verification link lifetime | `emailVerification.expiresIn` | **1 hour** (pinned explicitly since the audit; it used to be an inherited library default) |
| Reset link lifetime | `emailAndPassword.resetPasswordTokenExpiresIn` | **1 hour** (same) |
| Sessions on reset | `revokeSessionsOnPasswordReset` | `true` — every existing session dies |
| Enforcement | `EMAIL_VERIFICATION_ENFORCED` in `infra/ecs.tf` (auth container) | `"false"` |
| Delivery feedback | `GET /api/auth/verification-status` ([`auth/server.js`](../../auth/server.js)) | live since #1748 — reports `sent` / `suppressed` / `rate_limited` / `failed` / `unknown` for the SIGNED-IN caller's own address, from `auth_email_deliveries` plus a SESv2 suppression lookup |
| Logs | CloudWatch log group `/archimedes/app`, stream prefix `auth` | 90-day retention |

Two properties worth holding in mind for the whole of this runbook, because they change
what "worked" looks like:

- **The verification link is a bearer sign-in credential, once.** `autoSignInAfterVerification`
  is on, so whoever opens the URL first gets a live 7-day session as that account, without a
  password. Opening it a second time verifies nothing further and mints no session
  (`auth/test/email-flows.test.js` pins both halves). Treat a verification URL like a
  password for its 1-hour life: do not paste one into a ticket, a Discord message, or a
  screenshot.
- **A verification token is a stateless JWT; a reset token is a database row.** The reset
  token is consumed on first use and its identifier is stored hashed, so it is single-use
  *and* revocable. The verification token is neither — nothing can invalidate it early, only
  its 1-hour expiry closes the window.

---

## 2. Pre-flight — do these before you send anything

All read-only. Run from a shell with prod AWS credentials.

```bash
# 1. Is the account still in the SES sandbox? This is the whole precondition.
aws sesv2 get-account --region us-east-1 \
  --query '{Sandbox:ProductionAccessEnabled,SendingEnabled:SendingEnabled,Quota:SendQuota}'
#   ProductionAccessEnabled: false = SANDBOX. true = production access granted.

# 2. Is the domain identity verified, and is DKIM signing?
aws sesv2 get-email-identity --region us-east-1 --email-identity archimedes-arc.com \
  --query '{Verified:VerifiedForSendingStatus,DkimStatus:DkimAttributes.Status,DkimSigning:DkimAttributes.SigningEnabled}'
#   Expect Verified: true, DkimStatus: SUCCESS, DkimSigning: true.

# 3. Anyone on the suppression list? A suppressed address silently never receives mail —
#    this is the single most common "verification email doesn't work" false alarm.
aws sesv2 list-suppressed-destinations --region us-east-1 --query 'SuppressedDestinationSummaries[].{Email:EmailAddress,Reason:Reason}'

# 4. Public DNS agrees with infra/dns_email.tf.
dig +short TXT archimedes-arc.com @8.8.8.8         # expect the SPF string
dig +short TXT _dmarc.archimedes-arc.com @8.8.8.8  # expect v=DMARC1; p=none; rua=...
```

Then the log side — the auth service is loud about the two failure modes that matter, and
both are greppable:

```bash
# Has any send failed since the last deploy?
aws logs filter-log-events --region us-east-1 --log-group-name /archimedes/app \
  --start-time $(( ($(date +%s) - 86400) * 1000 )) \
  --filter-pattern '"email send failed"' --query 'events[].message'

# Findings check (see § 7, EV-1). EV-1 is FIXED (#1691) — nginx sets X-Client-IP and
# auth.js resolves the rate key from it. This line must therefore return NOTHING dated
# after that deploy; if it reappears, the header stopped arriving and every caller in the
# world is back to sharing one bucket per path.
aws logs filter-log-events --region us-east-1 --log-group-name /archimedes/app \
  --start-time $(( ($(date +%s) - 604800) * 1000 )) \
  --filter-pattern '"Rate limiting could not determine a client IP"' --query 'events[].message'
```

**Finally, the number that decides whether the flip is safe at all** — how many existing
accounts the flip would lock out. Aurora is only reachable through the SSM jump host
(`archimedes-runner`); see [`operations.md`](operations.md) for the tunnel.

```sql
SELECT "emailVerified", count(*) FROM auth_users GROUP BY 1;
```

An account row with `emailVerified = false` **and no OAuth account row** is a person who
will be refused sign-in the moment the flag flips, with no session of their own to fix it
from. Write the number down; it is the first row of the evidence table in § 8.

---

## 3. Part A — verification, on the local stack first

Do this before touching anything deployed. It costs nothing, sends no mail, and catches
copy/wiring regressions with a five-minute loop.

```bash
cp .env.example .env          # generate a real BETTER_AUTH_SECRET
docker compose up -d --build  # nginx on http://localhost:8080
```

Compose passes no `EMAIL_MAILER`, so `auth/mailer.js` takes its `console` default and every
message — subject, body, and the full link — is printed to the container log.

1. Sign up at <http://localhost:8080/sign-up> with any address.
2. `docker compose logs auth | grep -A6 'mailer:console'` — you should see one message,
   subject **`Verify your Archimedes account`**, containing a
   `…/api/auth/verify-email?token=…` URL.
3. Open that URL in a **fresh private window** (no session). Expected: you land signed in.
   That is `autoSignInAfterVerification`, not a bug.
4. Open the same URL again. Expected: no new session, no error page.
5. In Account Settings, the Profile block now reads **`Email verified ✓`** instead of
   offering a *Send verification email* button.

**Rehearsing enforcement locally.** `EMAIL_VERIFICATION_ENFORCED` is not plumbed through
`docker-compose.yml`, so the local stack can never refuse an unverified sign-in as written.
To rehearse it, add an override file at the repo root — compose loads it automatically —
and delete it afterwards. It is untracked but **not gitignored**, so do not commit it:

```yaml
# docker-compose.override.yml  (temporary, delete when done)
services:
  auth:
    environment:
      EMAIL_VERIFICATION_ENFORCED: "true"
```

`docker compose up -d auth`, then sign up as a new user and try to sign in without opening
the link. Expected: **HTTP 403**, the message names email verification, and a
**Resend verification email** control appears next to the error. Press it once and confirm
exactly one new message in the log.

---

## 4. Part B — verification against production, while still in the sandbox

**In the sandbox, SES will only deliver to an address you have individually verified as a
destination identity.** This is the entire reason enforcement is still off, and it is also
what makes a safe pre-flip rehearsal possible: you can prove the real SES path end to end
using an address you control, with zero risk to anyone else.

1. Verify your test address as an identity (one-time, and it sends *that* address an AWS
   confirmation mail you must click):
   ```bash
   aws sesv2 create-email-identity --region us-east-1 --email-identity you+archtest@example.com
   ```
   Use a **plus-addressed alias of a mailbox you own**, not a colleague's address and not a
   disposable-mail service. Gmail, Outlook/Microsoft 365, and one non-Google/non-Microsoft
   provider (Fastmail, Proton, an ISP account) are the three worth covering — they filter
   very differently.
2. Sign up at <https://archimedes-arc.com/sign-up> with that address.
3. **Start a stopwatch.** Time to inbox is a number worth having: it is the difference
   between "mail is broken" and "mail is slow" the next time someone reports this.
4. Check the inbox **and the spam folder**. If it landed in spam, that is a finding, not a
   pass — record which provider, and see § 9.
5. Open the link. Expected: signed in, and Account Settings reads `Email verified ✓`.
6. Confirm the send is visible from the AWS side:
   ```bash
   aws cloudwatch get-metric-statistics --region us-east-1 \
     --namespace AWS/SES --metric-name Send --statistics Sum \
     --start-time $(date -u -v-1H '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '1 hour ago' '+%Y-%m-%dT%H:%M:%SZ') \
     --end-time $(date -u '+%Y-%m-%dT%H:%M:%SZ') --period 3600
   ```
7. **Expiry, the half nobody checks.** Sign up a *second* test account, do not open its
   link, wait **over an hour**, then open it. Expected: an error naming an expired token,
   and the account still unverified. (The hermetic equivalent already runs in CI — this
   step is confirming the deployed `BETTER_AUTH_SECRET` and clock produce the same result.)

---

## 5. Part C — password-reset rehearsal

The reset flow has never been exercised against a real inbox. Run it on the **same** test
account from Part A/B, and run it *deliberately* from a second signed-in session so you can
watch the revocation happen.

1. Sign in on **two** devices/browsers (call them **X** and **Y**) with the test account.
2. On **X**, sign out, then click **Forgot password?** on the sign-in screen and submit the
   address.
3. Expected on screen, immediately and regardless of whether the address exists: a neutral
   "if this address has an account, check your mail" message. **If the UI ever tells you
   whether the address exists, stop — that is an account-enumeration regression** and
   `auth/test/auth.test.js` has three tests that should have caught it.
4. Check the inbox for subject **`Reset your Archimedes password`**. The link points at
   `/api/auth/reset-password/<token>?callbackURL=…`, which bounces you to the public
   `/reset-password` screen with the token in the query string.
5. Set a new password (12–128 characters; the checklist on screen is live).
6. Verify **all four** of these, in order — this is the actual acceptance test:
   - the new password signs in;
   - the **old** password does not;
   - **session Y is dead** — refresh it and confirm it has been signed out
     (`revokeSessionsOnPasswordReset`); this is the property that makes a reset useful
     after a credential compromise, and nothing else in the product enforces it;
   - **the same reset link, opened a second time, is refused.** Do this last, with the
     browser back button or by re-pasting the URL. Expected: an invalid-token error and no
     password change.
7. Wait an hour and try a *fresh, unused* reset link. Expected: expired-token error.
8. **The trap.** If you are running this with enforcement on, note that completing a reset
   does **not** mark the address verified (finding EV-4, § 7). An unverified account that
   successfully resets its password is still refused at sign-in. Confirm the behaviour
   matches that expectation rather than assuming the reset failed.

---

## 6. Part D — the flip, and getting back

Preconditions, all of them:

- § 2 step 1 returns `ProductionAccessEnabled: true`.
- Parts A, B and C are all recorded as passing in § 8.
- The `emailVerified = false` count from § 2 is known and you have decided what happens to
  those accounts (see below).
- A configuration set with a bounce/complaint destination exists — see § 9, finding EV-5.

Mechanics: `EMAIL_VERIFICATION_ENFORCED` lives in the auth container's `environment` block
in `infra/ecs.tf` (around line 716). It is a **task-definition change plus `terraform
apply`** — no image rebuild, no code change. Roll back the same way; there is no state to
unwind, because the flag only decides whether `/sign-in/email` refuses an unverified
account. Nothing it does is destructive and no data changes shape.

**What the flip does and does not touch:**

| Path | After the flip |
|---|---|
| `POST /api/auth/sign-in/email` for an unverified account | **403 `EMAIL_NOT_VERIFIED`** — this is the only thing the flag changes |
| Signup | unchanged; mail was already being sent on every signup |
| Google / GitHub sign-in | **unchanged.** The library reads `requireEmailVerification` in exactly one place, the credential sign-in handler; the OAuth callback never consults it (source-pinned in `auth/test/email-flows.test.js`) |
| An already-signed-in session | **unchanged** — the gate is at sign-in, not on every request. Existing sessions live out their 7 days |
| Password reset | still works for unverified accounts; still does not verify them (EV-4) |
| The refused user's escape route | the **Resend verification email** control on the sign-in error, and the *Send verification email* button in Account Settings |

**Blast radius, stated plainly:** every account with `emailVerified = false` and no OAuth
credential is locked out at its next sign-in until it opens a fresh verification mail. If
that count is non-trivial, send the batch a heads-up *before* the apply, not after.

Rollback signal: set the value back to `"false"` and `terraform apply` if the auth container
starts logging `verification email send failed` at any volume, if support reports pile up,
or if the SES complaint rate moves (§ 9).

---

## 7. Findings that gate this flip

From the 2026-08-31 code-truth audit. The first two are the ones to close before flipping.

| ID | Finding | Where | Status |
|---|---|---|---|
| **EV-1** | Behind CloudFront → ALB → nginx the `X-Forwarded-For` header is multi-hop, and Better Auth trusts a forwarded header only when it carries exactly one value. No client IP resolved, so **every rate-limit bucket was global per-path**, not per-IP: three signups from anywhere exhausted `/sign-up/email` for the whole internet for ten minutes, and `/request-password-reset` + `/send-verification-email` were 3-per-minute *globally* — exactly the traffic this flip creates. | `auth/auth.js` set no `advanced.ipAddress`; `nginx/nginx.conf` appends `$proxy_add_x_forwarded_for` | **fixed — [#1691](https://github.com/aprin-labs/archimedes/issues/1691).** nginx now SETS `X-Client-IP` from its realip-resolved `$remote_addr` and `auth.js` sets `advanced.ipAddress.ipAddressHeaders: ['x-client-ip']`; the mail endpoints are pinned at 3/60s explicitly. Buckets are per-CloudFront-edge, not per-viewer (nginx trusts only the ALB CIDR) — coarser than one caller, but unspoofable and no longer global. Pinned by five tests in `auth/test/email-flows.test.js`, two of them adversarial. Post-deploy check: the § 2 warning grep must return nothing new |
| **EV-2** | Verification and reset token lifetimes were inherited library defaults, invisible to a reader of `auth/auth.js` and free to move on a version bump. | `auth/auth.js` | **fixed** — both pinned at 3600s, behaviour unchanged, with tests asserting both the literal and what reaches the wire |
| **EV-3** | `POST /api/auth/send-verification-email` is reachable with no session and was an account-existence oracle: Better Auth's 500 ms constant-time floor was defeated because the mail callback awaited the SES round trip (measured: unknown 504 ms, known-and-unverified 922 ms against a 900 ms mailer). The endpoint only becomes user-facing at this flip. | `auth/auth.js` `sendVerificationEmail` | **fixed** — fire-and-forget, mirroring `sendResetPassword`; regression-guarded by a timing test |
| **EV-4** | Completing a password reset proves mailbox control but does not set `emailVerified`, so under enforcement a successful reset still ends in a 403. | `auth/auth.js` — no `onPasswordReset` hook configured | **open, owner decision.** Closing it is a few lines; it is a security-semantics call, not a bug fix |
| **EV-5** | No SES configuration set and no event destination, so there is no bounce/complaint telemetry and no automated suppression handling. Production access is *kept* on those two rates. | AWS console / `infra/` | **open** — see § 9 |
| **EV-6** | An agent cannot open an inbox link. `scripts/agent_journey.py` signs up and immediately signs in (lines 49–76); after the flip that second call returns 403 and the programmatic path — including the dogfooding harness — stops. There is no API-key or bearer-token lane today (PR #1653 decision D3, open). | `scripts/agent_journey.py`, `docs/agent-quickstart.md` | **open, owner decision** — pick one before flipping: ship the API-key lane, exempt a named service account, or accept that the agent path ends at the flip and say so in the docs |

---

## 8. Evidence to capture

Attach to the flip PR or the issue. Screenshots, not descriptions — and **redact every
token**: a verification URL is a live sign-in credential for an hour (§ 1).

| # | Evidence | Passing looks like |
|---|---|---|
| 1 | `aws sesv2 get-account` output | `ProductionAccessEnabled: true` |
| 2 | `emailVerified` counts from Aurora | a number you have decided about |
| 3 | Inbox screenshot per provider, **From** header and subject visible, link blurred | in **Inbox**, not spam; sender `no-reply@archimedes-arc.com` |
| 4 | Time-to-inbox per provider | seconds, not minutes |
| 5 | Raw-message headers for one delivery ("Show original" in Gmail) | `SPF: PASS`, `DKIM: PASS`, `DMARC: PASS` |
| 6 | Account Settings after verifying | `Email verified ✓` |
| 7 | Expired verification link | error naming an expired token |
| 8 | Reset mail + the `/reset-password` screen | subject `Reset your Archimedes password` |
| 9 | Session Y after the reset | signed out |
| 10 | Reset link replayed | invalid-token error, password unchanged |
| 11 | SES `Send` / `Bounce` / `Complaint` metrics for the window | sends counted, zero bounces, zero complaints |
| 12 | Log grep for `email send failed` | empty |

---

## 9. SES sandbox vs production — the gotchas that bite

- **Sandbox delivers only to verified destination identities.** A signup from any other
  address produces a `MessageRejected` from SES, which `auth/auth.js` deliberately swallows
  fail-soft: the signup still returns 200 and the user sees nothing wrong. The only trace is
  one `verification email send failed:` line in `/archimedes/app`. That is why "we haven't
  really validated verification" is an accurate description of the current state — in
  sandbox, *no signal reaches the user either way*.
- **Sandbox caps are 200 messages/24h and 1 message/second.** Fine for this runbook, not for
  a launch.
- **Production access is granted per region and per account, not per identity**, and it can
  be revoked. AWS watches two numbers: **bounce rate < 5%** and **complaint rate < 0.1%**.
  Both are computed over your *sending*, which for us is almost entirely
  signup-verification mail — that is, mail to addresses typed by strangers, which is
  precisely the traffic that bounces. This is why EV-5 matters before the flip and not
  after.
- **Create a configuration set with an event destination before you flip.** Without one,
  bounces and complaints are invisible until AWS emails you about the account. A
  CloudWatch or SNS destination for `BOUNCE`, `COMPLAINT`, `REJECT`, and `DELIVERY_DELAY`
  is the minimum, plus a CloudWatch alarm on the bounce rate. Note that
  `auth/mailer.js`'s `SendEmailCommand` sends **no** `ConfigurationSetName` today, so
  adding the configuration set in the console alone changes nothing — the mailer has to
  pass it.
- **The SES suppression list is account-wide and silent.** One hard bounce puts an address
  on it, and every later send to that address is dropped without an error the app can see.
  Check it (§ 2 step 3) before believing "the mail never sends".
- **A custom MAIL FROM domain is not configured.** SPF alignment therefore rides on
  `amazonses.com`; DKIM alignment carries DMARC on its own. This is fine at `p=none` and is
  a prerequisite to look at again before moving DMARC to `p=quarantine`.
- **`archimedes-arc.com` also receives mail** (`infra/ses_inbound.tf` — the `privacy@`
  inbox). Inbound receipt rules and outbound sending are independent; changing one does not
  affect the other, but they share the domain identity, so do not delete it while
  "cleaning up".
- **Corporate mail gateways prefetch links.** Outlook Safe Links and similar scanners will
  open a verification URL before the human does — which, with `autoSignInAfterVerification`,
  consumes the one auto-sign-in the link carries. The user's own click then verifies nothing
  (already verified) and signs them into nothing. If a corporate tester reports "the link
  did nothing", this is the first hypothesis, not a bug in the token.
- **Test from a mailbox you own, at a plus-address.** Never a teammate's address, never a
  disposable-mail service: disposable domains are a fast route onto a blocklist, and a
  colleague's "this is spam" click is a complaint against the domain's reputation.

---

## 9b. "The mail is not arriving" — read the delivery state first

Before rehearsing anything below, ask the product what it recorded. Since
[#1748](https://github.com/aprin-labs/archimedes/issues/1748) item 2 the auth sidecar keeps
one row per send in `auth_email_deliveries` (the SES `MessageId` when SES accepted the
message, the error **name** when it did not) and exposes the reading at
`GET /api/auth/verification-status`. Signed in as the affected account:

```bash
curl -sS -b /tmp/session.jar https://archimedes-arc.com/api/auth/verification-status
```

| `state` | What it means | What to do |
|---|---|---|
| `suppressed` | The address is on the **account-level** SES suppression list (`suppression.reason` = `BOUNCE` or `COMPLAINT`). SES accepts sends to it and drops them. | Nothing in this runbook helps — this is suppression-list hygiene ([#1748](https://github.com/aprin-labs/archimedes/issues/1748) item 4), and an address is only ever removed when its owner confirms it is real. |
| `failed` | The last send threw; `lastError` is the AWS SDK error name. Nothing left the building. | `AccessDeniedException` → the task role's SES policy (`infra/ecs.tf`). `MessageRejected` → sender identity / sandbox. |
| `rate_limited` | The resend window is full (`retryAfterSeconds` says how long). | Wait. This is the limiter working. |
| `sent` | The mailer **accepted** the last send. Not delivery — SES returns a MessageId for a suppressed address too. | Continue with the rehearsals below; the failure is downstream of AWS. |
| `unknown` | `sends: 0` — no send on record. `sends: null` — the delivery log could not be read. | `0`: the send never happened, look at the auth logs. `null`: check the migration ran and `DATABASE_URL` is set for the auth container. |

`suppression.checked: false` means the lookup itself did not run (console mailer, throttling,
or a missing `ses:GetSuppressedDestination` grant). It is **not** "the address is fine".

## 10. What is already proven without an inbox

`cd auth && npm ci && npm test` — the hermetic half of these flows, run in CI on every PR
(`quality-gate.yml` → `frontend-and-auth`). It covers the verification token round trip,
expired-token refusal for both flows, reset-token single use, session revocation on reset,
the anti-enumeration properties of both request endpoints, and the two findings above.

If a step in this runbook fails, check whether the corresponding test in
[`auth/test/email-flows.test.js`](../../auth/test/email-flows.test.js) or
[`auth/test/auth.test.js`](../../auth/test/auth.test.js) still passes: green there and red
here narrows the problem to delivery — SES, DNS, or the receiving provider — and takes the
application code out of the search.
