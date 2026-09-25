"""The ERC-8004 identity leg must never claim more than it has (#1527).

Four surfaces describe this agent's on-chain identity:

- ``GET /api/agent/manifest`` → ``erc8004`` (built by ``agent_manifest_routes.erc8004_identity``)
- ``ui/public/.well-known/agent.json`` → ``erc8004`` (static, CDN-served)
- ``ui/public/.well-known/agent-registration.json`` — the spec-typed registration file
  an ``agentURI`` would resolve to
- ``ui/public/.well-known/agent-registration.domain.json`` — the EIP's optional
  domain-ownership variant, for a second endpoint-domain

#1448 is the precedent for why they are checked against each other rather than each
against a hand-kept list: the served manifest and the static card drifted for over a
month because nothing connected them.

Since #1527's identity leg landed there is a fifth thing to check, and it is the one that
makes the word "registered" mean anything: ``GET /api/agent/manifest`` →
``erc8004_verification``, the record of the live ``ownerOf()`` read the served status was
derived from. The status is no longer a constant somebody could edit — it is a reading —
so the tests below check the reading and the claim against each other.

The load-bearing property here is narrower than consistency, though. **Registration is
an owner-signed on-chain transaction this repo cannot perform.** Until it lands there is
no agentId and no tokenURI, and the honest word is ``registration_pending``. The
invariants below are written so they keep holding *after* a real registration — they
assert the surfaces move together, not that the values are frozen — except for one
deliberately frozen assertion at the bottom that fails the day the state changes, so the
change has to be made on purpose.

Hermetic: TESTING=1 ASGI call into ``archimedes.main`` (conftest sets it) plus reading
four committed JSON files. No DB / Redis / RPC / network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from httpx import ASGITransport, AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[2]
WELL_KNOWN = REPO_ROOT / "ui" / "public" / ".well-known"
AGENT_CARD = WELL_KNOWN / "agent.json"
REGISTRATION_FILE = WELL_KNOWN / "agent-registration.json"
DOMAIN_VARIANT = WELL_KNOWN / "agent-registration.domain.json"

# The exact string ERC-8004 requires. Not a prefix match, not a substring: a file typed
# with a near-miss is a file no ERC-8004 consumer will accept.
SPEC_TYPE = "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"

# The five keys #1527 specifies, plus the two this repo adds (registrationUri, note).
# Frozen so a key cannot be dropped from one surface and quietly kept on another.
IDENTITY_KEYS = {"chain", "identityRegistry", "agentId", "tokenURI", "status", "registrationUri", "note"}

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CAIP2_RE = re.compile(r"^eip155:[1-9][0-9]*$")

# Words that, said about ourselves, are the exact overclaim this issue's anti-goal names.
_CLAIM_WORDS = ("registered", "verified", "validated", "attested")


def _json(path: Path) -> dict:
    assert path.exists(), f"{path} is missing — the surface it backs would 404"
    return json.loads(path.read_text(encoding="utf-8"))


async def _manifest() -> dict:
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")
    assert resp.status_code == 200
    return resp.json()


def _all_identity_blocks() -> dict[str, dict]:
    """Every STATIC surface's identity block, keyed by filename for readable failures."""
    return {
        AGENT_CARD.name: _json(AGENT_CARD)["erc8004"],
        REGISTRATION_FILE.name: _json(REGISTRATION_FILE)["erc8004"],
        DOMAIN_VARIANT.name: _json(DOMAIN_VARIANT)["erc8004"],
    }


# ── shape ────────────────────────────────────────────────────────────────────


async def test_manifest_carries_a_well_shaped_erc8004_block():
    """The served block exists, has exactly the agreed keys, and is typed correctly."""
    block = (await _manifest())["erc8004"]

    assert set(block) == IDENTITY_KEYS, f"unexpected key set: {sorted(block)}"
    assert _CAIP2_RE.match(block["chain"]), f"chain must be an eip155 CAIP-2 id, got {block['chain']!r}"
    assert _ADDRESS_RE.match(block["identityRegistry"]), block["identityRegistry"]
    assert block["agentId"] is None or (isinstance(block["agentId"], int) and not isinstance(block["agentId"], bool))
    assert block["tokenURI"] is None or isinstance(block["tokenURI"], str)
    assert block["status"] in {"registration_pending", "registered"}
    assert block["registrationUri"].startswith("https://")
    assert block["registrationUri"].endswith("/.well-known/agent-registration.json")


