# Archimedes OWASP/ASVS/CWE Audit — 2026-09-27

**Scope:** Full repo, split into four parallel read-only lanes: backend (`backend/archimedes/`),
smart contracts (`contracts/src/` — `Vault`, `VaultFactory`, `SyntheticVault`,
`ReasoningTraceRegistry`, AMM/oracle/registry supporting contracts), frontend (`ui/src/`),
and infra/CI/secrets (`.github/workflows/`, `infra/`, `docker-compose*.yml`, `Dockerfile*`,
`.secrets.baseline`).
**Method:** 4-agent fan-out, one per lane, each briefed with an OWASP Top 10 / ASVS v4.0 /
CWE-SANS Top 25 methodology (attack-surface & taint-flow mapping → access control/BOLA →
business-logic/race conditions → crypto & secrets), each instructed to cite concrete
file+function for every finding and to report "nothing confirmed" rather than pad with
speculative items. No exploit code or weaponized PoCs were produced or requested. No fixes
were applied — this is a findings audit only.
**Lineage:** Independent of the `2026-06-14-full-tree-audit.md` chain; some of that audit's
HIGH findings (rigor-gate duplication, agent==owner custody, on-chain commit-reveal unwired)
are superseded by later work per `CLAUDE.md` / `docs/adr/`. This pass does not re-verify
those; it starts fresh against current `main` (`755ac771`).

---

## Headline

The backend and frontend lanes came back largely clean — both came out of prior audit
cycles well-hardened, with fixes for the classic vulnerability classes (IDOR, SQLi, weak
crypto, race conditions, fail-soft-on-security-critical-paths) already shipped and covered
by regression tests. The two lanes that touch **real money movement** — the non-custodial
vault contracts and the client-side payment-key flow — each produced one HIGH-severity,
citable finding:

1. **`Vault.sol`'s `creator` role silently outlives ownership transfer and contradicts the
   accepted non-custodial ADR.** The ADR says `creator` is configuration-only and "cannot
   rebalance()"; the shipped `onlyManager` modifier lets `creator` rebalance anyway, and
   `creator` is immutable — so a vault handed to a real depositor via `transferOwnership`
   keeps its original deployer as a permanent, un-revocable co-manager.
2. **The #915 anti-churn guard can be defeated by its own subject for free.** Because
   `setTargetAllocations()` carries the same role gate as `rebalance()` with no cooldown,
   a manager can oscillate the target and legally grind vault NAV down to AMM fees/slippage
   over repeated cycles — value leaves depositors without ever touching the manager's
   wallet, so it evades every existing "agent can't extract funds" test.
3. **The client-side payment session key (`payment-session.js`) has no enforced deposit
   ceiling.** The design's own risk model assumes small, bounded deposits into a cleartext
   `localStorage` private key; the UI only enforces a lower bound (must cover price), so
   the actual blast radius of a future XSS/supply-chain compromise is "whatever the user
   was ever talked into depositing," not the small number the code comments assume.

A fourth HIGH sits in infra: a Terraform-drift workflow's OIDC-assumed AWS role is trusted
from **any** `pull_request` against this repo and, in that same job, fetches the production
Aurora master password into the process environment before evaluating PR-controlled
Terraform — a branch-push-level actor (not necessarily an anonymous fork) could reach a
live prod secret plus broad account-read through unreviewed `.tf` changes.

Everything else is MEDIUM/LOW hardening debt, not a live exploit path.

---

## Severity rollup

| Severity | Count | IDs |
| --- | --- | --- |
| Critical | 0 | — |
| High | 4 | SC-01, SC-02, FE-01, IN-01 |
| Medium | 2 | IN-02, SC-03 (partial — see note) |
| Low | 4 | IN-03, IN-04, IN-05, SC-04 |
| Backend (BE) | 0 confirmed | 2 non-blocking follow-ups noted, not ranked |

---

## Contracts (highest stakes — live funds on Arc testnet)

### [SC-01] `creator` retains permanent, unrevocable manager authority — contradicts the accepted non-custodial ADR

