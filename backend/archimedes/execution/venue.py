"""The executor boundary: what a venue owes the decision core, and nothing more.

A "venue" is the pair of ends the per-tick decision cannot supply for itself:

  1. ``token_address`` — how this venue names an instrument. On chain that is a
     checksummed ERC-20 address; on paper it is a symbolic identifier. This is
     the ONLY venue input to target construction, and it is deliberately not an
     input to the *weights*: two venues resolving different addresses for the
     same symbol must still produce the same target weight, which is the
     property ``test_execution_venue_parity`` pins.
  2. ``read_portfolio`` / ``execute_trades`` — where current positions come from
     and where trades go.

Everything between those two ends — aggregate, scale, diff, size — lives in
``archimedes.execution.core`` and is shared verbatim.

WHY ``ChainVenue`` IS A THIN ADAPTER AND NOT A REWRITE. #1410's first anti-goal
is "do NOT touch the chain executor's live behavior". So this class forwards to
the ``chain_executor`` singleton without reinterpreting anything: same portfolio
read (including the #1080 unpriced-holding semantics), same ``execute_trades``,
same errors. The live ``StrategyRunner`` still calls ``chain_executor`` directly
and is not rerouted through here by this change — the vault path's migration
onto this protocol is a follow-up whose risk is about real money, and bundling
it with the paper work would have made the paper work unshippable without it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from archimedes.execution.core import TickDecision
    from archimedes.models.portfolio import Portfolio


@runtime_checkable
class ExecutionVenue(Protocol):
    """Where a strategy's decision gets read from and written to.

    Implementations must be side-effect-free on construction: a venue is built
    per tick (and, in tests, per fixture) and constructing one must never open a
    connection, sign anything, or touch the network.
    """

    #: Short, stable label for logs and diagnostics. Deliberately NOT a stored
    #: provenance column: each venue writes to its own store (the chain to the
    #: vault, paper to ``paper_agent_trades``), so which venue produced a row is
    #: already answered by which table it is in, and a second answer could
    #: disagree with the first.
    name: str

    def token_address(self, symbol: str) -> str:
        """Resolve ``symbol`` to this venue's instrument identifier.

        Returns ``""`` for a symbol this venue cannot trade — on chain, a synth
        with no deployed address. The core does NOT filter on it: it carries the
        empty address onto the ``TargetAllocation`` so the decision stays
        inspectable and the weight still counts against the book. What happens
        downstream is the venue's business, and the two differ — the vault path
        cannot build a swap leg without an address, while the paper venue never
        produces one (see ``PaperVenue.token_address``).
        """
        ...

    async def read_portfolio(self, account_id: str) -> Portfolio:
        """Current positions for ``account_id`` (a vault address, or a paper
        deployment id — the venue decides what an account is)."""
        ...

    async def execute_trades(self, account_id: str, decision: TickDecision) -> list[str]:
        """Execute ``decision.trades`` and return venue-specific references.

        Takes the whole :class:`~archimedes.execution.core.TickDecision`, not a
        bare trade list, so a venue that persists what it executes cannot be
        handed trades without the tick, signals and prior portfolio that
        produced them. A venue with nothing to persist reads ``.trades`` and
        ignores the rest; it does not get to be *asked* without the provenance.
        """
        ...


class ChainVenue:
    """The vault side of the boundary — a forwarding adapter, by design.

    Holds no state and reimplements nothing. Every method delegates to the
    ``chain_executor`` singleton, so behavior on the vault path is whatever
    ``ChainExecutor`` does today, including its error types.
    """

    name = "chain"

    def __init__(
        self,
        executor=None,
        *,
        usdc_address: str | None = None,
        synth_addresses: dict[str, str] | None = None,
    ) -> None:
        # Lazy imports: `archimedes.execution` is imported by the paper
        # scheduler pass, which has no business pulling in web3, the contract
        # loader and the signer stack just to price a weight diff.
        if executor is None:
            from archimedes.chain.executor import chain_executor

            executor = chain_executor
        if usdc_address is None or synth_addresses is None:
            from archimedes.chain.client import chain_client

            settings = chain_client.settings
            # RESOLVED ONCE, at construction. `settings.synth_addresses` is a
            # property that re-reads env on every access; StrategyRunner has
            # always snapshotted it in __init__, and a venue that re-resolved
            # per call would make the addresses a tick could see depend on when
            # in the tick they were asked for.
            if usdc_address is None:
                usdc_address = settings.usdc_address
            if synth_addresses is None:
                synth_addresses = settings.synth_addresses
        self._executor = executor
        self._usdc_address = usdc_address or ""
        self._synth_addresses = synth_addresses or {}

    def token_address(self, symbol: str) -> str:
        if symbol == "USDC":
            return self._usdc_address
        return self._synth_addresses.get(symbol, "")

    async def read_portfolio(self, account_id: str) -> Portfolio:
        return await self._executor.read_portfolio(account_id)

    async def execute_trades(self, account_id: str, decision: TickDecision) -> list[str]:
        # Only `.trades` is read. The vault path anchors its tick provenance in
        # the on-chain commit-reveal trace (agent_runner._commit_trace);
        # re-recording it here would create a second, divergent answer to "which
        # tick produced this trade".
        return await self._executor.execute_trades(account_id, decision.trades)
