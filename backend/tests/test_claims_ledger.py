"""``docs/claims-ledger.md`` must cite code that exists and describe a tree that exists.

The ledger is the file that says which public claims are true. A ledger whose citations
have rotted is worse than no ledger: it reads as verification, and it is the thing a
reviewer reaches for instead of re-reading the code. So the citations are executable.

Same guard shape as ``test_agent_quickstart_drift.py`` — parse one committed markdown file
and assert its assertions against the tree — with two properties that are specific to a
ledger and are the reason this file is not just a link checker:

**Self-retiring exemptions.** The ledger cites one path that does not exist yet
(``docs/adr/market-data-sourcing.md``, landing with #1218) and declares it in a
``claims-ledger:pending-paths`` comment. ``test_pending_paths_have_not_landed_yet``
asserts each of those is *still absent*. When the ADR merges, this file goes red and the
row above it must be re-pointed at real evidence. An exemption that cannot expire is a
hole; this one closes itself.

**Open over-claims are pinned to the tree, not just described.** The ledger's
``OVER-CLAIMED`` rows assert that a specific sentence is still live on a specific surface.
``test_open_overclaims_are_still_present`` pins each one, and
``test_published_exit_codes_still_omit_incomplete`` pins the one over-claim that is an
*absence* rather than a sentence (the CLI manifest's missing exit code ``4``). Fixing
either therefore turns this test red — deliberately: the same change that scrubs the
sentence has to move the row from ``OVER-CLAIMED`` to ``CHANGED``, which is the whole point
of keeping a ledger rather than a memo.

**Both citation forms are checked, including the shorthand.** The ledger cites a path in
full once and then writes bare ``:NN`` for later lines in the same file. Those shorthands
are ~20% of its evidence, and an earlier revision of this file did not match them at all
while its docstring advertised that line numbers are verified — a guard with a silent hole,
which is the exact defect the ledger exists to record. They are now resolved against their
anchor and range-checked, and a shorthand with no anchor is a failure rather than a
silent skip.

Hermetic: reads committed files off disk. No DB, no Redis, no RPC, no network, no
``.env``, and no import of ``archimedes``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER = REPO_ROOT / "docs" / "claims-ledger.md"
DOCS_INDEX = REPO_ROOT / "docs" / "doc-index.md"

# The statuses a row is allowed to carry. Adding one is a deliberate act — a new word is a
# new promise to the reader about what the row means — so it goes here and in the ledger's
# own "How to read a row" table, together.
ALLOWED_STATUSES = frozenset({"TRUE", "CHANGED", "RETRACTED", "OVER-CLAIMED", "PENDING VENDOR CUTOVER"})

# A citation: a backticked repo-relative path, optionally `:line` or `:line-line`. The
# extension list is what keeps `GET /api/selection-bias/gate` and other backticked
# slash-bearing prose out — those name routes, not files, and there is nothing on disk to
# resolve them against.
#
# A directory component is REQUIRED, with the four repo-root docs as the named exception.
# That is what lets the ledger keep writing a bare `agent.json` or `live_rigor_gate.py` as
# shorthand for a path it already gave in full, without those shorthands being read as
# citations to files at the repo root that do not exist.
# `conf` and `tf` joined the list on 2026-08-31 with the Security-page rows. That page's
# claims are enforced in `nginx/nginx.conf` (the CSP, HSTS, framing and rate-limit zones)
# and pinned in `infra/ecs.tf` (the money switches), so without those two extensions its
# most load-bearing evidence would have been backticked prose the guard silently skipped —
# a row reading as cited while nothing resolved it. Both require a directory component
# like every other citation, so a bare `nginx.conf` shorthand is still not a citation.
_CITATION_RE = re.compile(
    r"`((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:py|jsx|js|md|json|txt|sol|html|xml|yml|yaml|toml|sh|css|conf|tf)"
    r"|(?:README|CLAUDE|SETUP|AGENTS)\.md)"
    r"(?::(\d+)(?:-\d+)?)?`"
)

# The ledger's second citation form: a bare `:NN` that inherits the path from the last full
# citation earlier in the same line ("`live_rigor_gate.py:54` defines DEGENERATE; `:92`
# makes `passes` a plain bool"). Roughly a fifth of the ledger's citations are written this
# way, and an earlier revision of this file did not match them at all — so that whole slice
# was unchecked while the module docstring advertised that line numbers are verified. A guard
# with a silent 20% hole is the failure mode this whole file exists to catch, so the
# shorthands are resolved against their anchor and range-checked like any other citation.
_SHORTHAND_RE = re.compile(r"`:(\d+)(?:-\d+)?`")

_PENDING_BLOCK_RE = re.compile(r"<!--\s*claims-ledger:pending-paths(.*?)-->", re.DOTALL)
_PENDING_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+\.[a-z]+$")

# Symbols the ledger's TRUE rows lean on by name. A rename that silently invalidates a row
# is exactly the rot this file exists to catch, and a path-existence check cannot see it.
_SYMBOL_PINS: tuple[tuple[str, str], ...] = (
    ("backend/archimedes/services/live_rigor_gate.py", "def verdict_from_returns"),
    ("backend/archimedes/services/live_rigor_gate.py", 'DEGENERATE = "degenerate"'),
    ("backend/archimedes/services/rigor_evaluator.py", "DEFAULT_BOARD_FDR_LEVEL = 0.05"),
    ("backend/archimedes/services/rigor_evaluator.py", "def compute_board_level_fdr"),
    ("backend/archimedes/api/leaderboard_schemas.py", "class BoardLevelFdr"),
    ("backend/archimedes/api/rigor_verify_routes.py", "verdict_capped"),
    ("backend/archimedes/api/corpus_routes.py", "kb_artifact_not_found"),
    ("backend/archimedes/api/wallet_routes.py", "_CHALLENGE_TTL = timedelta(minutes=5)"),
    ("backend/archimedes/services/generation_payment.py", 'os.getenv("GENERATION_PAYMENT_REQUIRED")'),
    ("backend/archimedes/agents/generation_pipeline.py", "mirrored on-chain in v1.5"),
    ("backend/archimedes/chain/agent_runner.py", "_commit_trace"),
    ("backend/archimedes/chain/agent_runner.py", "_reveal_trace"),
    ("ui/src/routes.js", "ANON_APP_PAGES"),
    ("ui/src/routes.js", "PUBLIC_PATHS"),
    ("ui/src/featureFlags.js", "ROADMAP_SURFACES_ENABLED"),
    ("cli/src/archimedes_cli/exits.py", "INCOMPLETE = 4"),
    # Security-page rows (2026-08-31). Every one of these is a symbol a Security-page
    # sentence is *about* — the cookie flags the page enumerates, the headers it lists, the
    # rate-limit zone it distinguishes, the paywall functions it describes. A rename here
    # leaves the page asserting enforcement that no longer has that name.
    ("auth/auth.js", "useSecureCookies: production"),
    ("auth/auth.js", "sameSite: 'lax'"),
    ("nginx/nginx.conf", "zone=api_write:10m rate=20r/m"),
    ("nginx/nginx.conf", 'add_header X-Frame-Options "DENY" always;'),
    ("nginx/nginx.conf", 'Permissions-Policy "geolocation=(), microphone=(), camera=()"'),
    ("backend/archimedes/api/account_auth.py", '"cookie": request.headers.get("cookie", "")'),
    ("backend/archimedes/api/auth_guard.py", "hmac.compare_digest"),
    ("backend/archimedes/services/generation_payment.py", "def settles_real_value"),
    ("backend/archimedes/services/generation_payment.py", "async def enforce_generation_payment"),
    ("backend/archimedes/services/generation_payment.py", "payer_mismatch"),
    ("infra/ecs.tf", '{ name = "GENERATION_PAYMENT_REQUIRED", value = "true" }'),
    ("infra/ecs.tf", '{ name = "GENERATION_PAYMENTS_DRY_RUN", value = "false" }'),
    ("ui/test/roadmap-copy.test.js", "EXECUTION_CLAIM_PATTERN"),
    # Paper-trading row (2026-09-03, #1807). The ledger says the "carries to mainnet"
    # retraction is guarded by a word-level ban over four named files; PAPER_SURFACES is
    # that list. Renaming or deleting it leaves the row asserting a guard that is gone.
    ("ui/test/no-mainnet-track-record.test.js", "const PAPER_SURFACES"),
)

# The ledger records that board-level FDR MOVED to the leaderboard (#1564/#1580), so the
# per-strategy gate module must not define the model again. Narrow on purpose: the file
# still *mentions* the old field names in the comment that records the move, and a blunt
# substring ban would fail on that comment while proving nothing.
_ABSENCE_PIN = ("backend/archimedes/api/selection_bias_routes.py", "class BoardLevelFdr")

# The paper-trading row says the "carries to mainnet" retraction reached the machine and doc
# surfaces too. Those four are outside `ui/test/no-mainnet-track-record.test.js`, which pins
# ui files only, so without this the row could rot back to false with every suite green —
# the phrase reappearing in a docstring a `git grep` would find and no test would.
_RETRACTED_PHRASE_PINS: tuple[tuple[str, str], ...] = (
    ("docs/api/paper-trading.md", "carries to mainnet"),
    ("backend/archimedes/models/paper_store.py", "carries to mainnet"),
    ("backend/archimedes/services/paper_marks.py", "carries to mainnet"),
    ("backend/migrations/versions/e41c7a9b2d63_add_paper_marks.py", "carries to mainnet"),
)

# The sentences the ledger's OVER-CLAIMED rows say are still live. See the module
# docstring: fixing one of these SHOULD break this test.
#
# The two agent-surface entries retired themselves in #1650, exactly as designed:
# scrubbing the sentence turned this test red, which is what forced the ledger rows
# from OVER-CLAIMED to CHANGED in the same change. They are not replaced by
# "must stay absent" pins here — that property belongs to the surfaces' own guard
# (``ui/test/roadmap-copy.test.js``, which requires roadmap tense on every vault
# sentence in ``ui/public/``), not to a ledger-citation test.
_OPEN_OVERCLAIMS: tuple[tuple[str, str], ...] = (
    # The index.html generation-on-chain tags and the user-stories present-tense
    # vault sentence were FIXED by the 2026-09-01 copy-honesty pass. Their
    # ledger rows moved OVER-CLAIMED → CHANGED in the same change. No open
    # over-claim pins remain here; a new OVER-CLAIMED row must add a pin.
)


def _ledger_text() -> str:
    assert LEDGER.exists(), f"{LEDGER} is missing — the doc index promises it"
    return LEDGER.read_text(encoding="utf-8")


def _citations() -> list[tuple[str, int | None]]:
    return [(m.group(1), int(m.group(2)) if m.group(2) else None) for m in _CITATION_RE.finditer(_ledger_text())]


def _shorthand_citations() -> list[tuple[str, int]]:
    """Resolve every bare ``:NN`` to the full path citation that anchors it.

    Anchor = the nearest full citation earlier in the SAME line. A shorthand with no anchor
    on its line is unresolvable prose and is reported by its own test rather than silently
    dropped — dropping it is precisely how the hole this closes came to exist.
    """
    resolved: list[tuple[str, int]] = []
    for line in _ledger_text().splitlines():
        anchors = list(_CITATION_RE.finditer(line))
        for short in _SHORTHAND_RE.finditer(line):
            prior = [a for a in anchors if a.start() < short.start()]
            if prior:
                resolved.append((prior[-1].group(1), int(short.group(1))))
    return resolved


def _unanchored_shorthands() -> list[str]:
    orphans: list[str] = []
    for line in _ledger_text().splitlines():
        anchors = list(_CITATION_RE.finditer(line))
        for short in _SHORTHAND_RE.finditer(line):
            if not [a for a in anchors if a.start() < short.start()]:
                orphans.append(f"{short.group(0)} in: {line.strip()[:90]}")
    return orphans


def _pending_paths() -> list[str]:
    block = _PENDING_BLOCK_RE.search(_ledger_text())
    assert block is not None, "the claims-ledger:pending-paths block is missing — it is part of the contract"
    return [line.strip() for line in block.group(1).splitlines() if _PENDING_PATH_RE.match(line.strip())]


def _claim_rows() -> list[list[str]]:
    """Ledger rows: a markdown table row with a claim, a status, and evidence.

    Two-column tables (the "How to read a row" legend) and separator rows are not claims
    and are skipped here rather than special-cased at each call site.
    """
    rows: list[list[str]] = []
    for line in _ledger_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3 or set(cells[0]) <= {"-", ":"}:
            continue
        if cells[1] == "Status":
            continue
        rows.append(cells)
    return rows


class TestLedgerCitationsResolve:
    """Every path the ledger cites is a real file, at a real line."""

    def test_every_cited_path_exists(self):
        pending = set(_pending_paths())
        missing = [path for path, _ in _citations() if path not in pending and not (REPO_ROOT / path).is_file()]
        assert not missing, (
            "claims-ledger.md cites paths that do not exist: "
            + ", ".join(sorted(set(missing)))
            + ". Fix the citation, or declare the path in the claims-ledger:pending-paths block."
        )

    def test_every_cited_line_is_inside_its_file(self):
        pending = set(_pending_paths())
        out_of_range: list[str] = []
        for path, line in _citations():
            if line is None or path in pending:
                continue
            target = REPO_ROOT / path
            if not target.is_file():
                continue  # reported by the test above; do not double-report
            length = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
            if line > length:
                out_of_range.append(f"{path}:{line} (file has {length} lines)")
        assert not out_of_range, "claims-ledger.md cites lines past end of file: " + ", ".join(out_of_range)

    def test_every_shorthand_line_is_inside_its_anchor_file(self):
        """The ``:NN`` shorthands are citations too, and were the guard's blind spot."""
        pending = set(_pending_paths())
        out_of_range: list[str] = []
        for path, line in _shorthand_citations():
            if path in pending:
                continue
            target = REPO_ROOT / path
            if not target.is_file():
                continue  # reported by test_every_cited_path_exists
            length = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
            if line > length:
                out_of_range.append(f"{path}:{line} (file has {length} lines)")
        assert not out_of_range, "claims-ledger.md shorthand citations point past end of file: " + ", ".join(
            out_of_range
        )

    def test_no_shorthand_is_left_without_an_anchor(self):
        orphans = _unanchored_shorthands()
        assert not orphans, (
            "claims-ledger.md has `:NN` shorthands with no full path citation earlier on the "
            "same line, so there is nothing to resolve them against: " + "; ".join(orphans)
        )

    def test_the_citation_parser_is_not_vacuous(self):
        """A ledger that cites nothing would pass every check above."""
        assert len(_citations()) >= 40, f"only {len(_citations())} citations parsed — the parser or the ledger broke"

    def test_the_shorthand_parser_is_not_vacuous(self):
        """Anti-vacuity for the two checks above: they must actually be resolving rows."""
        found = len(_shorthand_citations())
        assert found >= 20, f"only {found} shorthand citations resolved — the parser or the ledger broke"


