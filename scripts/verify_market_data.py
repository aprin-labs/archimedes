#!/usr/bin/env python3
"""Pull one known window of daily bars through the DAILY market-data seam (#1798).

**What this is for.** `docs/claims-ledger.md` carries the row "paid analysis
runs on licensed data". Until something *pulls* bars and says which vendor
answered, that row is a statement of intent. This script is the verified-pull
record the ledger is missing: it asks the live seam
(`services.market_data_provider.get_provider(seam="daily")` — the same object
every backtest and marketplace tick uses, not a private copy) for a fixed
symbol over a fixed, fully-historical window, and prints the seam, the vendor
name, the row count, and the first and last bar dates. Paste that output on the
issue.

**Which seam, and why it is named here.** Since #1798 there is no single
"active provider": `daily` (bars — this script's subject, and the licensed-data
cutover) and `intraday` (the live oracle push, paper marks, Explore history)
resolve their vendors independently, so a call that did not name its seam would
be picking one by accident. This script proves the DAILY seam and says so on
its own first output line. It proves nothing about intraday — which is the
point of the split: the two no longer move together.

**Why a FIXED window rather than "the last N days".** A relative window makes
every run's expected answer a fresh calculation, so nothing can be asserted and
the operator is left eyeballing plausible-looking numbers. `2026-06-01` through
`2026-06-12` inclusive is ten US trading days with no market holiday in it
(Memorial Day is 2026-05-25, Juneteenth is 2026-06-19), so the correct answer
is a constant: ten rows, first bar 2026-06-01, last bar 2026-06-12. A vendor
that returns nine, or starts on the 2nd, is wrong in a way this script can
*state* rather than leave to the reader.

**What a green run proves, and what it does not.** It proves the named vendor
answered with the right bars for that window, in this process, with whatever
credentials this process actually has. It does not prove the vendor is licensed
for commercial use — that is a contract, not a fetch — and it does not prove
any other surface reads the same seam. Read
`docs/adr/market-data-sourcing.md` for the licensing position.

**Provenance and the cache.** The read goes through `get_provider()`, which is
cache-wrapped exactly as production is. That is deliberate: the
`asset_daily_bars` cache is PER-VENDOR (its reads filter on the `source`
column), so a cached row can only be served to the vendor that wrote it, and
the vendor printed below is therefore honest about the bars printed below
whether they came from Postgres or from the wire. It does mean a green run on a
warm cache is not by itself proof that the vendor is reachable *right now* —
`--no-cache` bypasses the wrapper for that.

**Where to run it.** From a checkout, with the credential in the process
environment. The default read goes through the cache wrapper and therefore
needs a database; `--no-cache` does not, which is what makes a laptop run
possible with nothing but the token:

    # in-repo, against whatever this shell's env selects
    python scripts/verify_market_data.py --no-cache

    # the Tiingo rehearsal — token from SSM, one process, nothing else touched.
    # MARKET_DATA_DAILY_PROVIDER is the cutover switch (#1798); the older
    # MARKET_DATA_PROVIDER still moves this seam when the daily one is unset,
    # but it names the INTRADAY seam too, so prefer the specific variable here.
    export TIINGO_API_TOKEN=$(aws ssm get-parameter \
      --name /archimedes/prod/TIINGO_API_TOKEN --with-decryption \
      --query Parameter.Value --output text)
    MARKET_DATA_DAILY_PROVIDER=tiingo python scripts/verify_market_data.py --no-cache

Note it does NOT run inside the backend container today: `backend/Dockerfile`
COPYs `backend` into `/app`, so `/app/scripts` is `backend/scripts` and this
repo-root tree is not in the image. Confirming the container itself carries the
secret is a separate step — see
`docs/runbooks/market-data-provider-proof.md`.

Exit codes:  0 the active provider returned the expected bars
             1 anything else (provider raised, wrong count, wrong dates)
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from archimedes.services import market_data_provider as mdp  # noqa: E402

#: The known window. Ten US trading days, no holiday. `get_daily_ohlcv` takes
#: `[start, end)` — see its docstring — so END is the day AFTER the last bar.
DEFAULT_SYMBOL = "SPY"
DEFAULT_START = "2026-06-01"
DEFAULT_END = "2026-06-13"
EXPECTED_ROWS = 10
EXPECTED_FIRST_BAR = "2026-06-01"
EXPECTED_LAST_BAR = "2026-06-12"

#: Daily bars are what this script proves, so it asks the daily seam by name.
SEAM = mdp.DAILY_SEAM


def _provider(*, no_cache: bool):
    """The daily seam's provider — cache-wrapped like production, or the bare
    vendor underneath it.

    `--no-cache` reaches past every wrapper to the vendor adapter, so the pull
    is unambiguously a live vendor call. It unwraps ALL the way rather than one
    layer, because since #1798 `get_provider(seam=...)` nests two deep —
    `SeamRoutedProvider` → `CachingMarketDataProvider` → the vendor — and
    stopping at the first `_inner` would hand back the cache while this script
    printed "bypassed". It unwraps the object `get_provider()` returned rather
    than re-selecting a vendor from the env, so the two modes can never
    disagree about WHICH vendor is active — only about whether the cache was
    allowed to answer.
    """
    provider = mdp.get_provider(seam=SEAM)
    if not no_cache:
        return provider
    unwrapped = provider
    depth = 0
    while (inner := getattr(unwrapped, "_inner", None)) is not None:
        unwrapped = inner
        depth += 1
    if depth == 0:
        raise RuntimeError(
            "--no-cache: get_provider() no longer returns a cache wrapper with an "
            "inner provider; drop the flag or update this script"
        )
    return unwrapped


def verify(*, symbol: str, start: str, end: str, no_cache: bool = False, out=sys.stdout) -> int:
    """Fetch, report, and return the process exit code."""
    vendor = mdp.provider_name(SEAM)
    known_window = (symbol, start, end) == (DEFAULT_SYMBOL, DEFAULT_START, DEFAULT_END)

    print(f"provider     : {vendor}", file=out)
    print(f"seam         : {SEAM}", file=out)
    print(f"symbol       : {symbol}", file=out)
    print(f"window       : {start} .. {end} (end-exclusive){'' if known_window else '  [custom]'}", file=out)
    print(f"cache        : {'bypassed (--no-cache)' if no_cache else 'through the production wrapper'}", file=out)

    try:
        frame = _provider(no_cache=no_cache).get_daily_ohlcv(symbol, start, end)
    except Exception as exc:  # the reason IS the result here — report it, do not re-raise
        print(f"result       : FAILED — {type(exc).__name__}: {exc}", file=out)
        traceback.print_exc(file=out)
        return 1

    if frame is None or frame.empty:
        print("result       : FAILED — the provider returned no bars", file=out)
        return 1

    rows = len(frame)
    first_bar = str(frame.index[0].date())
    last_bar = str(frame.index[-1].date())
    print(f"rows         : {rows}", file=out)
    print(f"first bar    : {first_bar}", file=out)
    print(f"last bar     : {last_bar}", file=out)

    if not known_window:
        # Nothing to compare against: a custom window's correct row count is
        # the operator's to know. Report and stop short of a verdict we cannot
        # justify — a green tick on an unchecked number is worse than none.
        print("result       : OK (custom window — row count NOT checked against a known answer)", file=out)
        return 0

    problems: list[str] = []
    if rows != EXPECTED_ROWS:
        problems.append(f"expected {EXPECTED_ROWS} rows, got {rows}")
    if first_bar != EXPECTED_FIRST_BAR:
        problems.append(f"expected first bar {EXPECTED_FIRST_BAR}, got {first_bar}")
    if last_bar != EXPECTED_LAST_BAR:
        problems.append(f"expected last bar {EXPECTED_LAST_BAR}, got {last_bar}")

    if problems:
        print(f"result       : FAILED — {'; '.join(problems)}", file=out)
        return 1

    print(f"result       : OK — {vendor} served the known {EXPECTED_ROWS}-bar window", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--start", default=DEFAULT_START, help="ISO date, inclusive")
    parser.add_argument("--end", default=DEFAULT_END, help="ISO date, EXCLUSIVE")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the asset_daily_bars cache wrapper and hit the vendor directly",
    )
    args = parser.parse_args(argv)
    return verify(symbol=args.symbol, start=args.start, end=args.end, no_cache=args.no_cache)


if __name__ == "__main__":
    raise SystemExit(main())
