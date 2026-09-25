# Commit-Reveal Reasoning Trace — v1.5 Spec

> **Date:** 2026-05-13 (Day 3)
> **Owner:** Chuan (smart contracts) + Dan (passport / pitch framing)
> **Status:** Proposal — v1.5, post-hackathon MVP unless trivially droppable into the
> existing `ReasoningTraceRegistry` work
> **Prerequisite reading:**
> ``../agora_project_analysis.md`` (routed to the private docs repo, 2026-08-19) § 5.2,
> [`./strategy-passport-spec.md`](./strategy-passport-spec.md),
> [`./ecosystem-design-spec.md`](./ecosystem-design-spec.md) § 3.4

## The problem this solves

A reasoning-trace hash anchored on-chain at time T proves the trace **existed** at time
T. It does NOT prove the agent **used** that trace to decide the trade. An adversarial
actor (or a buggy agent) can:

1. Make a trade for any reason.
2. Generate 100 plausible reasoning traces post-hoc.
3. Pick the one that retroactively rationalizes the trade.
4. Publish its hash to the registry.

The user sees a hash on-chain and a matching trace off-chain and concludes "the agent
reasoned this way, then traded." But the audit trail does not actually prove that
ordering or causation.

The current v1 design from
[`ecosystem-design-spec.md`](./ecosystem-design-spec.md) § 3.4 is vulnerable to this
critique. We disclose the limit honestly per the Day-3 addition to `anti-features.md`
("NOT claiming: that an on-chain trace hash proves the agent used the trace") — but
disclosure is weaker than mitigation. Commit-reveal is the mitigation.

## The proposal

Anchor the reasoning trace's hash **before** the trade executes; reveal the trace content
**after** the trade settles. The agent then publicly cannot tailor reasoning to outcomes
without breaking the hash.

### Sequence

```
T-0:  Agent decides what trade to make + writes reasoning trace.
T-1:  Compute trace_hash = keccak256(canonical_trace_json).
T-2:  ReasoningTraceRegistry.commit(trace_hash, trade_intent_summary)
        - Emits TraceCommitted(traceId, hash, msg.sender, block.number)
        - Stores commitment in a pending-traces table
T-3:  Vault.rebalance(trades)  [trade executes on Arc]
T-4:  Upload canonical_trace_json to off-chain storage; obtain storagePointer.
T-5:  ReasoningTraceRegistry.reveal(traceId, storagePointer, fullTraceContent)
        - On-chain: verifies keccak256(fullTraceContent) == committed contentHash
        - Records storagePointer alongside the commitment
        - Emits TraceRevealed(traceId, storagePointer, block.number)
        - Promotes commitment from pending to revealed
```

Note that the reveal call carries both the off-chain `storagePointer` (an optional
locator — URL / CID / empty) and the `fullTraceContent` bytes (so the
contract itself can recompute the hash and verify the binding without trusting any
off-chain fetch). The storage pointer is recorded for convenience; the hash verification
is what enforces the commit.

**Live `storagePointer` is empty.** We do not pin traces to IPFS. The integrity
anchor is the on-chain keccak256 of the canonical bytes; the full JSON lives in
the off-chain store. Re-enabling a pin is an owner action (SSM JWT + `ecs.tf`
`secrets{}` + a rebuilt pin client) — see
[`../adr/ipfs-pinning-not-live.md`](../adr/ipfs-pinning-not-live.md). Do not read
an empty pointer as "a CID that failed to resolve."

Between T-2 and T-3, the trace hash is on-chain and **immutable**. The agent cannot
alter the content without breaking the verification at T-5. The reveal at T-5 publishes
the content. Anyone can verify post-hoc that the hash committed before the trade matches
the content revealed after.

### What this prevents

