"""One registry for every live LLM prompt.

Six call sites in this tree send a prompt to a provider, and until #1800 each
kept its own private string constant::

    generation_pipeline._validate_brief        brief screening
    strategy_fusion.StrategyFusion.propose     the fusion proposer (also the debate proposer)
    strategy_fusion._repair_spec               the one bounded spec-repair retry
    debate_engine._debate_round                the bull/bear turns (4 per generation)
    arxiv_pipeline.synthesize_passport         paper -> passport extraction
    portfolio_agent.PortfolioAgent.propose_portfolio   single-turn portfolio construction

Nothing could answer "what exactly do we send the model, and did it change?"
without reading five files. This module is the answer: every template that
becomes provider bytes lives here once, carries a stable ``id`` and a
``version``, declares its placeholders, and names its call sites. The call
sites import from here; they no longer own prompt text.

**Templating is ``string.Template`` (``$name`` / ``${name}``), never
``str.format``.** Most of these prompts print a JSON schema, so ``{`` and ``}``
are common literal characters; ``debate_engine`` had to double every one of
them to survive ``.format``. ``$`` appears in none of the prompts, so
``Template`` needs no escaping at all and the stored text is exactly the text a
reader sees in the generated inventory.

Downstream contracts that depend on this module:

* ``docs/specs/prompt-inventory.md`` is GENERATED from this registry by
  ``scripts/gen_prompt_inventory.py``; ``test_prompt_inventory_doc.py`` fails CI
  when the committed doc drifts from the registry.
* ``test_prompt_registry_goldens.py`` renders every prompt through its real
  caller and byte-compares against goldens captured from the pre-registry tree
  (``backend/tests/fixtures/prompt_goldens.json``). A prompt edit is therefore
  never accidental: it fails that test until the golden is deliberately
  re-captured and the ``version`` bumped.

**Versioning.** ``version`` starts at 1 for every prompt and is a plain
monotonic integer. Bump it in the SAME commit that changes what the provider
receives — normally ``text``, but also a caller changing the block it renders
into a placeholder (``portfolio.construction.user`` v2 is exactly that case).
The version is what a trace row stamps (``prompt_id``/``prompt_version``), so
an unbumped edit silently re-labels old traces as the new prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from string import Template
from types import MappingProxyType

__all__ = ["PROMPTS", "Prompt"]


@dataclass(frozen=True)
class Prompt:
    """One versioned prompt template.

    ``text`` is the literal template — what the provider receives once
    ``placeholders`` are substituted. ``role`` says where it lands:

    * ``"system"`` — passed as ``system`` to ``LLMBackend.complete``.
    * ``"user"``   — passed as ``user``. (Most user messages in this tree are
      ``json.dumps`` of a data payload rather than a template; only the ones
      that carry prose are registry entries.)
    * ``"fragment"`` — not sent on its own; substituted into, or concatenated
      onto, another entry. ``embedded_in`` names which.
    """

    id: str
    version: int
    role: str
    text: str
    placeholders: tuple[str, ...] = ()
    call_sites: tuple[str, ...] = ()
    summary: str = ""
    embedded_in: tuple[str, ...] = ()

    def render(self, **values: object) -> str:
        """Substitute every placeholder. Missing or unknown keys raise.

        Deliberately strict (``Template.substitute``, not ``safe_substitute``):
        a typo'd keyword must not ship a literal ``${rebuttal}`` to a provider.
        """
        supplied = set(values)
        declared = set(self.placeholders)
        if supplied != declared:
            missing = sorted(declared - supplied)
            unknown = sorted(supplied - declared)
            raise KeyError(f"{self.id}: missing={missing} unknown={unknown}")
        return Template(self.text).substitute(values)


# ── Shared fragment: the Archimedes-DSL spec contract ────────────────────────
# Concatenated onto BOTH fusion prompts. It is one constant, not two copies, so
# a DSL change can never land in the proposer and miss the repair retry —
# `test_multi_paper_utilization` asserts the containment directly.

_SPEC_CONTRACT_TEXT = """The strategy_spec field is REQUIRED. It is a machine-readable strategy definition \
using the Archimedes DSL (closed-enum vocabulary). For asset_universe, list the \
tickers the mechanism trades from the user's selected assets (in user_steer); the \
platform overrides this with the user's chosen universe, so do not default to a \
single broad-market proxy. Valid rebalance_frequency values: \
daily, weekly, monthly. Valid indicators: sma_N, ema_N, rsi_N, momentum_N, \
realized_vol_N (replace N with an integer period). momentum_N is the trailing \
N-bar RETURN, centred on 0 (+0.05 means +5%); write momentum thresholds on that \
scale — e.g. {"gt": ["momentum_20", 0]} means "trailing 20-bar return is \
positive". realized_vol_N is the ANNUALIZED standard deviation of the last N \
daily returns, so 0.15 means 15% annualized vol — e.g. \
{"lt": ["realized_vol_20", 0.15]} means "the last 20 bars were calm". \
Entry/exit conditions use comparison ops (gt, lt, gte, lte) \
or logic ops (and, or, not). Position sizing types: full_invested_when_in_market \
(all-in while the entry condition holds; no other keys), equal_weight (1/N of \
the account per name in asset_universe; no other keys), inverse_vol \
(equal_weight scaled by reference_vol_annual / realized vol; the ONLY extra key \
is reference_vol_annual, optional, must be > 0, defaults to 0.15), \
volatility_target (the ONLY extra key is annual_pct, required, > 0). \
position_sizing accepts NO other keys — a key outside that list is a hard \
validation error, not an ignored field, so do not invent one. Do NOT emit any \
field asserting the strategy's own correctness or look-ahead safety; the \
platform derives that structurally from the spec and ignores anything you \
claim about it.
parameter_variants is OPTIONAL: a dict mapping indicator aliases to 2-8 numeric \
values for CSCV overfitting detection (e.g. {"sma_200": [150, 175, 200, 225, 250]}). \
Keys must reference indicators used in entry/exit conditions.
paper_mechanisms is a TOP-LEVEL field of the proposal, NOT a key inside \
strategy_spec — do not emit it here. It maps each cited paper to the part of \
this spec it produced: each entry's spec_elements must name indicator aliases \
that actually appear in the spec's entry/exit conditions (the same rule \
parameter_variants keys follow). An alias that is not in entry/exit is \
dropped, and a cited paper left with no surviving spec_element counts as \
UNATTRIBUTED."""


# ── Fusion proposer (also the debate proposer) ───────────────────────────────

_FUSION_PROPOSER_SYSTEM_TEXT = (
    """You are Archimedes Fusion, an AI quant-research synthesizer. \
