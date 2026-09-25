# Repo Map — Frontend / UI / UX

> **Historical snapshot — 2026-07-04.** Findings describe the repository at that scout run and may be resolved on current HEAD. Re-run cited checks before acting.

Scope at capture: `ui/src` (React 19 + Vite 8 + viem + Circle Modular Wallets). No routing lib
(hand-rolled history routing in `App.jsx`). No test runner existed at capture time. Findings ranked by severity.

## Files Retrieved

- `ui/src/App.jsx` (1-300) — root: routing, wallet reconnect, WalletGate wiring, funnel beacon.
- `ui/src/api.js` (1-46) — shared `apiGet`/`apiPost` (both `credentials:'include'`).
- `ui/src/config.js` (1-560) — wallet connect/reconnect, EIP-6963 discovery, SIWE signer, ABIs, **hardcoded contract + ASSET addresses**.
- `ui/src/siwe.js` (1-90) — SIWE auth, `checkSession`, `logout`; uses `VITE_ARC_CHAIN_ID`.
- `ui/src/circle-wallet.js` (1-290) — passkey register/login/rehydrate.
- `ui/src/circle-tx-executor.js` (1-140) — bundler user-op batching + error map.
- `ui/src/components/WalletConnect.jsx` (94-125) — connect → SIWE (failure swallowed).
- `ui/src/components/WalletGate.jsx` (1-70) — gates on `walletAddr` presence only.
- `ui/src/components/Generate.jsx` (30-90) — lazy `ensureSiweSession` on 401.
- `ui/src/components/DepositFlow.jsx` (1-720) — EOA 3-step + passkey batched deposit.
- `ui/src/components/Portfolio.jsx` (120-165) — raw `fetch` polling, no credentials.
- `ui/eslint.config.js` (1-75) — aspirational react-hooks rules disabled.
- `ui/package.json` — **no test script/deps**.
- `ui/.env.example` — no `VITE_ARC_CHAIN_ID`.

## Ranked Findings

### 1. Auth desync on reload — "connected" ≠ authenticated (HIGH)

Evidence: `App.jsx:106-110` `reconnectWallet().then(r => setWalletAddr(r.address))` on
mount restores wallet **without** re-running SIWE or `checkSession()`. `WalletGate.jsx:14`
gates purely on `walletAddr`. SIWE cookie is httpOnly + session-scoped; after expiry/new
browser session the user sees gated pages (Portfolio/Library/Learnings/Reasoning) as
"logged in" but PII endpoints 401. `WalletConnect.jsx:107-110` catches SIWE failure and
only `console.warn('SIWE auth failed (non-fatal)')` — no user signal. Only `Generate.jsx`
recovers (lazy `ensureSiweSession` on 401); other gated pages don't.

- Impact: silent empty/blank personalized data; violates "claims must be true" (page says "Your Strategies" but session gone).
- Next issue title: `APIN - Frontend - Reconcile SIWE session with wallet reconnect (checkSession gate + re-auth on 401)`

### 2. Contradictory corpus-size claim: 1,014 vs 10,000 papers (HIGH — claim integrity)

Evidence: `App.jsx` Generate WalletGate desc = "1,014-paper q-fin corpus";
`OnboardingTour.jsx:103` = "1,014 papers"; but `Architecture.jsx:10,87,109,120,320,342`
and `CorpusExplorer.jsx:44` = "10,000-record" / backend `total=10000`. Two hardcoded
numbers disagree, and both are hardcoded rather than read from the live
`/api/papers/` total. Direct hit on the #1 rule (every UI claim true on the live path).

- Next issue title: `APIN - Frontend - Unify corpus-size claim to live count (drop hardcoded 1,014 / 10,000)`

### 3. Inconsistent API client — raw fetch bypasses credentials + error handling (MED-HIGH)

Evidence: `api.js` helpers send `credentials:'include'`, but ~15 components hit
`${API_BASE}/...` with bare `fetch` (grep of `import.meta.env.VITE_API_BASE`).
`Portfolio.jsx:131,142` (`/api/agent/status`, `/api/traces/`) and most others omit
`credentials`; only `Generate.jsx:88`, `GenerationStatus.jsx:73`,
`WelcomeProfileModal.jsx:73`, `Layout.jsx:81` include it. Any auth-scoped endpoint
reached via bare fetch won't send the SIWE cookie → silent unauthenticated reads.
Also divergent error handling (raw fetch has no 502/HTML guard that `apiGet` provides).

- Next issue title: `APIN - Frontend - Route all API calls through apiGet/apiPost (credentials + error normalization)`

### 4. Hardcoded contract + asset addresses in config.js (MED)

Evidence: `config.js` `NEW_CONTRACTS`, `ASSETS[].{oracle,vault,token}`, `USDC` all
inlined. Duplicates backend addresses (roadmap T2.3 wants these externalized out of
`client.py`); no `VITE_` env, so a redeploy needs a code edit + rebuild and can silently
drift from backend. Stale-address risk routes user funds to wrong vault.

- Next issue title: `APIN - Frontend - Externalize contract/asset addresses to build-time env (parity with backend)`

### 5. Zero frontend tests / no test runner (MED)

Evidence: `package.json` scripts = dev/build/lint/preview only; no vitest/jest/testing-
library. Untested surfaces are the highest-risk ones: SIWE message construction
(`siwe.js`), ERC-6492 wrap branch (`config.js:signSiweMessage`), passkey register/login
mode split (`circle-wallet.js`), bundler error map (`circle-tx-executor.js`), deposit
state machine (`DepositFlow.jsx`). CI `quality-gate.yml` only runs `npm run lint`.

