"""Exit codes.

These are an API. Someone will write ``archimedes verify returns.csv`` into a CI
job and branch on the result, so the meaning of each number has to stay fixed
from 0.0.1 onward. New conditions get new codes; existing codes do not get
redefined.

The split that matters is 1 vs everything else. Exit 1 means the command ran to
completion and the answer was "this does not pass" — a real verdict about the
strategy. Any other non-zero code means the command did not produce a verdict at
all, so a CI job that treats every non-zero as "strategy rejected" would be
reporting a network timeout as a research finding.
"""

OK = 0
"""The command did what it was asked. For ``verify``, the gate passed."""

GATE_FAILED = 1
"""The gate ran and returned a failing verdict. This is a real answer, not an error."""

USAGE = 2
"""Bad arguments or a missing file. Click exits with this on its own; it is named here
so the full set is documented in one place."""

AUTH = 2
"""No valid cached session (run ``archimedes login`` first), or the server rejected it.
Deliberately the SAME value as ``USAGE``, not a new code: a missing/expired session means
the command could not run to a verdict at all, exactly like a bad argument — it is not a
real answer about a strategy the way ``GATE_FAILED`` is. Kept as a separate name (rather
than spelling ``USAGE`` at every auth call site) purely so the reason is legible in the
code that raises it."""

INCOMPLETE = 4
"""``verify`` reached the server and got an answer, but not every runnable leg of
the gate could be evaluated — a leg that could not run on the NUMBERS, such as a
zero-variance stretch with no Sharpe to compute.

Not a short series: since #1803 the endpoint refuses anything under the 250-bar
evaluation window, which sits above the ~70 bars the walk-forward split needs.
That is a rejected body — ``USAGE`` (2) with ``window_too_short`` — and it never
reaches this code.

A new code rather than ``GATE_FAILED`` on purpose, per this module's own split:
an incomplete evaluation is not a verdict about the strategy, so collapsing it
into 1 would report "a leg could not run" as "strategy rejected" — precisely the
confusion the header warns about. See #1481."""

NOT_IMPLEMENTED = 3
"""The subcommand exists in the command tree but has no implementation in this
release. 0.0.1 returned this for every subcommand; 0.1.0 narrows it to ``backtest``
and ``verify --local``, which still need the not-yet-published local execution engine."""

PAYMENT_REQUIRED = 5
"""``generate`` reached the server and the server answered ``402``: this account
must pay before a generation runs.

New in 0.2.0, and a NEW number rather than a reuse, per this module's own rule.
The scripted action is unique to it — open a browser, pay, re-run — and it must
not be confused with either ``USAGE`` (nothing was wrong with the request) or
``GATE_FAILED`` (no strategy was evaluated at all). The CLI prints the x402
requirements the server sent and stops; it never signs anything."""

ACCOUNT_ACTION_REQUIRED = 6
"""``generate`` was refused ``409``: the blocker is account state, not payment
and not the request.

Kept separate from ``PAYMENT_REQUIRED`` because the fix is different and telling
a user to pay when the actual unlock is "verify your email" would be a false
claim. Which unlock applies is whatever the server says — the free-generation
policy is changing (#1658 and the owner's D1 decision), so the CLI reports the
server's own reason rather than a policy it assumes."""

JOB_FAILED = 7
"""The generation job reached a terminal state that is not ``done`` — the server
asserts it failed, stalled, timed out, or was cancelled.

This IS a real answer about the run (compare ``GATE_FAILED``'s role for
``verify``), which is why it is not folded into the "command did not complete"
family. Retrying the same brief may or may not help; the server's own message
says which."""

STILL_RUNNING = 8
"""``generate`` stopped waiting before the job reached any terminal state.

Deliberately NOT ``JOB_FAILED``: the job is still running server-side and may
still succeed. Reporting a client-side wait budget as a failed generation would
be exactly the false claim this repo's #1 rule forbids. The job id is printed so
the run can be picked up again."""

__all__ = [
    "ACCOUNT_ACTION_REQUIRED",
    "AUTH",
    "GATE_FAILED",
    "INCOMPLETE",
    "JOB_FAILED",
    "NOT_IMPLEMENTED",
    "OK",
    "PAYMENT_REQUIRED",
    "STILL_RUNNING",
    "USAGE",
]
