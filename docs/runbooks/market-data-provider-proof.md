# Market-data provider — wiring the Tiingo token and proving the pull

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-03
> **superseded-by:** —

**Scope:** getting `TIINGO_API_TOKEN` onto the running backend container, and producing the
verified-pull record that [`../claims-ledger.md`](../claims-ledger.md)'s "paid analysis runs
on licensed data" row is waiting on. Written for
[#1798](https://github.com/aprin-labs/archimedes/issues/1798); the split it serves is decided
in [`../adr/market-data-sourcing.md`](../adr/market-data-sourcing.md).

**Read the ADR before the flip step.** This page is the procedure. The question of *which
surface runs on which vendor, and what has to be true before money moves*, is the ADR's, and
the flip at the end of this page is the point where that decision becomes real.

---

## What was wrong, in one paragraph

`/archimedes/prod/TIINGO_API_TOKEN` has existed in SSM (SecureString, `us-east-1`, account
`037613907429`) since 2026-08-31, and the backend task definition carried no `TIINGO_*`
secret at all. It was **not** true that nothing could read it: the value already reached the
web-tier app process by a side door — `backend/archimedes/main.py:47-48` calls
`secrets_service.load_ssm_secrets()` whenever `PUBLIC_DOMAIN` is set, and that
`GetParametersByPath`es the whole `/archimedes/prod/` prefix into `os.environ`. But that
loader catches every error and boots degraded by design, so a credential arriving only
through it is a **soft** dependency: nothing declares it, nothing verifies it, and its
absence produces a log line rather than a failed deploy. What this change does is convert it
into a task-launch dependency, which is the thing step 1 below can actually observe.
`market_data_provider` refuses to fall back to yfinance when the token is missing — it raises
`TiingoAPIKeyMissingError` at provider construction, deliberately, so that a run can never
carry a "licensed data" provenance it did not earn.

## The two paths, and which one actually ships

There are two registrars for the backend task definition, and
[#1799](https://github.com/aprin-labs/archimedes/issues/1799) is the issue that says so:

| Path | Who registers | When it takes effect |
|---|---|---|
| `infra/ecs.tf` → `aws_ecs_task_definition.backend` | `terraform apply` | only when someone applies |
| `deploy.yml` → `.github/scripts/ecs_rewrite_task_def.py` | every CI deploy of `main` | on the next deploy |

The secret is declared on **both**. That is not belt-and-braces: `aws_ecs_service.backend`
carries `lifecycle { ignore_changes = [task_definition, desired_count] }`, so a terraform
apply registers a new revision but does **not** change which revision the service runs. The
deploy is the path that puts the token in front of the process.

**So there is no risky apply to schedule.** Merging and letting `main` deploy is sufficient.
The terraform entry is the declared baseline, so that whenever the task-def resource is next
applied it does not silently un-wire what the pipeline pinned.

## Step 1 — deploy, then confirm the secret is on the live revision

After the merge commit deploys:

```bash
aws ecs describe-services --cluster archimedes-cluster --services archimedes-backend \
  --query 'services[0].taskDefinition' --output text
```

Take the revision that returns and read its backend container's secrets:

```bash
aws ecs describe-task-definition --task-definition <that-arn> \
  --query "taskDefinition.containerDefinitions[?name=='backend'].secrets[].name" --output text
```

Expected: `TIINGO_API_TOKEN` in the list. (The rest of the list is whatever the clone chain
carries, not what `infra/ecs.tf` declares — the deploy clones the live revision. It will only
be the full eight after a `terraform apply` has also landed.) If `TIINGO_API_TOKEN` is absent,
the deploy did not run the rewrite script — check the `deploy-ecs` job's "Register a new
task-definition revision" step, not this page.

No IAM change is needed and none was made. The execution role's SSM statement is a prefix
wildcard over `parameter/archimedes/prod/*`, which already authorises the read, and its
`kms:Decrypt` statement already covers SecureStrings fetched via SSM. That property is pinned
by `test_execution_role_policy_is_a_prefix_wildcard` in
[`../../backend/tests/test_ecs_backend_secrets.py`](../../backend/tests/test_ecs_backend_secrets.py).

Then confirm the running *container's environment* carries it, not just the definition — without
ever printing the value. Note what this proves: the exec shell is a sibling process reading the
container env, not the app process's `os.environ`, so before this change it would have printed
`MISSING` while the app process held the token via the bulk load described above. `MISSING` here
means the `secrets` entry has not shipped yet — not that the credential is unavailable to the app:

```bash
aws ecs execute-command --cluster archimedes-cluster \
  --task <task-id> --container backend --interactive \
  --command 'sh -c "test -n \"$TIINGO_API_TOKEN\" && echo present:${#TIINGO_API_TOKEN} || echo MISSING"'
```

## Step 2 — the proof pull

[`../../scripts/verify_market_data.py`](../../scripts/verify_market_data.py) pulls `SPY` for a
fixed, fully-historical window — `2026-06-01` through `2026-06-12` inclusive, ten US trading
days with no market holiday in it — through the same `get_provider(seam="daily")` seam every
backtest uses, and reports the seam it asked and the vendor that answered.

**It does not run inside the backend container.** `backend/Dockerfile` COPYs `backend` into
`/app`, so `/app/scripts` is `backend/scripts`; the repo-root `scripts/` tree is not in the
image. That is why step 1 above checks the container's env separately. Run the pull from a
checkout instead, with the token in the process environment and `--no-cache` (which skips the
`asset_daily_bars` wrapper and so needs no database):

```bash
export TIINGO_API_TOKEN=$(aws ssm get-parameter \
  --name /archimedes/prod/TIINGO_API_TOKEN --with-decryption \
  --query Parameter.Value --output text)
MARKET_DATA_DAILY_PROVIDER=tiingo python scripts/verify_market_data.py --no-cache
```

`MARKET_DATA_DAILY_PROVIDER` rather than `MARKET_DATA_PROVIDER` since
[#1798](https://github.com/aprin-labs/archimedes/issues/1798): the daily variable is the one
this pull is about, and it is the one the cutover flips. The older name still moves this seam
when the daily one is unset, so the previous form of this command was not wrong — it was just
wider than the thing being rehearsed. See § Step 3.

The output has this shape — the block below is the format, **not** a recorded run:

```
provider     : tiingo
seam         : daily
symbol       : SPY
window       : 2026-06-01 .. 2026-06-13 (end-exclusive)
cache        : bypassed (--no-cache)
rows         : 10
first bar    : 2026-06-01
last bar     : 2026-06-12
result       : OK — tiingo served the known 10-bar window
```

Exit `0` on that; exit `1` on anything else (wrong count, wrong dates, no bars, or a provider
that raised — which is the shape a missing token takes). Dropping `--no-cache` reads through
the production cache wrapper instead and needs a database; the cache is per-vendor, so the
`provider` line is honest either way, but a warm cache means a green run does not by itself
prove the vendor is reachable right now.

**Paste your real output on [#1798](https://github.com/aprin-labs/archimedes/issues/1798)** —
with the `provider` line as it actually printed. That is the verified-pull record; the
claims-ledger row stays `PENDING ADR MERGE` until a run of this shows `provider : tiingo`
*and* the deployed seam is actually reading it.

## Step 3 — the flip, which is an owner decision with a blast radius

Neither market-data variable is set by this wiring, and a guard in
`test_ecs_backend_secrets.py` (`FORBIDDEN_WHY`, which `FORBIDDEN_NAMES` derives from) fails
if either is added to `infra/ecs.tf` without editing that guard first. That is deliberate, and
the reason is a real hazard rather than caution:

> **`MARKET_DATA_DAILY_PROVIDER` is the cutover switch.** Setting it to `tiingo` moves the
> daily-bar readers — strategy signal evaluation on the marketplace tick, the generation
> fusion panel, the portfolio backtester — and nothing else. Its blast radius is a cold
> `asset_daily_bars` cache: reads filter on the vendor that wrote each row, so the first run
> after a flip in *either* direction refetches. That is correct behaviour, not a fault.
>
> **`MARKET_DATA_PROVIDER` is the wider one, and setting it here is the mistake to avoid.**
> Since [#1798](https://github.com/aprin-labs/archimedes/issues/1798) it names the
> INTRADAY/history seam — the oracle push's provider leg, the paper-marks loop, the VIX/S&P
> regime reads, the Explore history modal — **and** is the daily seam's fallback when the
> daily variable is unset. So it flips daily bars too, by the back door and without naming
> them. It no longer *breaks* intraday the way this page warned before #1798: a vendor that
> cannot serve a seam is substituted for that seam and the substitution is logged by name
> (`docs/adr/market-data-sourcing.md` § "Amendment: per-seam routing"), so the oracle keeps
> pricing from yfinance rather than raising `NotImplementedError` out of an adapter. That is
> exactly why it needs a guard now — the breakage used to be its own alarm.

Step 2's command is exactly that rehearsal: `MARKET_DATA_DAILY_PROVIDER=tiingo` set for one
short-lived process, which exercises the token, the adapter and the vendor's real responses
and touches nothing else — the process exits. A green run there is evidence the credential
works and the adapter is correct against live vendor responses.

Be clear about what that green run does *not* unblock. The credential was never what stood
between here and the flip — it was already in the web tier's process environment via the bulk
load described at the top of this page, so the daily-bar path would have answered before this
change too. What *did* block the flip was the intraday surfaces' `NotImplementedError`s,
because one variable moved every seam at once. **#1798 removed that blocker**, by giving each
seam its own variable. This page said, before that landed, that "there is no way to flip only
the paid seam with the variables that exist" and that the flip "needs Tiingo's intraday
methods implemented first, or a second variable so the two seams can diverge" — the second
variable now exists, and those two sentences described the world before it. What remains is an
owner decision about provenance and cost, plus the ADR's mainnet gate, which is a commercial
licence rather than anything a fetch can prove.

## Rollback

Nothing to roll back in the *runtime* behaviour: nothing reads `TIINGO_API_TOKEN` while both
`MARKET_DATA_DAILY_PROVIDER` and `MARKET_DATA_PROVIDER` are unset, so no code path changes. If
a flip is in effect and backtests or the marketplace tick start failing, unset whichever
variable you set (or set it to `yfinance`) and redeploy. Note the ADR's cache consequence: **a
provider flip in either direction starts cold**, because `asset_daily_bars` reads filter on
the vendor that wrote each row. The first run after a flip is slow, and that is correct
behaviour, not a fault.

One thing this wiring does change: `/archimedes/prod/TIINGO_API_TOKEN` is now a task-LAUNCH
dependency. Deleting, renaming or moving that parameter fails every task start with
`ResourceInitializationError` — the whole service, not one feature. Before it was a soft
dependency reaching the process only via `main.py`'s best-effort bulk load. Rotating the
value in place is safe; removing the parameter is not.

## Related

- [`../adr/market-data-sourcing.md`](../adr/market-data-sourcing.md) — the split, the
  free-tier-is-for-testing position, and the Tiingo Business plan as a mainnet gate. Its
  § "Amendment: per-seam routing" is the per-feature seam table (#1798), including why the
  S&P moving averages sit on `intraday` despite being daily bars.
- [`../claims-ledger.md`](../claims-ledger.md) — the row this proof is for.
- [`../operations/feature-flag-fliplist.md`](../operations/feature-flag-fliplist.md) —
  `MARKET_DATA_DAILY_PROVIDER`, `MARKET_DATA_PROVIDER`, `ORACLE_CRYPTO_SOURCE` and
  `TIINGO_MIN_REQUEST_INTERVAL_S`.
- [#1798 comment correcting the issue's premise](https://github.com/aprin-labs/archimedes/issues/1798#issuecomment-5504420216)
  — why "nothing could read the token" was wrong, and what actually blocks the flip.
