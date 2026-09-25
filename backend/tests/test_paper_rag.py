"""Hermetic tests for paper_rag semantic retrieval (issue #874).

No real model downloads: every test stubs the encoder at the module-level
global or mocks ``SentenceTransformer.__init__`` so the HuggingFace cache is
never touched. Tests cover:

- Health state transitions:
    - model loads OK → ``live``
    - sentence-transformers import error → ``degraded``
    - model load raises exception → ``degraded``
    - feature flag OFF → ``disabled``
- Retrieval ranking: ``_rerank_with_embeddings`` calls encoder and returns
  papers sorted by descending score.
- TF-IDF fallback: ``_rerank_tfidf`` ranks by textual overlap (no model dep).
- Model-name env var: ``MINILM_MODEL`` overrides the default.
"""

from __future__ import annotations

import contextlib
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import archimedes.services.paper_rag as rag_module
import numpy as np

# Access all symbols via the module alias so monkeypatching the module-level
# globals (e.g. rag_module._embedding_model) is always reflected in calls.
_rerank_tfidf = rag_module._rerank_tfidf
_rerank_with_embeddings = rag_module._rerank_with_embeddings
augment_candidate_scores = rag_module.augment_candidate_scores
paper_rag_health = rag_module.paper_rag_health
semantic_rerank = rag_module.semantic_rerank


# ── Helpers ─────────────────────────────────────────────────────────────────


def _reset_model_cache() -> None:
    """Reset module-level model singleton so each test starts clean."""
    rag_module._embedding_model = None
    rag_module._embedding_load_attempted = False
    rag_module._weights_present_cache = None


def _fake_model(embeddings: dict[str, list[float]] | None = None):
    """Return a MagicMock that mimics a SentenceTransformer encode call.

    ``embeddings`` maps input text → embedding vector; if a text is not in
    the dict a zero vector is returned.  If ``embeddings`` is None a unit
    vector [1.0, 0.0] is returned for every input.
    """
    model = MagicMock()

    def _encode(texts: list[str]) -> np.ndarray:
        vecs = []
        for t in texts:
            if embeddings and t in embeddings:
                vecs.append(np.array(embeddings[t], dtype=float))
            else:
                vecs.append(np.array([1.0, 0.0], dtype=float))
        return np.array(vecs)

    model.encode.side_effect = _encode
    return model


@contextlib.contextmanager
def _stub_sentence_transformers(constructor):
    """Stub ``sentence_transformers`` — and ``torch`` — for the duration.

    Stubbing only ``sentence_transformers`` is not enough. Once that import
    succeeds, ``_get_embedding_model`` runs straight on into its CPU-guardrail
    ``import torch`` (``archimedes/services/paper_rag.py:277-285``). On a box
    that HAS torch installed, that is a real, first-time C-extension import
    performed *inside* a ``patch.dict("sys.modules")`` block — and on exit
    ``patch.dict`` rips the freshly created ``torch.*`` entries back out of
    ``sys.modules``, leaving dangling extension state that segfaults CPython
    (``Fatal Python error: Segmentation fault``, exit 139) either immediately
    or in some later, innocent-looking test.

    This file only survived on main because ``tests/services/test_paper_rag.py``
    sorts first, loaded the real MiniLM model, and left torch in ``sys.modules``
    — so the import was a harmless cache hit. The crash is therefore already
    reachable on main today, by running this file on its own on a torch box::

        pytest backend/tests/test_paper_rag.py   # → Segmentation fault (139)

    Pinning that sibling to its TF-IDF path (suite audit P1-3) removes the
    accidental pre-import, which also brings the latent crash into the normal
    full-suite ordering. Stubbing torch alongside sentence_transformers keeps
    every ordering hermetic: no real model, no real torch, no dangling C
    extensions.

    The torch stub carries a working ``set_num_threads`` so the 2026-07-04 CPU
    guardrail still runs its intended branch instead of being diverted into the
    ``except Exception`` arm.
    """
    st_mod = types.ModuleType("sentence_transformers")
    st_mod.SentenceTransformer = constructor
    torch_stub = types.ModuleType("torch")
    torch_stub.set_num_threads = MagicMock()
    with patch.dict("sys.modules", {"sentence_transformers": st_mod, "torch": torch_stub}):
        yield st_mod


