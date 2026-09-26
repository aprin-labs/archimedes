"""Generate ``docs/specs/prompt-inventory.md`` from the prompt registry.

The registry (``backend/archimedes/agents/prompts.py``) is the SSOT for every
template that becomes provider bytes. This renders it — id, version, role, call
sites, placeholders and the FULL template — so the question "what exactly do we
send the model?" has a reviewable, greppable answer that cannot go stale:
``test_prompt_inventory_doc.py`` byte-diffs a fresh render against the committed
file and fails CI on drift (the ``gen_asset_universe_doc.py`` / #757 pattern).

Usage (the canonical command goes through the thin ``scripts/`` wrapper; the
module form is equivalent)::

    PYTHONPATH=backend python scripts/gen_prompt_inventory.py            # write the doc
    PYTHONPATH=backend python scripts/gen_prompt_inventory.py --check    # exit 1 if stale (CI)
    python -m archimedes.scripts.gen_prompt_inventory [--check]          # equivalent module form
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from archimedes.agents.prompts import PROMPTS, Prompt

# repo-root-relative: scripts -> archimedes -> backend -> <repo root>
_OUTPUT_PATH = Path(__file__).resolve().parents[3] / "docs" / "specs" / "prompt-inventory.md"

# Four backticks, so a template containing ``` (none do today) still fences
# cleanly. The renderer asserts the invariant rather than trusting it.
_FENCE = "````"

_ROLE_NOTE = {
    "system": "sent as the `system` argument to `LLMBackend.complete`",
    "user": "sent as the `user` argument to `LLMBackend.complete`",
    "fragment": "never sent alone — substituted into or concatenated onto another entry",
}


def _placeholders(p: Prompt) -> str:
    return ", ".join(f"`${{{n}}}`" for n in p.placeholders) if p.placeholders else "—"


def _call_sites(p: Prompt) -> str:
    return "<br>".join(f"`{c}`" for c in p.call_sites) if p.call_sites else "—"


def _header() -> list[str]:
    return [
        "# Prompt inventory",
        "",
        "**GENERATED FILE — do not edit by hand.** Rendered from the prompt registry",
        "(`backend/archimedes/agents/prompts.py`) by `scripts/gen_prompt_inventory.py`.",
        "`backend/tests/test_prompt_inventory_doc.py` regenerates and byte-diffs this file, so an",
        "edit to a prompt that skips the regeneration fails CI.",
        "",
        "Every template below is one that becomes provider bytes. There is no other prompt text in",
        "the tree: `test_prompt_registry_goldens.py` walks every `LLMBackend.complete` call site and",
        "fails if one is served from anywhere but this registry.",
        "",
        "Two things this inventory is **not**. It is not the user messages — most of those are",
        "`json.dumps` of a data payload (the fusion candidate set, the brief, the paper abstract),",
        "so there is no template to version; only the user messages that carry prose are listed.",
        "And it is not a claim about what the model *does* with a prompt — only about what we send.",
        "",
        "`version` is a monotonic integer per id, bumped in the same commit that changes what the",
        "provider receives — the template `text`, or the block a caller renders into a placeholder.",
        "It is what a trace row stamps, so an unbumped edit would silently re-label old traces.",
        "",
    ]


def _summary_table() -> list[str]:
    out = [
        "## Summary",
        "",
        "| id | v | role | placeholders | call sites |",
        "|---|---|---|---|---|",
    ]
    for p in PROMPTS.values():
        out.append(f"| [`{p.id}`](#{_anchor(p.id)}) | {p.version} | {p.role} | {_placeholders(p)} | {_call_sites(p)} |")
    out.append("")
    return out


def _anchor(prompt_id: str) -> str:
    """GitHub's heading anchor for `### <id>` — dots dropped, dots/spaces to dashes."""
    return prompt_id.replace(".", "")


def _section(p: Prompt) -> list[str]:
    assert _FENCE not in p.text, f"{p.id}: template contains a {len(_FENCE)}-backtick run; widen the fence"
    out = [
        f"### {p.id}",
        "",
        f"- **version:** {p.version}",
        f"- **role:** {p.role} — {_ROLE_NOTE[p.role]}",
        f"- **placeholders:** {_placeholders(p)}",
        f"- **call sites:** {', '.join(f'`{c}`' for c in p.call_sites) if p.call_sites else '—'}",
    ]
    if p.embedded_in:
        out.append(f"- **embedded in:** {', '.join(f'`{e}`' for e in p.embedded_in)}")
    out += [
        "",
        p.summary,
        "",
        f"{_FENCE}text",
        p.text,
        _FENCE,
        "",
    ]
    return out


def render_doc() -> str:
    out = _header() + _summary_table() + ["## Templates", ""]
    for p in PROMPTS.values():
        out += _section(p)
    return "\n".join(out).rstrip("\n") + "\n"


def _force_utf8_stdio() -> None:
    """The doc and the unified diff contain non-ASCII (em-dash, `─`, `…`), so writing them to
    stdout/stderr in a C-locale shell would raise UnicodeEncodeError. Same reconfigure as
    ``gen_asset_universe_doc`` — the documented command must work in any locale."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Output path is overridable via env so the drift test can exercise the real --check CLI
    # (exit code + diff) against a temp file, without mutating the committed doc.
    out_path = Path(os.environ.get("PROMPT_INVENTORY_DOC_PATH", str(_OUTPUT_PATH)))
    content = render_doc()
    if "--check" in argv:
        if not out_path.exists():
            print(f"MISSING: {out_path} does not exist — run the generator.", file=sys.stderr)
            return 1
        committed = out_path.read_text(encoding="utf-8")
        if committed != content:
            import difflib

            sys.stderr.writelines(
                difflib.unified_diff(
                    committed.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"committed {out_path}",
                    tofile="freshly generated from the prompt registry",
                )
            )
            print(
                f"\nSTALE: {out_path} is out of sync with the prompt registry (diff above) — "
                "regenerate with `PYTHONPATH=backend python scripts/gen_prompt_inventory.py`.",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {out_path} is in sync with the prompt registry.")
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"wrote {out_path} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