You design a NOVEL trading-strategy hypothesis by FUSING the mechanisms of \
MULTIPLE peer-reviewed quantitative-finance papers into one combined approach.

Hard rules:
- You MUST fuse AT LEAST ${fuse_target_min} of the provided papers when that \
many are provided; NEVER fewer than ${min_papers}. A single-paper answer is \
invalid — that is a different tool's job — and never cite a paper not in the \
provided list.
- If you cite fewer than ${fuse_target_min}, you MUST justify the shortfall in \
`fusion_reasoning`, naming each rejected paper and why it contributes no \
distinct mechanism. Padding the citation list with a paper whose mechanism \
you cannot name is WORSE than an honest shortfall: citation count is read \
downstream as evidence depth, so a fabricated mechanism launders weak \
evidence into the provenance record.
- Reference papers ONLY by an arxiv_id from the provided candidates. Never \
invent a paper or an arxiv_id.
- Every id in `source_arxiv_ids` MUST have a matching entry in \
`paper_mechanisms` naming the one mechanism that paper contributes and the \
`spec_elements` — indicator aliases that literally appear in the spec's \
entry/exit conditions — it produced. If you cannot name the mechanism a paper \
contributes to THIS spec, OMIT that id from `source_arxiv_ids` entirely; do \
NOT pad the citation list with it and do not invent a mechanism for it. An \
honest shorter list is correct; a longer one you cannot map is laundering.
- OPTIMIZE FOR NOVELTY. The edge is the combination the literature has NOT \
published. Published single-paper alpha decays post-publication (McLean & \
Pontiff 2016) — your value is the non-obvious synthesis, not re-stating one \
paper. Explain why the COMBINATION is non-obvious relative to each paper alone.
- This is a HYPOTHESIS, not validated alpha. Do NOT invent Sharpe ratios, \
returns, or backtest numbers. Do NOT promise or forecast returns. State \
plainly that empirical validation (backtest / DSR / PBO) is pending.
- Respect the user's risk envelope (USYC floor/ceiling, target vol, max DD) \
as a synthesis constraint, not as a paper filter.

