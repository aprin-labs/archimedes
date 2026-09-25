"""Deterministic, pre-inference screening of untrusted text (#1801).

Three kinds of text reach a prompt in this system and none of them is trusted:

1. **The user's brief.** `GenerateBrief.intent` is inserted verbatim into the
   validator message (`generation_pipeline._validate_brief`) and, as
   `strategic_direction`, verbatim into the fusion proposer prompt for every
   steer (`strategy_fusion.propose`). Before this module the only deterministic
   check was `cheap_brief_reject` — min-length, word-token and keyboard-mash
   heuristics — which deliberately deferred jailbreak attempts to an LLM
   validator that itself failed OPEN on every error path. An LLM guarding an
   LLM, with the guard's failure mode being "admit everything".
2. **Model output that re-enters a prompt.** `strategy_name` on the debate
   candidate cards (`debate_engine._candidate_cards`) and the opponent's claim
   prose in the round-2 rebuttal (`debate_engine._turn`) are both interpolated
   unescaped into a single-line context. A name carrying ``\\n[C6] …`` forges a
   candidate card that no proposer produced.
3. **Third-party corpus metadata.** A paper `title` is printed on the debate
   card (`debate_engine._candidate_cards`) and on the portfolio agent's
   strategy line (`portfolio_agent._format_strategies`), in both cases raw
   inside a line-oriented format. arXiv titles are not text this project
   authors, and only two of the four ingest paths normalise whitespace in
   them, so a title carrying ``\\n      sharpe=9.99`` forges a metric.
   :func:`quote_for_prompt` screens them and renders them as one quoted data
   token.

This module is the deterministic answer to all three: pure Python, no network,
no LLM, no I/O. It decides admission; the LLM validator is downstream of it.

Two invariants worth stating out loud, because they are what make this
defensible rather than decorative:

* **Fail closed.** Any internal exception returns ``allow=False`` with
  ``screen.internal_error``. A screener that crashes must refuse, not admit —
  the exact inverse of the fail-open validator this replaces.
* **Never rewrite.** A rejected string is *omitted* from the outgoing prompt
  and the omission is recorded. Stored text — `transcript_json`, the brief on
  the job record — is never modified by this module. Prompt assembly may
  decline to include something and say so; it does not silently edit history.
  The single exception is :func:`quote_for_prompt`, which renders an ADMITTED
  title as a JSON string literal: an encoding of the same bytes that
  ``json.loads`` reverses, applied to the outgoing prompt only.
* **Match on a canonical copy.** Every rule runs against a normalized COPY
  (see ``_canonical``) as well as the raw string, because a regex word anchor
  is defeated by a character nobody can see — a zero-width space inside
  ``ig<ZWSP>nore``, a Cyrillic ``о`` in ``ignоre``, a fullwidth ``Ｓｙｓｔｅｍ``.
  The copy is computed per call and thrown away; it never reaches a prompt, a
  transcript or a store. Canonicalising for matching is not rewriting.

What this is NOT: a semantic judge. It has no opinion on whether a brief is a
*good* brief, whether it is on-topic, or whether the strategy it describes is
sane. Off-topic-but-grammatical text ("add flour and bake at 350F") passes
here and is the LLM validator's problem, exactly as before. Unfamiliar
vocabulary is never a rejection reason — most real briefs contain some.

Reason codes are a stable, versioned vocabulary: see ``ALL_CODES`` and
``RULESET_VERSION`` at the bottom of this module, and the red/green corpus at
``backend/tests/fixtures/brief_screen/``. Changing the code set without
bumping the version fails ``test_brief_screen.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

# Leaf import, deliberately. ``archimedes.api.generate_schemas`` imports only
# pydantic + stdlib and ``archimedes/api/__init__.py`` is a two-line comment,
# so this cannot re-enter the ``archimedes.services`` package the way a
# ``strategy_fusion`` import would (see the circular-import note in
# generate_schemas.py). It gives the length bound and the control-character
# class ONE definition shared by the request schema and the screener, instead
# of two copies held together by a drift test.
from archimedes.api.generate_schemas import CONTROL_CHARS, INTENT_MAX_LEN

logger = logging.getLogger(__name__)


# ── Surfaces ──────────────────────────────────────────────────────────────


class Surface(str, Enum):
    """Where the text came from — which decides which rule families run.

    ``BRIEF`` is text a human typed and is about to be spent on inference:
    it must be shaped like a brief (SHAPE), must not carry instructions aimed
    at the model (INJECT), and must be language rather than mash (LANG).

    ``MODEL_TEXT`` is text a model produced that is about to be interpolated
    back into another prompt. Shape and language rules do NOT apply — a
    two-word strategy name is not "too short", and model prose is not mash —
    but structural forgery (STRUCT) does, because that text lands inside a
    line-oriented card format, and INJECT does, because a compromised or
    merely creative model is not a trusted author either.

    ``PAPER_TITLE`` is third-party corpus metadata — an arXiv title — about to
    be printed on a debate card or a portfolio-agent strategy line. It is the
    surface with the loudest false-positive cost of the three, because a
    refused title strips real evidence off a card that the model is then asked
    to reason about, so it runs the fewest rules: the bounds, the delimiter
    markers that quoting cannot neutralise, PII, and the instruction-shaped
    INJECT rules. It deliberately skips ``inject.schema_forgery``'s bare-key
    branch (``"Confidence: a Bayesian…"`` is ordinary title punctuation, and a
    quoted title cannot terminate a JSON reply) and it skips the newline rule,
    because a line break in a title is an ingestion artefact — two of the four
    ingest paths only ``.strip()`` — and :func:`quote_for_prompt` escapes it
    rather than dropping the paper.
    """

    BRIEF = "brief"
    MODEL_TEXT = "model_text"
    PAPER_TITLE = "paper_title"


#: Upper bound for one model-produced string interpolated into a prompt.
#: A strategy name is ~40 chars and a claim ~200; 1,000 is generous headroom
#: while still bounding what one turn can inject into the next one's prompt.
MODEL_TEXT_MAX_CHARS = 1000

#: Upper bound for one paper title printed on a prompt card. arXiv's longest
#: titles run to ~250 characters; 300 is headroom over the real distribution
#: while still bounding what a single hostile corpus row can spend of the
#: debate's prompt budget (five cards × N cited papers per card).
PAPER_TITLE_MAX_CHARS = 300


# ── Verdict ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """The screening decision.

    ``reason`` and ``hint`` keep the exact shape and register the LLM
    validator's invalid-brief output uses (``generation_pipeline``'s
    ``_invalid_brief_message`` frames ``reason`` inside a sentence), so wiring
    this in front of the validator moves no API contract: ``reason`` is a
    lowercase fragment with no trailing period, ``hint`` is a full sentence.

    ``code`` is the machine-readable reason. ``None`` iff ``allow`` is True.
    """

    allow: bool
    code: str | None
    reason: str
    hint: str


_ALLOW = Verdict(allow=True, code=None, reason="", hint="")

_BRIEF_HINT = "Mention an asset class, a goal, or a risk appetite."
_INJECT_HINT = (
    "Describe the portfolio you want, not what the model should do. "
    "Links, code blocks, encoded blobs and instructions aimed at the system are not accepted."
)


def _reject(code: str, reason: str, hint: str) -> Verdict:
    return Verdict(allow=False, code=code, reason=reason, hint=hint)


# ── CANON — one normalisation pass, run before any rule ──────────────────
#
# The threat this closes: every INJECT pattern below is anchored on ``\b`` and
# on literal English words, and all three anchors are broken by characters a
# human reviewer cannot see or cannot tell apart. ``ig<U+200B>nore`` renders as
# "ignore"; ``ignоre`` with a Cyrillic "о" renders as "ignore"; ``Ｓｙｓｔｅｍ
# ｐｒｏｍｐｔ`` renders as "System prompt". None of the three is something a
# person writing about bonds produces by accident, and each is one keystroke
# for an attacker — the same argument the module already makes for refusing
# control characters, applied to the characters that survive a copy-paste.
#
# What this is NOT is a rewrite. ``_canonical`` returns a COPY used for
# matching and then discarded; the string that reaches the prompt, the
# transcript and the job record is byte-for-byte what its author wrote.

#: Invisible in rendered text, and every one of them splits a regex word
#: anchor: zero-width space / non-joiner / joiner, the LTR and RTL marks, word
#: joiner, the invisible math operators, the BOM, and the soft hyphen.
_ZERO_WIDTH = re.compile(r"[\u00ad\u200b-\u200f\u2060-\u2064\ufeff]")

#: Non-breaking and exotic spaces fold to a space; the Unicode line and
#: paragraph separators fold to a newline, so a payload that splits a rule
#: across ``U+2028`` is read as the line break it renders as.
_WHITESPACE_FOLD = str.maketrans({"\u00a0": " ", "\u202f": " ", "\u2007": " ", "\u2028": "\n", "\u2029": "\n"})

#: Cyrillic and Greek letters that are pixel-identical to a Latin letter in
#: the fonts a browser actually uses. Deliberately a SHORT, explicit table
#: rather than a general confusables database: these are the ones that spell
#: an English instruction word, and an explicit table can never quietly start
#: folding a legitimate Cyrillic or Greek brief into something the mash
#: heuristic mistakes for junk. Lowercase Greek is absent on purpose — it does
#: not spell English words, and mapping it would damage real Greek text.
_CONFUSABLES = str.maketrans(
    {
        # Cyrillic
        "\u0410": "A",  # А
        "\u0430": "a",  # а
        "\u0412": "B",  # В
        "\u0415": "E",  # Е
        "\u0435": "e",  # е
        "\u0406": "I",  # І
        "\u0456": "i",  # і
        "\u041a": "K",  # К
        "\u041c": "M",  # М
        "\u041d": "H",  # Н
        "\u041e": "O",  # О
        "\u043e": "o",  # о
        "\u0420": "P",  # Р
        "\u0440": "p",  # р
        "\u0421": "C",  # С
        "\u0441": "c",  # с
        "\u0422": "T",  # Т
        "\u0425": "X",  # Х
        "\u0445": "x",  # х
        "\u0423": "Y",  # У
        "\u0443": "y",  # у
        # Greek (uppercase only)
        "\u0391": "A",  # Α
        "\u0392": "B",  # Β
        "\u0395": "E",  # Ε
        "\u0396": "Z",  # Ζ
        "\u0397": "H",  # Η
        "\u0399": "I",  # Ι
        "\u039a": "K",  # Κ
        "\u039c": "M",  # Μ
        "\u039d": "N",  # Ν
        "\u039f": "O",  # Ο
        "\u03a1": "P",  # Ρ
        "\u03a4": "T",  # Τ
        "\u03a5": "Y",  # Υ
        "\u03a7": "X",  # Χ
    }
)

_HORIZONTAL_RUN = re.compile(r"[^\S\n]+")
_VERTICAL_RUN = re.compile(r"[^\S\n]*\n[\s]*")


def _canonical(text: str) -> str:
    """The matching copy: what the text *renders as*, spelled canonically.

    NFKC (fullwidth → ASCII, ligatures, compatibility forms) → drop the
    invisibles → fold the exotic spaces and separators → fold the homoglyphs →
    collapse runs of whitespace, keeping ONE newline where a line break was.

    The newline is kept deliberately: ``_ROLE_FORGERY``'s ``^system:`` anchor
    and ``_struct_rules``' line-break rule both read line structure, and a
    flat collapse to spaces would silently delete two rules.
    """
    folded = unicodedata.normalize("NFKC", text)
    folded = _ZERO_WIDTH.sub("", folded)
    folded = folded.translate(_WHITESPACE_FOLD)
    folded = folded.replace("\r\n", "\n").replace("\r", "\n")
    folded = folded.translate(_CONFUSABLES)
    folded = _HORIZONTAL_RUN.sub(" ", folded)
    return _VERTICAL_RUN.sub("\n", folded)


def _hits(pattern: re.Pattern[str], text: str, canon: str) -> bool:
    """Does this pattern fire on the raw text OR on its canonical copy?

    Both, never canonical alone. Collapsing whitespace is what makes the
    homoglyph and zero-width folds work, but it also erases the four-space
    indent that marks a markdown code block — so the raw string keeps its own
    vote. Searching a superset can only remove false negatives.
    """
    return bool(pattern.search(text) or pattern.search(canon))


# ── LANG — lifted verbatim from generation_pipeline (Lane 1.3c) ───────────
#
# Everything from here to the end of ``_lang_rules`` moved out of
# ``generation_pipeline.cheap_brief_reject`` unchanged, including the
# non-ASCII pass-through. It must stay conservative: a false NEGATIVE (missing
# real gibberish) is fine — the LLM validator still sees it. A false POSITIVE
# is not: `cheap_brief_reject` runs BEFORE the payment gate, so flagging a
# genuine brief refuses a paying user before they are even offered the chance
# to pay. Unfamiliar vocabulary is therefore never a rejection reason —
# "muni ladder", "SPY covered calls" and non-English text all pass.

_MIN_INTENT_CHARS = 3  # the request schema's min_length=1 only bars empty; 1-2 chars is not a brief
_MIN_GIBBERISH_TOKENS = 2  # ≥2 tokens before any mash token counts as junk

_VOWELS = frozenset("aeiouy")

# Straight runs across a keyboard row, forwards and backwards, are a
# fingerprint of mashing rather than typing ("asdf", "lkjh", "qwer", "poiu").
# No English word contains one; checked as 4-char windows.
_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
_KEYBOARD_RUNS = frozenset(
    row[i : i + 4] for base in _KEYBOARD_ROWS for row in (base, base[::-1]) for i in range(len(row) - 3)
)


def _looks_like_mash(token: str) -> bool:
    """Is this lowercased token *shaped* like keyboard mash?

    Structural only — this asks whether the letters could plausibly have been
    typed as a word, never whether the word is one we happen to know. A brief
    is full of words no list here contains ("muni", "ladder", "covered"), so
    unfamiliarity carries no signal at all.

    Non-ASCII tokens always return False. A Cyrillic, Greek or CJK brief has
    no vowel/consonant structure this test can read, and mis-reading one as
    mash would refuse a legitimate non-English user before they can even pay;
    those defer to the LLM validator, exactly like off-topic English does.
    """
    if not token.isascii():
        return False
    if not any(ch in _VOWELS for ch in token):
        return True  # "zxcvbnm", "qwrtp" — no vowel, not pronounceable
    run = 0
    for ch in token:
        run = 0 if ch in _VOWELS else run + 1
        if run >= 5:
            return True  # "lkjhgfdsa" — 5+ consonants with no break
    if any(a == b == c for a, b, c in zip(token, token[1:], token[2:], strict=False)):
        return True  # "aaargh", "jjjj"
    return any(token[i : i + 4] in _KEYBOARD_RUNS for i in range(len(token) - 3))


# Ordinary English function/content words. Their presence means the text is
# at least grammatical, even if off-topic — off-topic is the LLM's job, not
# this heuristic's.
_COMMON_WORDS = frozenset(
    "a an the and or but for nor so if of to in on at by with from into is are "
    "was were be been being this that these those i you he she it we they my "
    "your his her its our their not no yes want need make build create "
    "generate please can could would should like about money fund funds".split()
)

# Investing / finance-signal vocabulary. Presence means the text is on-topic
# even when it fails the common-word check above (e.g. "crypto momentum").
_FINANCE_WORDS = frozenset(
    "stock stocks bond bonds equity equities crypto bitcoin ethereum token "
    "tokens coin coins etf etfs treasury treasuries yield yields dividend "
    "dividends momentum value growth trend trending hedge hedged leverage "
    "leveraged volatility volatile vol risk risky conservative aggressive "
    "moderate income rebalance rebalancing diversify diversified "
    "diversification asset assets allocation market markets trading trade "
    "trades rate rates inflation macro commodity commodities gold silver "
    "oil futures options derivative derivatives arbitrage carry basis "
    "spread stablecoin usdc usdt defi staking lending long short bull bear "
    "index indices quant quantitative alpha beta sharpe drawdown portfolio "
    "invest investment strategy strategies".split()
)


def _lang_rules(_text: str, canon: str) -> Verdict:
    """``lang.no_words`` / ``lang.mash`` — is this language at all?"""
    # Unicode-aware: letters in ANY script, no digits/underscores. A Cyrillic
    # or CJK brief must tokenize as words, not vanish into the letter-free
    # branch below and be refused for "containing no words". Read off the
    # canonical copy so a brief padded with zero-width characters tokenizes as
    # the words it renders as.
    tokens = re.findall(r"[^\W\d_]{2,}", canon)
    if not tokens:
        # No word-like content at all — pure digits/punctuation/symbols.
        return _reject("lang.no_words", "it does not contain any words", _BRIEF_HINT)

    lowered = [t.lower() for t in tokens]
    if any(t in _COMMON_WORDS or t in _FINANCE_WORDS for t in lowered):
        return _ALLOW  # recognizable language — defer to the real validator

    if all(t.isupper() and len(t) <= 5 for t in tokens):
        return _ALLOW  # plausible ticker list, e.g. "BTC ETH SOL"

    # Junk needs positive evidence of mashing, never just unfamiliar words.
    if len(tokens) >= _MIN_GIBBERISH_TOKENS and any(_looks_like_mash(t) for t in lowered):
        return _reject("lang.mash", "it does not look like an investment goal", _BRIEF_HINT)
    return _ALLOW  # nothing mash-shaped to point at; defer to the LLM


# ── SHAPE ─────────────────────────────────────────────────────────────────


def _shape_rules_brief(text: str, canon: str) -> Verdict:
    """Empty / too short / too long / control characters.

    The control-character test reuses ``generate_schemas``' rule for the
    user-chosen strategy name: collapse whitespace FIRST so a ``\\t``/``\\n``
    arriving in a paste is normalized rather than rejected, then refuse
    anything that survives collapsing (NUL, ESC, DEL, …). Those never belong
    in text a human typed, and they are the classic way to smuggle a
    terminal-invisible payload past a human reviewer.

    Note this reads a COLLAPSED COPY. The brief itself is never rewritten —
    the stored intent is whatever the user sent.

    Emptiness and the minimum are judged on the CANONICAL copy, so a brief of
    nothing but zero-width spaces reads as the blank it renders as rather than
    as a three-character brief. The maximum is judged on the RAW string,
    because that is the bound ``GenerateBrief.intent``'s ``max_length``
    enforces one layer out — measuring a collapsed copy here would admit
    something the schema rejects.
    """
    stripped = canon.strip()
    if not stripped:
        return _reject("shape.empty", "it did not describe an investment goal", _BRIEF_HINT)
    if len(stripped) < _MIN_INTENT_CHARS:
        return _reject("shape.too_short", "too short to describe an investment goal", _BRIEF_HINT)
    if len(text) > INTENT_MAX_LEN:
        return _reject(
            "shape.too_long",
            f"it is longer than {INTENT_MAX_LEN} characters",
            "Trim it to one or two sentences — state the intent and let the pipeline choose the mechanics.",
        )
    if CONTROL_CHARS.search(re.sub(r"\s+", " ", text)):
        return _reject(
            "shape.control_chars",
            "it contained control characters",
            "Retype it as plain text — pasting from a PDF or a terminal can carry invisible characters.",
        )
    return _ALLOW


def _shape_rules_model(text: str, _canon: str) -> Verdict:
    """Model text: only the bounds that keep it safe to interpolate.

    No empty/too-short rule — an empty rebuttal is simply no rebuttal, and the
    caller already has a fallback for an empty strategy name.

    Both bounds read the RAW string on purpose: the length that matters is the
    length that lands in the next prompt, and a control character is refused,
    not folded.
    """
    if len(text) > MODEL_TEXT_MAX_CHARS:
        return _reject(
            "shape.too_long",
            f"model text longer than {MODEL_TEXT_MAX_CHARS} characters",
            "This is an internal bound; the text was left out of the next prompt.",
        )
    if CONTROL_CHARS.search(re.sub(r"[ \t]+", " ", text)):
        return _reject(
            "shape.control_chars",
            "model text contained control characters",
            "This is an internal bound; the text was left out of the next prompt.",
        )
    return _ALLOW


def _shape_rules_title(text: str, _canon: str) -> Verdict:
    """Paper title: the bounds, and nothing about emptiness or language.

    An empty title is not a refusal — both call sites already fall back to the
    bare ``arXiv:<id>`` when there is no title to print — and a title is not
    judged for "words" or mash, because a corpus row may legitimately be a
    formula, a non-English phrase or an acronym string.

    Whitespace is collapsed before the control-character test, exactly as
    ``_shape_rules_brief`` does it and unlike ``_shape_rules_model``: a tab or
    a newline inside a stored title is an ingestion artefact (only two of the
    four ingest paths normalise it), and ``quote_for_prompt`` escapes it into
    a ``\\n`` that cannot open a line. NUL, ESC and DEL still refuse.
    """
    if len(text) > PAPER_TITLE_MAX_CHARS:
        return _reject(
            "shape.too_long",
            f"paper title longer than {PAPER_TITLE_MAX_CHARS} characters",
            "This is an internal bound; the title was left off the card.",
        )
    if CONTROL_CHARS.search(re.sub(r"\s+", " ", text)):
        return _reject(
            "shape.control_chars",
            "paper title contained control characters",
            "This is an internal bound; the title was left off the card.",
        )
    return _ALLOW


# ── PII — secrets and identity numbers a brief never needs ────────────────
#
# The brief lands verbatim in a third-party model's prompt, is billed, and is
# persisted on the job record and in the raw-completion trace. Anything a user
# pastes into it that identifies them or authenticates them is therefore
# copied to a provider and stored — so the cheapest correct answer is to
# refuse it BEFORE any of that happens and tell the user which shape tripped.
#
# Every rule below needs a shape that is unambiguous on its own. A brief is a
# sentence about a portfolio: it contains numbers, tickers and percentages, so
# the discipline that governs INJECT governs this family twice over, because
# these rules read DIGITS and a finance brief is full of them. Hence: the
# email rule needs a real ``local@host.tld``; the credential rules need a
# vendor prefix or an explicit credential noun followed by a long value; the
# identity rule uses the issuing authority's own validity ranges; and the card
# rule needs a plausible network prefix AND a Luhn checksum, which alone
# throws out nine of every ten random digit runs.
#
# Phone numbers are deliberately absent — "+1 415 555 0132" and "allocate
# 1 415 555 0132 across the sleeve" are the same digits, and refusing a paying
# user for writing a number is worse than the leak. Postal addresses and IBANs
# are absent for the same reason. See docs/brief-guidelines.md § 3.1.

_PII_HINT = (
    "Remove the personal or account detail and describe the portfolio instead. "
    "A brief never needs an email address, a key, an identity number or a card number."
)

#: ``local@host.tld``. No ``\b`` anchor at the head: ``%`` and ``+`` are legal
#: in a local part and are not word characters, so a word boundary would miss
#: exactly the addresses most likely to be a real one.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,}")

#: Credential shapes. The first nine are vendor prefixes — a token that can
#: only be a token. The last is the generic form, and it is the one that
#: needs care: the noun must be an explicit credential noun (never a bare
#: "secret" or "token", which appear in ordinary prose), it must be followed
#: immediately by ``:`` or ``=``, and the value must be a long unbroken run
#: CONTAINING A DIGIT — so "my secret: momentum-and-carry" is not a key and
#: "api_key: sk_live_9f2a…" is.
#:
#: This is an enumeration of the prefixes we know, not a claim of
#: completeness; ``docs/brief-guidelines.md`` § 3.1 says so. Adding one is
#: cheap and needs no ``RULESET_VERSION`` bump, because the digest tracks the
#: reason-code SET and every prefix here reports the same ``pii.credential``.
_CREDENTIAL = (
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{16,}"),  # OpenAI / Anthropic secret keys
    # Stripe secret and restricted keys. One character away from the OpenAI
    # prefix above (``sk_`` vs ``sk-``) and the single most damaging thing a
    # user of a payments-adjacent product can paste into a text box. The
    # generic branch below does NOT catch it: "my api key is sk_live_…" has no
    # ``:`` or ``=`` after the noun.
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),  # AWS access key ids
    re.compile(r"\bxox[abeoprsu]-[A-Za-z0-9\-]{10,}"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API keys
    re.compile(r"-----BEGIN[ A-Z]{0,32}PRIVATE KEY-----"),  # PEM private key block
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9_\-.=+/]{20,}"),  # an Authorization header value
    re.compile(
        r"\b(?:api[_\- ]?keys?|access[_\- ]?keys?|secret[_\- ]?(?:access[_\- ]?)?keys?|"
        r"auth[_\- ]?tokens?|access[_\- ]?tokens?|refresh[_\- ]?tokens?|bearer[_\- ]?tokens?|"
        r"client[_\- ]?secrets?|private[_\- ]?keys?|passwords?|passphrases?|seed[_\- ]?phrases?)"
        r"\s*[:=]\s*(?=\S*\d)\S{12,}",
        re.IGNORECASE,
    ),
)

#: A US Social Security number, using the SSA's own validity rules rather than
#: a bare ``\d{3}-\d{2}-\d{4}``: area is never 000, 666 or 900–999, group is
#: never 00, serial is never 0000. The separator is captured and back-
#: referenced so it must be the SAME on both sides — "123-45 6789" is not a
#: number anyone writes, but "10-20 3040" is a range someone might.
_NATIONAL_ID = re.compile(r"(?<![\d\-])(?!000|666|9\d\d)\d{3}([\- ])(?!00)\d{2}\1(?!0000)\d{4}(?![\d\-])")

#: A digit run written the way a card number is written: either UNBROKEN
#: (13–19 digits, the shape of a paste) or grouped into fours — or Amex's
#: 4-6-5 — by a separator that is the SAME every time. This only PROPOSES a
#: candidate; ``_looks_like_card`` decides. The lookarounds exclude a decimal
#: point on either side so "1.4111111111111111" — a ratio a brief could
#: plausibly carry — is never read as an account number.
#:
#: The grouping constraint is not cosmetic, it is the false-positive fix. The
#: first version of this pattern allowed a separator between ANY two digits
#: (``\d(?:[ \-]?\d){11,18}``), which makes a parameter grid a card candidate:
#: "a 3 5 10 20 30 60 90 120 day lookback grid" is fifteen digits starting
#: with 3, and one such grid in ~25 also passes Luhn. That refused a real
#: brief BEFORE the payment gate and told the user it looked like a card
#: number. Measured over 5,000 generated briefs each: a six-number lookback
#: grid went 3.90% → 0.00%, an eight-number parameter sweep 1.42% → 0.00%.
#:
#: What survives, honestly: four 4-digit numbers separated uniformly — "over
#: 3661 3662 3663 3664 trading days" — is a 16-digit uniform grouping that
#: passes prefix and Luhn, and nothing about its SHAPE distinguishes it from a
#: PAN. That class is unchanged at ~4%, and it is stated as a boundary in
#: ``docs/brief-guidelines.md`` § 3.1 rather than papered over.
_CARD_CANDIDATE = re.compile(
    r"(?<![\d.])(?:"
    r"\d{13,19}"  # unbroken: a paste
    r"|\d{4}([ \-])(?:\d{4}\1){1,3}\d{1,4}"  # 4-4-4-N … 4-4-4-4-3, one separator throughout
    r"|\d{4}([ \-])\d{6}\2\d{5}"  # Amex's 4-6-5
    r")(?![\d.])"
)

#: Visa (4), Mastercard/Diners/JCB (3, 5), Discover/UnionPay (6). Every
#: consumer network begins with one of these; requiring it discards ~60% of
#: random digit runs before the checksum even runs.
_CARD_PREFIXES = frozenset("3456")


def _luhn_ok(digits: str) -> bool:
    """The mod-10 checksum every payment card carries.

    Roughly one random digit run in ten passes it, which is what turns "a long
    number" into "an account number" with enough confidence to refuse a brief
    over it.
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _looks_like_card(run: str) -> bool:
    """Is this candidate run actually shaped like a payment card number?"""
    digits = run.replace(" ", "").replace("-", "")
    if not 13 <= len(digits) <= 19:
        return False
    if digits[0] not in _CARD_PREFIXES:
        return False
    if len(set(digits)) == 1:
        return False  # "4444444444444444" is padding, not a PAN
    return _luhn_ok(digits)


