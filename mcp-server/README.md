# archimedes-mcp

An MCP server for [Archimedes](https://archimedes-arc.com) — nine tools an agent can call
instead of writing `curl`.

**It is a thin client and nothing else.** No business logic, no database, no Redis, no
chain RPC, no wallet key, and no import of the `archimedes` backend package. Its entire
reach is one `httpx.Client` pointed at one base URL. If a capability is not in the public
HTTP API, this server does not have it — a tool that needed one would be an argument for
adding the HTTP route, not for widening the client.

The routes each tool calls are declared in
[`src/archimedes_mcp/contract.py`](src/archimedes_mcp/contract.py) and asserted against the
running app's OpenAPI document by `backend/tests/test_mcp_contract_drift.py`, which also
drives the real app with no credential and checks that every tool's `auth` label is true.
A second surface that can drift from the API is the risk this design carries; that test is
the answer to it.

## Install

Both distributions live in this repo and neither is on PyPI yet
([D6](https://github.com/aprin-labs/archimedes/pull/1653)):

```bash
pip install -e ./cli -e ./mcp-server
archimedes-mcp --help 2>/dev/null || true   # it speaks MCP over stdio; a client launches it
```

## Configure a client

```json
{
  "mcpServers": {
    "archimedes": {
      "command": "archimedes-mcp",
      "env": {
        "ARCHIMEDES_API_URL": "https://archimedes-arc.com",
        "ARCHIMEDES_API_KEY": "archim_..."
      }
    }
  }
}
```

`ARCHIMEDES_API_KEY` is optional. Without it the server falls back to the session cookie
`archimedes login` caches at `~/.config/archimedes/session.json` (mode `600`), loaded
through `archimedes_cli.session.load_session` — imported, not copied.

`ARCHIMEDES_SESSION_FILE` in the same `env` block moves that file. Running more than one
agent on a machine? Give each one its own path (`"ARCHIMEDES_SESSION_FILE":
"/home/agent/.archimedes/lane-a.json"`, and `archimedes login --session-file` that same
path). One file per agent is what keeps two agents from sharing one identity — the failure
[#1752](https://github.com/aprin-labs/archimedes/issues/1752) reported.

## Authentication

| Order | Source | Sent as |
| --- | --- | --- |
| 1 | `ARCHIMEDES_API_KEY` | `Authorization: Bearer <key>` |
| 2 | `~/.config/archimedes/session.json`, or `$ARCHIMEDES_SESSION_FILE` | the `better-auth.session_token` cookie |

**Exactly one credential goes on the wire.** When both exist the key wins here and no
`Cookie` header is sent, so the server-side precedence rule (cookie wins) can never decide
which account a call acts as.

The credential is resolved per call, not per process: a long-lived stdio server outlives
`archimedes login` and outlives a key rotation, and a revoked key must stop working
immediately rather than living on in a captured variable.

**Status of the bearer lane, honestly.** Scoped API keys are owner decision **D3** on
[#1653](https://github.com/aprin-labs/archimedes/pull/1653), implemented on branch
`dbrowneup/1653-scoped-api-keys` and **not merged to `main` at the time of writing**. The
header is written to that branch's spec so this server works the day it merges; until then
a bearer key `401`s and the `401` remedy says so rather than sending you hunting a key
problem that is really a deployment fact.

The credential is never logged, never returned, and never rendered: `Credential.__repr__`
and `__str__` are redacted, and `tests/test_no_credential_leak.py` runs every tool against
every mapped failure with both credential kinds and searches the result, the log records
and the exception text — with a control test proving the search can find a planted leak.

## Tools

| Tool | Route | Auth | Cost |
| --- | --- | --- | --- |
| `archimedes_quote` | `GET /api/generate/quote` | none | free |
| `archimedes_usage` | `GET /api/account/usage` | credential | free |
| `archimedes_rigor_verify` | `POST /api/rigor/verify` | credential | free (5/min) |
| `archimedes_generate_start` | `POST /api/generate/start` | credential | **metered** |
| `archimedes_generate_status` | `GET /api/generate/jobs/{job_id}` | credential | free |
| `archimedes_strategy` | `GET /api/strategies/{strategy_id}` | none | free |
| `archimedes_passport` | `GET /api/strategies/passports/{strategy_id}` | none | free |
| `archimedes_leaderboard` | `GET /api/leaderboard` | none | free |
| `archimedes_corpus_search` | `GET /api/papers/` | none | free |

`archimedes_generate_start` can spend real money. `GET /api/generate/quote` is the only
authority on whether the host you are calling charges — the source defaults say the paywall
is off and dry-run is on, production says the opposite, so `archimedes_quote` is the first
call every time.

## Results

Every tool returns a JSON object whose first field is `ok`.

```jsonc
{ "ok": true,  "...": "the API's own response body, verbatim" }
{ "ok": false, "error": "payment_required", "message": "…", "remedy": "…", "quote": { … } }
```

Failures are **results, not protocol errors**. A `402` carries the live quote — price,
chain, recipient — and an agent needs all of it to decide whether to pay; a protocol error
would collapse that to a string. `error` codes are an API on the same rule
[`cli/src/archimedes_cli/exits.py`](../cli/src/archimedes_cli/exits.py) states for exit
codes: new conditions get new codes, existing codes are never redefined.

The paywall, the wallet gate and the quotas are the product's real answers. This server
does not retry them, does not hide them, and does not sign payments — it holds no wallet
key, exactly as the CLI deliberately has no `eth-account` dependency.

## Tests

```bash
pytest mcp-server/tests -q          # hermetic: every HTTP call is served by httpx.MockTransport
pytest -m "not integration" -q      # from the repo root; includes the contract-drift guard
```