- **Severity:** High | CWE-284 (Improper Access Control)
- **Component:** `contracts/src/Vault.sol :: onlyManager` (lines 239–242), consumed by
  `rebalance()` (440), `setTargetAllocations()` (516), `setTokenOraclesFromRegistry()` (576)
- **Path:** `VaultFactory.createVault()` sets `creator = msg.sender` immutably
  (`VaultFactory.sol:66`, `Vault.sol` constructor line 228). `onlyManager` is
  `if (msg.sender != creator && msg.sender != agent) revert Unauthorized();`. The vault's
  own handoff pattern (`contracts/test/Vault.t.sol::_handoffVault`, line 1511, used for both
  agent-deployed and permissionless community-created vaults) calls `v.transferOwnership(user)`
  to hand custody to the real depositor. `transferOwnership` changes `owner()`; `creator`
  is immutable and neither changeable nor revocable by the new owner. The original creator
  therefore keeps `onlyManager` rights forever, independent of what the new owner does with
  `setAgent()`.
- **Root cause:** Directly contradicts `docs/adr/non-custodial-vault-owner-agent.md`
  (Status: Accepted), which states `creator` is "configuration authority: setAgent(),
  oracle/slippage config. **Cannot rebalance().**" and `agent` alone holds
  "`rebalance()` / `setTargetAllocations()` only." The code instead grants `creator` the
  same rebalance/retarget authority as `agent`, unconditionally — and
  `Vault.t.sol::test_rebalance_creator_can_also_rebalance` (line 531) asserts exactly this
  as a "success" case, confirming the divergence from the ADR is intentional in the code,
  just never reconciled with the (still-Accepted, still-authoritative per `CLAUDE.md`) ADR.
- **Remediation:**

  ```diff
       modifier onlyManager() {
  -        if (msg.sender != creator && msg.sender != agent) revert Unauthorized();
  +        // ADR docs/adr/non-custodial-vault-owner-agent.md: only `agent` holds
  +        // rebalance/retarget authority. `creator` is deploy-time configuration
  +        // authority ONLY and must never retain trading rights — especially
  +        // since `creator` is immutable and otherwise un-revocable even after
  +        // transferOwnership() hands the vault to a different user.
  +        if (msg.sender != agent) revert Unauthorized();
           _;
       }
  ```

  Requires the new owner to call `setAgent()` (even to their own address) before management
  is possible on a fresh vault — the ADR already accepts this as the correct fail-safe
  default. `test_rebalance_creator_can_also_rebalance` would need updating to match the ADR,
  not preserving.
- **Recommendation:** Either fix the code to match the ADR, or open a superseding ADR if
  creator-can-manage is now the intended product behavior — don't leave an Accepted ADR and
  shipped code silently contradicting each other.

### [SC-02] `setTargetAllocations()` has no rate limit, letting the #915 churn guard be flipped for free

- **Severity:** High | CWE-841 (Improper Enforcement of Behavioral Workflow)
- **Component:** `contracts/src/Vault.sol :: setTargetAllocations` (516–541) and
  `_requireTowardTarget` (707–730), invoked from `rebalance()` (474, 501)
- **Path:** `_requireTowardTarget` blocks a swap only if it moves a token further from
  `targetWeightBps[token]` — but `targetWeightBps` is entirely controlled by
  `setTargetAllocations()`, which carries the *same* `onlyManager` gate as `rebalance()`
  (today: `creator` OR `agent` — see SC-01), has no cooldown, and is free/instant to call
  repeatedly (unlike `rebalance()`, it never touches `traceRegistry.executeTrade()`). A
  manager can set target A=100%/B=0%, rebalance to buy A; flip to A=0%/B=100%, rebalance to
  sell A / buy B (legally "toward" the new target); flip back; repeat. Each leg pays the
  AMM's fixed `swapFeeBps` (30 bps default, `AMMPool.sol:95`) plus slippage. Iterated, this
  grinds vault NAV down to the AMM pool/LPs over many transactions, without the manager's
  wallet ever receiving funds — so it evades every existing "agent cannot receive vault
  funds" test. `Vault.t.sol`'s drain test (line 1658) only asserts NAV conservation for a
  *single* rebalance call, never repeated cycles.
