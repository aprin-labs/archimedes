"""Shared LLM-generation JSON plumbing (relocated from `strategy_architect.py`).

The interactive Strategy Architect was retired (issue #1064 — the debate
society is now the sole strategy-generation path), but two of its pieces were
load-bearing for OTHER live call sites and had to survive the deletion:

- `extract_json` — robust JSON-object extraction from LLM text. Used by
  `debate_engine.py`, `strategy_fusion.py`, and `services/arxiv_pipeline.py`.
- `ArchitectCannedBackend` / `default_backend` — the offline-safe LLM
  backend fallback `services/arxiv_pipeline.py` uses for its extraction
  pipeline. Names kept as-is (not renamed) to keep the relocation a pure
  move, not a redesign.

A third piece — the `ArchitectProposal` / `StrategySelection` proposal DTOs —
was kept in 2026-07 for two consumers, `services/strategy_guardrail.py` and
`services/construction_trace.py`. Neither of those had any production caller
either; all three were deleted on 2026-08-31. Nothing in the tree carries an
Architect-shaped proposal any more.

Names are unchanged from the original module so this is a mechanical move,
not a rename.
"""

from __future__ import annotations

import json
import logging
import re

from archimedes.services.llm_backend import LLMBackend, make_llm_backend

logger = logging.getLogger(__name__)


# ── Robust JSON extraction ──────────────────────────────────────


def extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of an LLM response.

    Tolerates ```json fences and surrounding prose, and always returns a
    dict. When the top-level value is a JSON array or scalar (models
    sometimes wrap the object in a one-element array, e.g. ``[{...}]``), the
    brace scan below recovers the first embedded ``{...}`` object. Raises
    ValueError if no object can be recovered so the caller can degrade
    explicitly rather than calling ``.get()`` on a non-dict and crashing.
    """
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        parsed = json.loads(cleaned)
        # Only a top-level object is returned directly; a bare array/string/
        # number falls through to the brace scan (which yields the first
        # embedded object) so callers always receive a dict.
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        # Not valid JSON at the top level — fall through to the brace scan
        # below, which extracts the first balanced embedded object instead.
        pass

    start = cleaned.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(cleaned)):
            c = cleaned[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = cleaned.find("{", start + 1)
    raise ValueError("no parseable JSON object in LLM response")


# ── Offline-safe LLM backend fallback ────────────────────────────


class ArchitectCannedBackend:
    """Deterministic offline fallback — equal-weights the candidates.

    Keeps callers demoable with no API key and gives the parser a stable
    fixture in tests. The rationale text is explicit that this is a fallback
    so it never masquerades as model reasoning in a trace.
    """

    model_id = "canned-fallback"
    served_model = "canned-fallback"

    @property
    def available(self) -> bool:
        return False

    def complete(self, system: str, user: str) -> str:  # noqa: ARG002 — Protocol-shaped offline placeholder; signature matches live LLM backends
        ids = re.findall(r'"strategy_id"\s*:\s*"([0-9a-f]+)"', user)
        if not ids:
            ids = re.findall(r"\bid=([0-9a-f]{8,})", user)
        ids = ids[:4] or ["__none__"]
        w = round(1.0 / len(ids), 4)
        selected = [
            {
                "strategy_id": sid,
                "weight": w,
                "rationale": "Equal-weight fallback (no LLM backend available).",
                "paper_citation": "",
            }
            for sid in ids
        ]
        return json.dumps(
            {
                "selected": selected,
                "overall_reasoning": (
                    "Offline fallback: equal-weighted the risk-profile-eligible "
                    "library. Not model reasoning — set ANTHROPIC_API_KEY or "
                    "ANTHROPIC_AUTH_TOKEN+ANTHROPIC_BASE_URL for a real paper-grounded construction."
                ),
                "risk_notes": "Fallback allocation; downstream guardrail still applies.",
            }
        )


def default_backend() -> LLMBackend:
    """Claude or GLM when credentials are present; canned fallback otherwise.

    Delegates to the provider-agnostic ``llm_backend.make_llm_backend()`` factory.
    """
    backend = make_llm_backend()
    if backend.available:
        return backend  # type: ignore[return-value]
    logger.warning("No LLM credentials (LLM_* or ANTHROPIC_* env vars) — using canned fallback")
    return ArchitectCannedBackend()
