# ADR: GLM → AWS Bedrock LLM migration

> **Audience:** Archimedes team (decision owner: Dan)
> **Status:** Accepted
> **Date:** 2026-06-24
> **Owner:** Dan Browne
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** Which LLM provider for the live backend — GLM via z.ai's anthropic-compatible bridge, or AWS Bedrock?
> **Related:** CLAUDE.md § "LLM", `backend/archimedes/services/llm_backend.py` (`make_llm_backend`, the Converse backend).

## TL;DR

**AWS Bedrock is the live LLM, with Amazon Nova Micro the free-tier default via a multi-provider Converse backend** and a model cost-picker on the Generate page. GLM is removed from the default prod path (still selectable in the free-tier allowlist); BYOK and a local-Ollama single-user path are preserved. `response.model` is the provenance of record across the migration. The two Anthropic-on-Bedrock models (Haiku 4.5 / Sonnet 4.6) are pending AWS use-case activation (roadmap T3.8) before the paid tier has real models behind it.

## Context

The backend's LLM factory supported direct Anthropic (BYOK), GLM via z.ai's `anthropic_compatible` bridge, and local Ollama. Prod ran on GLM for cost/simplicity, but z.ai is a third-party SaaS bridge we don't control. The 2026-06-24 migration to Dan's own AWS account opened a cleaner path: Bedrock via IAM instance-role auth (no API keys in prod), and Bedrock's **Converse API**, which unifies the request/response shape across providers — removing per-provider SDKs. **Amazon Nova Micro** is the cheapest competitive text model and is invokable immediately (unlike Anthropic-on-Bedrock, which needs a one-time account-level use-case attestation).

## Decision

1. **`LLM_PROVIDER=bedrock_converse` is the live default**; model resolves from config, defaulting to Amazon Nova Micro. Bedrock auth is by AWS role, not static keys — the ECS task role today ([`infra/ecs.tf`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/infra/ecs.tf)), the EC2 instance role when this ADR was written.
2. **A multi-provider Converse abstraction** wraps the bedrock-runtime client so any Bedrock model is selectable via one API; it degrades gracefully when a model doesn't accept a system prompt in the Converse shape.
3. **Server-side free-tier model allowlist** governs which models a free user can pick (Nova family, open-source models, and GLM preserved as an option). Premium Anthropic-on-Bedrock models are gated behind the paid-tier entitlement (see [paid-tier gating], #723) and pending T3.8 activation.
4. **Provenance via `response.model`** — every completion records the model that actually answered, the source of truth across the GLM→Bedrock era.
5. **Backward-compatible dev paths** — `anthropic` (BYOK), `anthropic_compatible`, and `ollama` remain for dev/test; the factory falls back to a canned offline backend when a provider is unavailable.

## Consequences

### Positive
- **Removes a third-party bridge dependency** — Bedrock is first-party AWS infra under Dan's control.
- **Rational cost structure** — Nova Micro is cheap and adequate for free-tier synthesis/traces; stronger models are an upsell, not a cost on free traffic.
- **Model flexibility** — Converse decouples the UI picker from the SDK; adding a model is an allowlist change.
- **No consumer breakage** — the `LLMBackend` protocol + `make_llm_backend()` signature are unchanged; callers don't know which provider runs.

### Negative / costs we accept
- **Bedrock requires AWS/IAM** — local dev needs AWS creds (or the Ollama fallback).
- **Anthropic-on-Bedrock needs a one-time use-case form** (Dan) — until approved, premium tiers default to Nova (roadmap T3.8). The UI marks this "activating soon."
- **`.env.example` still defaults to `anthropic_compatible`** — stale vs the live `bedrock_converse`/Nova default; tracked as T3.10.

## Alternatives considered
- **Stay on GLM (status quo) — rejected** for infrastructure control: a third-party bridge is a live dependency we don't own; the AWS-account migration was the right moment to swap.
- **Anthropic API directly (paid Bedrock credits) — not chosen as default:** Converse enables the multi-model picker that a single-vendor API doesn't; BYOK remains available for dev.
- **Nova-only, no picker — rejected** for UX: users should see and choose the cost/performance tradeoff.

## Ratification

Decided; deployed 2026-06-24 (commit 199132c + PR #717). GLM stays selectable; Anthropic-on-Bedrock activation is queued (T3.8). See [aws-account-migration](aws-account-migration.md) for the account this runs on.
