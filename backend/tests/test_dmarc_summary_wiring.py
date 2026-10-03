"""The weekly DMARC summary is a chain, and every link fails silently (#1504).

    aws_scheduler_schedule.dmarc_weekly_summary   (Monday 13:00 UTC)
                     │
                     ▼
    aws_ecs_task_definition.dmarc_weekly_summary  (backend image, one container)
                     │  DMARC_REPORTS_BUCKET, DMARC_SUMMARY_TO, EMAIL_SENDER
                     ▼
    python -m archimedes.scripts.dmarc_weekly_summary
                     │
          ┌──────────┴───────────┐
          ▼                      ▼
    s3:GetObject / ListBucket   ses:SendEmail on identity/archimedes-arc.com
    (this file's grant)         (infra/ecs.tf's ecs_task_ses_send — NOT here)
                     │
                     ▼
              var.owner_alert_email

Every link fails without a sound. A schedule pointed at the wrong family runs
the wrong command and reports success. A `DMARC_SUMMARY_TO` that is a literal,
or missing, sends the summary to the wrong person or exits 2 into a log group
nobody tails. A bucket name that is a literal lists an empty prefix, and the
job mails "no reports received" — the exact false all-clear this issue exists
to end. And a schedule that quietly became `rate(1 hour)` or `rate(30 days)`
turns a signal the owner reads into noise he mutes, or a fortnight of evidence
he finds too late.

`terraform validate` catches none of it: a task definition nobody schedules, a
schedule naming an env var nothing reads, and a cron that fires every hour are
all syntactically valid.

These tests read the repo's own files as text. Hermetic — no AWS, no terraform
binary, no network, no env vars. Same shape and same reasoning as
`backend/tests/test_ses_event_wiring.py`, which guards the sibling loop.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DMARC_TF = REPO_ROOT / "infra" / "dmarc_reports.tf"
ECS_TF = REPO_ROOT / "infra" / "ecs.tf"
VARIABLES_TF = REPO_ROOT / "infra" / "variables.tf"
OUTPUTS_TF = REPO_ROOT / "infra" / "outputs.tf"

#: The env var names are the contract between terraform and
#: `archimedes.scripts.dmarc_weekly_summary`.
BUCKET_ENV = "DMARC_REPORTS_BUCKET"
RECIPIENT_ENV = "DMARC_SUMMARY_TO"
SENDER_ENV = "EMAIL_SENDER"

#: The terraform expression `DMARC_SUMMARY_TO` must be. Naming the variable
#: rather than repeating an address is what keeps "where the summary goes" and
#: "where alarms go" one decision — #1818 P5's whole finding was that mail sent
#: to the wrong one of those is mail the owner does not read.
RECIPIENT_REF = "var.owner_alert_email"

SCHEDULE_VAR = "dmarc_summary_schedule_expression"


def _read(path: Path) -> str:
    assert path.exists(), f"{path} is missing — nothing reads the DMARC reports bucket without it"
    return path.read_text(encoding="utf-8")


def _strip_comments(hcl: str) -> str:
    """Drop `#` comment lines so a mention in prose cannot satisfy a guard."""
    return "\n".join(line for line in hcl.splitlines() if not line.lstrip().startswith("#"))


def _resource(hcl: str, kind: str, name: str) -> str:
    """The body of one `resource "<kind>" "<name>"` block."""
    marker = f'resource "{kind}" "{name}"'
    parts = hcl.split(marker, 1)
    assert len(parts) == 2, f"{marker} is missing from infra/dmarc_reports.tf"
    return parts[1].split("\nresource ", 1)[0]


# ─────────────────── something actually runs the summary ────────────────


def test_a_schedule_invokes_the_summary_task():
    """Without this, the parser, the bucket and the mailer are all built and inert.

    That is the same product state #1504's owner comment opened against — "the
    issue is not done without it" — only with more terraform in it.
    """
    tf = _strip_comments(_read(DMARC_TF))
    body = _resource(tf, "aws_scheduler_schedule", "dmarc_weekly_summary")
    assert (
        f"schedule_expression          = var.{SCHEDULE_VAR}" in body
        or f"schedule_expression = var.{SCHEDULE_VAR}" in body
    ), "the cadence must be a variable so the owner can change it without a code change"
    assert 'state                        = "ENABLED"' in body or 'state = "ENABLED"' in body, (
        "a DISABLED schedule is indistinguishable from no schedule at all"
    )
    assert "aws_ecs_task_definition.dmarc_weekly_summary.family" in body, (
        "the schedule must target the summary's own task family — a schedule pointed at any other "
        "family runs the wrong command on a clock and reports success"
    )


def _schedule_expression() -> str:
    """The default of `var.dmarc_summary_schedule_expression`, from variables.tf."""
    variables = _read(VARIABLES_TF)
    block = variables.split(f'variable "{SCHEDULE_VAR}"', 1)
    assert len(block) == 2, f"infra/variables.tf declares no {SCHEDULE_VAR}"
    found = re.search(r'^\s*default\s*=\s*"([^"]+)"\s*$', block[1], re.MULTILINE)
    assert found, f"{SCHEDULE_VAR} has no string default"
    return found.group(1)


def test_the_schedule_is_weekly():
    """WEEKLY IS THE THING THE OWNER ASKED FOR, and both directions cost something.

    Too often and the summary becomes noise the owner mutes — at which point
    the arrival of the message stops being the heartbeat this design leans on.
    Too rarely and the fortnight of evidence #1504 needs before touching
    `p=none` is discovered on the day someone wants to change the policy.

    So: a cron naming exactly ONE weekday. `rate(...)` cannot express "weekly"
    with a fixed arrival time, and a cron whose day-of-week is `*`, `?`, a list
    or a range fires more often than once a week. Both are refused here rather
    than in review.
    """
    expression = _schedule_expression()
    assert expression.startswith("cron(") and expression.endswith(")"), (
        f"{expression!r} is not a cron expression — `rate(...)` cannot pin a weekly arrival time, and "
        "a predictable arrival is what makes a MISSING summary noticeable"
    )
    fields = expression[len("cron(") : -1].split()
    assert len(fields) == 6, (
        f"EventBridge Scheduler cron takes 6 fields (minutes hours day-of-month month day-of-week year); "
        f"{expression!r} has {len(fields)}"
    )
    minutes, hours, day_of_month, _month, day_of_week, _year = fields
    assert day_of_month == "?", (
        f"day-of-month is {day_of_month!r}: a cron that fires on a day-of-MONTH is monthly, not weekly"
    )
    assert day_of_week not in {"*", "?"}, (
        f"day-of-week is {day_of_week!r}, which fires every day — the owner asked for a weekly summary"
    )
    assert not set(day_of_week) & set(",-/"), (
        f"day-of-week is {day_of_week!r}: a list, range or step fires on more than one day a week"
    )
    for field, label in ((minutes, "minutes"), (hours, "hours")):
        assert field.isdigit(), f"{label} is {field!r}; a wildcard there fires many times on the chosen day"


def test_the_scheduled_task_runs_the_summary_module():
    """The only place the command is named.

    `python -m archimedes.scripts.dmarc_weekly_summary` is one string away from
    a task that fails identically every Monday, in a log group nobody tails.
    """
    container = _resource(_strip_comments(_read(DMARC_TF)), "aws_ecs_task_definition", "dmarc_weekly_summary")
    assert '"python", "-m", "archimedes.scripts.dmarc_weekly_summary"' in container, (
        "the scheduled task must run `python -m archimedes.scripts.dmarc_weekly_summary`"
    )
    assert "aws_ecr_repository.backend.repository_url" in container, (
        "the summary must ride the existing backend image — a second image is a second thing to build, "
        "and the job's whole point is that it is cheap enough to keep running"
    )


# ────────────── the task is told where to look and whom to tell ─────────


def _env_pattern(name: str, value_expr: str) -> re.Pattern[str]:
    return re.compile(r'\{\s*name\s*=\s*"' + name + r'"\s*,\s*value\s*=\s*' + re.escape(value_expr) + r"\s*\}")


def test_the_task_names_the_owner_alert_variable_as_the_summary_destination():
    """THE guard the owner's decision asks for: the summary goes to the alert address.

    Enforced structurally, not by string comparison — the env value is
    `var.owner_alert_email` itself, so the destination cannot be edited apart
    from the address #1818 P5 established the owner actually reads. A literal
    here is how the two drift, and a summary sent to an address nobody opens is
    the same silence this issue is about, one step further along.
    """
    container = _resource(_strip_comments(_read(DMARC_TF)), "aws_ecs_task_definition", "dmarc_weekly_summary")
    assert _env_pattern(RECIPIENT_ENV, RECIPIENT_REF).search(container), (
        f"the summary task must set {RECIPIENT_ENV} = {RECIPIENT_REF}. Without it the job exits 2 and "
        "mails nobody; with a literal, the address drifts from the one alarms use and nobody notices "
        "until the week it matters."
    )


def test_the_task_is_told_which_bucket_and_prefix_off_the_resources_themselves():
    """A wrong bucket or prefix lists nothing, and the job mails "no reports received"."""
    tf = _strip_comments(_read(DMARC_TF))
    container = _resource(tf, "aws_ecs_task_definition", "dmarc_weekly_summary")
    assert _env_pattern(BUCKET_ENV, "aws_s3_bucket.dmarc_reports.id").search(container), (
        f"the summary task must set {BUCKET_ENV} = aws_s3_bucket.dmarc_reports.id — a literal bucket "
        "name lists an empty prefix and the summary reports a quiet week"
    )
    assert _env_pattern("DMARC_REPORTS_PREFIX", "local.dmarc_object_key_prefix").search(container), (
        "the prefix must come from the same local the receipt rule's object_key_prefix uses; two "
        "copies drift and a drifted prefix is indistinguishable from an empty bucket"
    )
    rule = _resource(tf, "aws_ses_receipt_rule", "dmarc_reports")
    assert "object_key_prefix = local.dmarc_object_key_prefix" in rule, (
        "the receipt rule must write under the same local the reader reads from"
    )


def test_the_task_sends_as_the_verified_domain_identity():
    container = _resource(_strip_comments(_read(DMARC_TF)), "aws_ecs_task_definition", "dmarc_weekly_summary")
    assert _env_pattern(SENDER_ENV, '"no-reply@${var.domain_name}"').search(container), (
        f'the summary task must set {SENDER_ENV} = "no-reply@${{var.domain_name}}", the same identity '
        "infra/ecs.tf's auth container sends verification mail as — a different sender needs a second "
        "SES identity and a second IAM grant"
    )


def test_the_task_carries_no_database_secrets():
    """It touches no database, and the module's import surface is pinned to keep it that way.

    `backend/tests/scripts/test_dmarc_weekly_summary.py::test_the_job_needs_no_database`
    is the other half: if an import of `archimedes.services` ever appears, that
    test fails here rather than a Fargate task crash-looping on a missing
    DATABASE_URL at 13:00 on a Monday.
    """
    container = _resource(_strip_comments(_read(DMARC_TF)), "aws_ecs_task_definition", "dmarc_weekly_summary")
    assert "DATABASE_URL" not in container, (
        "the summary reads S3 and calls SES; a DATABASE_URL here would be an unused secret grant"
    )


# ──────────────────────────── the two grants ───────────────────────────


def _statements(policy_body: str) -> dict[str, str]:
    """Split a `jsonencode` policy into `{Sid: statement text}`.

    PER STATEMENT, not over the blob. A substring check across a two-statement
    policy is satisfied by EITHER statement, so it cannot see one of them being
    widened: with `Resource = "*"` on the `ListBucket` statement (list every
    bucket in the account) or on the `GetObject` statement (read every object
    in every bucket), a whole-body `"aws_s3_bucket.dmarc_reports.arn" in grant`
    still passes on the strength of the other one.
    """
    chunks = re.split(r'\bSid\s*=\s*"([^"]+)"', policy_body)
    # re.split with one group yields [prefix, sid, body, sid, body, ...].
    return dict(zip(chunks[1::2], chunks[2::2], strict=True))


def _resource_expr(statement: str) -> str:
    """The `Resource = …` right-hand side of one statement, verbatim."""
    found = re.search(r"^\s*Resource\s*=\s*(.+?)\s*$", statement, re.MULTILINE)
    assert found, f"a statement with no Resource grants nothing legible:\n{statement}"
    return found.group(1)


def test_the_task_role_can_read_the_reports_bucket_and_nothing_more():
    tf = _strip_comments(_read(DMARC_TF))
    grant = _resource(tf, "aws_iam_role_policy", "ecs_task_dmarc_reports_read")
    assert "role = aws_iam_role.ecs_task.id" in grant, (
        "the grant must be on the shared ECS task role, which is the identity the backend image runs "
        "under wherever it runs — the same way infra/ses_events.tf adds its queue grant"
    )
    for action in ('"s3:ListBucket"', '"s3:GetObject"'):
        assert action in grant, f"the summary cannot read reports without {action}"
    for forbidden in ("s3:PutObject", "s3:DeleteObject", "s3:*"):
        assert forbidden not in grant, (
            f"{forbidden} is granted to a job that only reads; the 180-day lifecycle rule must stay the "
            "only thing that removes a report"
        )

    statements = _statements(grant)
    assert set(statements) == {"ListDmarcReportsBucket", "ReadDmarcReports"}, (
        f"unexpected statements in the read grant: {sorted(statements)}. Each one widens what a job "
        "that only reads two weeks of aggregate reports can reach, so each one has to be named here."
    )

    listing = statements["ListDmarcReportsBucket"]
    assert _resource_expr(listing) == "aws_s3_bucket.dmarc_reports.arn", (
        "the listing must name THIS bucket's arn. A widened Resource here lists every bucket in the "
        "account, and nothing about the rest of the policy would show it"
    )
    assert 'StringLike = { "s3:prefix" = ["${local.dmarc_object_key_prefix}*"] }' in listing, (
        "s3:prefix is the only thing that scopes a listing — an object-arn Resource does not — so the "
        "condition is load-bearing, and it must read the same local the receipt rule writes under"
    )

    read = statements["ReadDmarcReports"]
    assert _resource_expr(read) == '"${aws_s3_bucket.dmarc_reports.arn}/${local.dmarc_object_key_prefix}*"', (
        "the object grant must name this bucket AND this prefix. A widened Resource here reads every "
        "object in every bucket in the account"
    )

    for sid, statement in statements.items():
        assert _resource_expr(statement) != '"*"', f"{sid} grants s3 on every resource in the account"


def test_the_send_grant_still_exists_where_it_is_reused_from():
    """The summary adds NO SES IAM of its own — it reuses infra/ecs.tf's grant.

    That reuse is what makes sending from no-reply@ free rather than a second
    identity to provision, and it is only true while the grant is there. Deleting
    it in ecs.tf would turn the Monday mail off with an AccessDeniedException
    that surfaces nowhere but a task exit code, so it fails here instead.
    """
    ecs = _strip_comments(_read(ECS_TF))
    grant = ecs.split('resource "aws_iam_role_policy" "ecs_task_ses_send"', 1)
    assert len(grant) == 2, (
        "infra/ecs.tf no longer grants the ECS task role ses:SendEmail — the weekly DMARC summary "
        "(infra/dmarc_reports.tf) deliberately adds no SES grant of its own and depends on this one"
    )
    body = grant[1].split("\nresource ", 1)[0]
    assert '"ses:SendEmail"' in body
    assert "identity/${var.domain_name}" in body, (
        "the send grant must be scoped to the verified domain identity the summary sends as"
    )


def test_the_scheduler_may_only_run_this_one_family():
    """An `ecs:RunTask` grant scoped to `*` would let a compromised schedule run anything."""
    tf = _strip_comments(_read(DMARC_TF))
    body = _resource(tf, "aws_iam_role_policy", "dmarc_summary_scheduler_run_task")
    assert "task-definition/${aws_ecs_task_definition.dmarc_weekly_summary.family}:*" in body, (
        "the RunTask grant must name the summary family, not a wildcard"
    )
    assert 'ArnLike = { "ecs:cluster" = aws_ecs_cluster.main.arn }' in body, (
        "the RunTask grant must be scoped to this cluster"
    )
    assert '"iam:PassedToService" = "ecs-tasks.amazonaws.com"' in body, (
        "the PassRole grant must be scoped to ECS — the task role carries the bucket read and the SES "
        "send, and an unscoped PassRole hands both to anything that can assume the scheduler role"
    )


def test_the_scheduler_role_only_trusts_this_account():
    tf = _strip_comments(_read(DMARC_TF))
    body = _resource(tf, "aws_iam_role", "dmarc_summary_scheduler")
    assert 'Principal = { Service = "scheduler.amazonaws.com" }' in body
    assert '"aws:SourceAccount" = data.aws_caller_identity.current.account_id' in body, (
        "the assume-role policy must be scoped to this account, the same way aws_iam_role.ses_events_scheduler is"
    )


def test_the_runbook_sections_the_summary_mails_people_to_actually_exist():
    """The mail's only next step is a runbook section name.

    Both bodies end by naming a section — the arrival ladder on an empty
    window, the parse-failure section when objects landed and none parsed, the
    weekly-summary section everywhere else. A renamed or deleted heading turns
    the one actionable line in the message into a dead reference, and the
    person reading it is by definition already looking at something they do not
    understand.
    """
    runbook = _read(REPO_ROOT / "docs" / "runbooks" / "dmarc-reports.md")
    headings = {line.lstrip("# ").strip() for line in runbook.splitlines() if line.startswith("#")}
    for pointed_at in (
        "No reports are arriving",
        "Reports arrive but cannot be parsed",
        "The weekly summary",
    ):
        assert any(heading.endswith(pointed_at) for heading in headings), (
            f"docs/runbooks/dmarc-reports.md has no section ending '{pointed_at}', but "
            "archimedes/scripts/dmarc_weekly_summary.py mails the owner there"
        )


def test_the_task_family_is_exported_for_the_operator():
    """`aws ecs run-task --task-definition $(terraform output ...)` is the runbook's manual path."""
    outputs = _strip_comments(_read(OUTPUTS_TF))
    assert 'output "dmarc_summary_task_definition_family"' in outputs
    assert "aws_ecs_task_definition.dmarc_weekly_summary.family" in outputs
