"""Lane 1.3c: never charge for a brief we can cheaply reject.

Before this, a gibberish brief only surfaced BRIEF_INVALID from the LLM
validator running INSIDE run_generation — after the caller had already paid
(payment happens in generate_routes.start_generation, before the job runs;
see generation_pipeline._validate_brief). This file pins two things:

  * ``cheap_brief_reject`` itself — the deterministic, no-LLM prelude, unit
    tested directly (empty / too-short / keyboard-mash rejected; coherent-
    but-vague, finance-signal, and ticker-list intents pass through to the
    real validator, matching the LLM system prompt's own worked examples).
  * the ROUTE WIRING — POST /api/generate/start returns 422 with the honest
    BRIEF_INVALID shape for a gibberish brief, and — the load-bearing
    assertion — the payment dependency is never invoked and nothing is
    enqueued. Same harness as test_generate_payment_gate.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.agents.generation_pipeline import GenerateBrief, _validate_brief, cheap_brief_reject
from archimedes.services.brief_screen import Surface, screen
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.auth_helpers import auth_cookies

RECIPIENT = "0x00000000000000000000000000000000000000a1"

# Deliberately unambiguous keyboard-mash: no everyday word, no finance-signal
# word, not a plausible ticker list (lowercase, >5 chars each).
_GIBBERISH_BODY = {"brief": {"intent": "zxcvbnm qwiopasd lkjhgfdsa", "risk_appetite": "moderate"}}


@pytest.fixture(autouse=True)
def _paid_tier_only(monkeypatch):
    """Switch the #1643 free allowance OFF for this whole file.

    The ordering these tests pin is "cheap brief rejection runs BEFORE the
    payment gate", and the flag-on tests below prove it by showing a valid
    brief actually reaching the paywall. Under the default free allowance the
    valid brief would be served free instead and that half of the ordering
    proof would evaporate. ``FREE_GENERATIONS_PER_ACCOUNT=0`` restores the
    pre-#1643 gate exactly; the free path is covered in
    ``test_free_generation_gate.py``.
    """
    monkeypatch.setenv("FREE_GENERATIONS_PER_ACCOUNT", "0")


def _client() -> TestClient:
    from archimedes.main import app

    return TestClient(app)


def _mock_store() -> MagicMock:
    store = MagicMock()
    store.enqueue = AsyncMock(return_value="job-should-not-exist")
    return store


def _close_background_coroutine(coro):
    coro.close()
    return MagicMock()


def _harness(store):
    return (
        patch("archimedes.api.generate_routes.get_job_store", return_value=store),
        patch("archimedes.api.generate_routes.asyncio.create_task", side_effect=_close_background_coroutine),
    )


# ── cheap_brief_reject — unit tests ─────────────────────────────────────────


def test_empty_intent_cannot_even_be_constructed():
    """Stronger than the pre-#1801 contract this replaces.

    ``intent`` now carries ``min_length=1``, so an empty brief is refused by
    the request schema before any handler code runs; ``cheap_brief_reject``
    never sees one. The screen still owns ``shape.empty`` for whitespace-only
    text (below) and for callers that build the string themselves.
    """
    with pytest.raises(ValidationError):
        GenerateBrief(intent="")
    assert screen("", Surface.BRIEF).code == "shape.empty"


def test_whitespace_only_intent_is_rejected():
    result = cheap_brief_reject(GenerateBrief(intent="   \t\n  "))
    assert result is not None
    assert result["code"] == "shape.empty"


def test_too_short_intent_is_rejected():
    result = cheap_brief_reject(GenerateBrief(intent="ab"))
    assert result is not None


def test_keyboard_mash_is_rejected():
    result = cheap_brief_reject(GenerateBrief(intent="zxcvbnm qwiopasd lkjhgfdsa"))
    assert result is not None
    assert result["reason"]
    assert result["hint"]


@pytest.mark.parametrize(
    ("intent", "why"),
    [
        ("zxcvbnm qwrtplk", "no vowel at all"),
        ("lkjhgfdsa mnbvcxz", "5+ consonants with no break"),
        ("aaaargh bbbb", "same character 3+ times"),
        ("asdf jkli qwer", "straight runs along a keyboard row"),
    ],
)
def test_each_mash_signal_is_rejected(intent, why):
    """One case per mash signal, so a regression names which rule broke."""
    assert cheap_brief_reject(GenerateBrief(intent=intent)) is not None, why


def test_pure_symbols_is_rejected():
    result = cheap_brief_reject(GenerateBrief(intent="!!! ### $$$ 1234"))
    assert result is not None


def test_finance_signal_word_defers_to_llm():
    # "momentum" alone is enough — cheap check must not overreach into
    # judging whether the FULL sentence is a coherent strategy ask.
    assert cheap_brief_reject(GenerateBrief(intent="crypto with momentum")) is None


def test_vague_but_coherent_bond_alternative_defers_to_llm():
    # Straight from the validator's own system-prompt example of a VALID
    # (if vague) brief — the cheap check must not reject it.
    assert cheap_brief_reject(GenerateBrief(intent="low-vol bond alternative")) is None


def test_ticker_list_defers_to_llm():
    # All-caps short tokens with no vowels required (BTC has none) must not
    # be flagged as gibberish — tickers commonly lack recognizable "words".
    assert cheap_brief_reject(GenerateBrief(intent="BTC ETH SOL")) is None


def test_single_unrecognized_token_defers_to_llm():
    # A single odd token could be a real (unlisted) word or slang; only
    # reject once there are enough unrecognized tokens to be confident.
    assert cheap_brief_reject(GenerateBrief(intent="degen")) is None


def test_off_topic_but_grammatical_text_defers_to_llm():
    # "add flour and bake at 350F" — off-topic (a recipe), but grammatical.
    # Judging topicality is the LLM's job, not this heuristic's: the cheap
    # check must not weaken/duplicate-drift what the real validator decides.
    assert cheap_brief_reject(GenerateBrief(intent="add flour and bake at 350F")) is None


# ── False positives: the failure mode that actually costs us a customer ─────
#
# Every brief below is a REAL strategy ask built entirely out of words the
# module's two hand-written vocab lists do not contain. An earlier revision
# rejected exactly these, because it treated "no recognized vocabulary" as
# proof of gibberish. It is not: unfamiliar vocabulary is the normal case,
# and a false positive here refuses a paying user before the paywall — the
# one outcome this check is not allowed to produce. Rejection now requires
# positive evidence of mashing (no vowel / consonant pile-up / repeated char
# / keyboard-row run), which none of these have.


@pytest.mark.parametrize(
    "intent",
    [
        "SPY covered calls",
        "muni ladder",
        "sector rotation",
        "covered call overlay on dividend aristocrats",
    ],
)
def test_real_briefs_made_of_unlisted_words_defer_to_llm(intent):
    assert cheap_brief_reject(GenerateBrief(intent=intent)) is None


@pytest.mark.parametrize(
    "intent",
    [
        "estrategia de baja volatilidad para bonos",  # Spanish — ASCII, unlisted
        "стратегия низкой волатильности",  # Cyrillic
        "低波动率债券替代方案",  # CJK — one token, no spaces
    ],
)
def test_non_english_briefs_defer_to_llm(intent):
    """A non-English brief has no structure this heuristic can read, so it
    must always fall through to the LLM. Two ways it could wrongly reject:
    ASCII-but-unlisted words (Spanish), and non-ASCII scripts — which must
    also survive TOKENIZATION, or a Cyrillic/CJK brief looks letter-free and
    gets refused for 'not containing any words'."""
    assert cheap_brief_reject(GenerateBrief(intent=intent)) is None


# ── _validate_brief — the shared prelude fires without ever touching the LLM ─


async def test_validate_brief_rejects_gibberish_without_calling_the_llm_backend():
    """The real validator's OWN prelude (shared with the route gate) must
    catch this before it ever constructs an LLM backend — proves the two
    call sites use the same function, not a duplicated/drifted copy."""
    with patch(
        "archimedes.services.llm_backend.make_llm_backend",
        side_effect=AssertionError("the LLM backend must never be constructed for a cheaply-rejectable brief"),
    ):
        result = await _validate_brief(GenerateBrief(intent="zxcvbnm qwiopasd lkjhgfdsa"))
    assert result["is_valid"] is False
    assert result["reason"]
    assert result["hint"]


# ── Route wiring: POST /api/generate/start ──────────────────────────────────


def test_gibberish_brief_is_422_before_payment_and_settlement(monkeypatch) -> None:
    """The guard this issue is about: a gibberish brief must be refused with
    422 BEFORE the payment gate runs — no settlement attempted, nothing
    enqueued. Payment is ON (mirrors test_generate_payment_gate.py) so this
    actually exercises the ordering, not a vacuously-skipped gate."""
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")

    store = _mock_store()
    p1, p2 = _harness(store)
    paywall_spy = AsyncMock(side_effect=AssertionError("payment must never be invoked for a cheaply-rejected brief"))
    with (
        p1,
        p2,
        patch("archimedes.api.generate_routes.generation_payment.enforce_generation_payment", paywall_spy),
    ):
        resp = _client().post("/api/generate/start", json=_GIBBERISH_BODY, cookies=auth_cookies())

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "brief_invalid"
    assert detail["code"] == "BRIEF_INVALID"
    assert "message" in detail and detail["message"]
    assert "hint" in detail and detail["hint"]

    paywall_spy.assert_not_called()
    store.enqueue.assert_not_called()


def test_valid_brief_still_reaches_the_paywall(monkeypatch) -> None:
    """Negative control: a normal brief must NOT be caught by the new gate —
    it still reaches the payment step exactly as before (402, since no
    Payment-Signature header is presented)."""
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")

    store = _mock_store()
    p1, p2 = _harness(store)
    body = {"brief": {"intent": "low-vol treasury alternative", "risk_appetite": "moderate"}}
    with p1, p2:
        resp = _client().post("/api/generate/start", json=body, cookies=auth_cookies())

    assert resp.status_code == 402
    assert resp.json()["detail"]["reason"] == "payment_required"
    store.enqueue.assert_not_called()


def test_brief_of_only_unlisted_words_still_reaches_the_paywall(monkeypatch) -> None:
    """The false-positive guard at the ROUTE, where the money is.

    "SPY covered calls" contains no word in either vocab list, so an earlier
    revision of the cheap check refused it with 422 — a real customer turned
    away before ever being shown a price. It must reach the paywall (402)
    like any other brief. Distinct from the test above, whose intent hits
    the recognized-vocabulary fast path and so never exercises this."""
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")

    store = _mock_store()
    p1, p2 = _harness(store)
    body = {"brief": {"intent": "SPY covered calls", "risk_appetite": "moderate"}}
    with p1, p2:
        resp = _client().post("/api/generate/start", json=body, cookies=auth_cookies())

    assert resp.status_code == 402, f"a legitimate brief was refused pre-payment: {resp.json()}"
    assert resp.json()["detail"]["reason"] == "payment_required"
    store.enqueue.assert_not_called()