- **Root cause:** The 2026-06-14 review deliberately split `setTokenOracles` (owner-only)
  from `setTokenOraclesFromRegistry` (manager, but bounded to an owner-curated allowlist)
  because letting one role define *and* consume a protective allowlist defeats it
  (comment at `Vault.sol:544–551`). `setTargetAllocations` didn't get the analogous
  treatment: the party whose trades are checked "toward target" is also the sole,
  cost-free author of what "target" means.
- **Remediation:** add a minimum interval between allocation changes (illustrative, not the
  only valid fix):

  ```diff
  +    uint256 public constant MIN_TARGET_ALLOCATION_INTERVAL = 1 days;
  +    uint256 public lastTargetAllocationChange;
  +    error TargetAllocationsRateLimited();
  +
       function setTargetAllocations(...) external override onlyManager {
           if (tokens.length != weightsBps.length) revert InvalidAllocations();
  +        if (lastTargetAllocationChange != 0 &&
  +            block.timestamp < lastTargetAllocationChange + MIN_TARGET_ALLOCATION_INTERVAL) {
  +            revert TargetAllocationsRateLimited();
  +        }
           ...
  +        lastTargetAllocationChange = block.timestamp;
           emit TargetAllocationsSet(tokens.length, block.timestamp);
       }
  ```

  Do **not** fix this by making `setTargetAllocations` `onlyOwner` — the ADR assigns it to
  `agent`; that would itself be an ADR violation in the other direction.
- **Recommendation:** Pair the cooldown with a per-call max weight-delta bound (same idiom
  as `MAX_REBALANCE_BAND_BPS`), and/or route allocation changes through the same
  commit-reveal delay `rebalance()` already uses.

### [SC-03] Unbounded `heldTokens` growth — gas-griefing DoS on deposit/withdraw/redeem

- **Severity:** Medium | CWE-400 (Uncontrolled Resource Consumption)
- **Component:** `Vault._addHolding` (line 935); consumed by `totalAssets()` (called every
  deposit/withdraw/redeem/rebalance via `_accrueFees()`) and `_liquidateToUsdc`
- **Detail:** nothing caps `heldTokens.length`. A manager who wires many distinct
  oracle-registered tokens (bounded only by `AssetRegistry` size) and takes a dust position
  in each grows this loop's gas cost until deposit/withdraw/redeem revert for everyone — a
  griefing vector gated by registry size, not by the vault itself.
- **Recommendation:** cap `heldTokens` length per vault, or evict zero-balance entries
  proactively rather than only on explicit liquidation.

### [SC-04] `AMMPool` reserve accounting has no balance-before/after reconciliation