class TestPendingExemptionsRetireThemselves:
    def test_pending_paths_have_not_landed_yet(self):
        landed = [p for p in _pending_paths() if (REPO_ROOT / p).is_file()]
        assert not landed, (
            "these paths now exist and are no longer 'pending': "
            + ", ".join(landed)
            + ". Re-point the ledger row at the real evidence and drop the exemption."
        )

    def test_the_pending_block_is_parsed(self):
        """Anti-vacuity for the PARSER, not for the content.

        The original form asserted the live block was non-empty — right up
        until the last promised path (docs/adr/market-data-sourcing.md, #1627)
        actually landed and the exemption retired itself, at which point an
        empty block is the CORRECT state, not a regex silently failing. The
        parser's health is proven against a fixture instead, so a broken
        _PENDING_BLOCK_RE can never again hide behind "well, it parsed to
        empty"; the live block only has to exist.
        """
        fixture = "<!-- claims-ledger:pending-paths\n     prose the parser must skip\ndocs/adr/example.md\n-->"
        m = _PENDING_BLOCK_RE.search(fixture)
        assert m is not None, "the pending-paths regex no longer matches its own documented shape"
        parsed = [
            line.strip()
            for line in m.group(1).splitlines()
            if line.strip() and not line.strip().startswith(("Paths", "STILL", "red", "prose"))
        ]
        assert parsed == ["docs/adr/example.md"], f"parser mis-read the fixture block: {parsed}"
        assert _PENDING_BLOCK_RE.search(LEDGER.read_text()), (
            "the claims-ledger:pending-paths block is missing from the ledger — "
            "it stays, empty or not, as the landing place for the next promised path"
        )


