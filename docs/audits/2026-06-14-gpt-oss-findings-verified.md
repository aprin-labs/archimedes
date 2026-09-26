# GPT-OSS Findings (2026-06-14) — Verified

Source: a 10-section audit report (`gpt-oss-findings.md`, not checked into this repo)
produced by a different model, explicitly framed as a correction of an earlier
hallucination-prone pass. Per CLAUDE.md's "verify your own audits" rule, every
finding below was re-checked against the literal current source on `main`
(post-#616, commit `a1a1fe7`) before any fix was written. Status legend:

- **REAL** — confirmed against source, fix shipped in this batch.
- **REAL / TRACKED** — confirmed against source, but the right fix is a design
  decision, not a mechanical patch → tracked via GitHub issue.
- **ALREADY FIXED** — the underlying problem existed but was already resolved by
  an earlier PR merged today (#609, #616) or by CI config that predates this audit.
- **DEBUNKED** — re-verification shows the finding is incorrect, mislabeled, or
  describes intentional/documented behavior.
- **LOCAL-ENV ONLY** — real on the auditor's machine, but the repo's declared
  floors are already correct; the gap is conda/node_modules/venv staleness.

## 1️⃣ Critical smart contract bugs

### 1a. Vault redemption slippage-revert DoS — **REAL**

[`Vault.sol:_liquidateToUsdc`](../../contracts/src/Vault.sol#L498) sizes the
liquidation at a fixed `shortfall + shortfall/200` (0.5% buffer), then requires
`usdcAfter >= shortfall`. `_oracleMinOut` guarantees the swap returns at least
`expectedOut * (BPS - maxSlippageBps) / BPS`. Worst case:
`usdcAfter_min = liquidationTarget * (BPS - maxSlippageBps) / BPS`. For the
fixed 0.5% buffer, `usdcAfter_min >= shortfall` only holds when
`maxSlippageBps <= ~50`. `MAX_SLIPPAGE_CAP_BPS = 500` (5%), so any operator
setting between 51–500 bps guarantees `InsufficientLiquidity()` on a large
redemption that needs liquidation — funds get stuck.

**Fix:** `liquidationTarget = ceil(shortfall * BPS / (BPS - maxSlippageBps))`,
which ties the buffer directly to the slippage floor that bounds the swap's
worst case, making `usdcAfter_min >= shortfall` an algebraic identity. Division
is always safe (`maxSlippageBps <= MAX_SLIPPAGE_CAP_BPS = 500 < BPS = 10000`).

→ `onder/vault-liquidation-slippage-buffer` (contracts, **source-only /
redeploy-gated**, same pattern as #609).

### 1b. Tier-2 `setTokenOracles` is `onlyOwner`, agent is `onlyManager` — **REAL / TRACKED**

`grep -A1 "function setTokenOracles" contracts/src/Vault.sol` on current `main`
shows `onlyOwner` (changed from `onlyManager` by **#609**, merged today
2026-06-14T01:49:45Z — `gh pr view 609 --json mergedAt` confirms). #609's intent
was deliberate: an `onlyManager` (agent-controlled) `setTokenOracles` lets a
compromised/prompt-injected agent repoint a synthetic's price oracle at an
attacker contract and drain the vault via mispriced swaps — a strictly worse bug
than this one.

But the report is right about the *consequence*: for a Tier-2 community vault,
`owner` = the vault creator's wallet, `agent` = Archimedes' `onlyManager`
account. Post-#609, the agent can never call `setTokenOracles`, so it can never
price (or rebalance into) a new synthetic in a Tier-2 vault without an
out-of-band owner transaction. This is a real product gap, not a revert of #609.

→ Tracked as a new issue (resolution directions: registry-allowlisted
`onlyManager` function that can only point at oracles already present in
`AssetRegistry`/`PriceOracle`, vs. owner pre-approval at vault-creation time).
**Not a PR in this batch** — needs a design call, and any contract change here
is again redeploy-gated.

## 2️⃣ Portfolio optimization math

### 2a. Max-Sharpe maximizes volatility when all excess returns are negative — **REAL**

[`portfolio_optimizer.py:_max_sharpe`](../../backend/archimedes/services/portfolio_optimizer.py#L324)
minimizes `-(w·μ - rf) / sqrt(w'Σw)`. When `max(μ) <= _RF_DAILY`, the numerator
is negative for every feasible `w`; the optimizer then *maximizes* `sqrt(w'Σw)`
to push the (negative) ratio toward zero — i.e. it deliberately picks the
highest-volatility corner of the simplex during a downturn.

**Fix:** guard `max(mu) <= _RF_DAILY` and fall back to `_gmv` (Global Minimum
Variance) — six call sites (lines ~198-200, 564, 571, 587, 591, 594) all funnel
through `_max_sharpe`, so a guard at the top of that function (or its
dispatcher) covers all of them.

→ `onder/portfolio-max-sharpe-bearish-guard`

### 2b. `ledoit_wolf_shrinkage_from_cov` naming — **DOCUMENTED, not fixed this batch**

[`portfolio_optimizer.py:703`](../../backend/archimedes/services/portfolio_optimizer.py#L703)
applies a hardcoded `intensity = 0.10` shrinkage-to-scaled-identity; the
data-driven LW2004 analytic-intensity estimator is the separate
`ledoit_wolf_shrinkage` (line 881). The function already carries a comment
distinguishing the two. The name is arguably confusing but not incorrect
behavior, and a rename touches call sites for cosmetic gain only — left as a
documented low-priority follow-up rather than spending one of the 10 PR slots.

## 3️⃣ Distributed systems / session state

### 3a. SIWE nonces are process-local — **REAL**

[`auth_siwe.py:54`](../../backend/archimedes/api/auth_siwe.py#L54)
`_pending_nonces: dict[str, float] = {}` is per-worker memory. In any
multi-worker deploy, `/nonce` (Worker A) → `/verify` (Worker B) returns `401
Nonce not found or already used` whenever the load balancer routes the two
requests differently.

**Fix:** move `_pending_nonces` to `AgentStateStore` (Redis), with a
same-process fallback dict if Redis is unreachable (matches the
Redis-down-defaults pattern already used for `AgentStateStore` elsewhere).

→ `onder/siwe-nonce-redis-store`

### 3b. `_SESSION_SECRET` per-worker-random fallback — **MITIGATED, folded into 3a**

[`auth_siwe.py:41`](../../backend/archimedes/api/auth_siwe.py#L41)
`_SESSION_SECRET = os.getenv("EMAIL_ENCRYPTION_KEY", secrets.token_hex(32))`.
The random fallback would indeed cause cross-worker cookie-verification
failures — **but** [`main.py:127`](../../backend/archimedes/main.py#L127)
already fail-closes: `RuntimeError` at boot if `PUBLIC_DOMAIN` is set
(production) and `EMAIL_ENCRYPTION_KEY` is unset. So the random-per-worker path
is unreachable in any deployment that has multiple workers. Local dev runs a
single worker, so it's a non-issue there too.

→ No separate PR. A one-line comment clarifying *why* this is safe is added as
part of `onder/siwe-nonce-redis-store` (same file, adjacent line).

### 3c. Unhandled `decrypt_email` exception → 500 on `/api/user/profile` — **REAL**

[`user_routes.py:61`](../../backend/archimedes/api/user_routes.py#L61)
`email=decrypt_email(p.email)` has no exception guard. A `Fernet.InvalidToken`
(e.g. after an `EMAIL_ENCRYPTION_KEY` rotation leaves old ciphertext
undecryptable) takes down the whole profile endpoint for that user.

**Fix:** wrap in try/except for `cryptography.fernet.InvalidToken`, log a
warning, return `None` for the email field instead of crashing.

→ `onder/user-profile-decrypt-guard`

## 4️⃣ Architectural flaws

### 4a. CWD-dependent SQLite path — **REAL**

[`db.py:24`](../../backend/archimedes/db.py#L24)
`DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./archimedes_chat.db")` —
`./` is CWD-relative, so running from repo root vs. `backend/` produces two
disjoint `archimedes_chat.db` files. This is the exact gotcha documented twice
already in session memory ("stale `archimedes_chat.db` ... bites chat tests").

**Fix:** anchor to `Path(__file__).resolve().parent.parent /
"archimedes_chat.db"` (→ always `backend/archimedes_chat.db` regardless of CWD).

→ `onder/db-cwd-sqlite-path`

### 4b. "Ghost" regime detection — **REAL / TRACKED, PLAN-PROTECTED**

`regime_detector.py` (v1, VIX+SMA heuristics) and `statistical_regime.py` (v2,
GMM) are both unused by production code — confirmed only their own unit tests
import them. Production (`agent_runner.py:195-208`) uses an ad-hoc
`flat_pct`-based 3-bucket classifier (`risk_off`/`transition`/`risk_on`),
diverging from the 4-regime model (`+crisis`) the two unused files define.

This is **not** ordinary dead code: `docs/dead-code-audit-2026-05-24-v2.md`
marks both files **PLAN-PROTECTED** pending "Önder's consolidation read" — i.e.
a deliberate decision to keep them until a chosen regime model is picked and
wired into `agent_runner.py`, at which point the *other* implementation(s)
become deletable. Deleting either file now would be premature.

→ Tracked as a new issue (no issue existed for this per
`dead-code-audit-2026-05-24-v2.md`'s own "no issue filed yet" note, and
`gh issue list` search for "regime" turned up only the unrelated, closed
frontend issue #391). **Not a PR in this batch.**

## 5️⃣ Toolchain & test suite — **ALREADY FIXED**

Both items in this section describe problems that no longer exist on `main`:

- **`pytest-asyncio`/`pytest-cov` missing**: `backend/requirements.txt`
  intentionally does not list them — `.github/workflows/quality-gate.yml:33,180`
  installs `"pytest-asyncio>=0.24" "pytest-cov>=5.0"` explicitly alongside `-r
  backend/requirements.txt` in every CI job. `environment.yml` carries both for
  local dev. The report's "fix" (install them locally) is exactly what
  `environment.yml` already provides — this was a local-env gap, not a repo gap.
- **`_annualized_sharpe_arr` NameError in `rigor_evaluator.py`**: `grep
  _annualized_sharpe_arr backend/archimedes/services/rigor_evaluator.py` returns
  nothing — `rigor_evaluator.py:28-44` now imports `regime_conditional_sharpe`
  and `regime_robustness_score` *from* `_rigor_helpers` (re-exported, `# noqa:
  F401 - re-exported for test_rigor_regime`) instead of redefining them.
  `_annualized_sharpe_arr` is only used internally within `_rigor_helpers.py`
  (lines 361/500/749), which doesn't need to import it from itself. This was
  fixed by **#616** ("Remove duplicated rigor_evaluator function bodies, fix
  re-export imports"), merged earlier today (commit `c72cb07`) — i.e. fixed
  *before* this audit ran, by an unrelated PR.

No action needed.

## 6️⃣ Dead / stale / orphaned code — **REAL**, all three confirmed present and unreferenced

- ~~`backend/archimedes/api/marketplace_routes.py`, `marketplace_schemas.py`,
  `backend/archimedes/services/marketplace_service.py` (+ `__pycache__`):
  `main.py` carries "removed (Issue #381)" comments at the import/mount sites;
  zero test files reference marketplace. → `onder/remove-dead-marketplace-files`~~
  **RETRACTED 2026-07-27 — DO NOT ACT ON THIS.** The marketplace was subsequently
  built out and **shipped**; it is live behind the `PAYMENTS_DRY_RUN` flag
  (`backend/archimedes/marketplace/`, `api/marketplace_routes.py`,
  `ui/src/pages/{MarketplacePage,PublishPage,SubscriptionsPage}.jsx`). Deleting these
  files would remove shipped product surface.
- `backend/archimedes/services/_deprecated/` (`kelly_portfolio.py`,
  `portfolio_constructor.py`, `__init__.py`): zero references outside the
  folder; fully superseded by `portfolio_optimizer.py` +
  `portfolio_agent.py`. → `onder/remove-deprecated-portfolio-services`
  **DONE as of 2026-09-01.** The two modules were removed earlier; the empty
  `__init__.py` package shell went in the audit P1-1 dead-module sweep. There is
  no `services/_deprecated/` in the tree any more.
- `backend/archimedes/scripts/migrate_to_unified_passport_store.py`: one-shot,
  already-run migration; referenced only by itself and an archived doc
  (`docs/archive/launch-execution-plan-2026-05-23.md`). →
  `onder/remove-stale-migration-script`

## 7️⃣ Debunked findings from the *previous* (hallucinated) report — **CONFIRMED DEBUNKED**

Re-verified all three claims from the prior report are indeed false, matching
this report's own correction:
- No `config_loader.py` anywhere under `backend/archimedes/chain`.
- No file named `price_aggregator*` anywhere in the repo.
- `pyyaml` is not in `backend/requirements.txt`, `environment.yml`, or imported
  by any production module.

No action needed — this section is itself just a correction record.

## 8️⃣ Ruff style/lint violations — **REAL**, mechanical

`ruff check .` surfaces ~30 issues, all confined to `backend/tests/`: unused
locals (`quant_factories.py:103`, `test_rigor_evaluator.py:1214`,
`test_rigor_regime.py:102`), unused unpacked tuple elements
(`test_dsr_parity.py:92-93`, `test_quant_factories.py:89`), and unsorted-import
blocks in several `test_*.py` files. All auto-fixable via `ruff check --select
I --fix .` + `ruff check --fix .` + `ruff format .` (the documented cleanup
recipe), no behavior change.

→ `onder/ruff-lint-cleanup`

## 9️⃣ CI/CD / workflow findings — mostly **DEBUNKED** or **BY DESIGN**

No workflow-file PRs in this batch (re-affirms #608's precedent: `gh` token
lacks `workflow` scope, so pushes touching `.github/workflows/*` need a
web-UI merge — not worth spending a slot on debunked/by-design items).

- **`quality-gate.yml:173` `t2o2`-only coverage gate** — **BY DESIGN**.
  CLAUDE.md states explicitly: "Agent PRs (`t2o2`) also get a coverage gate (≥
  60%)." Broadening this is a policy change affecting all future contributors,
  not a bug fix — left as-is.
- **"Deprecated `F82` rule"** — **DEBUNKED**. `ruff check --select F82 .` →
  `All checks passed!`. `F82` is a valid prefix-selector (matches `F821`,
  `F822`, ...) and CLAUDE.md's own table already lists it among the blocking
  selectors (`E9,F63,F7,F40,F82`), contradicting the table's framing of F82 as a
  "next candidate" not yet added — it's already there and working.
- **"Fragile multi-line commit" in `main-format-guard.yml:61-66`** —
  **DEBUNKED**. The block uses a standard YAML literal block scalar (`|`) to
  pass a single multi-line string to `git commit -m`; this is the conventional,
  working pattern, not fragile.
- **"Hardcoded deployment secrets" in `deploy.yml:59,71`** — **DEBUNKED /
  mislabeled**. `AWS_ROLE_ARN` and `EC2_INSTANCE_ID` are resource identifiers,
  not secrets — actual AWS credentials come via OIDC role assumption. The file's
  header comments already document this as an accepted alternative to GitHub
  Variables.
- **`import-guard.yml:39-40` `deptry ... || true` + `continue-on-error`** —
  **BY DESIGN**, intentional per the file's own inline comment (informational
  dependency-hygiene check, not a gate).

## 🔟 Dependency upgrades — mixed

### Repo floors already correct (LOCAL-ENV ONLY drift)

`starlette>=1.0.1` (closes PYSEC-2026-161, already the exact fix version),
`pypdf>=6.13.0` (already above the 6.12.0 CVE-fix line),
`uvicorn[standard]>=0.49.0`, `anthropic>=0.104.1`, `redis>=8.0.0`,
`numpy>=2.4.6,<3.0` are all already declared correctly in both
`environment.yml` and `backend/requirements.txt`. The report's "Conda env vs
requirements mismatches" table is the local `archimedes` conda env having
drifted behind these already-correct floors (`numpy 1.26.4` vs.
`>=2.4.6` etc.) — a `conda env update -f environment.yml --prune` fixes it, but
per prior session guidance that's not done mid-session while other agents may
share the env. **Routine maintenance, not a repo bug.**

Same story for `ui/` (`react-dom`/`viem`/`vite`/`@iconify-json/simple-icons` —
`package.json` already pins the versions the report wants; `npm ci` resolves
local `node_modules` drift) and `analytics-engine/.venv` (`yfinance`/`numpy` —
local venv staleness vs. already-correct `backend/requirements.txt` floors).

### Repo-floor CVE gaps — **REAL**

- **`aiohttp`**: floor is `>=3.13.5`, but `3.13.5` itself carries
  `CVE-2026-34993` / `CVE-2026-47265` (fix: `>=3.14.0`). The dependabot-set
  floor (#250) predates these CVEs.
- **`tornado`** (transitive, via `jupyter`): `CVE-2026-49854` fixed in `6.5.6`;
  not currently pinned anywhere.

**Fix:** bump `aiohttp>=3.13.5` → `>=3.14.0` in both `environment.yml` and
`backend/requirements.txt`; add a direct `tornado>=6.5.6` dev-only pin in
`environment.yml`, mirroring the existing `starlette>=1.0.1` precedent
(CLAUDE.md: "Pin transitively-vulnerable deps directly when CVEs warrant it").

→ `onder/dependency-cve-floor-bumps`

### Out of scope this batch

- `pip` `26.1.1` → `PYSEC-2026-196` (fix `>=26.1.2`, "Low" severity, affects
  package installation tooling itself, not the running app) — noted for a
  future routine bump, not worth a slot here.
- `openzeppelin-contracts@5.4.0` — report confirms this is correct/stable, no
  action.

## The 10 fix branches (this batch)

| # | Branch | Scope |
|---|--------|-------|
| 1 | `onder/vault-liquidation-slippage-buffer` | contracts, source-only/redeploy-gated |
| 2 | `onder/portfolio-max-sharpe-bearish-guard` | `portfolio_optimizer.py` + tests |
| 3 | `onder/siwe-nonce-redis-store` | `auth_siwe.py` + tests |
| 4 | `onder/user-profile-decrypt-guard` | `user_routes.py` + tests |
| 5 | `onder/db-cwd-sqlite-path` | `db.py` + tests |
| 6 | `onder/remove-dead-marketplace-files` | delete 3 files + pycache |
| 7 | `onder/remove-deprecated-portfolio-services` | delete `_deprecated/` |
| 8 | `onder/remove-stale-migration-script` | delete 1 script |
| 9 | `onder/ruff-lint-cleanup` | `backend/tests/` only |
| 10 | `onder/dependency-cve-floor-bumps` | `environment.yml` + `backend/requirements.txt` |

## Follow-up issues (not PRs)

- Tier-2 `setTokenOracles` access for the agent post-#609 (§1b).
- `regime_detector.py` / `statistical_regime.py` consolidation (§4b).

## Re-verification commands

```bash
# 1a — slippage math (read the function, check the algebra)
sed -n '477,545p' contracts/src/Vault.sol

# 1b — current modifier + #609 merge status
grep -A1 "function setTokenOracles" contracts/src/Vault.sol
gh pr view 609 --repo a-apin/archimedes --json mergedAt,title

# 2a — call sites
grep -n "_max_sharpe(\|_gmv(" backend/archimedes/services/portfolio_optimizer.py

# 3a/3b — nonce store + fail-closed secret
grep -n "_pending_nonces\|_SESSION_SECRET" backend/archimedes/api/auth_siwe.py
grep -n "EMAIL_ENCRYPTION_KEY" backend/archimedes/main.py

# 3c
sed -n '55,65p' backend/archimedes/api/user_routes.py

# 4a
sed -n '20,28p' backend/archimedes/db.py

# 5 — both already-fixed
grep -n "_annualized_sharpe_arr" backend/archimedes/services/rigor_evaluator.py   # empty
grep -n "pytest-asyncio" .github/workflows/quality-gate.yml                       # present

# 8
ruff check . | wc -l

# 9 — F82 not deprecated
ruff check --select F82 .

# 10
grep -n "aiohttp\|tornado" environment.yml backend/requirements.txt
```