| Attack | Without commit-reveal | With commit-reveal |
|---|---|---|
| Generate 100 traces, pick best one to publish | Possible | The committed hash binds the agent to a single trace before the trade lands |
| Edit trace content after seeing trade outcome | Possible (the hash is computed on the edited content) | Hash verification at reveal fails |
| Backdate a trace to look like it preceded the trade | Possible — the off-chain timestamp is forgeable | Commit transaction's block number is the timestamp; reveal must follow |
| Claim "the agent reasoned, then traded" causally | Disclaimed in anti-features.md as unverifiable | Provable: commit block < trade block < reveal block |

### What it does not prevent

- The agent **could** still commit a trace it never internally used. But it has to commit
  to *one* trace per decision, before the outcome is knowable, which is a much stronger
  bar than "any trace consistent with the outcome." This is the same property that
  makes commit-reveal valuable in prediction-market resolution and zk-proof submission.
- A coordinated attacker controlling both the agent and the on-chain registry could
  game both. We assume the registry is genuinely public and the agent does not control
  the chain.
- This does not address the "garbage in, garbage out" reasoning quality problem — only
  the integrity-over-time problem.

## Contract changes

The existing `IReasoningTraceRegistry` from
[`../../contracts/src/interfaces/IReasoningTraceRegistry.sol`](../../contracts/src/interfaces/IReasoningTraceRegistry.sol)
needs three new functions plus state. Sketch:

```solidity
interface IReasoningTraceRegistry {
    // ── Existing v1 ─────────────────────────────────────────
    function publishTrace(bytes32 hash, bytes calldata metadata) external;
    function verifyTrace(uint256 id, bytes calldata fullTrace) external view returns (bool);
    function getTraces(address agent, uint256 from, uint256 to) external view returns (bytes32[] memory);

    // ── New for v1.5 ────────────────────────────────────────
    function commit(
        bytes32 contentHash,
        bytes calldata tradeIntentSummary   // ABI-encoded: vaultAddr, decisionType, numTrades, totalNotionalUsdc
    ) external returns (uint256 traceId);

    function reveal(
        uint256 traceId,
        string calldata storagePointer,    // empty on the live path (hash-only); optional locator
        bytes calldata fullTraceContent    // For on-chain verification of the hash
    ) external;

    function getCommitment(uint256 traceId) external view returns (
        bytes32 contentHash,
        address committer,
        uint256 commitBlock,
        bool revealed,
        uint256 revealBlock,
        string memory storagePointer
    );
}
```

### Storage

```solidity
struct Commitment {
    bytes32 contentHash;
    address committer;
    uint64 commitBlock;
    uint64 revealBlock;          // 0 until revealed
    string storagePointer;        // empty until revealed
}

mapping(uint256 => Commitment) public commitments;
uint256 public nextTraceId;
```

### Events

```solidity
event TraceCommitted(uint256 indexed traceId, bytes32 indexed contentHash, address indexed committer, uint256 commitBlock);
event TraceRevealed(uint256 indexed traceId, string storagePointer, uint256 revealBlock);
```

### Gas + cost estimate

Per ecosystem-design-spec § 3.4, traces anchor for ~$0.01 on Arc via Paymaster. The
commit-reveal pattern doubles the on-chain footprint per decision: one commit tx + one
reveal tx. Conservative estimate: **~$0.02 per decision instead of $0.01**. At ~20
agent decisions across 4 Tier-1 vaults during the demo window, that's $1.60 in
testnet gas. Negligible.

## Backend changes

`backend/archimedes/services/trace_publisher.py` (Marten's lane per
[`component-interfaces-spec.md`](./component-interfaces-spec.md)) extends from a single
`publish(trace)` call to a two-step sequence:

```python
class ITracePublisher(Protocol):
    async def commit(self, trace: ReasoningTrace, trade_intent: TradeIntent) -> int:
        """Commit the trace hash on-chain BEFORE the rebalance executes.

        Returns the on-chain traceId. Stores the commitment locally so the
        reveal step can complete asynchronously after the vault settles.
        """
        ...

    async def reveal(self, trace_id: int, trace: ReasoningTrace) -> str:
        """Reveal the full trace content AFTER the rebalance settles.

        Uploads the canonical trace JSON to storage, then calls the registry's
        reveal() with the storage pointer + the full content for hash
        verification. Returns the tx hash.
        """
        ...
```

