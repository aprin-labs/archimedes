# archimedes-cli

Command-line access to the Archimedes rigor gate.

A backtest with a Sharpe of 2.1 tells you very little on its own. If it was the best of
four hundred variants you tried, most of that number is selection, not skill. The gate
here is the standard correction for that: a deflated Sharpe ratio that prices in how many
variants were tried, a probability of backtest overfitting, a walk-forward out-of-sample
pass, and a look-ahead audit. Point it at a returns series and it tells you which of those
four the series survives.

## Status

**0.2.0** — `login`, `meter`, `verify`, and `generate` work against the hosted API.
`generate` is the new one: a research brief in, a rigor-gated strategy and its passport
URL out, without leaving the terminal and without a wallet. `verify --local` and
`backtest` still exit 3 (`NOT_IMPLEMENTED`); both need the local execution engine, which
is published separately and isn't out yet.

0.1.0 made `login`, `meter`, and `verify` real. 0.0.1 was a name reservation: the command
tree and flags were fixed, but every subcommand exited 3.

## Install

**Not on PyPI yet** — `pip install archimedes-cli` does not resolve (verified 2026-08-31:
`https://pypi.org/pypi/archimedes-cli/json` → HTTP 404). Install it from this repo:

```bash
pip install -e ./cli          # from the repo root
```

Python 3.10 or newer. Two dependencies, both small, so this is a seconds-long install
rather than a compiler-and-a-coffee one. Once the package is published, `pip install
archimedes-cli` becomes the one-liner and this section gets shorter.

## Commands

```
archimedes login                      sign in (Better Auth email + password) and cache the session
archimedes meter                      show today's generation usage and the live price
archimedes generate "BRIEF"           generate a rigor-gated strategy from a research brief
archimedes verify RETURNS_CSV         run the gate over a returns series
archimedes backtest --strategy-path   run a strategy locally and print its returns
archimedes manifest                   emit this tool's machine-readable contract
```

`login` prompts for email and password (or reads `ARCHIMEDES_EMAIL` / `ARCHIMEDES_PASSWORD`
for CI) and caches the session cookie at `~/.config/archimedes/session.json`, mode 600 —
or wherever `--session-file` / `ARCHIMEDES_SESSION_FILE` points, see below. `meter`,
`generate`, and `verify` read that cache; run `login` first. `generate` also accepts an
`ARCHIMEDES_API_KEY` environment variable, which it sends as an `Authorization: Bearer`
header for non-interactive use.

## One session file per lane

The session cache is a path, not a fixed location. `--session-file PATH` on `login`,
`meter`, `verify` and `generate` — or `ARCHIMEDES_SESSION_FILE` in the environment, which
the flag overrides — decides which file that invocation reads and writes:

```bash
# Two agents, one runner, two identities.
ARCHIMEDES_SESSION_FILE=~/.archimedes/lane-a.json archimedes login   # as agent A
ARCHIMEDES_SESSION_FILE=~/.archimedes/lane-b.json archimedes login   # as agent B
ARCHIMEDES_SESSION_FILE=~/.archimedes/lane-a.json archimedes meter   # still agent A

archimedes meter --session-file ~/.archimedes/lane-b.json            # or per command
```

