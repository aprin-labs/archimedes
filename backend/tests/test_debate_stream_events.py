"""The debate reaches the SSE stream — sanitized, complete, and only when it ran.

Owner, dogfooding a live generation on a phone: *"You still can't see the
actual reasoning traces anywhere as best I can tell."* His stream showed
``#17 Tool result — synthesize → leader=… dsr=0.082005 of 1 entries`` and
nothing of the debate itself. The four paid bull/bear turns existed on the wire
only as ``tool_called`` markers whose ``args_summary`` was ``cards[:120]`` — a
truncated evidence card, never the turn — so the proposer's argument, the
bear's rebuttal, the per-paper verdicts and the reasons papers were thrown out
were all invisible during the run and reachable afterwards only through the
owner-only ``/debate`` route.

Nothing here is newly *produced*: every byte these events carry was already
computed and already sanitized-on-write. What is new is the transport, and a
new transport is exactly where a write-time sanitization contract quietly
breaks. Hence this file.

Four guards, each with the mutation that makes it red:

1. Every ``debate_turn`` payload went through ``sanitize_transcript``.
   *Mutation:* emit the raw ``turn`` dict from ``_emit_debate_turn``.
2. Exactly four ``debate_turn`` events, in the fixed
   ``[bull-r1, bear-r1, bull-r2, bear-r2]`` order, each byte-identical to the
   turn that gets persisted. *Mutation:* emit only round 1.
3. ``debate_attribution`` is the SAME entry ``_paper_attribution_entry``
   appends for persistence — one summary sentence, one place.
   *Mutation:* reword the sentence in either place.
4. No LLM backend ⇒ zero ``debate_turn`` and zero ``debate_attribution``. An
   empty debate is never rendered as a debate that happened.

Hermetic: the LLM seam is mocked at ``make_llm_backend``, the backtest at
``evaluate_fusion_spec``, the regime read at ``current_regime``. No network, no
Redis, no DB.
"""

from __future__ import annotations

import json

import pytest
from archimedes.agents import debate_engine as de
from archimedes.agents.generation_pipeline import _CandidateResult, _paper_attribution_entry
from archimedes.agents.strategy_fusion import load_corpus
from archimedes.api.generate_schemas import GenerateBrief
from archimedes.models.debate_transcript import sanitize_transcript

from tests.test_debate_engine import (
    _ALL_ROWS,
    _EVIDENCE_BY_ID,
    _DebateBackend,
    _fake_ev,
    _fake_proposal,
    _FakeEmit,
    _passthrough_to_thread,
    _patch_regime,
)

#: The internal roadmap vocabulary `sanitize_transcript` exists to scrub. Kept
#: as three separate patterns because they hit three different regexes in
#: `models/debate_transcript._JARGON_PATTERNS`.
_JARGON = ("T3.5", "cutover", "Phase-4")


@pytest.fixture
def corpus(tmp_path):
    p = tmp_path / "manifest.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in _ALL_ROWS), encoding="utf-8")
    return load_corpus(p)


class _LeakyDebateBackend:
    """A debate backend whose every string is internal jargon.

    Verdict, claim prose and discard reason all carry it, because
    `sanitize_transcript` scrubs those three fields by three different code
    paths (`verdict` directly, `claims[]` through `_sanitize_claim`,
    `discard[]` through the same) and a guard that only probes one of them
    would pass while the other two leaked.
    """

    model_id = "x"
    served_model = "x"
    available = True

    def complete(self, system, user):
        role = "bull" if "bull researcher" in system else "bear"
        return json.dumps(
            {
                "verdict": f"{role}: hold until the T3.5 cutover",
                "confidence": 0.7,
                "key_claims": [
                    {
                        "claim": "the carry leg is blocked on Phase-4 and the T3.5 cutover",
                        "candidate_id": "C1",
                        "arxiv_ids": ["2401.00001"],
                    }
                ],
                "discard": [{"arxiv_id": "2402.00003", "reason": "deferred to the T3.5 cutover"}],
            }
        )