The agent orchestrator (`IAgentOrchestrator.tick`) sequence becomes:

1. Decide → trace object built (no hash yet)
2. `trace.compute_hash()` → 32-byte hash
3. `trace_publisher.commit(trace, intent)` → on-chain commitment + traceId
4. `chain_executor.execute_trades(vault, trades)` → vault settles
5. `trace_publisher.reveal(traceId, trace)` → full trace published
6. Both `commit_tx_hash` and `reveal_tx_hash` recorded on the `reasoning_traces` row

The `ReasoningTrace` dataclass needs two additions:

```python
commit_tx_hash: str | None = None       # Tx that committed the hash pre-trade
reveal_tx_hash: str | None = None       # Tx that revealed the content post-trade
```

`is_anchored` becomes `is_commit_anchored` + `is_reveal_anchored`.

## UI implications

The passport's "verify trace" element grows by one button:

- **Verify content hash** — recompute keccak256 on the displayed trace, compare to the
  on-chain commitment. (Already in spec.)
- **Verify temporal binding** — show the user that `commitBlock < tradeBlock <
  revealBlock`. A green checkmark with a tooltip explaining what this proves vs. what
  it does not prove.

The tooltip text matters. Suggested copy:

> "The reasoning trace was hashed and recorded on Arc before this trade executed. The
> hash committed the agent to a single trace; the reveal published the content
> afterward. This proves the trace existed at the time of the trade — it does not
> prove the agent's reasoning was correct or that the trade was profitable."

That's honesty about what the cryptography buys you.

## Acceptance criteria for v1.5

- [ ] `ReasoningTraceRegistry.sol` has working `commit()` and `reveal()` with
      hash-verification on reveal.
- [ ] `trace_publisher.commit()` and `reveal()` round-trip cleanly against an Arc
      testnet deployment.
- [ ] At least one Tier-1 vault demo decision shows: commit tx → rebalance tx → reveal
      tx in that block order.
- [ ] The "Verify temporal binding" UI element renders green for that decision.
- [ ] The `anti-features.md` "NOT claiming: that an on-chain trace hash proves the agent
      used the trace" entry is updated to reflect that v1.5 mitigates this — though does
      not eliminate it.

## Why this is v1.5 not v1

- Doubles the orchestrator's on-chain call sequence — adds latency between decision and
  trade settlement (commit must confirm before the rebalance can fire).
- Doubles the contract surface — `ReasoningTraceRegistry` is currently interface-only;
  adding commit-reveal before there's even a v1 implementation risks scope-creeping
  Chuan's contract lane in the final hackathon week.
- The disclosure-not-mitigation stance in `anti-features.md` § "pitch-rigor anti-claims"
  is a coherent v1 posture. v1.5 turns it into a real mitigation; v2 could go further
  (zk-proof of LLM execution, threshold-encrypted commit windows).

## Out-of-scope

- ZK-proof of the LLM call itself. The commit-reveal binds the trace content; it does
  not prove the LLM actually generated that content vs. a human typing it. v2 territory.
- Time-locked reveal windows that force a minimum delay between commit and reveal.
  v2 — only useful in adversarial settings we don't expose in v1.5.
- Compressed batched commitments (Merkle-root over many traces). Useful at scale, not
  at the ~20-decisions-per-demo scale of the hackathon.

## References

- The commit-reveal pattern in cryptography: Naor (1989), "Bit commitment using
  pseudorandom generators."
- The same pattern in prediction-market resolution: Augur, Realitio, and most
  oracle-based markets use commit-reveal for honest reporting.
- The "trace existed at T, doesn't prove causation" critique:
  ``../agora_project_analysis.md`` (routed to the private docs repo, 2026-08-19) § 5.2.
