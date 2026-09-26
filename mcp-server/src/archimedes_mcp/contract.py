"""The tool contract — what this server offers, what each tool costs, and what it calls.

**This module imports nothing.** Not the MCP SDK, not ``httpx``, not a sibling module.
That is load-bearing: ``backend/tests/test_mcp_contract_drift.py`` runs inside the backend
unit suite, which does not install this distribution, and reads this file by path. A guard
that cannot run without first installing the thing it guards is a guard that gets skipped.

Two promises are made here and pinned by tests rather than by good intentions:

1. **Every route below exists.** ``test_mcp_contract_drift.py`` resolves each
   ``"METHOD /path"`` against the running app's OpenAPI document, reusing
   ``tests.test_agent_discovery.unresolved_routes``. A tool that calls a route the API
   does not serve is the #1293 failure mode with an extra layer of indirection — the
   agent spends a call, gets a 404, and cannot tell "wrong path" from "endpoint down".
2. **Every ``auth`` label is true.** The same file drives the real ASGI app with no
   credential and asserts each ``credential`` tool 401s and each ``none`` tool does not.

``description`` is not documentation. An LLM agent reads it as ground truth before
deciding whether to spend money, so ``claims must be true`` (CLAUDE.md's first rule)
applies to these strings exactly as it applies to the UI. In particular: ``generate_start``
says it charges, ``corpus_search`` says it is lexical, and nothing here promises a
capability the HTTP API does not have.

Shape mirrored from ``cli/src/archimedes_cli/manifest.py`` — a hand-written machine-readable
contract whose agreement with the real command tree is asserted by CI.
"""

from __future__ import annotations

SERVER_NAME = "archimedes"

SERVER_INSTRUCTIONS = """\
Archimedes turns quantitative-finance research literature into rigor-gated strategies.

This server is a THIN CLIENT over the public HTTP API at https://archimedes-arc.com. It
holds no business logic and has no database, no Redis, and no private key. It can do
exactly what an unprivileged HTTP caller can do and nothing more.

Read `archimedes_quote` before `archimedes_generate_start`. Generation is metered: on
production it charges real testnet USDC per run, and the quote is the ONLY authority on
whether the host you are calling charges and how much — the source defaults disagree with
production on purpose.

Every tool returns a JSON object whose first field is `ok`. Read `ok` before reading
anything else. On `ok: false` the object carries `error` (a stable machine-readable code),
`message`, and `remedy`. Paywall, quota and gate refusals (402/409/429) are returned that
way rather than being retried or hidden: they are the product's real answers, and acting
on them is your job, not this server's.

This server does not sign payments. A 402 is handed to you with the server's own quote
attached; signing an x402/EIP-3009 authorization needs a wallet key this server
deliberately does not have.
"""

# Cost classes. `free` means the API charges nothing for the call itself (rate limits and
# daily quotas still apply — those refuse, they do not bill). `metered` means the call can
# take money on a host whose `GET /api/generate/quote` says `payment_required: true`.
COST_FREE = "free"
COST_METERED = "metered"

# Auth classes, matching how the API actually behaves — not how we wish it behaved.
AUTH_NONE = "none"
AUTH_CREDENTIAL = "credential"

CREDENTIAL_SOURCES = (
    "ARCHIMEDES_API_KEY  ->  Authorization: Bearer <key>",
    "~/.config/archimedes/session.json  ->  the session cookie `archimedes login` cached "
    "(mode 600) -- __Secure-better-auth.session_token in production, "
    "better-auth.session_token on local HTTP. ARCHIMEDES_SESSION_FILE moves that file, "
    "which is how concurrent agents on one machine hold separate identities",
)