def _pii_rules(text: str, canon: str) -> Verdict:
    """``pii.*`` — an identifier or a secret that has no business in a prompt.

    Runs against the raw string and the canonical copy like every other
    family, so an address hollowed out with a zero-width space or spelled with
    a Cyrillic homoglyph is still an address.
    """
    if _hits(_EMAIL, text, canon):
        return _reject("pii.email", "it contained an email address", _PII_HINT)
    if any(_hits(p, text, canon) for p in _CREDENTIAL):
        return _reject("pii.credential", "it contained something shaped like a key, token or password", _PII_HINT)
    if _hits(_NATIONAL_ID, text, canon):
        return _reject("pii.national_id", "it contained something shaped like a government id number", _PII_HINT)
    # ``finditer``, not ``findall``: the grouped branches back-reference their
    # separator, so the pattern has capture groups and ``findall`` would hand
    # back the separators instead of the runs.
    for match in (*_CARD_CANDIDATE.finditer(text), *_CARD_CANDIDATE.finditer(canon)):
        if _looks_like_card(match.group(0)):
            return _reject("pii.payment_card", "it contained something shaped like a payment card number", _PII_HINT)
    return _ALLOW


# ── INJECT ────────────────────────────────────────────────────────────────
#
# Every pattern below is written to need POSITIVE evidence of an instruction
# aimed at the system, never a merely unusual phrase. The false-positive cost
# is a refused paying user, so patterns that a real finance brief could
# plausibly contain are deliberately absent — most notably "act as", which is
# ordinary English in "act as a hedge against inflation".