def _mk_paper(arxiv_id: str, title: str, abstract: str = "") -> dict:
    return {"arxiv_id": arxiv_id, "title": title, "abstract": abstract}


# ── Health state transitions ─────────────────────────────────────────────────


class TestPaperRAGHealth:
    def setup_method(self):
        _reset_model_cache()

    def test_disabled_when_flag_off(self, monkeypatch):
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "false")
        _reset_model_cache()
        h = paper_rag_health()
        assert h.status == "disabled"

    def test_live_when_model_loads(self, monkeypatch):
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()
        with patch.object(rag_module, "_get_embedding_model", return_value=_fake_model()):
            h = paper_rag_health(probe=True)
        assert h.status == "live"
        assert "model=" in h.reason

    def test_live_reason_includes_model_name(self, monkeypatch):
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        monkeypatch.setenv("MINILM_MODEL", "all-MiniLM-L6-v2")
        _reset_model_cache()
        with patch.object(rag_module, "_get_embedding_model", return_value=_fake_model()):
            h = paper_rag_health(probe=True)
        assert h.status == "live"
        assert "all-MiniLM-L6-v2" in h.reason

    def test_degraded_when_model_returns_none(self, monkeypatch):
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()
        with patch.object(rag_module, "_get_embedding_model", return_value=None):
            h = paper_rag_health(probe=True)
        assert h.status == "degraded"
        assert "TF-IDF" in h.reason

    def test_degraded_on_import_error(self, monkeypatch):
        """ImportError during SentenceTransformer import → degraded (TF-IDF fallback)."""
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()

        # Simulate ImportError by making _get_embedding_model return None
        # (which is what happens when sentence-transformers isn't installed).
        with patch.object(rag_module, "_get_embedding_model", return_value=None):
            h = paper_rag_health(probe=True)
        assert h.status == "degraded"

    def test_model_load_exception_leads_to_degraded(self, monkeypatch):
        """Exception during model loading (e.g., corrupted cache) → degraded."""
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()

        # Constructor raises the way a corrupted on-disk model cache would.
        with _stub_sentence_transformers(MagicMock(side_effect=OSError("corrupted model files"))):
            h = paper_rag_health(probe=True)
        # Reset after test
        _reset_model_cache()
        assert h.status == "degraded"


# ── /health must never load the model (2026-08-19 OOM regression guard) ──────


class TestHealthDoesNotLoadModel:
    """The ALB polls /health every 30s. Loading all-MiniLM-L6-v2 there costs a
    measured 521 MB of RSS and was a direct contributor to the 2026-08-19 OOM
    crash loop. These tests exist to make that regression impossible to
    reintroduce silently.
    """

    def setup_method(self):
        _reset_model_cache()

    def test_default_call_never_loads_the_model(self, monkeypatch):
        """THE GUARD: probe=False must not call _get_embedding_model at all."""
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()
        loader = MagicMock(return_value=_fake_model())
        with patch.object(rag_module, "_get_embedding_model", loader):
            paper_rag_health()
        loader.assert_not_called()

    def test_probe_true_does_load_the_model(self, monkeypatch):
        """The dedicated /health/paper-rag endpoint still proves the load."""
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()
        loader = MagicMock(return_value=_fake_model())
        with patch.object(rag_module, "_get_embedding_model", loader):
            h = paper_rag_health(probe=True)
        loader.assert_called_once()
        assert h.status == "live"

    def test_ready_when_weights_present_but_unloaded(self, monkeypatch):
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()
        with _baked_hf_cache() as hf_home:
            monkeypatch.setenv("HF_HOME", hf_home)
            h = paper_rag_health()
        assert h.status == "ready"
        assert "not loaded" in h.reason

    def test_degraded_when_weights_missing(self, monkeypatch):
        """No weights on disk is a real problem and must not read as 'ready'."""
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()
        with tempfile.TemporaryDirectory() as empty:
            monkeypatch.setenv("HF_HOME", empty)
            h = paper_rag_health()
        assert h.status == "degraded"

    def test_live_reported_free_once_already_loaded(self, monkeypatch):
        """After a real retrieval loaded the model, /health reports live with
        no further allocation and without calling the loader again."""
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()
        rag_module._embedding_model = _fake_model()
        loader = MagicMock()
        try:
            with patch.object(rag_module, "_get_embedding_model", loader):
                h = paper_rag_health()
        finally:
            _reset_model_cache()
        loader.assert_not_called()
        assert h.status == "live"

    def test_disabled_short_circuits_before_any_disk_check(self, monkeypatch):
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "false")
        _reset_model_cache()
        probe = MagicMock(return_value=True)
        with patch.object(rag_module, "_scan_for_weights", probe):
            h = paper_rag_health()
        probe.assert_not_called()
        assert h.status == "disabled"