- **Severity:** Low (informational — not currently exploitable given today's token set)
- **Component:** `AMMPool.sol:105-120, 154-159`
- **Detail:** reserves are updated by nominal `amountIn`/`amount0`/`amount1` before the
  corresponding `safeTransferFrom`, with no reconciliation against actual balances. A
  fee-on-transfer or rebasing ERC-20 would desync tracked reserves from real balances
  (correctable only via the permissionless `sync()`, mispriced until then). Neither USDC nor
  `SyntheticToken` is fee-on-transfer today, so no live exploit — but nothing in the code
  enforces the invariant for a future token addition.

**Contracts — checked, nothing found:** reentrancy (all state-mutating functions carry
`nonReentrant`, verified CEI ordering), integer over/underflow (`^0.8.24` checked math
throughout, zero `unchecked` blocks in `contracts/src`), unchecked external-call return
values (SafeERC20 everywhere), delegatecall / proxy-storage collision (none — no proxy
pattern, no `initialize()`), front-running on price-sensitive functions (`SyntheticVault`
mint/burn enforce a tight separate staleness window per issue #910; `Vault` swaps floor on
oracle-derived `minAmountOut`).

---

## Frontend

### [FE-01] Client-side payment session key has no enforced deposit ceiling

- **Severity:** High | CWE-522 (Insufficiently Protected Credentials) / CWE-312 (Cleartext
  Storage of Sensitive Information)
- **Component:** `ui/src/payment-session.js :: getOrCreateSessionAccount()` (62–74),
  consumed by `ui/src/components/Generate.jsx :: handlePayAndGenerate()` (592–611, 653–669)
  and its free-text `depositAmount` field (204, 1419–1430)
- **Path:** `generatePrivateKey()` (viem) mints a real secp256k1 key client-side; it's
  written to `localStorage.setItem(storageKey(scaAddress), key)` (`payment-session.js:72`)
  in plaintext. `Generate.jsx`'s `handlePayAndGenerate` funds this key's on-chain Gateway
  balance via `depositToGateway(req, amountRaw, …)`, where `amountRaw` comes from a plain
  `<input type="text">` with **no upper bound** — `parseUsdcAmount` validates decimal format
  only, and the sole check against `need` is a *lower* bound (`if (amountRaw < need) throw`).
  Any script execution on the origin (future XSS, compromised transitive dependency,
  malicious extension) can read `localStorage`, reconstruct the account, and sign
  ERC-3009 burn intents against the entire deposited balance.
- **Root cause:** `payment-session.js`'s own header comment (28–33) documents this as a
  deliberate v1 trade-off — cleartext key, risk "bounded by what the user deposited...
  deposits default small." The mitigating claim is a UI *default* (`"20.00"`), not an
  enforced *ceiling* — the user can type any value, and nothing rejects an oversized one.
- **Remediation:**

  ```diff
  --- a/ui/src/payment-session.js
  +++ b/ui/src/payment-session.js
  @@
  +export const MAX_SESSION_KEY_DEPOSIT_RAW = 50_000_000n; // 50.00 USDC (6 decimals)

  --- a/ui/src/components/Generate.jsx
  +++ b/ui/src/components/Generate.jsx
  @@
  +import { MAX_SESSION_KEY_DEPOSIT_RAW } from "../payment-session";
  @@
     const amountRaw = parseUsdcAmount(depositAmount || "20.00");
     if (amountRaw < need) { throw new Error("Deposit amount must at least cover the generation price."); }
  +  if (amountRaw > MAX_SESSION_KEY_DEPOSIT_RAW) {
  +    throw new Error(
  +      "Deposit amount exceeds the device payment key's cap ($50) — this key is stored " +
  +      "unencrypted in your browser, so deposits are capped to limit exposure.",
  +    );
  +  }
  ```

  Apply the same guard at the second call site (`Generate.jsx:660-666`).
- **Recommendation:** Enforce the ceiling server-side too (client JS is bypassable) — reject
  or flag any on-chain `depositFor` into a payment-key-linked wallet above the cap. Surface
  balance-at-risk prominently next to "Pay & generate" rather than in a collapsed
  `<details>`. Consider periodic auto-sweep back to the enclave-protected passkey SCA between
  generations. Retire this rail if Circle's facilitator ever adds SCA/ERC-1271 support (the
  code already flags this as a facilitator limitation, not a permanent constraint).

**Frontend — checked, nothing found:** no `dangerouslySetInnerHTML`/`innerHTML`/`eval`/`new
Function` anywhere in `ui/src`; no markdown/HTML-rendering dependency; a prior XSS class
(backend-supplied `pdf_url`) already remediated with an `https://`-only allowlist
(`CorpusExplorer.jsx:383-388`). No embedded secrets beyond the intentionally-public Circle
client key. Every ERC-20 `approve()` found is bounded to the exact deposit amount (no
`MaxUint256`), every vault/spender address is `isAddress()`-validated before use, no
display/sign divergence. No recurrence of the retired `.app`-domain passkey rpId bug — origin
binding is delegated entirely to the Circle SDK against the single `archimedes-arc.com`
origin. Open-redirect surface (`?next=` on `/sign-in`) is soundly guarded by
`routes.js::safeNextPath()` (same-origin + `/app`-prefix required, rejects `//` and
cross-origin values).

---

## Infra / CI / secrets

### [IN-01] Terraform-drift OIDC role, trusted from any `pull_request`, exposes the prod Aurora master password to PR-controlled Terraform execution

- **Severity:** High | CWE-829 (Inclusion of Functionality from Untrusted Control Sphere) /
  CWE-522
