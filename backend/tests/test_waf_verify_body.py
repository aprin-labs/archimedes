"""The 8 KB ceiling on POST /api/rigor/verify — at the edge and in the app (#1749).

`infra/waf.tf` attached `AWSManagedRulesCommonRuleSet` to the ALB in full BLOCK
mode with no `rule_action_override` and no `scope_down_statement`. Its
`SizeRestrictions_BODY` rule blocks any body over the WAF body-inspection limit
for a REGIONAL web ACL — 8,192 bytes. `POST /api/rigor/verify` serialises one
JSON object per bar and had no `max_length`, so a real returns series crossed
8 KB at ~163 rows. Dogfood 2026-09-01 (agent-cli), deterministic:

    160 rows / 8,076 B -> 200     MEASURED
    165 rows / 8,326 B -> 403     MEASURED (HTML from awselb/2.0, not FastAPI)
    252 rows           -> 403     MEASURED, every time  <- ONE TRADING YEAR

(50.5 B/row including the envelope, from those two measured points; the 8,192
limit is crossed at ~163 rows.)

The endpoint that exists to give a DSR/OOS verdict statistical power rejected
exactly the sample size that gives it power.

The fix is deliberately SCOPED, and these tests exist to keep it scoped:

  * `SizeRestrictions_BODY` is overridden to `count` on the CRS group — and
    NOTHING ELSE is. A blanket `override_action { count {} }` on the group, or a
    second `rule_action_override`, would silently disable real controls;
  * a custom rule at priority 11 (immediately after the CRS group, before
    known-bad-inputs at 20) re-imposes the 8,192-byte ceiling for every path
    EXCEPT the one literal path `/api/rigor/verify`. `positional_constraint =
    "EXACTLY"` matters: widening it to a `/api/*` prefix would hand every API
    route an unlimited body, which is the DoS/parser-abuse control the managed
    rule was there to provide;
  * the rule's statement NESTING is asserted as a parsed tree, not as substrings.
    `not_statement { and_statement { size, byte_match } }` — the negation wrapped
    AROUND the pair rather than ANDed beside the size test — is valid HCL, passes
    `terraform validate`, and contains every string a substring guard looks for,
    while meaning "block everything EXCEPT an oversize POST to /api/rigor/verify":
    a site-wide outage the moment it is applied;
  * the application, not the edge, owns the real ceiling — `RigorVerifyRequest`
    caps `returns` at 2,600 rows (a decade of daily bars) and fails closed with
    a 422 that names the limit.

`terraform validate` cannot catch any of this. A deleted override, an override
on the wrong rule name, a prefix-widened exception, an inverted NOT/AND nesting
and a custom rule ordered ahead of the managed group are all syntactically valid
HCL.

Hermetic: reads one file from the repo and exercises the pydantic model plus the
router over an in-process ASGI transport. No AWS, no terraform binary, no
network, no DB.

Run:
    /path/to/env/bin/pytest backend/tests/test_waf_verify_body.py -q
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from archimedes.api import account_auth, rigor_verify_routes
from archimedes.api.rigor_verify_routes import _MAX_RETURN_ROWS, _MIN_RETURN_ROWS, RigorVerifyRequest
from archimedes.services.rigor_evaluator import DSR_MIN_BARS
from fastapi import FastAPI
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
WAF_TF = REPO_ROOT / "infra" / "waf.tf"

# The AWS body-inspection limit for a REGIONAL web ACL. The managed rule and the
# custom replacement must agree on it; spelled out so a drifting constant fails.
REGIONAL_BODY_INSPECTION_LIMIT = 8192

# The one managed rule this change is permitted to soften, and the one literal
# path it is permitted to soften it for.
OVERRIDDEN_RULE = "SizeRestrictions_BODY"
VERIFY_PATH = "/api/rigor/verify"

CUSTOM_RULE_NAME = "oversize-body-except-rigor-verify"
CRS_RULE_NAME = "aws-core-rules"
CRS_GROUP = "AWSManagedRulesCommonRuleSet"

# The managed groups that must keep blocking, untouched, at their existing
# priorities. The custom rule has to sit between the CRS group and these.
DOWNSTREAM_MANAGED_PRIORITIES = {"aws-known-bad-inputs": 20, "aws-ip-reputation": 30, "aws-sqli": 40}


# ── Minimal HCL reader ───────────────────────────────────────────────────
#
# Brace-matching over comment-stripped text. Enough to isolate a `rule { ... }`
# body and assert on its contents; deliberately not a full HCL parser.


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _brace_block(text: str, marker: str, *, start: int = 0) -> str:
    """Body of the first `{...}` block at/after `marker`, braces matched."""
    i = text.find(marker, start)
    assert i != -1, f"no {marker!r} block found — the scoped WAF fix for #1749 is gone"
    j = text.index("{", i)
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[j + 1 : k]
    raise AssertionError(f"unterminated block after {marker!r}")


def _rule_block(name: str) -> str:
    """The `rule { ... }` body whose `name = "<name>"` line it contains."""
    text = _strip_comments(WAF_TF.read_text())
    for match in re.finditer(r"\brule\s*\{", text):
        body = _brace_block(text, "rule", start=match.start())
        if re.search(rf'^\s*name\s*=\s*"{re.escape(name)}"\s*$', body, re.MULTILINE):
            return body
    raise AssertionError(f"no rule named {name!r} in infra/waf.tf")


def _priority(rule_body: str) -> int:
    match = re.search(r"^\s*priority\s*=\s*(\d+)\s*$", rule_body, re.MULTILINE)
    assert match, "rule has no priority"
    return int(match.group(1))


def _collapse(text: str) -> str:
    """Whitespace-insensitive form, so HCL formatting is not part of the assert."""
    return re.sub(r"\s+", " ", text).strip()


# ── Structural reader: the statement TREE, not the strings in it ─────────
#
# Substring assertions cannot see nesting. `not_statement { statement {
# and_statement { size, byte_match } } }` contains every token that
# `and_statement`/`not_statement`/`EXACTLY` string checks look for, is valid
# HCL, passes `terraform validate` — and means "block everything EXCEPT an
# oversize POST to /api/rigor/verify", i.e. a site-wide outage on apply. So the
# rule's statement tree is parsed and asserted node by node instead.

_TRAILING_IDENT = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*\Z")


def _skip_string(text: str, i: int) -> int:
    """Index just past the double-quoted string starting at `i`."""
    i += 1
    while i < len(text) and text[i] != '"':
        i += 2 if text[i] == "\\" else 1
    return i + 1


def _child_blocks(body: str) -> list[tuple[str, str]]:
    """Direct child `label { ... }` blocks of an HCL block body, in source order.

    Quoted strings are skipped, so `"${var.project_name}-oversize-body"` does not
    open a block. Attribute lines (`size = 8192`) are not blocks and are ignored.
    """
    blocks: list[tuple[str, str]] = []
    depth, label, opened_at, i = 0, None, 0, 0
    while i < len(body):
        char = body[i]
        if char == '"':
            i = _skip_string(body, i)
            continue
        if char == "{":
            if depth == 0:
                found = _TRAILING_IDENT.search(body, 0, i)
                label, opened_at = (found.group(1) if found else None), i
            depth += 1
        elif char == "}":
            depth -= 1
            assert depth >= 0, "unbalanced braces in infra/waf.tf"
            if depth == 0 and label is not None:
                blocks.append((label, body[opened_at + 1 : i]))
                label = None
        i += 1
    assert depth == 0, "unterminated block in infra/waf.tf"
    return blocks


def _own_attrs(body: str) -> str:
    """`body` with every child block's contents blanked — attributes of THIS block only."""
    kept, depth, i = [], 0, 0
    while i < len(body):
        char = body[i]
        if char == '"':
            end = _skip_string(body, i)
            kept.append(body[i:end] if depth == 0 else " " * (end - i))
            i = end
            continue
        if char == "{":
            depth += 1
            kept.append("{" if depth == 1 else " ")
        elif char == "}":
            kept.append("}" if depth == 1 else " ")
            depth -= 1
        else:
            kept.append(char if depth == 0 else ("\n" if char == "\n" else " "))
        i += 1
    return "".join(kept)