# ── _get_embedding_model caching ──────────────────────────────────────────────


class TestGetEmbeddingModelCaching:
    def setup_method(self):
        _reset_model_cache()

    def teardown_method(self):
        _reset_model_cache()

    def test_returns_none_on_import_error(self, monkeypatch):
        """If sentence_transformers is not importable, returns None.

        ``sys.modules[name] = None`` makes the *import statement itself* raise
        ImportError, which is the condition this test is named for and the one
        CI actually runs under (``requirements-base.txt`` ships no
        sentence-transformers). The previous version installed a fake module
        whose ``SentenceTransformer`` raised only when CALLED, so the import
        succeeded and ``_get_embedding_model`` ran on into its CPU-guardrail
        ``import torch`` — a real, first-time torch import performed *inside*
        a ``patch.dict("sys.modules")`` block, which segfaults CPython when
        the patch tears the freshly-created C extension modules back out.

        That never fired on main only because ``tests/services/test_paper_rag.py``
        happened to load the real MiniLM model first and left torch in
        ``sys.modules``, making this import a cheap cache hit. Pinning that file
        to its TF-IDF path (audit P1-3) removed the accidental pre-import and
        exposed the order dependency. Failing at the import keeps the test on
        the branch it claims and never touches torch at all.
        """
        _reset_model_cache()
        with patch.dict("sys.modules", {"sentence_transformers": None}):
            result = rag_module._get_embedding_model()
        _reset_model_cache()
        assert result is None

    def test_caches_successful_load(self, monkeypatch):
        """Second call returns the cached instance without calling SentenceTransformer again."""
        _reset_model_cache()
        stub = _fake_model()
        call_count = [0]

        # Use a plain function on a ModuleType (not a class attribute) so it is
        # never treated as a bound method and receives exactly the one positional
        # arg that _get_embedding_model passes.
        def _fake_constructor(name):
            call_count[0] += 1
            return stub

        with _stub_sentence_transformers(_fake_constructor):
            r1 = rag_module._get_embedding_model()  # triggers real load path
            r2 = rag_module._get_embedding_model()  # must hit the cache
        # Both calls must return the same cached stub.
        assert r1 is stub
        assert r2 is stub
        # The constructor must have been called exactly once; the second call
        # must have short-circuited at the "is not None" guard.
        assert call_count[0] == 1
        # A third call with the cache warm must also return the stub.
        r3 = rag_module._get_embedding_model()
        assert r3 is stub

    def test_attempted_flag_prevents_retry_after_failure(self, monkeypatch):
        """After a failed load, _embedding_load_attempted=True blocks re-tries."""
        _reset_model_cache()
        rag_module._embedding_load_attempted = True  # simulate prior failure
        # Should immediately return None without trying to import
        result = rag_module._get_embedding_model()
        assert result is None


# ── Env-configurable model name ───────────────────────────────────────────────


