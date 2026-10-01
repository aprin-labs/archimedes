"""The static discovery document and the served manifest must agree (#1448).

Two surfaces describe the same API to an agent:

- ``ui/public/.well-known/agent.json`` — the static discovery document, fetched
  straight off the CDN at a well-known path;
- ``GET /api/agent/manifest`` — the served manifest, built in
  ``api/agent_manifest_routes.py``.

    They drifted. PR #1447 corrected the served manifest's ``deploy`` /
    ``marketplace`` / ``monitor`` groups from "landing with the T3.2 redeploy
    (#588)" to ``live``, because the redeploy landed 2026-07-09 and #588 closed
    2026-07-14. The static file kept asserting the stale state in seven places, so
    an agent that discovered us the well-known way was told three live capabilities
    were unavailable — for over a month.

    The 2026-09-01 copy-honesty pass then moved those three groups from ``live``
    to ``roadmap``: the routes resolve, but marketplace is not a public surface
    and no user vault has ever been created. This test still connects the two
    files. The value they must agree on is now ``roadmap``, not ``live``.

Nothing connected the two files, so nothing noticed. This test is that
connection: the drift is the defect, not either value on its own.

Statuses only. Route KEYS deliberately differ between the surfaces — the static
document is camelCase (``createVault``) and the served one is snake_case
(``create_vault``) — and forcing those to match would be a different, larger
change than keeping the two honest about what works.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_MANIFEST = REPO_ROOT / "ui" / "public" / ".well-known" / "agent.json"


def _static_statuses() -> dict[str, str]:
    doc = json.loads(STATIC_MANIFEST.read_text(encoding="utf-8"))
    return {
        name: group["status"]
        for name, group in doc["endpoints"].items()
        if isinstance(group, dict) and "status" in group
    }


async def _served_statuses() -> dict[str, str]:
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")
    assert resp.status_code == 200, resp.text
    return {
        name: group["status"]
        for name, group in resp.json()["endpoints"].items()
        if isinstance(group, dict) and "status" in group
    }


async def _manifest() -> dict:
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_both_surfaces_describe_the_same_capability_groups():
    """A group on one surface and not the other is drift too, not just a status gap."""
    static, served = _static_statuses(), await _served_statuses()
    assert set(static) == set(served), (
        f"only in the static document: {sorted(set(static) - set(served))}; "
        f"only in the served manifest: {sorted(set(served) - set(static))}"
    )


async def test_the_two_manifests_agree_on_every_group_status():
    static, served = _static_statuses(), await _served_statuses()
    disagreements = {
        name: (static[name], served[name]) for name in set(static) & set(served) if static[name] != served[name]
    }
    assert not disagreements, (
        "the static discovery document and the served manifest disagree about what works:\n"
        + "\n".join(
            f"  {name}: .well-known/agent.json says {s!r}, /api/agent/manifest says {v!r}"
            for name, (s, v) in sorted(disagreements.items())
        )
        + "\nAn agent's answer would depend on which surface it happened to read."
    )


async def test_the_comparison_actually_covers_the_groups_that_drifted():
    """Guard on the guard: an empty or tiny comparison would pass vacuously."""
    static = _static_statuses()
    assert len(static) >= 8, f"only {len(static)} static groups parsed — the reader is broken"
    for group in ("deploy", "marketplace", "monitor", "paper"):
        assert group in static, f"{group} not compared — it is one of the groups that drifted"


@pytest.mark.parametrize("group", ["deploy", "marketplace", "monitor"])
def test_the_groups_that_were_stale_are_not_still_advertised_as_pending(group: str):
    """The specific false claim, pinned by value.

    Distinct from the agreement test above on purpose: that one would also pass
    if BOTH surfaces regressed to the stale string together.
    """
    status = _static_statuses()[group]
    assert "588" not in status, f"{group} still cites #588, which closed 2026-07-14"
    assert status == "roadmap", (
        f"{group} advertises {status!r}; deploy / marketplace / monitor are not "
        "a public surface (no user vault, marketplace is not shipped)"
    )


def test_marketplace_skills_are_not_advertised_as_live():
    """Marketplace is not a public surface. Skills that told an agent it could
    publish or subscribe as a live journey were the same over-claim as
    ``endpoints.marketplace.status: "live"``."""
    doc = json.loads(STATIC_MANIFEST.read_text(encoding="utf-8"))
    skills = {s["id"]: s for s in doc["skills"]}
    for skill_id in ("publish", "subscribe"):
        status = skills[skill_id]["status"]
        assert status == "roadmap", f"skills[{skill_id}].status is {status!r}, not 'roadmap'"


def _static_chain_ids() -> list[int]:
    """Every chain id the static document advertises, wherever it appears."""
    doc = json.loads(STATIC_MANIFEST.read_text(encoding="utf-8"))
    found: list[int] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("chain_id", "chainId", "walletLinkChainId") and isinstance(value, int):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return found


def test_the_static_document_advertises_the_chain_the_backend_enforces():
    """The static file cannot read env, so it must not be free to disagree (#1240).

    ``agent_manifest_routes._CHAIN_ID`` reads ``ARC_CHAIN_ID`` — as do
    ``wallet_routes`` and ``auth_siwe`` — while ``.well-known/agent.json`` is
    served straight off the CDN and can only ever hold a literal. The
    achievable property is therefore not "the static file is configurable" but
    "it cannot silently drift from the value the backend enforces", which is
    what this pins.

    A real cutover has to edit this file. That is the point: the edit becomes a
    required, visible step instead of a discrepancy nobody notices, which is
    exactly how the #1448 status drift survived a month.

    Fails against changing ARC_CHAIN_ID's default without updating the static
    document, and against adding a new chain-carrying field that disagrees.
    """
    from archimedes.api import agent_manifest_routes

    advertised = _static_chain_ids()
    assert advertised, "no chain id found in the static document — the reader is broken"
    disagreeing = sorted({c for c in advertised if c != agent_manifest_routes._CHAIN_ID})
    assert not disagreeing, (
        f".well-known/agent.json advertises chain(s) {disagreeing} but the backend enforces "
        f"{agent_manifest_routes._CHAIN_ID}. An agent reading the well-known path would link a "
        f"wallet on the wrong chain."
    )


def test_the_chain_id_reader_sees_every_structured_field():
    """Guard on the guard: a reader that found nothing would pass vacuously.

    The document names a chain in two structured shapes — `walletLinkChainId`
    at the top level and a nested `chain_id` — so a reader that only checked
    the top level would let the nested one drift.
    """
    advertised = _static_chain_ids()
    assert len(advertised) >= 2, (
        f"only {len(advertised)} chain id(s) parsed; the document names more. "
        "A partial reader would let a nested one drift."
    )


async def test_the_two_surfaces_carry_the_same_prose_description():
    """Statuses were pinned in #1448; the SENTENCE was not, and it drifted next.

    ``agent.json``'s ``description`` and the manifest's ``blurb`` are the same
    sentence served two ways, and both said a strategy is "executed in a
    non-custodial USDC vault on the Arc testnet" — present tense, on the two
    surfaces built for agent consumers, while no user vault has ever been
    created (#1650). Fixing one and not the other would leave an agent's answer
    depending on which surface it happened to read, which is the exact defect
    the status checks above exist to prevent.

    Equality, not substring: a served blurb that merely *contains* the static
    description would let the served one append a claim of its own.
    """
    static = json.loads(STATIC_MANIFEST.read_text(encoding="utf-8"))["description"]
    served = (await _manifest())["blurb"]
    assert served == static, (
        "the static agent card and the served manifest describe the product differently:\n"
        f"  .well-known/agent.json: {static!r}\n"
        f"  /api/agent/manifest:    {served!r}"
    )


@pytest.mark.parametrize("surface", ["static", "served"])
async def test_neither_surface_claims_present_tense_vault_execution(surface: str):
    """#1650's acceptance, pinned by value on both surfaces.

    Separate from the equality test on purpose: that one also passes if BOTH
    surfaces regress to the old sentence together — the same reasoning as
    ``test_the_groups_that_were_stale_are_not_still_advertised_as_pending``.

    The positive half matters as much as the negative one. #1650's anti-goal is
    that the vault roadmap mention must SURVIVE ("future tense is honest and
    good marketing"), so deleting the sentence is not a fix and does not pass
    here.
    """
    if surface == "static":
        text = json.loads(STATIC_MANIFEST.read_text(encoding="utf-8"))["description"]
    else:
        text = (await _manifest())["blurb"]

    assert "executed in" not in text.lower(), (
        f"the {surface} surface states present-tense vault execution again: {text!r}. "
        "No user vault has ever been created and the journey is gated off every shipped surface."
    )
    assert "is roadmap, not shipped" in text, (
        f"the {surface} surface no longer frames vault execution as roadmap: {text!r}. "
        "Do not delete the mention — state it in the future tense (#1650 anti-goal)."
    )


def test_the_prose_description_does_not_name_a_different_chain():
    """The description says the chain in words, and words drift too (#1240).

    `description` currently reads "... on the Arc testnet (chain ID 5042002)".
    That is a claim an agent reads, and it is not covered by the structured
    check above, so at a cutover it could keep naming testnet while every field
    around it moved. Checked separately rather than folded in, because a test
    that passed once the structured fields were fixed would leave the sentence
    saying something false.
    """
    import re

    from archimedes.api import agent_manifest_routes

    doc = json.loads(STATIC_MANIFEST.read_text(encoding="utf-8"))
    described = [int(m) for m in re.findall(r"chain ID (\d+)", doc.get("description", ""))]
    assert described, "the description no longer names a chain — update or remove this guard"
    wrong = sorted({c for c in described if c != agent_manifest_routes._CHAIN_ID})
    assert not wrong, (
        f"the description names chain(s) {wrong} while the backend enforces {agent_manifest_routes._CHAIN_ID}"
    )


# --- The minimum evaluation window, on both surfaces (#1803) --------------------
#
# The window landed on the static document only. The served manifest kept a
# ``verdict_note`` that described the quorum and said nothing about the 250-bar
# floor, so an agent that discovered the API through ``GET /api/agent/manifest``
# — the surface the product actually serves — would build a 60-bar body, get a
# 422, and have been told nothing that predicted it. Same class as the #1448
# status drift these tests exist for: one surface corrected, the other not.
#
# Bound to the enforced constant rather than to the literal 250 on purpose: the
# floor is an owner decision that can move, and when it does, a guard that only
# pins a number would go green on a manifest that still advertises the old one.


def _enforced_window() -> int:
    from archimedes.api import rigor_verify_routes

    return rigor_verify_routes._MIN_RETURN_ROWS


def _static_rigor_note() -> str:
    return json.loads(STATIC_MANIFEST.read_text(encoding="utf-8"))["endpoints"]["rigor"]["note"]


async def _served_rigor_note() -> str:
    return (await _manifest())["endpoints"]["rigor"]["verdict_note"]


@pytest.mark.parametrize("surface", ["static", "served"])
async def test_both_discovery_surfaces_state_the_minimum_evaluation_window(surface: str):
    """The number AND the reason code, on whichever surface an agent happens to read."""
    note = _static_rigor_note() if surface == "static" else await _served_rigor_note()
    window = _enforced_window()
    assert str(window) in note, (
        f"the {surface} discovery surface does not state the {window}-bar minimum evaluation "
        f"window: {note!r}. An agent builds its body from this note, not from the docs tree."
    )
    assert "window_too_short" in note, (
        f"the {surface} discovery surface does not name the `window_too_short` reason code: "
        f"{note!r}. Branching on detail.reason is what the note exists to make possible."
    )


async def test_neither_surface_advertises_a_window_the_route_does_not_enforce():
    """A stale number is worse than no number: it is a body an agent will build and lose.

    Catches the reverse drift of the test above — the floor moves in
    ``rigor_verify_routes`` and the notes keep quoting the old one.
    """
    import re

    window = _enforced_window()
    for surface, note in (("static", _static_rigor_note()), ("served", await _served_rigor_note())):
        quoted = [int(m) for m in re.findall(r"(\d+) daily bars", note)]
        assert quoted, f"the {surface} surface no longer states a bar count — update or remove this guard"
        wrong = sorted({n for n in quoted if n != window})
        assert not wrong, (
            f"the {surface} discovery surface advertises a {wrong}-bar window while "
            f"POST /api/rigor/verify enforces {window}."
        )