def _attr(body: str, key: str) -> str | None:
    """Value of `key = ...` declared directly on this block (never on a child)."""
    found = re.search(rf"^\s*{re.escape(key)}\s*=\s*(\S.*?)\s*$", _own_attrs(body), re.MULTILINE)
    return found.group(1) if found else None


def _sole_child(body: str, label: str, *, where: str) -> str:
    """The body of the ONLY child block, which must be named `label`.

    Strict on purpose: this is what makes an inverted nesting fail. A
    `not_statement` wrapped around the `and_statement` shows up here as the sole
    child being `not_statement` instead of `and_statement`.
    """
    labels = [name for name, _ in _child_blocks(body)]
    assert labels == [label], f"{where} must contain exactly one block, `{label} {{...}}`; got {labels}"
    return _child_blocks(body)[0][1]


def _named_child(body: str, label: str, *, where: str) -> str:
    """The body of the one child block named `label` (siblings of other kinds allowed)."""
    matches = [child for name, child in _child_blocks(body) if name == label]
    assert len(matches) == 1, f"{where} must have exactly one `{label}` block; found {len(matches)}"
    return matches[0]


def _descendant_labels(body: str) -> list[str]:
    labels: list[str] = []
    for name, child in _child_blocks(body):
        labels.append(name)
        labels.extend(_descendant_labels(child))
    return labels


