# Testing conventions

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-31
> **superseded-by:** —

Codified 2026-05-27, hard-won during the post-hackathon test-coverage push and the
env-flaky-test sweep. Extracted from [`../CLAUDE.md`](../CLAUDE.md) § Testing conventions on
2026-08-31 so the session file keeps only the rule an agent gets wrong by default; the full
rules are here. **Read this before writing any new test.**

**CI green ≠ local green is itself a bug.** Tests must pass identically in both
environments. A test that passes in CI but fails locally (or the reverse) is a real defect
to fix, not a flaky test to skip-mark.

## Tests must be hermetic

No `.env` dependence, no live Redis / Postgres / Anthropic / Arc RPC. CI runs without `.env`
or those services. The hermetic gate a reviewer runs:

```bash
env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend \
  python -m pytest backend/tests/test_<module>.py -q
# must end: N passed, 0 failed
```

## `asyncio.get_event_loop().run_until_complete(...)` is forbidden

Python 3.12 removed implicit loop creation in non-running contexts and raises
`RuntimeError`. Use `asyncio.run(coro)` for a sync test calling an async function, or
`async def` plus the automatic `@pytest.mark.asyncio` (`asyncio_mode` is `auto` in
[`../pytest.ini`](../pytest.ini)) for async tests. The CI gate:
`grep -r "asyncio.get_event_loop" backend/tests/` must return nothing.

## Subprocess tests must use `_clean_subprocess_env()` + `_DOTENV_NEUTRALIZE`

Reference pattern in
[`../backend/tests/test_security_hardening.py`](../backend/tests/test_security_hardening.py).
Inheriting `os.environ` leaks the developer's `.env` (which sets
`DATABASE_URL=postgresql://...@postgres:5432/...`, a hostname only reachable inside docker
compose) into the subprocess, causing `psycopg2.OperationalError` on bare-metal local. The
parent pytest process can also leak `.env` vars via earlier test imports that trigger
`load_dotenv` — `_DOTENV_NEUTRALIZE` plus an explicit `env=` whitelist on `subprocess.run`
are both needed.

## Mock at boundaries, not internals

Wrong: mocking dict operations or internal helpers. Right: mocking the HTTP client, the DB
session, the Redis client, the chain client, the Circle signer. Real precedents to copy:

- `AgentStateStore` mock for Redis-down scenarios — see
  [`../backend/tests/test_api_routes.py`](../backend/tests/test_api_routes.py)
  `TestAgentRoutes::test_agent_status_redis_down_defaults` (uses
  `patch.object(AgentStateStore, ..., AsyncMock(side_effect=ConnectionError))`).
- `chain_client` + `chain_executor` mocking — see
  [`../backend/tests/test_api_routes.py`](../backend/tests/test_api_routes.py) `client`
  fixture (line 36).
- SIWE signed-cookie test helper — see
  [`../backend/tests/test_user_routes.py`](../backend/tests/test_user_routes.py)
  `_siwe_cookies(wallet)` for testing PII-gated endpoints with a real signed session (not
  header spoofing).
- tmp-sqlite DB fixture — see
  [`../backend/tests/test_api_routes.py`](../backend/tests/test_api_routes.py) `_use_tmp_db`
  (monkeypatch.setenv `DATABASE_URL` to a tmp sqlite).
- `httpx.ASGITransport` for endpoint tests — see
  [`../backend/tests/test_risk_routes.py`](../backend/tests/test_risk_routes.py).

**Corollary from the 2026-08-31 merge-train break:** a boundary mock must stub the shared
function's *full* surface, not only the calls the branch under test makes. A double that
covers your own path silently drops a sibling's — see [`../CLAUDE.md`](../CLAUDE.md)
§ "Before you approve a merge", rule 5.

## Test the production code path, not the easy one

When a function accepts multiple input types (e.g. `_confirm_receipt` takes both `str` and
`bytes` HexBytes), the test matrix must cover *every* type the production code path emits.
The raw-key signer in `chain/executor.py` emits `HexBytes`; tests that only exercise the
`str` branch leave the production path uncovered. Issue
[#408](https://github.com/aprin-labs/archimedes/issues/408) was filed to backfill this specific
gap.

## Coverage targets and gates

Per-module ≥85% line coverage is the standard for new test work. Measure with:

```bash
pytest --cov=archimedes.<module> --cov-report=term-missing backend/tests/test_<module>.py
```

The repo-level `--cov-fail-under=60` gate is conditioned on `t2o2` being the PR author and
is therefore **dormant** — nothing enforces repo-level coverage today. See
[`agent-operations.md`](agent-operations.md) § Spec-driven execution for why that account is
not a live resource.

## No skip-marks on flaky tests

If a test is flaky, the cause is almost always a missing mock at a boundary or hidden
environmental state. Fix the flakiness, don't `@pytest.mark.skip`. Skip-marks should be rare
and load-bearing (e.g. "Requires `chain_client.settings` module-level init mocking" — a
known architectural limitation, not a flaky test).

## Running the suite

Command reference, coverage picture, and the analytics-engine's separate suite are in
[`../SETUP.md`](../SETUP.md) § Running the test suite. The exact command CI's blocking gate
runs, from the repo root, is `pytest -m "not integration"`.
