# Archimedes

*Research. Rigor. Proof.*

[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue.svg)](LICENSE)
[![Settled on: Arc](https://img.shields.io/badge/settled%20on-Arc-2A4DD1.svg)](https://www.arc.network/)

**Portfolio strategy, under scrutiny.** Archimedes is an agentic strategy generation and
validation system, grounded in research and statistical rigor. You describe what you want
from a portfolio in plain English; it proposes strategies drawn from a corpus of arXiv
quantitative-finance preprints, and then the part that makes it different — the honest
validation layer — spends its effort trying to reject every one of them: a deflated Sharpe
ratio, a probability of backtest overfitting, a walk-forward out-of-sample pass, and a
static look-ahead audit, with the measured verdict recorded whichever way it lands.
Survivors run as paper deployments, so a gated strategy's results play out in the open
with full provenance.

Live at **<https://archimedes-arc.com/>**, running against Arc testnet.

## The spine

```
generate  →  rigor-gate  →  execute (paper)  →  explore      (roadmap: vaults → monitor)
```

**Generate** and **rigor-gate** are the shipped product, and so is **explore** — the
reasoning traces, the rejected alternatives, the paper provenance behind every proposal.
**Execute** ships as paper: strategies that survive the gate run as paper deployments —
the same decision core a vault will one day use, executing against an append-only paper
trade ledger instead of a chain, so results accrue in the open with nothing at stake.
**Vault execution** and **monitor** are roadmap. The `Vault` / `VaultFactory` contracts are written
and deployed to Arc testnet, but the deploy-a-vault journey is gated off every public
surface behind `ROADMAP_SURFACES_ENABLED`
([`ui/src/featureFlags.js`](ui/src/featureFlags.js), off by default), and no user vault has
been deployed. When it ships, a strategy that survives the gate will be deployable into a
non-custodial vault on Arc, and the agent will rebalance it on a schedule. Today it will
not. The locked spine is [`docs/user-stories.md`](docs/user-stories.md).

## Three things to know before anything else

- **Most briefs fail the gate. That is the product working.** A strategy that fails is shown
  to you with its DSR, PBO and out-of-sample numbers, so you can see exactly why. How many
  strategies in the curated library currently pass is **unestablished** — the live gate is
  the only authority on that, and this file will never quote a count.
- **Research marketplace, not a casino.** Payments are real (USDC on Arc); marketplace
  settlement is stubbed behind `PAYMENTS_DRY_RUN` pending mainnet. Single-user MVP —
  multi-user library and social features are roadmap.
- **Arc testnet only** (chain `5042002`). Faucet USDC comes from
  <https://faucet.circle.com/> (20 USDC / 2h — on Arc, USDC *is* gas). **No mainnet
  money.** Generation still settles real testnet USDC — read `GET /api/generate/quote`
  (prod answers `dry_run: false`). Arc has no mainnet yet; mainnet launch, real-funds
  custody, and the regulatory architecture are roadmap.

## The rigor gate

The gate is evidence, not proof. The deflation prices in how many candidates were searched
before this one was picked; the DSR bar is 0.95 — a one-sided 5% test — and it has exactly
one definition in the tree (`DSR_P_BADGE_MIN` in
`backend/archimedes/services/rigor_profiles.py`, #1794). PBO is computed and disclosed on every passport but does not block the badge while the
library holds fewer than ten graded strategies — below that, CSCV lacks the power to gate
honestly, so it reports `NOT_RUN` with the reason rather than a pass. A check that cannot
run says so; it never reports a silent pass.

Full method and thresholds: [`docs/rigor-methods.md`](docs/rigor-methods.md) and
[`docs/specs/selection-bias-corrections-spec.md`](docs/specs/selection-bias-corrections-spec.md).
The papers the gate rests on, including the two cited against us:
[`docs/cited-literature.md`](docs/cited-literature.md).

## How a strategy gets made

A brief fans out across a regime × mechanism steer grid. Each proposer selects candidate
papers from the corpus, and fusion turns them into a strategy spec in the internal DSL.
Deterministic critics then cull the pool with zero LLM calls: provenance and embargo audit,
a real backtest per survivor, and a null check that a candidate must beat buy-and-hold by at
least 5 bps net — if none clears it, the run abstains rather than shipping a weak winner. A
deterministic synthesizer ranks what is left; only the K=1 winner is persisted, and the
rejected alternatives are kept and surfaced so you can see what was tried. The externalized
rigor gate then runs on the winner, outside the debate.

Mechanism in full: [`docs/specs/multi-agent-debate-spec.md`](docs/specs/multi-agent-debate-spec.md).

## Quickstart

### The live site

<https://archimedes-arc.com/> runs against Arc testnet. Sign in with email and password,
describe a brief, and read the verdict.

### Run it locally

```bash
git clone --recurse-submodules https://github.com/aprin-labs/archimedes.git
cd archimedes
cp .env.example .env
# REQUIRED: generate a local auth secret, then paste it after BETTER_AUTH_SECRET=
python -c "import secrets; print(secrets.token_urlsafe(48))"
docker compose up -d --build
```

Then open <http://localhost:8080>. The backend shares that ingress:
<http://localhost:8080/health> for the honesty flags, <http://localhost:8080/docs> for the
API. LLM credentials are optional — without one, generation uses the canned fallback.

**[`SETUP.md`](SETUP.md) is the full walkthrough** — prerequisites, platform notes
(macOS / Linux / WSL2), host tooling for frontend and contract work, and the test suite.
`make help` lists the dev targets.

### The CLI

The `archimedes` CLI runs the rigor gate over your own returns series. It is **not on PyPI
yet**, so install it from this repo:

```bash
conda env create -f environment.yml   # first time only
conda activate archimedes
pip install -e ./cli
```

Check the install and read the machine-readable contract — no network, no account:

```bash
archimedes --version        # archimedes, version 0.1.0
archimedes manifest         # JSON: every command, flag, exit code, and cost class
```

Sign in (Better Auth email + password — no wallet signature), then read your meter and run
the gate:

```bash
archimedes login            # prompts; or set ARCHIMEDES_EMAIL / ARCHIMEDES_PASSWORD
archimedes meter            # today's generation usage + the live price quote
archimedes verify returns.csv --trials 40
```

`returns.csv` is two columns — date and daily return — or `-` to read stdin; a header row is
skipped automatically. Exit codes are a stable contract, and the split that matters is **`1`
vs everything else**: `1` means the gate ran and returned a failing verdict, a real answer.
Any other non-zero means no verdict was produced, so branch on `1` specifically:

```bash
archimedes verify returns.csv
case $? in
  0) echo "gate passed" ;;
  1) echo "gate failed, not deploying"; exit 1 ;;
  *) echo "verify did not run"; exit 2 ;;
esac
```

The codes: `0` passed · `1` gate ran and failed · `2` bad input or no session · `3` not
implemented in this release · `4` the gate was reached but not every runnable leg could be
evaluated. **`4` is a known gap in the published contract** —
[`cli/src/archimedes_cli/exits.py`](cli/src/archimedes_cli/exits.py) defines it and `verify`
exits with it, but `archimedes manifest` still publishes only `0`–`3`. The `case` above
handles it correctly regardless, because it treats every non-`1` non-zero as "no verdict".

`verify` sends only numbers; your strategy code is never uploaded. Two of the gate's four
checks cannot run over a bare returns series — PBO needs a trial matrix and the look-ahead
audit needs strategy source — so both always report `not_evaluable` with a reason, never a
silent pass. `archimedes backtest` and `archimedes verify --local` are not implemented yet
and exit `3`.

Full reference: [`cli/README.md`](cli/README.md) and
[`skills/archimedes-cli/SKILL.md`](skills/archimedes-cli/SKILL.md).

## Documentation

[`docs/doc-index.md`](docs/doc-index.md) is the register — **a doc not listed there does not
exist.** The same tree is published, curated, at <https://docs.archimedes-arc.com>.
The entry points:

| If you want to… | Read |
|---|---|
| Run it locally, including the test suite | [`SETUP.md`](SETUP.md) |
| Know what the product *is* (the locked spine) | [`docs/user-stories.md`](docs/user-stories.md) |
| See the architecture map and the stack | [`docs/architecture.md`](docs/architecture.md) |
| Understand the rigor gate's math | [`docs/rigor-methods.md`](docs/rigor-methods.md) |
| Read the papers the claims rest on | [`docs/cited-literature.md`](docs/cited-literature.md) |
| Audit our public claims one by one | [`docs/claims-ledger.md`](docs/claims-ledger.md) |
| Understand the paper corpus end to end | [`docs/corpus-architecture.md`](docs/corpus-architecture.md) |
| Drive the whole journey programmatically | [`docs/agent-api.md`](docs/agent-api.md) |
| Go zero-to-paper-traded as an external agent | [`docs/agent-quickstart.md`](docs/agent-quickstart.md) |
| Use the command-line tool | [`cli/README.md`](cli/README.md) |
| Load a grounded agent skill (every claim file:line cited) | [`skills/README.md`](skills/README.md) |
| Understand Arc / Circle integration | [`docs/arc-integration.md`](docs/arc-integration.md) |
| Operate the live stack | [`docs/runbooks/operations.md`](docs/runbooks/operations.md) |
| Browse every design + planning doc | [`docs/doc-index.md`](docs/doc-index.md) |
| Add a doc without misfiling it | [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md) |
| Write a test the way this repo wants | [`docs/testing-conventions.md`](docs/testing-conventions.md) |
| Know who owns what | [`docs/team.md`](docs/team.md) |
| Get context for a Claude Code session | [`CLAUDE.md`](CLAUDE.md) |

**Live numbers come from the live system, never from this file.** Contract census is
`GET /api/config/contracts`, honesty flags are `GET /health`, and the test count is
`pytest --collect-only -q | tail -1`. This file quotes no counts that a reader cannot
re-derive from one of those.

## Known limitations (Arc testnet)

- **The corpus is arXiv preprints, not peer-reviewed papers**, and it holds metadata and
  abstracts only — the row count `/health` publishes as `corpus_papers` is a manifest
  import, not a measure of anything analysed. Do not freeze that count in prose; the
  corpus probe can timeout. Candidate selection over it is a **keyword
  filter**. Only that already-selected candidate set is then re-scored, at request time,
  across title and abstract — by `all-MiniLM-L6-v2` when that model is loaded in-process, by
  lexical TF-IDF when it is not. Nothing is precomputed: the `papers` schema carries title
  and abstract text and no vector column, so no index is built ahead of the request.
  `/health` names the scorer that is actually live in `paper_rag` and `paper_rag_reason`,
  publishes `corpus_embedded_at_rest: false` for the corpus itself, and publishes
  `rerank_candidate_cap` because only that many candidates reach the model. Read those
  fields rather than this line. Tracked in
  [#778](https://github.com/aprin-labs/archimedes/issues/778) and
  [#1488](https://github.com/aprin-labs/archimedes/issues/1488).
- **The knowledge graph is not built.** No KB artifact has ever been produced, so `/health`
  reports `corpus_kg_built: false` with zero entities and zero relations,
  `GET /api/corpus/graph` refuses with **503 `kb_artifact_not_found`** instead of
  synthesizing a graph, and `GET /api/corpus/kg/*` returns empty entity and relation sets.
  Citation-link extraction over the corpus is roadmap. Tracked in
  [#778](https://github.com/aprin-labs/archimedes/issues/778).
- **Vault execution is not shipped**, per the spine above. The contracts are deployed and
  the routes exist, but the journey is flag-gated off every public surface and no user vault
  has been deployed.
- **AMM pools are thinly funded**, so many swaps are not executable. The agent's liquidity
  guard skips empty pools and logs the reason instead of routing capital into a doomed trade
  — see [`docs/arc-integration.md`](docs/arc-integration.md).
- **Not every reasoning trace is anchored on-chain.** The agent writes a trace for every
  decision it reaches, including a `skip`, and a decision that produced no transaction has
  no hash to verify. Read `GET /api/traces/` and check `arc_tx_hash` before treating a trace
  as anchored.

Every public claim this repo makes, with a per-claim verdict and the `file:line` that backs
it, is tracked in [`docs/claims-ledger.md`](docs/claims-ledger.md).

## Contributing

Fork, branch, PR to `main`. The branch model in one paragraph:

- **`main` is the only long-lived branch, and it is the deploy branch** — every merge builds
  and deploys to the live stack. There is no `develop`. `main` moves continuously; branch
  late, rebase right before merging, and merge in a tight window.
- Short-lived per-owner branches, `<your-handle>/<short-name>` → PR → merge → delete.
- **Merge commits only.** Squash- and rebase-merge are disabled in repo settings; use
  `gh pr merge <n> --merge`.
- **Close the issue from the PR body with a real keyword** — `Closes #123` / `Fixes #123` /
  `Resolves #123`, the keyword immediately before each `#`, repeated once per issue
  (`Closes #1 and #2` closes only #1). Use `Part of #123` for a deliberate non-closing
  reference. [`.github/pull_request_template.md`](.github/pull_request_template.md) prompts
  for it.
- One logical change per PR. Never force-push `main`. Never commit secrets or `.env`.
  Force-pushing your *own* unmerged branch is fine and expected.

Full conventions — review rules, CI gates, commit style —
are in [`CLAUDE.md`](CLAUDE.md); testing conventions are in
[`docs/testing-conventions.md`](docs/testing-conventions.md) and doc conventions in
[`docs/CONVENTIONS.md`](docs/CONVENTIONS.md).

## License

[Unlicense](LICENSE) — full public-domain dedication. Use, modify, distribute freely. No
warranty.
