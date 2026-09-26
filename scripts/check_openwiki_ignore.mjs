#!/usr/bin/env node
/**
 * Adversarial check for the `.openwikiignore` read boundary.
 *
 * `.openwikiignore` is an ALLOW-LIST: it excludes the whole repository and
 * re-includes the readable slice. That shape is easy to get subtly wrong —
 * gitignore semantics are last-match-wins, so a rule added in the wrong place
 * silently re-admits everything, and OpenWiki then reads (and can reproduce in
 * generated prose) source it was never meant to see.
 *
 * The slice is now codebase-wide (backend, analytics engine, UI, contracts,
 * infra, CI, docs) rather than the `docs/quant/` bootstrap slice, which raises
 * the stakes on two rule families that did not matter before: the deny rules
 * that keep bulk and generated content out of a much larger allow, and the
 * secret patterns that now have to hold across `infra/` — where a stray
 * `*.tfstate` really can contain a private key.
 *
 * A boundary is only a boundary if something has been shown to be REJECTED by
 * it. This script builds the paths that SHOULD be excluded — path traversal out
 * of the slice, alternate case, backslash spellings, secrets nested inside the
 * allowed slice — and asserts the matcher excludes them, alongside the paths
 * that must stay readable.
 *
 * It runs against OpenWiki's own matcher, not a reimplementation, so it cannot
 * pass while the real boundary fails. That means OpenWiki must be installed:
 *
 *   OPENWIKI_DIR=/path/to/dir/containing/node_modules/openwiki \
 *     node scripts/check_openwiki_ignore.mjs
 *
 * Exit 0 = every case behaved as required. Exit 1 = the boundary is wrong.
 * Exit 2 = OpenWiki is not installed (the check did not run; not a pass).
 *
 * WHEN TO RUN IT: any time `.openwikiignore` changes — in particular when
 * widening the allow-list, which is the documented way to grow the wiki (see
 * docs/decisions/tooling-adoptions-2026-08.md). Add the new slice's paths to
 * ALLOWED and a neighbouring out-of-slice path to DENIED first, and confirm the
 * DENIED one fails before the rule is added.
 *
 * Every directory `.openwikiignore` deliberately leaves OUT (backend/tests,
 * ui/test, auth, cli, wallet-setup, submodules, data, …) has a DENIED case
 * below. That is the point: those entries are what make a future widening a
 * deliberate act — the guard goes red, someone moves the case to ALLOWED, and
 * the change is visible in the diff instead of implied by a `!` line.
 */
import { fileURLToPath } from "node:url";
import path from "node:path";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const openWikiDir = process.env.OPENWIKI_DIR ?? path.join(REPO_ROOT, "..", "tools", "openwiki");
const matcherModule = path.join(
  openWikiDir,
  "node_modules",
  "openwiki",
  "dist",
  "agent",
  "openwiki-ignore.js",
);

let OpenWikiIgnore;
try {
  ({ OpenWikiIgnore } = await import(matcherModule));
} catch {
  console.error(
    `SKIPPED: could not load OpenWiki's ignore matcher from\n  ${matcherModule}\n` +
      `Install OpenWiki (npm install openwiki@0.4.3) and set OPENWIKI_DIR to the\n` +
      `directory holding its node_modules. This is a skip, not a pass.`,
  );
  process.exit(2);
}

