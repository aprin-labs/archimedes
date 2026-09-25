"""Guard tests for issue #1044 leak 1 — OllamaBackend must probe honestly.

Before this fix, ``OllamaBackend.available`` was hardcoded ``True`` (unlike
every other backend), so a misconfigured or unreachable local Ollama was
reported live by ``make_llm_backend()`` and ``/health`` right up until the
first real request errored. These tests are hermetic — no real Ollama server,
no network — via a fake ``httpx.get``/``httpx.Response``.

Second pass (#1044 B1): a bool is honest but not actionable. ``available:
False`` reads identically for the three ways the documented local recipe goes
wrong — ollama not running, ``LLM_MODEL`` never set, model never pulled — and
the operator has to guess which. ``unavailable_reason`` distinguishes them and
survives the factory's swap to ``CannedBackend``, which is where the diagnosis
used to be thrown away. The ``TestItSaysWhyNotJustNo`` class below is the guard
for that; each case asserts the reason names the ACTUAL cause, not a plausible
neighbouring one.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _fake_response(*, status_ok: bool = True, models: list[str] | None = None):
    resp = MagicMock()
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = Exception("boom")
    resp.json.return_value = {"models": [{"name": name} for name in (models or [])]}
    return resp


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch):
    # These tests construct OllamaBackend directly and don't exercise
    # LLM_PROVIDER routing, but keep the env clean so a stray LLM_MODEL/
    # LLM_BASE_URL from the developer's shell can't change the assertions.
    for var in ("LLM_MODEL", "LLM_BASE_URL", "LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)


def test_available_false_when_server_unreachable():
    from archimedes.services.llm_backend import OllamaBackend

    backend = OllamaBackend(model="llama3.1")
    with patch("httpx.get", side_effect=OSError("connection refused")):
        assert backend.available is False


def test_available_false_when_model_not_pulled():
    from archimedes.services.llm_backend import OllamaBackend

    backend = OllamaBackend(model="llama3.1")
    with patch("httpx.get", return_value=_fake_response(models=["mistral:latest"])):
        assert backend.available is False


def test_available_true_when_exact_tag_pulled():
    from archimedes.services.llm_backend import OllamaBackend

    backend = OllamaBackend(model="llama3.1:latest")
    with patch("httpx.get", return_value=_fake_response(models=["llama3.1:latest"])):
        assert backend.available is True


def test_available_true_when_bare_name_matches_tagged_pull():
    """LLM_MODEL=llama3.1 (no variant) must match a pulled "llama3.1:latest"."""
    from archimedes.services.llm_backend import OllamaBackend

    backend = OllamaBackend(model="llama3.1")
    with patch("httpx.get", return_value=_fake_response(models=["llama3.1:latest"])):
        assert backend.available is True


def test_make_llm_backend_returns_live_ollama_when_reachable_and_pulled(monkeypatch):
    """Adversarial-pass counterpart: a correctly configured, reachable server
    with the model pulled must NOT be downgraded to CannedBackend — the fix
    must not overcorrect into always-unavailable.
    """
    from archimedes.services.llm_backend import OllamaBackend, make_llm_backend

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3.1")
    monkeypatch.setenv("LLM_BASE_URL", "http://host.docker.internal:11434")
    with patch("httpx.get", return_value=_fake_response(models=["llama3.1:latest"])):
        backend = make_llm_backend()
    assert isinstance(backend, OllamaBackend)


def test_make_llm_backend_falls_back_to_canned_when_ollama_unreachable(monkeypatch):
    """Integration: the /health honesty contract — an unreachable ollama must
    resolve to CannedBackend, not silently report live.
    """
    from archimedes.services.llm_backend import CannedBackend, make_llm_backend

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3.1")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1")  # nothing listens here
    with patch("httpx.get", side_effect=OSError("connection refused")):
        backend = make_llm_backend()
    assert isinstance(backend, CannedBackend)


class TestItSaysWhyNotJustNo:
    """``unavailable_reason`` must name the actual cause (#1044 B1).

    The failure mode these guard against is a *plausible neighbouring reason*:
    reporting "model not pulled" when the real problem is that LLM_MODEL was
    never set sends the operator to ``ollama pull claude-sonnet-4-…``, which is
    not a model that exists. Each test therefore asserts both the phrase that
    must be there and, where they are confusable, that the wrong one is not.
    """

    def test_unset_llm_model_is_named_as_such_not_as_an_unpulled_model(self, monkeypatch):
        """MUTATION: delete the DEFAULT_MODEL pre-check in ``available``.

        With LLM_MODEL unset the factory hands OllamaBackend the cloud
        DEFAULT_MODEL. Without the pre-check the tags probe runs and answers
        "not pulled" — true of the string, useless as advice.
        """
        from archimedes.services.llm_backend import DEFAULT_MODEL, OllamaBackend

        monkeypatch.delenv("LLM_MODEL", raising=False)
        backend = OllamaBackend()  # exactly what make_llm_backend() builds when LLM_MODEL is unset
        with patch("httpx.get", return_value=_fake_response(models=["llama3.1:latest"])) as probe:
            assert backend.available is False
        assert "LLM_MODEL is unset" in backend.unavailable_reason
        assert DEFAULT_MODEL in backend.unavailable_reason
        assert "not pulled" not in backend.unavailable_reason
        # And it must not have wasted a round trip to diagnose its own env.
        probe.assert_not_called()

    def test_an_explicit_model_still_gets_the_network_answer(self, monkeypatch):
        """Over-correction guard: the pre-check must fire ONLY on the unset
        case. A user who deliberately sets LLM_MODEL to something odd is owed
        the real probe result, not a lecture about LLM_MODEL."""
        from archimedes.services.llm_backend import DEFAULT_MODEL, OllamaBackend

        monkeypatch.setenv("LLM_MODEL", DEFAULT_MODEL)
        backend = OllamaBackend(model=DEFAULT_MODEL)
        with patch("httpx.get", return_value=_fake_response(models=["llama3.1:latest"])):
            assert backend.available is False
        assert "not pulled" in backend.unavailable_reason
        assert "LLM_MODEL is unset" not in backend.unavailable_reason

    def test_unreachable_server_names_the_url_and_the_error(self):
        from archimedes.services.llm_backend import OllamaBackend

        backend = OllamaBackend(model="llama3.1")
        with patch("httpx.get", side_effect=OSError("connection refused")):
            assert backend.available is False
        assert "unreachable" in backend.unavailable_reason
        assert "localhost:11434" in backend.unavailable_reason  # the URL actually probed
        assert "connection refused" in backend.unavailable_reason

    def test_unpulled_model_names_the_pull_command_and_what_is_there(self):
        from archimedes.services.llm_backend import OllamaBackend

        backend = OllamaBackend(model="llama3.1")
        with patch("httpx.get", return_value=_fake_response(models=["mistral:latest"])):
            assert backend.available is False
        assert "ollama pull llama3.1" in backend.unavailable_reason
        assert "mistral:latest" in backend.unavailable_reason  # what IS there, so the fix is obvious

    def test_a_live_backend_states_no_reason(self):
        """Empty on success. A reason left over from a previous failed probe
        would be reported by /health next to ``llm_available: true``."""
        from archimedes.services.llm_backend import OllamaBackend

        backend = OllamaBackend(model="llama3.1")
        with patch("httpx.get", side_effect=OSError("connection refused")):
            assert backend.available is False
        assert backend.unavailable_reason  # populated by the failed probe
        with patch("httpx.get", return_value=_fake_response(models=["llama3.1:latest"])):
            assert backend.available is True
        assert backend.unavailable_reason == ""

    def test_the_factory_carries_the_reason_into_the_canned_fallback(self, monkeypatch):
        """MUTATION: restore the bare ``return CannedBackend()``.

        This is where the diagnosis used to die. The factory swallows the
        configured backend and returns a canned one; without the reason riding
        along, /health can only say "false", and the ollama-specific detail the
        probe just computed is discarded one line after it was produced.
        """
        from archimedes.services.llm_backend import CannedBackend, make_llm_backend

        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_MODEL", "llama3.1")
        monkeypatch.setenv("LLM_BASE_URL", "http://host.docker.internal:11434")
        with patch("httpx.get", return_value=_fake_response(models=["mistral:latest"])):
            backend = make_llm_backend()
        assert isinstance(backend, CannedBackend)
        assert "ollama" in backend.unavailable_reason
        assert "ollama pull llama3.1" in backend.unavailable_reason

    def test_no_provider_configured_is_its_own_reason(self, monkeypatch):
        """ "Nothing configured" and "configured but broken" must not collapse
        into one indistinguishable ``false``."""
        from archimedes.services.llm_backend import make_llm_backend

        for var in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        backend = make_llm_backend()
        assert backend.available is False
        assert "no LLM provider configured" in backend.unavailable_reason