def _turn_events(emit: _FakeEmit) -> list[dict]:
    return [kw for (name, kw) in emit.events if name == "debate_turn"]


def _attribution_events(emit: _FakeEmit) -> list[dict]:
    return [kw for (name, kw) in emit.events if name == "debate_attribution"]


# ── Guard 1 — the write-time sanitizer covers the new transport ───────────────


async def test_streamed_debate_turns_are_sanitized(monkeypatch):
    """The leak this whole file is here for.

    ``sanitize_transcript`` runs inside ``record_debate_transcript``, i.e. on
    the way into the DB. The SSE path never touches the DB, so a turn pushed
    onto the stream reaches a browser through a transport the write-time
    scrubber does not see. ``_emit_debate_turn`` calls the scrubber itself;
    deleting that call ships the raw model text.

    MUTATION (verified red): in ``_emit_debate_turn``, replace
    ``payload = safe[0]`` with ``payload = turn`` — every assertion below fails
    on the first event.
    """
    monkeypatch.setattr(
        "archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: _LeakyDebateBackend()
    )
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    emit = _FakeEmit()
    transcript = await de._debate_round(
        [_fake_proposal("A", ["2401.00001", "2402.00001"])], "m", emit, "cand_1", _EVIDENCE_BY_ID
    )
    assert len(transcript) == 4, "the debate must actually have run"

    events = _turn_events(emit)
    assert len(events) == 4
    for payload in events:
        wire = json.dumps(payload)
        for token in _JARGON:
            assert token not in wire, f"{token!r} reached the wire unscrubbed"
        assert "cutover" not in wire.lower()
        # Scrubbed, not dropped: the claim, its verdict and its discard reason
        # are all still on the record, carrying [redacted] where the jargon was.
        assert "[redacted]" in payload["verdict"]
        assert "[redacted]" in payload["claims"][0]["claim"]
        assert "[redacted]" in payload["discard"][0]["reason"]
        # Provenance is deliberately NOT scrubbed — the ids are the checkable part.
        assert payload["claims"][0]["arxiv_ids"] == ["2401.00001"]
        assert payload["discard"][0]["arxiv_id"] == "2402.00003"
        # The server-written headline is derived from the SANITIZED turn, so it
        # cannot re-introduce the jargon the payload just had scrubbed out.
        for token in _JARGON:
            assert token not in payload["headline"]


# ── Guard 2 — the stream is 1:1 with what gets stored ────────────────────────


