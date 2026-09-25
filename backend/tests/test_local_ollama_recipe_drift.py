"""The documented local-ollama recipe must stay true of the tree (#1044).

Issue #1044 leak 1 is a *documentation* bug that produced a *runtime* failure:
``.env.example`` Option (D) told people how to run locally on ollama, and the
instructions did not work. Fixing the instructions once does not keep them
fixed — the recipe makes falsifiable claims about `docker-compose.yml`, and
nothing re-checks them when compose changes. This file is that re-check.

Found by this guard on the day it was written, against ``origin/main``:
Option (D) claimed ``host.docker.internal`` was mapped for
``backend``/``oracle``/``agent``/``kb-runner``. Compose maps it for three of
those; ``kb-runner`` has no ``extra_hosts`` at all. A local user following the
comment would have set ``LLM_BASE_URL=http://host.docker.internal:11434`` and
watched only the kb-runner leg fail, with the docs insisting it should work.

**Stated so it cannot be misread:** these assertions compare the *prose* to the
*tree*. Either side may change; the test's job is to make sure both do. If you
add ``extra_hosts`` to a service, this goes red until Option (D) says so — which
is the point.

``import yaml`` is safe in CI: ``backend/requirements-base.txt`` pins
``uvicorn[standard]``, whose extra requires ``pyyaml>=5.1``, and the unit job
installs exactly that file (``quality-gate.yml`` line 137). Same precedent and
same reasoning as ``backend/tests/test_docs_site.py``.

Hermetic: reads two files off disk. No env, no network, no services.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_COMPOSE = _REPO_ROOT / "docker-compose.yml"

#: The host alias the recipe tells a local user to point ``LLM_BASE_URL`` at.
_HOST_ALIAS = "host.docker.internal"

#: The model id ``make_llm_backend()`` falls back to when ``LLM_MODEL`` is
#: unset. It is a cloud model; no ollama server will ever have it pulled, so
#: Option (D) documenting it would re-open the original leak.
_CLOUD_DEFAULT_FRAGMENT = "claude-"


def _option_d_block() -> str:
    """The Option (D) comment block from ``.env.example``, as one string.

    Fail-loud: raises rather than returning "" if the block is gone, so a
    rewrite of the file cannot silently turn every assertion below into a
    vacuous pass.
    """
    lines = _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if "(D)" in line and "llama" in line.lower()), None)
    if start is None:
        raise AssertionError(
            ".env.example no longer has an Option (D) Local Ollama block — either restore it or "
            "delete this guard deliberately; do not let it pass vacuously."
        )
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith("#"):
            break
        if re.match(r"^#\s+\([A-Z]\) ", line):  # the next lettered option
            break
        block.append(line)
    return "\n".join(block)


def _services_with_host_alias() -> set[str]:
    """Compose services that actually map ``host.docker.internal``."""
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    services = compose.get("services") or {}
    assert services, "docker-compose.yml parsed with no services — the scanner is broken, not the tree"
    return {
        name
        for name, spec in services.items()
        if any(_HOST_ALIAS in str(entry) for entry in ((spec or {}).get("extra_hosts") or []))
    }


def _services_claimed_by_option_d(block: str) -> set[str]:
    """Service names Option (D) claims carry the mapping.

    Reads only the span between "mapped for the" and "via extra_hosts", so
    unrelated backticked words elsewhere in the block (`ollama pull llama3.1`,
    `available`) are not mistaken for claims.
    """
    span = re.search(r"mapped for the(.*?)via extra_hosts", block, re.S)
    if span is None:
        raise AssertionError(
            "Option (D) no longer contains a 'mapped for the … via extra_hosts' claim. If the claim "
            "was removed on purpose, remove this guard too; if it was reworded, update the scanner."
        )
    return set(re.findall(r"`([a-z][a-z0-9-]*)`", span.group(1)))


class TestOptionDMatchesCompose:
    def test_the_extra_hosts_claim_names_exactly_the_services_that_have_it(self):
        """MUTATION: put `kb-runner` back in the Option (D) list.

        That is the literal pre-fix state of ``origin/main`` and it is what this
        test was written against — it went red on the unmodified tree, which is
        the only reason it is worth keeping. Run it before the .env.example hunk
        in this PR to see it reject.
        """
        claimed = _services_claimed_by_option_d(_option_d_block())
        actual = _services_with_host_alias()

        assert claimed, "the scanner extracted no service names — fix the scanner before trusting a green"
        assert claimed == actual, (
            f".env.example Option (D) claims {_HOST_ALIAS} is mapped for {sorted(claimed)}, but "
            f"docker-compose.yml maps it for {sorted(actual)}. Over-claiming sends a local user to "
            f"debug their ollama install for a service that was never wired; under-claiming hides a "
            f"leg of the stack that does work. Update whichever side is wrong."
        )

    def test_every_claimed_service_actually_exists_in_compose(self):
        """A name that has been renamed or deleted must not read as a promise."""
        compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
        names = set((compose.get("services") or {}).keys())
        claimed = _services_claimed_by_option_d(_option_d_block())
        unknown = claimed - names
        assert not unknown, f"Option (D) names compose services that do not exist: {sorted(unknown)}"


class TestOptionDIsAWorkingRecipe:
    """The three things that made the documented recipe fail in the first place."""

    def test_it_sets_llm_model(self):
        """The original leak: Option (D) documented LLM_PROVIDER and
        LLM_BASE_URL but not LLM_MODEL, so the backend was built with the cloud
        DEFAULT_MODEL and every call errored."""
        block = _option_d_block()
        assert re.search(r"^#\s+LLM_MODEL=\S+", block, re.M), (
            "Option (D) no longer sets LLM_MODEL — without it make_llm_backend() falls back to the "
            "cloud DEFAULT_MODEL and the recipe is broken again (#1044 leak 1)"
        )

    def test_it_does_not_document_a_cloud_model_as_the_ollama_model(self):
        model_lines = re.findall(r"^#\s+LLM_MODEL=(\S+)", _option_d_block(), re.M)
        assert model_lines, "no LLM_MODEL line found in Option (D)"
        for model in model_lines:
            assert _CLOUD_DEFAULT_FRAGMENT not in model, (
                f"Option (D) documents LLM_MODEL={model}, which is a cloud model id, not an ollama one"
            )

    def test_it_points_at_the_host_alias_not_localhost(self):
        """Inside the compose network ``localhost`` is the container itself."""
        base_urls = re.findall(r"^#\s+LLM_BASE_URL=(\S+)", _option_d_block(), re.M)
        assert base_urls, "no LLM_BASE_URL line found in Option (D)"
        assert any(_HOST_ALIAS in url for url in base_urls), (
            f"Option (D) must point LLM_BASE_URL at {_HOST_ALIAS} — from inside the compose network, "
            f"localhost is the container, not the host running ollama"
        )


def test_the_scanner_fails_loudly_on_a_block_it_cannot_read():
    """Guard on the guard: a scanner that returns "" on an unrecognised file
    would turn every assertion above into a vacuous pass, which is worse than
    no test at all."""
    with pytest.raises(AssertionError, match="mapped for the"):
        _services_claimed_by_option_d("#  (D) Local Ollama — a rewritten block with no such claim")