- **Component:** `.github/workflows/terraform-drift.yml` (trigger 111–116, permissions
  149–151, credential+secret fetch 201–221, init/plan 223–244);
  `infra/scripts/setup-github-plan-role.sh` (trust suffixes ~line 130 include
  `pull_request` — the script's own header states the role is assumable from *any*
  in-repo PR branch, "including one that edits the workflow that assumes it")
- **Path:** A `pull_request` touching `infra/**` → checkout of the PR's own (merge-ref)
  content → OIDC token with `sub: repo:aprin-labs/archimedes:pull_request` → assumes
  `archimedes-github-plan` (trust condition matches any PR, not a specific branch/actor) →
  job fetches `/archimedes/prod/AURORA_MASTER_PASSWORD` via `ssm get-parameter
  --with-decryption` into `TF_VAR_aurora_master_password` (masked in *log output* only, not
  in the process env) → `terraform plan` then evaluates the **PR's own, possibly modified**
  `infra/*.tf` with that credential live in the same process. `::add-mask::` doesn't stop
  PR-controlled Terraform from reading the env var and exfiltrating it over the runner's
  open egress.
- **Root cause:** The IAM policy is otherwise carefully scoped (explicit `Deny` on
  `ssm:Get*`/`s3:GetObject` outside the one Aurora parameter and the tfstate bucket) — a
  real compensating control. The residual gap: `ReadOnlyAccess` minus those denies still
  grants account-wide `Describe*`/`List*`/`Get*` across ec2/ecs/rds/elasticache/iam/logs/etc.,
  and the one secret it *can* read is the prod DB master password, handed to a job that then
  executes untrusted PR-supplied Terraform. Real-world reach likely requires branch-push
  access (not necessarily anonymous-fork), since `secrets.TF_VAR_ALARM_EMAIL` is gated by a
  preflight check — but branch-push access → prod DB password + broad account read + code
  execution with cloud creds is still a meaningful escalation. Gate is also self-gated behind
  `vars.TF_DRIFT_ENABLED` (live state unverifiable from the tree).