#: verb + previous-ish + instruction-ish, within a short window. "disregard
#: prior signals" does not match (signals is not an instruction noun);
#: "ignore all previous instructions" does.
#:
#: The gap class is ``[^.]`` and NOT ``[^.\n]``: a single Enter between two
#: anchors is not a sentence boundary, it is the cheapest possible bypass of
#: this module's headline rule, and the brief box is a three-row textarea whose
#: newlines are pinned as legitimate by ``test_a_brief_may_contain_a_newline``.
#: The period still bounds the window, which is what keeps two unrelated
#: sentences from being read as one directive.
_OVERRIDE_DIRECTIVE = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass|discard|set\s+aside|put\s+aside)\b[^.]{0,40}"
    r"\b(?:previous|prior|above|earlier|preceding|foregoing|all|any|everything)\b[^.]{0,40}"
    r"\b(?:instruction|direction|prompt|rule|guideline|guardrail|restriction|constraint|"
    r"policy|message|command|system)s?\b",
    re.IGNORECASE,
)

#: Asking for the prompt back. Distinct from an override directive (nothing is
#: being disregarded) and from role forgery (no role is being reassigned): the
#: payload is "print what you were told", which is a request for the one string
#: this service must never emit. Needs a leak verb AND a prompt-ish noun AND a
#: pointer at earlier text, so "print the portfolio rules" is not enough.
_PROMPT_LEAK = re.compile(
    r"\b(?:repeat|reveal|print|show|output|display|echo|disclose|dump|reproduce|recite)\b[^.]{0,40}"
    r"\b(?:instructions?|prompts?|configuration|config|rules?|guidelines?|guardrails?|"
    r"system\s+message)\b[^.]{0,40}"
    r"\b(?:above|earlier|preceding|previous|prior|verbatim|word\s+for\s+word|in\s+full)\b",
    re.IGNORECASE,
)