# `{ name = "<var>", value = "<value>" }` — the shape every env entry in the ECS container
# definitions takes (see infra/ecs.tf). Matched against committed text only, same as every
# other check in this file: no import of archimedes, no reading of the live task definition.
_ECS_ENV_VAR_RE = re.compile(r'\{\s*name\s*=\s*"(?P<name>[^"]+)"\s*,\s*value\s*=\s*"(?P<value>[^"]*)"\s*\}')


def _pinned_env_value(hcl_text: str, var_name: str) -> str | None:
    """The value pinned for `var_name` in an ECS container `environment` block, or None if unpinned."""
    for m in _ECS_ENV_VAR_RE.finditer(hcl_text):
        if m.group("name") == var_name:
            return m.group("value")
    return None


class TestVendorCutoverStillPending:
    """Self-retiring pin for the claims-ledger row 'Paid analysis runs on licensed data'.

    That row is `PENDING VENDOR CUTOVER`: the ADR is merged and the Tiingo secret is wired
    (#1798), but nothing has pointed a seam's default vendor at it. Since #1798 the daily
    seam reads `MARKET_DATA_DAILY_PROVIDER`, falling back to `MARKET_DATA_PROVIDER`
    (`market_data_provider.py::provider_name`), and defaults to `"yfinance"` when neither is
    set. `infra/ecs.tf` pins neither today. The moment it pins either to something other
    than `"yfinance"`, the owner has performed the cutover and this pin goes red — on
    purpose, so the ledger row cannot rot true by silent drift the way the equivalent
    file-existence exemption above already did once.
    """

    def test_ecs_tf_pins_no_non_yfinance_daily_provider(self):
        ecs_tf = (REPO_ROOT / "infra" / "ecs.tf").read_text()
        for var in ("MARKET_DATA_DAILY_PROVIDER", "MARKET_DATA_PROVIDER"):
            value = _pinned_env_value(ecs_tf, var)
            assert value in (None, "yfinance"), (
                f"infra/ecs.tf now pins {var}={value!r} — the Tiingo cutover has happened. "
                "Re-point docs/claims-ledger.md's 'Paid analysis runs on licensed data' row "
                "off PENDING VENDOR CUTOVER and onto the verified-pull record "
                "(scripts/verify_market_data.py)."
            )

    def test_the_env_var_parser_is_not_vacuous(self):
        """Anti-vacuity for the parser: proven against a fixture that DOES pin a cutover,
        so the assertion above is known to be capable of going red rather than
        vacuously matching `None` forever."""
        fixture = (
            "environment = [\n"
            '  { name = "AWS_REGION", value = "us-east-1" },\n'
            '  { name = "MARKET_DATA_DAILY_PROVIDER", value = "tiingo" },\n'
            "]"
        )
        assert _pinned_env_value(fixture, "MARKET_DATA_DAILY_PROVIDER") == "tiingo"
        assert _pinned_env_value(fixture, "MARKET_DATA_PROVIDER") is None