TOOLS: tuple[dict, ...] = (
    {
        "name": "archimedes_quote",
        "auth": AUTH_NONE,
        "cost": COST_FREE,
        "routes": ("GET /api/generate/quote",),
        "description": (
            "Price a strategy generation on THIS host, before holding any credential. "
            "Free, public, and the only authority on whether generation costs money: it "
            "returns payment_required, price, asset, chain, recipient and dry_run as the "
            "deployment actually has them. The code defaults are the opposite of what "
            "production serves (defaults: paywall off, dry-run on; production today: "
            "paywall on, dry_run false, $2.00 USDC per run), so reading the source or "
            "carrying an answer over from another host gets the wrong answer. Call this "
            "first, every time, on every host."
        ),
    },
    {
        "name": "archimedes_usage",
        "auth": AUTH_CREDENTIAL,
        "cost": COST_FREE,
        "routes": ("GET /api/account/usage",),
        "description": (
            "Today's generation usage against both daily caps (per account and per IP) "
            "plus the live price quote, for the credential you are holding. Free, and "
            "reading it increments nothing. The caps sit UNDERNEATH the paywall and are "
            "enforced before it, so a quota-blocked caller is refused without ever being "
            "asked to pay. A bucket may report used: null with an error string — that is "
            "an honest 'the counter could not be read', never a fabricated 0; do not "
            "treat it as zero usage."
        ),
    },
    {
        "name": "archimedes_rigor_verify",
        "auth": AUTH_CREDENTIAL,
        "cost": COST_FREE,
        "routes": ("POST /api/rigor/verify",),
        "description": (
            "Run the rigor gate over a returns series you already have, and get the gate's "
            "verdict back. Free; rate-limited to 5 requests per minute. Computes a deflated "
            "Sharpe ratio (deflated by the trials count YOU declare — it is self-attested "
            "and unverifiable) and a walk-forward out-of-sample consistency check, using the "
            "identical functions the strategy-passport gate uses. Two of the gate's four "
            "legs can never run on a bare returns series: PBO needs a trial matrix of "
            "candidate strategies and the look-ahead audit needs strategy source code. Both "
            "come back not_evaluable with the decisive reason rather than being scored as "
            "passes, so a `passes: true` here is a CAPPED verdict, not the full gate. Your "
            "strategy code is never uploaded — only the returns series you pass in. "
            "THE INPUT CONTRACT IS STRICT AND THE SERVER REPAIRS NOTHING: dates must be strict "
            "YYYY-MM-DD, unique, and in ASCENDING order; daily_return must be a finite simple "
            "decimal with |r| <= 1.0 (+1.3% is 0.013, not 1.3); the series is 250 to 2600 rows "
            "and trials is 1 to 10000. THE MINIMUM EVALUATION WINDOW IS 250 DAILY BARS — one "
            "trading year. Under it you get a refusal (422, reason window_too_short, with "
            "bars_received and bars_required), never a verdict and never a verdict with a warning "
            "on it, so do not retry a short series expecting a caveated answer: fetch more "
            "history. A violating body comes back 422 with a machine-readable "
            "reason: invalid_date, duplicate_date, unsorted_dates, non_finite, out_of_range, "
            "window_too_short, too_many_rows or trials_out_of_range. It will not sort or "
            "deduplicate your rows for you — the walk-forward split is positional, so re-ordering "
            "them server-side would return a verdict on a series you did not send. A verdict can "
            "still be INCOMPLETE (legs_evaluated < legs_runnable) when a leg cannot run on the "
            "numbers themselves — a zero-variance series, say — which is never a pass."
        ),
    },
    {
        "name": "archimedes_generate_start",
        "auth": AUTH_CREDENTIAL,
        "cost": COST_METERED,
        "routes": ("POST /api/generate/start",),
        "description": (
            "THIS TOOL CAN SPEND MONEY. Submit a research brief and start a strategy "
            "generation. On a host whose archimedes_quote says payment_required: true "
            "(production today, $2.00 USDC, dry_run false, settles for real) this call is "
            "refused until a wallet is linked and an x402 payment is signed — you get "
            "409 wallet_link_required, then 402 with the machine-readable requirements. "
            "This server does NOT sign payments and holds no wallet key: a 402 is returned "
            "to you as ok: false with error: payment_required and the server's own quote "
            "attached, for you to act on. It is never retried, and it must not be blindly "
            "retried by you either — a fresh x402 signature is a fresh real charge. On a "
            "payment_required: false host the call needs no wallet and no payment and "
            "returns 202 with a job_id. Refusals arrive in a fixed order and none of them "
            "takes money: daily cap (429), queue full (429), wallet missing (409), payment "
            "required (402)."
        ),
    },
    {
        "name": "archimedes_generate_status",
        "auth": AUTH_CREDENTIAL,
        "cost": COST_FREE,
        "routes": ("GET /api/generate/jobs/{job_id}",),
        "description": (
            "Poll one generation job. Free. state is one of queued, running, stalled, done, "
            "error, cancelled; move on when state == 'done' AND best_strategy_id is "
            "non-null. 'stalled' is derived at read time (a running job whose heartbeat is "
            "over five minutes old), 'error' and 'cancelled' are terminal. A job that is "
            "not yours returns 404, never 403 — existence is private, so a 404 here is not "
            "proof the id is wrong. Then read the AUTHORITATIVE verdict with "
            "archimedes_strategy: the generation-time verdict and the stored verdict of "
            "record can disagree, and the stored one wins."
        ),
    },
    {
        "name": "archimedes_strategy",
        "auth": AUTH_NONE,
        "cost": COST_FREE,
        "routes": ("GET /api/strategies/{strategy_id}",),
        "description": (
            "Read one strategy and its authoritative rigor verdict. Free and public — no "
            "credential needed; a private strategy answers 404 rather than 401. This route "
            "runs no gate: it serves the STORED verdict of record, graded once by the real "
            "gate and persisted on the strategy's passport, so it agrees with "
            "archimedes_passport for the same id by construction. rigor_gate_status is "
            "four-state and each state means something different: 'pass' (the stored grade "
            "passed), 'fail' (the stored grade failed at least one criterion — an honest "
            "outcome, not an error), 'pending' (NO GATE HAS GRADED THIS ROW — either there "
            "are no persisted returns to grade, or the grading job has not run over the "
            "ones there are; read graded_at on archimedes_passport to tell them apart), "
            "'degenerate' (the graded series was zero-variance). passes_rigor_gate is true "
            "only for 'pass'. Never read 'pending' or 'fail' as a soft yes, and never read "
            "'pending' as proof that no backtest exists."
        ),
    },
    {
        "name": "archimedes_passport",
        "auth": AUTH_NONE,
        "cost": COST_FREE,
        "routes": ("GET /api/strategies/passports/{strategy_id}",),
        "description": (
            "Read one strategy passport — the RIGOR VERDICT OF RECORD, the papers the "
            "strategy was built from, and its provenance. Free and public. The verdict is "
            "graded once, at backtest time, and stored: this route serves it verbatim and "
            "never recomputes it, so it agrees with archimedes_strategy for the same id by "
            "construction — curated or generated. rigor_gate_status is the same four-state "
            "that tool documents "
            "('pass'|'fail'|'pending'|'degenerate'), and passes_rigor_gate is true only for "
            "'pass'. Three provenance fields say WHICH grade you are reading: graded_at "
            "(null means never graded, which agrees with 'pending'), gate_version (the gate "
            "that produced it — the literal 'legacy-derived' means the verdict was inferred "
            "from older columns by a migration, not produced by a gate run, so treat it as "
            "un-regraded), and cohort_n (1 = graded against itself alone). 'pending' means "
            "no gate has looked at this strategy yet — it is not a soft fail, and it is the "
            "honest answer for a curated strategy whose grading job has not run. Two status "
            "fields, on purpose: 'status' is the persisted lifecycle column (what "
            "archimedes_strategies filters on) and 'served_status' is the card status the "
            "stored verdict derives, which is what archimedes_strategy returns in its "
            "'status' field. display_metrics_source names WHICH source the headline "
            "numbers on this row came from ('strategy_record' = a fixture snapshot the "
            "record ships with, 'persisted_backtest' = a real backtest run, "
            "'stub_placeholder' = a constant hand-declared in the strategy file, "
            "'unavailable', or null when it was never recorded) — read it before quoting a "
            "Sharpe, and note that only 'persisted_backtest' is a measurement. "
            "Unpublished passports that are not yours "
            "answer 404, never 403 (a 403 would confirm the id exists). Owner wallet "
            "addresses are redacted for anyone but the owner."
        ),
    },
    {
        "name": "archimedes_leaderboard",
        "auth": AUTH_NONE,
        "cost": COST_FREE,
        "routes": ("GET /api/leaderboard",),
        "description": (
            "Rank strategies. Free; never 401s. scope='own' ranks the credentialed "
            "caller's own strategies against each other (this is a single-user product — "
            "it is not a cross-user competition); scope='curated' returns the curated seed "
            "library as reference. An anonymous request for 'own' is transparently served "
            "'curated' instead, so read the response's own `scope` field to learn what you "
            "actually got rather than assuming you got what you asked for. If the data "
            "source is unavailable the board comes back empty rather than erroring."
        ),
    },
    {
        "name": "archimedes_corpus_search",
        "auth": AUTH_NONE,
        "cost": COST_FREE,
        "routes": ("GET /api/papers/",),
        "description": (
            "Search the q-fin paper corpus. Free and public. LEXICAL ONLY: a "
            "case-insensitive substring match over title, abstract and author names — no "
            "embeddings, no semantic similarity, no ranking, no stemming. A query that "
            "would need meaning rather than characters will miss. By default only papers "
            "the knowledge-base pipeline has fully processed are returned; "
            "processed_only=false reveals the larger raw metadata-only set, which has no "
            "topic labels or neighbours. Abstracts are truncated to 200 characters."
        ),
    },
)

TOOL_NAMES: tuple[str, ...] = tuple(t["name"] for t in TOOLS)


def routes() -> tuple[str, ...]:
    """Every ``"METHOD /path"`` any tool can call, deduplicated and sorted."""
    return tuple(sorted({route for tool in TOOLS for route in tool["routes"]}))


def by_name(name: str) -> dict:
    for tool in TOOLS:
        if tool["name"] == name:
            return tool
    raise KeyError(name)


__all__ = [
    "AUTH_CREDENTIAL",
    "AUTH_NONE",
    "COST_FREE",
    "COST_METERED",
    "CREDENTIAL_SOURCES",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "TOOLS",
    "TOOL_NAMES",
    "by_name",
    "routes",
]