- Next issue title: `APIN - Frontend - Add vitest + unit tests for siwe/config/circle-wallet/deposit state machine`

### 6. ESLint disables data-fetching correctness rules (MED)

Evidence: `eslint.config.js` `DISABLED_ASPIRATIONAL` turns off
`react-hooks/set-state-in-effect` and `react-hooks/refs`. Documented as "days of refactor
w/o react-query," but this masks the exact fetch-then-setState + `ref.current`-in-render
patterns that cause the auth/data races above (e.g. `App.jsx` mount effects,
`Portfolio.jsx` polling, `CorpusGraph` dimension reads). Real bug-surface, not just style.

- Next issue title: `APIN - Frontend - Adopt a data-fetching layer (react-query/SWR) and re-enable react-hooks rules`

### 7. Undocumented `VITE_ARC_CHAIN_ID` flag (MED)

Evidence: `siwe.js:15` reads `VITE_ARC_CHAIN_ID ?? '5042002'` and bakes it into the SIWE
message `Chain ID` line, which must match backend `_EXPECTED_CHAIN_ID`. Not present in
`ui/.env.example`. A chain change flips backend but not the client default → silent SIWE
verification failure (auth break) with no docs pointing at the fix.

- Next issue title: `APIN - Frontend - Document VITE_ARC_CHAIN_ID in ui/.env.example`

### 8. DepositFlow allocation ignores the chosen strategy (MED — UX/claim gap)

Evidence: `DepositFlow.jsx:defaultAllocations()` = equal-weight across the **first 4**
`ASSETS` (75% synth / 25% USDC) for **every** deposit, EOA and passkey. Step label says
"Configure the target portfolio allocation" and passkey summary says "Set target
allocation across N synthetics," but the allocation is a hardcoded generic, not the
generated/selected strategy's weights. User signs an on-chain allocation that doesn't
match the strategy they deployed.

- Next issue title: `APIN - Frontend - Drive DepositFlow allocations from the selected strategy, not a hardcoded default`

### 9. Mock-data panels could read as live (LOW-MED — claim adjacent)

Evidence: `RiskAnalysis.jsx`, `BacktestVisualizer.jsx`, `PortfolioAdvisorPanels.jsx`
render `buildMock*` defaults; Quant Lab intro (`App.jsx`) + `RiskAnalysis.jsx:529`
disclaim "illustrative/mock" — honest, but the numbers still look like measurements. A
judge screenshotting Quant Lab sees plausible Sharpe/VaR that are synthetic.

- Next issue title: `APIN - Frontend - Strengthen mock-data affordance on Quant Lab panels (watermark/badge)`

### 10. Reasoning page claim vs on-chain reality (LOW-MED)

Evidence: WalletGate reasoning desc (`App.jsx`) = "hashed off-chain and anchored on Arc
via the ReasoningTraceRegistry"; but `Reasoning.jsx:306,360,374` show "Not yet anchored
on-chain" / "commit-reveal wiring is on the roadmap." The gate copy overstates vs the
per-trace honest state. Confirm live anchoring path before the gate copy claims it.

- Next issue title: `APIN - Frontend - Align Reasoning gate copy with actual on-chain anchoring state`

### 11. Portfolio polls background tabs (LOW)

Evidence: `Portfolio.jsx:155-160` 30s `setInterval` (4 loaders) with no `document.hidden`
guard; interval cleanup is present. Wastes chain/API calls in hidden tabs.

- Next issue title: `APIN - Frontend - Pause Portfolio polling on document.hidden`

## Architecture Notes

- Single-file hand-rolled router in `App.jsx` (`resolveRoute`/`pageToPath` + `popstate`);
  every route is a `switch` case. Adding routes touches 3 maps.
- Wallet state is module-global singletons in `config.js` (`_address`, `_walletClient`,
  `_smartAccount`), synced to React via `window` CustomEvents (`wallet-changed`,
  `wallet-chain-changed`, `open-wallet-modal`). No context/provider — components re-derive
  via `getConnectedProvider()`/`getAddress()`.
- Two wallet paths: EOA (viem `writeContract`, N popups) vs Circle passkey MSCA
  (bundler `sendUserOperation`, 1 biometric, gas-sponsored). `DepositFlow` branches on
  `getConnectedProvider() === CIRCLE_PROVIDER_ID`. SIWE signer branches in
  `config.js:signSiweMessage` (ERC-6492 wrap for undeployed accounts).
- SIWE is best-effort/optional today (swallowed failures + lazy re-auth); backend gating
  appears flag-driven (`REQUIRE_SIWE_FOR_GENERATION`, per Generate.jsx comment).

## Start Here

Open `ui/src/App.jsx` (mount `useEffect` reconnect, `renderPage` WalletGate wiring) +
`ui/src/components/WalletConnect.jsx:94-125` (connect→SIWE swallow). Those two frame
Findings #1–#3, the highest-leverage cluster.

## Clarification Questions

1. Corpus size ground truth — is the live `/api/papers/` total 1,014 or 10,000? (Finding #2 remediation direction depends on the real number.)
2. Is `REQUIRE_SIWE_FOR_GENERATION` currently ON in prod? Determines whether Finding #1/#3 are live breakage or latent.
3. Are `/api/agent/status`, `/api/traces/`, `loadVaults` endpoints intended public (no session)? If yes, #3 is style-only; if PII-scoped, it's a live bug.
4. Is on-chain reasoning-trace anchoring live or roadmap (Finding #10)? Governs whether gate copy is a claim violation.
5. Should DepositFlow allocations come from the strategy passport payload, and does that field exist server-side yet (Finding #8)?