Give every concurrent lane its own file. Sharing one file means sharing one identity: the
second `login` overwrites the first, and the first lane carries on getting clean `200`s as
somebody else — which is exactly what happened on 2026-09-01, when `$HOME` was the only
lever there was ([#1752](https://github.com/aprin-labs/archimedes/issues/1752)).

Every file the CLI writes here is mode 600 wherever you point it, and reads and writes take
an advisory `flock` on it, so a lane that *does* share a file with another still never reads
a half-written one. The same variable moves the MCP server's credential — it loads this
cache rather than reimplementing it — so a per-agent `ARCHIMEDES_SESSION_FILE` in an
`mcpServers` env block isolates that agent too.

## Generate

```bash
archimedes generate "momentum on liquid US equities, monthly rebalance"
```

It quotes the price, starts the job, streams the pipeline's progress as it happens, and
prints the strategy id and the URL of its passport when the run lands:

```
Price: $2.000000 USDC
Job job-7f3a accepted.
  brief_validated      brief accepted
  candidates_selected  stage=debate
  candidate_evaluated  candidate_id=c1 strategy_name=Cross-sectional Momentum
  best_selected        candidate_id=c1
  done                 served_model=amazon.nova-micro-v1:0
Done. strategy_id=strat-91c4
Passport: https://archimedes-arc.com/app/strategy/strat-91c4
```

The brief can come from a file or a pipe instead: `--brief-file brief.txt`, or
`--brief-file -` to read stdin. `--risk-appetite`, `--name`, `--n-candidates`, and
`--model` map one-to-one onto the fields the API's brief schema accepts.

Progress arrives over Server-Sent Events. The server caps a single stream connection at
five minutes and proxies cut them sooner, so when the stream ends without a verdict the
command falls back to polling the job endpoint. Either way the job record is what the
final answer is read from, so a dropped connection changes nothing about the result.

`--timeout` is a wait budget, not a cancel. When it runs out the job is still running on
the server and the command says so, exiting 8 rather than pretending the run failed.

### It never holds a key

Generation may sit behind a paywall, and that boundary is drawn deliberately:

**This CLI holds no private key and signs nothing.** When the server answers `402` it
prints the x402 payment requirements exactly as the server sent them, plus a URL to pay
in a browser, and exits 5. Pay there and re-run. If your account's unlock is a verified
email rather than a payment, the server says so and the message names verification
instead — the command reads the server's reason rather than assuming a policy, which is
what keeps it correct while the free-tier rules are still being decided.

A brief the validator rejects comes back as `422` before any payment gate is reached, so
that path says nothing was charged — and for any `422` whose shape doesn't prove that
ordering, it makes no claim about charges at all.

When a run fails, the CLI reports the server's own terminal state and points at
`GET /api/generate/credits`. It does not tell you a credit was restored, because nothing
in the response says so, and a refund is not something to promise on inference.

`RETURNS_CSV` is two columns, date and daily return, or `-` to read from stdin. So the
whole loop is one line:

```bash
archimedes backtest --strategy-path mine.py --strategy-class Mine | archimedes verify -
```

`verify` sends the returns series to `POST /api/rigor/verify`, which computes DSR and a
walk-forward out-of-sample check for real against those numbers, using the exact functions
and thresholds the strategy-passport verdict uses. PBO and the look-ahead audit can't run
over a bare series (PBO needs a trial matrix, look-ahead needs strategy source), so both
always render `not_evaluable` — never a silent pass.

Every command takes `--json` and prints a single object on stdout, including on its error
paths. A script never has to parse prose to find out what happened.

`verify --local` runs the gate on your machine with no network, no account, and no charge
— not implemented yet.

## Exit codes

Stable from 0.0.1 onward. New conditions get new numbers; an existing number never changes
meaning.

| Code | Meaning |
| --- | --- |
| 0 | The command completed; for `verify`, the gate passed |
| 1 | The gate ran and returned a failing verdict |
| 2 | Bad arguments, a missing file, or no valid session (`login` first) |
| 3 | Subcommand not implemented in this release |
| 4 | `verify` got an answer but not every runnable leg could be evaluated — not a verdict |
| 5 | `generate` hit the paywall; the x402 requirements are printed, pay in a browser and re-run |
| 6 | `generate` needs an account action — verify your email, or connect a wallet |
| 7 | The generation job reached a terminal state that is not `done` |
| 8 | `generate` stopped waiting; the job is **not** cancelled and may still finish |

The line worth drawing is 1 against everything else. Exit 1 is a real answer about the
strategy. Any other non-zero code means no verdict was produced at all, so a CI job that
treats every failure as "strategy rejected" would report a network timeout — or an expired
session — as a research finding. Branch on 1 specifically:

```bash
archimedes verify returns.csv
case $? in
  0) echo "gate passed" ;;
  1) echo "gate failed, not deploying"; exit 1 ;;
  *) echo "verify did not run"; exit 2 ;;
esac
```

`generate`'s codes are each a different next action, which is why they are separate
numbers rather than one generic failure:

```bash
archimedes generate "$BRIEF" --json > out.json
case $? in
  0) jq -r .passport_url out.json ;;
  5) echo "pay at $(jq -r .pay_url out.json), then re-run" ;;
  6) echo "account action needed: $(jq -r .message out.json)" ;;
  7) echo "the run failed: $(jq -r .message out.json)" ;;
  8) echo "still running as $(jq -r .job_id out.json) — check back" ;;
  *) echo "generate did not start" ;;
esac
```

## Your code stays on your machine

Archimedes never executes strategy code it received over the wire, and `backtest` is local
for that reason rather than as a performance choice.

Producing returns from a strategy file means importing and running it. Any server that
offered to do that for you would be running arbitrary code from strangers. So the split is
drawn at the file boundary: `backtest` runs your Python on your machine and emits a returns
series, and only that series is ever sent anywhere.

The two places the hosted service touches code at all are read-only. The look-ahead audit
parses the file to an AST and inspects it. Strategy constants are read with `ast.parse` and
`literal_eval`. Neither one imports the module or evaluates an expression.

If you would rather nothing leave your machine at all, `verify --local` closes that loop
too.

## Links

- [Repository](https://github.com/aprin-labs/archimedes)
- [Issues](https://github.com/aprin-labs/archimedes/issues)
- [archimedes-arc.com](https://archimedes-arc.com)

Released into the public domain under the [Unlicense](https://unlicense.org).