def _verify_rule_statement_tree() -> tuple[str, str]:
    """Assert the custom rule's EXACT statement nesting; return (size body, byte_match body).

        rule
          statement
            and_statement
              statement -> size_constraint_statement      (the 8 KB ceiling)
              statement -> not_statement
                            statement -> byte_match_statement   (the one exempt path)

    The two branches are SIBLINGS. If the `not_statement` were an ancestor of the
    size constraint instead, the rule would read "block everything except an
    oversize POST to /api/rigor/verify" — every other request on the site, of any
    size, blocked at the edge.
    """
    rule = _rule_block(CUSTOM_RULE_NAME)
    top = _named_child(rule, "statement", where=f"rule {CUSTOM_RULE_NAME!r}")

    # The top-level statement must BE the and_statement. Anything wrapped around
    # it — a not_statement above all, an or_statement — changes what the rule
    # means while leaving every substring intact.
    and_body = _sole_child(top, "and_statement", where="rule.statement")

    branches = _child_blocks(and_body)
    assert [name for name, _ in branches] == ["statement", "statement"], (
        "rule.statement.and_statement must hold exactly two `statement` branches — "
        f"the size test and the negated path test; got {[name for name, _ in branches]}"
    )
    kinds = {
        _child_blocks(branch)[0][0]: _sole_child(branch, _child_blocks(branch)[0][0], where="an and_statement branch")
        for _, branch in branches
    }
    assert set(kinds) == {"size_constraint_statement", "not_statement"}, (
        "the and_statement's two branches must be a size_constraint_statement and a "
        f"not_statement, as SIBLINGS; got {sorted(kinds)}"
    )

    size = kinds["size_constraint_statement"]
    negated = kinds["not_statement"]

    # The negation must not contain the size test: nesting it there is the
    # inversion this function exists to catch.
    assert "size_constraint_statement" not in _descendant_labels(negated), (
        "the not_statement must NOT wrap the size constraint — that inverts the rule "
        "into a site-wide block of everything except an oversize verify POST"
    )

    inner = _sole_child(negated, "statement", where="rule.statement.and_statement.*.not_statement")
    byte_match = _sole_child(inner, "byte_match_statement", where="the not_statement's inner statement")

    # Exactly one of each in the whole rule: no second size test, no smuggled
    # extra path exception, no or_statement anywhere.
    labels = _descendant_labels(rule)
    for label, expected in (
        ("and_statement", 1),
        ("not_statement", 1),
        ("size_constraint_statement", 1),
        ("byte_match_statement", 1),
        ("or_statement", 0),
    ):
        assert labels.count(label) == expected, (
            f"rule {CUSTOM_RULE_NAME!r} must contain exactly {expected} `{label}`; found {labels.count(label)}"
        )

    return size, byte_match


# ── 1. The override exists, on the right rule, in the right group ────────


def test_size_restrictions_body_is_overridden_to_count_on_the_crs_group():
    crs = _rule_block(CRS_RULE_NAME)
    group = _brace_block(crs, "managed_rule_group_statement")
    assert f'name = "{CRS_GROUP}"' in _collapse(group).replace('name= "', 'name = "')

    override = _brace_block(group, "rule_action_override")
    collapsed = _collapse(override)
    assert f'name = "{OVERRIDDEN_RULE}"' in collapsed, f"the CRS override must name {OVERRIDDEN_RULE}; got: {collapsed}"
    assert "count {}" in _collapse(_brace_block(override, "action_to_use")), (
        "the override must set the action to count {} (keeps the metric/label while "
        "the custom rule at priority 11 carries the block)"
    )


