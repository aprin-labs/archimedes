"""The prompt registry may move bytes, never change them (#1800).

``agents/prompts.py`` pulled six call sites' prompt constants into one versioned
registry. The refactor's whole claim is **zero behaviour change**: the provider
must receive the same bytes it received before. These tests hold that claim to
the byte.

``backend/tests/fixtures/prompt_goldens.json`` was captured from ``origin/main``
at ``9cb868eb`` — the commit BEFORE the registry existed — by running
``backend/tests/prompt_capture.py`` against that tree
(``git archive origin/main backend | tar -x``; ``PYTHONPATH=<tmp>/backend``).
The harness drives the REAL callers with a recording backend, so what is frozen
is the string that reached ``LLMBackend.complete``, not a module constant that
merely moved.

A diff here is therefore never noise. It means a prompt changed, and a prompt
change is legitimate only with a deliberate ``version`` bump in
``agents/prompts.py`` and a re-captured golden in the same commit.

One key is re-captured from THIS tree rather than from ``9cb868eb``:
``portfolio.construction.user``, because #1835 made the portfolio agent quote
paper titles as data. A re-captured key is recorded in the fixture's
``_meta.changed_since_capture`` with the version it landed as and why, and it
stops claiming the pre-registry provenance the rest of the fixture claims. The
provenance test holds those rows to the same standard as the goldens: an
exemption whose bytes actually DO reproduce from the pinned commit, or whose
version was never bumped, fails.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from archimedes.agents.prompts import PROMPTS, Prompt

from tests.prompt_capture import capture_prompts

# The pre-registry commit the goldens were captured from. Pinned to a SHA, not to
# `origin/main`: once this PR lands, `origin/main` HAS the registry and comparing
# against it would be tautological.
PRE_REGISTRY_COMMIT = "9cb868eba2ef7ad9ed3807c5f518b0e580ee69cf"


def _repo_root() -> Path:
    import archimedes  # backend/archimedes/__init__.py → parents: archimedes, backend, <repo>

    return Path(archimedes.__file__).resolve().parents[2]


def _payload() -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / "prompt_goldens.json").read_text(encoding="utf-8"))


def _goldens() -> dict[str, str]:
    return _payload()["prompts"]


def _changed_since_capture() -> dict[str, dict]:
    """Capture keys the fixture deliberately no longer reproduces from the pinned commit.

    A prompt is allowed to change after #1800 — that is the whole point of carrying a
    ``version``. What is not allowed is for the fixture to quietly stop being what it says
    it is. Each key listed here names the registry prompt it belongs to, the version that
    change landed as, and why; the provenance test below checks the exemption is REAL (the
    pre-registry tree must actually render different bytes) and that the version really was
    bumped, so a stale or invented row fails just as loudly as an unrecorded edit.
    """
    return _payload()["_meta"].get("changed_since_capture", {})


def test_every_live_prompt_is_byte_identical_to_the_pre_registry_capture() -> None:
    live = capture_prompts()
    goldens = _goldens()
    assert sorted(live) == sorted(goldens), (
        "the set of captured prompts changed — a call site was added, removed or renamed. "
        f"only-live={sorted(set(live) - set(goldens))} only-golden={sorted(set(goldens) - set(live))}"
    )
    for key in sorted(goldens):
        assert live[key] == goldens[key], (
            f"{key} no longer renders the bytes it rendered at {PRE_REGISTRY_COMMIT[:8]}. "
            "If the change is intentional: bump that prompt's `version` in agents/prompts.py "
            "and re-capture backend/tests/fixtures/prompt_goldens.json in the same commit."
        )


def test_the_capture_covers_every_registry_prompt_that_is_sent() -> None:
    # Fragments are proven through the prompt they are embedded in, so they are the only
    # entries allowed to have no capture key of their own.
    captured = set(_goldens())
    for prompt in PROMPTS.values():
        covered = any(key == prompt.id or key.startswith(prompt.id + ".") for key in captured)
        if prompt.role == "fragment":
            assert prompt.embedded_in, f"{prompt.id}: a fragment must name what it is embedded in"
            continue
        assert covered, f"{prompt.id} is sent to a provider but no golden captures it"


# Capture keys that are deliberately NOT registry entries: user messages built from
# data rather than from a template, so there is no text to version. Listed here (not
# silently skipped) so adding a real prose user prompt without registering it fails.
_DATA_SHAPED_CAPTURE_KEYS = {
    "brief_validation.user": "json.dumps({intent, stated_risk_appetite, asset_classes_hint})",
    "fusion.proposer.user": "json.dumps({user_steer, candidate_papers[, market_context]})",
    "fusion.spec_repair.user": "json.dumps of the accepted proposal's fields",
    "paper_passport.synth.user": "json.dumps({title, authors, year, abstract, body_excerpt})",
    "debate.turn.user": "_candidate_cards(): one '[C1] Name — cites arXiv:… \"Title\"' line per candidate",
}


def test_every_capture_key_belongs_to_a_registry_prompt_or_is_declared_data() -> None:
    ids = set(PROMPTS)
    for key in _goldens():
        if key in _DATA_SHAPED_CAPTURE_KEYS:
            continue
        assert any(key == i or key.startswith(i + ".") for i in ids), (
            f"golden {key!r} matches no registry id. Either register the template in "
            "agents/prompts.py, or declare it in _DATA_SHAPED_CAPTURE_KEYS with what builds it."
        )


# ── Registry invariants ──────────────────────────────────────────────────────


def test_declared_placeholders_match_the_template() -> None:
    from string import Template

    for prompt in PROMPTS.values():
        found = {m.group("named") or m.group("braced") for m in Template.pattern.finditer(prompt.text)}
        found.discard(None)
        assert found == set(prompt.placeholders), (
            f"{prompt.id}: declared placeholders {sorted(prompt.placeholders)} != "
            f"the ${{names}} actually in the template {sorted(found)}"
        )


def test_ids_are_sorted_unique_and_versioned() -> None:
    ids = list(PROMPTS)
    assert ids == sorted(ids), "registry entries must be ordered by id (the doc renders in this order)"
    assert len(ids) == len(set(ids))
    for prompt in PROMPTS.values():
        assert prompt.version >= 1
        assert prompt.role in {"system", "user", "fragment"}
        assert prompt.summary.strip(), f"{prompt.id}: every prompt must say what it is for"
        assert prompt.call_sites, f"{prompt.id}: every prompt must name where it is used"


def test_render_rejects_missing_and_unknown_placeholders() -> None:
    debate = PROMPTS["debate.turn.system"]
    with pytest.raises(KeyError):
        debate.render(role="bull", rnd=1, stance="x")  # rebuttal missing
    with pytest.raises(KeyError):
        debate.render(role="bull", rnd=1, stance="x", rebuttal="", extra="typo")
    # A prompt with no placeholders takes no keywords, and `.text` is already final.
    with pytest.raises(KeyError):
        PROMPTS["brief_validation.system"].render(anything="x")


def test_render_does_not_leave_an_unsubstituted_placeholder() -> None:
    # The failure this guards: a `safe_substitute` would ship a literal "${rebuttal}"
    # to the provider. `Prompt.render` uses strict substitution instead.
    rendered = PROMPTS["debate.turn.system"].render(role="bear", rnd=2, stance="s", rebuttal="r")
    assert "${" not in rendered and "$rebuttal" not in rendered


# ── Governance: no prompt may be served from outside the registry ────────────


def _modules_calling_complete() -> set[str]:
    """Every module with an attribute access named ``complete`` (the LLM seam).

    AST, not grep: a docstring that mentions ``LLMBackend.complete`` must not count,
    and ``asyncio.to_thread(backend.complete, ...)`` — a bare reference, no call —
    must. ``services/llm_backend.py`` is where ``complete`` is DEFINED, so it is
    excluded; ``def complete`` is a FunctionDef, never an Attribute, so the offline
    canned backends fall out for free.
    """
    root = _repo_root() / "backend" / "archimedes"
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if path.relative_to(root).as_posix() == "services/llm_backend.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "complete":
                found.add("archimedes." + path.relative_to(root).with_suffix("").as_posix().replace("/", "."))
                break
    return found


def test_every_llm_call_site_is_served_by_the_registry() -> None:
    declared = {".".join(site.split(".")[:3]) for p in PROMPTS.values() for site in p.call_sites}
    actual = _modules_calling_complete()
    assert actual == declared, (
        "a module calls LLMBackend.complete but is not named by any registry entry's call_sites "
        f"(or vice versa). undeclared={sorted(actual - declared)} stale={sorted(declared - actual)}. "
        "Every live prompt lives in agents/prompts.py — add the entry, do not add a new constant."
    )


# Module-level string constants in the LLM-calling modules that are NOT prompt text.
# The pair is (path relative to backend/archimedes, constant name) → why it is exempt.
_NOT_PROMPT_TEXT = {
    ("agents/generation_pipeline.py", "_FINANCE_WORDS"): (
        "deterministic token allowlist for cheap_brief_reject — matched against the brief, never sent"
    ),
    ("agents/generation_pipeline.py", "_COMMON_WORDS"): (
        "deterministic stop-word set for cheap_brief_reject — matched against the brief, never sent"
    ),
}
# The threshold is a size at which a constant is plausibly a prompt rather than a label
# or a log line. Both exempt constants above are word lists that happen to clear it; the
# allowlist is what keeps them from silently widening the guard.
_PROMPT_SIZED = 200


def test_the_prompt_modules_hold_no_prompt_text_of_their_own() -> None:
    """A re-pointed call site must not keep a private copy of a template.

    The regression this blocks: someone adds `_NEW_SYSTEM = \"\"\"You are...\"\"\"` next to
    the registry lookup and the inventory silently stops being the whole story.
    """
    root = _repo_root() / "backend" / "archimedes"
    modules = [m.replace("archimedes.", "").replace(".", "/") + ".py" for m in _modules_calling_complete()]
    for rel in sorted(modules):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not any(n.lstrip("_").isupper() for n in names):
                continue
            literal = "".join(
                sub.value
                for sub in ast.walk(node.value)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            )
            if len(literal) < _PROMPT_SIZED:
                continue
            allowed = {(rel, n) for n in names} & set(_NOT_PROMPT_TEXT)
            assert allowed, (
                f"{rel}: module constant {names} holds {len(literal)} chars of literal text. "
                "Prompt text belongs in agents/prompts.py — add a registry entry, not a constant. "
                "If it is not prompt text, declare it in _NOT_PROMPT_TEXT with the reason."
            )


# ── Provenance: the goldens really came from the pre-registry tree ───────────


@pytest.mark.skipif(os.environ.get("ARCHIMEDES_SKIP_GIT_TESTS") == "1", reason="git-dependent provenance test disabled")
def test_goldens_reproduce_from_the_pinned_pre_registry_commit(tmp_path: Path) -> None:
    """Re-derive the goldens from ``9cb868eb`` and compare to the committed fixture.

    This is the provenance half: the test above proves the live tree matches the
    fixture, and this proves the fixture matches the tree it claims to come from.
    Skipped (never failed) when that commit is not in the local object store — a
    shallow CI clone is a legitimate reason not to be able to check.
    """
    root = _repo_root()
    have = subprocess.run(
        ["git", "cat-file", "-e", f"{PRE_REGISTRY_COMMIT}^{{commit}}"],
        cwd=str(root),
        capture_output=True,
    )
    if have.returncode != 0:
        pytest.skip(f"{PRE_REGISTRY_COMMIT[:8]} not present locally (shallow clone) — cannot re-derive")

    base = tmp_path / "base"
    base.mkdir()
    archive = subprocess.run(["git", "archive", PRE_REGISTRY_COMMIT, "backend"], cwd=str(root), capture_output=True)
    assert archive.returncode == 0, archive.stderr.decode()
    subprocess.run(["tar", "-x", "-C", str(base)], input=archive.stdout, check=True)

    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(base / "backend"),
    }
    proc = subprocess.run(
        [sys.executable, str(root / "backend" / "tests" / "prompt_capture.py")],
        cwd=str(root),
        env=env,
        capture_output=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"capture against {PRE_REGISTRY_COMMIT[:8]} failed:\n{proc.stderr}"
    fresh = json.loads(proc.stdout)
    committed = _goldens()
    changed = _changed_since_capture()
    assert sorted(fresh) == sorted(committed), (
        "the set of captured prompts differs from the pre-registry tree — a call site was added, "
        f"removed or renamed. only-pinned={sorted(set(fresh) - set(committed))} "
        f"only-fixture={sorted(set(committed) - set(fresh))}"
    )
    assert set(changed) <= set(committed), (
        f"_meta.changed_since_capture names keys that are not captured: {sorted(set(changed) - set(committed))}"
    )
    for key in sorted(committed):
        if key in changed:
            entry = changed[key]
            assert fresh[key] != committed[key], (
                f"{key} is exempted in _meta.changed_since_capture but still reproduces "
                f"byte-for-byte from {PRE_REGISTRY_COMMIT[:8]} — drop the row rather than "
                "carry an exemption that hides nothing."
            )
            prompt = PROMPTS.get(entry["prompt_id"])
            assert prompt is not None, f"{key}: changed_since_capture names an unknown prompt {entry['prompt_id']!r}"
            assert prompt.version == entry["version"] > 1, (
                f"{key}: changed_since_capture claims {entry['prompt_id']} is at version "
                f"{entry['version']}, the registry says {prompt.version}. A recorded prompt "
                "change must carry the version bump it claims."
            )
            assert entry.get("why", "").strip(), f"{key}: changed_since_capture must say why the bytes changed"
            continue
        assert fresh[key] == committed[key], (
            f"backend/tests/fixtures/prompt_goldens.json[{key}] does not match a fresh capture "
            f"from {PRE_REGISTRY_COMMIT[:8]} — the fixture's provenance claim is false. A prompt "
            "that legitimately changed since then belongs in _meta.changed_since_capture with its "
            "version bump."
        )


def test_prompt_dataclass_is_frozen() -> None:
    # The registry is read-only at runtime: a test (or a route) must not be able to
    # mutate a live prompt out from under the goldens.
    with pytest.raises(dataclasses.FrozenInstanceError):
        PROMPTS["debate.turn.system"].version = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        PROMPTS["nope"] = Prompt(id="nope", version=1, role="system", text="x")  # type: ignore[index]
