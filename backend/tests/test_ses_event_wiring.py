"""The SES bounce signal is a chain, and every link is invisible when broken (#1804).

    auth/mailer.js  ──ConfigurationSetName──▶  aws_sesv2_configuration_set.mail
                                                       │
                                          event destination (BOUNCE, COMPLAINT)
                                                       ▼
                                              aws_sns_topic.ses_events
                                                       ▼
                                              aws_sqs_queue.ses_events
                                                       ▼
                              aws_scheduler_schedule.ses_events_drain
                                                       ▼
                                  archimedes.scripts.ses_events drain

Every one of those links fails SILENTLY. A send that names no configuration
set is a perfectly successful send that publishes no event. A configuration
set whose name does not match the one the mailer sends with publishes events
for nobody. An SNS topic with no SQS subscriber drops each notification on the
floor. A task role without the queue grant makes the drain raise AccessDenied
where nobody is watching. And with no schedule at all, every resource above is
built, wired, and inert: bounces pile up in SQS and the 14-day retention
deletes them unread. In every case mail still goes out, no alarm fires, and the
product goes back to being unable to tell a dead address from an impatient
human — the exact state #1804 is about.

``terraform validate`` catches none of it: a configuration set nobody
references, an event destination with an empty ``matching_event_types``, and a
mailer that sends without a set are all syntactically valid.

These tests read the repo's own files as text. Hermetic by construction — no
AWS, no terraform binary, no network, no env vars.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SES_EVENTS_TF = REPO_ROOT / "infra" / "ses_events.tf"
ECS_TF = REPO_ROOT / "infra" / "ecs.tf"
MAILER_JS = REPO_ROOT / "auth" / "mailer.js"
OUTPUTS_TF = REPO_ROOT / "infra" / "outputs.tf"

#: The terraform expression the auth container's SES_CONFIGURATION_SET value
#: must be, and the one the IAM configuration-set ARN must interpolate. Using
#: the resource attribute rather than a copy of the string is what makes the
#: "name in terraform == name the mailer sends with" guard structural: there is
#: one name, in one place, and the other two files reference it.
CONFIG_SET_REF = "aws_sesv2_configuration_set.mail.configuration_set_name"

#: The env var name is the contract between terraform and auth/mailer.js.
CONFIG_SET_ENV = "SES_CONFIGURATION_SET"
QUEUE_URL_ENV = "SES_EVENTS_QUEUE_URL"


def _read(path: Path) -> str:
    assert path.exists(), f"{path} is missing — the SES feedback loop has no push half without it"
    return path.read_text(encoding="utf-8")


def _strip_comments(hcl: str) -> str:
    """Drop `#` comment lines so a mention in prose cannot satisfy a guard."""
    return "\n".join(line for line in hcl.splitlines() if not line.lstrip().startswith("#"))


#: The three containers `aws_ecs_task_definition.backend` defines, in the order
#: infra/ecs.tf declares them.
CONTAINERS = ("backend", "auth", "nginx")


def _container(ecs: str, name: str) -> str:
    """ONE container object out of infra/ecs.tf's `container_definitions`.

    WHY THE SLICE, AND NOT A GREP OVER THE WHOLE FILE. The three containers
    share one task definition but do NOT run the same code. `auth/mailer.js`
    only ever runs in the `auth` container; `archimedes.scripts.ses_events`
    only ever runs where the backend image runs. A file-wide regex therefore
    passes just as happily when `SES_CONFIGURATION_SET` is defined on the
    `backend` (or `nginx`) container — at which point no send names the set,
    SES publishes nothing, and every test in this file is still green. That is
    the same silent deafening the file's own docstring exists to defend
    against, one level down, so the guard has to be able to say WHICH
    container, not merely "somewhere in ecs.tf".

    Sliced on the object braces rather than on the container names so an entry
    smuggled ABOVE a container's own `name` line still lands in the right
    slice. `terraform fmt -check -recursive` (CI, quality-gate.yml) is what
    pins the indentation this relies on, and every assert below fails loudly
    rather than silently passing if the shape ever moves.
    """
    marker = "container_definitions = jsonencode(["
    assert ecs.count(marker) == 1, (
        f"infra/ecs.tf has {ecs.count(marker)} `container_definitions` blocks, expected exactly 1 — "
        "this helper slices the one belonging to aws_ecs_task_definition.backend"
    )
    region = ecs.split(marker, 1)[1]
    resource_end = re.search(r"^\}$", region, re.MULTILINE)
    assert resource_end, "could not find the end of the aws_ecs_task_definition.backend resource"
    region = region[: resource_end.start()]

    blocks = {}
    for chunk in re.split(r"^    \{$", region, flags=re.MULTILINE)[1:]:
        found = re.search(r'^\s+name\s*=\s*"([a-z0-9-]+)"\s*$', chunk, re.MULTILINE)
        if found:
            blocks[found.group(1)] = chunk
    assert set(CONTAINERS) <= set(blocks), (
        f"could not slice infra/ecs.tf's containers apart: found {sorted(blocks)}, expected at least "
        f"{sorted(CONTAINERS)}. Fix this helper before trusting anything below it — a slice that "
        "silently matched nothing would turn every per-container assert into a pass."
    )
    return blocks[name]


# ─────────────────── the mailer actually names the set ──────────────────


def test_mailer_sends_with_the_configuration_set_from_the_environment():
    """No ConfigurationSetName on SendEmail == no bounce events, ever."""
    mailer = _read(MAILER_JS)
    assert f"env.{CONFIG_SET_ENV}" in mailer, (
        f"auth/mailer.js must read {CONFIG_SET_ENV} — without it every send is invisible to the feedback loop"
    )
    assert "ConfigurationSetName" in mailer, (
        "auth/mailer.js must pass ConfigurationSetName to SendEmailCommand; a configuration set that no "
        "send names publishes events for nothing"
    )
    # The property must be inside the SES SendEmailCommand payload, not merely
    # mentioned: everything between `new ses.SendEmailCommand({` and the
    # mailer's console branch.
    send_command = mailer.split("new ses.SendEmailCommand(", 1)[1].split("kind: 'console'", 1)[0]
    assert "ConfigurationSetName" in send_command


def test_the_set_name_is_never_hardcoded_in_the_mailer():
    """One name, one place. The mailer reads it; terraform owns it."""
    mailer = _read(MAILER_JS)
    assert "archimedes-mail" not in mailer, (
        "auth/mailer.js must not carry a literal configuration set name — it comes from "
        f"{CONFIG_SET_ENV}, whose value infra/ecs.tf takes straight off the terraform resource"
    )


def test_a_blank_configuration_set_falls_back_to_todays_behaviour():
    """Landing this before the terraform apply must not break verification mail.

    The set does not exist until the owner applies; until then the variable is
    unset. Sending an empty ConfigurationSetName would be rejected by SES, so
    the property has to be OMITTED rather than blank.
    """
    mailer = _read(MAILER_JS)
    assert "configurationSet ? { ConfigurationSetName: configurationSet } : {}" in mailer


# ──────────────── terraform and the mailer agree on the name ────────────


def test_the_auth_container_is_given_the_configuration_sets_own_name():
    """THE guard #1804 asks for: the name in terraform == the name the mailer sends with.

    Enforced structurally rather than by string comparison — the env value is
    the resource attribute itself, so the two cannot be edited apart.

    Pinned to the AUTH container specifically. `auth/mailer.js` runs there and
    nowhere else, so the same line sitting on the backend or nginx container
    is a variable no send will ever read: mail keeps flowing, SES publishes
    nothing, and the loop is deaf with every other assertion in this file
    still green.
    """
    ecs = _strip_comments(_read(ECS_TF))
    pattern = re.compile(
        r'\{\s*name\s*=\s*"' + CONFIG_SET_ENV + r'"\s*,\s*value\s*=\s*' + re.escape(CONFIG_SET_REF) + r"\s*\}"
    )
    assert pattern.search(_container(ecs, "auth")), (
        f"infra/ecs.tf's auth container must set {CONFIG_SET_ENV} = {CONFIG_SET_REF}. A hardcoded literal "
        "here is exactly how the two names drift, and a drifted name publishes events for nobody while "
        "mail keeps flowing."
    )
    for other in ("backend", "nginx"):
        assert CONFIG_SET_ENV not in _container(ecs, other), (
            f"{CONFIG_SET_ENV} is defined on the `{other}` container. auth/mailer.js runs in the `auth` "
            "container only, so a set named there is named by no send at all — SES publishes nothing and "
            "the whole feedback loop goes silently deaf while verification mail keeps being delivered."
        )


def test_the_task_role_may_send_with_that_configuration_set():
    """Naming a set on SendEmail needs IAM on the SET as well as the identity.

    Without the configuration-set ARN in the same statement, turning the signal
    on turns verification mail OFF — every send fails AccessDeniedException.
    """
    ecs = _strip_comments(_read(ECS_TF))
    assert f"configuration-set/${{{CONFIG_SET_REF}}}" in ecs, (
        "infra/ecs.tf's ecs_task_ses_send policy must also grant the configuration-set ARN"
    )


def test_the_backend_container_knows_which_queue_to_drain():
    """The operator path: `ecs execute-command` into the running service task.

    Pinned to the BACKEND container for the same reason as the set name above.
    `python -m archimedes.scripts.ses_events drain` can only run where the
    backend image runs; the variable on nginx or auth is read by nothing, and
    the drain would exit 2 ("no queue URL") in the one place an operator
    reaches for it.
    """
    ecs = _strip_comments(_read(ECS_TF))
    pattern = re.compile(
        r'\{\s*name\s*=\s*"'
        + QUEUE_URL_ENV
        + r'"\s*,\s*value\s*=\s*'
        + re.escape("aws_sqs_queue.ses_events.id")
        + r"\s*\}"
    )
    assert pattern.search(_container(ecs, "backend")), (
        f"infra/ecs.tf's backend container must set {QUEUE_URL_ENV} = aws_sqs_queue.ses_events.id — "
        "`ses_events drain` reads it, and hardcoding a queue URL anywhere else is how it ends up "
        "draining the wrong account's queue"
    )
    for other in ("auth", "nginx"):
        assert QUEUE_URL_ENV not in _container(ecs, other), (
            f"{QUEUE_URL_ENV} is defined on the `{other}` container, which does not carry the backend "
            "image and cannot run the drain — the variable would be read by nothing"
        )


def test_the_task_role_can_read_and_delete_from_the_queue():
    tf = _strip_comments(_read(SES_EVENTS_TF))
    grant = tf.split('resource "aws_iam_role_policy" "ecs_task_ses_events_queue"', 1)
    assert len(grant) == 2, "no IAM grant for the SES event queue — the drain would raise AccessDenied"
    body = grant[1]
    for action in ("sqs:ReceiveMessage", "sqs:DeleteMessage"):
        assert action in body, f"the queue grant must allow {action}"
    assert "sqs:SendMessage" not in body, (
        "only SNS writes to this queue; granting the task role SendMessage would let application code "
        "manufacture bounce events about its own users"
    )


# ───────────────────── the events actually reach the queue ──────────────


def test_the_event_destination_carries_bounce_and_complaint():
    tf = _strip_comments(_read(SES_EVENTS_TF))
    match = re.search(r"matching_event_types\s*=\s*\[([^\]]*)\]", tf)
    assert match, "no matching_event_types — an event destination that matches nothing is not a signal"
    types = {value.strip().strip('"') for value in match.group(1).split(",") if value.strip()}
    assert {"BOUNCE", "COMPLAINT"} <= types, f"BOUNCE and COMPLAINT are the two that stamp a user; got {sorted(types)}"
    # Not an accident of scope creep: link tracking rewrites URLs and embeds a
    # pixel in transactional mail people are asked to trust.
    assert not ({"OPEN", "CLICK"} & types), "no open/click tracking on verification mail"


def test_the_destination_publishes_to_the_topic_the_queue_subscribes_to():
    """SNS with no subscriber DROPS notifications — the queue is the durability."""
    tf = _strip_comments(_read(SES_EVENTS_TF))
    assert "sns_destination {" in tf
    assert "topic_arn = aws_sns_topic.ses_events.arn" in tf
    subscription = tf.split('resource "aws_sns_topic_subscription" "ses_events_sqs"', 1)
    assert len(subscription) == 2, "the topic has no SQS subscription — every bounce would be dropped"
    body = subscription[1].split("\n}", 1)[0]
    assert 'protocol  = "sqs"' in body or 'protocol = "sqs"' in body
    assert "endpoint  = aws_sqs_queue.ses_events.arn" in body or "endpoint = aws_sqs_queue.ses_events.arn" in body


def test_only_our_own_ses_can_publish_and_only_our_topic_can_enqueue():
    """Both hops are scoped: these messages are treated as truth about users."""
    tf = _strip_comments(_read(SES_EVENTS_TF))
    assert '"AWS:SourceAccount" = data.aws_caller_identity.current.account_id' in tf, (
        "the SNS topic policy must scope ses.amazonaws.com to this account"
    )
    assert 'ArnEquals = { "aws:SourceArn" = aws_sns_topic.ses_events.arn }' in tf, (
        "the queue policy must scope sns.amazonaws.com to this topic, not to SNS as a whole"
    )


def test_the_queue_is_durable_and_has_a_dead_letter_queue():
    """The retention is what makes a PERIODIC consumer honest rather than lossy."""
    tf = _strip_comments(_read(SES_EVENTS_TF))
    retentions = re.findall(r"message_retention_seconds\s*=\s*(\d+)", tf)
    assert retentions, "no message_retention_seconds — a bounce that arrives between drains would expire early"
    assert all(int(value) == 1209600 for value in retentions), (
        f"both queues must hold the SQS maximum of 14 days; got {retentions}"
    )
    assert "deadLetterTargetArn = aws_sqs_queue.ses_events_dlq.arn" in tf, (
        "without a DLQ an unparseable message is retried forever at the head of the queue"
    )
    assert "sqs_managed_sse_enabled = true" in tf


def test_the_queue_url_is_exported_for_the_operator():
    outputs = _strip_comments(_read(OUTPUTS_TF))
    assert 'output "ses_events_queue_url"' in outputs
    assert 'output "ses_configuration_set_name"' in outputs


# ─────────────────── something actually CALLS the consumer ──────────────


def _drain_task_container(tf: str) -> str:
    """The one container object inside aws_ecs_task_definition.ses_events_drain."""
    block = tf.split('resource "aws_ecs_task_definition" "ses_events_drain"', 1)
    assert len(block) == 2, (
        "there is no task definition for the drain — every resource in this file would be built, wired, and still deaf"
    )
    return block[1].split("\nresource ", 1)[0]


def test_something_invokes_the_consumer_on_a_clock():
    """A built loop nobody calls is the defect #1804 opened against, with terraform in it.

    The queue's 14-day retention makes a PERIODIC drain honest — a bounce that
    arrives between ticks is late, never lost — but only if the ticks exist.
    With no schedule, bounces accumulate in SQS and the retention deletes them
    unread, and every other assertion in this file still passes.
    """
    tf = _strip_comments(_read(SES_EVENTS_TF))
    schedule = tf.split('resource "aws_scheduler_schedule" "ses_events_drain"', 1)
    assert len(schedule) == 2, (
        "no aws_scheduler_schedule for the drain — nothing calls "
        "`archimedes.scripts.ses_events drain`, so nothing is ever stamped"
    )
    body = schedule[1].split("\nresource ", 1)[0]
    assert "schedule_expression = var.ses_events_drain_schedule_expression" in body, (
        "the interval must be a variable, so the owner can loosen it without a code change"
    )
    assert 'state               = "ENABLED"' in body or 'state = "ENABLED"' in body, (
        "a DISABLED schedule is indistinguishable from no schedule at all"
    )
    assert "aws_ecs_task_definition.ses_events_drain.family" in body, (
        "the schedule must target the drain's own task family — a schedule pointed at any other "
        "family runs the wrong command on a clock and reports success"
    )


def test_the_scheduled_task_actually_runs_the_drain_command():
    """The task definition is the only place the subcommand is named.

    `python -m archimedes.scripts.ses_events` with no subcommand exits 2
    (argparse: a subcommand is required), and `clear` would need an address.
    Only `drain` consumes the queue, so this string is the difference between
    a schedule that works and one that fails identically every tick.
    """
    container = _drain_task_container(_strip_comments(_read(SES_EVENTS_TF)))
    assert '"archimedes.scripts.ses_events", "drain"' in container, (
        "the scheduled task must run `python -m archimedes.scripts.ses_events drain`"
    )


def test_the_scheduled_task_is_told_which_queue_and_which_database():
    """Both are hard requirements, and only one of them fails loudly.

    Without SES_EVENTS_QUEUE_URL the command exits 2 with a message. Without
    DATABASE_URL there is nothing to write the stamp through. The queue URL
    comes off the resource, never a literal — same one-name-one-place rule as
    the configuration set.
    """
    container = _drain_task_container(_strip_comments(_read(SES_EVENTS_TF)))
    pattern = re.compile(
        r'\{\s*name\s*=\s*"'
        + QUEUE_URL_ENV
        + r'"\s*,\s*value\s*=\s*'
        + re.escape("aws_sqs_queue.ses_events.id")
        + r"\s*\}"
    )
    assert pattern.search(container), (
        f"the drain task must set {QUEUE_URL_ENV} = aws_sqs_queue.ses_events.id — without it every "
        "scheduled invocation exits 2 without reading a single message"
    )
    assert "parameter/archimedes/prod/DATABASE_URL" in container, (
        "the drain task must resolve DATABASE_URL from SSM — the stamp is a database write"
    )


def test_the_scheduler_may_only_run_this_one_family():
    """An `ecs:RunTask` grant scoped to `*` would let a compromised schedule run anything."""
    tf = _strip_comments(_read(SES_EVENTS_TF))
    grant = tf.split('resource "aws_iam_role_policy" "ses_events_scheduler_run_task"', 1)
    assert len(grant) == 2, "the scheduler role has no RunTask grant — every tick would fail AccessDenied"
    body = grant[1].split("\nresource ", 1)[0]
    assert "task-definition/${aws_ecs_task_definition.ses_events_drain.family}:*" in body, (
        "the RunTask grant must name the drain family, not a wildcard"
    )
    assert 'ArnLike = { "ecs:cluster" = aws_ecs_cluster.main.arn }' in body, (
        "the RunTask grant must be scoped to this cluster"
    )
    assert '"iam:PassedToService" = "ecs-tasks.amazonaws.com"' in body, (
        "the PassRole grant must be scoped to ECS — the task role carries the queue grant and the "
        "SSM secret access, and an unscoped PassRole hands both to anything that can assume the "
        "scheduler role"
    )


def test_a_drain_that_stops_running_sets_off_an_alarm():
    """The schedule must not become the next unwatched link.

    A drain that silently stops — a revoked grant, a failing image, a disabled
    schedule — puts the product straight back in #1804's original state, and
    the ONLY externally visible symptom is a queue that stops emptying.
    """
    tf = _strip_comments(_read(SES_EVENTS_TF))
    assert 'resource "aws_cloudwatch_metric_alarm" "ses_events_not_being_drained"' in tf, (
        "nothing watches whether the drain is still running"
    )
    backlog = tf.split('resource "aws_cloudwatch_metric_alarm" "ses_events_not_being_drained"', 1)[1]
    backlog = backlog.split("\nresource ", 1)[0]
    assert 'metric_name = "ApproximateAgeOfOldestMessage"' in backlog, (
        "age-of-oldest-message is the metric that says 'nothing is draining this'; a message COUNT "
        "cannot tell a busy quarter-hour apart from a dead consumer"
    )
    assert "dimensions  = { QueueName = aws_sqs_queue.ses_events.name }" in backlog
    assert "alarm_actions = [aws_sns_topic.alerts.arn]" in backlog, "an alarm that notifies nobody is a dashboard"
    # An empty queue publishes no datapoints at all, and empty is the healthy
    # steady state — `missing = breaching` would fire every quiet week, which
    # is how an alarm gets muted and stops being a signal.
    assert 'treat_missing_data = "notBreaching"' in backlog


def test_a_message_the_parser_cannot_handle_sets_off_an_alarm():
    """The DLQ is where an AWS schema change lands, and it is silent by default."""
    tf = _strip_comments(_read(SES_EVENTS_TF))
    assert 'resource "aws_cloudwatch_metric_alarm" "ses_events_dlq_not_empty"' in tf, (
        "nothing watches the dead-letter queue — a parser behind an SES schema change would look "
        "exactly like a quiet week"
    )
    dlq = tf.split('resource "aws_cloudwatch_metric_alarm" "ses_events_dlq_not_empty"', 1)[1]
    dlq = dlq.split("\nresource ", 1)[0]
    assert "dimensions  = { QueueName = aws_sqs_queue.ses_events_dlq.name }" in dlq
    assert "threshold           = 0" in dlq, "one message in the DLQ is already the signal"
    assert "alarm_actions = [aws_sns_topic.alerts.arn]" in dlq


def test_the_drain_task_family_is_exported_for_the_operator():
    outputs = _strip_comments(_read(OUTPUTS_TF))
    assert 'output "ses_events_drain_task_definition_family"' in outputs, (
        "the operator needs the family name to force a drain by hand (`aws ecs run-task`) when a "
        "bounce needs clearing now rather than at the next tick"
    )
