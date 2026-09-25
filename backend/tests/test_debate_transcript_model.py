"""Debate transcript persistence — the bull/bear round the society always ran
but the pipeline used to discard the instant it returned
(``debate_engine.py`` used to ``await _debate_round(...)`` with no
assignment). Hermetic: tmp-file sqlite via ``redirect_to_tmp_sqlite``, no
Postgres / Redis / LLM / network.

Guards demonstrated with the input that SHOULD fail them (CLAUDE.md rule 4):

* **Sanitization is real, not a no-op.** Text carrying the exact internal-
  jargon patterns named in the issue (``T\\d\\.\\d`` tier codes, "cutover",
  "Phase-" labels) is redacted; ordinary text is untouched.
* **``strategy_id`` is honestly nullable.** A considered-alternative's row
  (no strategy ever persisted for it) is a real, queryable NULL — not an
  empty string, not a sentinel.
* **A corrupt row reads as absent, never as an empty transcript** — the
  fail-soft rule from CLAUDE.md § fail-soft.
"""

from __future__ import annotations

import pytest
from archimedes.models.debate_transcript import (
    DebateTranscriptRecord,
    debate_transcript_for_strategy,
    record_debate_transcript,
    sanitize_debate_text,
    sanitize_transcript,
)

from tests.db_isolation import redirect_to_tmp_sqlite


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


_TRANSCRIPT = [
    {"role": "bull", "round": 1, "verdict": "act", "claims": ["momentum persists in cross-section"]},
    {"role": "bear", "round": 1, "verdict": "decline", "claims": ["drawdown risk in crowded factor"]},
    {"role": "bull", "round": 2, "verdict": "act", "claims": ["rebuttal: liquidity supports the trade"]},
    {"role": "bear", "round": 2, "verdict": "decline", "claims": ["rebuttal: still crowded post-2020"]},
]


# ── Sanitization: the adversarial input SHOULD be redacted ──────────────────