class TestModelName:
    def test_default_model_name(self, monkeypatch):
        monkeypatch.delenv("MINILM_MODEL", raising=False)
        assert rag_module._model_name() == "all-MiniLM-L6-v2"

    def test_custom_model_name_via_env(self, monkeypatch):
        monkeypatch.setenv("MINILM_MODEL", "paraphrase-MiniLM-L3-v2")
        assert rag_module._model_name() == "paraphrase-MiniLM-L3-v2"


# ── Semantic reranking with stubbed encoder ───────────────────────────────────


class TestReRankWithEmbeddings:
    """Verify _rerank_with_embeddings ranks papers by cosine similarity."""

    def _vol_paper(self) -> dict:
        return _mk_paper("2401.v1", "Volatility-Managed Portfolio Returns", "vol management sharpe")

    def _momentum_paper(self) -> dict:
        return _mk_paper("2401.m1", "Cross-Sectional Momentum Strategies", "momentum factor returns")

    def test_high_similarity_paper_ranks_first(self):
        query = "volatility managed equity"
        # query embedding ≈ volatility paper; momentum paper orthogonal
        embeddings = {
            query: [1.0, 0.0],
            "Volatility-Managed Portfolio Returns vol management sharpe": [0.99, 0.01],
            "Cross-Sectional Momentum Strategies momentum factor returns": [0.0, 1.0],
        }
        model = _fake_model(embeddings)
        papers = [self._momentum_paper(), self._vol_paper()]
        results = _rerank_with_embeddings(query, papers, model)
        assert results[0][0]["arxiv_id"] == "2401.v1", "vol paper should rank first"
        assert results[1][0]["arxiv_id"] == "2401.m1"

    def test_encoder_called_with_query_and_paper_texts(self):
        model = _fake_model()
        papers = [_mk_paper("1", "title one", "abstract one"), _mk_paper("2", "title two", "abstract two")]
        _rerank_with_embeddings("some query", papers, model)
        assert model.encode.called
        # encode called at least twice: once for query, once for papers
        assert model.encode.call_count >= 2

    def test_scores_clamped_to_unit_range(self):
        # Dot product could exceed 1 for non-normalised embeddings
        model = MagicMock()
        model.encode.side_effect = lambda texts: np.array([[2.0, 0.0] for _ in texts])
        papers = [_mk_paper("x", "foo", "bar")]
        results = _rerank_with_embeddings("query", papers, model)
        score = results[0][1]
        assert 0.0 <= score <= 1.0

    def test_empty_papers_returns_empty(self):
        model = _fake_model()
        assert _rerank_with_embeddings("query", [], model) == []


# ── TF-IDF fallback (no model dep) ───────────────────────────────────────────


class TestReRankTFIDF:
    def test_relevant_paper_ranks_first(self):
        query = "volatility momentum portfolio"
        papers = [
            _mk_paper("vol", "Volatility Momentum Equity Portfolio", "vol momentum returns risk"),
            _mk_paper("unrelated", "Cooking Recipes", "pasta tomato sauce"),
        ]
        results = _rerank_tfidf(query, papers)
        assert results[0][0]["arxiv_id"] == "vol"

    def test_empty_papers_returns_empty(self):
        assert _rerank_tfidf("query", []) == []

    def test_scores_in_unit_range(self):
        papers = [_mk_paper("a", "Test paper", "test abstract content")]
        results = _rerank_tfidf("test", papers)
        for _, score in results:
            assert 0.0 <= score <= 1.0


# ── semantic_rerank delegates correctly ──────────────────────────────────────


