"""The CLI's machine-readable contract — ``archimedes manifest``.

Agentic connectivity is the interface (Ricardo's skill-store brief, applied):
an agent can only invoke what it can understand, and it should understand this
tool through a declarative contract, not by parsing ``--help`` prose. Every
command declares its inputs, output contract, exit codes, cost class, and
whether it is implemented at all.

This dict is hand-written but it is NOT allowed to drift: a test walks the
real click command tree and asserts every command and every flag here matches
it (``cli/tests/test_cli.py``, the manifest-sync tests) — the promise is
checked against the truth by CI, per house convention.
"""

from __future__ import annotations

from . import __version__

EXIT_CODES = {
    "0": "OK — command succeeded (for `verify`: the gate PASSED)",
    "1": "GATE_FAILED — `verify` produced a verdict and it fails the gate",
    "2": "USAGE/AUTH — bad invocation, or no/expired session (run `archimedes login`)",
    "3": "NOT_IMPLEMENTED — the subcommand has no implementation in this version",
    # 4 has existed in exits.py since #1481 but was missing from this table —
    # an agent branching on the manifest would have read it as undefined. Fixed
    # here rather than left inconsistent while 5-8 are added around it.
    "4": "INCOMPLETE — `verify` got an answer but not every runnable leg could be evaluated; not a verdict",
    "5": "PAYMENT_REQUIRED — `generate` got a 402; the x402 requirements are printed, pay in a browser and re-run",
    "6": "ACCOUNT_ACTION_REQUIRED — `generate` got a 409; verify your email or connect a wallet, per the server's reason",
    "7": "JOB_FAILED — the generation job reached a terminal state that is not `done`",
    "8": "STILL_RUNNING — the CLI stopped waiting; the job is NOT cancelled and may still finish",
}

# Declared once and shared by every command that touches the session cache: the same
# flag, the same env var, the same meaning on all four. Four copies of this prose would
# be four places for it to drift (#1752).
SESSION_FILE_INPUT = {
    "env": "ARCHIMEDES_SESSION_FILE",
    "default": "~/.config/archimedes/session.json",
    "meaning": (
        "which session cache this invocation reads/writes; the flag wins over the env var. "
        "Give each concurrent agent its own file — two lanes sharing one file share one "
        "identity, and the second `login` wins"
    ),
}