/** Paths that MUST remain readable, as [path, isDirectory]. */
const ALLOWED = [
  // --- the implementation: the whole reason the slice was widened ---
  ["backend", true],
  ["backend/archimedes", true],
  ["backend/archimedes/main.py", false],
  ["backend/archimedes/services/live_rigor_gate.py", false],
  ["backend/archimedes/api/selection_bias_routes.py", false],
  ["backend/archimedes/chain/executor.py", false],
  ["backend/archimedes/models/strategy.py", false],
  ["backend/archimedes/agents/generation_pipeline.py", false],
  ["backend/archimedes/marketplace/settlement.py", false],
  ["backend/archimedes/interfaces/strategy.py", false],
  // Schema truth + the dependency/deploy contracts CLAUDE.md asserts about.
  ["backend/migrations", true],
  ["backend/migrations/env.py", false],
  ["backend/requirements.txt", false],
  ["backend/requirements-base.txt", false],
  ["backend/alembic.ini", false],
  ["backend/Dockerfile", false],
  // The rigor math the quant docs describe, and the curated strategy files.
  ["analytics-engine", true],
  ["analytics-engine/src/archimedes_analytics_engine/pbo.py", false],
  ["analytics-engine/src/archimedes_analytics_engine/walk_forward.py", false],
  ["analytics-engine/src/archimedes_analytics_engine/purged_kfold.py", false],
  ["analytics-engine/strategies", true],
  ["analytics-engine/strategies/appel_1979_macd.py", false],
  // Frontend source (its CSS and assets are denied below).
  ["ui", true],
  ["ui/src", true],
  ["ui/src/App.jsx", false],
  ["ui/src/featureFlags.js", false],
  ["ui/src/components/Generate.jsx", false],
  // Contracts, infra, CI, repo scripts.
  ["contracts", true],
  ["contracts/src/Vault.sol", false],
  ["contracts/test/Vault.t.sol", false],
  ["contracts/README.md", false],
  ["infra", true],
  ["infra/ecs.tf", false],
  ["infra/runbooks/disaster-recovery.md", false],
  ["infra/terraform.tfvars.example", false],
  ["scripts/check_openwiki_ignore.mjs", false],
  [".github", true],
  [".github/workflows/quality-gate.yml", false],
  [".github/scripts/mkdocs_hooks.py", false],
  // --- all of docs/, not just the bootstrap slice: the cross-check needs it ---
  ["docs", true],
  ["docs/quant", true],
  ["docs/quant/methodology.md", false],
  ["docs/quant/README.md", false],
  ["docs/quant/admission-criteria.md", false],
  ["docs/doc-index.md", false],
  ["docs/architecture.md", false],
  ["docs/api", true],
  ["docs/api/generation.md", false],
  ["docs/adr/non-custodial-vault-owner-agent.md", false],
  ["docs/decisions/tooling-adoptions-2026-08.md", false],
  // --- root contracts an alignment run has to read ---
  ["README.md", false],
  ["SETUP.md", false],
  ["mkdocs.yml", false],
  ["pytest.ini", false],
  ["ruff.toml", false],
  ["environment.yml", false],
  ["docker-compose.yml", false],
  // OpenWiki's own output tree and marker files, or the run cannot write.
  ["openwiki", true],
  ["openwiki/quickstart.md", false],
  ["openwiki/rigor/admission-gate.md", false],
  ["AGENTS.md", false],
  ["CLAUDE.md", false],
];