async def test_manifest_chain_is_derived_from_the_configured_arc_chain_id():
    """The CAIP-2 id must be BUILT from ``_CHAIN_ID``, not a parallel literal —
    the same rule ``auth.chain_id`` already lives under."""
    from archimedes.api.agent_manifest_routes import _CHAIN_ID

    block = (await _manifest())["erc8004"]
    assert block["chain"] == f"eip155:{_CHAIN_ID}"
    assert block["chain"].split(":")[1] == str((await _manifest())["auth"]["chain_id"])


# ── cross-surface agreement ──────────────────────────────────────────────────


async def test_the_three_committed_surfaces_are_byte_for_byte_identical():
    """Agent card, registration file and domain variant must carry the SAME block.

    Two discovery documents that disagree about whether an agent is registered are worse
    than one — a consumer has no way to tell which is stale. These three are all committed
    JSON, so there is no legitimate reason for any daylight between them.
    """
    blocks = _all_identity_blocks()
    reference_name, reference = next(iter(blocks.items()))
    for name, block in blocks.items():
        assert block == reference, (
            f"{name} disagrees with {reference_name}.\n"
            f"  {name}: {json.dumps(block, sort_keys=True)}\n"
            f"  {reference_name}: {json.dumps(reference, sort_keys=True)}\n"
            "Regenerate both from agent_manifest_routes.erc8004_identity()."
        )


async def test_the_served_manifest_never_claims_more_than_the_committed_surfaces():
    """The manifest may be WEAKER than the committed files, never stronger (#1527).

    This is the one deliberate asymmetry in the four-surface agreement, and it arrived with
    the live read. ``status`` / ``agentId`` / ``tokenURI`` on the manifest come from an
    ``ownerOf()`` call made while serving the request; a committed JSON file cannot make a
    call. So when that read does not complete — CI has no RPC, and neither does a VPC with
    a dark endpoint — the served block drops to ``registration_pending`` while the
    committed files keep publishing the registration that really happened.

    That divergence is a refusal to claim, and a refusal to claim is not the #1448 drift:
    the direction is what matters. Configuration fields still have to match exactly (a
    manifest advertising a different registry than the card is drift in the old sense), and
    the manifest is never permitted to assert a registration the committed record does not
    carry.
    """
    manifest = await _manifest()
    served = manifest["erc8004"]
    verification = manifest["erc8004_verification"]
    committed = _json(AGENT_CARD)["erc8004"]

    for field in ("chain", "identityRegistry", "registrationUri"):
        assert served[field] == committed[field], (
            f"{field} differs between the served manifest ({served[field]!r}) and the agent "
            f"card ({committed[field]!r}). These are configuration, not readings — they must match."
        )

    if served["status"] == "registered":
        # Stronger than the committed record is never allowed: the id we serve has to be
        # the id we published, and the committed files carry the registrations entry.
        assert committed["status"] == "registered", (
            "the served manifest claims a registration the committed agent card does not. "
            "Land the registrations entries and the regenerated card in the same commit."
        )
        assert served["agentId"] == committed["agentId"]
    else:
        # Weaker is allowed, and must be explained by the verification record rather than
        # being silent — a pending status with no reason attached is the fail-soft shape.
        assert verification["source"] in {"onchain", "unconfigured", "unavailable"}, verification
        assert verification["detail"], "a pending status must say what produced it"


def test_registration_uri_points_at_the_file_this_repo_actually_publishes():
    """A tokenURI that resolves to nothing is a registration that proves nothing."""
    block = _json(AGENT_CARD)["erc8004"]
    assert block["registrationUri"].endswith(f"/.well-known/{REGISTRATION_FILE.name}")
    assert REGISTRATION_FILE.exists()


# ── the registration files ───────────────────────────────────────────────────


def test_registration_files_are_spec_typed_json():
    for path in (REGISTRATION_FILE, DOMAIN_VARIANT):
        doc = _json(path)
        assert doc["type"] == SPEC_TYPE, f"{path.name} has type {doc.get('type')!r}, expected {SPEC_TYPE!r}"
        assert isinstance(doc["registrations"], list), f"{path.name}: registrations must be a list"