def test_the_crs_group_itself_is_still_in_block_mode():
    """`override_action { none {} }` — softening the GROUP would disable CRS wholesale."""
    crs = _rule_block(CRS_RULE_NAME)
    assert "none {}" in _collapse(_brace_block(crs, "override_action"))


def test_no_other_managed_rule_is_overridden_anywhere_in_the_web_acl():
    """Exactly one rule_action_override in the file, and it is SizeRestrictions_BODY."""
    text = _strip_comments(WAF_TF.read_text())
    names: list[str] = []
    for match in re.finditer(r"\brule_action_override\s*\{", text):
        body = _brace_block(text, "rule_action_override", start=match.start())
        found = re.search(r'name\s*=\s*"([^"]+)"', body)
        names.append(found.group(1) if found else "<unnamed>")
    assert names == [OVERRIDDEN_RULE], f"exactly one managed rule may be overridden ({OVERRIDDEN_RULE}); found {names}"


# ── 2. The custom rule re-imposes the ceiling, with an EXACT-path exception ──


def test_custom_rule_blocks_oversize_bodies():
    rule = _rule_block(CUSTOM_RULE_NAME)
    assert "block {}" in _collapse(_brace_block(rule, "action")), "the replacement rule must BLOCK"

    size, _ = _verify_rule_statement_tree()
    assert _attr(size, "comparison_operator") == '"GT"'
    assert _attr(size, "size") == str(REGIONAL_BODY_INSPECTION_LIMIT), (
        f"the replacement must keep the managed rule's own {REGIONAL_BODY_INSPECTION_LIMIT}-byte "
        f"ceiling; got size = {_attr(size, 'size')}"
    )
    field = _named_child(size, "field_to_match", where="the size_constraint_statement")
    measured = _sole_child(field, "body", where="the size_constraint_statement's field_to_match")
    assert _attr(measured, "oversize_handling") == '"MATCH"', (
        "on an ALB only the first 8 KB reaches WAF, so the GT comparison alone can "
        'never fire; oversize_handling = "MATCH" on `body` is what blocks a >8 KB body; '
        f"got field_to_match -> body {{ oversize_handling = {_attr(measured, 'oversize_handling')} }}"
    )
    transform = _named_child(size, "text_transformation", where="the size_constraint_statement")
    assert _attr(transform, "type") == '"NONE"', "the body must be measured as sent, untransformed"


def test_the_exception_is_exactly_the_verify_path_and_is_negated():
    _, byte_match = _verify_rule_statement_tree()
    assert _attr(byte_match, "search_string") == f'"{VERIFY_PATH}"', (
        f"the exception must be the literal path {VERIFY_PATH}; got: {_collapse(_own_attrs(byte_match))}"
    )
    assert _attr(byte_match, "positional_constraint") == '"EXACTLY"', (
        "EXACTLY, never a prefix — a /api/* widening would lift the body ceiling off every API route"
    )
    field = _named_child(byte_match, "field_to_match", where="the byte_match_statement")
    assert [name for name, _ in _child_blocks(field)] == ["uri_path"], (
        "the exception must match on uri_path, not on headers, the query string or the body; "
        f"got field_to_match {{ {_collapse(field)} }}"
    )
    transform = _named_child(byte_match, "text_transformation", where="the byte_match_statement")
    assert _attr(transform, "type") == '"NONE"', (
        "NONE: the path is compared as sent, so every encoded/case variant of the "
        "verify path falls through to blocked rather than exempt"
    )