/** Paths that MUST be excluded, as [path, isDirectory]. */
const DENIED = [
  // --- deliberately deferred waves. Each of these is a directory a future
  //     widening would re-include; the case fails first, then moves to ALLOWED.
  ["backend/tests", true],
  ["backend/tests/test_api_routes.py", false],
  ["backend/scripts/import_daily_returns.py", false],
  ["backend/docs/benchmarks/index.md", false],
  ["ui/test", true],
  ["ui/test/roadmap-copy.test.js", false],
  ["ui/package.json", false],
  ["ui/vite.config.js", false],
  ["analytics-engine/tests/test_pbo.py", false],
  ["analytics-engine/pyproject.toml", false],
  ["auth/server.js", false],
  ["cli/main.py", false],
  ["wallet-setup/index.js", false],
  ["company-site/infra/main.tf", false],
  ["nginx/nginx.conf", false],
  ["reports/summary.md", false],
  ["skills/example/SKILL.md", false],
  ["tests/flows/test_journey.py", false],
  ["submodules/context-arc/README.md", false],
  ["data/corpus/manifest.jsonl", false],
  // --- dotted entries are excluded by `/*`; only `.github/` is re-included ---
  [".git/config", false],
  [".git/objects/ab/cdef", false],
  [".claude/settings.json", false],
  [".secrets.baseline", false],
  [".gitignore", false],
  [".pre-commit-config.yaml", false],
  // --- bulk, generated, and non-text content inside the allowed slice ---
  ["node_modules/foo/index.js", false],
  ["ui/src/node_modules/x.js", false],
  ["contracts/lib/forge-std/src/Test.sol", false],
  ["contracts/abis/Vault.json", false],
  ["contracts/src/generated/SyntheticUniverse.sol", false],
  ["contracts/broadcast/Deploy.s.sol/5042002/run-latest.json", false],
  ["contracts/out/Vault.sol/Vault.json", false],
  ["analytics-engine/artifacts/2026-08-31/run.json", false],
  ["backend/archimedes/data/rf/DGS3MO.csv", false],
  ["backend/archimedes/data/synthetic_universe.json", false],
  ["backend/archimedes/services/gmm_model.pkl", false],
  ["ui/src/App.css", false],
  ["ui/src/assets/flow-diagram.svg", false],
  ["docs/benchmarks/stockbench-vs-baselines.png", false],
  ["ui/package-lock.json", false],
  ["infra/.terraform.lock.hcl", false],
  // --- superseded history: an archived doc disagreeing with code is not a
  //     conflict, and 210KB of it would drown the real findings ---
  ["docs/archive", true],
  ["docs/archive/launch-execution-plan-2026-05-23.md", false],
  ["docs/archive/design.md", false],
  // --- secrets, including inside the newly-reachable infra/ tree ---
  ["docs/quant/secrets.env", false],
  ["docs/quant/.env", false],
  ["docs/quant/id_rsa.pem", false],
  ["docs/quant/deploy.key", false],
  ["openwiki/.env", false],
  ["openwiki/node_modules/x.js", false],
  ["backend/archimedes/.env", false],
  ["infra/terraform.tfstate", false],
  ["infra/terraform.tfstate.backup", false],
  ["infra/prod.tfvars", false],
  ["infra/archimedes-deploy-key.pem", false],
  ["infra/certs/site.crt", false],
  ["infra/certs/site.p12", false],
  ["ui/src/aws.key", false],
  [".github/workflows/deploy.key", false],
  // --- alternate spellings must not dodge an anchored or deferred rule ---
  ["./backend/tests/test_api_routes.py", false],
  ["/backend/tests/test_api_routes.py", false],
  ["docs/quant/../../auth/server.js", false],
  ["backend/archimedes/../tests/test_api_routes.py", false],
  ["../../../etc/passwd", false],
  ["BACKEND/TESTS/TEST_API_ROUTES.PY", false],
  ["backend\\tests\\test_api_routes.py", false],
];

const ignore = await OpenWikiIgnore.load(REPO_ROOT);
if (!ignore.isActive) {
  console.error("FAIL: .openwikiignore parsed to zero usable rules — enforcement is a no-op.");
  process.exit(1);
}

let failures = 0;
for (const [cases, expected, label] of [
  [ALLOWED, false, "readable"],
  [DENIED, true, "excluded"],
]) {
  for (const [candidate, isDirectory] of cases) {
    const actual = ignore.ignores(candidate, isDirectory);
    if (actual !== expected) {
      failures += 1;
      console.error(`FAIL: ${candidate} should be ${label}, matcher says ignores=${actual}`);
    }
  }
}

const total = ALLOWED.length + DENIED.length;
console.log(
  `.openwikiignore: ${total - failures}/${total} cases correct ` +
    `(${ALLOWED.length} must stay readable, ${DENIED.length} must be excluded), ` +
    `${ignore.rules.length} rules active.`,
);
process.exit(failures === 0 ? 0 : 1);