"""
    # The half above is the only templated one: ${fuse_target_min} / ${min_papers}
    # are substituted from strategy_fusion's constants at import. Everything from
    # here down is literal — the JSON schema is full of braces, which is exactly
    # why the registry templates on `$name` (string.Template) and not `{name}`.
    + """Output STRICT JSON ONLY (no prose, no markdown fences), exactly this schema:
{
  "strategy_name": "<short working name for the fused strategy>",
  "thesis": "<the fused strategy in plain language, honest it is pre-backtest>",
  "source_arxiv_ids": ["<arxiv_id from candidates>", "<another>", ...],
  "fusion_reasoning": "<what mechanism EACH cited paper contributes and how \
they combine>",
  "paper_mechanisms": [
    {"arxiv_id": "<from source_arxiv_ids>",
     "mechanism": "<the one mechanism THIS paper contributes>",
     "spec_elements": ["<indicator alias used in entry/exit>", "..."]}
  ],
  "novelty_rationale": "<why this specific combination is not already in the \
literature>",
  "risk_notes": "<key risks + the pre-backtest / selection-bias caveat>",
  "strategy_spec": {
    "name": "<same as strategy_name>",
    "asset_universe": ["<ticker>", "<ticker>", ...],
    "rebalance_frequency": "monthly",
    "entry": {"gt": ["close", "sma_200"]},
    "exit": {"lt": ["close", "sma_200"]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": ["<from source_arxiv_ids above>"],
    "indicators": ["sma_200"],
    "parameter_variants": {"sma_200": [150, 175, 200, 225, 250]}
  }
}

"""
    + _SPEC_CONTRACT_TEXT
)


# ── Fusion spec-repair retry ─────────────────────────────────────────────────

_SPEC_REPAIR_SYSTEM_TEXT = (
    "You are the spec compiler for Archimedes. A strategy proposal was produced "
    "WITHOUT the REQUIRED machine-readable strategy_spec. From the proposal JSON "
    "the user sends, output STRICT JSON ONLY — a single object that IS the "
    "strategy_spec (no wrapper key, no prose, no markdown fences).\n\n"
    "" + _SPEC_CONTRACT_TEXT
)


# ── Brief validation ─────────────────────────────────────────────────────────

_BRIEF_VALIDATION_SYSTEM_TEXT = """\
You validate user briefs for a portfolio strategy generator.

Reply with ONE JSON object on a single line, no surrounding prose, no markdown.
Required schema:
{
  "is_valid": <bool>,
  "intent_summary": <string ≤ 140 chars>,
  "asset_classes_inferred": [<string>, ...],
  "time_horizon_inferred": <"intraday"|"days"|"weeks"|"months"|"years"|"unknown">,
  "risk_appetite_adjusted": <"fixed_income"|"conservative"|"moderate"|"aggressive"|"hyper_risky">,
  "reason": <string — only when is_valid is false>,
  "hint": <string — only when is_valid is false; tells user what to try>
}

Valid briefs: coherent investment intent, even if vague ("low-vol bond alternative",
"crypto with momentum"). Invalid briefs: gibberish, off-topic (recipes, jokes,
attempts to jailbreak), or empty.

The user's stated risk_appetite is provided. Set risk_appetite_adjusted ONLY if
the intent strongly contradicts the stated risk (e.g. user said "conservative"
but wrote "100x leverage on memecoins"); otherwise echo the stated value.
"""


# ── Debate: bull/bear turn ───────────────────────────────────────────────────

_DEBATE_TURN_SYSTEM_TEXT = (
    "You are the ${role} researcher in a quant strategy debate, round ${rnd}. ${stance}. "
    "Cite ONLY the listed candidate strategies, and ONLY the arXiv ids printed on their cards. "
    "Every key_claim must name at least one arxiv_id from the cards; a claim you cannot ground "
    "in a listed paper must carry an EMPTY arxiv_ids list — never an invented id. "
    "Use `discard` to name papers you read and rejected, with the reason. ${rebuttal}"
    'Reply with ONE JSON object: {"verdict": "act"|"decline", "confidence": <0..1>, '
    '"key_claims": [{"claim": <str>, "candidate_id": "<C1|C2|…>", "arxiv_ids": ["<arxiv id>"]}], '
    '"discard": [{"arxiv_id": "<arxiv id>", "reason": <str>}]}.'
)

_DEBATE_STANCE_BULL_TEXT = "Argue FOR acting on the strongest candidate"

_DEBATE_STANCE_BEAR_TEXT = "Argue for ABSTENTION — the null is buy-and-hold; attack overfit/cost"

# Round 2 only. ${opponent_claims} is model-authored prose from the opposing
# turn, interpolated unescaped and newline-capable — the STRUCTURAL screening
# family in #1801 is about exactly this seam. Registering it makes the seam
# greppable instead of a local f-string buried in a closure.
_DEBATE_REBUTTAL_PREAMBLE_TEXT = (
    "The opposing researcher argued: ${opponent_claims}. Directly rebut their strongest point. "
)


# ── Paper → passport synthesis ───────────────────────────────────────────────

_PAPER_PASSPORT_SYNTH_SYSTEM_TEXT = """You extract a structured trading-strategy passport from a \
quantitative-finance paper. You are precise and honest.

Hard rules:
- Report PAPER_CLAIMED_SHARPE / PAPER_CLAIMED_CAGR / PAPER_CLAIMED_MAX_DD ONLY \
if the paper explicitly states that number. If it does not, use null. Never \
estimate or infer a performance number.
- METHODOLOGY_TEXT must be faithful to the paper's actual method, not a \
generic description.
- POSITION_SIZING must be one of: equal_weight, risk_parity, kelly, inverse_vol.
- REBALANCE_FREQUENCY must be one of: daily, weekly, monthly.
- RISK_PROFILES is a subset of: conservative, moderate, aggressive, hyper_risky.

Output STRICT JSON ONLY, exactly this schema:
{
  "methodology_summary": "<2-3 sentence plain-English summary>",
  "methodology_text": "<faithful, detailed description of the method>",
  "asset_universe": ["<ticker-or-asset-class>", ...],
  "position_sizing": "<one allowed value>",
  "rebalance_frequency": "<one allowed value>",
  "risk_profiles": ["<allowed value>", ...],
  "paper_claimed_sharpe": <number or null>,
  "paper_claimed_cagr": <number or null>,
  "paper_claimed_max_dd": <number or null>
}"""


# ── Portfolio construction ───────────────────────────────────────────────────

_PORTFOLIO_SYSTEM_TEXT = (
    "You are Archimedes, an autonomous portfolio-construction agent for a "
    "non-custodial USDC-settled vault on Arc.\n\n"
    "Your responsibility: pick a diversified portfolio of *individual* tradable "
    "instruments (individual stocks, bonds, futures, FX, crypto — not just "
    "broad ETFs unless they are the best vehicle for a thesis). Every pick "
    "MUST be anchored to one of the paper-grounded quant strategies in our "
    "library (the 'strategy passport' model). You may NOT invent strategies — "
    "anchor only to the ones provided in the user prompt.\n\n"
    "PRINCIPLES\n"
    "- Diversify by asset class AND by exchange (US, European, Asian, Turkish, "
    "  metals/futures, FX, crypto).\n"
    "- Prefer individual stocks where you have a specific thesis (e.g. NVDA, ASML "
    "  for AI capex; THYAO, KCHOL for Turkish play; XOM, CVX for energy).\n"
    "- Use individual bond ETFs by maturity (BIL=t-bills, SHY=1-3y, IEF=7-10y, "
    "  TLT=20y+, TIP=inflation-linked) rather than only aggregate funds.\n"
    "- Respect the synth-budget cap. The remainder is held as USDC (the safety "
    "  floor) which the user already knows about — you don't list USDC.\n"
    "- No single pick > 20% of the synth budget.\n"
    "- Pick 5-12 instruments total.\n\n"
    "OUTPUT FORMAT\n"
    "Return ONLY a JSON object, nothing else (no prose before or after). Schema:\n"
    "{\n"
    '  "thesis": "1-2 sentence portfolio thesis tying regime + risk profile to picks",\n'
    '  "picks": [\n'
    '    {"ticker": "NVDA", "weight": 0.12, "paper_anchor": "moskowitz_2012_tsmom",\n'
    '     "reasoning": "12m return +75%, qualifies for TSMOM long; AI capex cycle"},\n'
    "    ...\n"
    "  ]\n"
    "}\n\n"
    "`ticker` MUST be the display symbol shown in the AVAILABLE UNIVERSE table. "
    "Weights are fractions of the synth budget (will be renormalized if needed). "
    "`paper_anchor` MUST be one of the strategy ids listed below."
)


# Percentages arrive PRE-FORMATTED (`f"{x:.0%}"` at the call site): string.Template
# has no format specs, and moving the formatting to the caller keeps the template
# readable as the text it actually is.
_PORTFOLIO_USER_TEXT = (
    "## CONTEXT\n"
    "- regime: ${regime} (confidence ${regime_confidence})\n"
    "- risk_profile: ${risk_profile}\n"
    "- usdc_floor: ${usdc_floor} (held as USDC, you do not allocate this)\n"
    "- synth_budget: ${synth_budget} (your weights must sum to <= this)\n\n"
    "## TOP MARKET OPPORTUNITIES (live 90-day risk-adjusted ranking)\n"
    "${market_scan}\n\n"
    "## PAPER STRATEGIES (you must anchor every pick to one of these ids)\n"
    "${strategies}\n\n"
    "## AVAILABLE UNIVERSE (pick any of these tickers; * = appeared in top scan)\n"
    "${universe}\n\n"
    "## YOUR TASK\n"
    "Construct the portfolio. Return ONLY JSON per the schema in the system prompt."
)


# ── The registry ─────────────────────────────────────────────────────────────
# Keyed by id, ordered by id. `scripts/gen_prompt_inventory.py` renders this
# into docs/specs/prompt-inventory.md; the drift test regenerates and compares.

_ENTRIES: tuple[Prompt, ...] = (
    Prompt(
        id="brief_validation.system",
        version=1,
        role="system",
        text=_BRIEF_VALIDATION_SYSTEM_TEXT,
        call_sites=("archimedes.agents.generation_pipeline._validate_brief",),
        summary=(
            "Asks the model to judge whether a user brief is a coherent investment intent "
            "and to infer asset classes / horizon / risk. Enrichment plus admission today; "
            "#1801 moves admission to a deterministic screener."
        ),
    ),
    Prompt(
        id="debate.rebuttal_preamble",
        version=1,
        role="fragment",
        text=_DEBATE_REBUTTAL_PREAMBLE_TEXT,
        placeholders=("opponent_claims",),
        call_sites=("archimedes.agents.debate_engine._rebuttal_clause",),
        embedded_in=("debate.turn.system",),
        summary=(
            "Round-2 only: the opposing researcher's round-1 claims, screened by "
            "_rebuttal_clause (#1801) and joined with '; ', substituted into "
            "debate.turn.system's ${rebuttal}. Round 1 substitutes ''."
        ),
    ),
    Prompt(
        id="debate.stance.bear",
        version=1,
        role="fragment",
        text=_DEBATE_STANCE_BEAR_TEXT,
        call_sites=("archimedes.agents.debate_engine._debate_round",),
        embedded_in=("debate.turn.system",),
        summary="The bear researcher's standing instruction, substituted into ${stance}.",
    ),
    Prompt(
        id="debate.stance.bull",
        version=1,
        role="fragment",
        text=_DEBATE_STANCE_BULL_TEXT,
        call_sites=("archimedes.agents.debate_engine._debate_round",),
        embedded_in=("debate.turn.system",),
        summary="The bull researcher's standing instruction, substituted into ${stance}.",
    ),
    Prompt(
        id="debate.turn.system",
        version=1,
        role="system",
        text=_DEBATE_TURN_SYSTEM_TEXT,
        placeholders=("rebuttal", "rnd", "role", "stance"),
        call_sites=("archimedes.agents.debate_engine._debate_round",),
        summary=(
            "One bull/bear debate turn (4 per generation: bull-r1, bear-r1, bull-r2, bear-r2). "
            "The user message is the candidate evidence cards, not a template."
        ),
    ),
    Prompt(
        id="fusion.proposer.system",
        version=1,
        role="system",
        text=_FUSION_PROPOSER_SYSTEM_TEXT,
        placeholders=("fuse_target_min", "min_papers"),
        call_sites=("archimedes.agents.strategy_fusion.StrategyFusion.propose",),
        summary=(
            "The multi-paper fusion synthesizer. This is ALSO the debate proposer: "
            "debate_engine._propose_pool fans StrategyFusion.propose across regime x "
            "mechanism steers, and the steer travels in the user JSON payload "
            "(strategic_direction), not in this system prompt."
        ),
    ),
    Prompt(
        id="fusion.spec_contract",
        version=1,
        role="fragment",
        text=_SPEC_CONTRACT_TEXT,
        call_sites=(
            "archimedes.agents.strategy_fusion.StrategyFusion.propose",
            "archimedes.agents.strategy_fusion._repair_spec",
        ),
        embedded_in=("fusion.proposer.system", "fusion.spec_repair.system"),
        summary="The Archimedes-DSL strategy_spec contract, concatenated onto both fusion prompts.",
    ),
    Prompt(
        id="fusion.spec_repair.system",
        version=1,
        role="system",
        text=_SPEC_REPAIR_SYSTEM_TEXT,
        call_sites=("archimedes.agents.strategy_fusion._repair_spec",),
        summary=(
            "One bounded retry when the proposer omitted strategy_spec: re-send the accepted "
            "proposal, ask for the spec JSON alone. Never loops."
        ),
    ),
    Prompt(
        id="paper_passport.synth.system",
        version=1,
        role="system",
        text=_PAPER_PASSPORT_SYNTH_SYSTEM_TEXT,
        call_sites=("archimedes.services.arxiv_pipeline.synthesize_passport",),
        summary=(
            "Corpus ingestion: extract a structured strategy passport from one paper. "
            "Offline pipeline, not on the Generate request path."
        ),
    ),
    Prompt(
        id="portfolio.construction.system",
        version=1,
        role="system",
        text=_PORTFOLIO_SYSTEM_TEXT,
        call_sites=("archimedes.agents.portfolio_agent.PortfolioAgent.propose_portfolio",),
        summary=(
            "Single-turn portfolio construction. Two consumers only: the StockBench adapter, "
            "and generation_pipeline's availability/model probe. It never generates strategies "
            "(docs/adr/debate-society-sole-generation-pipeline.md)."
        ),
    ),
    Prompt(
        id="portfolio.construction.user",
        # v2 (#1835): `text` is byte-for-byte v1 — what changed is the ${strategies} block
        # `portfolio_agent._format_strategies` renders into it. A paper title used to be
        # interpolated bare (`title=Faber 2007 …`) into a line that carries `sharpe=` and
        # `rigor=` on its next line; it is now screened and emitted as one quoted JSON token
        # (`title="Faber 2007 …"`). The bytes the provider receives changed, so the version
        # a trace row stamps has to change with them — an unbumped edit would label the
        # pre-#1835 traces and the post-#1835 traces with the same number.
        version=2,
        role="user",
        text=_PORTFOLIO_USER_TEXT,
        placeholders=(
            "market_scan",
            "regime",
            "regime_confidence",
            "risk_profile",
            "strategies",
            "synth_budget",
            "usdc_floor",
            "universe",
        ),
        call_sites=("archimedes.agents.portfolio_agent.PortfolioAgent.propose_portfolio",),
        summary=(
            "The portfolio agent's user message — the one user-side template in the tree with "
            "prose in it. The three rendered blocks (market scan, strategies, universe) are "
            "built by portfolio_agent's _format_* helpers."
        ),
    ),
)

PROMPTS: Mapping[str, Prompt] = MappingProxyType({p.id: p for p in _ENTRIES})