async def test_every_debate_turn_is_streamed_exactly_once_and_matches_the_stored_turn(monkeypatch):
    """What you watched is what was written.

    Four turns are produced; four events are emitted, in the same fixed
    ``[bull-r1, bear-r1, bull-r2, bear-r2]`` order, each carrying exactly the
    turn that ``record_debate_transcript`` will sanitize and store. A stream
    showing three of four turns, or a different round-2 rebuttal from the one
    persisted, would be a second, unverifiable account of the same run.

    MUTATION (verified red): drop the ``_emit_debate_turn`` call from the
    round-2 loop — the count assertion fails at 2, and the role/round list is
    missing the rebuttal.
    """
    monkeypatch.setattr("archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: _DebateBackend())
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    emit = _FakeEmit()
    transcript = await de._debate_round(
        [_fake_proposal("A", ["2401.00001", "2402.00001"])], "m", emit, "cand_1", _EVIDENCE_BY_ID
    )

    events = _turn_events(emit)
    assert [(e["role"], e["round"]) for e in events] == [("bull", 1), ("bear", 1), ("bull", 2), ("bear", 2)]

    stored = sanitize_transcript(transcript)
    assert len(events) == len(stored) == 4
    for payload, turn in zip(events, stored, strict=True):
        assert {k: payload[k] for k in ("role", "round", "verdict", "claims", "discard")} == turn
        assert payload["candidate_id"] == "cand_1"
        # The headline is ADDITIONAL copy, never a replacement for the turn: a
        # reader can check every count in it against the fields above.
        assert payload["headline"]


def test_turn_headline_counts_are_checkable_against_the_turn():
    """The headline states counts, not adjectives — each one re-derivable from
    the payload rendered underneath it."""
    line = de._turn_headline(
        {
            "role": "bear",
            "round": 2,
            "verdict": "decline",
            "claims": [
                {"claim": "a", "arxiv_ids": ["2401.00001"]},
                {"claim": "b", "arxiv_ids": []},
                {"claim": "c", "arxiv_ids": ["2402.00001"]},
            ],
            "discard": [{"arxiv_id": "2402.00003", "reason": "no distinct mechanism"}],
        }
    )
    assert line == (
        "Bear researcher, rebuttal — verdict: decline. 3 claims, 2 grounded in a named paper; 1 paper set aside."
    )
    # An unparseable backend answer says so rather than rendering a blank card.
    degraded = de._turn_headline({"role": "bull", "round": 1, "verdict": "n/a", "claims": [], "discard": []})
    assert "could not be read" in degraded


# ── Guard 3 — one summary sentence, one place ────────────────────────────────


async def _run_board(monkeypatch, corpus, *, debate_backend=None, debate_round=None):
    """The full society run, hermetic. Returns (leaderboard, emit)."""
    from archimedes.models.regime import Regime

    _patch_regime(monkeypatch, Regime.RISK_ON)
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)
    monkeypatch.setattr("archimedes.agents.strategy_fusion.load_corpus", lambda *a, **k: corpus)
    monkeypatch.setattr(
        "archimedes.services.llm_backend.make_llm_backend",
        lambda model=None, **k: debate_backend if debate_backend is not None else _DebateBackend(),
    )
    if debate_round is not None:
        monkeypatch.setattr(de, "_debate_round", debate_round)

    pool = [_fake_proposal("A", ["2401.00001", "2402.00001"])]

    async def _fake_propose_pool(brief, model, corpus_arg):
        return pool, _EVIDENCE_BY_ID

    monkeypatch.setattr(de, "_propose_pool", _fake_propose_pool)
    monkeypatch.setattr(
        "archimedes.services.fusion_evaluator.evaluate_fusion_spec",
        lambda spec, *, num_trials=None, **kw: _fake_ev(cagr=0.2, num_trials=num_trials),
    )

    emit = _FakeEmit()
    board = await de._run_debate_leaderboard(
        candidate_id="cand_1", brief=GenerateBrief(intent="momentum equities", max_papers=4), emit=emit
    )
    return board, emit


async def test_streamed_attribution_is_the_entry_that_gets_persisted(monkeypatch, corpus):
    """The stream's paper-attribution event and the passport's trailing
    transcript entry are the SAME object, produced by the SAME helper.

    ``_paper_attribution_entry`` is the only writer of that summary sentence.
    Before the extraction it was inlined in ``_transcript_with_paper_record``;
    building a second copy for the stream is the drift this pins — two
    sentences, both plausible, disagreeing about how many papers a run engaged
    with.

    MUTATION (verified red): reword the summary in ``_paper_attribution_entry``
    but not in a hand-rolled stream copy — or vice versa — and the equality
    below fails.
    """
    board, emit = await _run_board(monkeypatch, corpus)
    leader = board[0]

    events = _attribution_events(emit)
    assert len(events) == 1, "one debate, one paper-attribution record"
    payload = events[0]

    expected = sanitize_transcript([_paper_attribution_entry(leader)])[0]
    assert {k: payload[k] for k in expected} == expected
    assert payload["role"] == "attribution"
    assert payload["verdict"].startswith("Paper attribution:")
    assert payload["candidate_id"] == "cand_1"
    # The budget-vs-used pair rides along so the stream can say "5 offered, 2
    # cited" rather than leaving `len(source_arxiv_ids)` to be read as the whole
    # story (#1739's own reason for carrying them).
    assert payload["papers_offered"] == leader.papers_offered
    assert payload["distinct_mechanism_papers"] == leader.distinct_mechanism_papers
    # Every retrieved paper is accounted for, including the ones nobody touched.
    assert {v["arxiv_id"] for v in payload["paper_verdicts"]} == set(_EVIDENCE_BY_ID)


