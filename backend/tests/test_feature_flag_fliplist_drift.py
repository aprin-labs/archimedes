"""Every feature flag in the tree must have a row in the flip-list (#834).

``docs/operations/feature-flag-fliplist.md`` is the go-live checklist: the one
page that says, per flag, what it gates, what it is set to in the deployed
config, and what has to be true before someone flips it. A flip-list is only
worth reading if it is complete — an undocumented money switch is exactly the
failure mode #834 was opened to prevent, and the issue has already drifted
twice (2026-07-05 found four live flags missing from it; 2026-08-20 found a
fifth, ``AGENT_DRY_RUN``, the live-signal switch).

Hand-maintenance is what let that happen, so this guard mechanises the grep.

**The guard runs in both directions** (the reverse half added 2026-08-31):

* *Forward* — every flag this module **discovers in the tree** must appear
  somewhere in the doc. The doc may name more (retired flags, mode selectors,
  companion tunables): extra rows are history and context, and forcing them out
  would make the page worse. What this blocks is a new flag landing in code
  with no row on the checklist.
* *Reverse* — every flag named in the doc's two **actionable** tables (``##
  LIVE`` and ``## FLIP-AT-LAUNCH``) must still have a **reader**. A checklist
  row telling Dan to flip a name nothing reads is the mirror-image failure: he
  flips it, nothing happens, and the page has lied about being the go-live
  list. Surviving in ``.env.example`` / compose / terraform does **not** count
  — that is an injection site, and a flag that is only injected is dead
  (rule 4 exists to find exactly those). The ``## DEAD / RETIRED``,
  ``## Deployed knobs`` and ``## Not flags`` sections are exempt: naming a name
  that is *gone* is the whole point of the first, and the others hold knobs and
  companions rather than flips.

Discovery rules, all mechanical, all reproducible with ripgrep:

1. **Python env reads** (``os.getenv("X")`` / ``os.environ.get("X")`` /
   ``os.environ["X"]`` / ``environ.get("X")``) under the runtime source roots,
   kept when either the *name* is flag-shaped (``*_ENABLED``, ``*_DRY_RUN``,
   ``*_REQUIRED``, ``ENABLE_*``, ``*_HALT``, ``*_ENFORCED``, ``*_FIXTURE``,
   ``*_ALLOW_*``, ``FEATURE_*``) or the *read site* parses a boolean literal
   (``"true"``/``"false"``/``"yes"``/``"no"``/``"on"``/``"off"``, ``_TRUTHY``,
   ``_FALSY``, ``_env_bool``, ``strtobool``).
2. **Python env-name constants** — ``_PUBLISH_ENV = "PAPER_TRACE_PUBLISH"``
   style — kept when the constant is passed to a boolean parser in the same
   file. This is how ``paper_trace.py`` spells its two switches, and rule 1
   cannot see them.
3. **UI flags**: every ``import.meta.env`` name in ``ui/src/featureFlags.js``,
   which is the frontend's whole flag surface by construction, plus
   flag-shaped names elsewhere in ``ui/src`` and ``auth/``.
4. **Deployed config**: flag-shaped env names in ``infra/**.tf``,
   ``docker-compose*.yml``, ``.env.example``, and ``ui/.env.example`` — this is
   what catches a flag that is *injected* but has no reader (a dead flag) as
   well as one that has a reader but no row. Terraform spells env two ways and
   both are matched: the ECS ``{ name = "X", value = ... }`` list entry and the
   Lambda ``environment { variables { X = "..." } }`` bare assignment. The
   second form is how ``COST_KILL_SWITCH_DRY_RUN`` is pinned, and it evaded
   this guard until 2026-08-31.
5. **Repo-variable flags**: ``vars.X`` in ``.github/workflows/*.yml``
   (``DEPLOY_ENABLED`` and ``RUNNER_DEPLOY_ENABLED`` gate whole deploy jobs —
   they belong on a go-live checklist). ``DOCS_SITE_ENABLED`` used to be a
   third; #1634 retired it when the docs site moved to our own S3 + CloudFront,
   where the bucket's existence is the gate.
6. **Shell escape hatches**: ``${X:-}`` in ``infra/scripts`` / ``scripts``,
   flag-shaped only.
7. **Numeric knobs that someone deliberately deployed** — a name read through
   ``int(os.getenv("X"...))`` / ``float(...)`` **and** pinned in the prod task
   definition (``infra/ecs.tf``). The intersection is the point. Bare numeric
   reads are excluded on purpose: 39 names in this tree are read that way and
   most are RPC timeouts and lease TTLs, which would bury the checklist. But a
   knob a human wrote into ``ecs.tf`` is a *decision on the deploy path* —
   ``GENERATION_MAX_CONCURRENT``, ``GENERATION_MAX_QUEUE``, ``DEBATE_POOL_MAX``
   (#1686) and ``GENERATION_TIMEOUT_SECONDS`` (#1692) all landed on
   2026-08-30/31 and all evaded rules 1–6.

Knobs that are read numerically but *not* deployed stay out of the scanner and
are documented by hand anyway (extra rows are always allowed).
``backend/tests/test_admission_knobs_drift.py`` and
``backend/tests/test_ecs_generation_timeout.py`` separately pin the ecs.tf
value against the code default for the admission knobs, in both directions.

Hermetic: reads committed files off disk and matches regexes in memory. No
``.env``, no network, no DB, no Redis, no app import.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLIPLIST = REPO_ROOT / "docs" / "operations" / "feature-flag-fliplist.md"

# An underscore-delimited segment from any of these makes a name flag-shaped.
# "DRY_RUN" is checked as a substring because it spans two segments.
FLAG_TOKENS = frozenset(
    {
        "ENABLE",
        "ENABLED",
        "DISABLE",
        "DISABLED",
        "REQUIRE",
        "REQUIRED",
        "ENFORCED",
        "HALT",
        "FIXTURE",
        "ALLOW",
        "FEATURE",
    }
)

# Boolean parsing at a read site. Bare "1"/"0" are deliberately excluded: they
# are the default for numeric knobs (GENERATION_MAX_CONCURRENT, timeouts) and
# would drag half the config surface in as "flags".
_BOOL_PARSE_RE = re.compile(r'"(?:true|false|yes|no|on|off)"|_TRUTHY|_FALSY|_env_bool|strtobool')

_NAME = r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)"
_PY_READ_RE = re.compile(
    r'os\.getenv\(\s*"' + _NAME + r'"'
    r'|os\.environ\.get\(\s*"' + _NAME + r'"'
    r'|os\.environ\[\s*"' + _NAME + r'"'
    r'|(?<![.\w])environ\.get\(\s*"' + _NAME + r'"'
)
_PY_ENV_CONST_RE = re.compile(r'^(_?[A-Z][A-Z0-9_]*)\s*(?::\s*[^=]+?)?\s*=\s*"' + _NAME + r'"\s*$', re.M)
_JS_READ_RE = re.compile(r"(?:process\.env|import\.meta\.env\??)\." + _NAME + r"|(?<![.\w])env\." + _NAME + r"\b")
_TF_ENV_RE = re.compile(r'name\s*=\s*"' + _NAME + r'"')
# Lambda-style `environment { variables { X = "..." } }`. Anchored to an
# indented line so top-level HCL (`variable "x" {`, `locals {`) cannot match.
_TF_ASSIGN_RE = re.compile(r"^[ \t]+" + _NAME + r'\s*=\s*"', re.M)
_DOTENV_RE = re.compile(r"^\s*#?\s*" + _NAME + r"=", re.M)
_COMPOSE_RE = re.compile(r"^\s+" + _NAME + r":\s*\$\{", re.M)
_GHA_VAR_RE = re.compile(r"vars\." + _NAME)
_SH_READ_RE = re.compile(r"\$\{" + _NAME + r"(?::-|\}|:\?)")

# Rule 7 — the two halves whose *intersection* is a deployed numeric knob.
_PY_NUMERIC_READ_RE = re.compile(r'(?:int|float)\(\s*os\.(?:getenv|environ\.get)\(\s*"' + _NAME + r'"')
_TF_PINNED_ENV_RE = re.compile(r'\{\s*name\s*=\s*"' + _NAME + r'"\s*,\s*value\s*=')
_PROD_TASK_DEF = "infra/ecs.tf"

# `infra/lambda` holds the cost-kill-switch and deploy-drift Lambdas: real
# deployed Python, outside the backend package. `mcp-server/src` is the MCP
# server shipped by #1703. Both are runtime code an env read can hide in.
_PY_ROOTS = (
    "backend/archimedes",
    "scripts",
    "cli",
    "analytics-engine/src",
    "infra/lambda",
    "mcp-server/src",
)
_JS_ROOTS = ("ui/src", "auth")
_SH_ROOTS = ("infra/scripts", "scripts")
_DOTENV_FILES = (".env.example", "ui/.env.example")
_UI_FLAG_MODULE = "ui/src/featureFlags.js"

# Reverse direction: only these two headings carry rows someone is expected to
# ACT on. "## DEAD / RETIRED" and "## Not flags — ..." exist precisely to name
# things that are gone, so they are exempt by construction.
_ACTIONABLE_HEADINGS = ("## LIVE", "## FLIP-AT-LAUNCH")
_BACKTICK_NAME_RE = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`")
_ANY_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


def _is_flag_shaped(name: str) -> bool:
    return "DRY_RUN" in name or bool(FLAG_TOKENS & set(name.split("_")))


def _names(pattern: re.Pattern[str], text: str):
    """Yield (name, offset) for every capturing group that matched."""
    for match in pattern.finditer(text):
        for group in match.groups():
            if group:
                yield group, match.start()


def _is_test_path(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    return "/tests/" in f"/{rel}" or "/test/" in f"/{rel}" or base.startswith("test_") or ".test." in base


def discover_flags(root: Path) -> dict[str, set[str]]:
    """Map every discovered flag name to the repo-relative files that mention it.

    ``root`` is a parameter (not a module constant) so the adversarial test can
    point it at a synthetic tree and prove the guard actually rejects.
    """
    found: dict[str, set[str]] = {}

    def add(name: str, rel: str) -> None:
        found.setdefault(name, set()).add(rel)

    def walk(pattern: str, *dirs: str):
        for directory in dirs:
            base = root / directory
            if not base.is_dir():
                continue
            for path in sorted(base.rglob(pattern)):
                rel = path.relative_to(root).as_posix()
                if _is_test_path(rel) or "node_modules" in rel:
                    continue
                yield path, rel

    for path, rel in walk("*.py", *_PY_ROOTS):
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        for name, offset in _names(_PY_READ_RE, text):
            line = lines[text.count("\n", 0, offset)] if lines else ""
            if _is_flag_shaped(name) or _BOOL_PARSE_RE.search(line):
                add(name, rel)
        for match in _PY_ENV_CONST_RE.finditer(text):
            ident, name = match.group(1), match.group(2)
            bool_call = re.compile(
                r"_(?:env_)?bool\(\s*" + re.escape(ident) + r"\b|_truthy\(\s*" + re.escape(ident) + r"\b"
            )
            if bool_call.search(text):
                add(name, rel)

    for pattern in ("*.js", "*.jsx"):
        for path, rel in walk(pattern, *_JS_ROOTS):
            text = path.read_text(encoding="utf-8", errors="replace")
            whole_file_is_flags = rel == _UI_FLAG_MODULE
            for name, _ in _names(_JS_READ_RE, text):
                if whole_file_is_flags or _is_flag_shaped(name):
                    add(name, rel)

    for path, rel in walk("*.tf", "infra"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in (_TF_ENV_RE, _TF_ASSIGN_RE):
            for name, _ in _names(pattern, text):
                if _is_flag_shaped(name):
                    add(name, rel)

    for rel in _DOTENV_FILES:
        path = root / rel
        if path.is_file():
            for name, _ in _names(_DOTENV_RE, path.read_text(encoding="utf-8", errors="replace")):
                if _is_flag_shaped(name):
                    add(name, rel)

    for path in sorted(root.glob("docker-compose*.yml")):
        for name, _ in _names(_COMPOSE_RE, path.read_text(encoding="utf-8", errors="replace")):
            if _is_flag_shaped(name):
                add(name, path.name)

    for path, rel in walk("*.yml", ".github/workflows"):
        for name, _ in _names(_GHA_VAR_RE, path.read_text(encoding="utf-8", errors="replace")):
            if _is_flag_shaped(name):
                add(name, rel)

    for path, rel in walk("*.sh", *_SH_ROOTS):
        for name, _ in _names(_SH_READ_RE, path.read_text(encoding="utf-8", errors="replace")):
            if _is_flag_shaped(name):
                add(name, rel)

    # Rule 7 — numeric knob AND pinned in the prod task definition.
    task_def = root / _PROD_TASK_DEF
    if task_def.is_file():
        pinned = {name for name, _ in _names(_TF_PINNED_ENV_RE, task_def.read_text(encoding="utf-8", errors="replace"))}
        for path, rel in walk("*.py", *_PY_ROOTS):
            for name, _ in _names(_PY_NUMERIC_READ_RE, path.read_text(encoding="utf-8", errors="replace")):
                if name in pinned:
                    add(name, rel)
                    add(name, _PROD_TASK_DEF)

    return found


def _is_config_only(rel: str) -> bool:
    """True for files that *inject* a flag rather than read one.

    A name that survives only in `.env.example`, a compose file, or terraform
    is a flag with no reader — a dead flag. It is still *discovered* (rule 4
    exists to catch exactly that), but it must not hold a row on an actionable
    table, because flipping it would do nothing.
    """
    base = rel.rsplit("/", 1)[-1]
    return base.endswith((".env.example", ".tf")) or base.startswith("docker-compose")


def tree_mentions(root: Path) -> set[str]:
    """Every env-shaped name that appears ANYWHERE in the scanned source.

    Deliberately far broader than :func:`discover_flags` — a comment, a
    docstring or a terraform variable description all count. Used for the
    *companion* names in a flag cell (``PREMIUM_MODELS_ALLOWLIST`` beside
    ``PREMIUM_MODELS_ENABLED``), which are real config but are not themselves
    flag-shaped enough for the scanner to find.
    """
    seen: set[str] = set()
    globs = (
        ("*.py", _PY_ROOTS),
        ("*.js", _JS_ROOTS),
        ("*.jsx", _JS_ROOTS),
        ("*.sh", _SH_ROOTS),
        ("*.tf", ("infra",)),
        ("*.yml", (".github/workflows",)),
    )
    for pattern, roots in globs:
        for directory in roots:
            base = root / directory
            if not base.is_dir():
                continue
            for path in sorted(base.rglob(pattern)):
                rel = path.relative_to(root).as_posix()
                if _is_test_path(rel) or "node_modules" in rel:
                    continue
                seen.update(_ANY_ENV_NAME_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
    for rel in _DOTENV_FILES:
        path = root / rel
        if path.is_file():
            seen.update(_ANY_ENV_NAME_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
    for path in sorted(root.glob("docker-compose*.yml")):
        seen.update(_ANY_ENV_NAME_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
    return seen


def actionable_rows(doc: Path) -> dict[str, str]:
    """Map each flag named in the LIVE / FLIP-AT-LAUNCH tables to its heading.

    A row's flag cell is the first cell containing a backticked ``UPPER_CASE``
    name — column 1 in the LIVE table, column 2 in FLIP-AT-LAUNCH (whose first
    column is a row number). Every backticked name in that one cell counts, so
    ``PREMIUM_MODELS_ENABLED`` (+ ``PREMIUM_MODELS_ALLOWLIST``) yields both.
    The **first** name in the cell is the row's flag; any others are companions
    and are held to the weaker "still exists somewhere" bar.
    """
    rows: dict[str, str] = {}
    heading = ""
    for line in doc.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            heading = next((h for h in _ACTIONABLE_HEADINGS if line.startswith(h)), "")
            continue
        if not heading or not line.lstrip().startswith("|"):
            continue
        for cell in line.strip().strip("|").split("|"):
            names = _BACKTICK_NAME_RE.findall(cell)
            if names:
                rows.setdefault(names[0], heading)
                for companion in names[1:]:
                    rows.setdefault(companion, heading + " (companion)")
                break
    return rows


def undocumented_flags(root: Path, doc: Path) -> dict[str, set[str]]:
    """Discovered flags with no mention in ``doc``. Empty dict = the doc is complete."""
    text = doc.read_text(encoding="utf-8")
    documented = set(_ANY_ENV_NAME_RE.findall(text))
    return {name: files for name, files in discover_flags(root).items() if name not in documented}


def vanished_flags(root: Path, doc: Path) -> dict[str, str]:
    """Actionable rows whose flag no longer has a reader. Empty dict = clean.

    Two bars, because the two kinds of name fail differently:

    * A row's **flag** must be discovered in a file that *reads* it. A name
      surviving only in ``.env.example`` / compose / terraform is injected and
      unread — flipping it does nothing, which is the failure this direction
      exists to catch, and its row belongs in § DEAD / RETIRED instead.
    * A row's **companions** only have to still exist somewhere. They are named
      for context and are frequently read through a helper the scanner cannot
      attribute.
    """
    discovered = discover_flags(root)
    mentions = tree_mentions(root)
    gone: dict[str, str] = {}
    for name, heading in actionable_rows(doc).items():
        if heading.endswith("(companion)"):
            if name not in mentions:
                gone[name] = heading
            continue
        readers = {rel for rel in discovered.get(name, set()) if not _is_config_only(rel)}
        if not readers:
            gone[name] = heading
    return gone


# ── The guard ────────────────────────────────────────────────────────────────


def test_fliplist_exists() -> None:
    assert FLIPLIST.is_file(), (
        f"{FLIPLIST.relative_to(REPO_ROOT)} is missing. It is the go-live checklist "
        "referenced by issue #834 and indexed in docs/doc-index.md — restore it rather "
        "than deleting this guard."
    )


def test_discovery_is_not_vacuous() -> None:
    """A scanner that finds nothing would make the drift guard pass forever.

    These five span all six discovery rules (boolean read, name constant, UI
    module, terraform env, repo variable). If a regex regresses, this fails
    before the completeness assertion can pass vacuously.
    """
    discovered = discover_flags(REPO_ROOT)
    for anchor in (
        "PAYMENTS_DRY_RUN",  # rule 1 — boolean read site
        "PAPER_TRACE_PUBLISH",  # rule 2 — env-name constant
        "VITE_ROADMAP_SURFACES",  # rule 3 — ui/src/featureFlags.js
        "EMAIL_VERIFICATION_ENFORCED",  # rule 4 — infra/ecs.tf + auth/auth.js
        "DEPLOY_ENABLED",  # rule 5 — GitHub Actions repo variable
        "COST_KILL_SWITCH_DRY_RUN",  # rule 4, Lambda form — infra/cost_kill_switch.tf
        "GENERATION_TIMEOUT_SECONDS",  # rule 7 — numeric knob pinned in ecs.tf
    ):
        assert anchor in discovered, f"flag discovery regressed: {anchor} is no longer found"
    assert len(discovered) >= 20, f"flag discovery collapsed to {len(discovered)} names — a regex regressed"


def test_every_discovered_flag_is_on_the_fliplist() -> None:
    missing = undocumented_flags(REPO_ROOT, FLIPLIST)
    assert not missing, (
        "Feature flags exist in the tree with no row on the go-live checklist "
        f"({FLIPLIST.relative_to(REPO_ROOT)}). Add a row classifying each as LIVE, "
        "FLIP-AT-LAUNCH, or DEAD — issue #834.\n"
        + "\n".join(f"  {name}: {sorted(files)}" for name, files in sorted(missing.items()))
    )


def test_row_parsing_is_not_vacuous() -> None:
    """The reverse guard is only as good as its table parser.

    If ``actionable_rows`` silently stopped matching, ``vanished_flags`` would
    pass forever with nothing to check — the same vacuous-pass trap the forward
    direction guards against above.
    """
    rows = actionable_rows(FLIPLIST)
    assert rows.get("PAYMENTS_HALT") == "## LIVE"
    assert rows.get("AGENT_DRY_RUN") == "## FLIP-AT-LAUNCH"
    # A two-name flag cell must yield both halves, not just the first.
    assert "PREMIUM_MODELS_ALLOWLIST" in rows
    # A name that appears ONLY in an exempt section must not be pulled in.
    assert "ARCHIMEDES_X402_ENABLED" not in rows, "DEAD / RETIRED rows must stay exempt"
    assert len(rows) >= 20, f"row parsing collapsed to {len(rows)} names — the table shape changed"


def test_no_actionable_row_names_a_flag_that_no_longer_exists() -> None:
    """The reverse direction: the row stays, the flag is deleted from the code.

    A go-live row for a name with no reader is worse than no row — someone
    flips it under pressure and believes they changed something.
    """
    gone = vanished_flags(REPO_ROOT, FLIPLIST)
    assert not gone, (
        f"{FLIPLIST.relative_to(REPO_ROOT)} lists flags with no reader left in the tree — a "
        "row that tells someone to flip a name nothing reads. Either restore the reader or "
        "move the row to the '## DEAD / RETIRED' section, which is exempt from this check "
        "(a name surviving only in .env.example / compose / terraform is injected, not read) "
        "— issue #834.\n" + "\n".join(f"  {name} (in {heading})" for name, heading in sorted(gone.items()))
    )


# ── Adversarial: the guard must be shown to reject ───────────────────────────


@pytest.fixture()
def synthetic_tree(tmp_path: Path) -> Path:
    """A minimal tree the discovery rules can walk, with a complete doc."""
    src = tmp_path / "backend" / "archimedes" / "services"
    src.mkdir(parents=True)
    (src / "widget.py").write_text(
        "import os\n\n\ndef widget_enabled() -> bool:\n"
        '    return os.getenv("WIDGET_ENABLED", "false").strip().lower() == "true"\n',
        encoding="utf-8",
    )
    doc = tmp_path / "fliplist.md"
    doc.write_text(
        "## LIVE\n\n"
        "| Flag | Reader | What it gates |\n"
        "|---|---|---|\n"
        "| `WIDGET_ENABLED` | `widget.py` | gates the widget |\n\n"
        "## DEAD / RETIRED\n\n"
        "| Name | Where | Status |\n"
        "|---|---|---|\n"
        "| `LONG_GONE_ENABLED` | nowhere | **RETIRED** |\n",
        encoding="utf-8",
    )
    return tmp_path


def test_guard_passes_on_a_complete_synthetic_doc(synthetic_tree: Path) -> None:
    doc = synthetic_tree / "fliplist.md"
    assert discover_flags(synthetic_tree).keys() == {"WIDGET_ENABLED"}
    assert undocumented_flags(synthetic_tree, doc) == {}
    assert actionable_rows(doc) == {"WIDGET_ENABLED": "## LIVE"}
    assert vanished_flags(synthetic_tree, doc) == {}


def test_guard_rejects_a_flag_added_without_a_doc_row(synthetic_tree: Path) -> None:
    """Adding a flag to the code and not the doc must fail — the drift case."""
    (synthetic_tree / "backend" / "archimedes" / "services" / "sneaky.py").write_text(
        "import os\n\n\ndef sneaky() -> bool:\n"
        '    return os.getenv("SNEAKY_MONEY_DRY_RUN", "false").lower() == "true"\n',
        encoding="utf-8",
    )
    missing = undocumented_flags(synthetic_tree, synthetic_tree / "fliplist.md")
    assert "SNEAKY_MONEY_DRY_RUN" in missing
    assert missing["SNEAKY_MONEY_DRY_RUN"] == {"backend/archimedes/services/sneaky.py"}


def test_guard_rejects_a_row_deleted_from_the_doc(synthetic_tree: Path) -> None:
    """The other drift direction: the flag stays, the row is removed."""
    doc = synthetic_tree / "fliplist.md"
    doc.write_text("(no flags documented)\n", encoding="utf-8")
    assert "WIDGET_ENABLED" in undocumented_flags(synthetic_tree, doc)


def test_guard_rejects_an_actionable_row_whose_flag_was_deleted(synthetic_tree: Path) -> None:
    """Reverse direction: delete the reader, leave the LIVE row behind."""
    (synthetic_tree / "backend" / "archimedes" / "services" / "widget.py").unlink()
    gone = vanished_flags(synthetic_tree, synthetic_tree / "fliplist.md")
    assert gone == {"WIDGET_ENABLED": "## LIVE"}


def test_reverse_guard_rejects_a_flag_that_is_only_injected(synthetic_tree: Path) -> None:
    """The half-deletion: reader gone, the `.env.example` line left behind.

    The name is still *discovered* (rule 4 is what catches dead injected
    flags), so a mentions-only check would wave it through. It has no reader,
    so its LIVE row is a lie: flipping it does nothing.
    """
    (synthetic_tree / "backend" / "archimedes" / "services" / "widget.py").unlink()
    (synthetic_tree / ".env.example").write_text("WIDGET_ENABLED=false\n", encoding="utf-8")
    assert "WIDGET_ENABLED" in discover_flags(synthetic_tree), "still injected, so still discovered"
    assert "WIDGET_ENABLED" in tree_mentions(synthetic_tree), "a mentions-only check would pass"
    assert vanished_flags(synthetic_tree, synthetic_tree / "fliplist.md") == {"WIDGET_ENABLED": "## LIVE"}


def test_reverse_guard_exempts_the_dead_section(synthetic_tree: Path) -> None:
    """`LONG_GONE_ENABLED` exists nowhere in the synthetic tree and must NOT fire.

    Without this exemption the guard would force the DEAD / RETIRED section to
    be emptied, deleting exactly the history that stops a retired flag being
    re-added.
    """
    assert "LONG_GONE_ENABLED" not in tree_mentions(synthetic_tree)
    assert "LONG_GONE_ENABLED" not in vanished_flags(synthetic_tree, synthetic_tree / "fliplist.md")


def test_lambda_style_terraform_env_is_discovered(tmp_path: Path) -> None:
    """Rule 4's second form — the shape that hid `COST_KILL_SWITCH_DRY_RUN`."""
    tf = tmp_path / "infra"
    tf.mkdir()
    (tf / "kill_switch.tf").write_text(
        'resource "aws_lambda_function" "k" {\n'
        "  environment {\n"
        "    variables = {\n"
        "      SOME_ARN                = aws_sns_topic.x.arn\n"
        '      SYNTHETIC_KILL_DRY_RUN  = "false"\n'
        "    }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    discovered = discover_flags(tmp_path)
    assert "SYNTHETIC_KILL_DRY_RUN" in discovered
    assert "SOME_ARN" not in discovered, "non-flag-shaped names must stay out"


def test_numeric_knob_needs_both_halves(tmp_path: Path) -> None:
    """Rule 7 fires on the intersection only: numeric read AND pinned in ecs.tf."""
    src = tmp_path / "backend" / "archimedes" / "api"
    src.mkdir(parents=True)
    src.joinpath("routes.py").write_text(
        "import os\n\n\ndef budget() -> int:\n"
        '    return int(os.getenv("SYNTHETIC_BUDGET_SECONDS", "600"))\n\n\n'
        "def ttl() -> int:\n"
        '    return int(os.getenv("SYNTHETIC_LEASE_TTL_MS", "5000"))\n',
        encoding="utf-8",
    )
    infra = tmp_path / "infra"
    infra.mkdir()
    infra.joinpath("ecs.tf").write_text(
        '        { name = "SYNTHETIC_BUDGET_SECONDS", value = "300" },\n', encoding="utf-8"
    )
    discovered = discover_flags(tmp_path)
    assert "SYNTHETIC_BUDGET_SECONDS" in discovered, "deployed numeric knob must be discovered"
    assert "SYNTHETIC_LEASE_TTL_MS" not in discovered, "an undeployed numeric knob is not a flag"