class TestSanitizeDebateText:
    @pytest.mark.parametrize(
        ("jargon", "label"),
        [
            ("Ship this ahead of T3.5.", "tier code T3.5"),
            ("Ship this ahead of T1.1.", "tier code T1.1"),
            ("We need the cutover before Friday.", "lowercase cutover"),
            ("We need the CUTOVER before Friday.", "uppercase CUTOVER"),
            ("This lands in Phase-3 of the rollout.", "Phase-3 label"),
            ("A bare Phase- prefix with nothing after it.", "bare Phase-"),
        ],
    )
    def test_jargon_pattern_is_redacted(self, jargon, label):
        out = sanitize_debate_text(jargon)
        assert "[redacted]" in out, f"{label} was not redacted: {out!r}"
        # None of the raw jargon survives.
        for leak in ("T3.5", "T1.1", "cutover", "CUTOVER", "Phase-3", "Phase-"):
            if leak.lower() in jargon.lower():
                assert leak.lower() not in out.lower() or "[redacted]" in out

    def test_ordinary_debate_text_is_untouched(self):
        text = "Momentum persists in the cross-section of large-cap equities."
        assert sanitize_debate_text(text) == text

    def test_empty_and_none_pass_through(self):
        assert sanitize_debate_text("") == ""

    def test_sanitize_transcript_scrubs_verdict_and_claims_only(self):
        dirty = [
            {
                "role": "bull",
                "round": 1,
                "verdict": "act — ship ahead of the T3.5 cutover",
                "claims": ["cites Phase-4 rollout data", "genuine claim about momentum"],
            }
        ]
        clean = sanitize_transcript(dirty)
        assert clean[0]["role"] == "bull"  # non-jargon fields untouched
        assert clean[0]["round"] == 1
        assert "T3.5" not in clean[0]["verdict"]
        assert "cutover" not in clean[0]["verdict"].lower()
        assert "Phase-4" not in clean[0]["claims"][0]
        assert clean[0]["claims"][1] == "genuine claim about momentum"  # untouched

    def test_sanitize_transcript_scrubs_dict_claims(self):
        """#1636 companion fix. The scrubber's claim loop was guarded by
        ``isinstance(c, str)``, so the moment claims became
        ``{claim, candidate_id, arxiv_ids}`` dicts — carrying paper
        attribution — every one of them passed through UNSCRUBBED. A shape
        change silently re-opened the exact jargon-leak path this write-time
        sanitizer exists to close. Revert ``_sanitize_claim`` to the old
        string-only guard and this fails.

        The arXiv ids are deliberately NOT scrubbed: they are validated
        provenance, and redacting them would corrupt the record rather than
        protect it.
        """
        dirty = [
            {
                "role": "bear",
                "round": 2,
                "verdict": "decline",
                "claims": [
                    {
                        "claim": "the T1.1 cutover data is stale",
                        "candidate_id": "C1",
                        "arxiv_ids": ["2401.00001"],
                    },
                    {"claim": "genuine claim about drawdown", "candidate_id": "C2", "arxiv_ids": []},
                ],
                "discard": [{"arxiv_id": "2402.00003", "reason": "superseded in Phase-4"}],
            }
        ]
        clean = sanitize_transcript(dirty)
        leaked = clean[0]["claims"][0]["claim"]
        assert "T1.1" not in leaked
        assert "cutover" not in leaked.lower()
        assert "[redacted]" in leaked
        # Attribution survives untouched — scrubbing an id would corrupt provenance.
        assert clean[0]["claims"][0]["arxiv_ids"] == ["2401.00001"]
        assert clean[0]["claims"][0]["candidate_id"] == "C1"
        assert clean[0]["claims"][1]["claim"] == "genuine claim about drawdown"
        # `discard` reasons are model prose too, and were scrubbed by nothing at all.
        assert "Phase-4" not in clean[0]["discard"][0]["reason"]
        assert clean[0]["discard"][0]["arxiv_id"] == "2402.00003"

    def test_sanitize_transcript_still_scrubs_legacy_string_claims(self):
        """Rows persisted before #1636 are bare strings and must keep working
        — the dict branch is an addition, never a replacement."""
        clean = sanitize_transcript([{"role": "bull", "round": 1, "claims": ["a T3.5 leak", "clean text"]}])
        assert "T3.5" not in clean[0]["claims"][0]
        assert clean[0]["claims"][1] == "clean text"

    def test_sanitize_transcript_drops_non_dict_entries(self):
        assert sanitize_transcript(["not a dict", 42, None]) == []

    def test_sanitize_transcript_tolerates_missing_fields(self):
        # A turn with neither verdict nor claims (or the wrong type) must not raise.
        out = sanitize_transcript([{"role": "bull", "round": 1}, {"role": "bear", "round": 1, "claims": "not-a-list"}])
        assert out[0] == {"role": "bull", "round": 1}
        assert out[1]["claims"] == "not-a-list"  # left alone, not coerced


# ── record_debate_transcript / debate_transcript_for_strategy round trip ────


