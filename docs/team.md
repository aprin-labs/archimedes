# Team

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-07-28
> **superseded-by:** —

Roster, bios, timezones, and the sync window. **This is a decaying doc** — the roster
changes and this file is not on the hot path of any agent session. The part that changes
agent behaviour (the lead + coverage table, and the rule that lanes are descriptive rather
than prescriptive) lives in [`../CLAUDE.md`](../CLAUDE.md) § Team; everything else is here.

## Roster

Roster note (2026-06-24): the team grew through the Lepton community and Chuan Bai stepped
back (2026-06-24). Ages are author estimates pending team confirmation. Discord handles in
parentheses; the human handles are what shows up in the channel.

| Name | Age (est.) | Discord | Location | TZ | Role |
| --- | --- | --- | --- | --- | --- |
| **Dan Browne** | 37 | dbrowneup | Chicago | UTC-5 | **Owner of smart contracts + on-chain integration + infra (incl. AWS account `037613907429` and contract deploys), full-stack control.** Strategy engine (q-fin paper corpus, strategy-library curation), pitch architecture. Senior Scientist @ LanzaTech, PhD biochemistry. Day job — evenings/weekends. |
| **Marten Windler** | ~31 | Marten | Bremen | UTC+2 | Off-chain → on-chain integration via Arc CLI. Systems Engineering @ U. Bremen, ML-uncertainty B.Sc. thesis. ROS + Python/C++/Rust. Coordinator lean. |
| **Daniel Reis dos Santos** | early 20s | The go guy / Daniel [vibe] | Brazil | UTC-3 | Frontend ownership (React 19 + Vite 8 + UnoCSS). Backend engineer day-side. Go / Java / TypeScript, distributed systems, AWS, Terraform. Healthcare-ERP day role. |
| **Bogdan Sivochkin** | — | (GitHub `mnemonik-dev`) | — | — | Joined for Lepton. Blockchain and cryptography architect; 15+ yrs distributed systems; Solidity, Rust, ZK, account abstraction, secure smart-contract engineering (founder, Mnemonic protocol). Ran the full-tree technical audit ([PR #710](https://github.com/aprin-labs/archimedes/pull/710)); on-chain provenance / commit-reveal + IPFS ([issue #714](https://github.com/aprin-labs/archimedes/issues/714)). **Preferred two-eyes reviewer on contract changes when active (as of 2026-08 he is not — Dan is the sole required approver).** |
| **Önder Akkaya** | ~21 | Önder | Ankara | UTC+3 | Portfolio math (Kelly criterion / +EV, backtest evaluation, risk pricing). Statistics @ Hacettepe; [ASA Statistical Insight World Champion](https://www.linkedin.com/in/onder-akkaya/); President of [TİD-Genç](https://www.tid.org.tr/); trainee actuary. |
| **Ricardo Obregon Huaman** | — | (GitHub `rcrdoh`) | — | — | Nanopayment marketplace — x402-gated strategy access, Circle Gateway settlement, on-chain revenue split, per-user spend caps ([issue #713](https://github.com/aprin-labs/archimedes/issues/713)). |

## Ownership change (2026-06-24)

**Chuan Bai stepped back** — much less involved, not gone entirely. **Dan took on
smart-contract + on-chain-integration + infra ownership** (he owns the AWS account and
deploys the contracts himself). Where older docs route contract / infra review and approval
to Chuan, **it now routes to Dan (the human owner)**, with **Bogdan (`mnemonik-dev`) as the
preferred contract reviewer when active** and other teammates who know the contract stack
able to step in (as of 2026-08 Bogdan is not active — Dan is the sole required approver).
The funds-safety care is unchanged: contracts are high-stakes; two-eyes review is still
wise, and resumes when a second contract reviewer is available.

**The `t2o2` agent account is dormant (2026-08-03).** Chuan's autonomous agentic system
executed a large share of the mid-May–June issue backlog; it is not an active resource now
and no work is being dispatched to it. The `t2o2` coverage condition still present in
`quality-gate.yml`, and the `*-t2o2-issue.md` specs referenced from `docs/archive/`, are
historical record rather than a live capability — do not plan around either. Chuan remains in the Discord channel.

Chuan formerly led on-chain integration, smart contracts, infra, and architecture; Dan now
owns all four. Marten remains a backup on the on-chain layer. `api/` + `services/` +
`models/` + `interfaces/` under `backend/archimedes/` remain led by Daniel R.; the `chain/`
subdirectory is Dan's lead. Both layers share the `backend/archimedes/` Python package and
the FastAPI app boots them together via `main.py`.

## Availability

Two team members (Dan, Daniel R.) have demanding day roles and commit evenings/weekends.
Marten and Önder are students with flexible time.

**Daily sync window:** 13:00 UTC = 8am Chicago / 10am São Paulo / 14:00 London / 15:00
Bremen / 16:00 Ankara. Works across the whole team without anyone in unsocial hours.

**Schedule/flow owner:** Marten. Standups in `#standups` in Discord.

## Non-team contacts

- **Anuhya** (Discord: `moonshot` in the Canteen server, *NOT* the Chuan-moonshot in
  Archimedes Arcadia) is a **Canteen admin** who ran the Agora hackathon. Stakeholder /
  judge-adjacent, not a teammate.
