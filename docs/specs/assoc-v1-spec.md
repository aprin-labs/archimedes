# `assoc/v1` — the paper→strategy association contract

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-03
> **superseded-by:** —

> **Internal engineering reference. Not published to `docs.archimedes-arc.com`** —
> it is listed in `mkdocs.yml`'s `exclude_docs`, per the default-deny rule
> ([#1751](https://github.com/aprin-labs/archimedes/issues/1751)) and the owner's
> Q6 call on [#1688](https://github.com/aprin-labs/archimedes/pull/1688): *"a
> contract that lives only in a merged diff gets re-derived wrong."*

**An association is a record, not a string.** This spec is the contract every
writer of a paper→strategy link conforms to, and the reason the content hash
cannot see anything but paper identity.

Normative source: [`backend/archimedes/models/paper_assoc.py`](https://github.com/aprin-labs/archimedes/blob/main/backend/archimedes/models/paper_assoc.py).
Where this document and that module disagree, the module is right and this
document is a bug. Issue: [#1637](https://github.com/aprin-labs/archimedes/issues/1637).

## 1. Why the contract exists

A "source paper" used to be a string in three incompatible dict shapes, and the
*shape* was inside the content hash:

| writer | shape it emitted |
|---|---|
| `main.py` (example seed) | `{arxiv_id, title, authors}` |
| `agents/debate_engine.py` | `{arxiv_id, title: ""}` — since #1739, `{arxiv_id, title, mechanism, spec_elements}` (this one is a subset of `assoc/v1`, not a rival shape) |
| `api/strategies_routes.py` (fusion job) | `{arxiv_id, sha256: ""}` — route deleted by #1595 |
| `agents/generation_pipeline.py` (fixture) | `{arxiv_id, title}` |
| the `source_papers` column comment | `[{arxiv_id, sha256}]` — a fourth shape nothing emitted |

`strategy_store._compute_content_hash` hashed the **whole dicts**. So the same
paper set arriving through two writers produced two content hashes, two ids and
two "different" strategies; dedup and the paper→strategy back-index
(`strategies_by_paper`) both degraded; and the split-brain would have returned
the moment anyone backfilled a title onto one writer's output.

## 2. The record

```python
{
  "arxiv_id":       str | None,   # the id space
  "role":           "cited" | "considered",
  "content_hash":   str | None,   # corpus hash — NULL in production (#1091)
  "title":          str | None,
  "authors":        list[str],    # a list, possibly empty — never None
  "year":           int | None,
  "venue":          str | None,
  "doi":            str | None,
  "contribution":   str | None,   # the ATTRIBUTED statement a reader is shown
  "mechanism":      str | None,   # #1739: the model's raw claim
  "spec_elements":  list[str],    # #1739: validated indicator aliases backing it
  "selection_rank": int | None,   # 1-based rank in the selection list
  "semantic_score": float | None, # reranker score, when a rerank ran
  "schema":         "assoc/v1",
}
```

Key set is **closed and complete**: every writer emits all fourteen keys,
absent facts as `None` (or `[]` for the two collections), so `set(a) == set(b)`
for any two associations. A missing key is a bug, not a shrug. Unknown keys are
dropped by the normalizer — a shape that carried extra fields carried them
unhashed and unread.

### `mechanism` is not `contribution`

The two look alike and must not be collapsed:

- **`mechanism`** is the model's raw claim about what a paper supplies, and
  **`spec_elements`** are the indicator aliases from the *validated* spec that
  back it. Either can be present without the other.
- **`contribution`** is the attributed statement — the one a reader is shown.
  `generation_pipeline._passport_paper_refs` derives it as
  `mechanism if mechanism and spec_elements else None`, so a mechanism the spec
  never used stays an em-dash on the passport. Writing the unbacked prose into
  `contribution` would launder an unverified claim into a cited-paper column,
  which is the defect #1739 removed.

Both are durable on purpose: `_resolve_source_papers` carries them to the API
response, and `test_persisted_source_papers_carry_title_and_mechanism` pins that
the link survives the write. Neither is identity — see § 3.

**Honesty rules, baked into the normalizer rather than left to call sites:**

- `None` means "not recorded". `""` never does — a blank title, DOI or venue
  normalizes to `None`, so a renderer prints "unavailable" instead of an empty
  pair of quotes and a merge can tell "no value" from "value that is blank".
- `role` is closed. An unrecognised role **raises** rather than being coerced:
  a wrong role is a false provenance claim, and silently rewriting it to
  `"cited"` would manufacture one.
- Nothing fabricates. A missing hash stays `None`. The corpus's `content_hash`
  / `pdf_sha256` columns are NULL in production ([#1091]), so `None` is the
  *correct* answer until hydration lands, not a gap to fill.

[#1091]: https://github.com/aprin-labs/archimedes/issues/1091

### Where normalization happens

At **one choke point**, not at N call sites: `strategy_store.upsert_strategy`
runs `normalize_assocs` over whatever a writer handed it, so every row written
from here on holds `assoc/v1` whichever historical shape arrived. `main.py`'s
example seed builds a `StrategyRecord` directly, bypassing that choke point, so
it normalizes itself via `paper_ref_to_assoc`.

**On write only.** Rows stored before this landed keep their writer's legacy
shape, and neither PR-1 nor its migration rewrites one — that pass is PR-2's,
and holding it back is a deviation from the owner's PR-1 list that the PR calls
out for sign-off. So the column holds two shapes in the meantime, and exactly
one reader normalizes on the way out (`StrategyRecord.to_strategy_passport`,
via `cited`). `to_dict`, `strategies_by_paper` and `resolve_source_papers`
return the stored JSON verbatim. What makes that survivable is that those paths
address an entry only through `.get()` on keys every historical shape either
carries or omits — the full argument, and the standing cost, are in
`strategy_store`'s module docstring under "Legacy rows are not rewritten".

`_CandidateResult.source_papers` is the pre-store carrier, and it holds exactly
these keys — the `mechanism` / `spec_elements` pair included, because #1739 made
that link durable rather than request-scoped.

## 3. Identity: what the hash may see

```python
assoc_identity(assocs) -> [[handle, role], ...]   # sorted, de-duplicated
```

That projection, and nothing else, reaches `_compute_content_hash`'s canonical
JSON. Consequences, all of them intended:

- **Enrichment cannot fork a strategy.** Backfilling a title, author list,
  year, venue, DOI, contribution, mechanism, spec elements, rank or score
  leaves the hash, the id and the dedup behaviour unchanged. A title is a fact *about* a paper; it is not what
  makes the association a different association.
- **Order and duplicates do not matter.** Two writers listing the same papers
  in different orders — or one listing a paper twice — produce one identity.
- **`role` is part of identity.** A *considered* paper is not a cited one, and
  a strategy that cites a paper is not the strategy that merely looked at it.

### `assoc_handle` — the id space, and its documented cost

```
arxiv_id  →  "doi:<doi>"  →  "title:<case-folded title>"  →  None
```

Owner decision Q9 on #1688: **accepted as written.** A strict arXiv-only rule
would be cleaner, and it would silently drop **all 34 curated strategies**,
every one of which declares `PAPER_ARXIV_ID = None` and identifies its paper by
title (sometimes DOI) alone.

The cost, stated at the definition rather than discovered later: for a paper
with neither an arXiv id nor a DOI, **the title _is_ the identifier**, so
backfilling a better title moves that association's identity and therefore can
move the strategy's `content_hash`. That is bounded to curated rows, it does
not touch the arXiv-identified papers every generated strategy cites, and
`id` — the FK every other table joins on, and the key #1792's stored rigor
verdict is written under — never moves.

`passport_loader._ref_key` delegates to `assoc_handle` rather than keeping a
second copy of this precedence, so the store's idea of "the same paper" and the
passport's cannot drift apart.

## 4. Projections

| surface | rule |
|---|---|
| `StrategyRecord.source_papers` | the association list, verbatim `assoc/v1` |
| `StrategyRecord.to_strategy_passport().papers` | the **cited** subset only |
| `passport_paper_refs` | one row per cited association, all twelve fields |
| `PassportPaperRef.to_dict()` | wire shape; blank title → `null` |
| `PaperRefResponse` | the same, as the API sees it |
| `consulted_paper_hashes` | `"{arxiv_id}:{content_hash or ''}"` per cited paper |

A `"considered"` association records that the selector surfaced a paper, **not**
that the strategy is built on it. Putting one in `papers[]` or binding a
decision to it would claim provenance that does not exist.

### Null, not a stand-in

Three fallbacks were removed because each printed something in a slot labelled
as something else:

- the **strategy's own name** in the cited-paper title column (`_resolve_source_papers`);
- the **arXiv id** in the title column (`_passport_to_strategy_response._resolved_title`) —
  owner decision Q3: *"an arXiv id printed in a title slot is a small
  fabrication and this is a validation product"*;
- `""` as a title on a zero-paper passport (`paper_title`).

The one legitimate title fallback left is `strategy_store._with_display_title`,
which fills a blank `PaperRef.title` with the strategy name **for the live
signal evaluator only** — its legacy keyword path selects an evaluator from
`paper_title` and a blank one drops the strategy from the scan. It is
deliberately not applied on any wire projection.

## 5. Merge, never blind-delete

`ingest_passport(force_update=True)` used to `DELETE FROM passport_paper_refs`
and rebuild. The caller on the real-returns refresh path has **id + title
only**, so every backfilled author list, year, venue, DOI and contribution was
guaranteed to be wiped on the next metrics refresh — enrichment could not
survive by construction.

Three rules, in order (`passport_loader._merge_paper_refs`):

1. **Match, don't replace** — on `_ref_key`, i.e. on `assoc_handle`.
2. **Never overwrite a value with an absence** — a populated column survives an
   incoming ref that has nothing to say about it.
3. **Never blind-delete** — a stored ref the caller did not mention is dropped
   only when the caller demonstrably knows the full cited set (it passed a
   non-empty list). An **empty incoming list means "I don't know the papers"**,
   not "this strategy has none".

`role` is promoted `considered → cited` but never silently demoted, because a
caller that simply defaulted the field has not made a claim about it.

## 6. Verification: what a green check means

`GET /api/traces/{id}/verify` runs **two independent checks** and reports them
independently:

| field | question it answers |
|---|---|
| `is_verified` / `verification_mode` | were these bytes anchored on-chain? |
| `papers_verified` / `source_paper_verification` | does the corpus have the papers this decision cites? |

Neither is folded into the other: a trace can be correctly anchored while
citing a paper that has since left the corpus, and a reader needs to see that
rather than have it averaged into one boolean.

`papers_verified` is **tri-state**, and the reason is the same one that made
`verification_mode` tri-state in #1359:

| value | `mode` | meaning |
|---|---|---|
| `true` | `checked` | every claimed id was found (and any non-empty claimed hash matched) |
| `false` | `checked` | at least one id is missing, or a claimed hash disagrees |
| `null` | `no_papers_claimed` | the trace claims no papers — nothing was attempted |
| `null` | `corpus_unavailable` | the corpus is unreachable — nothing was attempted |

An outage must not read as a provenance failure, and it must not read as a pass.

**`true` means the corpus HAS these papers — it is not a hash comparison.**
Claimed hashes are empty in production (#1091), so `verify_source_papers`
treats an empty claim as "no hash asserted" and checks existence only. Nothing
synthesizes a hash on either side to make a comparison come out clean. Owner
decision Q8: *"the button must not say 'verified' unqualified — it says the
paper exists in the corpus, and the tri-state mode must be surfaced, not just
returned."* That copy lives in exactly one place,
`ui/src/trace-binding.js:sourcePapersCopy`.

## 7. Decisions recorded, so they are not re-litigated

| # | decision | call |
|---|---|---|
| Q1 | identity-only hashing | **yes**; the one-time re-stamp of existing rows is a separate, dry-run-gated change |
| Q3 | `title` / `paper_title` → `null`, arXiv-id-as-title fallback dropped | **yes** |
| Q4 | admit `"construction"` to `STRATEGY_REFERENCE_DECISION_TYPES` | **no** — #1595 deleted the writer; widening a provenance filter for a type nothing emits costs the only promise the constant makes |
| Q5 | converge `build_consulted_hashes` with `paper_trace.resolve_paper_hashes` | converge on the trace side's rule (empty suffix, not dropped id) — follow-up, not a blocker |
| Q8 | existence-only `papers_verified` | **acceptable, with the copy fixed** — see § 6 |
| Q9 | `assoc_handle` fallback chain | **accepted as written** — see § 3 |

Source: owner answers on [#1688](https://github.com/aprin-labs/archimedes/pull/1688), 2026-09-03.

## 8. Anti-goals

- Do **not** fabricate provenance to fill a column. `null` authors/DOI/venue
  stay `null`. No "unknown" placeholders, no inferred contributions.
- Do **not** claim a hash comparison the corpus cannot back (#1091).
- Do **not** re-anchor existing on-chain traces. Old traces keep their (wrong,
  signed) single-paper `consulted_paper_hashes` and are legacy by construction.
- Do **not** describe retrieval as semantic / embedding-based / knowledge-graph
  on any provenance surface (#778). Retrieval is **lexical**; the honest
  vocabulary is `rerank_mode` and `rerank_cap_hit`, and `semantic_score` is a
  field name for a reranker's own output that is `None` when no rerank ran.