class TestRecordAndRead:
    def test_winner_row_carries_a_real_strategy_id(self):
        from archimedes.db import get_session

        with get_session() as session:
            record_debate_transcript(
                session,
                strategy_id="strat-winner-1",
                generation_id="job-1",
                candidate_id="cand_neutral",
                transcript=_TRANSCRIPT,
            )
            session.commit()

        with get_session() as session:
            payload = debate_transcript_for_strategy(session, "strat-winner-1")
        assert payload is not None
        assert payload["strategy_id"] == "strat-winner-1"
        assert payload["generation_id"] == "job-1"
        assert payload["candidate_id"] == "cand_neutral"
        assert [t["role"] for t in payload["transcript"]] == ["bull", "bear", "bull", "bear"]

    def test_loser_row_has_a_real_queryable_null_strategy_id(self):
        """A considered-alternative never gets a strategy_store row — its
        transcript row must carry an HONEST NULL, not '' or a sentinel."""
        from archimedes.db import get_session

        with get_session() as session:
            rec = record_debate_transcript(
                session,
                strategy_id=None,
                generation_id="job-2",
                candidate_id="cand_neutral_alt1",
                transcript=_TRANSCRIPT,
            )
            session.commit()
            rec_id = rec.id

        with get_session() as session:
            row = session.query(DebateTranscriptRecord).filter_by(id=rec_id).first()
            assert row.strategy_id is None
            # A loser is never looked up by strategy_id (it has none) — the
            # strategy-keyed reader must not accidentally match on NULL/None.
            assert debate_transcript_for_strategy(session, "") is None

    def test_unknown_strategy_reads_as_none_not_empty(self):
        from archimedes.db import get_session

        with get_session() as session:
            assert debate_transcript_for_strategy(session, "does-not-exist") is None

    def test_written_transcript_is_sanitized_at_rest(self):
        """The stored column itself must be clean — sanitization happens at
        WRITE time, so a raw read of transcript_json is already safe."""
        from archimedes.db import get_session

        dirty = [{"role": "bull", "round": 1, "verdict": "ship before T3.5 cutover", "claims": ["Phase-3 data"]}]
        with get_session() as session:
            record_debate_transcript(
                session, strategy_id="strat-dirty", generation_id="job-3", candidate_id="c1", transcript=dirty
            )
            session.commit()

        with get_session() as session:
            row = session.query(DebateTranscriptRecord).filter_by(strategy_id="strat-dirty").first()
            assert "T3.5" not in row.transcript_json
            assert "cutover" not in row.transcript_json.lower()
            assert "Phase-3" not in row.transcript_json

    def test_newest_row_wins_when_a_strategy_has_more_than_one(self):
        from archimedes.db import get_session

        with get_session() as session:
            record_debate_transcript(
                session, strategy_id="strat-multi", generation_id="job-old", candidate_id="c1", transcript=_TRANSCRIPT
            )
            record_debate_transcript(
                session, strategy_id="strat-multi", generation_id="job-new", candidate_id="c1", transcript=_TRANSCRIPT
            )
            session.commit()

        with get_session() as session:
            payload = debate_transcript_for_strategy(session, "strat-multi")
        assert payload["generation_id"] == "job-new"

    def test_corrupt_json_reads_as_absent_not_empty(self):
        """Fail-soft rule (CLAUDE.md): a row that cannot be decoded is an
        absence, never a fabricated empty transcript."""
        from archimedes.db import get_session

        with get_session() as session:
            rec = record_debate_transcript(
                session, strategy_id="strat-corrupt", generation_id="job-4", candidate_id="c1", transcript=_TRANSCRIPT
            )
            session.commit()
            rec_id = rec.id

        with get_session() as session:
            row = session.query(DebateTranscriptRecord).filter_by(id=rec_id).first()
            row.transcript_json = "{not valid json"
            session.commit()

        with get_session() as session:
            assert debate_transcript_for_strategy(session, "strat-corrupt") is None

    @pytest.mark.parametrize(
        ("kwargs", "why"),
        [
            (
                {"strategy_id": None, "generation_id": "", "candidate_id": "c1", "transcript": []},
                "missing generation_id",
            ),
            (
                {"strategy_id": None, "generation_id": "j1", "candidate_id": "", "transcript": []},
                "missing candidate_id",
            ),
            (
                {"strategy_id": None, "generation_id": "j1", "candidate_id": "c1", "transcript": "not-a-list"},
                "non-list transcript",
            ),
        ],
    )
    def test_missing_required_fields_raise(self, kwargs, why):
        from archimedes.db import get_session

        with get_session() as session, pytest.raises(ValueError):
            record_debate_transcript(session, **kwargs)