def test_the_negation_is_a_sibling_of_the_size_test_not_a_wrapper():
    """Adversarial: the inversion that every substring guard passes.

    `not_statement { statement { and_statement { size, byte_match } } }` is valid
    HCL, passes `terraform validate`, and contains `and_statement`,
    `not_statement`, `EXACTLY` and the verify path — everything the old string
    assertions looked for. It means the OPPOSITE of the intended rule: block
    every request site-wide except an oversize POST to /api/rigor/verify. On
    apply that is a total outage, so the nesting is asserted structurally.
    """
    _verify_rule_statement_tree()

    rule = _rule_block(CUSTOM_RULE_NAME)
    top = _named_child(rule, "statement", where=f"rule {CUSTOM_RULE_NAME!r}")
    assert _child_blocks(top)[0][0] == "and_statement", (
        "the rule's top-level statement must BE the and_statement; wrapping it in a "
        f"not_statement inverts the rule into a site-wide block. got: {_collapse(top)[:120]}"
    )
    # …and the path negation lives strictly beneath it, on one of its branches.
    and_body = _sole_child(top, "and_statement", where="rule.statement")
    assert "not_statement" in _descendant_labels(and_body), (
        "the size test and the negated path test must be SIBLING statements inside the and_statement"
    )


def test_no_wildcard_or_prefix_exception_anywhere_in_the_custom_rule():
    """Adversarial: the literal shapes a widening would take."""
    collapsed = _collapse(_rule_block(CUSTOM_RULE_NAME))
    for widened in ('"/api/*"', '"/api/"', '"/api"', '"/api/rigor/"', "STARTS_WITH", "CONTAINS"):
        assert widened not in collapsed, f"exception widened to {widened} — every other route loses the 8 KB ceiling"


# ── 3. Ordering ──────────────────────────────────────────────────────────


def test_custom_rule_is_evaluated_after_the_managed_group_and_before_the_rest():
    crs_priority = _priority(_rule_block(CRS_RULE_NAME))
    custom_priority = _priority(_rule_block(CUSTOM_RULE_NAME))
    assert custom_priority > crs_priority, (
        "WAF evaluates by ascending priority; the replacement must run AFTER the group whose rule it replaces"
    )
    for name, expected in DOWNSTREAM_MANAGED_PRIORITIES.items():
        downstream = _priority(_rule_block(name))
        assert downstream == expected, f"{name} priority moved ({downstream} != {expected})"
        assert custom_priority < downstream, f"the replacement must run before {name}, not be buried behind it"


# ── 4. The application owns the real ceiling ─────────────────────────────


def _rows(n: int) -> list[dict]:
    base = datetime(2024, 1, 1)
    return [
        {"date": (base + timedelta(days=i)).date().isoformat(), "daily_return": round(-0.0031 + i * 1e-6, 6)}
        for i in range(n)
    ]


def test_boundary_measurement_from_the_issue_still_holds():
    """One trading year really does exceed the 8 KB edge limit — the whole premise."""
    year = len(json.dumps({"returns": _rows(252), "trials": 1}, separators=(",", ":")).encode())
    assert year > REGIONAL_BODY_INSPECTION_LIMIT, (
        f"252 rows serialise to {year} B; the issue's premise is that this exceeds {REGIONAL_BODY_INSPECTION_LIMIT}"
    )
    assert 8_192 < year < 20_000, f"252 rows = {year} B, outside the measured band"


def test_the_waf_exemption_is_load_bearing_for_every_request_not_just_long_ones():
    """DEPLOY PRECONDITION. The 250-bar floor (#1803) coupled this endpoint to
    the #1749 terraform, and the coupling is total.

    Before the floor, the exemption was an improvement: a caller could send ~160
    bars and get a 200 at the edge without it. With the floor, the SMALLEST body
    the schema accepts — 250 rows, most-compact JSON, no whitespace — is already
    larger than ``SizeRestrictions_BODY``'s 8,192-byte inspection limit. There is
    no longer any legal request that survives the ALB with the managed rule in
    block mode: the endpoint is not degraded without the exemption, it is dead.

    So the ordering is not a preference:

        `infra/waf.tf` carries the exemption on main, but terraform state is not
        the repo. **Do not deploy the 250-bar floor to production until
        `infra/apply.sh` has been applied** (plan first; expect one in-place
        change to ``aws_wafv2_web_acl.main``), and confirm with a real 250-row
        POST through the edge — a `403` whose body is HTML from `awselb/2.0` is
        the WAF, not FastAPI. Deploying in the other order takes
        `POST /api/rigor/verify` from partially working to answering nothing.

    This test cannot see terraform state and does not pretend to. It pins the
    fact that makes the ordering matter, so the coupling is discoverable from
    the repo instead of living only in a PR body.
    """
    smallest = len(json.dumps({"returns": _rows(_MIN_RETURN_ROWS), "trials": 1}, separators=(",", ":")).encode())
    assert smallest > REGIONAL_BODY_INSPECTION_LIMIT, (
        f"the minimum accepted body is {smallest} B, at or under the {REGIONAL_BODY_INSPECTION_LIMIT} B "
        "edge limit. If the floor moved back below the crossover, some requests survive the ALB "
        "without the exemption again — re-read this test's docstring before relaxing it."
    )
    assert smallest > 10_000, f"the minimum accepted body measured {smallest} B, outside the measured band"