def test_primary_registration_file_carries_the_fields_a_consumer_reads():
    """The EIP's registration-file fields, with types an ERC-8004 client can rely on."""
    doc = _json(REGISTRATION_FILE)
    assert doc["name"] == "Archimedes"
    assert isinstance(doc["description"], str) and doc["description"]
    assert doc["image"].startswith("https://")
    assert isinstance(doc["active"], bool)
    assert isinstance(doc["x402Support"], bool)
    assert isinstance(doc["supportedTrust"], list)

    services = doc["services"]
    assert services, "a registration file with no services describes no agent"
    for service in services:
        assert set(service) == {"name", "endpoint"}, service
        assert service["endpoint"].startswith("https://"), service

    # The services must include the two surfaces this document is discovered through,
    # or a consumer that starts from the registry cannot find the API at all.
    endpoints = {s["endpoint"] for s in services}
    assert "https://archimedes-arc.com/api/agent/manifest" in endpoints
    assert "https://archimedes-arc.com/.well-known/agent.json" in endpoints


def test_domain_variant_is_the_minimal_file_the_eip_describes():
    """ "…containing at least a registrations list" — at least, and here exactly.

    The variant deliberately does NOT restate name/description/services: a second copy of
    those is a second thing to go stale, and the EIP asks for none of it.
    """
    doc = _json(DOMAIN_VARIANT)
    assert set(doc) == {"type", "registrations", "erc8004", "_note"}, sorted(doc)


def test_supported_trust_claims_nothing_this_agent_has_not_earned():
    """``supportedTrust`` is an on-chain-trust-model claim. We make none.

    If a model is ever listed here it must be one the EIP defines AND one that is
    actually wired — this assertion is the place that conversation happens.
    """
    trust = _json(REGISTRATION_FILE)["supportedTrust"]
    assert trust == [], (
        f"supportedTrust claims {trust}. ERC-8004 trust models (reputation, "
        "crypto-economic, tee-attestation) are none of them implemented here; listing one "
        "advertises a guarantee a counterparty could rely on and lose money to."
    )


# ── the x402 payment claim ───────────────────────────────────────────────────
#
# ``x402Support`` shipped as ``false`` while the deployment serving this file was charging
# $2.00 a generation for real and settling it. The field is a bool, so the shape assertion
# above waved either value through — nothing connected the claim to the runtime it
# describes. That is the #1448 drift shape one level down: not two documents disagreeing
# with each other, but every document agreeing with the *source defaults* instead of with
# the deployment.
#
# The runtime reading itself cannot be pinned from inside the repo — tests are hermetic and
# a deployed flag is not in the tree — so ``X402_PRICE`` is the deliberate seam: re-reading
# ``GET /api/generate/quote`` and editing one literal is the act. What IS checkable, and is
# what these guards enforce, is that no surface prices generation differently from the
# others, and that ``true`` is backed by prose citing the endpoint it was read from.

QUICKSTART = REPO_ROOT / "docs" / "agent-quickstart.md"

# What https://archimedes-arc.com/api/generate/quote answered on 2026-08-30. Deliberately
# NOT imported from ``generation_payment.DEFAULT_PRICE_USD``: the default and the
# deployment agree on the price today and disagree on the flags, and it is the deployment
# these documents describe. Binding to the default would re-create the exact bug.
X402_PRICE = "$2.000000"

# A circlekit price string, the "$X.XXXXXX" form ``generation_payment._price`` emits.
_PRICE_RE = re.compile(r"\$\d+\.\d{6}")


def _priced_surfaces() -> dict[str, str]:
    """Every committed surface that tells a caller what a generation costs."""
    return {
        "agent-registration.json → _note.x402Support": _json(REGISTRATION_FILE)["_note"]["x402Support"],
        "agent.json → endpoints.generate.note": _json(AGENT_CARD)["endpoints"]["generate"]["note"],
        "docs/agent-quickstart.md": QUICKSTART.read_text(encoding="utf-8"),
    }