class TestSemanticRerank:
    def setup_method(self):
        _reset_model_cache()

    def teardown_method(self):
        _reset_model_cache()

    def test_uses_embedding_model_when_available(self, monkeypatch):
        stub = _fake_model()
        papers = [_mk_paper("a", "title", "abstract")]
        with patch.object(rag_module, "_get_embedding_model", return_value=stub):
            results = semantic_rerank("query", papers)
        assert stub.encode.called
        assert len(results) == 1

    def test_falls_back_to_tfidf_when_model_none(self, monkeypatch):
        papers = [_mk_paper("a", "title", "abstract")]
        with patch.object(rag_module, "_get_embedding_model", return_value=None):
            results = semantic_rerank("query", papers)
        # TF-IDF still produces a result
        assert len(results) == 1
        _, score = results[0]
        assert 0.0 <= score <= 1.0


# ── augment_candidate_scores integration ─────────────────────────────────────


class TestAugmentCandidateScores:
    def setup_method(self):
        _reset_model_cache()

    def teardown_method(self):
        _reset_model_cache()

    def _make_candidate(self, arxiv_id: str, title: str, abstract: str = "") -> MagicMock:
        c = MagicMock()
        c.arxiv_id = arxiv_id
        c.title = title
        c.abstract = abstract
        return c

    def test_returns_uniform_scores_when_disabled(self, monkeypatch):
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "false")
        candidates = [self._make_candidate("1", "Paper One"), self._make_candidate("2", "Paper Two")]
        results = augment_candidate_scores("some direction", candidates)
        scores = [s for _, s in results]
        assert all(s == 1.0 for s in scores)

    def test_semantic_scores_applied_when_enabled(self, monkeypatch):
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()
        embeddings = {
            "volatility managed portfolio": [1.0, 0.0],
            "Volatility Portfolio Returns ": [0.95, 0.0],
            "Random Unrelated Text ": [0.0, 1.0],
        }
        stub = _fake_model(embeddings)
        c1 = self._make_candidate("vol", "Volatility Portfolio Returns", "")
        c2 = self._make_candidate("unrel", "Random Unrelated Text", "")
        with patch.object(rag_module, "_get_embedding_model", return_value=stub):
            results = augment_candidate_scores("volatility managed portfolio", [c1, c2])
        # vol paper should rank ahead of unrelated paper
        ids = [c.arxiv_id for c, _ in results]
        assert ids[0] == "vol"

    def test_empty_candidates_returns_empty(self, monkeypatch):
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        assert augment_candidate_scores("direction", []) == []

    def test_gracefully_handles_rerank_exception(self, monkeypatch):
        """If semantic_rerank raises, augment falls back to uniform 1.0 scores."""
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        _reset_model_cache()
        candidates = [self._make_candidate("a", "Paper A")]
        with (
            patch.object(rag_module, "semantic_rerank", side_effect=RuntimeError("boom")),
            patch.object(rag_module, "_get_embedding_model", return_value=_fake_model()),
        ):
            results = augment_candidate_scores("direction", candidates)
        assert all(s == 1.0 for _, s in results)

    def test_empty_arxiv_ids_get_zero_not_default_promotion(self, monkeypatch):
        """Candidates with empty arxiv_id must not ride the old 0.5 default above a
        genuinely-scored paper — they get 0.0 and rank below it (#938)."""
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        c_real = self._make_candidate("2401.v1", "Volatility Portfolio")
        c_empty1 = self._make_candidate("", "Mystery Paper A")
        c_empty2 = self._make_candidate("", "Mystery Paper B")

        # Every paper scores 0.3 — below the OLD 0.5 default that empty ids used to get.
        def fake_rerank(query, papers, **kw):
            return [(p, 0.3) for p in papers]

        with patch.object(rag_module, "semantic_rerank", side_effect=fake_rerank):
            results = augment_candidate_scores("volatility", [c_real, c_empty1, c_empty2])

        empty_scores = [s for c, s in results if not c.arxiv_id]
        assert empty_scores == [0.0, 0.0]  # not 0.5
        real_score = next(s for c, s in results if c.arxiv_id == "2401.v1")
        assert real_score == 0.3
        assert results[0][0].arxiv_id == "2401.v1"  # real paper ranks first

    def test_duplicate_arxiv_ids_each_keep_their_own_score(self, monkeypatch):
        """Two candidates sharing an arxiv_id must not collapse to one score in the
        map — each keeps its own semantic score (#938)."""
        monkeypatch.setenv("FUSION_SEMANTIC_RETRIEVAL", "true")
        c1 = self._make_candidate("dup", "Paper One about momentum")
        c2 = self._make_candidate("dup", "Paper Two about value")

        # papers preserve head order, so papers[0] is c1's dict, papers[1] is c2's.
        def fake_rerank(query, papers, **kw):
            return [(papers[0], 0.9), (papers[1], 0.2)]

        with patch.object(rag_module, "semantic_rerank", side_effect=fake_rerank):
            results = augment_candidate_scores("x", [c1, c2])

        by_title = {c.title: s for c, s in results}
        assert by_title["Paper One about momentum"] == 0.9  # not collapsed to 0.2
        assert by_title["Paper Two about value"] == 0.2


