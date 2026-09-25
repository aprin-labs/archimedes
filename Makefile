.PHONY: help setup wallet fund compile test deploy feed balance \
        up down logs pytest lint format ui-dev clean routes rotate-secret docs-check \
        check-local

# ─── Help (default) ──────────────────────────────────

help:
	@echo "Archimedes — common dev targets (run 'make <target>'):"
	@echo ""
	@echo "  Stack lifecycle:"
	@echo "    up           Migrate, build + start local postgres/redis/auth/backend/nginx"
	@echo "    down         Stop the docker stack"
	@echo "    logs         Tail backend logs (Ctrl-C to detach)"
	@echo ""
	@echo "  Python:"
	@echo "    pytest       Run the backend test suite (stack must be up first)"
	@echo "    lint         ruff check"
	@echo "    format       ruff format"
	@echo "    routes       Dump FastAPI route inventory"
	@echo ""
	@echo "  Local-mode contract:"
	@echo "    check-local  Verify this config is local mode — no runners, no prod secrets,"
	@echo "                 no ECR pull path (docs/local-vs-prod.md; needs no docker daemon)"
	@echo ""
	@echo "  Docs:"
	@echo "    docs-check   Run the docs gate locally (links + index block; staleness informs)"
	@echo ""
	@echo "  Frontend:"
	@echo "    ui-dev       Run the Vite dev server (ui/)"
	@echo ""
	@echo "  Contracts (Foundry):"
	@echo "    compile      forge build"
	@echo "    test         forge test -vv"
	@echo ""
	@echo "  Circle wallet + oracle (wallet-setup/):"
	@echo "    setup        npm install in wallet-setup/"
	@echo "    register     Register Circle entity secret"
	@echo "    wallet       Create Circle dev wallet"
	@echo "    fund         Open Circle wallet console (request testnet USDC)"
	@echo "    deploy       Run deploy.mjs"
	@echo "    feed         Push current prices into the on-chain oracle"
	@echo "    balance      Query wallet token balance"
	@echo "    rotate-secret  Rotate the Circle entity secret (security hygiene — see"
	@echo "                   backend/archimedes/chain/README.md § Rotating the Entity Secret)"
	@echo ""
	@echo "  Maintenance:"
	@echo "    clean        Remove __pycache__/.pytest_cache/.ruff_cache"

# ─── Setup ───────────────────────────────────────────

setup:
	cd wallet-setup && npm install

register:
	cd wallet-setup && node --env-file=../.env register-entity-secret.mjs

wallet:
	cd wallet-setup && node --env-file=../.env create-wallet.mjs

# Generates a fresh entity secret, registers it with Circle, test-signs a call,
# and overwrites CIRCLE_ENTITY_SECRET in .env — only once the new secret is
# proven live. Run this periodically (rotation hygiene) or immediately if the
# current secret may have leaked. See backend/archimedes/chain/README.md
# § "Rotating the Entity Secret" for the full runbook.
rotate-secret:
	cd wallet-setup && node --env-file=../.env rotate-secret.mjs

fund:
	open https://console.circle.com/wallets/dev/wallets

# ─── Contracts ───────────────────────────────────────

compile:
	cd contracts && forge build

test:
	cd contracts && forge test -vv

deploy:
	cd wallet-setup && node --env-file=../.env deploy.mjs

deploy-new:
	cd wallet-setup && node --env-file=../.env deploy-new.mjs

# ─── Oracle ──────────────────────────────────────────

feed:
	cd wallet-setup && node --env-file=../.env feed-price.mjs

setvault:
	cd wallet-setup && node --env-file=../.env setvault.mjs

# ─── UI ─────────────────────────────────────────────

ui-dev:
	cd ui && npm run dev

ui: ui-dev  # back-compat alias

# ─── Stack lifecycle ─────────────────────────────────

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f backend

# ─── Python ──────────────────────────────────────────

pytest:
	pytest -q --deselect backend/tests/test_api_routes.py::TestAgentRoutes::test_agent_status_redis_down_defaults \
	          --deselect backend/tests/test_api_routes.py::TestAdvisorRoutes::test_advisor_redis_unavailable

lint:
	ruff check backend/

format:
	ruff format backend/

routes:
	python -c "from archimedes.main import app; \
	    print('\n'.join(sorted(f'{sorted(r.methods or [])[0] if r.methods else \"-\":6} {r.path}' for r in app.routes)))"

# ─── Maintenance ─────────────────────────────────────

clean:
	find . -type d -name __pycache__ -not -path "./submodules/*" -not -path "./.claude/*" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -not -path "./submodules/*" -not -path "./.claude/*" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -not -path "./submodules/*" -not -path "./.claude/*" -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned __pycache__, .pytest_cache, .ruff_cache"

# ─── Queries ─────────────────────────────────────────

balance:
	cd wallet-setup && node --env-file=../.env -e "\
		import { initiateDeveloperControlledWalletsClient } from '@circle-fin/developer-controlled-wallets';\
		const c = initiateDeveloperControlledWalletsClient({ apiKey: process.env.CIRCLE_API_KEY, entitySecret: process.env.CIRCLE_ENTITY_SECRET });\
		const r = await c.getWalletTokenBalance({ id: process.env.WALLET_ID });\
		console.log(JSON.stringify(r.data, null, 2));"

# ─── Local-mode contract ─────────────────────────────

# Reads the committed compose files + `.env` (or `.env.example` when there is no
# `.env`) and checks the five things that separate local mode from production —
# see docs/local-vs-prod.md. Parses YAML rather than shelling `docker compose
# config`, so it runs with the docker daemon closed and CI can hold it.
check-local:
	@python3 scripts/check-local-mode.py

# ─── Docs ────────────────────────────────────────────

# Same three checks .github/workflows/docs-gate.yml runs, in the same order.
# links and index are blocking (non-zero exit); staleness is informational and
# never fails the target. Stdlib only — no venv, no install.
docs-check:
	@echo "── links ──────────────────────────────────────"
	@python3 .github/scripts/docs_links.py
	@echo "── index ──────────────────────────────────────"
	@python3 .github/scripts/docs_index.py
	@echo "── staleness (informational) ──────────────────"
	@python3 .github/scripts/docs_staleness.py || true