_ROLE_FORGERY = (
    re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"^\s*(?:system|assistant|user|human)\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bpretend\s+(?:to\s+be|you\s+are|that\s+you)\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+now\s+on,?\s+you\b", re.IGNORECASE),
    re.compile(r"\b(?:developer|jailbreak|god)\s+mode\b", re.IGNORECASE),
    # Chat-template markers: ChatML, Llama-style, Anthropic-style turn tags.
    re.compile(r"<\|?\s*(?:im_start|im_end|endoftext|system|/?s)\s*\|?>", re.IGNORECASE),
    re.compile(r"\[/?INST\]|<<\s*/?SYS\s*>>"),
)

#: Keys from the two JSON reply schemas this system asks models to produce —
#: ``_BRIEF_VALIDATION_SYSTEM`` (generation_pipeline) and ``_DEBATE_SYSTEM``
#: (debate_engine). Text carrying one of these as a quoted JSON key is
#: forging a reply terminator, not describing a portfolio.
_SCHEMA_KEYS = (
    "is_valid",
    "intent_summary",
    "asset_classes_inferred",
    "time_horizon_inferred",
    "risk_appetite_adjusted",
    "verdict",
    "confidence",
    "key_claims",
    "fatal_flaws",
    "candidate_id",
    "arxiv_ids",
    "discard",
    "claim",
)
#: Straight quotes, the four smart quotes macOS/iOS/Word substitute for them
#: by default, and the backtick. Autocorrect turning ``"`` into ``“`` was
#: enough to walk a forged reply terminator straight past this rule.
_QUOTE_CHARS = "[\"'\u2018\u2019\u201c\u201d`]"

