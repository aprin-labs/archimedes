# ADR: IPFS pinning is not live — hash-only `storagePointer`

> **Status:** Accepted
> **Date:** 2026-09-01
> **Owner:** Dan Browne
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** The Pinata pin path was code-complete but never wired into prod (`PINATA_JWT` absent from `infra/ecs.tf` `secrets{}`). Do we inject a live JWT, or remove the dead path so we do not claim pinning we do not do? (#1526, split from #714)
> **Related:** [`../specs/commit-reveal-trace-spec.md`](../specs/commit-reveal-trace-spec.md), [`../specs/ipfs-reasoning-traces-design-note.md`](../specs/ipfs-reasoning-traces-design-note.md), `CLAUDE.md` § fail-soft.

## TL;DR

**We do not pin reasoning traces to IPFS.** `ReasoningTraceRegistry.reveal` is called with an empty `storagePointer`. The on-chain keccak256 of the canonical trace bytes is the integrity anchor; the full JSON lives in the off-chain store (Postgres / Redis). Public copy must not say "IPFS-pointed" or "pinned to IPFS".

Re-enabling a pin is an **owner action**, not a code merge: seed a live JWT in SSM, add it to the backend Fargate `secrets{}` block, rebuild the pin client, and verify one CID on a public gateway. This cloud agent cannot inject that secret.

## Context

#714 sub-task B shipped `pinata_client.py` / `provenance_publisher.py` and wired the CID into `reveal()`'s `storagePointer`. `PINATA_JWT` appeared in `infra/scripts/setup-ssm-secrets.sh` only under "Forthcoming, as features land" and was **absent from `infra/ecs.tf`'s backend `secrets{}`**. Prod pinning was therefore silently off: every reveal stored a `storagePointer` that nothing had pinned. That is fail-soft on a provenance claim — the pattern [`architectural-principles.md`](../architectural-principles.md) § fail-soft forbids.

#1526 split the pin question out of #714 so commit-reveal could land on its own. The issue allowed exactly two outcomes: (a) wire the JWT into prod and prove a CID resolves, or (b) drop the dead path. A half-wired client that degrades to empty with a WARN is not an outcome.

Wiring needs a Pinata (or successor) JWT in AWS SSM under `/archimedes/prod/…` and a Terraform apply. That secret is not in this repo and is not something an unattended agent can seed. Removal is the honest outcome that does not wait on credentials.

## Decision

1. **Delete the pin client and its glue** (`chain/pinata_client.py`, `chain/provenance_publisher.py`). Do not leave a module that reads a JWT nobody injects.
2. **Reveal hash-only.** `agent_runner._reveal_trace` calls `trace_publisher.reveal(trace_id, trace, storage_pointer="")`. The contract still re-hashes `fullTraceContent` against the commitment; that binding does not need a CID.
3. **Do not claim pinning on public surfaces.** Architecture page, account-deletion copy, architecture map, and the commit-reveal spec describe off-chain storage + on-chain hash, not IPFS.
4. **Keep `ipfs_cid` on the trace API as a nullable historical field** — the name is leftover. Live reveals persist `None`. Reconciliation may copy a non-empty on-chain `storagePointer` if one already exists. Do not present it as a live pin.
5. **Re-enablement is owner-gated.** A future pin needs: a live JWT in SSM, an `ecs.tf` `secrets{}` entry, the pin client rebuilt, and a gateway fetch of one real reveal's CID. Until those four are true, do not reintroduce the path.

## Consequences

### Positive
- Public copy matches the live path. A visitor who reads "IPFS-pointed" would have been wrong; they no longer read that.
- The tick loop no longer spends a Pinata round-trip that always returned `None`.
- `grep` of the vendor JWT acronym under `backend/` and `infra/` is empty, which is the issue's machine-checkable acceptance for (b).

### Negative / costs we accept
- A third party cannot fetch a trace from a public gateway. They fetch it from us (the API / off-chain store) and recompute the keccak against the on-chain commitment. That is weaker availability than a pin, and we say so.
- The `ipfs_cid` field name on `TraceResponse` is a fossil. Renaming it is a breaking API change left for a dedicated pass; the schema docstring tells the truth.
- Re-enabling pinning is more work than flipping a secret, because the client is gone. That is deliberate: a client without a JWT was the defect.

## Alternatives considered
- **Wire `PINATA_JWT` into `ecs.tf` and SSM (issue outcome a) — rejected here:** this agent cannot seed a production JWT. Shipping Terraform that points at an unseeded parameter would fail the task at launch (`ResourceInitializationError`) or recreate the silent-degrade path if the secret were optional. Either is worse than removal.
- **Keep the client, degrade loudly when the JWT is unset — rejected:** that *was* the tree, and it is the fail-soft #1526 exists to close. A WARN in agent logs is not a public-honest absence.
- **Pass the keccak hex as `storagePointer` — rejected:** the hash is already on the commitment. Stuffing it into the pointer would look like a locator and resolve nowhere. Empty is the honest value.

## Ratification

Accepted 2026-09-01 as #1526 outcome (b). Owner (Dan) still owns any future JWT + SSM + `ecs.tf` wiring; this record does not authorize that apply.
