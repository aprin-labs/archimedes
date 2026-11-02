# Claims ledger

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-11-01
> **superseded-by:** —

Every public claim Archimedes makes, with a verdict on each and the code that backs it.

`CLAUDE.md` § "The hard constraint, above everything else" says claims must be true. This
file is where that rule becomes checkable instead of aspirational: one row per claim, one
citation per row, and a test (`backend/tests/test_claims_ledger.py`) that fails when a
citation stops resolving.

**Measured against `main` on 2026-09-01.** Every `TRUE` row below was re-read in the live
tree on that date — the citation is the thing that was read, not a remembered fact. A row
whose evidence could not be confirmed is not marked `TRUE`. The 2026-09-01 copy-honesty
pass moved the remaining `OVER-CLAIMED` generation-on-chain tags to `CHANGED`.

**Amended 2026-09-03 (#1807)** with the paper-trading section below — the one place the
cancelled mainnet cutover ([#1240](https://github.com/aprin-labs/archimedes/issues/1240),
owner call 2026-08-30) had left a live claim standing on four user-facing surfaces. Those
rows were measured against `main` on 2026-09-03; no other row was re-measured.

**Amended 2026-10-01 (#1432)** with the policy-pages section below (`/privacy` and
`/terms`), measured against `main` at `3591cd79` plus the live stack (task definition
`archimedes-backend:281`, which runs that commit). The footer row under Landing gained
the free-allowance precision; no other row was re-measured. A second audit pass the same
day applied 23 more findings: sentences on both pages, and three entries in the storage
inventory the Privacy page points to (`ui/src/storage-consent.js`, rendered on
`/security`). The five rows from "Our API still accepts a vault-deployment request"
through "Storage inventory", and the amended rows above them, record it.

**Amended 2026-10-03 (#1432 final pass)**, measured against `main` at `1f355a28` plus the
live stack (task definition `archimedes-backend:285`, which runs that commit): #1927 gated
the vault-create and marketplace publish/subscribe routes, #1926's purge ran in prod, and
the manual Aurora snapshots and the EC2 server's EBS snapshot were deleted. The policy-page
rows below whose sentence changed or went away were re-measured, the owner's
erase-and-copy-on-request commitment was added to the deletion row, and every
policy-page line citation was re-pointed at the edited files.

**Amended 2026-10-03 (#1432 audit round 1)**, against the same `1f355a28` and
`archimedes-backend:285`: the first audit round's confirmed findings. The row headed
"Audit round 1 (2026-10-03)" in the policy section records the disclosures it added; the
deletion, docs-site and roadmap-gate rows were amended; every policy-page line citation was re-pointed again; and four rows
outside that section whose line citations had drifted (the census row under Landing, the
three-layer guard row under the Security page, the sitemap row and the Explore row) were
re-pointed. The sitemap row was also re-measured: this branch added `/privacy` and
`/terms` to the sitemap and to `PUBLIC_PATHS`. The census citation was already off on
`main`. No other row was re-measured.

**Amended 2026-10-03 (#1432 audit round 2)**, against the same `1f355a28` and
`archimedes-backend:285`: the second audit round's confirmed findings. A new row headed
"Audit round 2 (2026-10-03)" in the policy section records what it corrected; the deletion row, the Account Settings
deletion-copy row and the "Does Archimedes trade for me?" row under Landing (whose
vault-route sentence predated #1927's gate and cited the wrong line; the same text is on
`main`) were amended; the profile-visibility and visitor-marker rows gained the guards
they lacked; and every line citation into the edited files was re-pointed. No other row
was re-measured.

**Amended 2026-10-03 (#1432 final triaged fixes)**, against the same `1f355a28` and
`archimedes-backend:285`: the last triaged set of findings. The row headed "Final triaged
fixes (2026-10-03)" in the policy section records them. The round-1 row (whose bounce
sentence came back off the page), the deletion, vault-route, storage-inventory and round-2
rows, and the policy section's preamble were amended. Outside that section, five rows had
citations corrected: the three-layer guard row under the Security page, whose nginx lines
held unrelated comments; the Known limits "No execution" row and the README
vault-execution row, which cited `vaults_routes.py:330` (a line inside the rigor helper)
for the create route and described that route only as existing, although #1927 gated it;
and the "Does Archimedes trade for me? No." row under Landing and the "Paper trading is
free" row under the Security page, which cited `POST /api/paper/deployments` at
`paper_routes.py:92`, a line in its spec helper. `POST /api/vaults/metadata` is gated by
the companion PR `dbrowneup/1432-gate-vault-metadata`, not by this branch, so #1432 must
merge after that PR or together with it. Every line citation into the edited files was
re-pointed. No other row was re-measured.

**Amended 2026-11-01 (#1432 local-first rewrite)**, measured from code and config against
`origin/main` at `b10453b2`: the hosted site is being shut down and the repo is now
open-source and local-first, so `/privacy` and `/terms` became short notices about the
software. The policy section was rewritten. Its hosted-service rows collapsed into one
`RETRACTED` row, with the old rows kept in this file's history at `c4f27093`. The
sign-in-notice row is new, and the closure sentence the owner's teardown session must
confirm is recorded as not a row. Outside that section, only the Landing footer row's
closing clause changed: `/terms` no longer states the free allowance. The new pages no
longer describe any vault route, so they add no dependency on the companion
`dbrowneup/1432-gate-vault-metadata` PR. The older amendments' statements of that
dependency describe the pages as they were. No other row was re-measured.

**Amended 2026-11-01 (local-first copy pass)**, measured from code and config on the
branch `dbrowneup/local-first-copy-pass` (from `dbrowneup/privacy-terms` at `026ca0fb`),
plus one anonymous probe of the hosted site the same day. The command-line client and the
MCP server now default to the local stack, so the Privacy page's sentence about their
hosted default was deleted. The public footer, the Landing FAQ's fee answer and the
Security page's payment sentences described the hosted deployment's paywall (on, settling
for real); they now state the software's default (off). The Docs links point at the
repository's `docs/` folder. The app-shell footer labels `/terms` "License and terms". The
top of `README.md` is local-first. Every row whose sentence changed was rewritten, the new
README rows were added, and every line citation into an edited file was re-pointed by
content. The README says the hosted site "is being shut down", which is true today (deploys are
off; the teardown is in progress). It does not say the site has closed or what became of
its data. No other row was re-measured.

## How to read a row

| Status | Means |
|---|---|
| `TRUE` | The claim, as it reads on the surface today, is backed by the cited live path. |
| `CHANGED` | The surface used to over-claim. The row records what it said, and the PR/commit that narrowed it. |
| `RETRACTED` | The claim was removed outright, and a guard exists to stop it coming back. |
| `OVER-CLAIMED` | Still live, still wrong, not fixed by this ledger. These are the open ones. |
| `PENDING VENDOR CUTOVER` | The decision record is merged and the mechanism is built; the owner has not yet applied the deploy action that switches a seam's default vendor. |

Two things this file deliberately does **not** do. It does not quote a curated-library
pass count — `CLAUDE.md` forbids it and the live gate is the only authority. And it does
not fix copy: `OVER-CLAIMED` rows are findings, and the fix is a separate change per
[#1241](https://github.com/aprin-labs/archimedes/issues/1241)'s own sequencing.

## Method

Each row was checked by reading the surface, then reading the code path the surface
implies, then asking whether a visitor who believed the sentence would be right. Three
checks that recur:

- **Does the reader's own path reach it?** A capability that exists in `backend/` but is
  hidden behind `ROADMAP_SURFACES_ENABLED` is not a claim the shipped build makes — and a
  capability the shipped build *describes* but the reader cannot reach is over-claimed
  even when the route is real.
- **Is the fallback loud?** A number with a cached substitute behind it is a claim about
  the cache, not about the system.
- **Is there a guard?** A retraction with no guard is a retraction until the next rewrite.

---

## Landing — `ui/src/components/Landing.jsx`

| Claim | Status | What backs it |
|---|---|---|
| "Four independent checks run outside the generator, on persisted returns, so the thing being graded cannot influence its own grade" | `TRUE` | `backend/archimedes/services/live_rigor_gate.py:151` — `verdict_from_returns` grades a persisted series outside the generator. The four checks are named at `ui/src/components/Landing.jsx:43`. The second grading scale (`BacktestResult.passes_rigor_gate`) that made this false was deleted in #1242 (`d7073f13`). |
| "Four verdicts, not two" — `pass` / `fail` / `pending` / `degenerate`, and a pass never rounds up | `TRUE` | `backend/archimedes/services/live_rigor_gate.py:54` defines `DEGENERATE`; `:92` makes `passes` a plain bool set `True` only by the `pass` constructor, so `pending` and `degenerate` are fail-closed. |
| Each rigor card states its own limit (DSR at the 95% one-sided level, PBO is a selection-set property, OOS is one chronological hold-out with no purge gap) | `TRUE` | `ui/src/components/Landing.jsx:28` documents each `limit` string against the module that computes it; the bar is `DSR_P_BADGE_MIN` in `backend/archimedes/services/rigor_profiles.py` — one definition since #1794, guarded by `backend/tests/test_single_dsr_bar.py`. |
| "Board-level correction — ranking N strategies is counted as N tests", Benjamini–Hochberg at α = 0.05, advisory, never flips a verdict | `TRUE` | `backend/archimedes/services/rigor_evaluator.py:475` (`DEFAULT_BOARD_FDR_LEVEL = 0.05`) and `:486` (`compute_board_level_fdr`, advisory by its own docstring). Served publicly on `GET /api/leaderboard` — `backend/archimedes/api/leaderboard_schemas.py:82`, and `backend/archimedes/api/leaderboard_routes.py:175` never 401s (anonymous callers are served the curated scope — the endpoint's own `scope` docstring says so in as many words). |
| "Inspect — nothing is discarded ... a fail as durably as a pass" | `CHANGED` | Was "every run leaves a reasoning trace bound to the chain". Retracted 2026-08-30; the retracted wording is pinned in `ui/test/public-visuals.test.js`. A generation run computes a keccak provenance hash (`backend/archimedes/agents/generation_pipeline.py:1635`) whose own comment says it is "mirrored on-chain in v1.5" (`:1614`) — i.e. not today. The only writers to the registry are the agent rebalance tick's `_commit_trace` (`backend/archimedes/chain/agent_runner.py:1441`) and `_reveal_trace` (`:1596`), which no generation run reaches. |
| "Does Archimedes trade for me? No." | `TRUE` | The reachable act-on step is `POST /api/paper/deployments` (`backend/archimedes/api/paper_routes.py:114`), which is simulated. `POST /api/vaults/create` (`backend/archimedes/api/vaults_routes.py:351`) answers 404 unless `FEATURE_ROADMAP_SURFACES` is `"true"` (`:362`, #1927; unset in `archimedes-backend:285`), and its UI journey is off the shipped build — `ui/src/featureFlags.js:57` lists `portfolio` / `vault-detail` in `ROADMAP_PAGES`, and `:28` defaults `ROADMAP_SURFACES_ENABLED` to false. |
| "A failing strategy stays a failing strategy. Paper-trading one is allowed. Relabelling one is not." | `TRUE` | `backend/archimedes/api/paper_routes.py:92` checks ownership and that the stored spec still validates — and nothing else, so there is no rigor precondition to claim. The verdict itself is computed server-side (`live_rigor_gate.py:151`), so it cannot be relabelled from the browser. |
| Every vault / non-custodial / custody claim on this page | `RETRACTED` | #1469. `ui/test/roadmap-copy.test.js` source-scans this file for `/vault\|non-?custodial\|custody/i` with no carve-outs — which is why the file's own comments are written around the words. |
| "Arc census live — ≥N reported instances" | `TRUE` | `ui/src/components/Landing.jsx:484` renders "Live census unavailable · No cached count substituted" when `GET /api/config/contracts` fails or the pool count is unreadable. The count is a floor by construction (`:17` scopes it to five core fields). |
| The page quotes no strategy pass count and no performance number | `TRUE` | The only percentage on the page is "70/30" — a methodology parameter, not a result. Deliberate; see `ui/src/components/Landing.jsx:94`. |
| Footer / announcement — "No real funds" | `CHANGED` | Was an unqualified "No real funds" on `PublicLayout.jsx` and the Landing footer, which read as *nothing you sign moves value* after the hosted site's generation paywall shipped (`dry_run: false`). Narrowed to "No mainnet money" in the announcement (`ui/src/components/PublicLayout.jsx:13`). The shell footer's second sentence changed again in the 2026-11-01 local-first copy pass: it read "Generation fee is real testnet USDC", true of the hosted deployment (`infra/ecs.tf`) and false for a local install. It now reads "Generation is free unless the operator turns on the testnet USDC paywall" (`ui/src/components/PublicLayout.jsx:151`): the template sets `GENERATION_PAYMENT_REQUIRED=false`, and `backend/archimedes/services/generation_payment.py:78` turns the paywall on only for the literal `"true"`. The Landing page carries no footer of its own (`ui/test/policy-pages.test.js`). Guard: `ui/test/public-visuals.test.js` pins the old wording's absence, the new line, and the template default. |
| FAQ — "Is this running with real money? No mainnet money ... Generation is free by default: the paywall is off unless the operator turns it on, and then it asks for a testnet USDC payment. GET /api/generate/quote gives this instance's answer." | `CHANGED` | Was "Generation fee is real testnet USDC. Read GET /api/generate/quote (prod answers dry_run: false)", the hosted deployment's settings. Changed in the 2026-11-01 local-first copy pass (`ui/src/components/Landing.jsx:195`). Off by default: `backend/archimedes/services/generation_payment.py:78`, read against the template's `GENERATION_PAYMENT_REQUIRED=false`. When it is on, `enforce_generation_payment` (`:200`) answers 402 past the free allowance. The quote route takes no credential (`backend/archimedes/api/generate_routes.py:392`) and reports the instance's own `payment_required` (`backend/archimedes/services/generation_payment.py:163`). Guard: `ui/test/public-visuals.test.js`. |
| Header and footer "Docs" links | `CHANGED` | Were `https://docs.archimedes-arc.com/`, the mkdocs site that is being shut down with the hosted service. Both now point at the repository's docs folder on GitHub, `https://github.com/aprin-labs/archimedes/tree/main/docs` (`ui/src/components/PublicLayout.jsx:42`, `:110`), which holds `docs/doc-index.md`. Guard: `ui/test/docs-link.test.js` requires both links and the folder, and forbids the docs-site host and `mkdocs.yml`'s `site_url` on the shell. |
| "Does Archimedes trade for me?" paper-trading split | `CHANGED` | Landing FAQ no longer names the unmerged `paper_agent_trades` table as a visitor path. `ui/src/components/Landing.jsx:184` says simulated paper trading grades `paper_daily_returns`, not on-chain execution proof. The two-book split from [PR #1704](https://github.com/aprin-labs/archimedes/pull/1704#issuecomment-5493036672) lives on machine surfaces (`agent.json` paper note), which state that table is not on main. |

## Security page — `ui/src/components/Security.jsx`

Rewritten 2026-08-31 (owner directive: the Security page "must be accurate and in good
alignment with the reality of the code"). This is the one public surface whose entire
content is claims about enforcement, so it gets a row per claim rather than a row per
theme. Every corrected sentence, every retraction, and the code literal behind each is
also pinned in `ui/test/security-claims.test.js` — the ledger records the reading, that
file makes the reading executable from the page's end, and the page therefore cannot
drift away from its own row without a red test.

| Claim | Status | What backs it |
|---|---|---|
| Hero status list — "Value at risk: **Testnet USDC only**" | `CHANGED` | Was "No real funds", which was written before the paywall shipped and read as *nothing you sign moves value*. Generation settles testnet USDC for real: `infra/ecs.tf:572` sets `GENERATION_PAYMENT_REQUIRED="true"` and `:556` sets `GENERATION_PAYMENTS_DRY_RUN="false"`, so `backend/archimedes/services/generation_payment.py:108`'s `settles_real_value()` is true in production. Confirmed anonymously against the live deployment on 2026-08-31: `GET /api/generate/quote` → `payment_required: true`, `dry_run: false`, `halted: false`. The row is `CHANGED` and not `RETRACTED` because the honest half survives — the value is faucet USDC on a testnet, not mainnet money. |
| Hero — "Generation can be made a paid call on testnet, with the paywall off by default" | `CHANGED` | Was "Generation is a paid call on this testnet", the hosted deployment. Changed in the 2026-11-01 local-first copy pass (`ui/src/components/Security.jsx:16`); same backing as the Payment row below. The old sentence is pinned absent in `ui/test/security-claims.test.js`. |
| Hero status list — "Paid surface: Generation (paywall off by default)" | `CHANGED` | Was "Generation (quoted live)", written for the hosted deployment, whose paywall `infra/ecs.tf` turns on. Changed in the 2026-11-01 local-first copy pass (`ui/src/components/Security.jsx:37`): the template sets `GENERATION_PAYMENT_REQUIRED=false`, and `backend/archimedes/services/generation_payment.py:78` turns the paywall on only for the literal `"true"`. The price is still not written into the copy: it comes from `GENERATION_PRICE_USD` (default `2.00`, `backend/archimedes/services/generation_payment.py:67`). |
| "Three boundaries. No role inflation." | `CHANGED` | Was four. The fourth was a vault-share-owner role over a path that has never run; removed by #1469 (`ui/src/components/Security.jsx:51`). |
| "Better Auth account. Canonical application identity. A connected wallet never creates or replaces the account session." | `TRUE` | `backend/archimedes/api/account_auth.py:73` — the session lookup forwards the request **cookie** and nothing else to the auth service, so no wallet header can participate in establishing a session. Restated in `docs/security/auth-model.md`. |
| "A five-minute, single-use EIP-4361 challenge proves control before a wallet is linked" | `TRUE` | `backend/archimedes/api/wallet_routes.py:22` — `_CHALLENGE_TTL = timedelta(minutes=5)`; `:350` rejects a challenge whose `consumed_at` is set, and `:389` consumes it in a conditional update that must affect exactly one row. |
| "Bounded internal agent ... a user session cannot assume that role, and that role cannot read or write another account's private records" | `TRUE` | `backend/archimedes/api/auth_guard.py:38` compares `X-Internal-Agent-Key` with `hmac.compare_digest` and **fails closed when the key is unset** (`:35-38`), so a misconfiguration cannot open the role. The scope half is true by enumeration: exactly two routes carry the dependency — `POST /api/traces/publish` (`backend/archimedes/api/traces_routes.py:398`) and `POST /api/agent/bootstrap-liquidity` (`backend/archimedes/api/agent_routes.py:209`). Both write; neither reads a per-account record. |
| Session — "Production session cookies are HttpOnly, Secure, and SameSite=Lax" | `CHANGED` | Was "Production cookies are HttpOnly and Secure", which under-stated a control that is actually there. `auth/auth.js:942` sets `useSecureCookies: production`; `:944` `httpOnly: true`; `:945` `secure: production`; `:946` `sameSite: 'lax'`. |
| Session — "nginx, the UI route guard, and FastAPI independently protect private surfaces. Two browse pages are deliberately anonymous, and the edge and client halves of that carve-out list are kept in lockstep" | `CHANGED` | Was "Four browse pages", true under #1194 rev d and false as of #1753 (the owner's call): the leaderboard and the strategy passport were gated, leaving Explore and Corpus. The three-layer half is unchanged and still true (`nginx/nginx.conf:516` `location ^~ /app`, whose `auth_request /_auth_session` is `:517`; `ui/src/App.jsx:147` the client bounce; FastAPI's own dependencies). The count: `nginx/nginx.conf:445-460` serves bare `/app`, `/app/explore` and `/app/corpus` with no `auth_request` — two pages, since bare `/app` is the SPA's alias for Explore — matching `ANON_APP_PAGES` at `ui/src/routes.js:60`. The "kept in lockstep" clause was carried by a comment in each file until #1753 and is now enforced: `backend/tests/test_nginx_anonymous_carve_outs.py` derives the expected nginx location set from `ANON_APP_PAGES` and fails in both directions. |
| Scope — "Profile, strategy, job, and linked-wallet reads resolve through the authenticated Better Auth user—not a client-supplied address." | `TRUE` | `backend/archimedes/api/user_routes.py:150` and `:164` filter on `owner_user_id == user.id`; `backend/archimedes/api/strategies_routes.py:681` builds its owner filter from `user.id`; `backend/archimedes/api/wallet_routes.py:385` scopes the challenge lookup to `user.id`. `docs/security/auth-model.md` records that selected-wallet headers are hints, honoured only when they resolve to a verified link on the current user. |
| Integrity — "Agent-only writes require a service credential." | `TRUE` | Same enumeration as the Bounded internal agent row: `backend/archimedes/api/auth_guard.py:35`, applied at `backend/archimedes/api/traces_routes.py:398`. |
| Payment — "When the paywall is on, generation is a paid call, bound to the wallet you proved ... The paywall is off by default ... a mismatch is refused before any settlement round-trip" | `CHANGED` | Was the unconditional "Generation is a paid call, bound to the wallet you proved", true of the hosted deployment (`infra/ecs.tf`) and false for a local install. Changed in the 2026-11-01 local-first copy pass (`ui/src/components/Security.jsx:148`, `:152`). Off by default: `backend/archimedes/services/generation_payment.py:78` turns the paywall on only for the literal `"true"`, and the template sets `false`. When it is on: `backend/archimedes/services/generation_payment.py:200` is the paywall; `:221` reads the `Payment-Signature` header and `:272-277` reject `payer_mismatch` when the authorization's `from` is not the account's linked wallet — **before** the `verify`/`settle` calls at `:282` and `:290`. The wallet-link precondition that makes "the wallet you proved" meaningful is `backend/archimedes/api/generate_routes.py:566` (`wallet_link_required`). Both the new sentences and the old one's absence are pinned in `ui/test/security-claims.test.js`. |
| Payment — "published anonymously at GET /api/generate/quote" | `TRUE` | `backend/archimedes/api/generate_routes.py:356` returns `generation_payment.quote()`, defined at `backend/archimedes/services/generation_payment.py:133`, which reports price, asset, chain, recipient, `dry_run` and `halted`. The route carries no auth dependency, and an anonymous fetch against the live deployment returned the full quote on 2026-08-31. |
| Payment — "An operator kill switch refuses service rather than serving the paid product unpaid." | `TRUE` | `backend/archimedes/services/generation_payment.py:218` raises 503 on `PAYMENTS_HALT` instead of accepting an unverified header, and the docstring at `:78` records why this rail differs from the marketplace one. |
| Payment — "Paper trading is free." | `TRUE` | By absence: `backend/archimedes/api/paper_routes.py` never references `generation_payment`, so `POST /api/paper/deployments` (`:114`) reaches no paywall. Pinned as an absence in `ui/test/security-claims.test.js`, because a presence pin on that file would prove nothing. |
| Edge — "a script policy admitting same-origin bundles plus one hashed inline bootstrap" | `CHANGED` | Was "a hash-restricted script policy", which invites the reading that every script is hash-pinned. The policy is `script-src 'self' 'sha256-…'` (`nginx/nginx.conf:72`): same-origin bundles, plus one exact hash for the pre-paint theme bootstrap, per the comment at `:190`. Verified as served: the live `Content-Security-Policy` response header on `/security` carries that exact value. |
| Edge — "HSTS, framing denied outright, and a permissions policy that turns off geolocation, microphone, and camera" | `TRUE` | `nginx/nginx.conf:209` (`Strict-Transport-Security`), `:210` (`X-Frame-Options: DENY`), `:213` (`Permissions-Policy`), all with `always` so they are emitted on error responses too. All three confirmed on the live response headers 2026-08-31. **Two sources, found by probing rather than reading:** `infra/cloudfront.tf:197-218` re-asserts HSTS and frame-options at the edge with `override = true`, so those two arrive from CloudFront and nginx both; CSP and `Permissions-Policy` are nginx-only. The tell is `Referrer-Policy` — `nginx/nginx.conf:212` sets `same-origin`, the wire says `strict-origin-when-cross-origin` (`infra/cloudfront.tf:215`). The page names no referrer policy, so nothing on it is wrong; recorded because an auditor reading only the nginx config would get a different answer than the response headers give. |
| Edge — "Two per-IP request-rate zones run at the edge — the tighter one on the credential surface — with tighter per-route limits on expensive endpoints behind them." | `CHANGED` | Was "separate read/write rate limits", which is not what is deployed. The two zones exist (`nginx/nginx.conf:140-141`, `api_read` 60r/m and `api_write` 20r/m) but are applied **by path, not by method**: `:358` puts `/api/auth/` on `api_write` and `:365` puts all of `/api/` — every POST included — on `api_read`. A reader who believed the old sentence would have thought `POST /api/generate/start` was rate-limited more tightly than a read; it is not, at the edge. What actually bounds it is the per-route slowapi limit at `backend/archimedes/api/generate_routes.py:389` (`5/minute`), one of ~30 in the tree. |
| Verdict — "A rigor verdict is computed server-side, never asserted ... on persisted returns, so the thing being graded cannot influence its own grade." | `TRUE` | `backend/archimedes/services/live_rigor_gate.py:150` — `verdict_from_returns` grades a persisted series outside the generator and fails closed to `pending`. Same backing as the Landing row. |
| Provenance — "The agent's rebalance decisions are content-hashed and anchored on Arc" plus its two stated limits | `CHANGED` | Was the unscoped "Reasoning records are content-hashed and anchored on Arc", which the previous revision of this ledger already had to correct **in prose** — the row read `TRUE` and then spent a sentence explaining what the page failed to say. That is a finding, not a verification, so the scope now lives on the page: `contracts/src/ReasoningTraceRegistry.sol` and the commit/reveal pair at `backend/archimedes/chain/agent_runner.py:1441` / `:1596` anchor the agent's tick. A generation run computes the same shape of hash but is not anchored — `backend/archimedes/agents/generation_pipeline.py:1614` says "mirrored on-chain in v1.5", and `backend/archimedes/api/traces_routes.py:189` excludes generation traces outright. A decision producing no transaction has no anchor: `README.md:227`. Re-hashing against the anchor is real — `backend/archimedes/api/traces_routes.py:505`. |
| Known limits — "Testnet: Arc public testnet only, and the USDC in play is faucet USDC." | `TRUE` | Chain `5042002`; the live quote reports `chain: arcTestnet`. The faucet framing matches the pricing rationale recorded at `infra/ecs.tf:573-576` (one $20 drip = ten generations). |
| Known limits — "No execution: Archimedes does not trade with capital today. Generation (free unless the paywall is on), the rigor gate, paper trading, and the agent's trace anchoring are what run" | `CHANGED` | The claim was true and the enumeration after it was not: it listed everything that runs *except* the one rail that moves value, so on 2026-08-31 it became "**Paid generation**, …". The 2026-11-01 local-first copy pass qualified that, because a local install's paywall is off by default (`backend/archimedes/services/generation_payment.py:78`): `ui/src/components/Security.jsx:236`. The reachable act-on step is still `POST /api/paper/deployments` (`backend/archimedes/api/paper_routes.py:114`), simulated. `POST /api/vaults/create` (`backend/archimedes/api/vaults_routes.py:351`) answers 404 unless `FEATURE_ROADMAP_SURFACES` is `"true"` (`:362`, gated by #1927; unset in `archimedes-backend:285`), as does `POST /api/vaults/metadata`, gated by the companion PR `dbrowneup/1432-gate-vault-metadata` (#1432 must merge after it or together with it); the journey is also off the shipped build (`ui/src/featureFlags.js:28`). |
| Known limits — "Charges: the generation paywall is off by default, so by default generation moves no USDC. GET /api/generate/quote reports its state anonymously. An operator who turns it on charges testnet USDC per run, past the free allowance, to the recipient address they set; in dry-run, no value moves. It is a fee, not a balance held for you, and there is nothing there to withdraw." | `CHANGED` | Was "Live charge: the generation paywall is on and not in dry-run, so a signed payment settles testnet USDC for real ... A settled fee lands in a platform-operated wallet Archimedes signs for through its payment provider", the hosted deployment (`infra/ecs.tf` pins `GENERATION_PAYMENT_REQUIRED`, `GENERATION_PAYMENTS_DRY_RUN` and the recipient). Changed in the 2026-11-01 local-first copy pass (`ui/src/components/Security.jsx:242`). Off by default: `backend/archimedes/services/generation_payment.py:78`, against the template's `false`. The quote carries `payment_required` and `dry_run` (`:163`, `:169`) on a route that takes no credential (`backend/archimedes/api/generate_routes.py:392`). The recipient is the operator's `GENERATION_PAYMENT_RECIPIENT` (`backend/archimedes/services/generation_payment.py:124`), and turning the flag on without one is a 503 (`:212`). In dry-run a presented payment is accepted without verify or settle (`:229`). **The page claims no dry-run default, deliberately:** the template's `GENERATION_PAYMENTS_DRY_RUN=` is an empty value, which `_payments_dry_run()` (`:92`) reads as set and not true, so a local install that turns the paywall on settles for real unless its operator also sets that variable. The recipient is still described mechanically because `ui/test/roadmap-copy.test.js:198` scans this page for `/vault\|non-?custodial\|custody/i`. |
| Known limits — "Some current oracle and risk inputs are mock data" | `TRUE` | Listed under Known limits (`ui/src/components/Security.jsx:256`), consistent with the Explore page's own source labelling (`ui/src/components/Explore.jsx:381`). |
| Known limits — "Immutable contracts: a defect requires a new deployment and migration rather than an in-place upgrade." | `TRUE` | No proxy, `delegatecall`, or upgradeable base appears anywhere under `contracts/src/`; `contracts/src/ReasoningTraceRegistry.sol:67` is a plain constructor + `Ownable`. The single "upgradeable" mention in the tree, `contracts/src/PriceOracle.sol:69`, is about an **external** Chainlink feed, not an Archimedes contract. |
| Known limits — "No independent security audit ... is claimed by this page" | `TRUE` | A disclaimer, correct as written (`ui/src/components/Security.jsx:264`). |
| Evidence links resolve to the code that enforces the control | `TRUE` | Seven links as of 2026-08-31: `/architecture`, `docs/claims-ledger.md`, the live `GET /api/generate/quote`, `docs/security/auth-model.md`, `contracts/src/ReasoningTraceRegistry.sol`, `backend/archimedes/services/generation_payment.py`, and `backend/archimedes/services/live_rigor_gate.py` — all present. The quote link is the one that matters most: it lets a reader check the page's payment claims against the running system without an account. |

**Not verified, and therefore not claimed.** Two things a reader might expect this section to
settle and it does not. (1) Whether the agent rebalance tick is *executing* in production
today — the anchoring path is real and cited above, but confirming it is currently running
needs prod access this audit deliberately did not use, so the page says what the mechanism
does and never says how often it fires. (2) Whether `INTERNAL_AGENT_API_KEY` is populated in
prod SSM (`infra/scripts/setup-ssm-secrets.sh:47`); it does not change the claim either way,
because `backend/archimedes/api/auth_guard.py:38` refuses every request when the key is
unset, so the boundary holds under both answers.
## `README.md`

| Claim | Status | What backs it |
|---|---|---|
| "Archimedes is open-source software you run yourself, against Arc testnet. The hosted site at archimedes-arc.com is being shut down, so the way to use it is to run your own instance." | `TRUE` | `README.md:18`, added in the 2026-11-01 local-first copy pass. The repo is released under the Unlicense, and `docker-compose.yml` runs the whole stack. The shutdown is the owner's 2026-11-01 decision and is under way: the four AWS deploy workflows are disabled and the deploy variables are off (checked 2026-11-01), while an anonymous probe the same day found the site still serving. When the teardown has taken it down, this sentence can say "has closed", after a re-probe. The README does not say what became of the site's data (see "Not a row" under the policy pages). |
| "Run it locally": `cp .env.example .env`, generate a `BETTER_AUTH_SECRET`, `docker compose up -d --build`, then open http://localhost:8080 | `TRUE` | Moved unchanged from the Quickstart to the top of the file in the 2026-11-01 local-first copy pass (`README.md:21`). The template carries `BETTER_AUTH_SECRET` and `ARCHIMEDES_HTTP_PORT=8080`, and `docker-compose.yml:170` publishes nginx on `${ARCHIMEDES_HTTP_PORT:-80}:8080`. |
| Vault execution — "**Vault execution** and **monitor** are roadmap … When it ships, a strategy that survives the gate *will be* deployable into a non-custodial vault on Arc … Today it will not." | `CHANGED` | `README.md:51`, rewritten to the future tense in the 2026-08-31 README refresh, which is what #1469's scrub had done for Landing, `/security`, and `ui/index.html` without reaching the README. The facts behind the retraction are unchanged: the route exists (`backend/archimedes/api/vaults_routes.py:351`) and since #1927 answers 404 unless `FEATURE_ROADMAP_SURFACES` is `"true"` (`:362`), the journey is gated off every shipped surface (`ui/src/featureFlags.js:57`), and zero user vaults have ever been deployed. `README.md:220` repeats the limitation in the limitations list. The pin that held this row honest was removed from `backend/tests/test_claims_ledger.py` in the same commit, per that test's own instruction. |
| "How many strategies in the curated library currently pass is **unestablished** — this file will never quote a count" | `TRUE` | `README.md:63`. Matches `CLAUDE.md` § corollaries. No count appears anywhere in the file. |
| "The corpus is arXiv preprints, not peer-reviewed papers … Do not freeze that count in prose; the corpus probe can timeout" | `CHANGED` | Was "The corpus is 10,000 arXiv preprints". `README.md:202` now points at `/health` `corpus_papers` / `corpus_db_count` and refuses to freeze a number. `backend/archimedes/main.py:1586` still publishes `corpus_papers`; `:1473` / `:1604` publish `corpus_embedded_at_rest`; the rerank cap is at `:1606`. |
| "The knowledge graph is not built ... `GET /api/corpus/graph` refuses with 503 `kb_artifact_not_found`" | `TRUE` | `backend/archimedes/api/corpus_routes.py:276` raises exactly that; `backend/archimedes/main.py:1110` derives `corpus_kg_built` from a live entity count rather than a constant. |
| "Arc has no mainnet yet; mainnet launch, real-funds custody, and the regulatory architecture are roadmap" | `TRUE` | `README.md:72`, true on 2026-08-31. **Date-gated:** #1241 records this as false from Sept 16. Re-check on that date; nothing in CI will tell you. |
| "Payments are real USDC on Arc when an operator turns them on; marketplace settlement is stubbed behind `PAYMENTS_DRY_RUN` pending mainnet" | `CHANGED` | Was the unconditional "Payments are real (USDC on Arc)", true of the hosted deployment. Qualified in the 2026-11-01 local-first copy pass (`README.md:65`): the generation rail is off unless `GENERATION_PAYMENT_REQUIRED` is `"true"` (`backend/archimedes/services/generation_payment.py:78`). Marketplace settlement rides the separate `PAYMENTS_DRY_RUN`, which defaults to dry-run (`backend/archimedes/main.py:143`; the template sets `true`). |
| "A local install charges nothing for generation by default (`GENERATION_PAYMENT_REQUIRED=false` in `.env.example`); `GET /api/generate/quote` reports what your instance does" | `CHANGED` | Was "Generation still settles real testnet USDC — read `GET /api/generate/quote` (prod answers `dry_run: false`)", the hosted deployment. Changed in the 2026-11-01 local-first copy pass (`README.md:70`), backed by `backend/archimedes/services/generation_payment.py:78` and the quote route, which takes no credential (`backend/archimedes/api/generate_routes.py:392`). |
| "Not every reasoning trace is anchored on-chain ... check `arc_tx_hash` before treating a trace as anchored" | `TRUE` | `README.md:226`. This is the general form the `ui/index.html` meta tags now match. |
| CLI exit codes are a stable contract, published machine-readably | `CHANGED` | **Was `OVER-CLAIMED`: the published table omitted a code the CLI actually exits with.** `cli/src/archimedes_cli/exits.py:33` defines `INCOMPLETE = 4` (added for #1481) and `cli.py:944` exits with it on the real `verify` path, but `cli/src/archimedes_cli/manifest.py:19` published only `0`/`1`/`2`/`3` — so an agent branching on the machine-readable table hit an undocumented `4`. Fixed by #1705 (issue #1697), the `archimedes generate` work, which published `4` and added `5`–`8` for the new command's outcomes (`exits.py:48` `PAYMENT_REQUIRED`, `:77` `STILL_RUNNING`). The completeness defect is now guarded rather than pinned: `test_claims_ledger.TestLedgerClaimsMatchTheTree::test_the_published_exit_code_table_covers_every_code_the_cli_defines` derives the expected set from `exits.py`, so a future code added without publishing it fails. The originally-documented four were always accurate, and the `AUTH`-reuses-`2` rationale is sound (`exits.py:25`); the defect was completeness, not correctness. |
| "`archimedes login` signs in to http://localhost:8080 unless `--api-url` or `ARCHIMEDES_API_URL` names another server, and later commands go back to the server the session was cached against" | `TRUE` | New in the 2026-11-01 local-first copy pass (`README.md:127`). `cli/src/archimedes_cli/cli.py:54` is the default; `:111` takes the flag or variable, then the cached session's URL, then the default. `cli/tests/test_api_url_resolution.py` binds the default to `docker-compose.yml` and the template's `ARCHIMEDES_HTTP_PORT`. |
| Documentation — "Browse the tree itself on GitHub at https://github.com/aprin-labs/archimedes/tree/main/docs" | `CHANGED` | Was "The same tree is published, curated, at https://docs.archimedes-arc.com", the docs site that is being shut down with the hosted service (`mkdocs.yml` still names it as `site_url`). `README.md:171`; the folder holds `docs/doc-index.md`. |

## Agent surfaces — `ui/public/llms.txt`, `ui/public/.well-known/agent.json`

| Claim | Status | What backs it |
|---|---|---|
| "executed in non-custodial USDC vaults on Arc testnet" (llms.txt header) | `CHANGED` | Was the highest-severity open row here: present tense, on the surface built for agent consumers, while no user vault has ever been created. #1650 rewrote it to "Executing strategies in non-custodial USDC vaults on Arc is roadmap, not shipped" — `ui/public/llms.txt:10`, mirroring `CLAUDE.md`'s own framing — and the paper-trade step of the agent journey with it. The mention is kept, in the future tense, on purpose. |
| `agent.json` `description` — "executed in a non-custodial USDC vault on the Arc testnet" | `CHANGED` | Same sentence, same fix, then split the paper books: `ui/public/.well-known/agent.json:4` says simulated paper deployments are live (`paper_daily_returns`), `paper_agent_trades` (PR #1704, not on main) is not a live path, generation is not anchored, and vault execution is roadmap, not shipped. The served twin — `/api/agent/manifest`'s `blurb`, `backend/archimedes/api/agent_manifest_routes.py:164` — carries the identical string, and `backend/tests/test_agent_manifest_static_consistency.py` asserts the two are equal and that neither says "executed in". |
| `agent.json` `skills[deploy-vault].status: "live"` / `endpoints.deploy.status: "live"` | `CHANGED` | #1650 moved the *skill* to `status: "roadmap"` while leaving `endpoints.deploy.status: "live"` as route-truthful. The 2026-09-01 copy-honesty pass moved the endpoint too: `ui/public/.well-known/agent.json` `endpoints.deploy.status` is `"roadmap"`, matching the served manifest (`backend/archimedes/api/agent_manifest_routes.py`). Marketplace skills `publish` / `subscribe` moved from `"live"` to `"roadmap"` in the same pass — marketplace is not a public surface. `backend/tests/test_agent_manifest_static_consistency.py` pins deploy / marketplace / monitor as `"roadmap"`. |
| `agent.json` `endpoints.paper.note` — "deploy is also live and puts real capital on-chain" | `CHANGED` | Testnet faucet USDC is not real capital. Rewritten by #1650, then split: `ui/public/.well-known/agent.json:114` `status: "live"` names simulated `POST /api/paper/deployments` whose graded book is `paper_daily_returns`. `paper_agent_trades` is PR #1704, not on main, and is not what that status advertises. Vault execution is roadmap, not shipped. |
| `agent-registration.json` `description` — same present-tense vault sentence | `CHANGED` | Not previously in this ledger, and it should have been: `ui/public/.well-known/agent-registration.json:4` carried the agent-card sentence verbatim. #1650 rewrote the prose to match and touched nothing else in the file — the ERC-8004 semantics (`registrations`, `supportedTrust`, `erc8004`, `active`) are #1626's lane and are unchanged. |
| The machine surfaces cannot regress to present-tense vault execution | `TRUE` | `ui/test/roadmap-copy.test.js:290` scans `ui/public/`'s six machine surfaces and requires every sentence mentioning a vault, custody, or "real capital" to carry `roadmap` or `not shipped` in that same sentence; `:305` and `:314` are the two patterns. Anti-vacuity is explicit: the six sentences removed by #1650 are kept verbatim in the file and each must still trip the guard, the identifier exemption is asserted not to swallow any of them, and a separate test requires the roadmap mention to still be *present* so a scrub cannot pass by deletion. |
| `agent.json` `endpoints.generate.note` — quote is public and authoritative; prod answers `payment_required: true`, `dry_run: false`, `$2.000000`; the code defaults are the opposite | `TRUE` | `backend/archimedes/services/generation_payment.py:56` reads `GENERATION_PAYMENT_REQUIRED` and defaults off; `:72` defaults dry-run on. `backend/archimedes/api/generate_routes.py:449` is the 402 that carries the x402 requirements. |
| "x402-gated strategy access ... no endpoint returns 402 with payment requirements" (the 2026-08-10 retraction) | `CHANGED` | The retraction is itself out of date. `POST /api/generate/start` now returns 402 carrying x402 requirements (`backend/archimedes/api/generate_routes.py:449`), and 409 `wallet_link_required` first (`:459`). What remains true is the marketplace half: `GET /api/marketplace/published/{strategy_id}` is public (`backend/archimedes/api/marketplace_routes.py:779`). |
| `agent.json` `erc8004` — "NO ERC-8004 identity, reputation, or validation claim is made" | `TRUE` | `ui/public/.well-known/agent.json` — self-disclosing, with `agentId` and `tokenURI` null, `status: "registration_pending"`, and the reason spelled out in the `note`. No `register()` transaction has been sent. |
| `agent.json` `endpoints.marketplace.note` — "does not assert that a subscription's recurring USDC charge settles for real" | `TRUE` | `ui/public/.well-known/agent.json`. Correctly separates the two dry-run switches — `backend/archimedes/services/generation_payment.py:64` documents the split that let the generation rail go live without un-drying marketplace settlement — and declines to claim the one no public endpoint publishes. |
| `agent.json` / llms.txt `POST /api/rigor/verify` — PBO and look-ahead "always report `not_evaluable`", `passes` is a capped quorum | `TRUE` | `backend/archimedes/api/rigor_verify_routes.py:47` states the capping contract; `:787` hard-codes both legs to `not_evaluable`; `:803` makes `passes` require every runnable leg to have run and passed. |
| `ui/public/sitemap.xml` lists only routes that render real content for an anonymous visitor | `TRUE` | Seven `<loc>` entries (`/leaderboard`, once the sixth, was removed by #1753 when the page was gated — a crawler following it now gets a 302 to `/sign-in`), checked one by one against the two allow-sets that between them cover all seven: `ui/src/routes.js:12` (`PUBLIC_PATHS` — `/`, `/architecture`, `/privacy`, `/terms`, `/security`) and `:60` (`ANON_APP_PAGES` — `explore`, `corpus`). **Verified by reading, not by a guard, and the row says so:** `ui/scripts/check-sitemap.mjs` enforces only the *forward* direction (every public route appears in the sitemap) and its own header documents the reverse — "every sitemap `<loc>` is actually anonymous-accessible" — as **NOT enforced**; `ui/test/sitemap.test.js` pins one specific exclusion (the admin-only `/insights` never appears anywhere in the served bytes), not this property. Claiming a test pins this would be the defect this ledger exists to catch. |

## `ui/index.html` — meta, OpenGraph, Twitter card, JSON-LD

| Claim | Status | What backs it |
|---|---|---|
| Vault / non-custodial framing in all four cards | `RETRACTED` | #1469 rewrote the meta description, OG card, Twitter card and JSON-LD together, on the reasoning that a claim retracted on the page but left in the share card is still shipped. Guarded by `ui/test/roadmap-copy.test.js`. |
| "records the whole decision on Arc public testnet" (meta description) / "check the reasoning trace on-chain" (Twitter) / "on-chain reasoning provenance" (JSON-LD) | `CHANGED` | Was `OVER-CLAIMED`. The three tags now describe generate → rigor-gate → inspect, and say generation is not anchored: `ui/index.html:9`, `:57`, `:76`. Guarded by `ui/test/public-visuals.test.js`. `README.md:226` already had the accurate wording. |
| "A research-grounded strategy-generation instrument with visible selection-bias checks" | `TRUE` | `ui/index.html:76`. The selection-bias checks are visible and live — `backend/archimedes/services/rigor_evaluator.py:486` computes the board-level correction and `backend/archimedes/services/live_rigor_gate.py:151` the per-strategy verdict. |

## `ui/src/components/Architecture.jsx`

| Claim | Status | What backs it |
|---|---|---|
| "with every decision hashed on-chain before anything acts on it" | `CHANGED` | Now only in the `ROADMAP_SURFACES_ENABLED` branch of the hero ternary (`ui/src/components/Architecture.jsx:92`), and that flag defaults false (`ui/src/featureFlags.js:28`), so the shipped build does not say it. |
| "Non-custodial by contract, not by promise" (rendered unconditionally) | `RETRACTED` | Replaced by `OnChainExecutionRoadmap` — "Non-custodial vault execution is on the roadmap; not yet live" (`ui/src/components/Architecture.jsx:615`). The "Live user vaults on Arc" hero tile was removed rather than left reporting a live zero. |
| "x402-gated strategy access" / the nanopayment marketplace section | `CHANGED` | The section still exists but is flag-gated off the shipped build (`ui/src/components/Architecture.jsx:1241`), and its own honesty note says fee settlement is in dry-run (`:833`). |
| "before anything can run live" (flag-off hero) | `CHANGED` | Was present-tense vault implication. Flag-off hero is now generate → rigor-gate → explore, with vault execute/monitor named as roadmap: `ui/src/components/Architecture.jsx:93`. |
| "Not one paper-derived alpha strategy clears our bar" (curated pass count of zero) | `CHANGED` | Removed. The page now says the pass count is unestablished and will never quote a count: `ui/src/components/Architecture.jsx` rigor honesty note. `CLAUDE.md` forbids quoting a curated pass count, including zero. |
| Xia Hierarchy of Truth — "the rebalance loop reads holdings from chain … before the trade is committed" | `CHANGED` | Was written as a live path. Copy now future-tenses the chain-holdings half and says the path is not live: `ui/src/components/Architecture.jsx:932`. Guarded by `ui/test/public-visuals.test.js`. |
| Honesty-ledger rows read from `/health` rather than being asserted | `TRUE` | `ui/src/components/Architecture.jsx:1093` renders "live value unavailable" on a health error instead of substituting a value; the corpus panel's contract is documented at `:842`. |
| "the full trace is published (IPFS-pointed)" | `RETRACTED` | #1526. Reveal copy now says the trace is published off-chain and "we do not pin traces to IPFS" (`ui/src/components/Architecture.jsx:793`). Guarded by `ui/test/ipfs-pinning-copy.test.js`. Live `storagePointer` is empty ([`docs/adr/ipfs-pinning-not-live.md`](adr/ipfs-pinning-not-live.md)). |

## `ui/src/components/AccountSettings.jsx`

| Claim | Status | What backs it |
|---|---|---|
| "Anything already published to a blockchain or pinned to IPFS stays there" | `RETRACTED` | #1526. Deletion copy now says chain writes stay; it does not claim an IPFS pin (`ui/src/components/AccountSettings.jsx:898`). Guarded by `ui/test/ipfs-pinning-copy.test.js`. |

## Policy pages — `ui/src/components/Privacy.jsx`, `ui/src/components/Terms.jsx`

Rewritten 2026-11-01 (#1432) for the open-source, local-first project. The hosted site and
its AWS deployment are being shut down, so there is no live stack left to measure. Every
row below was measured from code and config against `origin/main` at `b10453b2` (merged
into the branch at `c4f27093`). Both pages are dated 2026-11-01 and carry no draft banner
(`ui/test/policy-pages.test.js:162`). Every row is pinned by a guard in that file unless
the row names another. The full third-party inventory behind the Privacy page's lists
(every service, the switch that turns it on, and file:line) is summarised in the rows
below.

| Claim | Status | What backs it |
|---|---|---|
| "Archimedes is software you run yourself. APRIN Labs runs no hosted version of it and receives nothing from your installation: the code sends no telemetry, analytics or error reports to the project or to anyone else" | `TRUE` | No analytics or error-reporting SDK in `ui/src`, `backend/archimedes`, `auth` or `analytics-engine/src` (a grep for plausible, posthog, gtag, segment, mixpanel, sentry, datadog, newrelic, amplitude, hotjar, clarity, logrocket, fullstory, heap, bugsnag and rollbar finds only prose), and no OpenTelemetry exporter in either lockfile. Better Auth's telemetry package is a dependency (`auth/package-lock.json:396`, 1.6.25). Its `dist/index.mjs` enables it only when `BETTER_AUTH_TELEMETRY` or `options.telemetry.enabled` is set. That was read from the registry tarball, whose sha512 matched the lockfile. `auth/auth.js` sets neither, and no repo file names the variable. Visitor counting writes to the instance's own Redis: `backend/archimedes/services/funnel_store.py:49`, `:53`; `backend/archimedes/services/visitor_insights_store.py:58`, `:64`; `backend/archimedes/services/telemetry_store.py:32`, `:36`. The browser's landing beacon posts to the instance's API (`ui/src/App.jsx:126`). Page: `ui/src/components/Privacy.jsx:80`. Guard: `ui/test/policy-pages.test.js:290`. |
| "Your instance keeps its records in the Postgres database and the Redis cache that you run": accounts, linked wallets, what you make, payment receipts and credits when the rail is on, visitor counts; and "Every cookie and browser-storage key the web app writes ... is listed on the Security page" | `TRUE` | Accounts: `backend/archimedes/models/account.py:73` (`auth_users`), `:105` (`auth_sessions`), whose `ipAddress` and `userAgent` are `:114` and `:115`. The email log is `backend/migrations/versions/d4b1f7c8e206_auth_email_deliveries.py:110`. API keys are stored as `salt` + `secret_hash` (`backend/archimedes/models/api_key.py:97-98`). Receipts and credits: `backend/archimedes/models/payment_receipt.py:51` and `backend/archimedes/models/generation_credit.py:64`, written only past `backend/archimedes/services/generation_payment.py:78`. Paper decision records: `backend/archimedes/models/paper_store.py:242` and `backend/archimedes/services/redis_state.py:536`. "Records" is deliberate: services also write logs on the host, and the one log that carries personal data, the console mailer's, is disclosed in the email bullet below. The storage pointer resolves to `ui/src/components/StorageDisclosure.jsx:99`. Page: `ui/src/components/Privacy.jsx:91`, `:120`. Guard: `ui/test/policy-pages.test.js:313`. |
| With nothing configured, the software contacts Yahoo Finance (first when the server starts), the Arc testnet public RPC, Hugging Face (a model download when not cached) and jsDelivr (only the /docs page) | `TRUE` | yfinance is the default vendor for both seams (`backend/archimedes/services/market_data_provider.py:1060`, `:1087`; `analytics-engine/src/archimedes_analytics_engine/market_data.py:43`). The boot warmup primes the Explore cache from it (`backend/archimedes/services/request_path_warmup.py:88`, `:194`). The RPC defaults are `backend/archimedes/chain/client.py:148`, `ui/src/chain-config.js:25` (overridden by `VITE_ARC_RPC_URL` at `:79`) and viem's default transport (`ui/src/config.js:15`). Hugging Face: `backend/archimedes/services/paper_rag.py:195` (on by default) and `:286` (the load); the Docker image downloads the weights at build and runs with `HF_HUB_OFFLINE=1` (`backend/Dockerfile`). jsDelivr: `backend/archimedes/main.py:222-233` (docs on unless `PUBLIC_DOMAIN` is set) and `nginx/nginx.conf:179`. Page: `ui/src/components/Privacy.jsx:132`. Guard: `ui/test/policy-pages.test.js:325` ties each name to its code default. |
| The services contacted only when configured: the LLM provider chosen with `LLM_PROVIDER` ("The example .env file selects Bedrock, which is used only when AWS credentials are available"), Tiingo, CoinGecko and Pyth for the oracle runner, Circle, Google and GitHub sign-in, AWS SES / SSM / S3, and arXiv for the ingest scripts | `TRUE` | LLM: the template sets `LLM_PROVIDER=bedrock_converse`, and `backend/archimedes/services/llm_backend.py:360` falls back to canned output without AWS credentials. Anthropic is at `:121`, the compatible endpoint at `:190-191`, OpenAI at `:440`, and Ollama defaults to localhost at `:510`. Tiingo: `backend/archimedes/services/market_data_provider.py:461`, `:532`. The oracle runner is opt-in (`docker-compose.yml:220`); CoinGecko is its default crypto source (`backend/archimedes/chain/oracle_updater.py:121`, `:1275`) and Pyth needs `PRICE_SOURCE` (`backend/archimedes/services/price_source.py:38`, `:62`). Circle: `backend/archimedes/chain/circle_signer.py:26`, `:231`; `backend/archimedes/services/circle_service.py:17`; `backend/archimedes/services/generation_payment.py:78`; `ui/src/circle-wallet.js:33`, `:225`. OAuth: `auth/auth.js:31`. SES only under `EMAIL_MAILER=ses`, otherwise the log (`auth/mailer.js:70`, `:75`). SSM: `backend/archimedes/services/secrets_service.py:90`. S3: `backend/archimedes/services/kb_artifacts.py:36`. arXiv is called only by scripts (`scripts/bulk_ingest_arxiv.py:153`; `backend/archimedes/scripts/hydrate_corpus.py:129`); nothing in the running app calls `intake_from_arxiv` or `extract_from_paper`. Page: `ui/src/components/Privacy.jsx:155`. Guard: `ui/test/policy-pages.test.js:349`. |
| "Anything written to the blockchain is public and permanent", including anchored paper-trading reasoning records with the holdings before and after | `TRUE` | Anchoring needs both `PAPER_TRACE_ANCHOR` and the deployment's own opt-in (`backend/archimedes/models/paper_store.py:161`), and `reveal()` writes `portfolio_before`/`portfolio_after` (`backend/archimedes/services/paper_trace.py:114`). Page: `ui/src/components/Privacy.jsx:186`. Guard: `ui/test/policy-pages.test.js:349`. |
| "The web app serves its own fonts and loads no third-party scripts, fonts or analytics" | `TRUE` | Fonts: `ui/index.html:13` and `:20` preload `/fonts/dm-sans-latin.woff2` and `/fonts/inter-latin.woff2`, and the `@font-face` rules at `ui/public/theme.css:4` and `:11` load them same-origin from the directory that holds `ui/public/fonts/OFL-DM-Sans.txt`. The only scripts are the same-origin theme bootstrap `/theme-init.js` (`ui/index.html:69`), JSON-LD (`:70`) and `/src/main.jsx` (`:89`). The CSP's `connect-src` also allows four hosts that `ui/src` never fetches; that is an allowance, not a contact. Page: `ui/src/components/Privacy.jsx:192`. Guard: `ui/test/policy-pages.test.js:349`. |
| "One exception to these lists: the command-line client and the MCP server still default to archimedes-arc.com. Point them at your own instance with --api-url or the ARCHIMEDES_API_URL environment variable." | `RETRACTED` | Deleted in the 2026-11-01 local-first copy pass, because both clients now default to the local stack, `http://localhost:8080`: `cli/src/archimedes_cli/cli.py:54` and `mcp-server/src/archimedes_mcp/credentials.py:50`. That is the port `docker-compose.yml:170` publishes nginx on with the template's `ARCHIMEDES_HTTP_PORT=8080`, bound by `cli/tests/test_api_url_resolution.py`; `mcp-server/tests/test_credentials.py` pins the two defaults equal. Guard: `ui/test/policy-pages.test.js:387` requires both localhost defaults and forbids the sentence and any hosted address in the page body. |
| "If you run an instance for other people, telling them what it collects ... is your job. This page describes the software, not your deployment." | `TRUE` | A statement of scope: the page describes the code. Page: `ui/src/components/Privacy.jsx:200`. Guard: `ui/test/policy-pages.test.js:406`. |
| "Archimedes is free and unencumbered software released into the public domain under the Unlicense"; "provided 'as is', without warranty of any kind"; code it depends on or bundles keeps its own license | `TRUE` | The repo's LICENSE is the Unlicense, and the page links it and names it the full text. The warranty and liability sentences paraphrase its lines 16-22. The fonts carry their OFL texts (`ui/public/fonts/OFL-DM-Sans.txt`, `ui/public/fonts/OFL-Inter.txt`). Page: `ui/src/components/Terms.jsx:49`, `:54`, `:64`. Guard: `ui/test/policy-pages.test.js:431`, which also reads LICENSE. |
| "Research and testnet only": not financial advice; backtests and rigor verdicts are research outputs; "Paper trading is simulated ... No order is placed, and nothing is bought or sold"; built for the Arc public testnet | `TRUE` | Paper trading replays the graded backtest engine (`backend/archimedes/services/paper_trading.py:1`) and never touches the chain executor or the Circle signer (a grep of that file finds neither). Anchoring publishes a record, not a trade (row above). The chain default is the testnet (`backend/archimedes/chain/client.py:149`, `ui/src/chain-config.js:25`). Page: `ui/src/components/Terms.jsx:77`. Guard: `ui/test/policy-pages.test.js:446`. |
| "You are responsible for every key, wallet, fund and API credential you give an instance or connect to it ... and for following the terms of every service you connect" | `TRUE` | Every credential lives in the operator's own environment or browser. The project holds none, because it runs no service. Page: `ui/src/components/Terms.jsx:99`. Guard: `ui/test/policy-pages.test.js:455`. |
| The hosted-service policy and terms of 2026-10-03: our accounts database, the privacy@ relay to Gmail, SES mail and bounce handling, logs, the firewall, backups, the $2.00 paywall and Circle Gateway flow, the free allowance and daily caps, the deletion runbook, the 18-or-older rule, account suspension, the owner's seven promises, and Illinois law | `RETRACTED` | Each described the hosted deployment, which is being torn down, so each would now be false or govern nothing. Their rows, with their evidence, are in this file's history at `c4f27093`. **Illinois law was dropped rather than kept.** It chose the law for a contract between a user and the hosted service ("using the site means you accept these terms"). With no service there is no such contract, and the Unlicense has no choice-of-law clause (`Terms.jsx`'s header records this). Guards: `ui/test/policy-pages.test.js:472` (no privacy@, mailto, price, USDC, paywall, free generations, refund, age, suspension or service-promise text on either page), `:458` (no governing law or forum), `:157` (no draft banner or placeholder date). |
| The sign-in screen's notice | `CHANGED` | Was "By creating an account you agree to the Terms of Service and confirm you are 18 or older. The Privacy Policy explains what we keep." There is no service to agree to, and the age rule went with it. Now "Your account is stored in this instance's own database", with links to both pages (`ui/src/components/AuthPage.jsx:552`). The auth service writes to the instance's `DATABASE_URL` (`auth/auth.js:295`). Guard: `ui/test/policy-pages.test.js:496`, which keeps the notice above the OAuth buttons on both screens. |
| The app-shell footer's `/terms` link reads "License and terms" | `CHANGED` | Was "Terms". Relabelled in the 2026-11-01 local-first copy pass (`ui/src/components/Layout.jsx:474`) to match the public footer (`ui/src/components/PublicLayout.jsx:145`) and the page's own title. Guard: `ui/test/policy-pages.test.js:219`, on both shells. |

**Not a row:** what became of the former hosted service at archimedes-arc.com and its data.
Neither page says. The owner chose on 2026-11-01 not to wait for the teardown, and
`ui/test/policy-pages.test.js` fails if either page claims the service closed or its data
was deleted. Add a row when a sentence is added, after the teardown has actually deleted it.

## `docs/user-stories.md`

| Claim | Status | What backs it |
|---|---|---|
| "Vault execution reads as present tense throughout this doc ... it is roadmap, not shipped product" | `CHANGED` | Banner plus body, 2026-09-01 (`docs/user-stories.md:8`). Execute/monitor are labeled roadmap in the spine (`:109`, `:115`); CreateVaultModal is marked roadmap in the click-path mermaid. Lower-page stories that still read as a live user vault — generate deploy CTA, `/portfolio` as depositor, library Vault Leaderboard, passport on-chain Verify, landing “provenance-anchored” — were future-tensed or killed in the same honesty pass. The judge happy-path no longer walks Deploy as shipped. |
| "10,000 q-fin research papers" | `CHANGED` | Removed. The one-liner and the MVP-scope paragraph now point at live `GET /health` `corpus_papers` / `corpus_db_count` and refuse to freeze a number (`docs/user-stories.md`). |
| "Arc has **no mainnet** — it's testnet-only" | `TRUE` | `docs/user-stories.md`, true on 2026-09-01, and date-gated the same way as the README row. |

## `docs/agent-quickstart.md`

| Claim | Status | What backs it |
|---|---|---|
| "This page is narrower: nothing below creates a vault or puts capital on-chain" | `TRUE` | `docs/agent-quickstart.md:16`. The eleven steps end at `POST /api/paper/deployments`; the vault route is named only as a pointer to `agent-api.md`. |
| "It is not free ... step 6 charges $2.00 testnet USDC per run and settles for real ... the code defaults are the opposite" | `TRUE` | `docs/agent-quickstart.md:18`, backed by `backend/archimedes/services/generation_payment.py:56` and `:72`, and by the instruction to read `GET /api/generate/quote` against the host you are actually calling. |
| Every route and worked `curl` on the page resolves | `TRUE` | Guarded, not asserted: `backend/tests/test_agent_quickstart_drift.py` parses the page and fails on a route the app does not serve or a curl that drifts from the prose. |

## Passport, leaderboard, and the "Verified" badge

| Claim | Status | What backs it |
|---|---|---|
| "Archimedes Verified" cannot be earned by an imported return series | `CHANGED` | The 2026-08-20 reading — "no CSV/return-import endpoint exists, so the claim is vacuously true" — no longer holds: `POST /api/rigor/verify` accepts a bare returns series today. The claim survives on a stronger footing, by structure rather than by absence: two of the four legs are permanently `not_evaluable` on that transport (`backend/archimedes/api/rigor_verify_routes.py:787`), the verdict is explicitly `verdict_capped` and "not the strategy passport's gate" (`:47`), and the endpoint persists no strategy. |
| Leaderboard figures are provisional | `CHANGED` | The broad two-defect banner is retired: the #1203 routing defect and the backtest/live interpreter divergence were both fixed and re-verified, so their clauses became false and were removed (`ui/src/components/Leaderboard.jsx:446`). One caveat remains, scoped to the own view: generated-strategy figures are fixed at generation time and are not re-backtested (`:477`). |
| No public surface quotes a curated pass count | `TRUE` | Verified across Landing, `/security`, `ui/index.html`, `README.md`, `llms.txt`, and `agent.json` on 2026-08-31. |
| No paper-trading surface shows a performance number without the gate verdict beside it | `TRUE` | Was the open half of "Paper-trading a failing strategy is allowed. Relabelling one is not." (Landing, above): deploy genuinely had no rigor precondition, and the surfaces genuinely said nothing about the gate — a bare "+2.10% · total return" for a rejected strategy. #1764: `deployment_summary` (`backend/archimedes/services/paper_trading.py`) and `LivePaperEntry` (`backend/archimedes/api/leaderboard_schemas.py`) now carry `rigor_gate_status` / `graded_at` / `gate_version` READ from `strategy_passports` (`passport_loader.stored_rigor_verdict(s)`, never a recompute), and BOTH surfaces that render a paper number — `/app/paper`'s card (`ui/src/components/PaperTrading.jsx`) and the leaderboard's "Live paper trading" tab (`ui/src/components/Leaderboard.jsx`) — render the shared `<GateVerdictChip>` unconditionally beside it; on the card the number and the verdict also reach a screen reader as one `sr-only` line. Guarded both sides — `backend/tests/test_paper_deploy_verdict.py`, `backend/tests/test_leaderboard_live_paper.py`, `ui/test/paper-gate-verdict.test.js` (one call site per figure, no conditional anywhere in the chip's JSX region on either surface, and a payload with the verdict DROPPED renders "verdict unavailable" rather than silence). |
| No copy on `/app/paper` promotes a paper return into a verdict | `TRUE` | Pinned as a negative in `ui/test/paper-gate-verdict.test.js` over every label, tooltip and page note the surface renders: no "validates the vault / strategy", no "validated by paper", no "proves the strategy", no "guarantee". The positive obligation is pinned with it — `FORWARD_EVIDENCE_NOTE` says the forward record and the gate verdict do not re-label each other. |

## Paper trading — the ledger, the passport card, and the own-leaderboard tab

Added 2026-09-03 (#1807). The cutover to Arc mainnet was **cancelled** by owner call on
2026-08-30 ([#1240](https://github.com/aprin-labs/archimedes/issues/1240)): Archimedes stays a
testnet product until legal/regulatory review and sustained user traction justify charging real
money, and no date is named. Four shipped surfaces were still promising that the paper ledger
"carries to mainnet" — a claim about an event that is not scheduled, which is the strongest kind
of over-claim this file exists to catch, because it is unfalsifiable rather than merely optimistic.

| Claim | Status | What backs it |
|---|---|---|
| "This is the track record that **carries to mainnet**" — the paper ledger, on the Paper Trading page, the passport's paper-deploy card, and the own view of the research leaderboard | `RETRACTED` | #1807. All four carriers now say what is true: `ui/src/components/PaperTrading.jsx:319` and `ui/src/components/StrategyPassport.jsx:1438` say "a paper track record on Arc testnet — no real funds"; `ui/src/components/Leaderboard.jsx:341` says paper deployments record it forward on Arc testnet, with no real funds; and the module comment the other three were quoting is corrected at `ui/src/paperCopy.js:110`. |
| The retraction is guarded, not just performed | `TRUE` | `ui/test/no-mainnet-track-record.test.js:72` (`PAPER_SURFACES`) bans the **word** `mainnet` on those four files rather than the one phrasing — "ahead of mainnet" and "mainnet-ready" are the rewrites a sentence-scrub would miss. Two repo-wide sweeps stop the sentence migrating to a fifth file: the literal `carries to mainnet`, and `:154` (`ledgerMainnetPairs`), which flags any line pairing the word `mainnet` with `track record`/`ledger` — over every text extension under `ui/src`, not just `.js`/`.jsx`, with `:179` (`wrappedLedgerMainnetClaim`) making a second sentence-wide pass so a claim split across a line wrap is not invisible to a line scan. The positive "nothing true replaced it" assertion reads `:120` (`readerText`), which strips comments and flattens `'…' + '…'` wrapping, so a note in a comment does not count as copy and a line break cannot change the verdict. The other UI files that name mainnet are the honest negations ("No mainnet money" at `ui/src/components/PublicLayout.jsx:13`) and are deliberately out of scope. The only way the word comes back is a line-level `mainnet-claim-exemption: owner=<name> date=<YYYY-MM-DD> issue=#<n>`, and the guard proves a malformed marker does **not** silence a line. |
| The same sentence on the machine and doc surfaces | `RETRACTED` | Same change, same wording: `docs/api/paper-trading.md:100`, `backend/archimedes/models/paper_store.py:276`, `backend/archimedes/services/paper_marks.py:49`, `backend/migrations/versions/e41c7a9b2d63_add_paper_marks.py:10`. The design record is corrected in the open rather than rewritten in silence — `docs/plans/2026-08-30-intraday-paper-trading.md:8` carries a dated note saying what the plan used to claim and why it changed. These four are outside the UI guard's reach, so the phrase is pinned absent from them by `backend/tests/test_claims_ledger.py` (`_RETRACTED_PHRASE_PINS`) — without that, this row could rot back to false with every suite green. |

## Market data — the Explore page and what paid analysis runs on

The owner's framing, recorded here because the ledger is where the public position lives.
The decision record is `docs/adr/market-data-sourcing.md`, added by
[#1218](https://github.com/aprin-labs/archimedes/issues/1218)'s work and merged
(`Status: Accepted`, amended 2026-09-01 by
[#1798](https://github.com/aprin-labs/archimedes/issues/1798) to route per seam rather
than by one global variable). The mechanism the ADR describes has landed — `infra/ecs.tf`
wires the `TIINGO_API_TOKEN` secret and `backend/tests/test_ecs_backend_secrets.py` pins
its presence and ARN shape — but landing the mechanism is not the same as flipping it on:
the row below stays `PENDING VENDOR CUTOVER` until the owner actually points a seam's
default at Tiingo, which is a deploy action (`infra/apply.sh --apply` + one task-definition
rollout), not a code change. `TestVendorCutoverStillPending` in
`backend/tests/test_claims_ledger.py` pins today's absence of that pin in `infra/ecs.tf`, so
this row cannot rot true by silent drift — the day the owner performs the cutover, that
test goes red and this row must move off `PENDING VENDOR CUTOVER`.

| Claim | Status | What backs it |
|---|---|---|
| The Explore page is free, open, and ungated | `TRUE` | `ui/src/routes.js:60` puts `explore` in `ANON_APP_PAGES`, so it renders with no session; `backend/archimedes/api/explore_routes.py:24` and `:30` carry no auth dependency. The code is public domain (`LICENSE`). |
| Explore is a FOSS viewer over yfinance streams, not a redistribution product | `TRUE` | The mechanism is true and labelled on the page — `ui/src/components/Explore.jsx:381` tells the visitor which cards are oracle-priced and which come from yfinance, and `ui/src/components/AssetModal.jsx:22` labels the source per asset. The licensing position is stated in `docs/adr/market-data-sourcing.md` (landed with #1627): split sourcing, no commercial redistribution of yfinance data, Tiingo Business named as the mainnet prerequisite. |
| Paid analysis runs on licensed data | `PENDING VENDOR CUTOVER` | Not a statement of current state, and the ledger must not launder policy into fact. The vendor seam exists on both sides — `analytics-engine/src/archimedes_analytics_engine/market_data.py:96` and `backend/archimedes/services/market_data_provider.py:1060` (`provider_name`, a real Tiingo provider behind it) — and per-seam routing since #1798 means the daily seam (`MARKET_DATA_DAILY_PROVIDER`, falling back to `MARKET_DATA_PROVIDER`) can move to Tiingo without moving the intraday/live seam with it. **`infra/ecs.tf` pins neither variable today, so both seams still resolve to the code default, `yfinance`** — the paid path and the free path read the same source until the owner applies the cutover. |
| yfinance is an unlicensed commercial dependency on the critical path | `TRUE` | #1218's own finding, unchanged: `analytics-engine/src/archimedes_analytics_engine/market_data.py:32` imports it and `:96` makes it the default, and every strategy pulls its declared universe through it. |

---

## Defects this audit found

Three, all left for a separate change rather than smuggled into a docs PR. The first is the
one worth acting on: it was a live machine-readable contract that under-published itself.

0. ~~**`cli/src/archimedes_cli/manifest.py:19` omits exit code `4`.**~~ **FIXED (#1705).**
   `exits.py:33` defined `INCOMPLETE = 4` and `cli.py:587` exited with it, but the
   published `EXIT_CODES` table stopped at `3` — the one surface a script actually reads
   was the incomplete one, against the CLI's whole stated reason for pinning exit codes.
   The `archimedes generate` work published `4` and added `5`–`8` alongside it. The
   self-retiring pin that forced this row to move (`TestPublishedExitCodesStillOmitIncomplete`)
   has been replaced by a real guard deriving the published set from `exits.py`, so the
   next unpublished code fails CI instead of waiting for an audit to notice.

1. **`ui/src/components/Landing.jsx:84`** — the comment block above `BOARD_FDR` says the
   figure is "served publicly — `GET /api/selection-bias/gate` returns `board_level_fdr`
   ... (`selection_bias_routes.py:535-552`)". That endpoint carries no such key any more:
   #1564 (merged as #1580, commit `131947d7`) moved board-level FDR onto
   `GET /api/leaderboard`, and `backend/archimedes/api/selection_bias_routes.py:105`
   records the move. `test_selection_bias_routes.TestBoardFdrStaysOffThePerStrategyGate`
   fails if the key reappears — so the comment now points a reader at an endpoint that is
   guaranteed *not* to have it. The user-visible copy is unaffected and stays `TRUE`.
2. ~~**The machine surfaces lag the human ones.**~~ **FIXED (#1650, then 2026-09-01
   copy-honesty).** `ui/test/roadmap-copy.test.js` now scans `ui/public/` machine
   surfaces; deploy / marketplace / monitor advertise `roadmap`; generation SEO tags
   no longer claim an on-chain generation trace.

## Not covered here

- The pitch deck and any grant application text. Those live in the private `docs` repo by
  policy (`CLAUDE.md` § Project); this file covers the surfaces in this repo.
- `/health` field-by-field. It is a live endpoint and the ledger would rot; the rows above
  cite the code that computes each field instead.
- Contract-level claims beyond `ReasoningTraceRegistry`. `contracts/` has its own tests.

<!-- claims-ledger:pending-paths
     Paths cited above that do not exist in this tree yet. The guard asserts each one is
     STILL absent, so the exemption retires itself: when the file lands, the guard goes
     red and the row above must be re-pointed at real evidence rather than a promise.
-->