#: Quoted key, or bare key followed by a colon. The unquoted branch is there
#: because ``{is_valid: true}`` is not valid JSON and is read as a forged reply
#: by exactly the same downstream parser — and because dropping the quotes is
#: the first thing an attacker tries after the quoted form is refused. The
#: false-positive cost is a brief that writes one of these thirteen schema
#: keys immediately before a colon; a green-corpus line pins the near-miss
#: ("signal confidence, capped at 20%") that a real brief actually contains.
_SCHEMA_FORGERY = re.compile(
    _QUOTE_CHARS + r"(?:" + "|".join(_SCHEMA_KEYS) + r")" + _QUOTE_CHARS + r"\s*:"
    r"|(?<![\w-])(?:" + "|".join(_SCHEMA_KEYS) + r")\s*:",
    re.IGNORECASE,
)

_CODE_FENCE = re.compile(r"(?:`{3,}|~{3,})")

#: Markdown's OTHER code block: a blank line followed by a line indented four
#: spaces (or a tab). It renders exactly like a fenced block and it carried a
#: ``SYSTEM OVERRIDE`` payload past the fence rule. The blank line is required
#: on purpose — a brief that merely opens with an accidental indent is not a
#: code block in markdown either, and refusing it would cost a real user.
_INDENTED_CODE = re.compile(r"\n[ \t]*\n[ \t]*(?: {4}|\t)\S")

