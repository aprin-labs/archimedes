// /privacy — the privacy notice for the open-source, local-first project.
//
// Rewritten 2026-11-01 (#1432) when the hosted site was shut down and the repo
// became local-first. The page it replaces described a hosted service: our
// accounts database, our mail, the privacy mailbox relay, payments, backups
// and edge logs. None of that is ours to describe now. This page describes the
// SOFTWARE: what an instance stores, and which outside services it contacts.
// If you change what the software stores or contacts, this page is part of
// the change. ui/test/policy-pages.test.js pins the key facts and the absence
// of the hosted-service claims; docs/claims-ledger.md § Policy pages records
// the evidence. The full inventory these lists come from, with file:line for
// every row, was taken on 2026-11-01 against origin/main b10453b2.
//
// What makes the load-bearing sentences true today:
//   - "receives nothing from your installation": there is no analytics or
//     error-reporting SDK in ui/, backend/ or auth/. Better Auth's telemetry
//     package (@better-auth/telemetry 1.6.25, auth/package-lock.json) stays
//     off unless BETTER_AUTH_TELEMETRY or options.telemetry.enabled is set
//     (its dist/index.mjs:360-361); auth/auth.js sets neither and no file in
//     the repo names the variable. Visitor counting writes only to the
//     instance's own Redis (backend/archimedes/services/funnel_store.py,
//     visitor_insights_store.py, telemetry_store.py), and the browser's
//     landing beacon posts to the instance's own API (ui/src/App.jsx).
//   - The stored-data bullets: auth_users / auth_accounts / auth_sessions
//     (ipAddress, userAgent) in backend/archimedes/models/account.py;
//     auth_email_deliveries (auth/delivery-log.js); api_keys salt +
//     secret_hash (models/api_key.py); payment_receipts / generation_credits,
//     written only when GENERATION_PAYMENT_REQUIRED is "true"
//     (services/generation_payment.py payment_required); paper decision
//     records in Postgres (models/paper_store.py paper_decision_traces) and
//     Redis (services/redis_state.py save_trace).
//   - Contacted with nothing configured:
//       Yahoo Finance: yfinance is the default vendor for both market-data
//         seams (services/market_data_provider.py provider_name), and the
//         boot warmup primes the Explore asset cache from it
//         (services/request_path_warmup.py, REQUEST_PATH_WARMUP defaults on).
//       Arc testnet public RPC: chain/client.py arc_rpc_url and
//         ui/src/chain-config.js DEFAULT_RPC_URL; ARC_ARC_RPC_URL and
//         VITE_ARC_RPC_URL override them.
//       Hugging Face: services/paper_rag.py loads all-MiniLM-L6-v2 with
//         FUSION_SEMANTIC_RETRIEVAL on by default; backend/Dockerfile
//         downloads the weights at build and runs with HF_HUB_OFFLINE=1.
//       jsDelivr: FastAPI's Swagger UI at /docs, on unless PUBLIC_DOMAIN is
//         set (backend/archimedes/main.py docs gate; nginx/nginx.conf CSP).
//   - Bedrock: .env.example sets LLM_PROVIDER=bedrock_converse, and
//     BedrockConverseBackend (services/llm_backend.py) uses it only when
//     boto3 resolves AWS credentials; otherwise the canned fallback.
//   - Email: auth/mailer.js uses SES only under EMAIL_MAILER=ses; the default
//     "console" mode writes the message to the sign-in service's log. There
//     is no SMTP transport.
//   - arXiv: only the ingest scripts call it (scripts/bulk_ingest_arxiv.py,
//     archimedes.scripts.hydrate_corpus); nothing in the running app calls
//     corpus_service.intake_from_arxiv or StrategyProvider.extract_from_paper.
//   - The CLI and the MCP server default to https://archimedes-arc.com
//     (cli/src/archimedes_cli/cli.py DEFAULT_API_URL,
//     mcp-server/src/archimedes_mcp/credentials.py DEFAULT_API_URL). When
//     those defaults change, delete the sentence that says so.
//   - No sentence here says what became of the former hosted service at
//     archimedes-arc.com or its data. The owner chose (2026-11-01) not to
//     wait on the teardown: add one only once the teardown has actually
//     deleted that data, and say only what it confirms.
export default function Privacy() {
	return (
		<div className="page-content policy-page">
			<header>
				<p className="public-kicker">Privacy</p>
				<h1>Privacy</h1>
				<p className="policy-meta">
					Last updated 2026-11-01 · Archimedes is open-source software published
					under the name APRIN&nbsp;Labs, which is not a registered company
				</p>
			</header>

			<section>
				<h2>Software you run yourself</h2>
				<p>
					Archimedes is software you run yourself. APRIN&nbsp;Labs runs no
					hosted version of it and receives nothing from your installation: the
					code sends no telemetry, analytics or error reports to the project or
					to anyone else. Your data stays where you run it, except what your
					instance sends to the services listed below.
				</p>
			</section>

			<section>
				<h2>What your instance stores</h2>
				<p>
					Your instance keeps its records in the Postgres database and the Redis
					cache that you run:
				</p>
				<ul>
					<li>
						<strong>Accounts:</strong> name, email address, a hash of the password
						(never the password itself), each sign-in session with the IP address
						and browser user-agent that created it, a record of the account emails
						sent, and API keys as salted hashes.
					</li>
					<li>
						<strong>Wallets</strong> linked to an account, by address.
					</li>
					<li>
						<strong>What you make:</strong> research briefs, generated strategies,
						backtests and rigor verdicts, and paper-trading deployments with the
						reasoning record of each decision.
					</li>
					<li>
						<strong>Payment receipts and credits,</strong> if you switch the
						payment rail on.
					</li>
					<li>
						<strong>Visitor counts:</strong> an anonymous visitor id (the
						archimedes_vid cookie) and the counters built from it, kept in your
						Redis.
					</li>
				</ul>
				<p>
					Every cookie and browser-storage key the web app writes, and what each
					one reveals, is listed on the{" "}
					<a href="/security#storage-disclosure">Security page</a>.
				</p>
			</section>

			<section>
				<h2>Outside services the software contacts</h2>
				<p>
					Each service below receives what your instance sends it and handles it
					under its own terms.
				</p>
				<h3>With nothing configured</h3>
				<ul>
					<li>
						<strong>Yahoo Finance,</strong> through the yfinance library: the
						default source of market prices and history, first contacted when the
						server starts.
					</li>
					<li>
						<strong>The Arc testnet public RPC,</strong> from the server and from
						your browser, for blockchain reads and the transactions you or your
						instance send. Set your own endpoint with ARC_ARC_RPC_URL and
						VITE_ARC_RPC_URL.
					</li>
					<li>
						<strong>Hugging Face,</strong> to download a public text-embedding
						model when it is not already cached. The Docker image downloads it
						when the image is built.
					</li>
					<li>
						<strong>jsDelivr,</strong> which serves the script for the API
						documentation page at /docs, if you open it.
					</li>
				</ul>
				<h3>Only when you configure them</h3>
				<ul>
					<li>
						<strong>The language-model provider you choose</strong> with
						LLM_PROVIDER: Amazon Bedrock, Anthropic, or an OpenAI- or
						Anthropic-compatible endpoint (a local Ollama stays on your machine).
						It receives your research briefs and the paper excerpts sent with
						them. The example .env file selects Bedrock, which is used only when
						AWS credentials are available.
					</li>
					<li>
						<strong>Market data and prices:</strong> Tiingo, and CoinGecko and
						Pyth for the optional price-oracle runner.
					</li>
					<li>
						<strong>Circle:</strong> developer-controlled wallets, the Gateway
						payment service, and passkey wallets in the browser.
					</li>
					<li>
						<strong>Google and GitHub</strong> sign-in.
					</li>
					<li>
						<strong>Amazon Web Services:</strong> SES for account email (otherwise
						each email is written to the sign-in service&rsquo;s log), and SSM and
						S3 if you point the instance at them.
					</li>
					<li>
						<strong>arXiv,</strong> when you run the paper-ingest scripts.
					</li>
				</ul>
				<p>
					Anything written to the blockchain is public and permanent. That
					includes, if you turn on PAPER_TRACE_ANCHOR and a deployment opts in,
					the reasoning record of each of its paper-trading decisions, with the
					holdings before and after.
				</p>
				<p>
					The web app serves its own fonts and loads no third-party scripts,
					fonts or analytics. One exception to these lists: the command-line
					client and the MCP server still default to archimedes-arc.com. Point
					them at your own instance with --api-url or the ARCHIMEDES_API_URL
					environment variable.
				</p>
			</section>

			<section>
				<h2>If you run it for other people</h2>
				<p>
					If you run an instance for other people, telling them what it collects,
					and which of these services it sends their data to, is your job. This
					page describes the software, not your deployment.
				</p>
			</section>

			<section>
				<h2>Questions</h2>
				<p>
					Questions about this page, or mistakes in it, go to the
					project&rsquo;s issue tracker:{" "}
					<a
						href="https://github.com/aprin-labs/archimedes/issues"
						target="_blank"
						rel="noopener noreferrer"
					>
						github.com/aprin-labs/archimedes/issues
					</a>
					. It is public, so please do not post personal details there.
				</p>
			</section>
		</div>
	);
}