def test_every_surface_that_prices_generation_quotes_the_same_price():
    """One price, stated in three places. A fix that moves one of them is the defect."""
    for name, text in _priced_surfaces().items():
        found = set(_PRICE_RE.findall(text))
        assert found, f"{name} no longer states a price at all — it is what a caller budgets against"
        assert found == {X402_PRICE}, (
            f"{name} prices generation at {sorted(found)}, but the recorded quote is "
            f"{X402_PRICE}. Re-read GET /api/generate/quote and move ALL of "
            f"{sorted(_priced_surfaces())} plus X402_PRICE together, or one surface sends "
            "a caller to sign the wrong amount."
        )


def test_the_price_reader_actually_rejects_a_divergent_price():
    """Guard on the guard: a reader that matched nothing would pass vacuously.

    The failing input is the realistic one — a single surface edited to a new price while
    the others keep the old one, which is how the last drift happened.
    """
    assert _PRICE_RE.findall(f'"price": "{X402_PRICE}"') == [X402_PRICE]
    assert set(_PRICE_RE.findall('"price": "$0.150000"')) != {X402_PRICE}
    # Prose prices ("$2.00 testnet USDC") are not the machine-readable form and must not
    # be mistaken for a divergent quote.
    assert _PRICE_RE.findall("charges $2.00 testnet USDC per run") == []


def test_x402_support_true_is_backed_by_prose_citing_the_runtime_it_was_read_from():
    """``true`` is a claim about a deployment, so it must say which reading produced it.

    Written as an implication so it keeps guarding if the flag is ever turned back off:
    ``false`` has to be equally explicit that the paywall's absence is a deploy state and
    not a property of the code.
    """
    doc = _json(REGISTRATION_FILE)
    note = doc["_note"]["x402Support"]

    assert "/api/generate/quote" in note, (
        "the x402Support note must name the endpoint the claim was read from — it is the "
        "only thing a consumer can re-check it against."
    )
    assert "GENERATION_PAYMENT_REQUIRED" in note, (
        "the note must name the deploy flag, so a reader knows the value describes this "
        "deployment and not the repo's default."
    )

    if doc["x402Support"]:
        for required in ("payment_required: true", "dry_run: false", X402_PRICE):
            assert required in note, (
                f"x402Support is true but the note never states {required!r}. "
                "A bare true is the same unbacked claim as the bare false it replaced."
            )
    else:
        assert "payment_required: true" not in note, (
            "x402Support is false while the note quotes a live paywall — one of the two is "
            "stale. Re-read GET /api/generate/quote and fix both together."
        )


# ── the honesty invariants (these survive a real registration) ───────────────


async def test_the_served_status_is_registered_only_when_a_live_read_says_so():
    """The claim and the evidence must be the same fact, not two fields that can drift.

    ``erc8004.status`` is what a consumer acts on; ``erc8004_verification`` is why. If the
    two can ever disagree — status "registered" beside a source of "unavailable" — then the
    verification record is decoration and the status is back to being an assertion.
    """
    manifest = await _manifest()
    served = manifest["erc8004"]
    verification = manifest["erc8004_verification"]

    assert (served["status"] == "registered") == (verification["status"] == "registered"), (
        f"the block says {served['status']!r} and the verification says {verification['status']!r}"
    )
    if served["status"] != "registered":
        return
    assert verification["source"] == "onchain", (
        f"status is 'registered' but the evidence source is {verification['source']!r} — "
        "only a completed registry read may produce that claim."
    )
    assert verification["owner"] and verification["expectedOwner"]
    assert verification["owner"].lower() == verification["expectedOwner"].lower(), verification
    assert served["agentId"] == verification["agentId"]


async def test_no_surface_says_registered_while_the_agent_id_is_absent():
    """The anti-goal, enforced: agentId absent ⇒ status pending, everywhere.

    Written as an implication rather than an equality so it still holds — and still
    guards — after the owner registers for real.
    """
    surfaces = dict(_all_identity_blocks())
    surfaces["GET /api/agent/manifest"] = (await _manifest())["erc8004"]

    for name, block in surfaces.items():
        if block["agentId"] is None:
            assert block["status"] == "registration_pending", (
                f"{name} has no agentId but reports status {block['status']!r} — that is a "
                "registration claim with no registration behind it."
            )
            assert block["tokenURI"] is None, (
                f"{name} has no agentId but carries a tokenURI — nothing on-chain references it."
            )