def test_a_decade_of_daily_bars_fits_under_the_cap():
    assert _MAX_RETURN_ROWS == 2600
    assert _MAX_RETURN_ROWS >= 10 * 252, "the cap must admit ten years of daily bars"
    payload = len(json.dumps({"returns": _rows(_MAX_RETURN_ROWS), "trials": 1}, separators=(",", ":")).encode())
    assert 118_000 < payload < 126_000, (
        f"a full-cap payload is {payload} B; the ~122 KB measured claim in the code comment and the PR body is wrong"
    )


def test_schema_accepts_one_trading_year():
    model = RigorVerifyRequest(returns=_rows(252), trials=1)
    assert len(model.returns) == 252


def test_schema_accepts_exactly_the_cap():
    assert len(RigorVerifyRequest(returns=_rows(_MAX_RETURN_ROWS)).returns) == _MAX_RETURN_ROWS


def test_schema_rejects_one_row_over_the_cap_with_a_message_that_names_the_limit():
    with pytest.raises(ValidationError) as excinfo:
        RigorVerifyRequest(returns=_rows(_MAX_RETURN_ROWS + 1))
    message = str(excinfo.value)
    assert str(_MAX_RETURN_ROWS) in message
    assert str(_MAX_RETURN_ROWS + 1) in message, "the error must say how many rows were sent"


def test_length_bounds_are_declared_on_the_field_as_the_backstop():
    """The validator produces the message; min/max_length are the contract in the OpenAPI schema.

    ``minItems`` moved 1 -> ``_MIN_RETURN_ROWS`` with #1803: the endpoint
    refuses a series shorter than the minimum evaluation window (250 daily
    bars, one trading year — the owner's call) instead of answering 200 with a
    verdict nothing evaluable stands behind, and the published schema has to
    say so rather than advertise a length the server rejects.
    """
    schema = RigorVerifyRequest.model_json_schema()
    assert schema["properties"]["returns"]["maxItems"] == _MAX_RETURN_ROWS
    assert schema["properties"]["returns"]["minItems"] == _MIN_RETURN_ROWS
    assert _MIN_RETURN_ROWS == 250, "the window is one trading year"
    assert _MIN_RETURN_ROWS >= DSR_MIN_BARS, (
        "the product window may sit above the gate's own sample floor, never below it"
    )
    # The schema is the contract a generated client sees; the window has to be
    # legible there, not only in the refusal message.
    assert "window" in schema["properties"]["returns"]["description"].lower()


# ── 5. …and returns a 422 over HTTP, not a truncation or a silent accept ──


@pytest.fixture()
def app():
    application = FastAPI()
    application.middleware("http")(account_auth.better_auth_session_middleware)
    application.include_router(rigor_verify_routes.rigor_verify_router)
    return application


def _sign_in(monkeypatch, user_id: str = "user-1"):
    async def fetch(_request):
        return {
            "user": {"id": user_id, "name": user_id, "email": f"{user_id}@example.com", "emailVerified": True},
            "session": {"id": f"s-{user_id}", "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        }

    monkeypatch.setattr(account_auth, "_fetch_session", fetch)


@pytest.mark.asyncio
async def test_over_cap_payload_is_a_422_over_http(app, monkeypatch):
    _sign_in(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"cookie": "better-auth.session_token=opaque", "host": "archimedes-arc.com"},
    ) as client:
        resp = await client.post("/api/rigor/verify", json={"returns": _rows(_MAX_RETURN_ROWS + 1), "trials": 1})
    assert resp.status_code == 422
    assert str(_MAX_RETURN_ROWS) in json.dumps(resp.json())