class TestLedgerRowsSayOnlyWhatTheyMay:
    def test_every_row_carries_a_declared_status(self):
        bad = [(row[0][:70], row[1]) for row in _claim_rows() if row[1].strip("`") not in ALLOWED_STATUSES]
        assert not bad, (
            "rows with an undeclared status: "
            + "; ".join(f"{claim!r} -> {status!r}" for claim, status in bad)
            + f". Allowed: {sorted(ALLOWED_STATUSES)}"
        )

    def test_every_row_cites_a_file(self):
        """An evidence cell that names only an issue number is a pointer, not evidence.

        The failure this catches is the one the ledger is for: a row that reads as verified
        because it cites *something*, where the something is a link to a discussion.
        """
        uncited = [row[0][:70] for row in _claim_rows() if not _CITATION_RE.search(row[2])]
        assert not uncited, "rows with no file citation in the evidence column: " + "; ".join(repr(c) for c in uncited)

    def test_the_row_parser_is_not_vacuous(self):
        rows = _claim_rows()
        assert len(rows) >= 20, f"only {len(rows)} claim rows parsed — the parser or the ledger broke"

    def test_status_vocabulary_rejects_a_hedge(self):
        """Anti-vacuity for the status check: the allowed set must actually exclude things.

        A vocabulary that admits any word is not a vocabulary. These are the hedges a
        ledger drifts toward when nobody is enforcing it.
        """
        for hedge in ("MOSTLY TRUE", "TRUE-ISH", "SOFTEN", "Keep", "true"):
            assert hedge not in ALLOWED_STATUSES

    def test_the_legend_documents_exactly_the_allowed_statuses(self):
        text = _ledger_text()
        for status in ALLOWED_STATUSES:
            assert f"| `{status}` |" in text, f"{status} is allowed by this test but not explained in the ledger"