#: An HTML comment. Invisible everywhere the brief is rendered, carried
#: verbatim into the prompt, and read as text by the model — the same
#: "hidden from the human, visible to the machine" shape as a control
#: character. Only the opener is matched: a bare ``-->`` is a plausible arrow
#: in ordinary prose ("momentum --> value").
_HTML_COMMENT = re.compile(r"<!--")

#: A link needs a scheme, a ``www.`` host, or a bare host FOLLOWED BY A PATH
#: OR A QUERY. The last clause is the load-bearing one: "Amazon, Alphabet and
#: Booking.com" is a list of companies, not a link, and half of a realistic
#: sample of large-cap briefs named at least one ``.com`` company. Refusing
#: those costs a paying user *before* the payment gate, which is the most
#: expensive failure this module has. Something to fetch — ``/spec``,
#: ``?payload=`` — is what separates an instruction to go elsewhere from a
#: company's name.
#:
#: The TLD list stays short for the second reason: exchange suffixes are
#: written exactly like bare hosts (".TO" Toronto, ".CO" Copenhagen, ".ME"
#: Montenegro/Moscow, ".AI", ".L" London, ".PA" Paris, ".DE" Xetra, ".HK",
#: ".SW" Swiss, ".AX" Australia), so every TLD colliding with one is left out
#: rather than refusing a user for naming a foreign-listed ticker. Green
#: corpus lines pin both halves.
_URL = re.compile(
    r"\b(?:https?|ftp|file|data)://"
    r"|\bwww\.[a-z0-9-]+\.[a-z]{2,}"
    r"|(?<![\w.])[a-z0-9](?:[a-z0-9-]{1,61})?"
    r"\.(?:com|net|org|io|xyz|ru|cn|info|biz|app|dev)(?![a-z])"
    r"(?:/\S*|\?\S+)",
    re.IGNORECASE,
)

#: A long unbroken base64/hex-looking run. Requires mixed case AND a digit so
#: a long hyphenless English phrase can never match; no English word is 40
#: characters anyway.
_B64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")

#: An all-lowercase hex run walks past ``_B64_RUN``'s mixed-case requirement,
#: and hex is the encoding an attacker reaches for once base64 is refused. The
#: same both-kinds-of-character test applies below (digits AND a-f), so a long
#: run of one letter — the ``aaaa…`` padding in the length-bound corpus lines —
#: is not mistaken for a payload.
_HEX_RUN = re.compile(r"(?<![A-Za-z0-9])(?:[0-9a-f]{2}){16,}(?![A-Za-z0-9])", re.IGNORECASE)