- **Remediation:** restrict the credentialed `plan` step (the one reading the Aurora secret)
  to `schedule`/`push: branches: [main]` only; keep the `pull_request` trigger fmt/validate-only
  with no cloud credentials (matching `infra-gate.yml`'s existing `-backend=false` posture).
  If a pre-merge plan is wanted, drop the Aurora-password variable from it rather than handing
  a live prod secret to unreviewed code.
- **Recommendation:** Also add an explicit `Deny` on `logs:Get*`/`logs:Filter*` to the plan
  role so a compromised branch can't read application CloudWatch logs either.

### [IN-02] ElastiCache Redis has transit encryption but no AUTH token

- **Severity:** Medium | CWE-306 (Missing Authentication for Critical Function)
- **Component:** `infra/elasticache.tf:64-68`
- **Detail:** `at_rest_encryption_enabled`/`transit_encryption_enabled` are both `true`, but
  `auth_token` is present only as a **commented-out** line. `.env.example:85` and the ECS
  task's `REDIS_URL` both use bare `redis://` with no credential. Anything reaching TCP/6379
  within the allowed security-group sources (private app subnets, plus a transitional rule
  for the decommissioned EC2's SG) gets unauthenticated `PING`/`GET`/`FLUSHALL` on data
  CLAUDE.md itself describes as regime state, reasoning traces, and the job queue — network
  boundary (security group) is the *only* control, no defense-in-depth if it's ever crossed.
- **Remediation:**

  ```diff
     at_rest_encryption_enabled = true
     transit_encryption_enabled = true
  -  # auth_token = var.redis_auth_token
  +  auth_token                 = var.redis_auth_token
  ```

  wire `var.redis_auth_token` from a new SSM SecureString the same way `AURORA_MASTER_PASSWORD`
  is sourced, and update `REDIS_URL` to `rediss://:<token>@<endpoint>:6379/0`.

### [IN-03] Some third-party Actions pinned by mutable tag, not commit SHA

- **Severity:** Low | CWE-829
- **Component:** `contracts-test.yml:49` (`foundry-rs/foundry-toolchain@v1`),
  `infra-gate.yml:103` / `terraform-drift.yml:194` (`hashicorp/setup-terraform@v3`); lower
  risk, same class: first-party `actions/checkout@v7.0.1`, `setup-python@v7`,
  `setup-node@v7`, `github-script@v9`, `upload-/download-artifact@v5` pinned by tag across
  nearly every workflow — in contrast to `aws-actions/*` and everything in
  `openwiki-update.yml`, which **are** SHA-pinned with a version comment.
- **Remediation:** apply the same SHA+comment convention already used for `aws-actions/*`
  to the remaining workflows; consider Dependabot's `github-actions` ecosystem to keep pins
  current automatically.

### [IN-04] `.secrets.baseline` is stale against the current tree, not just unaudited

- **Severity:** Low | CWE-1188
- **Component:** `.secrets.baseline`
- **Detail:** All 28 entries are `is_verified: false` (2026-05-27, already flagged as
  outstanding in `CLAUDE.md`). Confirmed stale beyond that: it flags `.env.example` line 18
  as "Basic Auth Credentials," but line 18 today is plain prose — the actual
  placeholder-credential pattern is now at line 82. Likewise `infra/user-data.sh` line 51 is
  flagged, but the real DB-URL-shaped line is now 64, with a password generated at boot via
  `openssl rand -hex 24`. `detect-secrets` matches by hash, not line number, so the baseline
  still functionally suppresses the same strings — but an all-false, never-triaged baseline
  trains reviewers to treat "detect-secrets passed" as a clean bill of health, and
  `pre-commit install` isn't enforced.
- **Remediation:** `detect-secrets scan --update .secrets.baseline` against current `main`,
  then `detect-secrets audit .secrets.baseline` to set real `is_verified` values, rotating
  any true positive found.
- **Recommendation:** track baseline audit as a recurring task like the 60-day doc-staleness
  rule; `quality-gate.yml`'s `detect-secrets` step is already `continue-on-error`.

### [IN-05] `enforce_admins=false` lets any admin bypass all required checks, on a justification the script calls stale

- **Severity:** Low–Medium | CWE-284
- **Component:** `scripts/setup-branch-protection.sh` (header comment flags this as stale
  since 2026-08-03: scoped for the now-dormant `t2o2` automation account; every human admin
  bypasses branch protection as a side effect)
- **Detail:** `t2o2` is confirmed inactive (per `CLAUDE.md` § Spec-driven execution), so the
  exemption today only affects human admin accounts, with no compensating control catching a
  bad direct push before it deploys (build-on-deploy pushes straight to production).
- **Remediation:** `ENFORCE_ADMINS="${ENFORCE_ADMINS:-true}"`, scoping any future
  automation-actor exemption narrowly (a specific bypass token) rather than the admin role.
- This is a decision for **Dan** per `CLAUDE.md`'s "When to ask before acting" (CI/CD
  wiring) — not applied here.

**Infra — checked, nothing found:** no hardcoded secrets in tracked files; `.gitignore`
coverage for `*.pem`/`*.key`/`*.tfstate*` correct; no `pull_request_target` usage anywhere;
no untrusted PR field (title/body/head_ref) interpolated into a shell `run:` block or
`github-script` template; all Dockerfiles multi-stage/non-root with scoped `COPY`; compose
has no host port bindings on Postgres/Redis and hard-fails on missing required secrets; no
security group opens a non-HTTP(S) port to `0.0.0.0/0`; IAM wildcards are all
comment-justified or condition-scoped; S3 Terraform-state backend configured for
encryption + native locking; WAF managed rule groups all in `BLOCK` mode with one already-documented,
narrowly-scoped exception; the write-capable deploy OIDC role (`archimedes-github-deploy`) is
correctly scoped to `push` on `main` only, unlike the read-only plan role in IN-01.

---

## Backend

No finding met the bar for a ranked entry (concrete citation + non-speculative root cause +
not already a documented-and-fixed prior incident). This lane came back the cleanest: nearly
every classic vulnerability class (IDOR on strategy/vault/trace IDs, SQLi, weak crypto, race
conditions on money-moving paths, fail-soft-on-security-critical-paths) had already been
identified via a prior real incident and fixed with a regression test and an inline comment
naming the issue it closed (e.g. `wallet_routes.py`/`vaults_routes.py`/`traces_routes.py`
citing #916, #1556, #850, #1283). Verified clean: no dangerous-sink usage reachable from HTTP
input (the one `pickle` call round-trips a git-ignored, locally-produced model artifact, never
attacker-controlled); ownership checks enforced at the data-access layer, not just route
decorators, with 404-not-403 responses to avoid enumeration; `free_generations.py` uses a DB
unique constraint (not check-then-act) as its actual concurrency guard; `chain/executor.py`
refuses to default vault ownership to the agent address; API-key auth uses constant-time
comparison on both hit and miss paths; SIWE/EIP-4361 binding is complete (domain, chain-id,
issued-at window, single-use nonce); `email_crypto.py` fails closed in production rather than
using a silent ephemeral key; no wildcard CORS + credentials; rate limiter correctly keys on
the nginx-set `X-Real-IP`, not spoofable `X-Forwarded-For`.

Two non-blocking follow-ups, not vulnerabilities:

- `marketplace_routes.py:701-745`'s `withdraw_publisher_earnings` reads `held_balance` then
  calls `sweeper.withdraw_publisher` with no application-level lock between read and
  on-chain call. Could not rule out a double-withdraw race without reading
  `PaymentSplitter.sol` (out of the backend lane's scope) — worth a contracts-side check
  that the withdraw path is idempotent/reentrant-safe regardless of backend-side races.
- Individual secrets modules (`secrets_service.py`, `email_crypto.py`) are fail-closed, but
  no full trace was done of every SSM-sourced env var against the CLAUDE.md fail-soft
  principle across the whole config surface — that needs `infra/ecs.tf`, outside this lane.

---

## What's next

No code was changed as part of this audit. Every finding above, plus the two backend
follow-ups, is tracked as its own issue:

| Finding | Issue |
| --- | --- |
| SC-01 — `creator` outlives ownership transfer | [#1888](https://github.com/aprin-labs/archimedes/issues/1888) |
| SC-02 — `setTargetAllocations()` churn-guard bypass | [#1889](https://github.com/aprin-labs/archimedes/issues/1889) |
| SC-03 — unbounded `heldTokens` growth | [#1890](https://github.com/aprin-labs/archimedes/issues/1890) |
| SC-04 — AMMPool reserve accounting | [#1891](https://github.com/aprin-labs/archimedes/issues/1891) |
| FE-01 — payment-key deposit ceiling | [#1892](https://github.com/aprin-labs/archimedes/issues/1892) |
| IN-01 — Terraform-drift OIDC role | [#1893](https://github.com/aprin-labs/archimedes/issues/1893) |
| IN-02 — Redis missing AUTH token | [#1894](https://github.com/aprin-labs/archimedes/issues/1894) |
| IN-03 — Actions pinned by tag not SHA | [#1895](https://github.com/aprin-labs/archimedes/issues/1895) |
| IN-04 — stale `.secrets.baseline` | [#1896](https://github.com/aprin-labs/archimedes/issues/1896) |
| IN-05 — `enforce_admins=false` | [#1897](https://github.com/aprin-labs/archimedes/issues/1897) |
| Backend follow-up — publisher-withdraw race | [#1898](https://github.com/aprin-labs/archimedes/issues/1898) |
| Backend follow-up — SSM fail-soft audit | [#1899](https://github.com/aprin-labs/archimedes/issues/1899) |

Recommended order, given the severities:

1. **SC-01 / SC-02** (Vault contracts) — need Dan's explicit approval as contract owner per
   `CLAUDE.md`; live-funds risk, second reviewer (Bogdan) preferred when active.
2. **IN-01** (Terraform-drift OIDC) — CI/CD wiring change, needs team alignment per
   `CLAUDE.md`'s "When to ask before acting."
3. **FE-01** (payment-key deposit cap) — smaller, self-contained frontend fix; still worth a
   second pair of eyes given it's payment-adjacent.
4. **IN-02, IN-05** — infra changes, need team alignment.
5. **IN-03, IN-04, SC-03, SC-04, #1898, #1899** — low-severity hardening and verification
   follow-ups, can be picked up opportunistically.