class TestLedgerClaimsMatchTheTree:
    def test_cited_symbols_still_exist(self):
        missing = [
            f"{path} :: {needle}"
            for path, needle in _SYMBOL_PINS
            if needle not in (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        ]
        assert not missing, (
            "claims-ledger.md rows lean on symbols that are gone: "
            + "; ".join(missing)
            + ". The claim may still be true, but the row's evidence is not."
        )

    def test_board_fdr_model_did_move_off_the_per_strategy_gate(self):
        path, needle = _ABSENCE_PIN
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        assert needle not in text, (
            f"{needle} is defined in {path} again — the ledger says #1564 moved it to the "
            "leaderboard. Either the move was reverted or the ledger row is wrong."
        )

    def test_retracted_phrases_have_not_come_back(self):
        """The RETRACTED rows are absences, and an absence rots silently unless it is pinned."""
        back = [
            f"{path} :: {phrase!r}"
            for path, phrase in _RETRACTED_PHRASE_PINS
            if phrase in (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        ]
        assert not back, (
            "claims-ledger.md marks these RETRACTED, but the phrase is in the tree again: "
            + "; ".join(back)
            + ". The Arc mainnet cutover is cancelled (#1240) — say what is true today (a paper "
            "track record on Arc testnet, no real funds), or move the ledger row off RETRACTED."
        )

    def test_the_retracted_phrase_pins_are_not_vacuous(self):
        """A pin that names a file with no such phrase to begin with proves nothing.

        Each pinned path must be a file that still *discusses* the paper record, so the
        absence being asserted is an absence from the surface that carried the claim rather
        than from an unrelated file that would trivially never contain it.
        """
        assert _RETRACTED_PHRASE_PINS, "the retracted-phrase pins were emptied out"
        contextless = [
            path
            for path, _ in _RETRACTED_PHRASE_PINS
            if "paper track record" not in (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        ]
        assert not contextless, (
            "these pinned paths no longer carry the corrected sentence, so the absence they assert "
            "is vacuous: " + ", ".join(contextless)
        )

    def test_the_published_exit_code_table_covers_every_code_the_cli_defines(self):
        """Successor to ``test_published_exit_codes_still_omit_incomplete``.

        That test was a self-retiring pin: it asserted the manifest's ``EXIT_CODES``
        still stopped at ``3`` while ``exits.py`` defined ``INCOMPLETE = 4``, and it
        instructed whoever fixed the manifest to move the ledger row and delete it.
        The CLI ``generate`` work did exactly that — it published ``4`` and added
        ``5``–``8`` — so the pin is retired and the ledger row is now ``CHANGED``.

        What replaces it is the guard the original defect actually wanted: not "is
        this one omission still present" but "can an omission happen again". Adding
        a code to ``exits.py`` without publishing it now fails here. The CLI's own
        suite asserts the same invariant from the other side
        (``cli/tests/test_manifest.py``); this copy exists because the ledger row
        rests on it and CI runs the backend suite on every PR, including ones that
        touch ``cli/`` without running the CLI suite.
        """
        manifest = (REPO_ROOT / "cli/src/archimedes_cli/manifest.py").read_text(encoding="utf-8")
        exits_src = (REPO_ROOT / "cli/src/archimedes_cli/exits.py").read_text(encoding="utf-8")
        block = re.search(r"EXIT_CODES\s*=\s*\{(.*?)\n\}", manifest, re.DOTALL)
        assert block is not None, "EXIT_CODES table not found in manifest.py — the ledger row cites it"
        published = set(re.findall(r'"(\d+)":', block.group(1)))
        defined = set(re.findall(r"^[A-Z_]+ = (\d+)$", exits_src, re.MULTILINE))
        assert defined, "no exit codes parsed from exits.py — the parser broke, not the contract"
        assert defined <= published, (
            f"exits.py defines codes the manifest does not publish: {sorted(defined - published)}. "
            "The machine-readable table is what a CI job branches on; an unpublished code is an "
            "undocumented one."
        )

    def test_open_overclaims_are_still_present(self):
        fixed = [
            f"{path} :: {needle}"
            for path, needle in _OPEN_OVERCLAIMS
            if needle not in (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        ]
        assert not fixed, (
            "these over-claims are gone from the tree: "
            + "; ".join(fixed)
            + ". Good — now move the matching claims-ledger.md row from OVER-CLAIMED to CHANGED "
            "and record what fixed it."
        )


class TestLedgerIsIndexed:
    def test_docs_index_links_the_ledger(self):
        """`docs/doc-index.md` says a doc not listed there does not exist. Hold it to that."""
        assert "claims-ledger.md" in DOCS_INDEX.read_text(encoding="utf-8"), (
            "docs/doc-index.md has no row for claims-ledger.md — add one in the same commit"
        )

    def test_no_index_row_is_two_rows_glued_together(self):
        """A `||` inside a table row silently eats the rest of the line in GFM.

        This is how the ledger's own index row came to exist twice: one copy glued onto the
        end of the row above it with `||`, where the renderer reads the surplus cells as
        extra columns and drops them. The row looks present in the file and is absent on the
        page — the exact rot an index is supposed to be immune to. An empty cell is `| |`.
        """
        glued = [
            f"{n}: {line.strip()[:90]}"
            for n, line in enumerate(DOCS_INDEX.read_text(encoding="utf-8").splitlines(), start=1)
            if line.lstrip().startswith("|") and "||" in line
        ]
        assert not glued, (
            "docs/doc-index.md has table rows with `||`, which renders as dropped columns: "
            + "; ".join(glued)
            + ". Split them into separate rows, or write an empty cell as `| |`."
        )
