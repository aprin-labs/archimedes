---
name: archimedes-cli
description: How to install and drive the `archimedes` command-line tool — `login`, `meter`, and `verify` against the hosted API — including the exact `--json` shape on every path, the stable exit-code contract, and why `verify` can only ever evaluate two of the gate's four checks over a bare returns series.
triggers:
  - "how do I use the archimedes CLI"
  - installing or scripting `archimedes-cli` (`pip install archimedes-cli`)
  - "archimedes login" / "archimedes meter" / "archimedes verify"
  - wiring `archimedes verify` into a CI job and branching on its exit code
  - reading a `not_evaluable` PBO or look-ahead status from `verify`'s output
  - "what does archimedes verify actually check" / "why is PBO not_evaluable"
  - asking what the CLI does NOT do yet (`backtest`, `verify --local`)
---

# Driving Archimedes from the command line

This skill grounds every claim in the working tree. File:line citations refer to
`cli/src/archimedes_cli/` and `backend/archimedes/api/` at the commit this skill
shipped with (`cli/` commit `c834f41d`, backend commit `9f8fe258`, issue #1305) —
re-run the greps in "Verify" if the code has moved.

**Version status: 0.1.0.** 0.0.1 was a name-reservation stub — every subcommand
exited `NOT_IMPLEMENTED`. 0.1.0 fills in exactly three: `login`, `meter`, `verify`.
`backtest` and `verify --local` still exit `NOT_IMPLEMENTED` (3) — see "What's
still not implemented" below. Don't imply otherwise.

## Install

The package is **not published to PyPI yet** — `pip install archimedes-cli` returns "No
matching distribution found" (PyPI 404, verified 2026-08-20). Install from the repo:

```bash
pip install -e ./cli          # from the repo root
```

Python ≥3.10, two dependencies (`click`, `httpx`) —
[`cli/pyproject.toml`](../../cli/pyproject.toml):31-34. No `eth-account`/SIWE
dependency: login is Better Auth email+password, not a wallet signature
(pyproject.toml:23-30 explains why in a comment, not just a diff).

## The three commands that work

### `archimedes login`

[`cli/src/archimedes_cli/cli.py`](../../cli/src/archimedes_cli/cli.py):190-275.

```bash
archimedes login
# Email: you@example.com
# Password: ********
# Logged in as you@example.com. Session cached at /Users/you/.config/archimedes/session.json.

# CI path — no prompt:
ARCHIMEDES_EMAIL=you@example.com ARCHIMEDES_PASSWORD=hunter2 archimedes login --json
# {"ok": true, "email": "you@example.com"}
```

What actually happens, in order (cli.py:187-253):

1. Reads `ARCHIMEDES_EMAIL` / `ARCHIMEDES_PASSWORD` from the environment if both
   are set; otherwise prompts (`click.prompt`, password hidden) — cli.py:187-192.
2. `POST /api/auth/sign-in/email` with `{email, password}` — cli.py:196.
3. Reads the `better-auth.session_token` cookie off the response
   ([`session.py`](../../cli/src/archimedes_cli/session.py):19-22 —
   `SESSION_COOKIE_NAME`, matching the fragment
   `backend/archimedes/api/account_auth.py`'s `_SESSION_COOKIE_FRAGMENT` looks
   for). No cookie → fails as `no_session_cookie`, does not cache anything
   (cli.py:215-223).
4. **Confirms the cookie round-trips** against `GET /api/auth/get-session`
   before trusting it (cli.py:227-246) — mirrors `scripts/agent_journey.py`'s
   `step_auth`. A non-JSON or empty 200 body is treated as "not confirmed," not
   a crash (cli.py:231-235, the `ValueError` branch). No `user.email` in the
   response → fails as `session_not_confirmed` (cli.py:239-246) and, again,
   nothing is cached.
5. Only then: `save_session(...)` writes the session cache — by default
   `~/.config/archimedes/session.json`, or wherever `--session-file` /
   `ARCHIMEDES_SESSION_FILE` points (see "One session file per lane" below) —
   [`session.py`](../../cli/src/archimedes_cli/session.py):218-250, opened via
   `os.open(..., 0o600)` (never briefly world-readable) and `chmod(0o600)` again
   under the write lock, in case the path pre-existed with looser permissions
   (the docstring there explains why the second chmod isn't redundant).

The password is sent once, on that one request, and is never written to disk
(cli.py docstring, lines 180-182). Linking a crypto wallet is a separate, later,
optional step for on-chain vault actions — it is not part of `login` and does
not authenticate you (cli.py:183-185).

### One session file per lane

`login`, `meter`, `verify` and `generate` all take `--session-file PATH`, and all read
`ARCHIMEDES_SESSION_FILE` when the flag is absent (the flag wins;
[`session.py`](../../cli/src/archimedes_cli/session.py):129-142 is the whole precedence
rule — flag, then env var, then `~/.config/archimedes/session.json`).

```bash
ARCHIMEDES_SESSION_FILE=~/.archimedes/lane-a.json archimedes login   # agent A
ARCHIMEDES_SESSION_FILE=~/.archimedes/lane-b.json archimedes login   # agent B
archimedes meter --session-file ~/.archimedes/lane-a.json            # still agent A
```

**If you are driving more than one agent on one machine, give each one its own file.**
A shared file is a shared identity: the second `login` overwrites the first, and the
first lane keeps getting clean `200`s as somebody else. That is
[#1752](https://github.com/aprin-labs/archimedes/issues/1752), observed live on
2026-09-01, and before the flag existed the only lever was a fabricated `$HOME`.

The file is mode `600` wherever you point it, and every read and write takes an advisory
`flock` on it (`session.py`:150-170), so even a shared file never yields a half-written
read. The MCP server (`mcp-server/`) loads this same cache through
`archimedes_cli.session`, so `ARCHIMEDES_SESSION_FILE` in an `mcpServers` env block moves
that agent's credential too.

### `archimedes meter`

cli.py:296-338. Requires a cached session (`archimedes login` first).

```bash
archimedes meter
# Usage for 2026-08-19 (user usr_01H...)
#   Per-user: 3/10 used, 7 remaining
#   Per-IP: 3/20 used, 17 remaining
#   Price per generation: 0.5 USDC (dry run)

archimedes meter --json
# {"ok": true, "date": "2026-08-19", "user_id": "usr_01H...",
#  "user": {"used": 3, "cap": 10, "unlimited": false, "remaining": 7, "error": null},
#  "ip": {...}, "quote": {"price": 0.5, "asset": "USDC", "dry_run": true}}
```

`GET /api/account/usage` (cli.py:298) reads the SAME two Redis buckets
`enforce_generation_quota` enforces, via a new read-only
`GenerationQuota.peek()` — [`backend/archimedes/services/generation_quota.py`](../../backend/archimedes/services/generation_quota.py):170,
added for this route without touching enforcement — plus
`generation_payment.quote()` (`backend/archimedes/services/generation_payment.py`:77),
the identical call the paywall itself reads. The CLI's number and the paywall's
invoice can never drift apart because they're one function call, not two
implementations of the same math
([`backend/archimedes/api/account_usage_routes.py`](../../backend/archimedes/api/account_usage_routes.py):1-14).

**Honesty point:** a Redis outage renders as `"used": null, "error":
"quota_backend_unavailable"` (account_usage_routes.py:55-60,
`DailyCapUsage.error`), which the CLI renders as `unavailable
(quota_backend_unavailable)` (cli.py:262-263) — never a fabricated `0/cap`. This
is the same fail-loud-not-fail-soft convention the repo's `CLAUDE.md` names as
a hard rule for anything a claim depends on.

No session → exits `AUTH` (2) before any network call (cli.py:286-294).
Expired/revoked session → the API 401s, mapped to `AUTH` with
`error: "session_expired"` (cli.py:135-167, `_handle_api_error`).

### `archimedes verify RETURNS_CSV`

cli.py:392-486. Requires a cached session.

```bash
archimedes verify returns.csv
# n_bars=252  trials=1 (self-attested)
#   [PASS] DSR — self-attested trials=1: DSR p-value 0.6123 >= floor 0.50 (Newey-West HAC standard error)
#   [N/A] PBO — PBO (probability of backtest overfitting, ...) requires a trial matrix ...
#   [PASS] OOS consistency — walk-forward OOS Sharpe 0.7331 > floor 0.00 (chronological 70/30 holdout)
#   [N/A] Look-ahead — The look-ahead audit is AST-based static analysis of strategy SOURCE CODE; ...
# PASSES

echo $?   # 0

archimedes verify returns.csv --trials 40 --json
# {"ok": true, "passes": true, "trials": 40, "self_attested": true, "n_bars": 252,
#  "dsr": {"status": "pass", "reason": "...", "deflated_sharpe": ..., "dsr_p_value": ...},
#  "pbo": {"status": "not_evaluable", "reason": "..."},
#  "oos_consistency": {"status": "pass", "reason": "...", "oos_sharpe": ..., "in_sample_sharpe": ...},
#  "look_ahead": {"status": "not_evaluable", "reason": "..."}}
```

`RETURNS_CSV` is two columns (`date,daily_return`); a header row, or any row
whose second column doesn't parse as a float, is skipped automatically
(cli.py:341-364, `_parse_returns_csv`). `-` reads from stdin, so this pipes:

```bash
archimedes backtest --strategy-path mine.py --strategy-class Mine | archimedes verify -
```

(`backtest` itself is not implemented yet — see below — but the pipe shape is
already wired and tested, cli.py:466-493.)

`--trials N` (default 1) is a **self-attested** trial/variant count fed into the
DSR deflation — client-side guarded `>= 1` (cli.py:414-421) with **no HTTP call
attempted** on violation (`test_adversarial_trials_zero_rejected_before_any_network_call`,
[`cli/tests/test_cli.py`](../../cli/tests/test_cli.py):569-579). An empty parse
(e.g. a header-only CSV) is rejected the same way, same guarantee
(cli.py:424-431; `test_adversarial_empty_returns_rejected_before_any_network_call`,
test_cli.py:581-590).

## The honesty rule: why two of the four checks are `not_evaluable`

The rigor gate is four checks. `POST /api/rigor/verify`
([`backend/archimedes/api/rigor_verify_routes.py`](../../backend/archimedes/api/rigor_verify_routes.py):1-41)
takes a **bare returns series** — no strategy code, no trial matrix — and that
input shape can only ever support two of them:

| Check | Status for a bare series | Why | Source |
| --- | --- | --- | --- |
| **DSR** (deflated Sharpe ratio) | evaluable (`pass`/`fail`) | Needs only the series + a declared trial count | `compute_dsr_hac_and_iid`, gated on `DSR_P_FLOOR` — rigor_verify_routes.py:121-149 |
| **Walk-forward OOS consistency** | evaluable (`pass`/`fail`) | A chronological 70/30 holdout only needs the series | `compute_oos_sharpe`, gated on `OOS_ABS_FLOOR` — rigor_verify_routes.py:152-181 |
| **PBO** (probability of backtest overfitting) | always `not_evaluable` | PBO (Bailey et al. 2014 CSCV) is a property of a *selection set* — it measures how much of your winning candidate's edge came from having tried many variants and picking the best. A single series has no sibling candidates to compare against, so there is no selection set to measure overfitting probability against. | rigor_verify_routes.py:24-29, 64-70, 199 |
| **Look-ahead audit** | always `not_evaluable` | It is AST-based static analysis of strategy *source code*. A returns series carries no code — and Archimedes never executes or uploads strategy code server-side (the CLI README's hard boundary), so this endpoint deliberately accepts only numbers. | rigor_verify_routes.py:30-33, 71-76, 200 |

Both are reported with an explicit `status: "not_evaluable"` + `reason` string
(`_PBO_NOT_EVALUABLE_REASON` / `_LOOK_AHEAD_NOT_EVALUABLE_REASON`,
rigor_verify_routes.py:64-76) — never silently passed, never defaulted, never
scored as a `fail` for something that was structurally never run. This mirrors
the CPCV-honesty pattern documented in `skills/verdict-api/SKILL.md` (lands with PR #1260; "CPCV is
honestly reported as `NOT_RUN`, not silently absent") — same principle, applied
here to PBO and look-ahead.

**The pass rule accounts for this:** `passes` is `True` **iff every RUNNABLE
leg — DSR *and* walk-forward OOS — actually ran and passed**: a quorum, not
"no evaluable check failed" (#1481, `rigor_verify_routes.py:803`). A request
where every check comes back `not_evaluable` (a zero-variance series, say —
since #1803 a series too SHORT to grade is refused outright with
`window_too_short` rather than answered) must **not** read as passing by
vacuous truth — it renders 4×`[N/A]`, zero `[PASS]`/`[FAIL]`, and
`FAILS`/exit 1 (`test_not_evaluable_rendering_when_nothing_could_run`,
cli/tests/test_cli.py:663; backend-side:
`test_zero_evaluable_legs_never_read_as_a_pass`,
[`backend/tests/test_rigor_verify_routes.py`](../../backend/tests/test_rigor_verify_routes.py):194).

DSR and OOS reuse the **exact same functions and threshold constants**
(`compute_dsr_hac_and_iid`, `compute_oos_sharpe`, `DSR_P_FLOOR`,
`OOS_ABS_FLOOR` from `services/rigor_profiles.py`) that the strategy-passport
verdict uses — no reimplementation, no new thresholds
(rigor_verify_routes.py:6-9, 51-58). If you need PBO for real, that means
submitting a trial matrix to `POST /api/selection-bias/pbo` instead (the
`_PBO_NOT_EVALUABLE_REASON` string points there directly) — out of scope for
this CLI's `verify`, and out of scope for this skill; see
`skills/verdict-api/SKILL.md` (lands with PR #1260) for that endpoint family.

## Exit codes

[`cli/src/archimedes_cli/exits.py`](../../cli/src/archimedes_cli/exits.py):15-38 —
stable from 0.0.1 onward; this is treated as an API a CI job can branch on.

| Code | Name | Meaning |
| --- | --- | --- |
| 0 | `OK` | Command completed; for `verify`, the gate passed. |
| 1 | `GATE_FAILED` | The gate ran to completion and returned a failing verdict — a real answer about the strategy, not an error. |
| 2 | `USAGE` / `AUTH` | Bad arguments, a missing file, **or** no valid session / a rejected one. `AUTH` is a documented alias of `USAGE` (same value, `2`) — kept as a separate name in code so a no-session exit is legible at the call site, but deliberately not a new number (exits.py:25-31): a missing session means no verdict was produced, exactly like a bad argument, not a real answer the way `GATE_FAILED` is. |
| 3 | `NOT_IMPLEMENTED` | The subcommand exists in the tree but has no implementation this release. |

The split that matters is **1 vs. everything else**. A CI job that treats every
non-zero exit as "strategy rejected" would report a network timeout or an
expired session as a research finding — branch on `1` specifically:

```bash
archimedes verify returns.csv
case $? in
  0) echo "gate passed" ;;
  1) echo "gate failed, not deploying"; exit 1 ;;
  *) echo "verify did not run"; exit 2 ;;
esac
```

## The `--json` contract

Every command honors `--json` on **every** code path, including errors
(cli.py docstring lines 11-12; `_unavailable`, cli.py:80-110; `_fail`,
cli.py:113-121) — a script never has to parse prose to learn what happened.

- **Success:** `{"ok": true, ...command-specific fields...}` — `login`:
  `{ok, email}` (cli.py:251); `meter`: `{ok, **usage}` (cli.py:313); `verify`:
  `{ok, **body}`, where `body` is the full `RigorVerifyResponse` (cli.py:460).
- **A produced-but-failing `verify` verdict is still `ok: true`** — the
  *request* succeeded; the verdict is what failed
  (`test_failing_series_exits_1` asserts `payload["ok"] is True` alongside
  `payload["passes"] is False`, test_cli.py:509).
- **Any error path:** `{"ok": false, "command": ..., "error": <machine-readable slug>, "message": <human string>}`
  (cli.py:96, `_fail`) — e.g. `"invalid_trials"`, `"empty_returns"`,
  `"no_session"`, `"session_expired"`, `"rate_limited"`, `"network_error"`.
- **`NOT_IMPLEMENTED`'s JSON body** additionally carries `"version"` and
  `"lands_in"` (cli.py:76-85) — see the version-string note below.

## What's still NOT implemented

- **`archimedes backtest`** — always exits `NOT_IMPLEMENTED` (3), regardless of
  arguments (cli.py:489-516). Running a strategy file means importing and
  executing arbitrary Python, which is why it's local-only by design, not a
  performance choice — and that engine isn't published yet.
- **`archimedes verify --local`** — same exit, same reason: needs the same
  not-yet-published local execution engine (cli.py:411-412).
- Both report `lands_in: "unscheduled"` in `--json` mode by default
  (cli.py:59, `_unavailable`'s default parameter) rather than guessing a
  version number. This is a **deliberate fix during this same 0.1.0 work**:
  0.0.1 hardcoded `lands_in="0.1.0"`, which became a false, self-contradictory
  claim ("lands in 0.1.0") the instant `0.1.0` became the *running* version —
  exactly the kind of fabricated-future-value claim this repo's conventions
  forbid. Don't reintroduce a hardcoded target version here.

## Not covered by this skill

- The `POST /api/rigor/verify` / `GET /api/account/usage` endpoints as raw HTTP
  surfaces for a *non-CLI* client (curl, another agent) — that's
  `skills/verdict-api/SKILL.md`'s territory (lands with PR #1260; it covers the sibling
  `/api/generate/*` family in that style; a bare-series `/api/rigor/verify`
  entry could be added there or here later, but isn't duplicated in both).
- Reading a full strategy passport's DSR/PBO/OOS fields once a strategy has
  gone through real generation with a trial matrix — see
  `skills/strategy-passport/SKILL.md` (lands with PR #1260). `verify`'s `not_evaluable` PBO is not
  the same code path as a passport's real (evaluable) PBO.
- Wallet linking, on-chain vault actions, or anything payment-related — the
  CLI's `meter` only *reads* the price quote; it never pays.

## `archimedes manifest` — the machine-readable contract

Everything this document says in prose, an agent can get declaratively:
`archimedes manifest` emits one JSON object — every command's inputs
(flags, env vars, defaults including the session-aware `--api-url`
resolution), output shapes, cost class, side effects, exit codes, and
`implemented` status — always exit 0, no network, no session
(`cli/src/archimedes_cli/manifest.py`; command at cli.py:519). The
contract cannot silently drift: `cli/tests/test_manifest.py` walks the real
click command tree and asserts every command, flag, and exit code in the
manifest matches it. Prefer the manifest over parsing `--help` when
integrating programmatically.

## Verify (re-run these before trusting this document)

```bash
# The sibling skills this document points to exist (they land with PR #1260 —
# if this grep fails, that PR has not merged yet and those pointers dangle):
ls skills/verdict-api/SKILL.md skills/strategy-passport/SKILL.md
```


```bash
# The three working commands are still exactly these three:
grep -n "^@main.command" cli/src/archimedes_cli/cli.py

# Exit codes unchanged:
grep -n "^OK\|^GATE_FAILED\|^USAGE\|^AUTH\|^NOT_IMPLEMENTED" cli/src/archimedes_cli/exits.py

# The honesty rule is still enforced exactly this way:
grep -n "not_evaluable" backend/archimedes/api/rigor_verify_routes.py
grep -n "passes = bool(evaluable)" backend/archimedes/api/rigor_verify_routes.py

# Session cookie name still matches the backend's fragment:
grep -n "SESSION_COOKIE_NAME" cli/src/archimedes_cli/session.py
grep -n "_SESSION_COOKIE_FRAGMENT" backend/archimedes/api/account_auth.py

# Session file is still written mode 600:
grep -n "0o600" cli/src/archimedes_cli/session.py

# backtest / verify --local are still NOT_IMPLEMENTED, not silently shipped:
grep -n "_unavailable" cli/src/archimedes_cli/cli.py

# lands_in still defaults honestly rather than hardcoding a version:
grep -n 'lands_in: str = ' cli/src/archimedes_cli/cli.py

# No SIWE/wallet-signature residue in the CLI source (Better Auth replaced it):
grep -rn "SIWE" cli/src/    # expect: no output

# Full test suites this skill's claims are backed by:
cd cli && python -m pytest tests/ -q
cd .. && python -m pytest backend/tests/test_rigor_verify_routes.py backend/tests/test_account_usage_routes.py -q
```
