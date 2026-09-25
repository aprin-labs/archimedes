# IPFS-Pinned Reasoning Traces — Design Note

> **status:** archived (design note, never a spec)
> **owner:** Dan Browne
> **updated:** 2026-09-01
> **superseded-by:** [`../adr/ipfs-pinning-not-live.md`](../adr/ipfs-pinning-not-live.md)
>
> **ARCHIVED 2026-09-01.** This note proposed pinning traces to IPFS via Pinata.
> The pin client shipped later as code, but `PINATA_JWT` was never injected into
> prod ECS secrets, so pinning never ran. #1526 removed the dead path. The
> live reveal is hash-only (empty `storagePointer`). Kept so the alternatives
> table and pricing notes remain readable; do not implement from this page.
>
> **Status (original):** Design note, not a spec. Written 2026-05-20 after observing
> Rosetta-Alpha's hash → IPFS → on-chain pattern. Not implemented yet; this
> note exists to make the call before we build.
>
> **Author:** Daniel B. + Claude session, dbrowneup/strip-to-spine.
>
> **Reading list:**
> - [`docs/user-stories.md`](../user-stories.md) §④ Monitor — the reasoning-trace contract from the user's POV
> - [`backend/archimedes/api/routes.py`](../../backend/archimedes/api/routes.py) `publish_trace` — the current off-chain + on-chain anchor flow
> - [`contracts/src/ReasoningTraceRegistry.sol`](../../contracts/src/ReasoningTraceRegistry.sol) — the on-chain anchor today (32-byte hash only)
> - [`docs/specs/strategy-passport-spec.md`](strategy-passport-spec.md) — the trust-primitive contract this slots into
> - Rosetta-Alpha submission (Mihai-Codes/rosetta-alpha) — the prior art that motivated this

## The problem this solves

Today the reasoning-trace flow is:

1. Agent (or `construct_strategy`/fusion) builds a `ReasoningTrace`
2. Backend computes `keccak256(canonical_json(trace))` → 32-byte hash
3. Hash is anchored on Arc via `ReasoningTraceRegistry.publishTrace()`
4. Full trace payload is saved off-chain in **Redis only**

The hash is the integrity primitive — anyone can recompute it from the full
trace and verify against the on-chain anchor. But **the full trace lives in
our Redis.** If our Redis goes away, the on-chain hash is orphaned: verifiable
in form, useless in substance. A judge or third party can confirm "a trace
existed at block N" but cannot read what the agent actually reasoned.

This is the load-bearing weakness of the current trust story.

## What IPFS pinning buys us

Replace step 4 with:

4. Full trace payload is canonicalized + pinned to IPFS → returns a CID
5. CID is included on-chain alongside the hash (or the hash *is* the CID's
   content hash — they're related but not identical, see §"Hash vs CID" below)

Now the trust story becomes: **anyone, anywhere, with no access to our infra,
can fetch the full reasoning from a public IPFS gateway and recompute the
hash to verify it matches the on-chain anchor.** That is what Rosetta-Alpha
shipped and it's a strictly stronger provenance claim than ours today.

Concretely, the user-facing version:

> "Here is the agent's decision at block N. The 32-byte hash is on Arc.
> The full reasoning lives at `ipfs://bafy...`. Fetch it from any gateway.
> Recompute keccak256 of the canonical bytes. The numbers match. The agent
> can't have rewritten its reasoning after the fact — both ends are
> publicly anchored."

That sentence is the wedge.

## Hash vs CID — the subtle bit

We currently anchor `keccak256(canonical_json)`. IPFS CIDs are
`multihash(content)` — typically sha256, encoded as a base32 CID v1.

These are not interchangeable. Options:

- **(A) Anchor both:** registry stores `(keccak256_hash, ipfs_cid)`. Backwards
  compatible with current contract semantics (the hash field stays); CID is
  additive. Slightly more gas, but trivially small (one extra `bytes` field).
- **(B) Replace keccak with CID:** registry stores only the CID. Cleaner but
  breaks every existing verification path and the current `verifyTrace(id,
  canonicalBytes)` contract method which computes keccak256.
- **(C) Anchor keccak, pin to IPFS, link off-chain only:** registry unchanged;
  the off-chain trace record carries the CID as a metadata field. Cheapest to
  ship; weakest claim (no on-chain proof that the CID was committed at the
  same time as the hash).

**Recommendation: (A).** Strengthens the on-chain claim without breaking the
existing `verifyTrace` flow. Migration is contract-additive, not replacing.

## The honest cost analysis

The user's concern was correct: Pinata isn't free at scale.

**Pinata pricing (2026-05-20, public pricing page):**
- Free: 1 GB pinned + 100 GB/mo bandwidth
- Picnic ($20/mo): 50 GB pinned + 250 GB/mo bandwidth
- Fiesta ($100/mo): 250 GB pinned + 1 TB/mo bandwidth

**Our trace size estimate:**
- Canonical JSON of a typical `ReasoningTrace` ≈ 2-5 KB (market context +
  portfolio before/after + ~500 char reasoning + a handful of trades + a few
  strategy IDs). Call it 4 KB average.
- One pin per agent decision. If the autonomous agent rebalances 4×/day per
  vault and we run 10 vaults → 40 traces/day → ~14k/year → ~56 MB/year of
  pinned data. Cheap.
- If we scale to 1,000 vaults → 4M traces/year → 16 GB/year. Still fits
  in the free tier on storage; bandwidth is the constraint (downloads).
- If we scale to 100k vaults → 400M traces/year → 1.6 TB/year. **Now we have a
  real bill** ($100/mo Fiesta is plausibly tight, then it's enterprise pricing
  or self-hosted.)

So at hackathon-to-pilot scale the cost is negligible. Cost only becomes a
serious lever at the "real product, real volume" scale that's well beyond
the current MVP. **This is not a reason to defer; it's a reason to pick a
plug-replaceable abstraction so the back-end can swap to a self-hosted IPFS
cluster or Filecoin-anchored storage later.**

## Options ranked

| Option | Setup effort | Demo readiness | Scaling path | Risk |
|---|---|---|---|---|
| **Pinata (paid SDK)** | 30 min — sign up, API key, `pinata-sdk` pip install | High — judges can fetch CIDs in browser via `ipfs.io` gateway | Switch to self-hosted at scale; CIDs are portable | Pinata account on the team's name; pricing risk at scale |
| **Local IPFS node in docker-compose (Kubo)** | 60 min — add `ipfs/kubo` service, mount data volume, point pin client at it | Medium — CIDs only fetchable via local gateway unless we also pin to public gateway | Self-hosted from day one | Local node disk usage; pinned data evicted on container restart unless volume-mounted |
| **Helia (JS IPFS node embedded in backend)** | 90 min — npm dep, embed in Node sidecar or run-in-Python via subprocess | Low — embedded nodes are flaky in containers; uptime depends on backend uptime | Bad — embedded nodes don't scale | Doesn't add value over Kubo |
| **Filecoin pin via web3.storage / NFT.Storage** | 30 min — same shape as Pinata | High — public gateway, IPFS+Filecoin redundancy | Free for storage; rate-limited bandwidth | NFT.Storage shutdown rumors as of late 2024; check current status |
| **Mock IPFS (returns CID-shaped string)** | 5 min | Trap — judges who fetch get 404 | None | Lies. Don't do this. |

## Recommended path

**Ship Pinata in v1**, write the persistence as a `TracePinner` Protocol with a
`Kubo` (local-IPFS) alternative behind it. The architectural seam is:

```python
class TracePinner(Protocol):
    """Pin canonical-JSON bytes to content-addressed storage, return CID."""
    def pin(self, canonical_bytes: bytes) -> str: ...  # returns "bafy..."
    def gateway_url(self, cid: str) -> str: ...        # for UI display
```

Concrete impls:
- `PinataPinner` — POST `/pinning/pinJSONToIPFS` with API key from env.
- `KuboPinner` — POST `/api/v0/add` to local IPFS daemon (for self-hosted).
- `NoopPinner` — returns `""`; current behavior; for local dev without
  IPFS credentials (degrades to today's hash-only flow).

Backend wires the pinner in `publish_trace`, `construct_strategy`, and the
fusion job; failure to pin is non-fatal (log a warning, store with empty CID)
so an IPFS outage doesn't break trace publishing.

## Integration points

1. **`models/trace.py` — add `ipfs_cid: str | None` to `ReasoningTrace`.**
2. **`services/trace_pinner.py` — new module with the Protocol + impls.**
3. **`api/routes.py` `publish_trace` — pin canonical_bytes, set `arc_tx_hash`
   and `ipfs_cid` on the off-chain record, return both to the client.**
4. **`api/routes.py` `construct_strategy` + `_run_fusion_job` — same pin step
   after building the trace.**
5. **`api/schemas.py` `TraceResponse` — add `ipfs_cid: str | None` + a
   `ipfs_gateway_url` derived field for UI convenience.**
6. **`ReasoningTraceRegistry.sol` — add `bytes ipfsCid` parameter to
   `publishTrace` and store it alongside the hash. Backward-compat constructor
   keeps the old (no-CID) function as a deprecated overload.**
7. **`ui/components/Reasoning.jsx` + `Portfolio.jsx` agent feed — show
   "view on IPFS" link next to the hash; clicking opens
   `https://ipfs.io/ipfs/<cid>` in a new tab.**
8. **`ui/components/Strategies.jsx` `FeaturedCard` — strategy passport gains
   a "Source bytes" link that fetches the CID and shows a tiny verification
   block (recomputed hash matches the on-chain anchor).**

## Risks and ways to fail

- **Pinata account is a single point of failure.** If the API key leaks or
  the account is suspended, no new pins succeed. Mitigation: also run a
  local Kubo node in production as a redundant pinner; the same canonical
  bytes pin to the same CID on both, so loss of one provider doesn't
  invalidate the CID.
- **IPFS CIDs are not URLs — they don't HTTP-redirect.** UI must hardcode a
  trusted gateway (`ipfs.io`, `cloudflare-ipfs.com`, `dweb.link`) or run
  one. Judges following a link from our UI deserve a fast gateway — pick
  Cloudflare and document the dependency.
- **Trace size growth.** Today's 4 KB/trace estimate assumes short
  reasoning text. If the LLM gets verbose or we add long market context
  snapshots, size goes up linearly with bandwidth cost. Cap reasoning at
  some sensible bound (e.g., 8 KB total canonical) in the canonicalizer
  with a "truncated for size" flag if exceeded.
- **CIDs are forever.** Once pinned, the content is theoretically
  permanent — if a trace contains user PII or a wrong opinion, you cannot
  redact it. The canonicalizer must filter user-identifying fields *before*
  pinning. (Today the canonical JSON doesn't include user wallet addresses
  in reasoning, which is good, but this needs an explicit allowlist.)
- **The pin step adds ~500ms-2s of latency** to every trace-emitting
  endpoint. Either accept the latency, or fire-and-forget the pin in a
  background task (sacrifices the on-chain CID claim — the trace hash
  anchor goes to chain without the CID being final yet). Background path
  needs a "pending CID" state and a callback updater. Probably acceptable
  for v1 to do it inline.

## What this does NOT do

- It does not change the rigor-gate. Strategy backtests + DSR/PBO are
  separate; this is purely about provenance of agent *reasoning*.
- It does not anchor strategy passports themselves. The passport already
  has a `methodology_hash` field anchored separately; that flow is
  unchanged.
- It does not solve the "agent decisions need to be verifiably *temporally*
  ordered" problem. The commit-reveal scheme in
  [`docs/specs/commit-reveal-trace-spec.md`](commit-reveal-trace-spec.md)
  is orthogonal and complementary.

## Recommended next step

Spec the `TracePinner` Protocol + Pinata impl as a one-PR ticket sized for
the t2o2 agent (issue body with judge-grade acceptance criteria — see the
CLAUDE.md agentic-issue pipeline section for the format). Ship behind a
feature flag (`ARCHIMEDES_TRACE_PIN_ENABLED`); default off until validated;
flip on for the demo and the deck talks about it.

Owner suggestion: this overlaps Chuan's chain layer + Daniel R.'s backend
layer. The TracePinner abstraction lives in `services/` (Daniel R.'s lane)
but the contract change to take `ipfsCid` is Chuan's. Pair up; one PR
each side, integrate via a feature-flagged switch.