async def test_a_registered_status_requires_a_real_id_a_uri_and_a_registrations_entry():
    """The mirror implication: claiming registered obliges every other surface to prove it.

    This is what makes a real registration a whole-commit act — the manifest, both
    registration files, and the agent card have to move together or CI fails. Setting
    ``ERC8004_AGENT_ID`` in a deployment is not enough on its own and is not supposed to
    be: the env var points the verifier at a token, and this test is what obliges the
    committed record to catch up.
    """
    served = (await _manifest())["erc8004"]
    if served["status"] != "registered":
        return  # nothing to prove today; the implication above covers the pending state

    agent_id = served["agentId"]
    assert isinstance(agent_id, int) and not isinstance(agent_id, bool) and agent_id >= 0
    assert isinstance(served["tokenURI"], str) and served["tokenURI"].startswith("https://")

    expected = {"agentId": agent_id, "agentRegistry": f"{served['chain']}:{served['identityRegistry']}"}
    for path in (REGISTRATION_FILE, DOMAIN_VARIANT):
        registrations = _json(path)["registrations"]
        assert expected in registrations, (
            f"{path.name} does not list the registered agent. ERC-8004 requires the "
            f"registration file to carry a registrations entry matching the on-chain agent; "
            f"expected {expected} in {registrations}. "
            "scripts/register_erc8004_identity.py --print-followup <agentId> prints it."
        )


def test_registrations_entries_are_never_half_filled():
    """Whatever is listed must be listable: both mandatory EIP fields, correctly typed.

    An entry with a null agentId is not a placeholder, it is a malformed registration —
    and it is exactly the shape a well-meaning edit reaches for while waiting to register.
    """
    for path in (REGISTRATION_FILE, DOMAIN_VARIANT):
        for entry in _json(path)["registrations"]:
            assert set(entry) >= {"agentId", "agentRegistry"}, f"{path.name}: {entry}"
            assert isinstance(entry["agentId"], int) and not isinstance(entry["agentId"], bool), (
                f"{path.name}: agentId must be the integer the registry minted, got {entry['agentId']!r}"
            )
            namespace, chain_id, address = entry["agentRegistry"].split(":")
            assert namespace == "eip155" and chain_id.isdigit() and _ADDRESS_RE.match(address), entry


def test_prose_on_these_surfaces_never_asserts_a_registration_we_do_not_have():
    """Guard the *words*, not just the fields.

    A status field can stay ``registration_pending`` while a note beside it reads "our
    registered agent identity" — same file, same overclaim, and no structural assertion
    catches it. Every claim word in the free text must be negated or hypothetical.
    """
    if _json(AGENT_CARD)["erc8004"]["agentId"] is not None:
        return  # once registered, "registered" is simply true

    for path in (AGENT_CARD, REGISTRATION_FILE, DOMAIN_VARIANT):
        text = path.read_text(encoding="utf-8").lower()
        for sentence in re.split(r"(?<=[.;])\s+", text):
            for word in _CLAIM_WORDS:
                if word not in sentence:
                    continue
                assert any(
                    marker in sentence
                    for marker in ("not ", "no ", "never", "until", "once", "pending", "would", "empty", "cannot")
                ), (
                    f"{path.name} says {word!r} in a sentence that reads as a claim, while "
                    f"agentId is null:\n  {sentence.strip()[:300]}"
                )


# ── the one frozen assertion ─────────────────────────────────────────────────


async def test_the_shipped_state_is_pending():
    """Today, on main, nothing is registered. Frozen on purpose.

    If this fails because a real ``register()`` landed: good — update this test in the
    SAME commit as the transaction hash and the ``registrations`` entries, and leave the
    implication tests above untouched. If it fails for any other reason, someone claimed
    a registration that did not happen.
    """
    block = (await _manifest())["erc8004"]
    assert block["status"] == "registration_pending"
    assert block["agentId"] is None
    assert block["tokenURI"] is None
    assert _json(REGISTRATION_FILE)["registrations"] == []
    assert _json(DOMAIN_VARIANT)["registrations"] == []