MANIFEST: dict = {
    "tool": "archimedes",
    "version": __version__,
    "json_contract": (
        "--json makes every code path, including errors, emit exactly one JSON "
        "object on stdout; a script never parses prose."
    ),
    "exit_codes": EXIT_CODES,
    "commands": {
        "login": {
            "implemented": True,
            "description": "Authenticate with an Archimedes account and cache the session cookie.",
            "inputs": {
                "--api-url": {"env": "ARCHIMEDES_API_URL", "default": "https://archimedes-arc.com"},
                "--session-file": SESSION_FILE_INPUT,
                "--json": {"flag": True},
                "email": {"env": "ARCHIMEDES_EMAIL", "prompted_if_absent": True},
                "password": {"env": "ARCHIMEDES_PASSWORD", "prompted_if_absent": True, "hidden": True},
            },
            "output": {"ok": "bool", "email": "str"},
            "cost_class": "network: 2 HTTP calls (sign-in + session round-trip); no funds, no chain",
            "side_effects": "writes the session cache (mode 600) — see --session-file for where",
        },
        "meter": {
            "implemented": True,
            "description": "Today's generation usage vs both daily caps, plus the live price quote.",
            "inputs": {
                "--api-url": {
                    "env": "ARCHIMEDES_API_URL",
                    "default": "the cached session's URL, else https://archimedes-arc.com",
                },
                "--session-file": SESSION_FILE_INPUT,
                "--json": {"flag": True},
            },
            "output": {
                "user": "{used: int|null, cap: int} — null used means the quota backend was unavailable, never a fabricated 0",
                "ip": "{used: int|null, cap: int}",
                "quote": "the literal GET /api/generate/quote payload",
            },
            "cost_class": "network: 1 HTTP call; requires session; no funds, no chain",
            "side_effects": "none",
        },
        "verify": {
            "implemented": True,
            "description": "Run the rigor gate's evaluable checks over a returns CSV (or '-' for stdin).",
            "inputs": {
                "RETURNS_CSV": {"positional": True, "format": "two columns: date, daily_return; '-' reads stdin"},
                "--trials": {"default": 1, "min": 1, "meaning": "self-attested trial count deflating the DSR"},
                "--local": {"flag": True, "implemented": False},
                "--api-url": {
                    "env": "ARCHIMEDES_API_URL",
                    "default": "the cached session's URL, else https://archimedes-arc.com",
                },
                "--session-file": SESSION_FILE_INPUT,
                "--json": {"flag": True},
            },
            "output": {
                "passes": "bool — no evaluable check failed AND at least one was evaluable",
                "dsr": "{status: pass|fail|not_evaluable, deflated_sharpe, dsr_p_value, reason}",
                "oos_consistency": "{status, oos_sharpe, in_sample_sharpe, reason}",
                "pbo": "{status: always not_evaluable for a bare series — needs a trial matrix, reason names the decisive gap}",
                "look_ahead": "{status: always not_evaluable — needs strategy source; never uploaded}",
            },
            "cost_class": "network: 1 HTTP call; requires session; rate-limited 5/minute; free",
            "side_effects": "none",
            "honesty": (
                "checks the endpoint cannot honestly compute are reported not_evaluable "
                "with the decisive reason — never silently passed, failed, or defaulted"
            ),
        },
        "generate": {
            "implemented": True,
            "description": "Generate a rigor-gated strategy from a research brief and print its passport URL.",
            "inputs": {
                "BRIEF": {"positional": True, "format": "free text; omit it and use --brief-file instead"},
                "--brief-file": {"format": "path, or '-' to read the brief from stdin"},
                "--risk-appetite": {
                    "default": "moderate",
                    "choices": ["fixed_income", "conservative", "moderate", "aggressive", "hyper_risky"],
                },
                "--name": {"meaning": "optional name applied to the winning strategy only"},
                "--n-candidates": {"default": 1, "min": 1, "meaning": "candidates the pipeline considers internally"},
                "--model": {"meaning": "optional LLM model id; omit for the account's default free-tier model"},
                "--no-stream": {"flag": True, "meaning": "skip SSE and poll the job endpoint instead"},
                "--timeout": {"default": 900.0, "unit": "seconds", "meaning": "client-side wait budget, not a cancel"},
                "--api-url": {
                    "env": "ARCHIMEDES_API_URL",
                    "default": "the cached session's URL, else https://archimedes-arc.com",
                },
                "--session-file": SESSION_FILE_INPUT,
                "--json": {"flag": True},
            },
            "output": {
                "job_id": "str",
                "state": "done|error|cancelled|stalled|running|queued — the server's own job state",
                "strategy_id": "str|null — null is an honest absence, not a failure to report",
                "passport_url": "str|null — the browser-readable strategy passport",
                "events": "the pipeline events observed, in order, each {event, data}",
            },
            "auth": {
                "session": "the cached cookie from `archimedes login`",
                "api_key": {"env": "ARCHIMEDES_API_KEY", "sent_as": "Authorization: Bearer"},
            },
            "cost_class": (
                "network: quote + start + SSE stream + job polls; requires session; "
                "rate-limited 5/minute; MAY COST MONEY — the server's paywall decides, and a 402 "
                "exits 5 without spending anything"
            ),
            "side_effects": "creates a generation job on the server; writes nothing locally",
            "honesty": (
                "holds no keys and signs no payment: a 402 is rendered verbatim (x402 requirements + "
                "browser URL) and never acted on. Never claims a credit was restored unless the server "
                "asserts it, and never reports a client-side wait timeout as a failed job (exit 8, not 7)"
            ),
        },
        "backtest": {
            "implemented": False,
            "lands_in": "unscheduled",
            "description": "Local-only backtest of a strategy file (never uploaded).",
            "inputs": {
                "--strategy-path": {"required": True},
                "--strategy-class": {"required": True},
                "--json": {"flag": True},
            },
        },
        "manifest": {
            "implemented": True,
            "description": "This contract. Always JSON, always exit 0.",
            "inputs": {},
            "output": "this document",
            "cost_class": "local, no network",
            "side_effects": "none",
        },
    },
}