async def test_streamed_attribution_is_sanitized(monkeypatch, corpus):
    """Same transport, same contract: the attribution entry carries model prose
    under two keys (`fusion_reasoning`, and each row's `discard_reasons`), and
    the SSE path never reaches the DB writer that normally scrubs them."""
    board, emit = await _run_board(monkeypatch, corpus, debate_backend=_LeakyDebateBackend())
    assert board
    payload = _attribution_events(emit)[0]
    wire = json.dumps(payload)
    for token in _JARGON:
        assert token not in wire
    assert "cutover" not in wire.lower()
    discarded = [v for v in payload["paper_verdicts"] if v["verdict"] == "discarded"]
    assert discarded, "the leaky backend discards 2402.00003 — the tally must record it"
    assert "[redacted]" in discarded[0]["discard_reasons"][0]


# ── Guard 4 — an absent debate is never rendered as a debate ─────────────────


async def test_no_backend_emits_no_debate_events(monkeypatch):
    """No LLM backend ⇒ ``_debate_round`` returns ``[]`` ⇒ nothing on the wire.

    An empty ``debate_turn`` (or a ``debate_attribution`` over a table of
    all-"unused" rows) would read as "the researchers looked at 30 papers and
    engaged with none" rather than as the honest absence it is — the same
    reasoning that already gates ``debate_paper_verdicts`` on ``transcript``.
    """

    class _Down:
        available = False

    monkeypatch.setattr("archimedes.services.llm_backend.make_llm_backend", lambda model=None, **k: _Down())
    monkeypatch.setattr(de.asyncio, "to_thread", _passthrough_to_thread)

    emit = _FakeEmit()
    transcript = await de._debate_round([_fake_proposal("A", ["1", "2"])], "m", emit, "cand_1", _EVIDENCE_BY_ID)
    assert transcript == []
    assert _turn_events(emit) == []


async def test_leaderboard_with_no_transcript_emits_no_attribution(monkeypatch, corpus):
    """The whole-run half of the same rule.

    MUTATION (verified red): drop the ``if transcript:`` gate in
    ``_run_debate_leaderboard`` — the run still has ``fusion_reasoning``, so
    ``_paper_attribution_entry`` returns an entry and a "Paper attribution: 0
    of 0 …" event is emitted for a debate that never happened.
    """

    async def _no_debate(*a, **k):
        return []

    board, emit = await _run_board(monkeypatch, corpus, debate_round=_no_debate)
    assert board, "the society still produces a leaderboard — the debate never gates"
    assert board[0].fusion_reasoning, "the precondition: there IS prose to attribute"
    assert _attribution_events(emit) == []
    assert _turn_events(emit) == []


# ── The candidate carrier is unchanged ───────────────────────────────────────


def test_paper_attribution_entry_returns_none_when_there_is_nothing_to_attribute():
    """The extraction preserved ``_transcript_with_paper_record``'s guard:
    no verdicts and no prose ⇒ no entry, on either consumer."""
    bare = _CandidateResult(
        candidate_id="cand_1",
        strategy_name="x",
        thesis="t",
        asset_universe=["SPY"],
        source_papers=[],
        weights={},
        reasoning="r",
        rigor_verdict={"passing": True},
        passes_rigor=True,
        generation_method="debate",
    )
    assert _paper_attribution_entry(bare) is None