# ── _weights_present must understand the REAL huggingface_hub layout ─────────
#
# The first version of this scanned one level with iterdir(). huggingface_hub
# nests the cache as $HF_HOME/hub/models--<org>--<name>/snapshots/<sha>/..., so
# the top level of HF_HOME holds only "hub" and "xet" — neither matched, and
# production reported paper_rag="degraded" (and corpus_embedded=false on a
# public surface) on a container where /health/paper-rag proved the model loads.
#
# It shipped because the tests mocked _weights_present itself instead of
# exercising it — CLAUDE.md's "mock at boundaries, not internals". These build a
# real directory tree.


@contextlib.contextmanager
def _baked_hf_cache(model: str = "all-MiniLM-L6-v2", weight: str = "model.safetensors"):
    """A tmp HF_HOME laid out exactly as the Docker image bakes it."""
    with tempfile.TemporaryDirectory() as root:
        snap = (
            Path(root)
            / "hub"
            / f"models--sentence-transformers--{model}"
            / "snapshots"
            / "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
        )
        snap.mkdir(parents=True)
        (snap / weight).write_bytes(b"\x00")
        (Path(root) / "xet").mkdir()
        yield root


class TestWeightsPresent:
    def setup_method(self):
        _reset_model_cache()

    def teardown_method(self):
        _reset_model_cache()

    def test_finds_weights_in_the_real_nested_hf_layout(self, monkeypatch):
        """THE REGRESSION: weights nested under hub/ must be found."""
        monkeypatch.setenv("MINILM_MODEL", "all-MiniLM-L6-v2")
        with _baked_hf_cache() as hf_home:
            monkeypatch.setenv("HF_HOME", hf_home)
            assert rag_module._weights_present() is True

    def test_finds_pytorch_bin_layout_too(self, monkeypatch):
        monkeypatch.setenv("MINILM_MODEL", "all-MiniLM-L6-v2")
        with _baked_hf_cache(weight="pytorch_model.bin") as hf_home:
            monkeypatch.setenv("HF_HOME", hf_home)
            assert rag_module._weights_present() is True

    def test_false_when_model_dir_exists_but_holds_no_weights(self, monkeypatch):
        monkeypatch.setenv("MINILM_MODEL", "all-MiniLM-L6-v2")
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2").mkdir(parents=True)
            monkeypatch.setenv("HF_HOME", root)
            assert rag_module._weights_present() is False

    def test_false_when_hf_home_unset(self, monkeypatch):
        monkeypatch.delenv("HF_HOME", raising=False)
        assert rag_module._weights_present() is False

    def test_result_is_memoised(self, monkeypatch):
        """/health is polled every 30s; the tree must be walked once."""
        monkeypatch.setenv("MINILM_MODEL", "all-MiniLM-L6-v2")
        with _baked_hf_cache() as hf_home:
            monkeypatch.setenv("HF_HOME", hf_home)
            spy = MagicMock(return_value=True)
            with patch.object(rag_module, "_scan_for_weights", spy):
                rag_module._weights_present()
                rag_module._weights_present()
                rag_module._weights_present()
            assert spy.call_count == 1