def _inject_rules(text: str, canon: str, *, skip: frozenset[str] = frozenset()) -> Verdict:
    """Instructions aimed at the model rather than a description of a portfolio.

    Every pattern is tried against the raw string and against its canonical
    copy (:func:`_hits`), so an invisible character or a homoglyph changes what
    the payload looks like without changing what it matches.

    ``skip`` names codes this surface does not run. It exists for exactly one
    caller — :data:`Surface.PAPER_TITLE`, which skips ``inject.schema_forgery``
    — and it is a per-surface subtraction, never a per-string exemption: no
    input can talk the screen out of a rule.
    """
    if _hits(_OVERRIDE_DIRECTIVE, text, canon) and "inject.override_directive" not in skip:
        return _reject(
            "inject.override_directive",
            "it tried to override the system's instructions",
            _INJECT_HINT,
        )
    if any(_hits(p, text, canon) for p in _ROLE_FORGERY) and "inject.role_forgery" not in skip:
        return _reject(
            "inject.role_forgery",
            "it tried to reassign the model's role",
            _INJECT_HINT,
        )
    if _hits(_PROMPT_LEAK, text, canon) and "inject.prompt_leak" not in skip:
        return _reject(
            "inject.prompt_leak",
            "it asked the system to repeat its own instructions",
            _INJECT_HINT,
        )
    if _hits(_SCHEMA_FORGERY, text, canon) and "inject.schema_forgery" not in skip:
        return _reject(
            "inject.schema_forgery",
            "it contained a forged machine reply",
            _INJECT_HINT,
        )
    if (
        _hits(_CODE_FENCE, text, canon) or _hits(_INDENTED_CODE, text, canon) or _hits(_HTML_COMMENT, text, canon)
    ) and "inject.code_fence" not in skip:
        return _reject("inject.code_fence", "it contained a code block", _INJECT_HINT)
    if _hits(_URL, text, canon) and "inject.url" not in skip:
        return _reject("inject.url", "it contained a link", _INJECT_HINT)
    if "inject.base64_blob" not in skip:
        for run in (*_B64_RUN.findall(text), *_B64_RUN.findall(canon)):
            if any(c.isdigit() for c in run) and any(c.islower() for c in run) and any(c.isupper() for c in run):
                return _reject("inject.base64_blob", "it contained encoded data", _INJECT_HINT)
        for run in (*_HEX_RUN.findall(text), *_HEX_RUN.findall(canon)):
            if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
                return _reject("inject.base64_blob", "it contained encoded data", _INJECT_HINT)
    return _ALLOW


#: The one code :data:`Surface.PAPER_TITLE` does not run, and why. A real
#: arXiv title is very often ``Noun: subtitle`` — "Confidence: a Bayesian
#: treatment of…" — and ``_SCHEMA_FORGERY``'s bare-key branch fires on
#: thirteen nouns immediately followed by a colon, three of which
#: (``verdict``, ``confidence``, ``claim``) are ordinary English. On a brief
#: that trade is right; on a title it would silently strip real evidence off a
#: card, and the quoting :func:`quote_for_prompt` applies means a title cannot
#: terminate a JSON reply the way an unquoted brief fragment could.
_TITLE_INJECT_SKIP = frozenset({"inject.schema_forgery"})


def _title_inject_rules(text: str, canon: str) -> Verdict:
    """INJECT for a corpus title — see :data:`_TITLE_INJECT_SKIP`."""
    return _inject_rules(text, canon, skip=_TITLE_INJECT_SKIP)


# ── STRUCT — model text re-entering a prompt ──────────────────────────────
#
# The debate prompt is line-oriented: `_candidate_cards` emits one
# `[C1] Name — cites arXiv:xxxx "Title"` line per candidate and the rebuttal
# clause is a single sentence. Model text carrying a newline or a card marker
# forges a candidate the proposer never produced, and the anti-hallucination
# guard in `_turn` checks claims against the ids printed on the cards — so a
# forged card is a forged evidence base, not just cosmetic.

_CARD_MARKER = re.compile(r"\[\s*C\s*\d{1,3}\s*\]")
_CITES_MARKER = re.compile(r"[—-]\s*cites\b", re.IGNORECASE)
_REPLY_MARKER = re.compile(r"\breply\s+with\s+one\s+json\b", re.IGNORECASE)


def _delimiter_rules(text: str, canon: str) -> Verdict:
    """``struct.delimiter_forgery`` — a marker that quoting cannot neutralise.

    Split out of :func:`_struct_rules` so :data:`Surface.PAPER_TITLE` can run
    it without the newline rule. Escaping turns a line break into two harmless
    characters; it does nothing at all to a literal ``[C6]``, which reads as a
    card label wherever it lands.
    """
    if _hits(_CARD_MARKER, text, canon) or _hits(_CITES_MARKER, text, canon) or _hits(_REPLY_MARKER, text, canon):
        return _reject(
            "struct.delimiter_forgery",
            "untrusted text forged a prompt delimiter",
            "This is an internal bound; the text was left out of the next prompt.",
        )
    return _ALLOW


def _struct_rules(text: str, canon: str) -> Verdict:
    """``struct.newline_in_card_field`` / ``struct.delimiter_forgery``.

    The canonical copy folds ``U+2028``/``U+2029`` to ``\n``, so a separator
    that renders as a line break is refused as one rather than sliding through
    as an ordinary character.
    """
    if "\n" in text or "\r" in text or "\n" in canon:
        return _reject(
            "struct.newline_in_card_field",
            "model text contained a line break in a single-line prompt field",
            "This is an internal bound; the text was left out of the next prompt.",
        )
    return _delimiter_rules(text, canon)


# ── Entry point ───────────────────────────────────────────────────────────

_SURFACE_RULES = {
    # SHAPE → PII → INJECT → LANG. PII runs before INJECT so a pasted
    # credential reports as a credential rather than as `inject.base64_blob`,
    # which is the code a long opaque token would otherwise land on and is a
    # materially less useful thing to tell the user. INJECT still runs before
    # LANG so a jailbreak attempt reports as a jailbreak rather than as mash.
    Surface.BRIEF: (_shape_rules_brief, _pii_rules, _inject_rules, _lang_rules),
    # STRUCT first: it is the family that describes what actually breaks when
    # model text is interpolated, so it should own the code when both apply.
    Surface.MODEL_TEXT: (_struct_rules, _shape_rules_model, _pii_rules, _inject_rules),
    # No newline rule and no LANG — see Surface.PAPER_TITLE. A title carrying
    # a line break is escaped by `quote_for_prompt`, not dropped.
    Surface.PAPER_TITLE: (_shape_rules_title, _delimiter_rules, _pii_rules, _title_inject_rules),
}

