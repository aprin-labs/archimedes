<!--
The public front door. It replaced `Home: README.md` — the doc register — which
is why the left rail and the right rail used to render the same sixteen labels
on `/`. The register now lives at doc-index.md.

Two rules for anyone editing this page.

1. EVERY NUMBER IS A LIVE READ, PRINTED BESIDE THE ENDPOINT THAT PRODUCED IT,
   WITH THE DATE IT WAS READ. The docs build is hermetic on purpose
   (backend/tests/test_docs_site.py: "reads committed YAML and markdown off
   disk. No DB, Redis, RPC, network, or .env"), so nothing here is fetched at
   build time. If you cannot re-read a number from its endpoint, delete it —
   docs/CONVENTIONS.md § 4: "an absent number beats a substituted one".

2. THE ANALOGY FRAMING THE OWNER RETIRED ON 2026-09-01 MUST NOT COME BACK —
   not in prose, and not in a comment on this page: comments here are
   published verbatim in the page source. The identity is "Portfolio
   strategy, under scrutiny." — the product's own words,
   ui/src/components/Landing.jsx:231.
-->

# Archimedes documentation

**Portfolio strategy, under scrutiny.**
*Research. Rigor. Proof.*

Describe what you want from a portfolio in plain English. Archimedes proposes strategies
grounded in named quantitative-finance research, then spends the rest of its effort trying
to reject them — four checks that run **outside** the generator, on persisted returns, so
the thing being graded cannot influence its own grade
([why the gate lives outside the generator](adr/k1-generation-external-rigor-gate.md)).
The verdict is recorded whichever way it lands.

## Start where you are

<div class="grid cards" markdown>

-   **I want to try it**

    ---

    The journey, end to end — what each screen is for, in the order it happens:
    [the product spine](user-stories.md). Then
    [writing a brief that works](writing-a-brief.md) and
    [what you can build over](asset-universe.md).

    

-   **I want to check the method**

    ---

    [What the four checks actually do](rigor-methods.md), the
    [reading order for the math](quant/README.md), and the
    [papers the gate rests on](cited-literature.md) — two of the five are cited
    against us, deliberately.

-   **I'm building an agent**

    ---

    [Zero to paper-traded in eleven steps](agent-quickstart.md), with the exact
    response shapes and an error table. Then the
    [HTTP API reference](api/README.md) and the
    [auth model](security/auth-model.md).

    

</div>

## What ships today

| Step | Where it stands | Read next | The endpoint or file behind it |
|---|---|---|---|
| **Generate** | Live and paid. `"price":"$2.000000"`, `"asset":"USDC"`, `"chain":"arcTestnet"`, `"dry_run":false`, `"halted":false`. Each account gets 3 free generations once its email is verified. | [Generation API](api/generation.md) · [the quote contract](specs/generation-quote-contract.md) | `GET /api/generate/quote`; [`free_generations.py:68`](../backend/archimedes/services/free_generations.py#L68) |
| **Rigor gate** | Computed server-side on persisted daily returns, never inside the generator. Four verdicts, not two: `pass` / `fail` / `pending` / `degenerate`. A strategy with fewer than 10 real daily returns is `pending` — never a fixture value. No aggregate score is published. | [The four checks](rigor-methods.md) · [thresholds](quant/admission-criteria.md) | [`live_rigor_gate.py:51-54`](../backend/archimedes/services/live_rigor_gate.py#L51), [`:48`](../backend/archimedes/services/live_rigor_gate.py#L48) |
| **Explore** | 18,907 q-fin metadata rows, of which 0 have been through the enrichment pipeline and 0 clusters exist. 281 assets in the oracle universe. Retrieval over the corpus is **lexical**. | [What a passport contains](specs/strategy-passport-spec.md) · [the corpus](corpus-architecture.md) | `GET /api/corpus/overview`; `GET /api/health` → `oracle_universe_count`, `corpus_embedded_at_rest` |
| **Paper trade** | Free, and it is the execution engine's venue: an append-only forward-return ledger with intraday marks. Nothing settles on-chain from it. | [Paper-trading API](api/paper-trading.md) · [intraday marks](plans/2026-08-30-intraday-paper-trading.md) | `POST /api/paper/deployments`, `GET /api/paper/deployments/{id}/marks` |

*Read from production on **2026-09-02**, against `main` at `9cb868eb`. These are live reads,
printed here beside the endpoint that produced them — this page fetches nothing at build
time. **If a number here disagrees with its endpoint, the endpoint is right** — please
[file a docs correction](https://github.com/aprin-labs/archimedes/issues/new).*

## What it does not do

- **It does not trade with your capital.** The reachable act-on step is simulated: a paper
  deployment writes to a forward-return ledger, not to a venue.
- **Testnet only.** Arc public testnet, chain `5042002`
  ([`chain/client.py:149`](../backend/archimedes/chain/client.py#L149)), with faucet USDC. A
  mainnet cutover is not scheduled and there is no date for one.
- **Vaults, marketplace, publish, subscriptions, portfolio and learnings are out of the
  current cut.** They are hidden behind one flag, off by default
  ([`featureFlags.js:28`](../ui/src/featureFlags.js#L28), page list at
  [`:57`](../ui/src/featureFlags.js#L57)). Where the docs describe them, they sit on one
  shelf marked *Roadmap — not in the current cut*.
- **Retrieval is lexical.** There is no stored embedding index and no knowledge graph:
  `GET /api/health` reports `corpus_embedded_at_rest: false` with the reason *"no
  stored-vector column in the schema"*, and `corpus_kg_built: false` with
  `corpus_kg_entities: 0`, `corpus_kg_relations: 0`. Nothing on this site should describe
  the corpus search as semantic.
- **Reasoning traces are hashes, not pinned documents.**
  `ReasoningTraceRegistry.reveal` is called with an empty `storagePointer`; the on-chain
  keccak256 of the canonical trace bytes is the integrity anchor and the trace itself lives
  in our own store. Nothing is pinned to IPFS
  ([`adr/ipfs-pinning-not-live.md`](adr/ipfs-pinning-not-live.md)), and a hash proves a
  trace *existed*, not that it *caused* the trade
  ([`specs/commit-reveal-trace-spec.md`](specs/commit-reveal-trace-spec.md)).

## How we keep this honest

- **[Claims ledger](claims-ledger.md)** — every public claim, a per-claim verdict
  (`TRUE` / `CHANGED` / `RETRACTED` / `OVER-CLAIMED` / `PENDING ADR MERGE`), and the
  `file:line` that backs or retracts it. A test
  ([`test_claims_ledger.py`](../backend/tests/test_claims_ledger.py)) fails the build when a
  citation stops resolving. The open over-claims are in the same table as the true ones.
- **[API surface status](api-surface-status.md)** — a CI-enforced census of every router
  the backend mounts, including the 16 of 30 that have no per-surface reference doc yet.
  The gaps are listed rather than left for a reader to discover.

---

**The live product:** [archimedes-arc.com](https://archimedes-arc.com) (Arc public testnet) ·
**Source:** [github.com/aprin-labs/archimedes](https://github.com/aprin-labs/archimedes)
(the Unlicense) · **Every doc in the tree:** [the doc register](doc-index.md) ·
**Something here is wrong:** [open an issue](https://github.com/aprin-labs/archimedes/issues/new).
