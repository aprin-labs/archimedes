# On-Chain Integration Layer

> **What this layer does:** Executes portfolio operations on Arc, publishes verifiable reasoning traces, and maintains real-time price feeds via Circle-managed wallets.

> **Canonical specs:** [`docs/specs/ecosystem-design-spec.md` § 3.2](../../docs/specs/ecosystem-design-spec.md) (vault contracts), [`docs/specs/strategy-passport-spec.md`](../../docs/specs/strategy-passport-spec.md) (verifiable strategy metadata), [`docs/architectural-principles.md`](../../docs/architectural-principles.md) (on-chain provenance).

## Module Guide

### Core Execution Modules

**`executor.py`** — On-chain portfolio state read/write.  
Implements `IChainExecutor` from `archimedes/interfaces/chain.py`. Reads vault holdings and target allocations via contract calls; executes rebalance trades through the AMM; handles NAV computation and pool health checks. Raises `InsufficientLiquidityError` and `TradeRevertedError` to prevent recording failed trades as successes (audit fix: issue #408).

**`client.py`** — AsyncWeb3 singleton and connection settings.  
All contract calls route through this shared `ChainClient`. Loads chain settings from environment (`ARC_*` prefix): RPC URL, agent/owner private keys, contract addresses, and ABI directory. Configures the w3 instance with Arc's PoA middleware. Exposes `ChainSettings` dataclass for contract address lookups.

**`contracts.py`** — Contract ABI loader and typed wrapper factory.  
Reads cached ABIs from `contracts/abis/*.json`. Creates `AsyncContract` instances for Vault, VaultFactory, AMMRouter, ReasoningTraceRegistry, StrategyRegistry, SyntheticFactory, PriceOracle, and per-asset oracle contracts. The `@property` methods (`vault_factory`, `amm_router`, etc.) are the main API for the rest of the chain layer.

### Oracle and Price Feed

**`oracle_updater.py`** — Periodic price fetch and on-chain push.  
Fetches latest market prices from yfinance (equities/ETF/commodities) and CoinGecko (crypto). Validates upstream freshness (configurable staleness bound, default 15 min) and deviation vs. last known good price (default 20% / 2000 bps, matches `PriceOracle.sol` threshold). Pushes prices to individual PriceOracle contracts via Circle Developer-Controlled Wallet API (primary) or raw private key (fallback). Runs as a standalone async process; see `oracle_runner.py` for the lifecycle.

**`oracle_runner.py`** — Oracle updater orchestration loop.  
Minimal harness: creates an `OracleUpdater`, calls `fetch_prices() → push_prices_on_chain()` on interval (default 60 seconds), logs cycle outcomes. Intended to run as a separate container process in the Docker compose stack.

### Strategy and Trace Publication

**`strategy_publisher.py`** — Anchors Tier-1 strategies on-chain.  
Implements `IStrategyPublisher`. Registers strategy keccak256 hashes (methodology + consulted-papers hash) to `StrategyRegistry.sol` once a strategy passes the rigor gate (DSR + PBO + walk-forward OOS + look-ahead audit). Only Tier-1 strategies are promoted to VALIDATED and anchored; candidate/rejected strategies are never registered. Uses the dual-path signing pattern: Circle Developer-Controlled Wallet (primary) or raw agent private key (fallback).

**`trace_publisher.py`** — Anchors reasoning traces on-chain.  
Implements `ITracePublisher`. Publishes `ReasoningTrace` objects as keccak256 hashes to `ReasoningTraceRegistry.sol`. The hash is the proof of provenance; the full trace JSON is stored off-chain (Postgres). Anyone can recompute and verify against the on-chain anchor. Encodes metadata (timestamp, decision type, agent version) alongside the hash. Uses the same dual-path signing as `strategy_publisher.py`. Live reveals pass an empty `storagePointer` — we do not pin traces ([ADR](../../docs/adr/ipfs-pinning-not-live.md)).

### Agent Coordination

**`agent_runner.py`** — Autonomous portfolio rebalancing loop.  
The intelligence layer. Evaluates paper-grounded strategies (SMA200 cross, vol-targeting, 12-month momentum, etc.) against live market data to produce allocation signals. Aggregates signals into target weights. Runs `VCheck` validity checks (weights sum to 10000 bps, concentration limits, cost-benefit thresholds). Executes rebalances via the executor. Publishes reasoning traces via `trace_publisher.py`. Runs as a standalone async process; environment variables control tick interval (default 300s / 5 min), dry-run mode, explicit vault addresses, and USDC floor. The `_compute_confidence()` function combines vote ratio (signal consensus) and signal strength (magnitude) to produce dynamic confidence 0.0–1.0.

### Validation and Signing

**`v_check.py`** — Deterministic pre-trade validity gate (Xia et al. 2026 § 5).  
The LLM cannot override these checks; they are pure Python mechanical rules. Checks three invariants:
- `weights_sum_bps`: target weights must sum to exactly 10000 basis points.
- `max_concentration`: no single position exceeds a threshold (default 60%).
- `min_cost_benefit_bps`: expected improvement must exceed minimum (default 5 bps = 0.05%).

Returns a `VCheckResult` with pass/fail status and per-check details. Every rebalance action must pass `VCheck.run()` before transaction submission.

**`circle_signer.py`** — Circle Developer-Controlled Wallet integration.  
Executes on-chain contract calls via Circle's REST API rather than raw private keys. Encrypts the entity secret with Circle's RSA public key (OAEP/SHA-256); submits the contract call to the Circle SDK; polls for terminal state (COMPLETE/FAILED/DENIED/CANCELLED) with configurable poll interval (default 2s) and max polls (default 60 = 2 min). Replaces the oracle signer pattern: no private key in environment, reduced key management risk. Environment variables: `CIRCLE_API_KEY`, `CIRCLE_ENTITY_SECRET`, `WALLET_ID`.

## Integration Flow: Oracle → Executor → Publishers

```
┌──────────────┐
│ oracle_updater.fetch_prices()  (yfinance + CoinGecko)
└──────────────┘
       │
       ▼
┌─────────────────────────────────────────────────┐
│ PriceOracle.sol (on-chain)                      │
│ — latest prices for sTSLA, sSPY, sGLD, sBTC    │
└─────────────────────────────────────────────────┘
       │
       │ (read by: executor, vault.totalAssets())
       │
       ▼
┌──────────────────────────────────────┐
│ agent_runner.strategy_evaluator()    │
│ — evaluate SMA200, vol-target, TSMOM │
└──────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────┐
│ VCheck.run() — validate weights      │
│ (sum, concentration, cost-benefit)   │
└──────────────────────────────────────┘
       │
       ├─ PASS ──────────────────────────┐
       │                                  │
       ▼                                  ▼
  executor.execute_rebalance()    trace_publisher.publish()
  (AMMRouter.swap())              (ReasoningTraceRegistry hash)
       │                                  │
       │                                  ▼
       │                         On-chain proof of reasoning
       │
       ▼
  strategy_publisher.anchor()
  (StrategyRegistry hash, for Tier-1 only)
```

## Dependencies

### Circle SDK Integration

The chain layer depends on Circle's Developer-Controlled Wallets and Paymaster for transaction execution:

- **`circle_signer.py`** uses Circle REST API (`https://api.circle.com/v1/w3s`) to execute contract calls. Requires `CIRCLE_API_KEY`, `CIRCLE_ENTITY_SECRET`, `WALLET_ID` from Circle Dashboard.
- **`oracle_updater.py`** uses the same Circle API to push prices on-chain (oracle owner is a Circle-managed wallet).
- **Fallback:** Both modules support raw private key signing (from `ARC_AGENT_PRIVATE_KEY` / `ARC_OWNER_PRIVATE_KEY`) if Circle credentials are missing.

Reference: [`submodules/context-arc/circlefin-skills/use-arc.md`](../../submodules/context-arc/circlefin-skills/use-arc.md) (in-tree copy of Circle's official Arc integration guide).

### Contract ABIs

All ABIs are cached at `contracts/abis/*.json` (loaded via `ContractLoader`). The contracts are deployed to Arc testnet; addresses are configured via environment variables and stored in `ChainSettings`:

| Contract | Env Var | Role |
|---|---|---|
| `VaultFactory` | `ARC_VAULT_FACTORY_ADDRESS` | Creates ERC-4626 vaults; holds target allocations |
| `Vault` | (per-vault address) | ERC-4626 compliant; rebalance target, NAV |
| `AMMRouter` | `ARC_AMM_ROUTER_ADDRESS` | Swap synthetic assets + vault tokens against USDC |
| `SyntheticFactory` | `ARC_SYNTHETIC_FACTORY_ADDRESS` | Mints/redeems synth tokens (sTSLA, sSPY, etc.) |
| `PriceOracle` | `ARC_*_ORACLE_ADDRESS` | Per-asset price feed (TSLA, SPY, GLD, BTC, etc.) |
| `ReasoningTraceRegistry` | `ARC_REASONING_TRACE_REGISTRY_ADDRESS` | Anchors reasoning-trace hashes |
| `StrategyRegistry` | `ARC_STRATEGY_REGISTRY_ADDRESS` | Registers Tier-1 strategy hashes |

See `client.py` `ChainSettings` for the complete address list and defaults.

### On-Chain State Management

The executor reads live state from the Vault contract (holdings, target allocations, total assets) on every rebalance. State consistency relies on:

- **Vault contract:** `getHoldings() → (tokens[], amounts[])` and `getTargetAllocations() → (tokens[], weights[])`.
- **NAV computation:** `totalAssets()` or fallback to oracle-priced sum of holdings if the contract call reverts (stale price on testnet).
- **AMM health:** executor checks `MIN_HEALTHY_LIQUIDITY_USDC` (default $5 for testnet) before submitting swaps.
- **Nonce management:** raw-key signing path increments nonce per pending transaction; Circle API handles nonce internally.

## Getting Started

### 1. Set Up Circle Wallet Credentials

If using Circle Developer-Controlled Wallets (recommended for production):

```bash
# Get these from Circle Dashboard
export CIRCLE_API_KEY="TEST_API_KEY:<uuid>:<secret>"
export CIRCLE_ENTITY_SECRET="<32-byte hex entity secret>"
export WALLET_ID="<circle wallet uuid>"

# Test the signer
python -c "from archimedes.chain.circle_signer import circle_signer; print('Configured:', circle_signer.is_configured)"
```

For fallback raw private key signing:

```bash
export ARC_AGENT_PRIVATE_KEY="<0x-prefixed 32-byte hex>"  # or leave empty to use Circle
export ARC_OWNER_PRIVATE_KEY="<0x-prefixed 32-byte hex>"  # for oracle updates
```

#### Rotating the Entity Secret

`CIRCLE_ENTITY_SECRET` is a long-lived credential — rotate it periodically and
immediately if it may have leaked (committed to a branch, pasted in a shared
channel, etc.). [`wallet-setup/rotate-secret.mjs`](../../../wallet-setup/rotate-secret.mjs)
automates the full cycle:

```bash
make rotate-secret
# equivalent: cd wallet-setup && node --env-file=../.env rotate-secret.mjs
```

The script (1) generates a fresh 32-byte secret, (2) registers it with Circle
via `registerEntitySecretCiphertext`, saving the one-time-downloadable recovery
file to `wallet-setup/recovery_file.dat` (store this securely — Circle will not
re-issue it), (3) test-signs a real contract call to confirm the new secret is
live before touching anything else, and only then (4) overwrites
`CIRCLE_ENTITY_SECRET` in the root `.env`. If step 2 returns `409 Conflict`,
Circle already has an entity secret registered — reset it first from
[console.circle.com](https://console.circle.com) → Programmable Wallets →
Configuration → "Reset Entity Secret", then re-run.

This is distinct from `register-entity-secret.mjs`'s initial-setup path (`make
register`), which only runs when `CIRCLE_ENTITY_SECRET` is unset — use
`rotate-secret.mjs` when a secret already exists and needs replacing without a
gap in Circle-signed on-chain writes (oracle price pushes, agent rebalances).

**Warning:** step 3's test-sign is a real state-changing `setPrice(uint256)`
transaction, not a dry run, and its target contract address is hardcoded in
`rotate-secret.mjs` from a pre-T3.2 deploy generation — it does not appear
anywhere else in the repo. Before running this during an incident, verify the
hardcoded address still resolves to a contract you intend to write to; on a
stale address the call either 400s (halting rotation) or succeeds and
overwrites a live oracle's price.

### 2. Configure Arc RPC and Contract Addresses

```bash
# Arc testnet (default)
export ARC_RPC_URL="https://rpc.testnet.arc.network"
export ARC_CHAIN_ID="5042002"

# Deploy contracts and get addresses
cd contracts && forge script Deploy.s.sol --rpc-url "$ARC_RPC_URL" --broadcast
# Then set in .env:
export ARC_VAULT_FACTORY_ADDRESS="0x..."
export ARC_AMM_ROUTER_ADDRESS="0x..."
# ... etc for all contracts
```

### 3. Understand the Executor Lifecycle

The executor is stateless — it reads current state and executes transactions. A full rebalance cycle looks like:

```python
from archimedes.chain.executor import chain_executor
from archimedes.chain.v_check import VCheck

# Read current portfolio
portfolio = await chain_executor.read_portfolio(vault_address="0x...")

# Compute target weights (e.g., from strategy signals)
target_weights = {"sSPY": 0.60, "sBTC": 0.20, "USDC": 0.20}

# Validate
target_weights_bps = {k: int(v * 10000) for k, v in target_weights.items()}
v_check = VCheck(weights_bps=target_weights_bps)
if not v_check.run():
    logger.error(f"Validation failed: {v_check.failures}")
    return None

# Execute trades
trades = await chain_executor.execute_rebalance(vault_address, target_weights)
for trade in trades:
    logger.info(f"Trade {trade.direction}: {trade.symbol} {trade.amount}")

# Publish reasoning trace (example: store reason in ReasoningTrace)
from archimedes.models.trace import ReasoningTrace, DecisionType
trace = ReasoningTrace(
    vault_address=vault_address,
    decision_type=DecisionType.REBALANCE,
    reasoning="SMA200 cross signal: bullish entry",
    # ... additional fields
)
tx_hash = await trace_publisher.publish(trace)
logger.info(f"Trace anchored: {tx_hash}")
```

### 4. Test with Forge (Local)

To test contract interactions locally without Arc testnet:

```bash
# Start a local Anvil instance
anvil --fork-url "$ARC_RPC_URL"

# In another terminal, deploy to localhost
cd contracts
forge script Deploy.s.sol --rpc-url "http://localhost:8545" --broadcast

# Point the executor at localhost
export ARC_RPC_URL="http://localhost:8545"
# Update contract addresses from deploy output

# Run executor tests
cd ../backend && pytest tests/test_chain_executor.py -v
```

### 5. Run the Agent and Oracle Loops Locally

```bash
# Terminal 1: Oracle runner (updates prices every 60s)
ORACLE_INTERVAL_SECONDS=60 python -m archimedes.chain.oracle_runner

# Terminal 2: Agent runner (rebalances every 5 min)
AGENT_INTERVAL_SECONDS=300 python -m archimedes.chain.agent_runner

# Terminal 3: API server (for UI + manual calls)
python -m archimedes.main
```

Monitor logs; the oracle should post prices, and the agent should evaluate strategies and propose rebalances.

### 6. Docker Compose (Full Stack)

The production setup runs oracle + agent as separate services:

```yaml
# docker-compose.yml excerpt
services:
  oracle:
    image: archimedes:latest
    command: python -m archimedes.chain.oracle_runner
    environment:
      - ARC_RPC_URL=https://rpc.testnet.arc.network
      - CIRCLE_API_KEY=${CIRCLE_API_KEY}
      - CIRCLE_ENTITY_SECRET=${CIRCLE_ENTITY_SECRET}
      - WALLET_ID=${WALLET_ID}
    depends_on:
      - postgres
      - redis

  agent:
    image: archimedes:latest
    command: python -m archimedes.chain.agent_runner
    environment:
      - ARC_RPC_URL=https://rpc.testnet.arc.network
      - ARC_AGENT_PRIVATE_KEY=${ARC_AGENT_PRIVATE_KEY}
      - AGENT_VAULT_ADDRESSES=0x...
    depends_on:
      - postgres
      - redis
```

Run with `docker compose up -d --build`.

## Testing

### Unit Tests

Located in `backend/tests/test_chain_*.py`. Fixtures mock the Chain client, Circle signer, and contract ABIs.

```bash
# Test executor (mocked chain)
pytest backend/tests/test_chain_executor.py -v

# Test V_check
pytest backend/tests/test_v_check.py -v

# Test trace/strategy publishing
pytest backend/tests/test_chain_publisher.py -v
```

### Integration Tests

Require a running Arc testnet (or Anvil fork). Marked with `@pytest.mark.integration`:

```bash
# Run only integration tests (requires Arc RPC)
pytest backend/tests/ -m integration -v

# Run all except integration
pytest -m "not integration" -v
```

### Smoke Test (Manual)

Before deploying to live EC2:

```bash
# 1. Deploy contracts to Arc testnet
cd contracts && forge script Deploy.s.sol --rpc-url https://rpc.testnet.arc.network --broadcast

# 2. Update .env with contract addresses
# 3. Run oracle once
python -m archimedes.chain.oracle_updater

# 4. Create a test vault
python -c "from archimedes.chain.executor import chain_executor; await chain_executor.read_portfolio('0x...')"

# 5. Check on-chain state
cast call 0x... "getHoldings()" --rpc-url https://rpc.testnet.arc.network
```

## Architecture Notes

### Why Two Signing Paths (Circle + Raw Key)?

1. **Circle (primary):** Managed wallet; no private key in environment; works for oracle and agent.
2. **Raw key (fallback):** Simpler for testing and dev; required where Circle account doesn't exist.

Both paths sign the same transaction shape, so either can be swapped for the other. The agent and oracle first try Circle; if unconfigured, they fall back to raw private key.

### Why VCheck Is Separate from the Executor?

`VCheck` is deterministic and has no I/O; it runs in-process. The executor is stateful and touches on-chain state. Separating them ensures:

- **Testability:** VCheck can be unit-tested without mocking anything.
- **Auditability:** A failed VCheck is definitively rejected before any transaction is sent.
- **Pluggability:** Different validity rules can be swapped without changing the executor.

### Why Reasoning Traces Are Hashed, Not Stored On-Chain?

On-chain storage is expensive and immutable. The hash is the proof of existence and integrity. The full trace JSON is stored off-chain (Postgres). Anyone can recompute the hash and verify against the on-chain anchor. This is the "commit-reveal" pattern: fast on-chain (one hash), full details off-chain (full JSON), verifiable both ways. We do not pin traces to a public gateway ([ADR](../../docs/adr/ipfs-pinning-not-live.md)).

## Known Limitations and TODOs

- **Nonce management:** Raw-key signing increments nonce manually; concurrent transactions can collide. Circle API handles nonce internally but polling adds latency (~2–4s per tx).
- **No liquidation engine:** Vault collateral is assumed 100% sufficient. Real production needs a liquidation bot if collateral drops below a threshold.
- **No CLOB:** The AMM is a simple Uniswap V2 (x·y=k); post-hackathon upgrade is a CLOB for tighter spreads on large orders.
- **Limited asset set:** Currently 5 synthetics (sTSLA, sSPY, sGLD, sBTC, USYC). Extending requires deploying new oracle + synthetic contracts + AMM pools.

## Useful References

- **Arc RPC:** https://rpc.testnet.arc.network (or configure via `ARC_RPC_URL`)
- **Circle SDK:** [`submodules/context-arc/circlefin-skills/use-arc.md`](../../submodules/context-arc/circlefin-skills/use-arc.md)
- **Smart contract specs:** [`docs/specs/ecosystem-design-spec.md`](../../docs/specs/ecosystem-design-spec.md) § 3
- **Strategy passport (verifiable metadata):** [`docs/specs/strategy-passport-spec.md`](../../docs/specs/strategy-passport-spec.md)
- **Selection-bias correction (Tier-1 gate):** [`docs/specs/selection-bias-corrections-spec.md`](../../docs/specs/selection-bias-corrections-spec.md)
- **Xia et al. 2026 (agentic protocols):** arxiv 2605.19337, § 5 (V_check and Outcome Embargo)
- **Contract ABIs:** `contracts/abis/*.json`
- **Deployed addresses (Arc testnet):** environment variables in `.env`