_INTERNAL_ERROR = Verdict(
    allow=False,
    code="screen.internal_error",
    reason="we could not check this brief right now",
    hint="Try again in a moment, or shorten the brief.",
)


def screen(text: str, surface: Surface | str) -> Verdict:
    """Screen one untrusted string. Fails CLOSED.

    ``surface`` selects the rule families (see :class:`Surface`). An unknown
    surface, a non-string ``text``, or any exception raised inside a rule all
    return ``screen.internal_error`` with ``allow=False``: a screener that
    cannot run must refuse, never admit.

    The canonical copy is computed ONCE here, before any rule runs, and handed
    to every rule alongside the original. No rule sees a normalized string
    without also seeing what was actually sent, and neither string is stored.

    There is deliberately no "it is only whitespace, let it through"
    short-circuit for model text: a claim of three newlines is not empty, it is
    three line breaks landing in a single-line prompt slot, and the module's
    own docstring promises that is refused.
    """
    try:
        rules = _SURFACE_RULES[Surface(surface)]
        if not isinstance(text, str):
            raise TypeError(f"screen() expects str, got {type(text).__name__}")
        canon = _canonical(text)
        for rule in rules:
            verdict = rule(text, canon)
            if not verdict.allow:
                return verdict
        return _ALLOW
    except Exception:  # pragma: no cover - exercised via monkeypatch in tests
        logger.exception("brief screen failed internally; refusing (fail-closed)")
        return _INTERNAL_ERROR


def omit_if_rejected(text: str, *, field: str, context: str = "") -> tuple[str, Verdict]:
    """Screen model text bound for a prompt; return ``("", verdict)`` if refused.

    The contract callers depend on: a refused string is OMITTED from the
    outgoing prompt and the omission is logged with its reason code. The
    original is never rewritten, never escaped, never truncated — it stays
    exactly as the model produced it in whatever store holds it.
    """
    verdict = screen(text, Surface.MODEL_TEXT)
    if verdict.allow:
        return text, verdict
    logger.warning(
        "prompt omission: field=%s context=%s code=%s ruleset=%s chars=%d",
        field,
        context or "-",
        verdict.code,
        RULESET_VERSION,
        len(text or ""),
    )
    return "", verdict


#: ``json.dumps`` escapes every character that could break out of a quoted
#: field EXCEPT the two Unicode separators, which it emits raw under
#: ``ensure_ascii=False`` — and those are exactly the two a model reads as a
#: line break (the U+2028 payload this module's own corpus already carries).
_JSON_UNESCAPED_SEPARATORS = {"\u2028": "\\u2028", "\u2029": "\\u2029"}


def quote_for_prompt(text: str, *, field: str, context: str = "") -> tuple[str, Verdict]:
    """Screen a corpus paper title and return it as ONE quoted data token.

    The seam this closes: ``debate_engine._candidate_cards`` printed
    ``arXiv:2101.01234 "{title}"`` and ``portfolio_agent._format_strategies``
    printed ``title={paper_title}`` — both line-oriented, both interpolating a
    string this project does not author. arXiv metadata is third-party text
    (``arxiv_pipeline._doc_safe`` already says so, for the code-generation
    seam, #920) and only two of the four ingest paths normalise whitespace, so
    a title carrying ``\\n      sharpe=9.99`` forges a metric on the portfolio
    agent's own strategy line, and one carrying ``\\n[C6] … — cites
    arXiv:0000`` forges a debate card whose ids the anti-hallucination guard
    then trusts.

    Two mechanisms, doing two different jobs:

    * **Quoting is structural.** The return value is a JSON string literal —
      quotes, backslashes, line breaks and the two Unicode separators escaped
      — so the title occupies exactly one field of one line no matter what it
      contains. This is the ONE function in this module that returns a
      transformed string, and it is a rendering, not an edit: the argument is
      untouched, nothing is stored, and ``json.loads`` recovers the original
      byte-for-byte.
    * **Screening is semantic.** Escaping does nothing to a title that reads
      "Ignore all previous instructions", and nothing to a literal ``[C6]``.
      Those are refused, and a refused title is OMITTED — ``("", verdict)`` —
      leaving the caller to print the bare ``arXiv:<id>``, which is the
      fallback both call sites already have for a paper with no title. The
      omission is logged with its reason code.

    Returns ``("", verdict)`` for empty input too, so a caller can use one
    truthiness test for "nothing to print".
    """
    if not text:
        return "", _ALLOW
    verdict = screen(text, Surface.PAPER_TITLE)
    if not verdict.allow:
        logger.warning(
            "prompt omission: field=%s context=%s code=%s ruleset=%s chars=%d",
            field,
            context or "-",
            verdict.code,
            RULESET_VERSION,
            len(text),
        )
        return "", verdict
    quoted = json.dumps(text, ensure_ascii=False)
    for raw, escaped in _JSON_UNESCAPED_SEPARATORS.items():
        quoted = quoted.replace(raw, escaped)
    return quoted, verdict


# ── Versioned reason-code vocabulary ──────────────────────────────────────

#: Every code this module can return. Consumers (the corpus fixtures, the SSE
#: `reason_code`, the guidelines doc) treat this as the closed vocabulary.
ALL_CODES = frozenset(
    {
        "shape.empty",
        "shape.too_short",
        "shape.too_long",
        "shape.control_chars",
        "lang.no_words",
        "lang.mash",
        "inject.override_directive",
        "inject.prompt_leak",
        "inject.role_forgery",
        "inject.schema_forgery",
        "inject.code_fence",
        "inject.url",
        "inject.base64_blob",
        "struct.newline_in_card_field",
        "struct.delimiter_forgery",
        "pii.email",
        "pii.credential",
        "pii.national_id",
        "pii.payment_card",
        "screen.internal_error",
    }
)


def code_digest() -> str:
    """Stable 8-hex digest of the reason-code vocabulary."""
    return hashlib.sha256(",".join(sorted(ALL_CODES)).encode()).hexdigest()[:8]


#: ``<date>.<code_digest()>``. The suffix is checked against the live code set
#: by ``test_brief_screen.py``, so adding, renaming or deleting a code without
#: bumping this constant in the same commit is a red test — the version can
#: never quietly describe a different ruleset than the one that shipped.
RULESET_VERSION = "2026-09-03.76bdba0a"
