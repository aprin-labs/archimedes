# Claims ledger

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-03
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

## How to read a row

| Status | Means |
|---|---|
| `TRUE` | The claim, as it reads on the surface today, is backed by the cited live path. |
| `CHANGED` | The surface used to over-claim. The row records what it said, and the PR/commit that narrowed it. |
| `RETRACTED` | The claim was removed outright, and a guard exists to stop it coming back. |
| `OVER-CLAIMED` | Still live, still wrong, not fixed by this ledger. These are the open ones. |
| `PENDING ADR MERGE` | The position is the owner's stated one; the decision record it rests on is not merged yet. |

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
| "Four independent checks run outside the generator, on persisted returns, so the thing being graded cannot influence its own grade" | `TRUE` | `backend/archimedes/services/live_rigor_gate.py:151` — `verdict_from_returns` grades a persisted series outside the generator. The four checks are named at `ui/src/components/Landing.jsx:39`. The second grading scale (`BacktestResult.passes_rigor_gate`) that made this false was deleted in #1242 (`d7073f13`). |
| "Four verdicts, not two" — `pass` / `fail` / `pending` / `degenerate`, and a pass never rounds up | `TRUE` | `backend/archimedes/services/live_rigor_gate.py:54` defines `DEGENERATE`; `:92` makes `passes` a plain bool set `True` only by the `pass` constructor, so `pending` and `degenerate` are fail-closed. |
| Each rigor card states its own limit (DSR at the 95% one-sided level, PBO is a selection-set property, OOS is one chronological hold-out with no purge gap) | `TRUE` | `ui/src/components/Landing.jsx:26` documents each `limit` string against the module that computes it; the bar is `DSR_P_BADGE_MIN` in `backend/archimedes/services/rigor_profiles.py` — one definition since #1794, guarded by `backend/tests/test_single_dsr_bar.py`. |
| "Board-level correction — ranking N strategies is counted as N tests", Benjamini–Hochberg at α = 0.05, advisory, never flips a verdict | `TRUE` | `backend/archimedes/services/rigor_evaluator.py:475` (`DEFAULT_BOARD_FDR_LEVEL = 0.05`) and `:486` (`compute_board_level_fdr`, advisory by its own docstring). Served publicly on `GET /api/leaderboard` — `backend/archimedes/api/leaderboard_schemas.py:82`, and `backend/archimedes/api/leaderboard_routes.py:175` never 401s (anonymous callers are served the curated scope — the endpoint's own `scope` docstring says so in as many words). |
| "Inspect — nothing is discarded ... a fail as durably as a pass" | `CHANGED` | Was "every run leaves a reasoning trace bound to the chain". Retracted 2026-08-30; the retracted wording is pinned in `ui/test/public-visuals.test.js`. A generation run computes a keccak provenance hash (`backend/archimedes/agents/generation_pipeline.py:1635`) whose own comment says it is "mirrored on-chain in v1.5" (`:1614`) — i.e. not today. The only writers to the registry are the agent rebalance tick's `_commit_trace` (`backend/archimedes/chain/agent_runner.py:1441`) and `_reveal_trace` (`:1596`), which no generation run reaches. |
| "Does Archimedes trade for me? No." | `TRUE` | The reachable act-on step is `POST /api/paper/deployments` (`backend/archimedes/api/paper_routes.py:92`), which is simulated. `POST /api/vaults/create` exists (`backend/archimedes/api/vaults_routes.py:330`) but its UI journey is off the shipped build — `ui/src/featureFlags.js:57` lists `portfolio` / `vault-detail` in `ROADMAP_PAGES`, and `:28` defaults `ROADMAP_SURFACES_ENABLED` to false. |
| "A failing strategy stays a failing strategy. Paper-trading one is allowed. Relabelling one is not." | `TRUE` | `backend/archimedes/api/paper_routes.py:92` checks ownership and that the stored spec still validates — and nothing else, so there is no rigor precondition to claim. The verdict itself is computed server-side (`live_rigor_gate.py:151`), so it cannot be relabelled from the browser. |
| Every vault / non-custodial / custody claim on this page | `RETRACTED` | #1469. `ui/test/roadmap-copy.test.js` source-scans this file for `/vault\|non-?custodial\|custody/i` with no carve-outs — which is why the file's own comments are written around the words. |
| "Arc census live — ≥N reported instances" | `TRUE` | `ui/src/components/Landing.jsx:504` renders "Live census unavailable · No cached count substituted" when `GET /api/config/contracts` fails or the pool count is unreadable. The count is a floor by construction (`:16` scopes it to five core fields). |
| The page quotes no strategy pass count and no performance number | `TRUE` | The only percentage on the page is "70/30" — a methodology parameter, not a result. Deliberate; see `ui/src/components/Landing.jsx:90`. |
| Footer / announcement — "No real funds" | `CHANGED` | Was an unqualified "No real funds" on `PublicLayout.jsx` and the Landing footer, which read as *nothing you sign moves value* after the generation paywall shipped (`dry_run: false`). Narrowed to no mainnet money; generation fee is real testnet USDC: `ui/src/components/PublicLayout.jsx:23`, `:145` (the footer disclosure moved into the shared `PublicLayout` footer; `Landing.jsx` no longer renders its own). |
| "Does Archimedes trade for me?" paper-trading split | `CHANGED` | Landing FAQ no longer names the unmerged `paper_agent_trades` table as a visitor path. `ui/src/components/Landing.jsx:182` says simulated paper trading grades `paper_daily_returns`, not on-chain execution proof. The two-book split from [PR #1704](https://github.com/aprin-labs/archimedes/pull/1704#issuecomment-5493036672) lives on machine surfaces (`agent.json` paper note), which state that table is not on main. |

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
| Hero status list — "Paid surface: Generation — quoted live" | `TRUE` | New row, added with the sentence. The price is not written into the copy on purpose: it comes from `GENERATION_PRICE_USD` (`backend/archimedes/services/generation_payment.py:122`) and a number in prose would drift the first time it moved. `ui/src/components/Security.jsx:33`. |
| "Three boundaries. No role inflation." | `CHANGED` | Was four. The fourth was a vault-share-owner role over a path that has never run; removed by #1469 (`ui/src/components/Security.jsx:44`). |
| "Better Auth account. Canonical application identity. A connected wallet never creates or replaces the account session." | `TRUE` | `backend/archimedes/api/account_auth.py:73` — the session lookup forwards the request **cookie** and nothing else to the auth service, so no wallet header can participate in establishing a session. Restated in `docs/security/auth-model.md`. |
| "A five-minute, single-use EIP-4361 challenge proves control before a wallet is linked" | `TRUE` | `backend/archimedes/api/wallet_routes.py:22` — `_CHALLENGE_TTL = timedelta(minutes=5)`; `:350` rejects a challenge whose `consumed_at` is set, and `:389` consumes it in a conditional update that must affect exactly one row. |
| "Bounded internal agent ... a user session cannot assume that role, and that role cannot read or write another account's private records" | `TRUE` | `backend/archimedes/api/auth_guard.py:38` compares `X-Internal-Agent-Key` with `hmac.compare_digest` and **fails closed when the key is unset** (`:35-38`), so a misconfiguration cannot open the role. The scope half is true by enumeration: exactly two routes carry the dependency — `POST /api/traces/publish` (`backend/archimedes/api/traces_routes.py:398`) and `POST /api/agent/bootstrap-liquidity` (`backend/archimedes/api/agent_routes.py:209`). Both write; neither reads a per-account record. |
| Session — "Production session cookies are HttpOnly, Secure, and SameSite=Lax" | `CHANGED` | Was "Production cookies are HttpOnly and Secure", which under-stated a control that is actually there. `auth/auth.js:612` sets `useSecureCookies: production`; `:614` `httpOnly: true`; `:615` `secure: production`; `:616` `sameSite: 'lax'`. |
| Session — "nginx, the UI route guard, and FastAPI independently protect private surfaces. Two browse pages are deliberately anonymous, and the edge and client halves of that carve-out list are kept in lockstep" | `CHANGED` | Was "Four browse pages", true under #1194 rev d and false as of #1753 (the owner's call): the leaderboard and the strategy passport were gated, leaving Explore and Corpus. The three-layer half is unchanged and still true (`nginx/nginx.conf:382` `auth_request /_auth_session` on `^~ /app`; `ui/src/App.jsx:145` the client bounce; FastAPI's own dependencies). The count: `nginx/nginx.conf:310-324` serves bare `/app`, `/app/explore` and `/app/corpus` with no `auth_request` — two pages, since bare `/app` is the SPA's alias for Explore — matching `ANON_APP_PAGES` at `ui/src/routes.js:49`. The "kept in lockstep" clause was carried by a comment in each file until #1753 and is now enforced: `backend/tests/test_nginx_anonymous_carve_outs.py` derives the expected nginx location set from `ANON_APP_PAGES` and fails in both directions. |
| Scope — "Profile, strategy, job, and linked-wallet reads resolve through the authenticated Better Auth user—not a client-supplied address." | `TRUE` | `backend/archimedes/api/user_routes.py:150` and `:164` filter on `owner_user_id == user.id`; `backend/archimedes/api/strategies_routes.py:681` builds its owner filter from `user.id`; `backend/archimedes/api/wallet_routes.py:385` scopes the challenge lookup to `user.id`. `docs/security/auth-model.md` records that selected-wallet headers are hints, honoured only when they resolve to a verified link on the current user. |
| Integrity — "Agent-only writes require a service credential." | `TRUE` | Same enumeration as the Bounded internal agent row: `backend/archimedes/api/auth_guard.py:35`, applied at `backend/archimedes/api/traces_routes.py:398`. |
| Payment — "Generation is a paid call, bound to the wallet you proved ... a mismatch is refused before any settlement round-trip" | `TRUE` | New control block, added 2026-08-31 because the page described identity, edge, verdict and provenance while saying nothing at all about the one rail that moves value. `backend/archimedes/services/generation_payment.py:180` is the paywall; `:248` decodes the `Payment-Signature` header and `:253-258` rejects `payer_mismatch` when the authorization's `from` is not the account's linked wallet — **before** the `verify`/`settle` calls at `:262` and `:270`. The wallet-link precondition that makes "the wallet you proved" meaningful is `backend/archimedes/api/generate_routes.py:458`. |
| Payment — "published anonymously at GET /api/generate/quote" | `TRUE` | `backend/archimedes/api/generate_routes.py:356` returns `generation_payment.quote()`, defined at `backend/archimedes/services/generation_payment.py:133`, which reports price, asset, chain, recipient, `dry_run` and `halted`. The route carries no auth dependency, and an anonymous fetch against the live deployment returned the full quote on 2026-08-31. |
| Payment — "An operator kill switch refuses service rather than serving the paid product unpaid." | `TRUE` | `backend/archimedes/services/generation_payment.py:218` raises 503 on `PAYMENTS_HALT` instead of accepting an unverified header, and the docstring at `:78` records why this rail differs from the marketplace one. |
| Payment — "Paper trading is free." | `TRUE` | By absence: `backend/archimedes/api/paper_routes.py` never references `generation_payment`, so `POST /api/paper/deployments` (`:92`) reaches no paywall. Pinned as an absence in `ui/test/security-claims.test.js`, because a presence pin on that file would prove nothing. |
| Edge — "a script policy admitting same-origin bundles plus one hashed inline bootstrap" | `CHANGED` | Was "a hash-restricted script policy", which invites the reading that every script is hash-pinned. The policy is `script-src 'self' 'sha256-…'` (`nginx/nginx.conf:72`): same-origin bundles, plus one exact hash for the pre-paint theme bootstrap, per the comment at `:190`. Verified as served: the live `Content-Security-Policy` response header on `/security` carries that exact value. |
| Edge — "HSTS, framing denied outright, and a permissions policy that turns off geolocation, microphone, and camera" | `TRUE` | `nginx/nginx.conf:209` (`Strict-Transport-Security`), `:210` (`X-Frame-Options: DENY`), `:213` (`Permissions-Policy`), all with `always` so they are emitted on error responses too. All three confirmed on the live response headers 2026-08-31. **Two sources, found by probing rather than reading:** `infra/cloudfront.tf:197-218` re-asserts HSTS and frame-options at the edge with `override = true`, so those two arrive from CloudFront and nginx both; CSP and `Permissions-Policy` are nginx-only. The tell is `Referrer-Policy` — `nginx/nginx.conf:212` sets `same-origin`, the wire says `strict-origin-when-cross-origin` (`infra/cloudfront.tf:215`). The page names no referrer policy, so nothing on it is wrong; recorded because an auditor reading only the nginx config would get a different answer than the response headers give. |
| Edge — "Two per-IP request-rate zones run at the edge — the tighter one on the credential surface — with tighter per-route limits on expensive endpoints behind them." | `CHANGED` | Was "separate read/write rate limits", which is not what is deployed. The two zones exist (`nginx/nginx.conf:140-141`, `api_read` 60r/m and `api_write` 20r/m) but are applied **by path, not by method**: `:358` puts `/api/auth/` on `api_write` and `:365` puts all of `/api/` — every POST included — on `api_read`. A reader who believed the old sentence would have thought `POST /api/generate/start` was rate-limited more tightly than a read; it is not, at the edge. What actually bounds it is the per-route slowapi limit at `backend/archimedes/api/generate_routes.py:389` (`5/minute`), one of ~30 in the tree. |
| Verdict — "A rigor verdict is computed server-side, never asserted ... on persisted returns, so the thing being graded cannot influence its own grade." | `TRUE` | `backend/archimedes/services/live_rigor_gate.py:150` — `verdict_from_returns` grades a persisted series outside the generator and fails closed to `pending`. Same backing as the Landing row. |
| Provenance — "The agent's rebalance decisions are content-hashed and anchored on Arc" plus its two stated limits | `CHANGED` | Was the unscoped "Reasoning records are content-hashed and anchored on Arc", which the previous revision of this ledger already had to correct **in prose** — the row read `TRUE` and then spent a sentence explaining what the page failed to say. That is a finding, not a verification, so the scope now lives on the page: `contracts/src/ReasoningTraceRegistry.sol` and the commit/reveal pair at `backend/archimedes/chain/agent_runner.py:1441` / `:1596` anchor the agent's tick. A generation run computes the same shape of hash but is not anchored — `backend/archimedes/agents/generation_pipeline.py:1614` says "mirrored on-chain in v1.5", and `backend/archimedes/api/traces_routes.py:189` excludes generation traces outright. A decision producing no transaction has no anchor: `README.md:168`. Re-hashing against the anchor is real — `backend/archimedes/api/traces_routes.py:505`. |
| Known limits — "Testnet: Arc public testnet only, and the USDC in play is faucet USDC." | `TRUE` | Chain `5042002`; the live quote reports `chain: arcTestnet`. The faucet framing matches the pricing rationale recorded at `infra/ecs.tf:573-576` (one $20 drip = ten generations). |
| Known limits — "No execution: Archimedes does not trade with capital today. **Paid generation**, the rigor gate, paper trading, and the agent's trace anchoring are what run" | `CHANGED` | The claim was true and the enumeration after it was not: it listed everything that runs *except* the one rail that moves value. The reachable act-on step is still `POST /api/paper/deployments` (`backend/archimedes/api/paper_routes.py:92`), simulated; `POST /api/vaults/create` exists (`backend/archimedes/api/vaults_routes.py:330`) but its journey is off the shipped build (`ui/src/featureFlags.js:28`). |
| Known limits — "Live charge: ... A settled fee lands in a platform-operated wallet Archimedes signs for through its payment provider. It is a fee, not a balance held for you, and there is nothing there to withdraw." | `TRUE` | New row. The recipient is `infra/ecs.tf:578` (`GENERATION_PAYMENT_RECIPIENT`), a platform wallet whose Circle wallet id is pinned at `:583` and whose signing credentials live in SSM — so Archimedes controls it, and a reader deserves to know that before signing. **The wording is deliberately mechanical:** `ui/test/roadmap-copy.test.js:188` scans this page for `/vault\|non-?custodial\|custody/i` with no carve-outs and comments included, so the shorter word for this arrangement cannot appear here. Naming the constraint is better than letting the sentence look coy. |
| Known limits — "Some current oracle and risk inputs are mock data" | `TRUE` | Listed under Known limits (`ui/src/components/Security.jsx:245`), consistent with the Explore page's own source labelling (`ui/src/components/Explore.jsx:380`). |
| Known limits — "Immutable contracts: a defect requires a new deployment and migration rather than an in-place upgrade." | `TRUE` | No proxy, `delegatecall`, or upgradeable base appears anywhere under `contracts/src/`; `contracts/src/ReasoningTraceRegistry.sol:67` is a plain constructor + `Ownable`. The single "upgradeable" mention in the tree, `contracts/src/PriceOracle.sol:69`, is about an **external** Chainlink feed, not an Archimedes contract. |
| Known limits — "No independent security audit ... is claimed by this page" | `TRUE` | A disclaimer, correct as written (`ui/src/components/Security.jsx:253`). |
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
| Vault execution — "**Execute** and **monitor** are roadmap … When it ships, a strategy that survives the gate *will be* deployable into a non-custodial vault on Arc … Today it will not." | `CHANGED` | `README.md:24`, rewritten to the future tense in the 2026-08-31 README refresh, which is what #1469's scrub had done for Landing, `/security`, and `ui/index.html` without reaching the README. The facts behind the retraction are unchanged: the route is real (`backend/archimedes/api/vaults_routes.py:330`), the journey is gated off every shipped surface (`ui/src/featureFlags.js:57`), and zero user vaults have ever been deployed. `README.md:211` repeats the limitation in the limitations list. The pin that held this row honest was removed from `backend/tests/test_claims_ledger.py` in the same commit, per that test's own instruction. |
| "How many strategies in the curated library currently pass is **unestablished** — this file will never quote a count" | `TRUE` | `README.md:37`. Matches `CLAUDE.md` § corollaries. No count appears anywhere in the file. |
| "The corpus is arXiv preprints, not peer-reviewed papers … Do not freeze that count in prose; the corpus probe can timeout" | `CHANGED` | Was "The corpus is 10,000 arXiv preprints". `README.md:191` now points at `/health` `corpus_papers` / `corpus_db_count` and refuses to freeze a number. `backend/archimedes/main.py:1586` still publishes `corpus_papers`; `:1473` / `:1604` publish `corpus_embedded_at_rest`; the rerank cap is at `:1606`. |
| "The knowledge graph is not built ... `GET /api/corpus/graph` refuses with 503 `kb_artifact_not_found`" | `TRUE` | `backend/archimedes/api/corpus_routes.py:276` raises exactly that; `backend/archimedes/main.py:1110` derives `corpus_kg_built` from a live entity count rather than a constant. |
| "Arc has no mainnet yet; mainnet launch, real-funds custody, and the regulatory architecture are roadmap" | `TRUE` | `README.md:44`, true on 2026-08-31. **Date-gated:** #1241 records this as false from Sept 16. Re-check on that date; nothing in CI will tell you. |
| "Payments are real (USDC on Arc); settlement is stubbed pending mainnet" | `TRUE` | Two switches, and the line is right about both. Generation settles for real where deployed (`backend/archimedes/services/generation_payment.py:56`, `:72`); marketplace settlement rides the separate `PAYMENTS_DRY_RUN`, which defaults to dry-run (`backend/archimedes/services/generation_payment.py:74`). |
| "Not every reasoning trace is anchored on-chain ... check `arc_tx_hash` before treating a trace as anchored" | `TRUE` | `README.md:217`. This is the general form the `ui/index.html` meta tags now match. |
| CLI exit codes are a stable contract, published machine-readably | `CHANGED` | **Was `OVER-CLAIMED`: the published table omitted a code the CLI actually exits with.** `cli/src/archimedes_cli/exits.py:33` defines `INCOMPLETE = 4` (added for #1481) and `cli.py:587` exits with it on the real `verify` path, but `cli/src/archimedes_cli/manifest.py:19` published only `0`/`1`/`2`/`3` — so an agent branching on the machine-readable table hit an undocumented `4`. Fixed by #1705 (issue #1697), the `archimedes generate` work, which published `4` and added `5`–`8` for the new command's outcomes (`exits.py:48` `PAYMENT_REQUIRED`, `:77` `STILL_RUNNING`). The completeness defect is now guarded rather than pinned: `test_claims_ledger.TestLedgerClaimsMatchTheTree::test_the_published_exit_code_table_covers_every_code_the_cli_defines` derives the expected set from `exits.py`, so a future code added without publishing it fails. The originally-documented four were always accurate, and the `AUTH`-reuses-`2` rationale is sound (`exits.py:25`); the defect was completeness, not correctness. |

## Agent surfaces — `ui/public/llms.txt`, `ui/public/.well-known/agent.json`

| Claim | Status | What backs it |
|---|---|---|
| "executed in non-custodial USDC vaults on Arc testnet" (llms.txt header) | `CHANGED` | Was the highest-severity open row here: present tense, on the surface built for agent consumers, while no user vault has ever been created. #1650 rewrote it to "Executing strategies in non-custodial USDC vaults on Arc is roadmap, not shipped" — `ui/public/llms.txt:10`, mirroring `CLAUDE.md`'s own framing — and the paper-trade step of the agent journey with it. The mention is kept, in the future tense, on purpose. |
| `agent.json` `description` — "executed in a non-custodial USDC vault on the Arc testnet" | `CHANGED` | Same sentence, same fix, then split the paper books: `ui/public/.well-known/agent.json:4` says simulated paper deployments are live (`paper_daily_returns`), `paper_agent_trades` (PR #1704, not on main) is not a live path, generation is not anchored, and vault execution is roadmap, not shipped. The served twin — `/api/agent/manifest`'s `blurb`, `backend/archimedes/api/agent_manifest_routes.py:152` — carries the identical string, and `backend/tests/test_agent_manifest_static_consistency.py` asserts the two are equal and that neither says "executed in". |
| `agent.json` `skills[deploy-vault].status: "live"` / `endpoints.deploy.status: "live"` | `CHANGED` | #1650 moved the *skill* to `status: "roadmap"` while leaving `endpoints.deploy.status: "live"` as route-truthful. The 2026-09-01 copy-honesty pass moved the endpoint too: `ui/public/.well-known/agent.json` `endpoints.deploy.status` is `"roadmap"`, matching the served manifest (`backend/archimedes/api/agent_manifest_routes.py`). Marketplace skills `publish` / `subscribe` moved from `"live"` to `"roadmap"` in the same pass — marketplace is not a public surface. `backend/tests/test_agent_manifest_static_consistency.py` pins deploy / marketplace / monitor as `"roadmap"`. |
| `agent.json` `endpoints.paper.note` — "deploy is also live and puts real capital on-chain" | `CHANGED` | Testnet faucet USDC is not real capital. Rewritten by #1650, then split: `ui/public/.well-known/agent.json:114` `status: "live"` names simulated `POST /api/paper/deployments` whose graded book is `paper_daily_returns`. `paper_agent_trades` is PR #1704, not on main, and is not what that status advertises. Vault execution is roadmap, not shipped. |
| `agent-registration.json` `description` — same present-tense vault sentence | `CHANGED` | Not previously in this ledger, and it should have been: `ui/public/.well-known/agent-registration.json:4` carried the agent-card sentence verbatim. #1650 rewrote the prose to match and touched nothing else in the file — the ERC-8004 semantics (`registrations`, `supportedTrust`, `erc8004`, `active`) are #1626's lane and are unchanged. |
| The machine surfaces cannot regress to present-tense vault execution | `TRUE` | `ui/test/roadmap-copy.test.js:280` scans `ui/public/`'s six machine surfaces and requires every sentence mentioning a vault, custody, or "real capital" to carry `roadmap` or `not shipped` in that same sentence; `:295` and `:304` are the two patterns. Anti-vacuity is explicit: the six sentences removed by #1650 are kept verbatim in the file and each must still trip the guard, the identifier exemption is asserted not to swallow any of them, and a separate test requires the roadmap mention to still be *present* so a scrub cannot pass by deletion. |
| `agent.json` `endpoints.generate.note` — quote is public and authoritative; prod answers `payment_required: true`, `dry_run: false`, `$2.000000`; the code defaults are the opposite | `TRUE` | `backend/archimedes/services/generation_payment.py:56` reads `GENERATION_PAYMENT_REQUIRED` and defaults off; `:72` defaults dry-run on. `backend/archimedes/api/generate_routes.py:449` is the 402 that carries the x402 requirements. |
| "x402-gated strategy access ... no endpoint returns 402 with payment requirements" (the 2026-08-10 retraction) | `CHANGED` | The retraction is itself out of date. `POST /api/generate/start` now returns 402 carrying x402 requirements (`backend/archimedes/api/generate_routes.py:449`), and 409 `wallet_link_required` first (`:459`). What remains true is the marketplace half: `GET /api/marketplace/published/{strategy_id}` is public (`backend/archimedes/api/marketplace_routes.py:753`). |
| `agent.json` `erc8004` — "NO ERC-8004 identity, reputation, or validation claim is made" | `TRUE` | `ui/public/.well-known/agent.json` — self-disclosing, with `agentId` and `tokenURI` null, `status: "registration_pending"`, and the reason spelled out in the `note`. No `register()` transaction has been sent. |
| `agent.json` `endpoints.marketplace.note` — "does not assert that a subscription's recurring USDC charge settles for real" | `TRUE` | `ui/public/.well-known/agent.json`. Correctly separates the two dry-run switches — `backend/archimedes/services/generation_payment.py:64` documents the split that let the generation rail go live without un-drying marketplace settlement — and declines to claim the one no public endpoint publishes. |
| `agent.json` / llms.txt `POST /api/rigor/verify` — PBO and look-ahead "always report `not_evaluable`", `passes` is a capped quorum | `TRUE` | `backend/archimedes/api/rigor_verify_routes.py:47` states the capping contract; `:787` hard-codes both legs to `not_evaluable`; `:803` makes `passes` require every runnable leg to have run and passed. |
| `ui/public/sitemap.xml` lists only routes that render real content for an anonymous visitor | `TRUE` | Five `<loc>` entries (the sixth, `/leaderboard`, was removed by #1753 when the page was gated — a crawler following it now gets a 302 to `/sign-in`), checked one by one against the two allow-sets that between them cover all five: `ui/src/routes.js:3` (`PUBLIC_PATHS` — `/`, `/architecture`, `/security`) and `:49` (`ANON_APP_PAGES` — `explore`, `corpus`). **Verified by reading, not by a guard, and the row says so:** `ui/scripts/check-sitemap.mjs` enforces only the *forward* direction (every public route appears in the sitemap) and its own header documents the reverse — "every sitemap `<loc>` is actually anonymous-accessible" — as **NOT enforced**; `ui/test/sitemap.test.js` pins one specific exclusion (the admin-only `/insights` never appears anywhere in the served bytes), not this property. Claiming a test pins this would be the defect this ledger exists to catch. |

## `ui/index.html` — meta, OpenGraph, Twitter card, JSON-LD

| Claim | Status | What backs it |
|---|---|---|
| Vault / non-custodial framing in all four cards | `RETRACTED` | #1469 rewrote the meta description, OG card, Twitter card and JSON-LD together, on the reasoning that a claim retracted on the page but left in the share card is still shipped. Guarded by `ui/test/roadmap-copy.test.js`. |
| "records the whole decision on Arc public testnet" (meta description) / "check the reasoning trace on-chain" (Twitter) / "on-chain reasoning provenance" (JSON-LD) | `CHANGED` | Was `OVER-CLAIMED`. The three tags now describe generate → rigor-gate → inspect, and say generation is not anchored: `ui/index.html:9`, `:60`, `:76`. Guarded by `ui/test/public-visuals.test.js`. `README.md:217` already had the accurate wording. |
| "A research-grounded strategy-generation instrument with visible selection-bias checks" | `TRUE` | `ui/index.html:76`. The selection-bias checks are visible and live — `backend/archimedes/services/rigor_evaluator.py:486` computes the board-level correction and `backend/archimedes/services/live_rigor_gate.py:151` the per-strategy verdict. |

## `ui/src/components/Architecture.jsx`

| Claim | Status | What backs it |
|---|---|---|
| "with every decision hashed on-chain before anything acts on it" | `CHANGED` | Now only in the `ROADMAP_SURFACES_ENABLED` branch of the hero ternary (`ui/src/components/Architecture.jsx:86`), and that flag defaults false (`ui/src/featureFlags.js:28`), so the shipped build does not say it. |
| "Non-custodial by contract, not by promise" (rendered unconditionally) | `RETRACTED` | Replaced by `OnChainExecutionRoadmap` — "Non-custodial vault execution is on the roadmap; not yet live" (`ui/src/components/Architecture.jsx:613`). The "Live user vaults on Arc" hero tile was removed rather than left reporting a live zero. |
| "x402-gated strategy access" / the nanopayment marketplace section | `CHANGED` | The section still exists but is flag-gated off the shipped build (`ui/src/components/Architecture.jsx:1239`), and its own honesty note says fee settlement is in dry-run (`:830`). |
| "before anything can run live" (flag-off hero) | `CHANGED` | Was present-tense vault implication. Flag-off hero is now generate → rigor-gate → explore, with vault execute/monitor named as roadmap: `ui/src/components/Architecture.jsx:87`. |
| "Not one paper-derived alpha strategy clears our bar" (curated pass count of zero) | `CHANGED` | Removed. The page now says the pass count is unestablished and will never quote a count: `ui/src/components/Architecture.jsx` rigor honesty note. `CLAUDE.md` forbids quoting a curated pass count, including zero. |
| Xia Hierarchy of Truth — "the rebalance loop reads holdings from chain … before the trade is committed" | `CHANGED` | Was written as a live path. Copy now future-tenses the chain-holdings half and says the path is not live: `ui/src/components/Architecture.jsx:928`. Guarded by `ui/test/public-visuals.test.js`. |
| Honesty-ledger rows read from `/health` rather than being asserted | `TRUE` | `ui/src/components/Architecture.jsx:1090` renders "live value unavailable" on a health error instead of substituting a value; the corpus panel's contract is documented at `:838`. |
| "the full trace is published (IPFS-pointed)" | `RETRACTED` | #1526. Reveal copy now says the trace is published off-chain and "we do not pin traces to IPFS" (`ui/src/components/Architecture.jsx:788`). Guarded by `ui/test/ipfs-pinning-copy.test.js`. Live `storagePointer` is empty ([`docs/adr/ipfs-pinning-not-live.md`](adr/ipfs-pinning-not-live.md)). |

## `ui/src/components/AccountSettings.jsx`

| Claim | Status | What backs it |
|---|---|---|
| "Anything already published to a blockchain or pinned to IPFS stays there" | `RETRACTED` | #1526. Deletion copy now says chain writes stay; it does not claim an IPFS pin (`ui/src/components/AccountSettings.jsx:844`). Guarded by `ui/test/ipfs-pinning-copy.test.js`. |

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
| The retraction is guarded, not just performed | `TRUE` | `ui/test/no-mainnet-track-record.test.js:72` (`PAPER_SURFACES`) bans the **word** `mainnet` on those four files rather than the one phrasing — "ahead of mainnet" and "mainnet-ready" are the rewrites a sentence-scrub would miss. Two repo-wide sweeps stop the sentence migrating to a fifth file: the literal `carries to mainnet`, and `:154` (`ledgerMainnetPairs`), which flags any line pairing the word `mainnet` with `track record`/`ledger` — over every text extension under `ui/src`, not just `.js`/`.jsx`, with `:179` (`wrappedLedgerMainnetClaim`) making a second sentence-wide pass so a claim split across a line wrap is not invisible to a line scan. The positive "nothing true replaced it" assertion reads `:120` (`readerText`), which strips comments and flattens `'…' + '…'` wrapping, so a note in a comment does not count as copy and a line break cannot change the verdict. The other UI files that name mainnet are the honest negations ("No mainnet money" at `ui/src/components/PublicLayout.jsx:23`) and are deliberately out of scope. The only way the word comes back is a line-level `mainnet-claim-exemption: owner=<name> date=<YYYY-MM-DD> issue=#<n>`, and the guard proves a malformed marker does **not** silence a line. |
| The same sentence on the machine and doc surfaces | `RETRACTED` | Same change, same wording: `docs/api/paper-trading.md:100`, `backend/archimedes/models/paper_store.py:276`, `backend/archimedes/services/paper_marks.py:49`, `backend/migrations/versions/e41c7a9b2d63_add_paper_marks.py:10`. The design record is corrected in the open rather than rewritten in silence — `docs/plans/2026-08-30-intraday-paper-trading.md:8` carries a dated note saying what the plan used to claim and why it changed. These four are outside the UI guard's reach, so the phrase is pinned absent from them by `backend/tests/test_claims_ledger.py` (`_RETRACTED_PHRASE_PINS`) — without that, this row could rot back to false with every suite green. |

## Market data — the Explore page and what paid analysis runs on

The owner's framing, recorded here because the ledger is where the public position lives.
The decision record is `docs/adr/market-data-sourcing.md`, added by the now-merged
[#1218](https://github.com/aprin-labs/archimedes/issues/1218) work — open as
[PR #1627](https://github.com/aprin-labs/archimedes/pull/1627), not merged as of 2026-08-31.
The rows below are marked `PENDING ADR MERGE` until it lands, and they should be re-pointed
at the ADR then; the guard in `backend/tests/test_claims_ledger.py` fails the moment the
file appears, so that re-pointing cannot be forgotten. Checked against that PR's diff:
it keeps `yfinance` as the default on both seams and adds Tiingo as the paid-analysis
provider, which is what the rows below say.

| Claim | Status | What backs it |
|---|---|---|
| The Explore page is free, open, and ungated | `TRUE` | `ui/src/routes.js:43` puts `explore` in `ANON_APP_PAGES`, so it renders with no session; `backend/archimedes/api/explore_routes.py:24` and `:30` carry no auth dependency. The code is public domain (`LICENSE`). |
| Explore is a FOSS viewer over yfinance streams, not a redistribution product | `TRUE` | The mechanism is true and labelled on the page — `ui/src/components/Explore.jsx:380` tells the visitor which cards are oracle-priced and which come from yfinance, and `ui/src/components/AssetModal.jsx:22` labels the source per asset. The licensing position is stated in `docs/adr/market-data-sourcing.md` (landed with #1627): split sourcing, no commercial redistribution of yfinance data, Tiingo Business named as the mainnet prerequisite. |
| Paid analysis runs on licensed data | `PENDING ADR MERGE` | A statement of policy, not of current state, and the ledger must not launder one into the other. The vendor seam exists on both sides — `analytics-engine/src/archimedes_analytics_engine/market_data.py:96` and `backend/archimedes/services/market_data_provider.py:358` (a real Tiingo provider) — and one `MARKET_DATA_PROVIDER` value selects across both. **The default on both seams is still `yfinance`**, so today the paid path and the free path read the same source. |
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

1. **`ui/src/components/Landing.jsx:78`** — the comment block above `BOARD_FDR` says the
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
